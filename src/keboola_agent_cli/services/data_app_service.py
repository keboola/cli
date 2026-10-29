"""Data-app service — Keboola Data Science ``/v2`` API + ``keboola.data-apps``.

Owns the orchestration that the underlying APIs do *not* provide:

- the redeploy contract (read the latest Storage version, then
  ``POST /v2/apps/{id}/deployments`` pinned to it)
- per-project KMS encryption of git PATs via :class:`EncryptService`
- cleanup-in-finally on initial-deploy failure so a failed
  ``data-app create`` does not leak an empty app
- a poll loop that waits for a deploy to serve production, not just to run

Naming convention: callers pass an integer-like ``app_id``; we coerce to
str so paths build cleanly regardless of input type.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime
from typing import Any

from ..client import KeboolaClient
from ..constants import DEFAULT_JOB_RUN_TIMEOUT
from ..data_science_client import DataScienceClient
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..models import ProjectConfig

# Re-exported so existing `from ...data_app_service import _x` call sites
# (tests included) keep resolving after the split into _data_app_bodies.py.
from ._data_app_bodies import (
    DATA_APP_COMPONENT_ID,
    ENCRYPTED_PASSWORD_PREFIXES,
    RESERVED_RUNTIME_ENV_VARS,
    _auth_block_for,
    _build_runtime_block,
    _check_text_field,
    _coerce_config_dict,
    _derive_runtime_env_var_name,
    _has_control_chars,
    _redact_git_block,
    _redact_storage_config,
    _secret_fingerprint,
    git_backfill_warning,
    resolve_effective_version,
)
from ._data_app_password import (
    DataAppPassword,
    app_branch_id,
    build_data_app_password,
    data_app_auth_kind,
    require_password_auth,
)
from .base import BaseService, ClientFactory, make_session_aware_client_factory, project_error_entry
from .encrypt_service import EncryptService

logger = logging.getLogger(__name__)


DataScienceClientFactory = Callable[[str, str], DataScienceClient]


def _production_config_version(app: dict[str, Any]) -> str:
    """The Storage config version the app's production serves; ``""`` when never deployed."""
    return str((app.get("production") or {}).get("actualConfigVersion") or "")


def _configured_size(configuration: dict[str, Any]) -> str:
    """The backend size a data-app Storage config names."""
    backend = (configuration.get("runtime") or {}).get("backend") or {}
    return str(backend.get("size") or (configuration.get("parameters") or {}).get("size") or "")


def _configured_auto_suspend(configuration: dict[str, Any]) -> Any:
    return (configuration.get("parameters") or {}).get("autoSuspendAfterSeconds")


def make_default_ds_client_factory(config_store: Any) -> DataScienceClientFactory:
    """Session-aware default DS client factory, shared by DataAppService and
    SyncService so both reach browser-login projects the same way."""
    return make_session_aware_client_factory(config_store, DataScienceClient)


# ---------------------------------------------------------------------------
# Constants encoded from the writeup
# ---------------------------------------------------------------------------

# The types ``POST /v2/apps`` creates.
VALID_TYPES: tuple[str, ...] = ("python-js", "streamlit")
DEFAULT_TYPE = "python-js"

VALID_SIZES: tuple[str, ...] = ("tiny", "small", "medium", "large")
DEFAULT_SIZE = "tiny"

DEFAULT_AUTO_SUSPEND_SECONDS = 900

# Slug must match the URL-safe segment used in the auto-minted hostname.
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")

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
RUNNING_STATE = "running"
SUSPENDED_STATE = "suspended"
# Stopped by a failure; only an explicit start clears it.
DISABLED_STATE = "disabled"
FAILED_DEPLOYMENT_STATE = "failed"


# URL schemes accepted for ``--git-repo``. Anything else (file://, gopher://,
# bare ssh syntax like ``git@host:path``) is rejected at the service
# boundary -- the data-app runner only ever talks https / http / git / ssh.
ALLOWED_GIT_REPO_SCHEMES: tuple[str, ...] = (
    "https://",
    "http://",
    "ssh://",
    "git://",
)

# Secret-key shape accepted under ``parameters.dataApp.secrets``. Must start
# with ``#`` (Keboola encryption convention); the rest must form a valid
# environment-variable identifier after the runtime translation rule
# (uppercased, ``-`` -> ``_``, ``#`` stripped). Cap at 64 chars so the
# derived env var stays under typical shell limits.
SECRET_KEY_PATTERN = re.compile(r"^#[A-Za-z][A-Za-z0-9_-]{0,63}$")

