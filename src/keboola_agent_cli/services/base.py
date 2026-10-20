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
from ..config_store import (
    ConfigError,
    ConfigStore,
    project_not_found_error,
    project_not_registered_error,
)
from ..constants import (
    ENV_KBAGENT_PROJECT,
    ENV_MAX_PARALLEL_WORKERS,
    UNEXPECTED_ERROR_MAX_MESSAGE_LEN,
)
from ..effective_branch import TargetRole, record_branch, record_project
from ..errors import ErrorCode
from ..models import AppConfig, ProjectConfig
from ..project_ref import resolve_project_ref

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
    same short, actionable "not registered" message they already emitted
    (:func:`project_not_registered_error`) rather than the richer
    :meth:`ConfigStore.project_not_found_error`, to preserve behavior.
    """
    project = config_store.get_project(alias)
    if project is None:
        raise project_not_registered_error(alias)
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


def item_failure(
    exc: BaseException, /, *, fallback_code: str | ErrorCode = UNEXPECTED_ERROR_CODE, **keys: Any
) -> dict[str, Any]:
    """One failed item of a command that processes several items or projects (spec E5).

    ``keys`` are the entry's identity and the keys it always had (``error``,
    ``reason``). ``error_code``, ``retryable`` and ``message`` are added, so a
    ``--json`` consumer reads one shape. A code the exception carries is kept.
    A plain ``ConfigError`` gets ``CONFIG_ERROR``. Any other exception gets
    ``fallback_code``, and its message is truncated
    (:func:`sanitize_unexpected_error`, CWE-209) because its content is unknown.

    ``message`` is the field to show or log. The old ``error`` and ``reason``
    keys keep their previous raw text for compatibility.
    """
    code = getattr(exc, "error_code", None)
    if code is None and isinstance(exc, ConfigError):
        code = ErrorCode.CONFIG_ERROR
    if code:
        message = getattr(exc, "message", None) or str(exc)
    else:
        code = fallback_code
        message = sanitize_unexpected_error(exc)
    return {
        **keys,
        "error_code": str(code),
        "retryable": bool(getattr(exc, "retryable", False)),
        "message": message,
    }


def project_error_entry(
    alias: str,
    exc: BaseException,
    *,
    fallback_code: str | ErrorCode = UNEXPECTED_ERROR_CODE,
    message: str | None = None,
) -> dict[str, Any]:
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
    through whole. ``retryable`` comes from the exception (spec E5).
    """
    code = getattr(exc, "error_code", None)
    retryable = bool(getattr(exc, "retryable", False))
    if code:
        return {
            "project_alias": alias,
            "error_code": str(code),
            "message": message if message is not None else str(exc),
            "retryable": retryable,
        }
    return {
        "project_alias": alias,
        "error_code": str(fallback_code),
        "message": message if message is not None else sanitize_unexpected_error(exc),
        "retryable": retryable,
    }


def find_default_branch_id(branches: list[dict[str, Any]]) -> int | None:
    """The id of the ``isDefault`` branch in a ``list_dev_branches()`` result.

    The one home for the ``isDefault`` scan shared by the config, sync,
    workspace and merge-request services. Returns ``None`` when no flagged
    branch carries a usable (int-coercible) id -- an entry that cannot be
    coerced is skipped, never raised on, so a degenerate payload cannot
    crash a caller mid-operation. What ``None`` means (error vs. fallback)
    stays the caller's decision. ``lib.py`` keeps its own loop deliberately:
    the SDK facade does not import the services layer.
    """
    for branch in branches:
        if not branch.get("isDefault"):
            continue
        try:
            return int(branch["id"])
        except (KeyError, TypeError, ValueError):
            # Skipping is the right recovery, but not a silent one: the
            # callers then word "no default branch" for a project that DID
            # report one (followups F4). The log line is where the truth goes.
            logger.warning(
                "isDefault branch carries a non-numeric id %r -- skipped", branch.get("id")
            )
            continue
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


