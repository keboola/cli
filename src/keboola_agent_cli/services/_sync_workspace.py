"""Shared SQL workspaces in the sync engine (CLI-25).

``keboola.sandboxes`` is on :data:`ALWAYS_IGNORED_COMPONENTS`. A tree opts in
with the manifest key ``syncWorkspaces`` (``sync init --with-workspaces``);
then pull, diff, push and clone handle its **shared SQL workspaces** like any
other configuration. Scope and rules:

- **Which workspaces.** A Snowflake / BigQuery workspace created in the UI is a
  ``keboola.sandboxes`` configuration WITHOUT ``parameters.id``: its backend
  workspace belongs to a SQL editor session, not to the config. A Python / R
  (container) workspace carries ``parameters.id``, the id of its Data Science
  ``/apps`` record. The official CLI tells the two apart by the same key
  (keboola-as-code ``remote workspace detail``). A legacy SQL sandbox from
  before the SQL editor also carries ``parameters.id`` (an ``/apps`` record of
  type ``snowflake`` / ``bigquery``); it stays skipped, because its workspace
  belongs to that record and a config-only sync cannot create or delete it.
  Only ``runtime.shared: true`` workspaces are fetched: the UI shows the
  others to their creator only.
- **Tracked stays tracked.** A workspace already in the manifest stays in the
  remote listing even when someone turns ``runtime.shared`` off. Diff then
  reports the change and pull writes it, instead of the entry vanishing from
  the listing, which diff would read as a local ``added`` and push would
  create again.
- **Config only.** Push creates and updates the Storage configuration, nothing
  else: no Queue job, no SQL editor session, no table load. The UI creates
  the session when a user opens the workspace.
- **Delete.** Only ``push --force`` deletes (``plan_push``; a plain push
  lists the workspace under ``skipped_deletions``). It deletes the SQL editor
  sessions of the workspace (every user's, in the push branch) and then the
  configuration, like
  ``kbc remote workspace delete``. When the sessions cannot be listed or one
  cannot be deleted, the configuration is not deleted and the error names the
  sessions already deleted. A workspace whose config has ``parameters.id`` is
  refused: its Data Science app would keep running.
- **Turning it off.** Removing the key drops the workspace entries on the
  next pull (``_sync_stale``), except an edited one, which is kept.

Kept out of ``sync_service`` (frozen at its size budget) like the data-app
helpers in ``_sync_data_app``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..constants import ALWAYS_IGNORED_COMPONENTS
from ..errors import ErrorCode, KeboolaApiError
from ..sync.manifest import Manifest, load_manifest
from .base import find_default_branch_id

if TYPE_CHECKING:
    from .sync_service import SyncService

logger = logging.getLogger(__name__)

SANDBOXES_COMPONENT_ID = "keboola.sandboxes"


def effective_ignored_components(manifest: Manifest) -> frozenset[str]:
    """Components excluded from this working tree's sync operations.

    The hardcoded :data:`ALWAYS_IGNORED_COMPONENTS` plus the manifest's
    ``ignoredComponents``, which was declared in the schema from day one but
    read by nothing until issue #689. ``keboola.sandboxes`` leaves the
    hardcoded set when the manifest sets ``syncWorkspaces`` (CLI-25);
    ``ignoredComponents`` is added after that, so an explicit
    ``keboola.sandboxes`` entry there still wins over the opt-in.

    Computed ONCE per pull/diff and threaded through every filtering site so
    the remote side, the local side and the force-pull conflict guard can
    never disagree about what is ignored -- a disagreement is what turns a
    tracked-but-unfetchable config into a phantom "added" that ``sync push``
    duplicates on the remote, once per push.
    """
    always = ALWAYS_IGNORED_COMPONENTS
    if manifest.sync_workspaces:
        always = always - {SANDBOXES_COMPONENT_ID}
    return always | frozenset(manifest.ignored_components)


def is_shared_sql_workspace(config: dict[str, Any]) -> bool:
    """True for a ``keboola.sandboxes`` API config that sync fetches.

    A SQL workspace has no ``parameters.id`` (missing or empty, as the UI
    reads it); ``runtime.shared`` must be true. See the module docstring.
    """
    body = config.get("configuration") or {}
    parameters = body.get("parameters") or {}
    runtime = body.get("runtime") or {}
    if not isinstance(parameters, dict) or not isinstance(runtime, dict):
        return False
    return not parameters.get("id") and runtime.get("shared") is True


def scope_listing(components: list[dict[str, Any]], manifest: Manifest) -> list[dict[str, Any]]:
    """Drop the out-of-scope ``keboola.sandboxes`` configs from a remote listing.

    Applied right after ``list_components_with_configs`` in pull AND diff, so
    every later filtering site (the fetch loop, the stale sweep, the force-pull
    conflict guard, the remote side of the diff) sees the same set. Returns
    *components* unchanged when the tree has not opted in: the ignored set
    already drops the whole component then.
    """
    if not manifest.sync_workspaces:
        return components
    tracked = {c.id for c in manifest.configurations if c.component_id == SANDBOXES_COMPONENT_ID}
    scoped: list[dict[str, Any]] = []
    for component in components:
        if component.get("id") != SANDBOXES_COMPONENT_ID:
            scoped.append(component)
            continue
        configs = [
            cfg
            for cfg in component.get("configurations", [])
            if str(cfg.get("id", "")) in tracked or is_shared_sql_workspace(cfg)
        ]
        scoped.append({**component, "configurations": configs})
    return scoped


def _backend_size(configuration: Any) -> Any:
    parameters = configuration.get("parameters") if isinstance(configuration, dict) else None
    return parameters.get("backendSize") if isinstance(parameters, dict) else None


def warn_on_backend_size_change(
    client: Any,
    *,
    config_id: str,
    configuration: dict[str, Any],
    branch_id: int | None,
    warnings: list[dict[str, Any]],
) -> None:
    """Warn when a push changes a workspace's ``parameters.backendSize``.

    The editor service reads the size only when it creates a session; an
    existing session keeps the size it was created with. Compares with the
    remote config just before the update. Best-effort: a failed read logs and
    skips the warning, it never blocks the push.
    """
    try:
        remote = client.get_config_detail(
            component_id=SANDBOXES_COMPONENT_ID, config_id=config_id, branch_id=branch_id
        )
    except KeboolaApiError as exc:
        logger.warning("backendSize check skipped for workspace %s: %s", config_id, exc.message)
        return
    old_size = _backend_size(remote.get("configuration") if isinstance(remote, dict) else None)
    new_size = _backend_size(configuration)
    if old_size == new_size:
        return
    warnings.append(
        {
            "change_type": "workspace_backend_size",
            "component_id": SANDBOXES_COMPONENT_ID,
            "config_id": config_id,
            "old_backend_size": old_size,
            "new_backend_size": new_size,
            "message": (
                f"Workspace {config_id}: parameters.backendSize changed from {old_size!r} to "
                f"{new_size!r}. An existing SQL editor session keeps its size; the new size "
                "applies only to a session created after this push."
            ),
        }
    )


def _session_branch_id(client: Any, branch_id: int | None) -> int:
    """The branch id SQL editor sessions carry; production is its numeric id."""
    if branch_id is not None:
        return branch_id
    default_branch_id = find_default_branch_id(client.list_dev_branches())
    if default_branch_id is None:
        raise KeboolaApiError(
            message="Cannot find the default branch, so its SQL editor sessions cannot be listed.",
            status_code=0,
            error_code=ErrorCode.API_ERROR,
            retryable=False,
        )
    return default_branch_id


def list_workspace_sessions(
    client: Any, config_ids: set[str], branch_id: int | None
) -> dict[str, list[str]]:
    """Map each workspace config id to the ids of its SQL editor sessions.

    Lists every user's sessions (``listAll=1``) of the push branch once. The
    branch is checked again here: config ids are the same in every branch, so
    a production delete must not reach a dev branch's sessions.

    Raises:
        KeboolaApiError: the branch or the sessions cannot be read.
    """
    session_branch_id = _session_branch_id(client, branch_id)
    sessions: dict[str, list[str]] = {config_id: [] for config_id in config_ids}
    for session in client.list_editor_sessions(branch_id=session_branch_id):
        config_id = str(session.get("configurationId", ""))
        if (
            config_id in sessions
            and session.get("componentId") == SANDBOXES_COMPONENT_ID
            and str(session.get("branchId", "")) == str(session_branch_id)
            and session.get("id")
        ):
            sessions[config_id].append(str(session["id"]))
    return sessions


def _app_id(client: Any, config_id: str, branch_id: int | None) -> str | None:
    """The ``parameters.id`` of the remote workspace config, or ``None``.

    A ``parameters.id`` means a Python/R or legacy SQL workspace: its backend
    is a Data Science ``/apps`` record that a config-only delete would leave
    running. The remote config is read again right before the delete, so a
    failed read raises and nothing is deleted.
    """
    remote = client.get_config_detail(
        component_id=SANDBOXES_COMPONENT_ID, config_id=config_id, branch_id=branch_id
    )
    configuration = remote.get("configuration") if isinstance(remote, dict) else None
    parameters = configuration.get("parameters") if isinstance(configuration, dict) else None
    app_id = parameters.get("id") if isinstance(parameters, dict) else None
    return str(app_id) if app_id else None


def _app_backed_message(config_id: str, app_id: str) -> str:
    return (
        f"Workspace {config_id} is not deleted: its config has parameters.id ({app_id}), so it "
        "is backed by a Data Science app that a config delete would leave running. Delete it "
        "in the Keboola UI."
    )


def _refuse_app_backed_workspace(client: Any, config_id: str, branch_id: int | None) -> None:
    """Refuse to delete a workspace whose config points at a Data Science app."""
    app_id = _app_id(client, config_id, branch_id)
    if app_id:
        raise KeboolaApiError(
            message=_app_backed_message(config_id, app_id),
            status_code=0,
            error_code=ErrorCode.VALIDATION_ERROR,
            retryable=False,
        )


def _delete_session(client: Any, config_id: str, session_id: str, deleted: list[str]) -> None:
    """Delete one session and record it in *deleted*; a 404 counts as deleted.

    The HTTP layer repeats a DELETE after a 5xx, so a lost 204 comes back as a
    404 on the repeat. Any other failure raises with the ids deleted so far.
    """
    try:
        client.delete_editor_session(session_id)
    except KeboolaApiError as exc:
        if exc.status_code != 404:
            raise KeboolaApiError(
                message=(
                    f"Workspace {config_id} was not deleted: SQL editor session {session_id} "
                    f"could not be deleted ({exc.message}). Sessions already deleted: "
                    f"{', '.join(deleted) or 'none'}."
                ),
                status_code=exc.status_code,
                error_code=exc.error_code,
                retryable=exc.retryable,
                details={"deleted_session_ids": list(deleted), "failed_session_id": session_id},
            ) from exc
    deleted.append(session_id)


def delete_remote_config(client: Any, change: dict[str, Any], branch_id: int | None) -> None:
    """Delete a config on the remote; for a workspace, its sessions first.

    A workspace's sessions are deleted before its configuration, like
    ``kbc remote workspace delete``. A workspace backed by a Data Science app
    is refused. Any failure raises before the config delete, so the push
    records the error (with the ids of the sessions already deleted) and keeps
    the manifest entry. The deleted session ids are recorded on *change*
    (``deleted_session_ids``), one by one; a failed config delete after the
    sessions names them in its error.
    """
    component_id = change["component_id"]
    config_id = change["config_id"]
    if component_id != SANDBOXES_COMPONENT_ID:
        client.delete_config(component_id=component_id, config_id=config_id, branch_id=branch_id)
        return
    _refuse_app_backed_workspace(client, config_id, branch_id)
    session_ids = list_workspace_sessions(client, {config_id}, branch_id)[config_id]
    deleted: list[str] = []
    change["deleted_session_ids"] = deleted
    for session_id in session_ids:
        _delete_session(client, config_id, session_id, deleted)
    try:
        client.delete_config(component_id=component_id, config_id=config_id, branch_id=branch_id)
    except KeboolaApiError as exc:
        # The change is not in pushed_details on a failure, so the error is the
        # only place that says which sessions are already gone.
        raise KeboolaApiError(
            message=(
                f"Workspace {config_id}: the configuration could not be deleted ({exc.message}). "
                f"Its SQL editor sessions were already deleted: {', '.join(deleted) or 'none'}."
            ),
            status_code=exc.status_code,
            error_code=exc.error_code,
            retryable=exc.retryable,
            details={"deleted_session_ids": list(deleted)},
        ) from exc


def preview_deletes(
    service: SyncService,
    alias: str,
    project_root: Path,
    branch_override: int | None,
    dry_result: dict[str, Any],
) -> None:
    """``push --dry-run``: list the SQL editor sessions a delete would remove.

    Adds one ``warnings[]`` entry per deleted workspace with the session count
    and ids (never session details). A workspace backed by a Data Science app
    gets a ``workspace_delete_refused`` warning instead, as push refuses it. A
    read or listing failure becomes a warning that says push would not delete
    the workspace. ``dry_result["changes"]`` holds
    what push would apply (``plan_push``), so without ``--force`` it has no
    delete and nothing is previewed. No API call without a workspace delete.
    """
    deletes = [
        c
        for c in dry_result["changes"]
        if c["change_type"] == "deleted"
        and c["component_id"] == SANDBOXES_COMPONENT_ID
        and not c.get("is_row")
    ]
    if not deletes:
        return
    project = service.resolve_projects([alias])[alias]
    manifest = load_manifest(project_root)
    branch_id = service._resolve_branch_id(
        alias, manifest, project_root, branch_override=branch_override
    )
    warnings = dry_result.setdefault("warnings", [])
    client = service._client_factory(project.stack_url, project.token)
    try:
        with client:
            app_ids = {c["config_id"]: _app_id(client, c["config_id"], branch_id) for c in deletes}
            listed = {config_id for config_id, app_id in app_ids.items() if not app_id}
            sessions = list_workspace_sessions(client, listed, branch_id) if listed else {}
    except KeboolaApiError as exc:
        for change in deletes:
            warnings.append(
                {
                    "change_type": "workspace_sessions_unknown",
                    "component_id": SANDBOXES_COMPONENT_ID,
                    "config_id": change["config_id"],
                    "message": (
                        f"Cannot read workspace {change['config_id']} or list its SQL editor "
                        f"sessions: {exc.message}. Push would not delete this workspace."
                    ),
                }
            )
        return
    for change in deletes:
        app_id = app_ids[change["config_id"]]
        if app_id:
            warnings.append(
                {
                    "change_type": "workspace_delete_refused",
                    "component_id": SANDBOXES_COMPONENT_ID,
                    "config_id": change["config_id"],
                    "message": f"Push will refuse this delete. {_app_backed_message(change['config_id'], app_id)}",
                }
            )
            continue
        session_ids = sessions[change["config_id"]]
        warnings.append(
            {
                "change_type": "workspace_sessions",
                "component_id": SANDBOXES_COMPONENT_ID,
                "config_id": change["config_id"],
                "session_count": len(session_ids),
                "session_ids": session_ids,
                "message": (
                    f"Deleting workspace {change['config_id']} also deletes "
                    f"{len(session_ids)} SQL editor session(s) and their workspaces: "
                    f"{', '.join(session_ids) or '-'}."
                ),
            }
        )


@dataclass(frozen=True)
class ClonedWorkspace:
    """A workspace config a ``sync clone`` created, with its input table ids."""

    config_id: str
    path: str
    name: str
    sources: list[str]


def cloned_workspace(
    config_id: str, path: str, local_data: dict[str, Any]
) -> ClonedWorkspace | None:
    """The input tables of a cloned workspace's ``_config.yml``, or ``None`` if it has none."""
    storage_input = local_data.get("input")
    tables = storage_input.get("tables") if isinstance(storage_input, dict) else None
    if not isinstance(tables, list):
        return None
    sources = [str(t["source"]) for t in tables if isinstance(t, dict) and t.get("source")]
    if not sources:
        return None
    name = str(local_data.get("name", ""))
    return ClonedWorkspace(config_id=config_id, path=path, name=name, sources=sources)


