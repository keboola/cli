"""In-place edits of an existing data app's Storage config (``data-app update``).

Split out of ``data_app_service.py``: that module is grandfathered at its
current size by ``scripts/check_file_size.py`` and may only shrink, so new
functionality lands beside it as a mixin (the same composition pattern the
``client/`` package uses). :class:`DataAppUpdateMixin` is mixed into
:class:`~keboola_agent_cli.services.data_app_service.DataAppService`, so
``update_data_app`` and the shared config loader are ordinary methods on the
service from every caller's point of view.

The read-modify-write contract mirrors the secrets path: the Storage API's
``configuration`` field is a full-document overwrite, so we GET the whole
config, mutate only the requested keys, and PUT the untouched remainder back.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..errors import ErrorCode, KeboolaApiError
from ._data_app_bodies import _auth_block_for, _redact_storage_config
from .base import BaseService

# Keys the caller may change. Order is the order they are reported in.
UPDATABLE_FIELDS = ("workspace", "auto_suspend_after_seconds", "size", "auth", "git_branch")


class DataAppUpdateMixin(BaseService):
    """``update_data_app`` plus the Storage-config loader it shares with secrets.

    Inherits :class:`BaseService` so ``resolve_projects`` / ``_client_factory``
    type-check here as they do on the concrete service; ``_ds_client_factory``
    is annotated because ``DataAppService.__init__`` is what assigns it.
    """

    _ds_client_factory: Callable[[str, str], Any]

    def _load_data_app_storage_config(
        self,
        *,
        ds_client: Any,
        storage_client: Any,
        app_id: str,
        branch_id: int | None,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """Resolve ``configId`` from the Data Science app and load the Storage config.

        Returns ``(config_id, storage_envelope, body)`` where ``body`` is a
        deep-copy of ``storage_envelope.configuration`` so callers can
        mutate it freely.
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
        envelope = storage_client.get_config_detail(_component_id(), config_id, branch_id=branch_id)
        if not isinstance(envelope, dict):
            envelope = {}
        configuration = envelope.get("configuration")
        if not isinstance(configuration, dict):
            configuration = {}
        # Deep-copy so caller mutations don't reach the cached upstream.
        body = json.loads(json.dumps(configuration))
        return config_id, envelope, body

    def update_data_app(
        self,
        *,
        alias: str,
        app_id: str,
        workspace: bool | None = None,
        auto_suspend_after_seconds: int | None = None,
        size: str | None = None,
        auth: str | None = None,
        git_branch: str | None = None,
        branch_id: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Change deployment settings on an EXISTING data app's Storage config.

        Only the fields the caller passed are touched; every other key in the
        config body -- including ``parameters.dataApp.secrets`` and the git
        block's encrypted ``#password`` -- is preserved bit-identical.

        ``workspace`` is the switch behind ``runtime.workspace.enabled``: the
        one that makes the platform inject ``KBC_TOKEN`` / ``WORKSPACE_ID`` /
        ``QUERY_SERVICE_URL``. ``workspace=False`` deletes the key rather than
        writing ``enabled: false``, matching what ``create --no-workspace``
        produces so the two paths converge on the same body.

        Never deploys: per the §9 redeploy contract the running container
        keeps its pinned ``configVersion`` until the next ``data-app deploy``.
        """
        requested = {
            "workspace": workspace,
            "auto_suspend_after_seconds": auto_suspend_after_seconds,
            "size": size,
            "auth": auth,
            "git_branch": git_branch,
        }
        if all(value is None for value in requested.values()):
            raise KeboolaApiError(
                message=(
                    "Nothing to update. Pass at least one of --workspace/--no-workspace, "
                    "--auto-suspend, --size, --auth, --git-branch."
                ),
                status_code=0,
                error_code=ErrorCode.MISSING_PARAMETER,
                retryable=False,
            )
        self._validate_update_inputs(
            auto_suspend_after_seconds=auto_suspend_after_seconds,
            size=size,
            auth=auth,
            git_branch=git_branch,
        )

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
            new_body, changes = _apply_update(body, requested)
            version_before = str(envelope.get("version", "") or "")

            base: dict[str, Any] = {
                "project_alias": alias,
                "app_id": str(app_id),
                "config_id": config_id,
                "changed": [entry["field"] for entry in changes],
                "changes": changes,
                "config_version_before": version_before,
            }

            if not changes:
                # Idempotent: every requested value already matches. No PUT,
                # so no pointless config version and no redeploy prompt.
                return {
                    **base,
                    "config_version_after": version_before,
                    "deploy_required": False,
                    "message": (
                        f"Data app {app_id} already matches the requested settings; "
                        "nothing written."
                    ),
                }

            if dry_run:
                return {
                    **base,
                    "dry_run": True,
                    "put_storage_config_preview": _redact_storage_config(
                        {"configuration": new_body}
                    ),
                    "message": (
                        f"Dry run -- would update {len(changes)} field(s) and PUT the "
                        "body above. No API call made."
                    ),
                }

            put_response = storage_client.update_config(
                component_id=_component_id(),
                config_id=config_id,
                configuration=new_body,
                change_description=(
                    f"Update {', '.join(entry['field'] for entry in changes)} "
                    "via kbagent data-app update"
                ),
                branch_id=branch_id,
            )
            return {
                **base,
                "config_version_after": str(put_response.get("version", "") or ""),
                "deploy_required": True,
                "next_step": (
                    f"kbagent data-app deploy --project {alias} --app-id {app_id} --wait"
                ),
                "message": (
                    f"{len(changes)} field(s) updated on data app {app_id}. "
                    "The running container keeps the old config until you redeploy."
                ),
            }
        finally:
            ds_client.close()
            storage_client.close()

    def _validate_update_inputs(
        self,
        *,
        auto_suspend_after_seconds: int | None,
        size: str | None,
        auth: str | None,
        git_branch: str | None,
    ) -> None:
        """Service-boundary validation; the CLI validates too, this is defence in depth."""
        from .data_app_service import MAX_GIT_BRANCH_LENGTH, VALID_SIZES, _has_control_chars

        if size is not None and size not in VALID_SIZES:
            raise KeboolaApiError(
                message=f"Invalid --size '{size}'. Valid values: {', '.join(VALID_SIZES)}",
                status_code=0,
                error_code=ErrorCode.VALIDATION_ERROR,
                retryable=False,
            )
        if auth is not None and auth not in ("password", "public"):
            raise KeboolaApiError(
                message=f"Invalid --auth '{auth}'. Valid values: password, public",
                status_code=0,
                error_code=ErrorCode.VALIDATION_ERROR,
                retryable=False,
            )
        if auto_suspend_after_seconds is not None and auto_suspend_after_seconds < 0:
            raise KeboolaApiError(
                message="--auto-suspend must be a non-negative number of seconds.",
                status_code=0,
                error_code=ErrorCode.VALIDATION_ERROR,
                retryable=False,
            )
        if git_branch is not None:
            if not git_branch or len(git_branch) > MAX_GIT_BRANCH_LENGTH:
                raise KeboolaApiError(
                    message=(
                        "--git-branch must be non-empty and at most "
                        f"{MAX_GIT_BRANCH_LENGTH} characters."
                    ),
                    status_code=0,
                    error_code=ErrorCode.VALIDATION_ERROR,
                    retryable=False,
                )
            if _has_control_chars(git_branch):
                raise KeboolaApiError(
                    message="--git-branch contains disallowed control characters.",
                    status_code=0,
                    error_code=ErrorCode.VALIDATION_ERROR,
                    retryable=False,
                )


def _component_id() -> str:
    """Late import: ``data_app_service`` imports this module, so the constants it
    owns can only be reached from inside a function body."""
    from .data_app_service import DATA_APP_COMPONENT_ID

    return DATA_APP_COMPONENT_ID


def read_workspace_enabled(configuration: Any) -> bool:
    """Return ``runtime.workspace.enabled`` from a data-app config body.

    Absent key == disabled: ``create --no-workspace`` omits the block
    entirely rather than writing ``enabled: false``.
    """
    if not isinstance(configuration, dict):
        return False
    runtime = configuration.get("runtime")
    if not isinstance(runtime, dict):
        return False
    workspace = runtime.get("workspace")
    if not isinstance(workspace, dict):
        return False
    return bool(workspace.get("enabled", False))


def _apply_update(
    body: dict[str, Any], requested: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return ``(new_body, changes)`` -- a deep copy with only the asked-for edits.

    ``changes`` carries one ``{field, before, after}`` entry per field that
    actually differs, so a no-op update can skip the Storage PUT entirely
    instead of minting a config version that changes nothing.
    """
    new_body: dict[str, Any] = json.loads(json.dumps(body)) if isinstance(body, dict) else {}
    changes: list[dict[str, Any]] = []

    def record(field: str, before: Any, after: Any) -> bool:
        if before == after:
            return False
        changes.append({"field": field, "before": before, "after": after})
        return True

    parameters = new_body.setdefault("parameters", {})
    if not isinstance(parameters, dict):
        parameters = {}
        new_body["parameters"] = parameters
    runtime = new_body.setdefault("runtime", {})
    if not isinstance(runtime, dict):
        runtime = {}
        new_body["runtime"] = runtime

    workspace = requested["workspace"]
    if workspace is not None and record("workspace", read_workspace_enabled(body), workspace):
        if workspace:
            runtime["workspace"] = {"enabled": True}
        else:
            # Mirror `create --no-workspace`: drop the key, don't write false.
            runtime.pop("workspace", None)

    auto_suspend = requested["auto_suspend_after_seconds"]
    if auto_suspend is not None and record(
        "auto_suspend_after_seconds",
        parameters.get("autoSuspendAfterSeconds"),
        auto_suspend,
    ):
        parameters["autoSuspendAfterSeconds"] = auto_suspend

    size = requested["size"]
    if size is not None:
        backend = runtime.get("backend")
        if not isinstance(backend, dict):
            backend = {}
        if record("size", backend.get("size"), size):
            backend["size"] = size
            runtime["backend"] = backend

    auth = requested["auth"]
    if auth is not None:
        after_block = _auth_block_for(auth)
        if record("auth", _describe_auth(new_body.get("authorization")), auth):
            new_body["authorization"] = after_block

    git_branch = requested["git_branch"]
    if git_branch is not None:
        data_app = parameters.get("dataApp")
        if not isinstance(data_app, dict):
            data_app = {}
        git_block = data_app.get("git")
        if not isinstance(git_block, dict):
            # A managed-repo app has no parameters.dataApp.git -- the Git
            # Service owns the link -- so there is no branch to retarget.
            raise KeboolaApiError(
                message=(
                    "--git-branch has no effect on this app: its config carries no "
                    "parameters.dataApp.git block (a Keboola-managed repository, or an "
                    "app that has never been configured with an external repo)."
                ),
                status_code=0,
                error_code=ErrorCode.DATA_APP_INVALID_GIT,
                retryable=False,
            )
        if record("git_branch", git_block.get("branch"), git_branch):
            git_block["branch"] = git_branch
            data_app["git"] = git_block
            parameters["dataApp"] = data_app

    return new_body, changes


def _describe_auth(authorization: Any) -> str | None:
    """Collapse an ``authorization`` block back to ``password`` / ``public``.

    Anything the two builders did not produce reports as ``None`` so the
    update is treated as a real change rather than silently skipped.
    """
    if not isinstance(authorization, dict):
        return None
    for candidate in ("password", "public"):
        if authorization == _auth_block_for(candidate):
            return candidate
    return None
