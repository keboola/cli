"""Base service - shared infrastructure for multi-project parallel operations.

Provides resolve_projects(), worker pool management, and _run_parallel()
scaffold used by ConfigService, JobService, ProjectService, and LineageService.
"""

import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from ..auth.sentinel import is_session_token, parse_session_project_id, require_static_token
from ..client import KeboolaClient
from ..config_store import ConfigError, ConfigStore, project_not_found_error
from ..constants import ENV_MAX_PARALLEL_WORKERS, UNEXPECTED_ERROR_MAX_MESSAGE_LEN
from ..errors import ErrorCode
from ..models import ProjectConfig

logger = logging.getLogger(__name__)

ClientFactory = Callable[[str, str], KeboolaClient]

# Per-project error entries fall back to this code when the exception carries
# none of its own. `StrEnum`, so it still serialises as the plain string
# "UNEXPECTED_ERROR" that every multi-project service emits in its envelope.
UNEXPECTED_ERROR_CODE = ErrorCode.UNEXPECTED_ERROR


@dataclass(frozen=True)
class ResolvedProjectCredentials:
    """A project alias resolved to the ``(stack_url, token)`` a client needs.

    Returned by :func:`resolve_project_credentials` and the single-project
    services' ``_resolve_project`` helpers instead of a bare 2-tuple, so call
    sites read ``creds.stack_url`` / ``creds.token`` rather than relying on
    positional order (CONTRIBUTING.md "name them with dataclasses, not tuples").
    """

    stack_url: str
    token: str


def resolve_project_credentials(
    config_store: ConfigStore, alias: str
) -> ResolvedProjectCredentials:
    """Resolve ``alias`` to its stack URL + token, or raise :class:`ConfigError`.

    Shared by the single-project services (``token`` / ``stream`` / ``snapshot``)
    whose ``_resolve_project`` helpers were previously byte-identical. Uses the
    same short, actionable "not registered" message they already emitted (kept
    verbatim rather than switching to the richer
    :meth:`ConfigStore.project_not_found_error`, to preserve behavior).
    """
    project = config_store.get_project(alias)
    if project is None:
        raise ConfigError(f"Project alias '{alias}' is not registered. Run `kbagent project list`.")
    return ResolvedProjectCredentials(stack_url=project.stack_url, token=project.token)


def sanitize_unexpected_error(exc: BaseException) -> str:
    """Truncate an exception message to a safe length for JSON error envelopes.

    Any unhandled ``Exception`` surfaced as ``UNEXPECTED_ERROR`` may embed
    URLs with query params, internal state formatting, or (under
    ``--with-state``) fragments of runtime credentials such as OAuth
    refresh tokens. Truncating to
    :data:`keboola_agent_cli.constants.UNEXPECTED_ERROR_MAX_MESSAGE_LEN`
    characters keeps the envelope diagnostic without surfacing full
    response/state buffers. CWE-209.

    The full exception is still written to the debug log so operators
    retain the information when they explicitly opt in to verbose output.
    """
    raw = str(exc)
    if len(raw) > UNEXPECTED_ERROR_MAX_MESSAGE_LEN:
        return raw[:UNEXPECTED_ERROR_MAX_MESSAGE_LEN] + "..."
    return raw


def project_error_entry(
    alias: str,
    exc: BaseException,
    *,
    fallback_code: str | ErrorCode = UNEXPECTED_ERROR_CODE,
    message: str | None = None,
) -> dict[str, str]:
    """Build one per-project error entry, keeping a typed ``error_code`` intact.

    A `--json` consumer branches on ``error_code``, so a code the exception
    already carries must survive a catch-all handler: ``KeboolaApiError`` and
    the ``ConfigError`` subclasses that set one (``SessionAuthUnsupportedError``
    -> ``AUTH_NOT_SUPPORTED_ON_STACK``) are machine-readable contracts, not
    unexpected failures. ``error_code`` may be an :class:`ErrorCode` member or a
    plain string; ``str()`` normalises both to the wire form.

    ``fallback_code`` applies only to an exception without a code. Such a
    message is truncated (:func:`sanitize_unexpected_error`, CWE-209) because
    its content is unknown; a typed error's message is curated and passes
    through whole.
    """
    code = getattr(exc, "error_code", None)
    if code:
        return {
            "project_alias": alias,
            "error_code": str(code),
            "message": message if message is not None else str(exc),
        }
    return {
        "project_alias": alias,
        "error_code": str(fallback_code),
        "message": message if message is not None else sanitize_unexpected_error(exc),
    }


def find_default_branch_id(branches: list[dict[str, Any]]) -> int | None:
    """The id of the ``isDefault`` branch in a ``list_dev_branches()`` result.

    One home for the ``isDefault`` scan previously copy-pasted across
    services (config, sync, workspace, merge-request). Returns ``None`` when
    no branch is flagged -- what that means (error vs. fallback) stays the
    caller's decision. ``lib.py`` keeps its own loop deliberately: the SDK
    facade does not import the services layer.
    """
    for branch in branches:
        if branch.get("isDefault"):
            return int(branch["id"])
    return None


def default_client_factory(stack_url: str, token: str) -> KeboolaClient:
    """Create a KeboolaClient with the given stack URL and token.

    Static-token-only: fails fast with `SessionAuthUnsupportedError` on a
    `kbc-session://` sentinel rather than sending the literal sentinel string
    as a credential. Kept (name and signature unchanged) so existing
    importers and tests that inject this factory directly keep working --
    real runtime usage goes through `make_client_factory`'s bearer-aware
    factory instead (the new `BaseService` default).
    """
    require_static_token(token, feature="The static-token Storage API client")
    return KeboolaClient(stack_url=stack_url, token=token)