def make_session_aware_client_factory(
    config_store: ConfigStore, client_cls: Callable[..., Any]
) -> Callable[[str, str], Any]:
    """Return a ``(stack_url, token) -> client_cls`` factory that reaches a
    browser-login project the way ``KeboolaClient`` already does.

    For a plain static token it builds ``client_cls(stack_url, token)`` -- the
    unchanged, byte-identical path. For a `kbc-session://{project_id}` token it
    builds the client with ``http_auth=BearerAuth(...)`` and an empty token, so
    the request carries `Authorization: Bearer` + `X-KBC-ProjectId` instead of a
    static header. ``client_cls`` must accept a keyword-only ``http_auth`` (every
    ``BaseHttpClient`` subclass that forwards the argument does).

    `auth.state_store` / `auth.token_provider` are imported lazily inside the
    returned closure (not at module level) so the static-token path never pays
    for the auth package's heavier dependencies (filelock, httpx client
    machinery) -- only a session-registered project ever reaches that branch.
    """

    def _factory(stack_url: str, token: str) -> Any:
        if not is_session_token(token):
            return client_cls(stack_url=stack_url, token=token)

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
        return client_cls(
            stack_url=stack_url,
            token="",
            http_auth=BearerAuth(
                provider,
                project_id,
                refresh_on_401=getattr(client_cls, "BEARER_REFRESH_ON_401", True),
            ),
        )

    return _factory


def make_client_factory(config_store: ConfigStore) -> ClientFactory:
    """Return a ``(stack_url, token) -> KeboolaClient`` factory, sentinel-aware.

    The Storage-client specialisation of :func:`make_session_aware_client_factory`.
    The factory signature stays 2-arg, so none of the ~150 existing call sites
    change shape.
    """
    return make_session_aware_client_factory(config_store, KeboolaClient)


class NoImplicitProjectError(ConfigError):
    """No project could be chosen: none registered, or several and no default."""


def resolve_implicit_alias(
    config: AppConfig,
    env_value: str | None,
    *,
    env_fix: str,
    on_env_error: Callable[[str], None] | None = None,
) -> tuple[str, str]:
    """Pick the project a command uses when no project was named on it.

    The one copy of the implicit cascade: ``KBAGENT_PROJECT`` (``env``), then
    the persisted ``default_project`` pin (``pin``), then the only registered
    project (``sole``). :meth:`BaseService.resolve_pinned_alias` and the
    `auth` stack resolver both call it, so they cannot drift apart.

    Args:
        config: The loaded configuration.
        env_value: The ``KBAGENT_PROJECT`` value; empty or None means unset.
        env_fix: The fix appended to the message when ``env_value`` names no
            registered project.
        on_env_error: When given, an unusable ``env_value`` (not registered,
            or an ID registered under several aliases) is passed to it as a
            message and the cascade goes on without it. When None, it raises.

    Returns:
        Tuple of (alias, source).

    Raises:
        ConfigError: An unusable env value (without ``on_env_error``), or a
            pin that names an unregistered alias.
        NoImplicitProjectError: No project is registered, or several are and
            none is pinned.
    """
    if env_value:
        try:
            alias = resolve_project_ref(config.projects, env_value)
        except ConfigError as exc:
            if on_env_error is None:
                raise
            on_env_error(f"{ENV_KBAGENT_PROJECT}='{env_value}': {exc.message}")
        else:
            if alias in config.projects:
                return alias, "env"
            message = (
                f"{ENV_KBAGENT_PROJECT}='{env_value}' points to a project "
                "that is not registered (no alias or project ID matches it)."
            )
            if on_env_error is None:
                raise ConfigError(f"{message} {env_fix}")
            on_env_error(message)

    pinned = config.default_project
    if pinned:
        if pinned not in config.projects:
            raise ConfigError(
                f"Active project '{pinned}' is not registered. "
                "Run 'kbagent project use <alias>' to repair."
            )
        return pinned, "pin"

    if len(config.projects) == 1:
        (sole,) = config.projects.keys()
        return sole, "sole"

    if not config.projects:
        raise NoImplicitProjectError("No projects configured. Run 'kbagent project add' first.")

    raise NoImplicitProjectError(
        "Multiple projects configured and no active project. "
        "Pass --project <alias>, set KBAGENT_PROJECT, or run "
        "'kbagent project use <alias>'."
    )