def missing_input_table_warnings(
    service: SyncService, target_alias: str, workspaces: list[ClonedWorkspace]
) -> list[dict[str, Any]]:
    """``sync clone``: one warning per workspace whose input tables the target lacks.

    Clone creates buckets, never tables, so a cloned workspace can point at
    tables that do not exist in the target yet. Called by
    ``_sync_clone_warnings.collect_clone_warnings`` with the workspaces the
    clone created; lists the target's tables once, only when there is one.
    """
    if not workspaces:
        return []
    project = service.resolve_projects([target_alias])[target_alias]
    client = service._client_factory(project.stack_url, project.token)
    try:
        with client:
            existing = {str(t.get("id", "")) for t in client.list_tables()}
    except KeboolaApiError as exc:
        return [
            {
                "change_type": "workspace_input_check_failed",
                "component_id": SANDBOXES_COMPONENT_ID,
                "message": f"Cannot list the target's tables to check workspace inputs: {exc.message}",
            }
        ]
    warnings: list[dict[str, Any]] = []
    for workspace in workspaces:
        missing = [source for source in workspace.sources if source not in existing]
        if missing:
            warnings.append(
                {
                    "change_type": "workspace_input_tables_missing",
                    "component_id": SANDBOXES_COMPONENT_ID,
                    "config_id": workspace.config_id,
                    "path": workspace.path,
                    "missing_tables": missing,
                    "message": (
                        f"Workspace {workspace.name or workspace.path}: input table(s) "
                        f"missing in the target: {', '.join(missing)}. Clone creates buckets, "
                        "not tables; load the tables before you open the workspace."
                    ),
                }
            )
    return warnings
