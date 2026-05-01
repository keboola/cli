"""Data-app service — Keboola Data Science API + ``keboola.data-apps``.

Owns the orchestration that the underlying APIs do *not* provide:

- the §9 redeploy contract (read latest Storage version, then PATCH with
  ``{desiredState=running, configVersion, restartIfRunning=true}`` together)
- per-project KMS encryption of git PATs via :class:`EncryptService`
- cleanup-in-finally on initial-deploy failure so a failed
  ``data-app create`` does not leak an empty deployment shell
- a poll loop that refuses to treat ``state == stopped`` as terminal while
  ``desiredState == running`` (writeup §8 pitfall row 1 — the transient
  ``stopped`` between the initial container teardown and runtime spin-up)

Naming convention: callers pass an integer-like ``app_id``; we coerce to
str so paths build cleanly regardless of input type.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any

from ..constants import DEFAULT_JOB_RUN_TIMEOUT
from ..data_science_client import DataScienceClient
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..models import ProjectConfig
from .base import BaseService, ClientFactory
from .encrypt_service import EncryptService

logger = logging.getLogger(__name__)


DataScienceClientFactory = Callable[[str, str], DataScienceClient]


def _default_ds_client_factory(stack_url: str, token: str) -> DataScienceClient:
    return DataScienceClient(stack_url=stack_url, token=token)


# ---------------------------------------------------------------------------
# Constants encoded from the writeup
# ---------------------------------------------------------------------------

DATA_APP_COMPONENT_ID = "keboola.data-apps"

VALID_TYPES: tuple[str, ...] = (
    "python-js",
    "python",
    "streamlit",
    "r",
    "python-databricks",
    "python-snowpark",
    "python-mlflow",
)
DEFAULT_TYPE = "python-js"

VALID_SIZES: tuple[str, ...] = ("tiny", "small", "medium", "large")
DEFAULT_SIZE = "tiny"

DEFAULT_AUTO_SUSPEND_SECONDS = 900

# Slug must match the URL-safe segment used in the auto-minted hostname.
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")

# Encrypted-secret prefixes produced by the Encryption API for project-scoped
# (KMS) ciphertext. The platform emits both ``KBC::ProjectSecure`` (legacy)
# and ``KBC::ProjectSecureGKMS`` (GCP); both are project-bound and decrypt
# only with the originating project's KMS key.
ENCRYPTED_PASSWORD_PREFIXES: tuple[str, ...] = (
    "KBC::ProjectSecure::",
    "KBC::ProjectSecureGKMS::",
    "KBC::ProjectSecureKMS::",
)

# Defence-in-depth caps for free-form user input. The platform may accept
# longer values, but kbagent refuses anything beyond these bounds at the
# service boundary so an external caller using the service directly cannot
# exfiltrate giant payloads or smuggle control characters into audit logs.
MAX_NAME_LENGTH = 255
MAX_DESCRIPTION_LENGTH = 65_536  # 64 KiB
MAX_GIT_REPO_LENGTH = 1024
MAX_GIT_BRANCH_LENGTH = 255
MAX_GIT_USERNAME_LENGTH = 255

POLL_INTERVAL_SECONDS = 5.0
TERMINAL_ERROR_STATE = "error"
RUNNING_STATE = "running"
STOPPED_STATE = "stopped"


def _has_control_chars(value: str, *, allow_whitespace: bool = False) -> bool:
    """Return True if ``value`` contains any ASCII control byte (0x00-0x1f / 0x7f).

    Set ``allow_whitespace=True`` to permit ``\\t \\n \\r`` (description markdown);
    everything else under 0x20 + 0x7f is still rejected.
    """
    allowed = {0x09, 0x0A, 0x0D} if allow_whitespace else set()
    for ch in value:
        code = ord(ch)
        if code in allowed:
            continue
        if code < 0x20 or code == 0x7F:
            return True
    return False


# URL schemes accepted for ``--git-repo``. Anything else (file://, gopher://,
# bare ssh syntax like ``git@host:path``) is rejected at the service
# boundary -- the data-app runner only ever talks https / http / git / ssh.
ALLOWED_GIT_REPO_SCHEMES: tuple[str, ...] = (
    "https://",
    "http://",
    "ssh://",
    "git://",
)


def _build_simple_auth_block() -> dict[str, Any]:
    """Authorization block for password-gated apps (writeup §11.2)."""
    return {
        "app_proxy": {
            "auth_providers": [{"id": "simpleAuth", "type": "password"}],
            "auth_rules": [
                {
                    "type": "pathPrefix",
                    "value": "/",
                    "auth_required": True,
                    "auth": ["simpleAuth"],
                }
            ],
        },
    }


def _redact_secret(value: Any) -> Any:
    """Replace encrypted ``#`` values with a placeholder for human output."""
    if isinstance(value, str) and value.startswith("KBC::"):
        return "<encrypted>"
    return value