def make_client_factory(config_store: ConfigStore) -> ClientFactory:
    """Return a ``(stack_url, token) -> KeboolaClient`` factory, sentinel-aware.

    The factory signature stays 2-arg, so none of the ~150 existing call
    sites change shape: a `kbc-session://{project_id}` sentinel token is
    detected here, the project id is parsed out of the sentinel itself (the
    one datum the 2-arg signature otherwise lacks), and the client is built
    with `http_auth=BearerAuth(...)` instead of a static `X-StorageApi-Token`.
    A plain static token takes the unchanged, byte-identical path.

    `auth.state_store` / `auth.token_provider` are imported lazily inside the
    returned closure (not at module level) so the static-token startup path
    never pays for constructing the auth package's heavier dependencies
    (filelock, httpx client machinery) -- only a session-registered project
    ever reaches that branch.
    """

    def _factory(stack_url: str, token: str) -> KeboolaClient:
        if not is_session_token(token):
            return KeboolaClient(stack_url=stack_url, token=token)

        project_id = parse_session_project_id(token)
        if project_id is None:
            raise ConfigError(
                f"Malformed session sentinel token for stack {stack_url!r}: "
                "the project id could not be parsed. Re-run `kbagent auth login "
                "--register-projects` to repair the project's config entry."
            )

        from ..auth.state_store import AuthStateStore
        from ..auth.token_provider import BearerAuth, get_session_token_provider

        state_store = AuthStateStore.from_config_store(config_store)
        provider = get_session_token_provider(stack_url, state_store)
        return KeboolaClient(
            stack_url=stack_url,
            token="",
            http_auth=BearerAuth(provider, project_id),
        )

    return _factory


class BaseService:
    """Shared base for services that operate across multiple projects.

    Provides:
    - resolve_projects(): resolve aliases to ProjectConfig instances
    - _resolve_max_workers(): env var > config.json > default (10)
    - _run_parallel(): ThreadPoolExecutor scaffold for multi-project operations

    Uses dependency injection for config_store and client_factory.
    """

    def __init__(
        self,
        config_store: ConfigStore,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._config_store = config_store
        self._client_factory = client_factory or make_client_factory(config_store)

    def resolve_projects(self, aliases: list[str] | None = None) -> dict[str, ProjectConfig]:
        """Resolve project aliases to ProjectConfig instances.

        Args:
            aliases: Specific project aliases. If None or empty, returns all.

        Returns:
            Dict mapping alias to ProjectConfig.

        Raises:
            ConfigError: If any specified alias is not found.
        """
        config = self._config_store.load()

        if not aliases:
            return dict(config.projects)

        resolved: dict[str, ProjectConfig] = {}
        for alias in aliases:
            if alias not in config.projects:
                raise project_not_found_error(
                    alias, self._config_store.config_path, self._config_store.source
                )
            resolved[alias] = config.projects[alias]

        return resolved

    def _resolve_max_workers(self) -> int:
        """Resolve max parallel workers: env var > config.json > default (10).

        Returns:
            Positive integer for ThreadPoolExecutor max_workers. Always >= 1
            so a legacy config.json with ``max_parallel_workers: 0`` does not
            crash multi-project ops with ``ValueError`` from the executor
            (issue #269 sec-11). New configs are validated at the Pydantic
            layer (``ge=1``); this clamp guards loaded-from-disk values.
        """
        env_val = os.environ.get(ENV_MAX_PARALLEL_WORKERS)
        if env_val is not None:
            try:
                val = int(env_val)
                if val > 0:
                    return val
            except ValueError:
                pass

        config = self._config_store.load()
        return max(config.max_parallel_workers, 1)

    def _run_parallel(
        self,
        projects: dict[str, ProjectConfig],
        worker_fn: Callable[[str, ProjectConfig], tuple[Any, ...]],
    ) -> tuple[list[tuple[Any, ...]], list[dict[str, str]]]:
        """Run a worker function across projects in parallel using ThreadPoolExecutor.

        Each worker_fn receives (alias, project) and returns either:
        - A 3+-tuple on success (first element is alias)
        - A 2-tuple (alias, error_dict) on failure

        The distinction is made by tuple length: len == 2 means error.

        Args:
            projects: Dict mapping alias to ProjectConfig.
            worker_fn: Callable that processes a single project.

        Returns:
            Tuple of (successes, errors) where:
            - successes: list of 3+-tuples from successful workers
            - errors: list of error dicts with project_alias, error_code, message

        A worker that raises instead of returning an error tuple still lands in
        ``errors``, with its own ``error_code`` when it carries one (see
        :func:`project_error_entry`).
        """
        if not projects:
            return [], []

        successes: list[tuple[Any, ...]] = []
        errors: list[dict[str, str]] = []

        max_workers = min(len(projects), self._resolve_max_workers())
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_alias = {
                executor.submit(worker_fn, alias, project): alias
                for alias, project in projects.items()
            }

            for future in as_completed(future_to_alias):
                try:
                    result = future.result()
                except Exception as exc:
                    proj_alias = future_to_alias[future]
                    logger.debug(
                        "Worker error for project '%s': %s",
                        proj_alias,
                        exc,
                    )
                    errors.append(project_error_entry(proj_alias, exc))
                    continue

                if len(result) == 2:
                    _alias, error_dict = result
                    errors.append(error_dict)
                else:
                    successes.append(result)

        return successes, errors