def make_telemetry_client(
    config_store: ConfigStore, stack_url: str, token: str
) -> KeboolaClient | None:
    """Build a client for a best-effort usage event that never triggers a refresh.

    For a static token this mirrors `make_client_factory`. For a `kbc-session://`
    sentinel it reads a currently-fresh access token WITHOUT refreshing and stamps
    it with a non-refreshing bearer, so a stale on-disk token or a 401 can never
    make telemetry wait on the network or the cross-process refresh lease. Returns
    None when only a refresh would produce a usable token -- the caller then skips
    the event. Session projects stay covered whenever their token is already fresh.
    """
    if not is_session_token(token):
        return KeboolaClient(stack_url=stack_url, token=token)

    project_id = parse_session_project_id(token)
    if project_id is None:
        return None

    from ..auth.state_store import AuthStateStore
    from ..auth.token_provider import StaticBearerAuth, get_session_token_provider

    state_store = AuthStateStore.from_config_store(config_store)
    provider = get_session_token_provider(stack_url, state_store)
    access = provider.peek_access_token()
    if not access:
        return None
    return KeboolaClient(
        stack_url=stack_url, token="", http_auth=StaticBearerAuth(access, project_id)
    )


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
            for alias, project in config.projects.items():
                record_project(alias, "all", project)
            return dict(config.projects)

        resolved: dict[str, ProjectConfig] = {}
        for alias in aliases:
            if alias not in config.projects:
                raise project_not_found_error(
                    alias, self._config_store.config_path, self._config_store.source
                )
            resolved[alias] = config.projects[alias]
            record_project(alias, "explicit", resolved[alias])

        return resolved

    def production_branch_id(
        self, client: KeboolaClient, alias: str, *, role: TargetRole = "target"
    ) -> int:
        """The numeric ID of the default (production) branch of project ``alias``.

        ``--branch 0`` means production (``effective_branch.resolve_branch``),
        and the Storage client paths send a falsy branch ID to the production
        endpoint. Some calls need the ID itself: an endpoint that exists only
        under ``/v2/storage/branch/{id}/`` (``swap-tables``), or a match on the
        ``branchId`` of a Queue job (``job terminate --status``), or a source
        table branch inside a dev-branch request (``create-table
        --source-branch-id 0``). The branch is recorded as production with
        the ID, in ``role``.

        Raises:
            ConfigError: the project reports no default branch (``CONFIG_ERROR``,
                exit 5, HTTP 400 over ``kbagent serve``). It is a state of the
                project, not a failed API call, and the workspace and config
                services word the same case as ``ConfigError`` too.
        """
        branch_id = find_default_branch_id(client.list_dev_branches())
        if branch_id is None:
            raise ConfigError(
                f"Project '{alias}' reports no default branch, so kbagent cannot "
                f"find the production branch ID. Check the branches with "
                f"'kbagent branch list --project {alias}'."
            )
        record_branch(self._config_store, alias, branch_id, "production", role=role)
        return branch_id

    def resolve_pinned_alias(self, explicit: str | None = None) -> tuple[str, str]:
        """Resolve the effective project alias for a single-project operation.

        Precedence (first match wins):
        1. ``explicit`` argument (typically the CLI ``--project`` flag)
        2. ``KBAGENT_PROJECT`` env var
        3. Persisted ``default_project`` pin
        4. If exactly one project is registered, fall back to it (source=sole)
        5. Fail hard with ConfigError

        This is the single-project analog of ``resolve_projects()`` (which
        fans out to all projects). Every service that needs one implicit
        project must go through this cascade -- never a "first registered
        project" shortcut, which ignores the ``project use`` pin (issue #684).

        ``explicit`` and the env var may give a project ID instead of an
        alias (CLI-22, :func:`~keboola_agent_cli.project_ref.resolve_project_ref`);
        the returned value is always the alias. The CLI translates
        ``--project`` before a command runs, so for ``explicit`` this only
        matters to callers that pass their own value (telemetry, REST bodies).

        Args:
            explicit: Explicit alias (or project ID) from a CLI flag, or None.

        Returns:
            Tuple of (alias, source).

        Raises:
            ConfigError: If the resolved alias is not registered, or if none
                can be resolved.
        """
        config = self._config_store.load()

        if explicit:
            alias = resolve_project_ref(config.projects, explicit)
            if alias not in config.projects:
                raise project_not_found_error(
                    explicit, self._config_store.config_path, self._config_store.source
                )
            record_project(alias, "explicit", config.projects[alias])
            return alias, "explicit"

        alias, source = resolve_implicit_alias(
            config,
            os.environ.get(ENV_KBAGENT_PROJECT),
            env_fix="Use 'kbagent project add' or unset the env var.",
        )
        record_project(alias, source, config.projects[alias])
        return alias, source

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