def _redact_git_block(git: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the git block with the encrypted password redacted."""
    redacted = dict(git)
    if "#password" in redacted:
        redacted["#password"] = _redact_secret(redacted["#password"])
    return redacted


def _redact_storage_config(storage_config: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy the Storage config dict and redact any nested encrypted PAT.

    Used by ``get_data_app`` so the ``raw.storage_config`` echo cannot leak
    the encrypted git PAT verbatim into ``--json`` output. The redaction is
    cosmetic (the ciphertext is not a secret in the cryptographic sense --
    it can only be decrypted by Keboola's KMS), but defense-in-depth:
    keeping ciphertext out of consumed JSON limits its blast radius if a
    downstream consumer logs it.
    """
    if not isinstance(storage_config, dict):
        return storage_config
    redacted = dict(storage_config)
    configuration = redacted.get("configuration")
    if isinstance(configuration, dict):
        configuration = dict(configuration)
        parameters = configuration.get("parameters")
        if isinstance(parameters, dict):
            parameters = dict(parameters)
            data_app = parameters.get("dataApp")
            if isinstance(data_app, dict):
                data_app = dict(data_app)
                git = data_app.get("git")
                if isinstance(git, dict):
                    data_app["git"] = _redact_git_block(git)
                parameters["dataApp"] = data_app
            configuration["parameters"] = parameters
        redacted["configuration"] = configuration
    return redacted


class DataAppService(BaseService):
    """Lifecycle service for Keboola data apps.

    Wires together the Data Science API (deployment record), the Storage
    API (``keboola.data-apps`` config), and the Encryption API (per-project
    KMS for git PATs).
    """

    def __init__(
        self,
        config_store: Any,
        client_factory: ClientFactory | None = None,
        ds_client_factory: DataScienceClientFactory | None = None,
        encrypt_service: EncryptService | None = None,
    ) -> None:
        super().__init__(config_store=config_store, client_factory=client_factory)
        self._ds_client_factory = ds_client_factory or _default_ds_client_factory
        self._encrypt_service = encrypt_service or EncryptService(
            config_store=config_store, client_factory=client_factory
        )

    # ------------------------------------------------------------------
    # Public lifecycle methods (one per CLI subcommand)
    # ------------------------------------------------------------------

    def list_data_apps(
        self,
        aliases: list[str] | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Return data apps across one or more projects.

        Returns ``{"apps": [...], "errors": [...]}`` to match the envelope
        used by ``ConfigService.list_configs`` / ``StorageService.list_buckets``.
        Per-project failures are captured in ``errors``; they never abort
        the others.
        """
        projects = self.resolve_projects(aliases)

        def worker(
            alias: str, project: ProjectConfig
        ) -> tuple[str, list[dict[str, Any]], bool] | tuple[str, dict[str, str]]:
            ds_client = self._ds_client_factory(project.stack_url, project.token)
            storage_client = self._client_factory(project.stack_url, project.token)
            try:
                apps = ds_client.list_apps()
                config_names = self._fetch_data_app_config_names(storage_client, branch_id)
                merged: list[dict[str, Any]] = []
                for app in apps:
                    config_id = str(app.get("configId") or "")
                    merged.append(
                        {
                            "project_alias": alias,
                            "id": str(app.get("id", "")),
                            "config_id": config_id,
                            "name": config_names.get(config_id, app.get("name", "")),
                            "type": app.get("type", ""),
                            "state": app.get("state", ""),
                            "desired_state": app.get("desiredState", ""),
                            "config_version": str(app.get("configVersion", "") or ""),
                            "url": app.get("url", ""),
                            "size": app.get("size", ""),
                            "auto_suspend_after_seconds": app.get("autoSuspendAfterSeconds"),
                            "last_start_timestamp": app.get("lastStartTimestamp"),
                        }
                    )
                # Per-project sort dropped: the global sort below
                # subsumes it after we concatenate every worker's output.
                return (alias, merged, True)
            except KeboolaApiError as exc:
                return (
                    alias,
                    {
                        "project_alias": alias,
                        "error_code": str(exc.error_code),
                        "message": exc.message,
                    },
                )
            finally:
                ds_client.close()
                storage_client.close()

        successes, errors = self._run_parallel(projects, worker)
        all_apps: list[dict[str, Any]] = []
        for _alias, apps, _ok in successes:
            all_apps.extend(apps)
        all_apps.sort(key=lambda a: (a["project_alias"], a.get("id", "")))
        errors.sort(key=lambda e: e.get("project_alias", ""))
        return {"apps": all_apps, "errors": errors}

    def get_data_app(
        self,
        alias: str,
        app_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Merge the Data Science deployment record with the Storage config.

        The Data Science record is the source of truth for state / URL /
        configVersion; the Storage config carries slug / git settings /
        runtime size / human description. Callers normally want both.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        ds_client = self._ds_client_factory(project.stack_url, project.token)
        storage_client = self._client_factory(project.stack_url, project.token)
        try:
            app = ds_client.get_app(app_id)
            config_id = str(app.get("configId") or "")
            storage_config: dict[str, Any] = {}
            if config_id:
                try:
                    storage_config = storage_client.get_config_detail(
                        DATA_APP_COMPONENT_ID, config_id, branch_id=branch_id
                    )
                except KeboolaApiError as exc:
                    if exc.error_code != ErrorCode.NOT_FOUND:
                        raise
        finally:
            ds_client.close()
            storage_client.close()

        configuration = storage_config.get("configuration") or {}
        if isinstance(configuration, str):
            try:
                configuration = json.loads(configuration)
            except (ValueError, TypeError):
                configuration = {}
        parameters = configuration.get("parameters", {}) if isinstance(configuration, dict) else {}
        data_app_block = parameters.get("dataApp", {}) if isinstance(parameters, dict) else {}
        git_block = data_app_block.get("git", {}) if isinstance(data_app_block, dict) else {}

        return {
            "project_alias": alias,
            "id": str(app.get("id", "")),
            "config_id": config_id,
            "config_version_storage": str(storage_config.get("version", "") or ""),
            "config_version_deployed": str(app.get("configVersion", "") or ""),
            "name": storage_config.get("name", app.get("name", "")),
            "description": storage_config.get("description", ""),
            "type": app.get("type", ""),
            "state": app.get("state", ""),
            "desired_state": app.get("desiredState", ""),
            "url": app.get("url", ""),
            "size": app.get("size", "")
            or (
                configuration.get("runtime", {}).get("backend", {}).get("size", "")
                if isinstance(configuration, dict)
                else ""
            ),
            "auto_suspend_after_seconds": app.get(
                "autoSuspendAfterSeconds",
                parameters.get("autoSuspendAfterSeconds"),
            ),
            "last_start_timestamp": app.get("lastStartTimestamp"),
            "slug": data_app_block.get("slug", ""),
            "git": _redact_git_block(git_block) if git_block else {},
            "raw": {
                "deployment": app,
                "storage_config": _redact_storage_config(storage_config),
            },
        }

    def create_data_app(
        self,
        *,
        alias: str,
        name: str,
        description: str,
        slug: str,
        git_repo: str,
        git_branch: str = "main",
        git_public: bool = False,
        git_username: str | None = None,
        git_pat_plaintext: str | None = None,
        git_pat_encrypted: str | None = None,
        auth: str = "password",
        size: str = DEFAULT_SIZE,
        auto_suspend_after_seconds: int = DEFAULT_AUTO_SUSPEND_SECONDS,
        type_: str = DEFAULT_TYPE,
        branch_id: int | None = None,
        deploy: bool = True,
        wait: bool = False,
        timeout_seconds: float = DEFAULT_JOB_RUN_TIMEOUT,
        keep_on_failure: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """End-to-end create flow per writeup §6/§10/§11.

        Steps in order:

        1. Validate inputs (slug, size, type, git auth combination).
        2. POST a minimal shell to ``/apps`` to mint id + configId.
        3. If the repo is private, encrypt the PAT under THIS project's KMS
           via :class:`EncryptService`.
        4. PUT the full Storage config body, including the auto-injected
           ``parameters.id`` back-pointer (writeup §5 — required).
        5. If ``deploy``: read the latest Storage version, PATCH the
           deployment record with the §9 trio.
        6. If ``wait``: poll until terminal (running / error / timeout).
        7. On failure between (2) and (5), DELETE the orphan shell unless
           ``keep_on_failure`` is set.
        """
        self._validate_create_inputs(
            type_=type_,
            slug=slug,
            size=size,
            auth=auth,
            name=name,
            description=description,
            git_repo=git_repo,
            git_branch=git_branch,
            git_public=git_public,
            git_username=git_username,
            git_pat_plaintext=git_pat_plaintext,
            git_pat_encrypted=git_pat_encrypted,
        )

        if dry_run:
            return self._build_dry_run_payload(
                alias=alias,
                name=name,
                description=description,
                slug=slug,
                git_repo=git_repo,
                git_branch=git_branch,
                git_public=git_public,
                git_username=git_username,
                auth=auth,
                size=size,
                auto_suspend_after_seconds=auto_suspend_after_seconds,
                type_=type_,
                branch_id=branch_id,
                deploy=deploy,
            )

        projects = self.resolve_projects([alias])
        project = projects[alias]

        ds_client = self._ds_client_factory(project.stack_url, project.token)
        storage_client = self._client_factory(project.stack_url, project.token)

        shell: dict[str, Any] | None = None
        app_id: str | None = None
        config_id: str | None = None

        try:
            # Step 2: create the shell. Smallest body the API will accept.
            initial_config = {
                "parameters": {
                    "size": size,
                    "autoSuspendAfterSeconds": auto_suspend_after_seconds,
                    "dataApp": {"slug": slug},
                },
            }
            if auth == "password":
                initial_config["authorization"] = _build_simple_auth_block()

            shell = ds_client.create_app(
                type_=type_,
                name=name,
                description="",  # full description goes onto the Storage config below
                config=initial_config,
                branch_id=branch_id,
            )
            app_id = str(shell.get("id", ""))
            config_id = str(shell.get("configId", ""))
            if not app_id or not config_id:
                raise KeboolaApiError(
                    message="POST /apps response missing id or configId",
                    status_code=500,
                    error_code=ErrorCode.API_ERROR,
                    retryable=False,
                )

            # Step 3: encrypt PAT under target-project KMS if private repo.
            git_block = self._build_git_block(
                alias=alias,
                git_repo=git_repo,
                git_branch=git_branch,
                git_public=git_public,
                git_username=git_username,
                git_pat_plaintext=git_pat_plaintext,
                git_pat_encrypted=git_pat_encrypted,
            )

            # Step 4: PUT Storage config with full body + parameters.id back-pointer.
            full_config = self._build_storage_config_body(
                size=size,
                auto_suspend_after_seconds=auto_suspend_after_seconds,
                slug=slug,
                git_block=git_block,
                auth=auth,
                app_id=app_id,
            )
            storage_response = storage_client.update_config(
                component_id=DATA_APP_COMPONENT_ID,
                config_id=config_id,
                name=name,
                description=description,
                configuration=full_config,
                change_description=f"Initial data-app config via kbagent data-app create ({slug})",
                branch_id=branch_id,
            )
            storage_version = str(storage_response.get("version", "") or "")

            deployed_record: dict[str, Any] | None = None
            poll_result: dict[str, Any] | None = None

            if deploy:
                # Step 5: §9 redeploy contract.
                if not storage_version:
                    raise KeboolaApiError(
                        message=(
                            "Storage API did not return a version after PUT; "
                            "cannot pin configVersion for deploy."
                        ),
                        status_code=500,
                        error_code=ErrorCode.API_ERROR,
                        retryable=False,
                    )
                deployed_record = ds_client.patch_app(
                    app_id,
                    desired_state=RUNNING_STATE,
                    config_version=storage_version,
                    restart_if_running=True,
                )

                if wait:
                    poll_result = self._poll_until_terminal(
                        ds_client,
                        app_id,
                        target_desired_state=RUNNING_STATE,
                        timeout_seconds=timeout_seconds,
                    )

            return {
                "project_alias": alias,
                "id": app_id,
                "config_id": config_id,
                "name": name,
                "slug": slug,
                "type": type_,
                "size": size,
                "auto_suspend_after_seconds": auto_suspend_after_seconds,
                "auth": auth,
                "git": _redact_git_block(git_block),
                "branch_id": branch_id,
                "config_version": storage_version,
                "deployed": bool(deploy),
                "wait": bool(wait),
                "url": (deployed_record or shell).get("url", ""),
                "state": (poll_result or deployed_record or shell).get("state", ""),
                "desired_state": (poll_result or deployed_record or shell).get("desiredState", ""),
                "last_start_timestamp": (poll_result or deployed_record or {}).get(
                    "lastStartTimestamp"
                ),
                "message": self._format_create_message(
                    name=name,
                    auth=auth,
                    deployed=bool(deploy),
                    wait=bool(wait),
                    state=(poll_result or deployed_record or shell).get("state", ""),
                ),
            }
        except Exception:
            # Step 7: clean up the orphan shell unless caller asked us to
            # preserve it for forensics.
            if app_id and not keep_on_failure:
                try:
                    ds_client.delete_app(app_id)
                    logger.info("Cleaned up orphan data-app shell %s after failure", app_id)
                except Exception:
                    logger.exception("Failed to clean up orphan data-app shell %s", app_id)
            raise
        finally:
            ds_client.close()
            storage_client.close()

    def deploy_data_app(
        self,
        alias: str,
        app_id: str,
        config_version: str | None = None,
        wait: bool = False,
        timeout_seconds: float = DEFAULT_JOB_RUN_TIMEOUT,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Encapsulate the §9 redeploy contract.

        Default reads the latest Storage version and pins to it; pass
        ``config_version`` to deploy an older version. ALWAYS sends
        ``restartIfRunning=true`` together with ``configVersion`` -- the
        server returns HTTP 422 for any other shape.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        ds_client = self._ds_client_factory(project.stack_url, project.token)
        storage_client: Any | None = None
        try:
            app = ds_client.get_app(app_id)
            config_id = str(app.get("configId") or "")
            if not config_id:
                raise KeboolaApiError(
                    message=f"Data app {app_id} has no associated configId",
                    status_code=500,
                    error_code=ErrorCode.API_ERROR,
                    retryable=False,
                )

            effective_version = config_version
            if effective_version is None:
                # Only build the Storage client when we actually need to
                # read the latest version. Callers that pass an explicit
                # --config-version skip this path and the second client.
                storage_client = self._client_factory(project.stack_url, project.token)
                storage_config = storage_client.get_config_detail(
                    DATA_APP_COMPONENT_ID, config_id, branch_id=branch_id
                )
                effective_version = str(storage_config.get("version", "") or "")
            if not effective_version:
                raise KeboolaApiError(
                    message=(
                        f"Cannot resolve a Storage configVersion for app {app_id}; "
                        "Storage config returned no version."
                    ),
                    status_code=500,
                    error_code=ErrorCode.API_ERROR,
                    retryable=False,
                )

            deployed = ds_client.patch_app(
                app_id,
                desired_state=RUNNING_STATE,
                config_version=str(effective_version),
                restart_if_running=True,
            )
            poll_result: dict[str, Any] | None = None
            if wait:
                poll_result = self._poll_until_terminal(
                    ds_client,
                    app_id,
                    target_desired_state=RUNNING_STATE,
                    timeout_seconds=timeout_seconds,
                )
            return self._format_lifecycle_result(
                alias=alias,
                app_id=app_id,
                action="deploy",
                deployed=deployed,
                poll_result=poll_result,
                config_version=str(effective_version),
            )
        finally:
            ds_client.close()
            if storage_client is not None:
                storage_client.close()

    def start_data_app(
        self,
        alias: str,
        app_id: str,
        wait: bool = False,
        timeout_seconds: float = DEFAULT_JOB_RUN_TIMEOUT,
    ) -> dict[str, Any]:
        """Wake an auto-suspended app at its currently-pinned configVersion.

        Distinct from :meth:`deploy_data_app`: ``start`` does NOT bump the
        deployed version. This is the cheap restart path for the
        auto-suspend wake (writeup §8 pitfall row 2).
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        ds_client = self._ds_client_factory(project.stack_url, project.token)
        try:
            deployed = ds_client.patch_app(
                app_id,
                desired_state=RUNNING_STATE,
                restart_if_running=True,
            )
            poll_result: dict[str, Any] | None = None
            if wait:
                poll_result = self._poll_until_terminal(
                    ds_client,
                    app_id,
                    target_desired_state=RUNNING_STATE,
                    timeout_seconds=timeout_seconds,
                )
            return self._format_lifecycle_result(
                alias=alias,
                app_id=app_id,
                action="start",
                deployed=deployed,
                poll_result=poll_result,
            )
        finally:
            ds_client.close()

    def stop_data_app(
        self,
        alias: str,
        app_id: str,
        wait: bool = False,
        timeout_seconds: float = DEFAULT_JOB_RUN_TIMEOUT,
    ) -> dict[str, Any]:
        projects = self.resolve_projects([alias])
        project = projects[alias]
        ds_client = self._ds_client_factory(project.stack_url, project.token)
        try:
            deployed = ds_client.patch_app(app_id, desired_state=STOPPED_STATE)
            poll_result: dict[str, Any] | None = None
            if wait:
                poll_result = self._poll_until_terminal(
                    ds_client,
                    app_id,
                    target_desired_state=STOPPED_STATE,
                    timeout_seconds=timeout_seconds,
                )
            return self._format_lifecycle_result(
                alias=alias,
                app_id=app_id,
                action="stop",
                deployed=deployed,
                poll_result=poll_result,
            )
        finally:
            ds_client.close()

    def delete_data_app(self, alias: str, app_id: str) -> dict[str, Any]:
        """Delete the deployment AND the Storage config (cascade)."""
        projects = self.resolve_projects([alias])
        project = projects[alias]
        ds_client = self._ds_client_factory(project.stack_url, project.token)
        try:
            ds_client.delete_app(app_id)
        finally:
            ds_client.close()
        return {
            "project_alias": alias,
            "id": str(app_id),
            "deleted": True,
            "message": (
                f"Data app {app_id} deleted from project '{alias}'. "
                "Both the deployment record and the Storage config are gone; "
                "the URL is permanently retired."
            ),
        }

    def get_data_app_password(
        self,
        alias: str,
        app_id: str,
        manage_token: str,
    ) -> dict[str, Any]:
        """Return the auto-generated simpleAuth password.

        Requires both project Storage token and a Manage API token. The
        Manage token is passed per-call -- it is never persisted, never
        attached to the long-lived client, and never logged.
        """
        if not manage_token:
            raise KeboolaApiError(
                message=(
                    "Manage API token is required to read the data-app simpleAuth "
                    "password. Set KBC_MANAGE_API_TOKEN or run interactively."
                ),
                status_code=0,
                error_code=ErrorCode.INVALID_TOKEN,
                retryable=False,
            )
        projects = self.resolve_projects([alias])
        project = projects[alias]
        ds_client = self._ds_client_factory(project.stack_url, project.token)
        try:
            payload = ds_client.get_app_password(app_id, manage_token=manage_token)
        finally:
            ds_client.close()
        password = payload.get("password", "") if isinstance(payload, dict) else ""
        return {
            "project_alias": alias,
            "id": str(app_id),
            "password": password,
            "message": (
                f"Retrieved simpleAuth password for data app {app_id}. "
                "This password is auto-generated and cannot be rotated; "
                "delete and recreate the app to mint a new one."
            ),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_create_inputs(
        self,
        *,
        type_: str,
        slug: str,
        size: str,
        auth: str,
        name: str,
        description: str,
        git_repo: str,
        git_branch: str,
        git_public: bool,
        git_username: str | None,
        git_pat_plaintext: str | None,
        git_pat_encrypted: str | None,
    ) -> None:
        # Defence-in-depth length / control-char checks at the service
        # boundary. The service can be invoked directly (via --hint service
        # snippets or external Python callers) so we do not rely on the
        # command layer alone.
        for field_name, field_value, max_len, allow_ws in (
            ("--name", name, MAX_NAME_LENGTH, False),
            ("--description", description, MAX_DESCRIPTION_LENGTH, True),
            ("--git-repo", git_repo, MAX_GIT_REPO_LENGTH, False),
            ("--git-branch", git_branch, MAX_GIT_BRANCH_LENGTH, False),
            ("--git-username", git_username or "", MAX_GIT_USERNAME_LENGTH, False),
        ):
            if not isinstance(field_value, str):
                continue
            if len(field_value) > max_len:
                raise KeboolaApiError(
                    message=(
                        f"{field_name} exceeds the {max_len}-character limit "
                        "enforced at the service boundary."
                    ),
                    status_code=0,
                    error_code=ErrorCode.VALIDATION_ERROR,
                    retryable=False,
                )
            # Description allows tab/LF/CR (markdown); other fields reject any
            # control char including CR/LF (would break URL host derivation,
            # JSON serialization, or audit-log change descriptions).
            if _has_control_chars(field_value, allow_whitespace=allow_ws):
                raise KeboolaApiError(
                    message=(f"{field_name} contains disallowed control characters."),
                    status_code=0,
                    error_code=ErrorCode.VALIDATION_ERROR,
                    retryable=False,
                )

        # Reject git_repo URLs that don't use a known clone scheme. The
        # data-app runner only handles https / http / git / ssh; anything
        # else (file://, gopher://, etc.) is either nonsense or an SSRF /
        # local-file-read footgun and must not reach Storage.
        if git_repo and not any(git_repo.startswith(scheme) for scheme in ALLOWED_GIT_REPO_SCHEMES):
            raise KeboolaApiError(
                message=(
                    f"--git-repo must use one of {', '.join(ALLOWED_GIT_REPO_SCHEMES)}."
                    " Bare ssh syntax (git@host:path) and other schemes are not"
                    " accepted."
                ),
                status_code=0,
                error_code=ErrorCode.DATA_APP_INVALID_GIT,
                retryable=False,
            )

        if type_ not in VALID_TYPES:
            raise KeboolaApiError(
                message=(f"Invalid --type '{type_}'. Valid values: {', '.join(VALID_TYPES)}"),
                status_code=0,
                error_code=ErrorCode.VALIDATION_ERROR,
                retryable=False,
            )
        if size not in VALID_SIZES:
            raise KeboolaApiError(
                message=(f"Invalid --size '{size}'. Valid values: {', '.join(VALID_SIZES)}"),
                status_code=0,
                error_code=ErrorCode.VALIDATION_ERROR,
                retryable=False,
            )
        if auth not in ("password", "public"):
            raise KeboolaApiError(
                message=f"Invalid --auth '{auth}'. Valid values: password, public",
                status_code=0,
                error_code=ErrorCode.VALIDATION_ERROR,
                retryable=False,
            )
        if not SLUG_PATTERN.match(slug):
            raise KeboolaApiError(
                message=(
                    f"Invalid --slug '{slug}'. Slug must be lowercase alphanumeric "
                    "with hyphens, 2-64 chars, and cannot start or end with a hyphen."
                ),
                status_code=0,
                error_code=ErrorCode.VALIDATION_ERROR,
                retryable=False,
            )

        if git_public:
            if git_username or git_pat_plaintext or git_pat_encrypted:
                raise KeboolaApiError(
                    message=(
                        "--git-public is incompatible with --git-username / "
                        "--git-pat-env / --git-pat-file / --git-pat-encrypted."
                    ),
                    status_code=0,
                    error_code=ErrorCode.VALIDATION_ERROR,
                    retryable=False,
                )
        else:
            if not git_username:
                raise KeboolaApiError(
                    message="--git-username is required for private repositories.",
                    status_code=0,
                    error_code=ErrorCode.VALIDATION_ERROR,
                    retryable=False,
                )
            provided = sum(
                1 for v in (git_pat_plaintext, git_pat_encrypted) if v is not None and v != ""
            )
            if provided == 0:
                raise KeboolaApiError(
                    message=(
                        "Private repository requires a PAT. Pass one of: "
                        "--git-pat-env VAR / --git-pat-file PATH / "
                        "--git-pat-encrypted KBC::Project..."
                    ),
                    status_code=0,
                    error_code=ErrorCode.VALIDATION_ERROR,
                    retryable=False,
                )
            if provided > 1:
                raise KeboolaApiError(
                    message=(
                        "Specify exactly one of --git-pat-env / --git-pat-file / "
                        "--git-pat-encrypted; they are mutually exclusive."
                    ),
                    status_code=0,
                    error_code=ErrorCode.VALIDATION_ERROR,
                    retryable=False,
                )
            if git_pat_plaintext is not None and git_pat_plaintext.startswith("KBC::"):
                # A plaintext input that already looks like a Keboola
                # ciphertext is almost certainly someone pasting an
                # encrypted value into --git-pat-env / --git-pat-file by
                # mistake. EncryptService.encrypt() short-circuits any
                # ``KBC::``-prefixed value and would pass it through
                # unchanged, so a stray ciphertext from another project
                # could reach Storage and silently fail at runtime
                # decrypt. Reject up front.
                raise KeboolaApiError(
                    message=(
                        "--git-pat-env / --git-pat-file expect plaintext PATs. "
                        "The value starts with 'KBC::' which suggests an "
                        "already-encrypted ciphertext; if so, pass it via "
                        "--git-pat-encrypted instead so the prefix is validated."
                    ),
                    status_code=0,
                    error_code=ErrorCode.DATA_APP_INVALID_GIT,
                    retryable=False,
                )
            if git_pat_encrypted is not None and not any(
                git_pat_encrypted.startswith(p) for p in ENCRYPTED_PASSWORD_PREFIXES
            ):
                raise KeboolaApiError(
                    message=(
                        "--git-pat-encrypted must be a project-scoped Encryption "
                        f"API ciphertext (one of: {', '.join(ENCRYPTED_PASSWORD_PREFIXES)}). "
                        "Re-encrypt with `kbagent encrypt values --component-id "
                        f"{DATA_APP_COMPONENT_ID}` against THIS project; ciphertext "
                        "from another project will not decrypt (writeup §8)."
                    ),
                    status_code=0,
                    error_code=ErrorCode.VALIDATION_ERROR,
                    retryable=False,
                )

    def _build_git_block(
        self,
        *,
        alias: str,
        git_repo: str,
        git_branch: str,
        git_public: bool,
        git_username: str | None,
        git_pat_plaintext: str | None,
        git_pat_encrypted: str | None,
    ) -> dict[str, Any]:
        if git_public:
            return {
                "repository": git_repo,
                "private": False,
                "branch": git_branch,
            }

        # Plaintext PATs are encrypted under the target project's KMS via
        # EncryptService. Pre-encrypted ciphertext was prefix-validated in
        # _validate_create_inputs; EncryptService short-circuits anything
        # starting with ``KBC::`` (encrypt_service.py) and returns the value
        # unchanged, so we do not pay the encryption round-trip on a
        # caller-supplied ciphertext. We still re-validate the result below
        # so a misconfigured input cannot reach Storage as plaintext.
        secret_input = git_pat_plaintext or git_pat_encrypted or ""
        try:
            encrypted = self._encrypt_service.encrypt(
                alias=alias,
                component_id=DATA_APP_COMPONENT_ID,
                input_data={"#password": secret_input},
            )
        except ConfigError as exc:
            raise KeboolaApiError(
                message=f"Failed to prepare git PAT for encryption: {exc.message}",
                status_code=0,
                error_code=ErrorCode.ENCRYPTION_FAILED,
                retryable=False,
            ) from exc
        encrypted_pat = encrypted.get("#password", "")
        if not encrypted_pat or not any(
            encrypted_pat.startswith(p) for p in ENCRYPTED_PASSWORD_PREFIXES
        ):
            raise KeboolaApiError(
                message=(
                    "Encryption API did not return a project-scoped ciphertext "
                    "for the git PAT; refusing to write plaintext to Storage."
                ),
                status_code=0,
                error_code=ErrorCode.ENCRYPTION_FAILED,
                retryable=False,
            )
        return {
            "repository": git_repo,
            "private": True,
            "username": git_username or "",
            "#password": encrypted_pat,
            "branch": git_branch,
        }

    def _build_storage_config_body(
        self,
        *,
        size: str,
        auto_suspend_after_seconds: int,
        slug: str,
        git_block: dict[str, Any],
        auth: str,
        app_id: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "parameters": {
                "autoSuspendAfterSeconds": auto_suspend_after_seconds,
                "dataApp": {
                    "slug": slug,
                    "git": git_block,
                },
                "id": str(app_id),  # writeup §5: required back-pointer
            },
            "runtime": {"backend": {"size": size}},
        }
        if auth == "password":
            body["authorization"] = _build_simple_auth_block()
        return body

    def _build_dry_run_payload(
        self,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Render the three request bodies without making any API call."""
        size = kwargs["size"]
        slug = kwargs["slug"]
        auth = kwargs["auth"]
        auto_suspend = kwargs["auto_suspend_after_seconds"]
        type_ = kwargs["type_"]

        post_body = {
            "branchId": kwargs["branch_id"],
            "type": type_,
            "name": kwargs["name"],
            "description": "",
            "config": {
                "parameters": {
                    "size": size,
                    "autoSuspendAfterSeconds": auto_suspend,
                    "dataApp": {"slug": slug},
                },
            },
        }
        if auth == "password":
            post_body["config"]["authorization"] = _build_simple_auth_block()

        # We can't know the app_id pre-create; show the placeholder.
        git_block_preview: dict[str, Any]
        if kwargs["git_public"]:
            git_block_preview = {
                "repository": kwargs["git_repo"],
                "private": False,
                "branch": kwargs["git_branch"],
            }
        else:
            git_block_preview = {
                "repository": kwargs["git_repo"],
                "private": True,
                "username": kwargs["git_username"] or "<from input>",
                "#password": "<encrypted at runtime>",
                "branch": kwargs["git_branch"],
            }

        put_body = {
            "parameters": {
                "autoSuspendAfterSeconds": auto_suspend,
                "dataApp": {"slug": slug, "git": git_block_preview},
                "id": "<server-assigned numeric id>",
            },
            "runtime": {"backend": {"size": size}},
        }
        if auth == "password":
            put_body["authorization"] = _build_simple_auth_block()

        patch_body: dict[str, Any] = {}
        if kwargs["deploy"]:
            patch_body = {
                "desiredState": "running",
                "configVersion": "<latest after PUT>",
                "restartIfRunning": True,
            }

        return {
            "dry_run": True,
            "project_alias": kwargs["alias"],
            "requests": {
                "post_apps": post_body,
                "put_storage_config": put_body,
                "patch_apps": patch_body,
            },
            "message": (
                "Dry run -- no API calls made. "
                "Inspect the three request bodies above before re-running without --dry-run."
            ),
        }

    def _fetch_data_app_config_names(
        self,
        storage_client: Any,
        branch_id: int | None,
    ) -> dict[str, str]:
        """Map ``configId -> name`` for ``keboola.data-apps`` configs.

        Used by ``list_data_apps`` to enrich the thin Data Science index
        with the human-readable names that live on the Storage config.
        """
        try:
            configs = storage_client.list_component_configs(
                DATA_APP_COMPONENT_ID, branch_id=branch_id
            )
            return {str(cfg.get("id", "")): cfg.get("name", "") for cfg in configs}
        except Exception:
            return {}

    def _poll_until_terminal(
        self,
        ds_client: DataScienceClient,
        app_id: str,
        *,
        target_desired_state: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """Poll ``GET /apps/{id}`` until terminal.

        Terminal definition:

        - ``state == target_desired_state`` (the deploy succeeded), OR
        - ``state == "error"`` (the build / runtime failed).

        IMPORTANT: while ``desiredState == "running"``, observing
        ``state == "stopped"`` is NOT terminal -- the platform transitions
        ``created -> stopped -> starting -> running`` during initial
        deploy, and a naive poll exits prematurely (writeup §8 pitfall 1).
        """
        deadline = time.monotonic() + timeout_seconds
        last_record: dict[str, Any] = {}
        while True:
            last_record = ds_client.get_app(app_id)
            state = str(last_record.get("state", ""))
            if state == TERMINAL_ERROR_STATE:
                raise KeboolaApiError(
                    message=(
                        f"Data app {app_id} reached state=error during deploy. "
                        "See the app's Terminal Log in the Keboola UI for the build "
                        "error -- the Data Science API does not expose it as JSON."
                    ),
                    status_code=0,
                    error_code=ErrorCode.DATA_APP_BUILD_FAILED,
                    retryable=False,
                )
            if state == target_desired_state:
                return last_record
            if time.monotonic() >= deadline:
                raise KeboolaApiError(
                    message=(
                        f"Timed out after {timeout_seconds:.0f}s waiting for data app "
                        f"{app_id} to reach state={target_desired_state} "
                        f"(last observed: state={state}, desired={last_record.get('desiredState')})."
                    ),
                    status_code=0,
                    error_code=ErrorCode.DATA_APP_DEPLOY_TIMEOUT,
                    retryable=True,
                )
            time.sleep(POLL_INTERVAL_SECONDS)

    def _format_lifecycle_result(
        self,
        *,
        alias: str,
        app_id: str,
        action: str,
        deployed: dict[str, Any],
        poll_result: dict[str, Any] | None,
        config_version: str | None = None,
    ) -> dict[str, Any]:
        record = poll_result or deployed
        return {
            "project_alias": alias,
            "id": str(app_id),
            "action": action,
            "state": record.get("state", ""),
            "desired_state": record.get("desiredState", ""),
            "config_version": str(record.get("configVersion", "") or "") or (config_version or ""),
            "url": record.get("url", ""),
            "last_start_timestamp": record.get("lastStartTimestamp"),
            "message": (
                f"Data app {app_id} {action} requested in project '{alias}'. "
                f"state={record.get('state', '?')}, "
                f"desiredState={record.get('desiredState', '?')}."
            ),
        }

    def _format_create_message(
        self,
        *,
        name: str,
        auth: str,
        deployed: bool,
        wait: bool,
        state: str,
    ) -> str:
        if not deployed:
            return (
                f"Data app '{name}' created and configured. "
                "No deploy attempted (--no-deploy). Run `kbagent data-app deploy` "
                "to start the app."
            )
        if not wait:
            return (
                f"Data app '{name}' created and deploy requested. "
                "Use `kbagent data-app detail` to track state, or pass --wait "
                "to block until running."
            )
        # wait=True
        if state == RUNNING_STATE:
            tail = (
                " Run `kbagent data-app password` to retrieve the simpleAuth password."
                if auth == "password"
                else ""
            )
            return f"Data app '{name}' is running.{tail}"
        return f"Data app '{name}' deploy reached state={state}."