# Same shape as SECRET_KEY_PATTERN but the leading ``#`` is optional. The
# ``parameters.dataApp.secrets`` block legitimately holds BOTH ``#``-prefixed
# encrypted secrets and plain (unencrypted) env-var config values; read/remove
# operations must accept either, since ``secrets-list`` enumerates both. Only
# the write path (``secrets-set``) keeps requiring ``#`` -- it encrypts.
SECRET_OR_PLAIN_KEY_PATTERN = re.compile(r"^#?[A-Za-z][A-Za-z0-9_-]{0,63}$")


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
        self._ds_client_factory = ds_client_factory or make_default_ds_client_factory(config_store)
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
            # Both clients are built inside the try: the Data Science factory is
            # static-token-only, so on a session project its
            # AUTH_NOT_SUPPORTED_ON_STACK becomes this project's error entry
            # instead of aborting the whole listing under a generic code.
            ds_client: DataScienceClient | None = None
            storage_client: KeboolaClient | None = None
            try:
                ds_client = self._ds_client_factory(project.stack_url, project.token)
                storage_client = self._client_factory(project.stack_url, project.token)
                apps = ds_client.list_apps()
                configs = self._fetch_data_app_configs(storage_client, branch_id)
                merged: list[dict[str, Any]] = []
                for app in apps:
                    # Data apps only: an item of another component (or one that
                    # omits ``componentId``) is kept only when it is unclassifiable.
                    component_id = str(app.get("componentId") or "")
                    if component_id and component_id != DATA_APP_COMPONENT_ID:
                        continue
                    config_id = str(app.get("configId") or "")
                    config = configs.get(config_id, {})
                    configuration = _coerce_config_dict(config.get("configuration"))
                    merged.append(
                        {
                            "project_alias": alias,
                            "app_id": str(app.get("id", "")),
                            "config_id": config_id,
                            "component_id": component_id,
                            "name": config.get("name", ""),
                            "type": app.get("type", ""),
                            "state": app.get("state", ""),
                            "config_version": _production_config_version(app),
                            "url": app.get("url", ""),
                            "size": _configured_size(configuration),
                            "auto_suspend_after_seconds": _configured_auto_suspend(configuration),
                        }
                    )
                # Per-project sort dropped: the global sort below
                # subsumes it after we concatenate every worker's output.
                return (alias, merged, True)
            except (KeboolaApiError, ConfigError) as exc:
                return (alias, project_error_entry(alias, exc))
            finally:
                if ds_client is not None:
                    ds_client.close()
                if storage_client is not None:
                    storage_client.close()

        successes, errors = self._run_parallel(projects, worker)
        all_apps: list[dict[str, Any]] = []
        for _alias, apps, _ok in successes:
            all_apps.extend(apps)
        all_apps.sort(key=lambda a: (a["project_alias"], a.get("app_id", "")))
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

        configuration = _coerce_config_dict(storage_config.get("configuration"))
        parameters = configuration.get("parameters", {})
        data_app_block = parameters.get("dataApp", {}) if isinstance(parameters, dict) else {}
        git_block = data_app_block.get("git", {}) if isinstance(data_app_block, dict) else {}

        production = app.get("production") or {}
        return {
            "project_alias": alias,
            "app_id": str(app.get("id", "")),
            "config_id": config_id,
            "config_version_storage": str(storage_config.get("version", "") or ""),
            "config_version_deployed": _production_config_version(app),
            "deployment_id": str(production.get("actualDeploymentId") or ""),
            "name": storage_config.get("name", ""),
            "description": storage_config.get("description", ""),
            "type": app.get("type", ""),
            "state": app.get("state", ""),
            "url": app.get("url", ""),
            "size": _configured_size(configuration),
            "auto_suspend_after_seconds": _configured_auto_suspend(configuration),
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
        git_repo: str = "",
        git_branch: str = "main",
        git_public: bool = False,
        git_username: str | None = None,
        git_pat_plaintext: str | None = None,
        git_pat_encrypted: str | None = None,
        use_managed_git_repo: bool = False,
        auth: str = "password",
        size: str = DEFAULT_SIZE,
        auto_suspend_after_seconds: int = DEFAULT_AUTO_SUSPEND_SECONDS,
        type_: str = DEFAULT_TYPE,
        workspace: bool = True,
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
        2. POST a minimal app to ``/v2/apps`` to mint id + configId.
        3. If the repo is private, encrypt the PAT under THIS project's KMS
           via :class:`EncryptService`.
        4. PUT the full Storage config body, including the auto-injected
           ``parameters.id`` back-pointer (writeup §5 — required) and, unless
           ``workspace=False``, ``runtime.workspace.enabled: true`` -- the
           switch that grants the app Storage access (see
           :func:`_build_runtime_block`).
        5. If ``deploy``: create a deployment pinned to the Storage version
           step 4 wrote.
        6. If ``wait``: poll until the deployment serves production (or fails
           / times out).
        7. On failure between (2) and (5), DELETE the orphan app unless
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
            use_managed_git_repo=use_managed_git_repo,
        )

        # A managed git repo is provisioned empty at create time, so there is
        # nothing to deploy yet: the caller mints a credential, pushes code,
        # then runs `data-app deploy`. Force no-deploy regardless of --wait.
        if use_managed_git_repo:
            deploy = False

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
                use_managed_git_repo=use_managed_git_repo,
                auth=auth,
                size=size,
                auto_suspend_after_seconds=auto_suspend_after_seconds,
                type_=type_,
                workspace=workspace,
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
            initial_config["authorization"] = _auth_block_for(auth)

            shell = ds_client.create_app(
                type_=type_,
                name=name,
                description="",  # full description goes onto the Storage config below
                config=initial_config,
                branch_id=branch_id,
                use_managed_git_repo=use_managed_git_repo,
            )
            app_id = str(shell.get("id", ""))
            config_id = str(shell.get("configId", ""))
            if not app_id or not config_id:
                raise KeboolaApiError(
                    message="POST /v2/apps response missing id or configId",
                    status_code=500,
                    error_code=ErrorCode.API_ERROR,
                    retryable=False,
                )

            # Step 3: encrypt PAT under target-project KMS if private repo.
            # A managed repo has no external URL/PAT -- the Git Service owns the
            # link (app.managedGitRepoId), so we write NO git block into the
            # Storage config.
            git_block = (
                None
                if use_managed_git_repo
                else self._build_git_block(
                    alias=alias,
                    git_repo=git_repo,
                    git_branch=git_branch,
                    git_public=git_public,
                    git_username=git_username,
                    git_pat_plaintext=git_pat_plaintext,
                    git_pat_encrypted=git_pat_encrypted,
                )
            )

            # Step 4: PUT Storage config with full body + parameters.id back-pointer.
            full_config = self._build_storage_config_body(
                size=size,
                auto_suspend_after_seconds=auto_suspend_after_seconds,
                slug=slug,
                git_block=git_block,
                auth=auth,
                app_id=app_id,
                workspace=workspace,
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

            deployment: dict[str, Any] | None = None
            poll_result: dict[str, Any] | None = None

            if deploy:
                # Step 5: deploy the version step 4 wrote.
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
                deployment = ds_client.create_deployment(app_id, config_version=storage_version)

                if wait:
                    poll_result = self._poll_until_terminal(
                        ds_client,
                        app_id,
                        target_state=RUNNING_STATE,
                        deployment_id=str(deployment.get("id", "")),
                        timeout_seconds=timeout_seconds,
                    )

            state_record = poll_result or shell
            return {
                "project_alias": alias,
                "app_id": app_id,
                "config_id": config_id,
                "name": name,
                "slug": slug,
                "type": type_,
                "size": size,
                "auto_suspend_after_seconds": auto_suspend_after_seconds,
                "auth": auth,
                "workspace": bool(workspace),
                "git": _redact_git_block(git_block) if git_block else {},
                "use_managed_git_repo": use_managed_git_repo,
                "branch_id": branch_id,
                "config_version": storage_version,
                "deployed": bool(deploy),
                "deployment_id": str((deployment or {}).get("id", "")),
                "wait": bool(wait),
                "url": state_record.get("url", ""),
                "state": state_record.get("state", ""),
                "message": self._format_create_message(
                    name=name,
                    auth=auth,
                    deployed=bool(deploy),
                    wait=bool(wait),
                    state=state_record.get("state", ""),
                    use_managed_git_repo=use_managed_git_repo,
                ),
            }
        except Exception:
            # Step 7: clean up the orphan app unless caller asked us to
            # preserve it for forensics.
            if app_id and not keep_on_failure:
                try:
                    ds_client.delete_app(app_id)
                    logger.info("Cleaned up orphan data app %s after failure", app_id)
                except Exception:
                    logger.exception("Failed to clean up orphan data app %s", app_id)
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
        git_branch: str | None = None,
    ) -> dict[str, Any]:
        """Deploy the app at a pinned Storage config version and promote it to production.

        Default reads the latest Storage version and pins to it; pass
        ``config_version`` to deploy an older version.

        ``git_branch`` is the branch written into ``parameters.dataApp.git``
        when deploy backfills a pure managed repo; it has no effect otherwise.
        ``None`` (not given) means ``main`` for a never-deployed app and a
        VALIDATION_ERROR before any write for an app with run history.
        """
        # None (not given) is skipped: _check_text_field ignores non-strings.
        _check_text_field("--git-branch", git_branch, MAX_GIT_BRANCH_LENGTH, False, required=True)
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

            # An explicit --config-version always wins as an escape hatch; see
            # resolve_effective_version's docstring for the resolution rules.
            effective_version: str | None = config_version
            backfilled_branch: str | None = None
            if config_version is None:
                storage_client = self._client_factory(project.stack_url, project.token)
                resolved = resolve_effective_version(
                    ds_client, storage_client, app, app_id, config_id, branch_id, git_branch
                )
                # git_branch is None unless the backfill wrote the git block.
                effective_version, backfilled_branch = resolved.version, resolved.git_branch

            deployment = ds_client.create_deployment(app_id, config_version=effective_version)
            deployment_id = str(deployment.get("id", ""))
            if wait:
                app = self._poll_until_terminal(
                    ds_client,
                    app_id,
                    target_state=RUNNING_STATE,
                    deployment_id=deployment_id,
                    timeout_seconds=timeout_seconds,
                )
            result = self._format_lifecycle_result(
                alias=alias,
                app_id=app_id,
                action="deploy",
                app=app,
                config_version=str(deployment.get("configVersion") or effective_version),
            )
            result["deployment_id"] = deployment_id
            if backfilled_branch is not None:
                result["git_backfilled"] = True
                result["git_branch"] = backfilled_branch
                result["warnings"] = [git_backfill_warning(backfilled_branch)]
            return result
        finally:
            ds_client.close()
            if storage_client is not None:
                storage_client.close()

    def list_app_runs(self, alias: str, app_id: str, *, limit: int = 5) -> dict[str, Any]:
        """List a data app's recent deployment attempts (runs), newest first.

        Each run carries ``state`` plus, for failed attempts, a
        ``failure_reason`` and ``startup_logs`` -- including setup-phase failures
        (e.g. a git-clone error during ``app_setup``) that never produce
        container logs and so are invisible to ``data-app logs``. This is the
        canonical way to find out *why* a deploy reverted to ``stopped`` without
        the app ever serving.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        ds_client = self._ds_client_factory(project.stack_url, project.token)
        try:
            raw_runs = ds_client.list_app_runs(app_id, limit=limit)
        finally:
            ds_client.close()

        runs = [
            {
                "id": str(r.get("id", "")),
                "state": r.get("state", ""),
                "created_at": r.get("createdAt"),
                "started_at": r.get("startedAt"),
                "stopped_at": r.get("stoppedAt"),
                "failure_reason": r.get("failureReason"),
                "startup_logs": r.get("startupLogs"),
            }
            for r in raw_runs
        ]
        return {
            "project_alias": alias,
            "app_id": str(app_id),
            "runs": runs,
            "count": len(runs),
        }

    def start_data_app(
        self,
        alias: str,
        app_id: str,
        wait: bool = False,
        timeout_seconds: float = DEFAULT_JOB_RUN_TIMEOUT,
    ) -> dict[str, Any]:
        """Wake a suspended or disabled app at the deployment production already serves.

        Distinct from :meth:`deploy_data_app`: ``start`` does NOT bump the
        deployed version. ``409`` for an app that was never deployed.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        ds_client = self._ds_client_factory(project.stack_url, project.token)
        try:
            app = ds_client.resume_app(app_id)
            if wait:
                app = self._poll_until_terminal(
                    ds_client,
                    app_id,
                    target_state=RUNNING_STATE,
                    timeout_seconds=timeout_seconds,
                )
            return self._format_lifecycle_result(
                alias=alias, app_id=app_id, action="start", app=app
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
        """Suspend the app now; the next request to its URL wakes it again.

        ``/v2`` answers ``state=suspended`` at once, so ``wait`` has nothing
        to wait for.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        ds_client = self._ds_client_factory(project.stack_url, project.token)
        try:
            app = ds_client.suspend_app(app_id)
            return self._format_lifecycle_result(alias=alias, app_id=app_id, action="stop", app=app)
        finally:
            ds_client.close()

    def delete_data_app(self, alias: str, app_id: str) -> dict[str, Any]:
        """Delete the app: stop its deployments and move the Storage config to the trash."""
        projects = self.resolve_projects([alias])
        project = projects[alias]
        ds_client = self._ds_client_factory(project.stack_url, project.token)
        try:
            ds_client.delete_app(app_id)
        finally:
            ds_client.close()
        return {
            "project_alias": alias,
            "app_id": str(app_id),
            "deleted": True,
            "message": (
                f"Data app {app_id} deleted from project '{alias}'. "
                "Its deployments are stopped and the Storage config is in the trash; "
                "the URL is permanently retired."
            ),
        }

    def get_data_app_password(self, alias: str, app_id: str) -> DataAppPassword:
        """Read the password of a password-protected data app.

        Needs only the project token (static or session): the password
        endpoint checks just that the token belongs to the app's project.
        Reads the app record and its Storage config first and refuses
        (``VALIDATION_ERROR``) when the auth is not ``password`` -- the same
        check the Keboola UI makes before it asks for the password. The
        password comes back apart from the metadata; see
        :class:`DataAppPassword`.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        # ExitStack closes whatever was created, also when the second factory raises.
        with ExitStack() as clients:
            ds_client = self._ds_client_factory(project.stack_url, project.token)
            clients.callback(ds_client.close)
            storage_client = self._client_factory(project.stack_url, project.token)
            clients.callback(storage_client.close)
            app = ds_client.get_app(app_id)
            config_id = str(app.get("configId") or "")
            configuration: dict[str, Any] = {}
            if config_id:
                detail = storage_client.get_config_detail(
                    DATA_APP_COMPONENT_ID, config_id, branch_id=app_branch_id(app)
                )
                configuration = _coerce_config_dict(detail.get("configuration"))
            require_password_auth(str(app_id), data_app_auth_kind(configuration))
            payload = ds_client.get_app_password(app_id)
        return build_data_app_password(
            alias=alias, app_id=app_id, project=project, app=app, payload=payload
        )

    def get_app_logs(
        self,
        alias: str,
        app_id: str,
        *,
        lines: int | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        """Fetch the container log tail for a deployed data app.

        Wraps ``GET data-science/apps/{id}/logs/tail``. The two query
        parameters are mutually exclusive on the server -- the service
        rejects the combination locally with ``INVALID_ARGUMENT`` rather
        than letting it round-trip. Passing ``lines=None, since=None``
        returns the full current container buffer; this is the CLI's
        ``--lines 0`` semantics.

        The command layer enforces the same mutex with a clean exit-2
        usage error; this service-layer guard is the contract for
        programmatic callers (e.g. the ``kbagent serve`` route).

        Returns a dict with the raw text, the request echo (so callers
        can correlate envelopes to invocations), and a line count
        derived from splitting on newlines.

        Note: the log buffer can echo runtime secrets the app printed
        to stdout/stderr (tracebacks, debug ``os.environ`` dumps).
        ``--json`` callers piping into AI agent context should consider
        secret hygiene. The service does NOT post-process or attempt to
        mask the response -- false confidence is worse than honest
        passthrough; see ``gotchas.md``.
        """
        if lines is not None and since is not None:
            raise KeboolaApiError(
                message=(
                    "get_app_logs: 'lines' and 'since' are mutually exclusive; "
                    "pass exactly one (or neither for the full buffer)."
                ),
                status_code=0,
                error_code=ErrorCode.INVALID_ARGUMENT,
                retryable=False,
            )
        # Validation paths below are duplicated in the CLI command
        # (exit-2 USAGE_ERROR for a clean Click UX). The service-layer
        # guards are the contract for the ``kbagent serve``
        # GET /data-apps/{p}/{id}/logs route; without them, those
        # audiences would round-trip a 400 the server can phrase only
        # as "Invalid value".
        if lines is not None and lines < 0:
            raise KeboolaApiError(
                message=(
                    "get_app_logs: 'lines' must be 0 (full buffer) or a positive "
                    f"integer; got {lines}."
                ),
                status_code=0,
                error_code=ErrorCode.INVALID_ARGUMENT,
                retryable=False,
            )
        if since is not None:
            try:
                parsed_since = datetime.fromisoformat(since)
            except ValueError as exc:
                raise KeboolaApiError(
                    message=(
                        "get_app_logs: 'since' must be ISO 8601 "
                        f"(e.g. '2026-05-21T13:00:00Z'): {exc}"
                    ),
                    status_code=0,
                    error_code=ErrorCode.INVALID_ARGUMENT,
                    retryable=False,
                ) from None
            if parsed_since.tzinfo is None:
                raise KeboolaApiError(
                    message=(
                        "get_app_logs: 'since' must include a timezone (e.g. 'Z' "
                        "or '+00:00'); the Data Science endpoint rejects naive "
                        "datetimes with 'Invalid value'."
                    ),
                    status_code=0,
                    error_code=ErrorCode.INVALID_ARGUMENT,
                    retryable=False,
                )
        projects = self.resolve_projects([alias])
        project = projects[alias]
        ds_client = self._ds_client_factory(project.stack_url, project.token)
        try:
            text = ds_client.tail_app_logs(app_id, lines=lines, since=since)
        finally:
            ds_client.close()

        # ``splitlines()`` with no separator drops the trailing empty
        # string the final ``\n`` would create. A 3-line response with a
        # trailing newline is 3 lines, not 4.
        line_count = len(text.splitlines()) if text else 0
        return {
            "project_alias": alias,
            "app_id": str(app_id),
            "lines_requested": lines,
            "since_requested": since,
            "lines_returned": line_count,
            "text": text,
        }

    # ------------------------------------------------------------------
    # Secrets lifecycle (parameters.dataApp.secrets in the Storage config)
    # ------------------------------------------------------------------

    def _load_data_app_storage_config(
        self,
        *,
        ds_client: DataScienceClient,
        storage_client: Any,
        app_id: str,
        branch_id: int | None,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """Resolve ``configId`` from the Data Science app and load the Storage config.

        Returns ``(config_id, storage_envelope, body)`` where ``body`` is a
        deep-copy of ``storage_envelope.configuration`` so callers can
        mutate it freely. Mirrors the pattern at
        :meth:`deploy_data_app` (data_app_service.py:586-594).
        """
        app = ds_client.get_app(app_id)
        config_id = str(app.get("configId") or "")
        if not config_id:
            raise KeboolaApiError(
                message=f"Data app {app_id} has no associated configId",
                status_code=500,
                error_code=ErrorCode.API_ERROR,
                retryable=False,
            )
        envelope = storage_client.get_config_detail(
            DATA_APP_COMPONENT_ID, config_id, branch_id=branch_id
        )
        if not isinstance(envelope, dict):
            envelope = {}
        configuration = envelope.get("configuration")
        if not isinstance(configuration, dict):
            configuration = {}
        # Deep-copy so caller mutations don't reach the cached upstream.
        body = json.loads(json.dumps(configuration))
        return config_id, envelope, body

    def _read_secrets_block(self, body: dict[str, Any]) -> dict[str, str]:
        """Return ``parameters.dataApp.secrets`` from a config body, or ``{}``."""
        if not isinstance(body, dict):
            return {}
        params = body.get("parameters")
        if not isinstance(params, dict):
            return {}
        data_app = params.get("dataApp")
        if not isinstance(data_app, dict):
            return {}
        secrets = data_app.get("secrets")
        return dict(secrets) if isinstance(secrets, dict) else {}

    def set_data_app_secrets(
        self,
        *,
        alias: str,
        app_id: str,
        secrets: dict[str, str],
        branch_id: int | None = None,
        allow_plaintext_on_encrypt_failure: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Encrypt and write ``#``-prefixed secrets to the linked Storage config.

        Read-modify-write at the service layer. The Storage API's
        ``configuration`` field is a full-document overwrite; relying on
        Storage merge to preserve nested siblings under
        ``parameters.dataApp.secrets`` would clobber unrelated keys (the
        merge is shallow at the top level only). We GET the full config,
        modify the secrets sub-dict in place, and PUT the unchanged
        remainder + the new secrets back.

        Fail-closed: any encryption failure aborts before Storage is
        touched. ``allow_plaintext_on_encrypt_failure`` is bootstrap/debug
        only and emits a stderr warning when used.
        """
        if not secrets:
            raise KeboolaApiError(
                message="At least one --secret '#KEY=VALUE' is required.",
                status_code=0,
                error_code=ErrorCode.DATA_APP_INVALID_SECRET,
                retryable=False,
            )

        # Validate every key + value at the service boundary; we don't
        # trust the command layer to have caught everything.
        validated: dict[str, str] = {}
        for key, value in secrets.items():
            self._validate_secret_key(key)
            if not isinstance(value, str):
                raise KeboolaApiError(
                    message=(f"Secret '{key}' value must be a string; got {type(value).__name__}."),
                    status_code=0,
                    error_code=ErrorCode.DATA_APP_INVALID_SECRET,
                    retryable=False,
                )
            if value.startswith("KBC::"):
                raise KeboolaApiError(
                    message=(
                        f"Secret '{key}' value starts with 'KBC::', which suggests an "
                        "already-encrypted ciphertext. The --secret flag expects "
                        "plaintext; pass pre-encrypted values via --secrets-file or "
                        "re-encrypt under THIS project's KMS via "
                        "`kbagent encrypt values --component-id keboola.data-apps`."
                    ),
                    status_code=0,
                    error_code=ErrorCode.DATA_APP_INVALID_SECRET,
                    retryable=False,
                )
            validated[key] = value

        # Reserved-name warnings -- WARN (not BLOCKING). The platform
        # silently shadows colliding env vars at runtime. We still write
        # the secret so the user can recover by removing it, but we
        # surface the collision in the response.
        shadowed: list[str] = sorted(
            _derive_runtime_env_var_name(key)
            for key in validated
            if _derive_runtime_env_var_name(key) in RESERVED_RUNTIME_ENV_VARS
        )

        projects = self.resolve_projects([alias])
        project = projects[alias]
        ds_client = self._ds_client_factory(project.stack_url, project.token)
        storage_client = self._client_factory(project.stack_url, project.token)

        try:
            config_id, envelope, current_body = self._load_data_app_storage_config(
                ds_client=ds_client,
                storage_client=storage_client,
                app_id=str(app_id),
                branch_id=branch_id,
            )
            existing_secrets = self._read_secrets_block(current_body)

            unchanged = sorted(
                _derive_runtime_env_var_name(k) for k in existing_secrets if k not in validated
            )

            if dry_run:
                preview_secrets = dict(existing_secrets)
                for key in validated:
                    preview_secrets[key] = "<encrypted at runtime>"
                preview_body = self._merge_secrets_into_body(current_body, preview_secrets)
                return {
                    "dry_run": True,
                    "project_alias": alias,
                    "app_id": str(app_id),
                    "config_id": config_id,
                    "secrets_set": sorted(_derive_runtime_env_var_name(k) for k in validated),
                    "secrets_unchanged": unchanged,
                    "shadowed_by_runtime": shadowed,
                    "encryption_request_keys": sorted(validated.keys()),
                    "put_storage_config_preview": _redact_storage_config(
                        {"configuration": preview_body}
                    ),
                    "message": (
                        "Dry run -- no API calls made. Inspect the encryption "
                        "request keys and the proposed Storage PUT body above."
                    ),
                }

            # Encrypt every plaintext value under THIS project's KMS.
            try:
                encrypted = self._encrypt_service.encrypt(
                    alias=alias,
                    component_id=DATA_APP_COMPONENT_ID,
                    input_data=validated,
                )
            except ConfigError as exc:
                raise KeboolaApiError(
                    message=f"Failed to prepare secrets for encryption: {exc.message}",
                    status_code=0,
                    error_code=ErrorCode.ENCRYPTION_FAILED,
                    retryable=False,
                ) from exc

            # Validate every returned ciphertext starts with a project-scoped
            # prefix. Mirror the fail-closed check from _build_git_block at
            # data_app_service.py:1000-1008.
            problems: list[str] = []
            for key, ciphertext in encrypted.items():
                if not isinstance(ciphertext, str) or not any(
                    ciphertext.startswith(p) for p in ENCRYPTED_PASSWORD_PREFIXES
                ):
                    problems.append(key)
            if problems and not allow_plaintext_on_encrypt_failure:
                raise KeboolaApiError(
                    message=(
                        "Encryption API did not return a project-scoped ciphertext "
                        f"for key(s): {', '.join(sorted(problems))}. Refusing to "
                        "write plaintext to Storage. Re-run with "
                        "--allow-plaintext-on-encrypt-failure for bootstrap/debug ONLY."
                    ),
                    status_code=0,
                    error_code=ErrorCode.ENCRYPTION_FAILED,
                    retryable=False,
                    details={
                        "project_alias": alias,
                        "failed_keys": sorted(problems),
                    },
                )
            if problems:
                logger.warning(
                    "Encryption returned non-ciphertext for keys %s; writing anyway "
                    "because --allow-plaintext-on-encrypt-failure was set.",
                    sorted(problems),
                )

            # Read-modify-write: deep-copy of the body has the secrets sub-dict
            # replaced; everything else is preserved bit-identical.
            updated_secrets = dict(existing_secrets)
            updated_secrets.update(encrypted)
            new_body = self._merge_secrets_into_body(current_body, updated_secrets)

            put_response = storage_client.update_config(
                component_id=DATA_APP_COMPONENT_ID,
                config_id=config_id,
                configuration=new_body,
                change_description=(
                    f"Set {len(validated)} secret(s) via kbagent data-app secrets set"
                ),
                branch_id=branch_id,
            )
            new_version = str(put_response.get("version", "") or "")
            old_version = str(envelope.get("version", "") or "")

            secrets_set = sorted(_derive_runtime_env_var_name(k) for k in validated)
            return {
                "project_alias": alias,
                "app_id": str(app_id),
                "config_id": config_id,
                "secrets_set": secrets_set,
                "secrets_unchanged": unchanged,
                "shadowed_by_runtime": shadowed,
                # Keys whose ciphertext check failed but were written anyway under
                # --allow-plaintext-on-encrypt-failure (empty on full encryption).
                # Surfaces the plaintext leak structurally in --json, not just the
                # stderr warning above.
                "plaintext_written": sorted(problems),
                "config_version_before": old_version,
                "config_version_after": new_version,
                "deploy_required": True,
                "next_step": (
                    f"kbagent data-app deploy --project {alias} --app-id {app_id} --wait"
                ),
                "message": (
                    f"{len(secrets_set)} secret(s) encrypted and written. "
                    "The running container keeps the old config until you redeploy."
                ),
            }
        finally:
            ds_client.close()
            storage_client.close()

    def list_data_app_secrets(
        self,
        *,
        alias: str,
        app_id: str,
        branch_id: int | None = None,
        show_fingerprint: bool = False,
    ) -> dict[str, Any]:
        """Return metadata for every key in ``parameters.dataApp.secrets``.

        The block holds both ``#``-prefixed encrypted secrets and plain
        (unencrypted) env-var values; both are enumerated. Never returns an
        encrypted ciphertext in full and never attempts to decrypt -- the
        Encryption API is one-way; decryption from the CLI is impossible by
        design.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        ds_client = self._ds_client_factory(project.stack_url, project.token)
        storage_client = self._client_factory(project.stack_url, project.token)
        try:
            config_id, _envelope, body = self._load_data_app_storage_config(
                ds_client=ds_client,
                storage_client=storage_client,
                app_id=str(app_id),
                branch_id=branch_id,
            )
            raw_secrets = self._read_secrets_block(body)

            entries: list[dict[str, Any]] = []
            for key in sorted(raw_secrets.keys()):
                env_var = _derive_runtime_env_var_name(key)
                entry: dict[str, Any] = {
                    "key": key,
                    "env_var": env_var,
                    "shadowed_by_runtime": env_var in RESERVED_RUNTIME_ENV_VARS,
                }
                if show_fingerprint:
                    ciphertext = raw_secrets[key]
                    entry["fingerprint"] = _secret_fingerprint(ciphertext)
                    entry["encryption_prefix"] = self._derive_encryption_prefix(ciphertext)
                entries.append(entry)

            return {
                "project_alias": alias,
                "app_id": str(app_id),
                "config_id": config_id,
                "secrets": entries,
                "count": len(entries),
            }
        finally:
            ds_client.close()
            storage_client.close()

    def get_data_app_secret(
        self,
        *,
        alias: str,
        app_id: str,
        key: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Return metadata for ONE key in ``parameters.dataApp.secrets``.

        Two cases, dispatched on whether the stored value is a ``KBC::``
        ciphertext:

        - **Encrypted** (``#`` secret): metadata-only. The decrypted
          plaintext NEVER appears in the return dict, stderr, the log
          stream, or the change description. The Encryption API exposes no
          decrypt endpoint; the CLI cannot decrypt even if it wanted to.
          This metadata-only contract is the security boundary and is
          asserted by the test suite.
        - **Plain** (unencrypted env-var config value): the value is
          returned verbatim. It is not a secret -- it is already stored in
          clear in the config and visible via ``config detail`` -- so
          echoing it leaks nothing the caller could not already read.

        Accepts keys with OR without a leading ``#`` (``require_hash=False``),
        mirroring ``secrets-list``, which enumerates both kinds.
        """
        self._validate_secret_key(key, require_hash=False)

        projects = self.resolve_projects([alias])
        project = projects[alias]
        ds_client = self._ds_client_factory(project.stack_url, project.token)
        storage_client = self._client_factory(project.stack_url, project.token)
        try:
            config_id, _envelope, body = self._load_data_app_storage_config(
                ds_client=ds_client,
                storage_client=storage_client,
                app_id=str(app_id),
                branch_id=branch_id,
            )
            raw_secrets = self._read_secrets_block(body)
            if key not in raw_secrets:
                # Don't enumerate sibling keys -- avoid leaking neighbour
                # presence to a caller who knows only this key's name.
                raise KeboolaApiError(
                    message=(
                        f"Secret '{key}' not found on data app {app_id} in project '{alias}'."
                    ),
                    status_code=404,
                    error_code=ErrorCode.NOT_FOUND,
                    retryable=False,
                )
            stored_value = raw_secrets[key]
            env_var = _derive_runtime_env_var_name(key)
            is_encrypted = isinstance(stored_value, str) and stored_value.startswith("KBC::")
            result: dict[str, Any] = {
                "project_alias": alias,
                "app_id": str(app_id),
                "config_id": config_id,
                "key": key,
                "env_var": env_var,
                "shadowed_by_runtime": env_var in RESERVED_RUNTIME_ENV_VARS,
                "encrypted": is_encrypted,
                "present": True,
            }
            if is_encrypted:
                result["value"] = None
                result["fingerprint"] = _secret_fingerprint(stored_value)
                result["encryption_prefix"] = self._derive_encryption_prefix(stored_value)
                result["message"] = (
                    f"Secret '{key}' is set on data app {app_id}. "
                    "Decrypted plaintext is NOT exposed by the CLI."
                )
            else:
                result["value"] = stored_value
                result["fingerprint"] = ""
                result["encryption_prefix"] = ""
                result["message"] = (
                    f"'{key}' is a plaintext (unencrypted) config value on data app "
                    f"{app_id}; it is stored in clear in the config."
                )
            return result
        finally:
            ds_client.close()
            storage_client.close()

    def remove_data_app_secrets(
        self,
        *,
        alias: str,
        app_id: str,
        keys: list[str],
        branch_id: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Remove one or more keys from ``parameters.dataApp.secrets``. Idempotent.

        Accepts both ``#``-prefixed secrets and plain (unencrypted) env-var
        keys (``require_hash=False``), mirroring ``secrets-list`` -- anything
        that can be listed can be removed.
        """
        if not keys:
            raise KeboolaApiError(
                message="At least one --key 'KEY' is required ('#' optional).",
                status_code=0,
                error_code=ErrorCode.DATA_APP_INVALID_SECRET,
                retryable=False,
            )
        for key in keys:
            self._validate_secret_key(key, require_hash=False)

        projects = self.resolve_projects([alias])
        project = projects[alias]
        ds_client = self._ds_client_factory(project.stack_url, project.token)
        storage_client = self._client_factory(project.stack_url, project.token)
        try:
            config_id, envelope, body = self._load_data_app_storage_config(
                ds_client=ds_client,
                storage_client=storage_client,
                app_id=str(app_id),
                branch_id=branch_id,
            )
            existing_secrets = self._read_secrets_block(body)

            removed = sorted(_derive_runtime_env_var_name(k) for k in keys if k in existing_secrets)
            not_found = sorted(
                _derive_runtime_env_var_name(k) for k in keys if k not in existing_secrets
            )
            current_version = str(envelope.get("version", "") or "")

            if not removed:
                # Idempotent: removing a non-existent key is success.
                return {
                    "project_alias": alias,
                    "app_id": str(app_id),
                    "config_id": config_id,
                    "removed": [],
                    "not_found": not_found,
                    "config_version_before": current_version,
                    "config_version_after": current_version,
                    "deploy_required": False,
                    "message": (
                        f"No matching secrets to remove on data app {app_id}. "
                        f"Keys not present: {', '.join(not_found) or '<none>'}."
                    ),
                }

            if dry_run:
                preview = {k: v for k, v in existing_secrets.items() if k not in keys}
                preview_body = self._merge_secrets_into_body(body, preview)
                return {
                    "dry_run": True,
                    "project_alias": alias,
                    "app_id": str(app_id),
                    "config_id": config_id,
                    "to_remove": removed,
                    "not_found": not_found,
                    "put_storage_config_preview": _redact_storage_config(
                        {"configuration": preview_body}
                    ),
                    "message": (
                        f"Dry run -- would remove {len(removed)} secret(s) and PUT "
                        "the body above. No API call made."
                    ),
                }

            updated_secrets = {k: v for k, v in existing_secrets.items() if k not in keys}
            new_body = self._merge_secrets_into_body(body, updated_secrets)

            put_response = storage_client.update_config(
                component_id=DATA_APP_COMPONENT_ID,
                config_id=config_id,
                configuration=new_body,
                change_description=(
                    f"Remove {len(removed)} secret(s) via kbagent data-app secrets remove"
                ),
                branch_id=branch_id,
            )
            new_version = str(put_response.get("version", "") or "")
            return {
                "project_alias": alias,
                "app_id": str(app_id),
                "config_id": config_id,
                "removed": removed,
                "not_found": not_found,
                "config_version_before": current_version,
                "config_version_after": new_version,
                "deploy_required": True,
                "next_step": (
                    f"kbagent data-app deploy --project {alias} --app-id {app_id} --wait"
                ),
                "message": (
                    f"{len(removed)} secret(s) removed. The running container keeps "
                    "the old config until you redeploy."
                ),
            }
        finally:
            ds_client.close()
            storage_client.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_secret_key(self, key: str, *, require_hash: bool = True) -> None:
        """Reject any key that is not a valid data-app env-var identifier.

        With ``require_hash=True`` (the default, used by the encrypting
        ``secrets-set`` path) the ``#``-prefix convention is mandatory.
        With ``require_hash=False`` (read/remove paths) the ``#`` is
        optional, so plain unencrypted env-var keys -- which
        ``secrets-list`` enumerates alongside ``#`` secrets -- can be read
        and removed. Either way the rest of the key must form a valid
        env-var identifier after the runtime translation rule.

        Service-boundary check; the command layer also validates so the
        error surfaces with the friendliest exit code.
        """
        pattern = SECRET_KEY_PATTERN if require_hash else SECRET_OR_PLAIN_KEY_PATTERN
        if not isinstance(key, str) or not pattern.match(key):
            message = (
                (
                    f"Invalid secret key '{key}'. Keys must start with '#' and "
                    "the rest must match [A-Za-z][A-Za-z0-9_-]{0,63} so the "
                    "derived runtime env-var name (uppercase, '-' to '_') is a "
                    "valid identifier."
                )
                if require_hash
                else (
                    f"Invalid key '{key}'. The key must form a valid env-var "
                    "identifier -- [A-Za-z][A-Za-z0-9_-]{0,63} with an optional "
                    "leading '#' -- so the derived runtime env-var name "
                    "(uppercase, '-' to '_') is valid."
                )
            )
            raise KeboolaApiError(
                message=message,
                status_code=0,
                error_code=ErrorCode.DATA_APP_INVALID_SECRET,
                retryable=False,
            )
        if _has_control_chars(key):
            raise KeboolaApiError(
                message=f"Secret key '{key}' contains disallowed control characters.",
                status_code=0,
                error_code=ErrorCode.DATA_APP_INVALID_SECRET,
                retryable=False,
            )

    def _merge_secrets_into_body(
        self,
        body: dict[str, Any],
        secrets: dict[str, str],
    ) -> dict[str, Any]:
        """Return a deep-copy of ``body`` with ``parameters.dataApp.secrets`` replaced.

        Every untouched sibling -- under ``parameters.dataApp.secrets``
        (none, since the whole sub-dict is replaced), under
        ``parameters.dataApp`` (slug, git, id, etc.), under ``parameters``
        (everything else), and at the top level (``runtime``,
        ``authorization``, ``storage``) -- is preserved bit-identical.
        """
        new_body = json.loads(json.dumps(body)) if isinstance(body, dict) else {}
        if not isinstance(new_body, dict):
            new_body = {}
        params = new_body.setdefault("parameters", {})
        if not isinstance(params, dict):
            params = {}
            new_body["parameters"] = params
        data_app = params.setdefault("dataApp", {})
        if not isinstance(data_app, dict):
            data_app = {}
            params["dataApp"] = data_app
        if secrets:
            data_app["secrets"] = dict(secrets)
        elif "secrets" in data_app:
            # Remove the secrets key entirely if the new map is empty so
            # the diff reads "key dropped" rather than "key set to {}".
            del data_app["secrets"]
        return new_body

    def _derive_encryption_prefix(self, ciphertext: str) -> str:
        """Return the ``KBC::*`` prefix matched on this ciphertext, or '' if none."""
        if not isinstance(ciphertext, str):
            return ""
        for prefix in ENCRYPTED_PASSWORD_PREFIXES:
            if ciphertext.startswith(prefix):
                return prefix.rstrip(":")
        return ""

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
        use_managed_git_repo: bool = False,
    ) -> None:
        # A Keboola-managed repo is mutually exclusive with every external-git
        # flag: the Git Service provisions an empty repo and owns the link, so
        # there is no URL / branch-override / PAT to supply. Reject the combo
        # up front rather than silently ignoring the external flags.
        if use_managed_git_repo:
            offenders = []
            if git_repo:
                offenders.append("--git-repo")
            if git_public:
                offenders.append("--git-public")
            if git_username:
                offenders.append("--git-username")
            if git_pat_plaintext is not None and git_pat_plaintext != "":
                offenders.append("--git-pat-env/--git-pat-file")
            if git_pat_encrypted is not None and git_pat_encrypted != "":
                offenders.append("--git-pat-encrypted")
            if offenders:
                raise KeboolaApiError(
                    message=(
                        "--use-managed-git-repo provisions an empty Keboola-hosted "
                        "repository and is incompatible with external-git flags: "
                        f"{', '.join(offenders)}."
                    ),
                    status_code=0,
                    error_code=ErrorCode.VALIDATION_ERROR,
                    retryable=False,
                )

        # Defence-in-depth length / control-char checks at the service
        # boundary. The service can be invoked directly (via the
        # ``kbagent serve`` REST API or external Python callers) so we do
        # not rely on the command layer alone.
        for field_name, field_value, max_len, allow_ws in (
            ("--name", name, MAX_NAME_LENGTH, False),
            ("--description", description, MAX_DESCRIPTION_LENGTH, True),
            ("--git-repo", git_repo, MAX_GIT_REPO_LENGTH, False),
            ("--git-branch", git_branch, MAX_GIT_BRANCH_LENGTH, False),
            ("--git-username", git_username or "", MAX_GIT_USERNAME_LENGTH, False),
        ):
            _check_text_field(field_name, field_value, max_len, allow_ws)

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
        elif not use_managed_git_repo:
            # Managed repos skip the private-repo auth checks entirely -- the
            # mutex above guarantees git_public is False and all git/PAT flags
            # are empty here, so neither branch should fire for managed.
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
        git_block: dict[str, Any] | None,
        auth: str,
        app_id: str,
        workspace: bool = True,
    ) -> dict[str, Any]:
        data_app: dict[str, Any] = {"slug": slug}
        # Managed repos pass git_block=None: the Git Service owns the link via
        # app.managedGitRepoId, so we must NOT write parameters.dataApp.git.
        if git_block is not None:
            data_app["git"] = git_block
        body: dict[str, Any] = {
            "parameters": {
                "autoSuspendAfterSeconds": auto_suspend_after_seconds,
                "dataApp": data_app,
                "id": str(app_id),  # writeup §5: required back-pointer
            },
            "runtime": _build_runtime_block(size=size, workspace=workspace),
        }
        body["authorization"] = _auth_block_for(auth)
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
        use_managed_git_repo = bool(kwargs.get("use_managed_git_repo", False))
        workspace = bool(kwargs.get("workspace", True))

        post_body = {
            # 0 (`--branch 0`) is production (null); a dev branch id is a string,
            # as create_app sends it.
            "branchId": str(kwargs["branch_id"]) if kwargs["branch_id"] else None,
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
        post_body["config"]["authorization"] = _auth_block_for(auth)
        # The managed-repo flag lives on the POST /v2/apps body, NOT in the
        # Storage config -- the Git Service provisions the repo and links
        # it to the app, so the PUT below carries no git block.
        post_body["useManagedGitRepo"] = use_managed_git_repo

        # The Storage config's dataApp block carries the git pointer only for
        # external repos. Managed repos omit it (parameters.dataApp.git absent).
        data_app_preview: dict[str, Any] = {"slug": slug}
        if not use_managed_git_repo:
            # We can't know the app_id pre-create; show the placeholder.
            if kwargs["git_public"]:
                data_app_preview["git"] = {
                    "repository": kwargs["git_repo"],
                    "private": False,
                    "branch": kwargs["git_branch"],
                }
            else:
                data_app_preview["git"] = {
                    "repository": kwargs["git_repo"],
                    "private": True,
                    "username": kwargs["git_username"] or "<from input>",
                    "#password": "<encrypted at runtime>",
                    "branch": kwargs["git_branch"],
                }

        put_body = {
            "parameters": {
                "autoSuspendAfterSeconds": auto_suspend,
                "dataApp": data_app_preview,
                "id": "<server-assigned numeric id>",
            },
            "runtime": _build_runtime_block(size=size, workspace=workspace),
        }
        put_body["authorization"] = _auth_block_for(auth)

        deployment_body: dict[str, Any] = {}
        if kwargs["deploy"]:
            deployment_body = {"configVersion": "<latest after PUT>"}

        if use_managed_git_repo:
            message = (
                "Dry run -- no API calls made. A managed repo is provisioned "
                "empty (no deploy); after create, mint a credential, push code, "
                "then deploy. Inspect the request bodies above before re-running "
                "without --dry-run."
            )
        else:
            message = (
                "Dry run -- no API calls made. "
                "Inspect the three request bodies above before re-running without --dry-run."
            )
        return {
            "dry_run": True,
            "project_alias": kwargs["alias"],
            "use_managed_git_repo": use_managed_git_repo,
            "workspace": workspace,
            "requests": {
                "post_apps": post_body,
                "put_storage_config": put_body,
                "post_deployments": deployment_body,
            },
            "message": message,
        }

    def _fetch_data_app_configs(
        self,
        storage_client: Any,
        branch_id: int | None,
    ) -> dict[str, dict[str, Any]]:
        """Map ``configId -> Storage config`` for ``keboola.data-apps`` configs.

        - ``list_data_apps`` reads the name, size and auto-suspend from them:
          the ``/v2`` app record carries none of these.
        - Empty when the listing fails, so the apps still list.
        """
        try:
            configs = storage_client.list_component_configs(
                DATA_APP_COMPONENT_ID, branch_id=branch_id
            )
        except Exception:
            logger.warning("Failed to list data-app configs", exc_info=True)
            return {}
        return {str(cfg.get("id", "")): cfg for cfg in configs}

    def _deploy_failure_diagnostic(
        self,
        ds_client: DataScienceClient,
        app_id: str,
    ) -> tuple[str, dict[str, Any]]:
        """Best-effort: fetch the latest run's ``failureReason`` to enrich a deploy
        error message. Returns ``(message_suffix, details)``; NEVER raises -- a
        diagnostic fetch failure must not mask the original deploy error.

        Setup-phase failures (e.g. a git-clone error) produce no container logs,
        so ``data-app logs`` cannot show them; the run record is the only place
        the reason surfaces.
        """
        try:
            runs = ds_client.list_app_runs(app_id, limit=1)
        except Exception:
            return "", {}
        if not runs:
            return "", {}
        run = runs[0]
        failure = run.get("failureReason")
        if not isinstance(failure, dict):
            return "", {}
        reason = str(failure.get("reason") or "")
        raw_message = str(failure.get("message") or "")
        detail_line = next(
            (line.strip() for line in reversed(raw_message.splitlines()) if line.strip()),
            "",
        )
        suffix = f" Latest run failed ({reason or 'unknown reason'})"
        if detail_line:
            suffix += f": {detail_line}"
        suffix += ". See `kbagent data-app runs` for the full startup log."
        details = {
            "run_id": run.get("id"),
            "run_state": run.get("state"),
            "failure_reason": failure,
        }
        return suffix, details

    def _poll_until_terminal(
        self,
        ds_client: DataScienceClient,
        app_id: str,
        *,
        target_state: str,
        timeout_seconds: float,
        deployment_id: str | None = None,
    ) -> dict[str, Any]:
        """Poll ``GET /v2/apps/{id}`` until terminal; returns the last app record.

        Terminal definition:

        - success: ``state == target_state`` and, with ``deployment_id``,
          ``production.actualDeploymentId == deployment_id`` -- the new
          deployment serves production, not the one it replaces
        - failure: the deployment's ``buildState`` or ``runState`` is
          ``failed``, or (without ``deployment_id``) the app is ``disabled``
        """
        deadline = time.monotonic() + timeout_seconds
        while True:
            app = ds_client.get_app(app_id)
            state = str(app.get("state", ""))
            serving = str((app.get("production") or {}).get("actualDeploymentId") or "")
            if deployment_id:
                deployment = ds_client.get_deployment(app_id, deployment_id)
                failed = FAILED_DEPLOYMENT_STATE in (
                    deployment.get("buildState"),
                    deployment.get("runState"),
                )
            else:
                failed = state == DISABLED_STATE
            if failed:
                suffix, details = self._deploy_failure_diagnostic(ds_client, app_id)
                subject = f"deployment {deployment_id}" if deployment_id else f"state={state}"
                raise KeboolaApiError(
                    message=(
                        f"Data app {app_id} failed ({subject}).{suffix}"
                        if suffix
                        else (
                            f"Data app {app_id} failed ({subject}). "
                            "Run `kbagent data-app runs` for the failure reason, or see "
                            "the app's Terminal Log in the Keboola UI."
                        )
                    ),
                    status_code=0,
                    error_code=ErrorCode.DATA_APP_BUILD_FAILED,
                    retryable=False,
                    details=details or None,
                )
            if state == target_state and (not deployment_id or serving == deployment_id):
                return app
            if time.monotonic() >= deadline:
                suffix, details = self._deploy_failure_diagnostic(ds_client, app_id)
                raise KeboolaApiError(
                    message=(
                        f"Timed out after {timeout_seconds:.0f}s waiting for data app "
                        f"{app_id} to reach state={target_state} "
                        f"(last observed: state={state}, serving deployment={serving or 'none'})."
                        f"{suffix}"
                    ),
                    status_code=0,
                    error_code=ErrorCode.DATA_APP_DEPLOY_TIMEOUT,
                    retryable=True,
                    details=details or None,
                )
            time.sleep(POLL_INTERVAL_SECONDS)

    def _format_lifecycle_result(
        self,
        *,
        alias: str,
        app_id: str,
        action: str,
        app: dict[str, Any],
        config_version: str | None = None,
    ) -> dict[str, Any]:
        state = app.get("state", "")
        return {
            "project_alias": alias,
            "app_id": str(app_id),
            "action": action,
            "state": state,
            "config_version": config_version or _production_config_version(app),
            "url": app.get("url", ""),
            "message": f"Data app {app_id} {action} requested in project '{alias}'. state={state}.",
        }

    def _format_create_message(
        self,
        *,
        name: str,
        auth: str,
        deployed: bool,
        wait: bool,
        state: str,
        use_managed_git_repo: bool = False,
    ) -> str:
        if use_managed_git_repo:
            # Managed repos are provisioned empty -- there is no code to deploy
            # yet. Spell out the follow-up steps so the operator (human or agent)
            # knows the create call is only step one. The platform injects the
            # clone credentials at deploy time, so no credential wiring is needed.
            return (
                f"Data app '{name}' created with an empty Keboola-managed Git "
                "repository. Next: 1) `kbagent data-app git-credentials-create "
                "--type http_token --permissions readWrite` + push your code to the "
                "managed repo (`data-app git-repo` shows the URL), then "
                "2) `kbagent data-app deploy`."
            )
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
                " Copy its password with `kbagent data-app password`." if auth == "password" else ""
            )
            return f"Data app '{name}' is running.{tail}"
        return f"Data app '{name}' deploy reached state={state}."
