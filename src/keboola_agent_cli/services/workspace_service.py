"""Workspace service - business logic for workspace lifecycle management.

Orchestrates workspace CRUD, table loading, SQL query execution via Query Service,
and high-level from-transformation workflow. Provides multi-project list and
single-project operations.
"""

import csv
import io
import json
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ..client import _collect_inline_results
from ..constants import (
    BIGQUERY_WORKSPACE_LOGIN_TYPE,
    QUERY_RESULTS_DEFAULT_LIMIT,
    QUERY_SERVICE_COMPATIBLE_LOGIN_TYPES,
    QUERY_SERVICE_COMPATIBLE_LOGIN_TYPES_BIGQUERY,
    SNOWFLAKE_WORKSPACE_LOGIN_TYPE,
    WORKSPACE_LOAD_COPY_GUARD_BYTES,
    WORKSPACE_LOAD_JOB_MAX_WAIT,
    WORKSPACE_LOAD_TYPES,
)
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..models import ProjectConfig
from ._workspace_load_plan import (
    LOAD_TYPE_CLONE,
    LOAD_TYPE_COPY,
    LOAD_TYPE_VIEW,
    LoadTablePlan,
    coerce_data_size_bytes,
    plan_auto_load_type,
)
from .base import BaseService

logger = logging.getLogger(__name__)


def _summarize_load_types(plans: list[LoadTablePlan]) -> str:
    """Render "2 clone, 1 copy" for the human success message."""
    counts = Counter(plan.load_type.lower() for plan in plans)
    return ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))


@dataclass(frozen=True)
class SnowflakeWorkspaceKeyPair:
    """PEM key material for Snowflake key-pair workspace authentication."""

    private_pem: str
    public_pem: str


def _classify_qs_compatibility(login_type: str, backend: str) -> bool:
    """Map a workspace ``(loginType, backend)`` pair to Query-Service compat.

    Compatibility is keyed by BOTH backend and loginType because the same
    ``default`` string means opposite things per backend: a BigQuery workspace's
    ``default`` loginType IS Query-Service-compatible (verified against project
    9621 on connection.keboola.com), whereas a Snowflake legacy ``default``
    workspace is NOT ('JWT token is invalid'). See the two whitelists in
    ``constants`` for the empirical rationale.

    Conservative whitelist semantics: returns True only for ``loginType``s
    confirmed to work with POST /v2/storage/branch/{ID}/workspaces/{WS}/query.
    Unknown backends fall through to the Snowflake whitelist (false negatives
    over false positives).
    """
    if backend.lower() == "bigquery":
        return login_type in QUERY_SERVICE_COMPATIBLE_LOGIN_TYPES_BIGQUERY
    return login_type in QUERY_SERVICE_COMPATIBLE_LOGIN_TYPES


def _workspace_login_type_for_backend(backend: str) -> str | None:
    """Return the loginType kbagent should request for newly created workspaces."""
    normalized = backend.lower()
    if normalized == "snowflake":
        return SNOWFLAKE_WORKSPACE_LOGIN_TYPE
    if normalized == "bigquery":
        # BigQuery's Query-Service-compatible loginType. Omitting it lets the
        # backend default to the same value, but requesting it explicitly keeps
        # parity with keboola-mcp-server and is robust to a server-side change
        # of the implicit default.
        return BIGQUERY_WORKSPACE_LOGIN_TYPE
    return None


def _generate_snowflake_workspace_key_pair() -> SnowflakeWorkspaceKeyPair:
    """Generate the unencrypted PEM key pair required by Snowflake workspaces."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_key_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return SnowflakeWorkspaceKeyPair(private_pem=private_key_pem, public_pem=public_key_pem)


def _workspace_key_pair_for_backend(backend: str) -> SnowflakeWorkspaceKeyPair | None:
    """Return private/public key material for backends that require it."""
    if backend.lower() == "snowflake":
        return _generate_snowflake_workspace_key_pair()
    return None


def find_storage_workspace_for_sandbox_config(
    workspaces: list[dict[str, Any]],
    config_id: str,
) -> int | None:
    """Pure-function lookup: find the Storage workspace that backs a sandbox config.

    A ``keboola.sandboxes`` configuration's ``parameters.id`` is the
    sandbox-service internal ID, not a Storage workspace ID -- passing it to
    ``GET /v2/storage/workspaces/{ID}`` returns 404 (issue #304). The real
    relation goes the other direction: each Storage workspace exposes
    ``configurationId`` pointing back at its sandbox config.

    Extracted from ``WorkspaceService.resolve_sandbox_workspace_id`` so
    ``ConfigService.get_config_detail`` can call it with a workspace list it
    already has (avoiding a circular ``ConfigService -> WorkspaceService``
    dependency and the extra HTTP round-trip that would otherwise pile up
    in HTTP and web-UI consumers -- see issue #312).

    Args:
        workspaces: Raw output of ``KeboolaClient.list_workspaces()`` -- each
            entry is the Storage API workspace dict (not the normalised CLI
            shape).
        config_id: ``keboola.sandboxes`` configuration ID.

    Returns:
        Storage workspace ID (int), or None if no workspace currently backs
        this config (orphan sandbox, or workspace deleted but config kept
        around).
    """
    for ws in workspaces:
        if ws.get("component") == "keboola.sandboxes" and str(ws.get("configurationId", "")) == str(
            config_id
        ):
            ws_id = ws.get("id")
            if isinstance(ws_id, int):
                return ws_id
            try:
                return int(ws_id) if ws_id is not None else None
            except (TypeError, ValueError):
                return None
    return None


def _is_orphaned_workspace(ws: dict[str, Any], config_names: dict[str, str]) -> bool:
    """Return True if a workspace has no backing keboola.sandboxes config.

    A workspace is orphaned when it is tied to keboola.sandboxes (the normal
    kbagent creation path) but the sandbox config no longer exists — either it
    was deleted separately or was never created.
    """
    component_id = ws.get("component_id", "")
    config_id = str(ws.get("config_id", ""))
    if component_id != "keboola.sandboxes":
        return False
    # config_names keys are sandbox config IDs; absence means orphan
    return not config_id or config_id not in config_names


def _csv_cell(value: Any) -> Any:
    """Coerce one `/results` JSON cell to its CSV representation.

    ``None`` -> empty field (matches the warehouse CSV export). VARIANT/ARRAY/
    OBJECT (Snowflake) and STRUCT/ARRAY (BigQuery) columns arrive as native
    Python ``dict``/``list``; ``csv.writer`` would otherwise emit their Python
    ``repr`` (``{'k': 'v'}``), so we serialize them as compact JSON
    (``{"k":"v"}``) to match the warehouse's CSV serialization. Scalars pass
    through and are stringified by ``csv.writer``.
    """
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return value


def _rows_to_csv(columns: list[dict[str, Any]], rows: list[list[Any]]) -> str:
    """Render structured columns+rows as an RFC-4180 CSV string.

    Synthesized so the inline `/results` payload stays drop-in compatible with
    consumers that still read ``csv_data`` (CLI preview, web UI table + export
    buttons, REST). ``csv.writer`` handles quoting/escaping.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([col.get("name", "") for col in columns])
    for row in rows:
        writer.writerow([_csv_cell(value) for value in row])
    return buffer.getvalue()


class WorkspaceService(BaseService):
    """Business logic for managing Keboola workspaces.

    Supports:
    - Workspace CRUD (create, list, detail, delete, password reset)
    - Table loading into workspaces
    - SQL query execution via Query Service
    - High-level from-transformation workflow

    Uses dependency injection for config_store and client_factory.
    """

    def _resolve_branch_id(self, alias: str, project: ProjectConfig) -> int:
        """Resolve the effective branch ID for a project.

        Uses active_branch_id if set, otherwise fetches main branch from API.

        Returns:
            Branch ID (int).
        """
        if project.active_branch_id is not None:
            return project.active_branch_id

        client = self._client_factory(project.stack_url, project.token)
        try:
            branches = client.list_dev_branches()
            for branch in branches:
                if branch.get("isDefault", False):
                    return int(branch["id"])
            raise ConfigError(
                f"No default branch found for project '{alias}'. "
                "Set an active branch with 'kbagent branch use'."
            )
        finally:
            client.close()

    def _fetch_sandbox_config_names(
        self, client: Any, branch_id: int | None = None
    ) -> dict[str, str]:
        """Fetch sandbox configuration names for workspace name resolution.

        The workspace API returns internal names (WORKSPACE_xxxxx). The user-given
        name lives on the keboola.sandboxes configuration. This method fetches those
        configs and builds a config_id -> name mapping.

        Returns:
            Dict mapping config_id (str) to config name.
        """
        try:
            configs = client.list_component_configs("keboola.sandboxes", branch_id=branch_id)
            return {str(cfg.get("id", "")): cfg.get("name", "") for cfg in configs}
        except Exception:
            # Non-critical: fall back to internal workspace names
            return {}

    def _detect_backend(self, client: Any) -> str:
        """Detect the project's default backend via token verification.

        Returns:
            Backend string (e.g. 'snowflake', 'bigquery').
        """
        token_info = client.verify_token()
        return token_info.default_backend

    def create_workspace(
        self,
        alias: str,
        name: str = "",
        backend: str | None = None,
        read_only: bool = True,
        ui_mode: bool = False,
    ) -> dict[str, Any]:
        """Create a new workspace.

        Two modes:
        - Default (headless): fast (~1s) via Storage API. Not visible in Keboola UI.
        - UI mode (--ui): slower (~15s) via Queue job. Visible in UI Workspaces tab.

        IMPORTANT: Credentials are only available on creation (headless mode).
        Snowflake returns a generated private key; password-based workspaces
        return a password. In UI mode, password must be retrieved via
        'workspace password' command.

        Args:
            alias: Project alias.
            name: Human-readable name for the workspace.
            backend: Workspace backend. Auto-detected from project if None.
            read_only: Whether the workspace has read-only storage access.
            ui_mode: If True, create via Queue job (visible in Keboola UI).

        Returns:
            Dict with workspace details including connection credentials.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        branch_id = self._resolve_branch_id(alias, project)
        effective_name = name or f"kbagent-{alias}"

        client = self._client_factory(project.stack_url, project.token)
        try:
            effective_backend = backend or self._detect_backend(client)
            # Step 1: Create keboola.sandboxes config (in the correct branch)
            sandbox_config = client.create_sandbox_config(
                name=effective_name,
                description="Created by kbagent CLI",
                branch_id=branch_id,
            )
            config_id = sandbox_config.get("id", "")

            if ui_mode:
                return self._create_workspace_via_job(
                    client,
                    alias,
                    effective_name,
                    config_id,
                    effective_backend,
                )
            else:
                return self._create_workspace_direct(
                    client,
                    alias,
                    effective_name,
                    config_id,
                    branch_id,
                    effective_backend,
                    read_only,
                )
        finally:
            client.close()

    def _create_workspace_direct(
        self,
        client: Any,
        alias: str,
        name: str,
        config_id: str,
        branch_id: int,
        backend: str,
        read_only: bool,
    ) -> dict[str, Any]:
        """Create workspace via Storage API (fast, headless)."""
        key_pair = _workspace_key_pair_for_backend(backend)
        ws_data = client.create_config_workspace(
            branch_id=branch_id,
            component_id="keboola.sandboxes",
            config_id=config_id,
            backend=backend,
            login_type=_workspace_login_type_for_backend(backend),
            public_key=key_pair.public_pem if key_pair else None,
        )

        connection = ws_data.get("connection", {})
        credential_label = "private key" if key_pair else "password"
        result = {
            "project_alias": alias,
            "workspace_id": ws_data.get("id"),
            "name": name,
            "config_id": config_id,
            "backend": connection.get("backend", backend),
            "host": connection.get("host", ""),
            "warehouse": connection.get("warehouse", ""),
            "database": connection.get("database", ""),
            "schema": connection.get("schema", ""),
            "user": connection.get("user", ""),
            "password": connection.get("password", ""),
            "read_only": read_only,
            "ui_mode": False,
            "message": f"Workspace '{name}' created in project '{alias}'. "
            f"Save the {credential_label} -- it cannot be retrieved later!",
        }
        if key_pair is not None:
            result["private_key"] = key_pair.private_pem
        return result

    def _create_workspace_via_job(
        self,
        client: Any,
        alias: str,
        name: str,
        config_id: str,
        backend: str,
    ) -> dict[str, Any]:
        """Create workspace via Queue job (slower, visible in UI)."""
        # The Queue job path does not expose a publicKey/loginType input. Keep
        # returning a reset password here; headless Snowflake creates use the
        # key-pair path in _create_workspace_direct().
        job = client.create_job(
            component_id="keboola.sandboxes",
            config_id=config_id,
            config_data={
                "parameters": {
                    "task": "create",
                    "type": backend,
                    "shared": False,
                },
            },
        )
        job_id = str(job.get("id", ""))

        # Wait for the job to complete
        client.wait_for_queue_job(job_id)

        # Find the workspace created by the job
        workspaces = client.list_config_workspaces(
            branch_id=int(job.get("branchId", 0)),
            component_id="keboola.sandboxes",
            config_id=config_id,
        )

        if not workspaces:
            raise KeboolaApiError(
                message=f"Sandbox job completed but no workspace found for config {config_id}",
                status_code=500,
                error_code=ErrorCode.WORKSPACE_NOT_FOUND,
                retryable=False,
            )

        ws_data = workspaces[0]
        connection = ws_data.get("connection", {})
        workspace_id = ws_data.get("id")

        # Reset password so we can return it (job doesn't expose the initial password)
        password = ""
        try:
            pw_data = client.reset_workspace_password(workspace_id)
            password = pw_data.get("password", "")
        except KeboolaApiError:
            logger.debug("Could not reset password for workspace %s", workspace_id)

        return {
            "project_alias": alias,
            "workspace_id": workspace_id,
            "name": name,
            "config_id": config_id,
            "backend": connection.get("backend", backend),
            "host": connection.get("host", ""),
            "warehouse": connection.get("warehouse", ""),
            "database": connection.get("database", ""),
            "schema": connection.get("schema", ""),
            "user": connection.get("user", ""),
            "password": password,
            "read_only": True,
            "ui_mode": True,
            "message": (
                f"Workspace '{name}' ({workspace_id}) created in project '{alias}' (visible in UI). "
                "Save the password -- it cannot be retrieved later!"
            ),
        }

    def resolve_sandbox_workspace_id(
        self,
        alias: str,
        config_id: str,
        branch_id: int | None = None,
    ) -> int | None:
        """Map a ``keboola.sandboxes`` config ID to its Storage workspace ID.

        The sandbox config's ``parameters.id`` field looks like a Storage
        workspace ID but is actually a sandbox-service-internal handle --
        passing it to ``GET /v2/storage/workspaces/{ID}`` returns 404 (issue
        #304). The real mapping lives the other way around: each Storage
        workspace exposes ``configurationId`` pointing back at its sandbox
        config. This helper walks the workspace list to find the matching
        one.

        Args:
            alias: Project alias.
            config_id: ``keboola.sandboxes`` configuration ID.
            branch_id: Branch to query. When None, uses the project's
                resolved branch (production fallback).

        Returns:
            Storage workspace ID, or None if no workspace is currently
            backed by this config (orphan sandbox, or workspace deleted but
            config kept around).
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch = (
            branch_id if branch_id is not None else self._resolve_branch_id(alias, project)
        )

        client = self._client_factory(project.stack_url, project.token)
        try:
            workspaces = client.list_workspaces(branch_id=effective_branch)
        finally:
            client.close()

        return find_storage_workspace_for_sandbox_config(workspaces, config_id)

    def list_workspaces(
        self,
        aliases: list[str] | None = None,
        orphaned_only: bool = False,
        branch_id: int | None = None,
        qs_compatible_only: bool = False,
    ) -> dict[str, Any]:
        """List workspaces across one or multiple projects.

        Args:
            aliases: Project aliases to query. None means all projects.
            orphaned_only: If True, return only orphaned workspaces — those
                whose keboola.sandboxes config no longer exists.
            branch_id: When set, list workspaces from this specific dev branch
                (`/v2/storage/branch/{ID}/workspaces`). When None, the
                production endpoint is used; callers wanting to honour the
                alias's pinned branch should resolve it via
                ``resolve_branch()`` in the command layer before calling.
                Only valid with a single alias (mirrors storage commands).
            qs_compatible_only: If True, return only workspaces whose
                ``login_type`` is in ``QUERY_SERVICE_COMPATIBLE_LOGIN_TYPES``
                AND that are read-only -- the canonical shape for a Streamlit
                / Quix data-app reading via the Query Service.

        Returns:
            Dict with "workspaces" and "errors" lists. Each workspace entry
            carries ``login_type``, ``read_only``, ``qs_compatible``,
            ``database`` and ``warehouse`` so callers can pick a
            data-app-compatible workspace without spawning a probe query
            (closes #304).
        """
        if branch_id is not None and (aliases is None or len(aliases) != 1):
            raise ConfigError(
                "branch_id requires exactly one alias (a branch ID is scoped to a single project)."
            )

        projects = self.resolve_projects(aliases)

        def worker(
            alias: str, project: ProjectConfig
        ) -> tuple[str, list[dict[str, Any]], bool] | tuple[str, dict[str, str]]:
            client = self._client_factory(project.stack_url, project.token)
            try:
                effective_branch = (
                    branch_id if branch_id is not None else self._resolve_branch_id(alias, project)
                )
                raw_workspaces = client.list_workspaces(branch_id=effective_branch)

                # Fetch sandbox configs to resolve user-given names
                config_names = self._fetch_sandbox_config_names(client, effective_branch)

                workspaces: list[dict[str, Any]] = []
                for ws in raw_workspaces:
                    connection = ws.get("connection", {})
                    config_id = ws.get("configurationId") or ""
                    component_id = ws.get("component") or ""
                    login_type = connection.get("loginType", "") or ""
                    backend = connection.get("backend", "") or ""
                    read_only = bool(ws.get("readOnlyStorageAccess", False))
                    entry = {
                        "project_alias": alias,
                        "id": ws.get("id"),
                        "name": config_names.get(str(config_id), ws.get("name", "")),
                        "backend": backend,
                        "host": connection.get("host", ""),
                        "database": connection.get("database", ""),
                        "warehouse": connection.get("warehouse", ""),
                        "schema": connection.get("schema", ""),
                        "user": connection.get("user", ""),
                        "created": ws.get("created", ""),
                        "component_id": component_id,
                        "config_id": config_id,
                        "login_type": login_type,
                        "read_only": read_only,
                        "qs_compatible": _classify_qs_compatibility(login_type, backend),
                    }
                    if orphaned_only:
                        if _is_orphaned_workspace(entry, config_names):
                            workspaces.append(entry)
                    elif qs_compatible_only:
                        if entry["qs_compatible"] and entry["read_only"]:
                            workspaces.append(entry)
                    else:
                        workspaces.append(entry)
                return (alias, workspaces, True)
            except KeboolaApiError as exc:
                return (
                    alias,
                    {
                        "project_alias": alias,
                        "error_code": exc.error_code,
                        "message": exc.message,
                    },
                )
            except Exception as exc:
                return (
                    alias,
                    {
                        "project_alias": alias,
                        "error_code": "UNEXPECTED_ERROR",
                        "message": str(exc),
                    },
                )
            finally:
                client.close()

        successes, errors = self._run_parallel(projects, worker)

        all_workspaces: list[dict[str, Any]] = []
        for _alias, workspaces, _ok in successes:
            all_workspaces.extend(workspaces)

        all_workspaces.sort(key=lambda w: (w["project_alias"], w.get("id", 0)))
        errors.sort(key=lambda e: e.get("project_alias", ""))

        return {
            "workspaces": all_workspaces,
            "errors": errors,
        }

    def gc_workspaces(
        self,
        aliases: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete all orphaned workspaces (workspace GC).

        An orphan is a keboola.sandboxes-backed workspace whose config no longer
        exists. Reuses delete_workspace for each orphan so the sandbox config
        cleanup path is also exercised.

        Args:
            aliases: Project aliases to query. None means all projects.
            dry_run: If True, list orphans without deleting.

        Returns:
            Dict with dry_run flag, would_delete/deleted list, errors, count.
        """
        orphan_result = self.list_workspaces(aliases=aliases, orphaned_only=True)
        orphans = orphan_result["workspaces"]
        list_errors = orphan_result["errors"]

        if dry_run:
            return {
                "dry_run": True,
                "would_delete": orphans,
                "count": len(orphans),
                "errors": list_errors,
                "message": (
                    f"DRY RUN: {len(orphans)} orphaned workspace(s) would be deleted."
                    + (" No errors." if not list_errors else f" {len(list_errors)} list error(s).")
                ),
            }

        deleted: list[dict[str, Any]] = []
        delete_errors: list[dict[str, Any]] = []
        for ws in orphans:
            try:
                self.delete_workspace(alias=ws["project_alias"], workspace_id=ws["id"])
                deleted.append(ws)
            except Exception as exc:
                # Full traceback goes to the logger (observability for unexpected
                # errors like AttributeError); user-facing flow is unchanged.
                logger.exception(
                    "Failed to delete orphaned workspace %s in project %s",
                    ws["id"],
                    ws["project_alias"],
                )
                delete_errors.append(
                    {
                        "workspace_id": ws["id"],
                        "project_alias": ws["project_alias"],
                        "error": str(exc),
                    }
                )

        all_errors = list_errors + delete_errors
        return {
            "dry_run": False,
            "deleted": deleted,
            "errors": all_errors,
            "count_deleted": len(deleted),
            "count_errors": len(all_errors),
            "message": (
                f"GC complete: {len(deleted)} orphaned workspace(s) deleted"
                + (f", {len(all_errors)} error(s)." if all_errors else ".")
            ),
        }

    def get_workspace(
        self,
        alias: str,
        workspace_id: int,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Get workspace details (password NOT included).

        Args:
            alias: Project alias.
            workspace_id: Workspace ID.
            branch_id: When set, query the branch-scoped endpoint
                ``/v2/storage/branch/{ID}/workspaces/{WS}``. When None, falls
                back to the project's active branch (or the production
                endpoint if no active branch is pinned). Explicit None vs.
                resolved value lets the command layer surface a "production
                branch used" notice without changing the service signature.

        Returns:
            Dict with workspace details including ``login_type``,
            ``read_only`` and ``qs_compatible`` so callers can pick a
            Query-Service-compatible workspace without firing a probe query
            (closes #304).
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch = (
            branch_id if branch_id is not None else self._resolve_branch_id(alias, project)
        )

        client = self._client_factory(project.stack_url, project.token)
        try:
            ws_data = client.get_workspace(workspace_id, branch_id=effective_branch)
        finally:
            client.close()

        connection = ws_data.get("connection", {})
        login_type = connection.get("loginType", "") or ""
        backend = connection.get("backend", "") or ""
        return {
            "project_alias": alias,
            "workspace_id": ws_data.get("id"),
            "backend": backend,
            "host": connection.get("host", ""),
            "warehouse": connection.get("warehouse", ""),
            "database": connection.get("database", ""),
            "schema": connection.get("schema", ""),
            "user": connection.get("user", ""),
            "created": ws_data.get("created", ""),
            "login_type": login_type,
            "read_only": bool(ws_data.get("readOnlyStorageAccess", False)),
            "qs_compatible": _classify_qs_compatibility(login_type, backend),
            "component_id": ws_data.get("component", "") or "",
            "config_id": ws_data.get("configurationId", "") or "",
        }

    def delete_workspace(self, alias: str, workspace_id: int) -> dict[str, Any]:
        """Delete a workspace and its associated sandboxes config (if any).

        Args:
            alias: Project alias.
            workspace_id: Workspace ID.

        Returns:
            Dict confirming the deletion.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        branch_id = self._resolve_branch_id(alias, project)

        client = self._client_factory(project.stack_url, project.token)
        try:
            # Get workspace details to find associated config
            config_id = None
            component = None
            try:
                ws_data = client.get_workspace(workspace_id, branch_id=branch_id)
                component = ws_data.get("component")
                config_id = ws_data.get("configurationId")
            except KeboolaApiError:
                pass  # Workspace might not exist, proceed with delete

            # Delete the workspace
            client.delete_workspace(workspace_id, branch_id=branch_id)

            # Clean up associated sandboxes config (in the correct branch)
            if config_id and component == "keboola.sandboxes":
                try:
                    client.delete_config("keboola.sandboxes", config_id, branch_id=branch_id)
                except KeboolaApiError:
                    logger.debug("Could not delete sandbox config %s", config_id)
        finally:
            client.close()

        return {
            "project_alias": alias,
            "workspace_id": workspace_id,
            "message": f"Workspace {workspace_id} deleted from project '{alias}'.",
        }

    def reset_password(self, alias: str, workspace_id: int) -> dict[str, Any]:
        """Reset workspace password.

        Args:
            alias: Project alias.
            workspace_id: Workspace ID.

        Returns:
            Dict with the new password.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        branch_id = self._resolve_branch_id(alias, project)

        client = self._client_factory(project.stack_url, project.token)
        try:
            result = client.reset_workspace_password(workspace_id, branch_id=branch_id)
        finally:
            client.close()

        return {
            "project_alias": alias,
            "workspace_id": workspace_id,
            "password": result.get("password", ""),
            "message": (
                f"Password reset for workspace {workspace_id} in project '{alias}'. "
                "Save the new password -- it cannot be retrieved later!"
            ),
        }

    def load_tables(
        self,
        alias: str,
        workspace_id: int,
        tables: list[str],
        preserve: bool = False,
        load_type: str | None = None,
        force: bool = False,
        timeout: float | None = None,
        on_copy_guard: Callable[[list[LoadTablePlan]], bool] | None = None,
    ) -> dict[str, Any]:
        """Load tables into a workspace.

        Builds table mapping from table IDs, using the last segment as the
        destination name. Waits for the async storage job to complete.

        Load type resolution (issue #687):

        * ``load_type=None`` (default) is AUTO: the workspace backend and each
          table's detail are fetched, and every table that can be cloned is
          cloned -- a zero-copy registration that finishes in seconds instead
          of physically re-materializing the data. Tables that cannot fall
          back to COPY individually, each carrying the reason.
        * An explicit ``clone`` / ``copy`` / ``view`` is sent as asked for
          every table. Clone/view eligibility is NOT pre-validated: the server
          rejects an impossible combination with a precise 400, which beats a
          client-side rule that can drift.

        A COPY larger than ``WORKSPACE_LOAD_COPY_GUARD_BYTES`` is guarded --
        it is the case that costs real warehouse time, and the one users did
        not realise they were asking for. ``force=True`` skips the guard;
        otherwise ``on_copy_guard`` (interactive callers only) may approve it.
        With no callback -- ``--json``, ``kbagent serve``, any non-TTY -- the
        load is refused rather than started behind the caller's back.

        Args:
            alias: Project alias.
            workspace_id: Workspace ID.
            tables: List of table IDs (e.g. "in.c-bucket.table-name").
            preserve: If True, keep existing tables in the workspace.
            load_type: One of ``clone`` / ``copy`` / ``view`` (case-insensitive),
                or None for the auto decision.
            force: Skip the large-COPY size guard.
            timeout: Seconds to wait for the load job. ``None`` defaults to
                WORKSPACE_LOAD_JOB_MAX_WAIT; any other value must be > 0.
            on_copy_guard: Called with the oversized COPY plans; return True to
                proceed, False to refuse. None means "refuse without asking".

        Returns:
            Dict with load job results, including a per-table ``tables`` list.

        Raises:
            KeboolaApiError: INVALID_ARGUMENT for an unknown load_type or a
                non-positive timeout; WORKSPACE_LOAD_COPY_TOO_LARGE when the
                size guard trips and is neither forced nor approved.
        """
        requested = self._normalize_load_type(load_type)
        max_wait = self._normalize_timeout(timeout)

        projects = self.resolve_projects([alias])
        project = projects[alias]
        branch_id = self._resolve_branch_id(alias, project)

        client = self._client_factory(project.stack_url, project.token)
        try:
            plans = self._plan_table_loads(
                client,
                requested=requested,
                tables=tables,
                workspace_id=workspace_id,
                branch_id=branch_id,
            )
            self._enforce_copy_size_guard(plans, force=force, on_copy_guard=on_copy_guard)

            table_defs = [
                {
                    "source": plan.table_id,
                    # Last segment of the table ID is the workspace-side name.
                    "destination": plan.table_id.split(".")[-1],
                    "loadType": plan.load_type,
                }
                for plan in plans
            ]
            job_result = client.load_workspace_tables(
                workspace_id,
                table_defs,
                branch_id=branch_id,
                preserve=preserve,
                max_wait=max_wait,
            )
        finally:
            client.close()

        return {
            "project_alias": alias,
            "workspace_id": workspace_id,
            "tables_loaded": len(tables),
            "table_ids": tables,
            "job_id": job_result.get("id"),
            "job_status": job_result.get("status", ""),
            "load_type_requested": requested or "auto",
            "tables": [
                {
                    "table_id": plan.table_id,
                    "load_type": plan.load_type.lower(),
                    "data_size_bytes": plan.data_size_bytes,
                    "clone_ineligible_reason": plan.clone_ineligible_reason,
                }
                for plan in plans
            ],
            "message": (
                f"Loaded {len(tables)} table(s) into workspace {workspace_id} "
                f"({_summarize_load_types(plans)})."
            ),
        }

    @staticmethod
    def _normalize_load_type(load_type: str | None) -> str | None:
        """Lowercase and validate an explicit ``--load-type``.

        Returns None for the auto decision.
        """
        if load_type is None:
            return None
        normalized = load_type.strip().lower()
        if normalized not in WORKSPACE_LOAD_TYPES:
            raise KeboolaApiError(
                message=(
                    f"Invalid load_type {load_type!r}. "
                    f"Expected one of: {sorted(WORKSPACE_LOAD_TYPES)}."
                ),
                status_code=0,
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
        return normalized

    @staticmethod
    def _normalize_timeout(timeout: float | None) -> float:
        """Resolve the load-job wait budget, rejecting a non-positive override.

        ``None`` is the documented "use the default" sentinel and branches
        explicitly to ``WORKSPACE_LOAD_JOB_MAX_WAIT`` -- deliberately NOT
        ``timeout or WORKSPACE_LOAD_JOB_MAX_WAIT``, which would silently
        promote a falsy-but-real ``0.0`` to the 300s default instead of
        rejecting it. The CLI and the ``kbagent serve`` router both already
        guard a non-positive timeout before calling in, but a direct
        service/SDK caller would not go through either guard.
        """
        if timeout is None:
            return WORKSPACE_LOAD_JOB_MAX_WAIT
        if timeout <= 0:
            raise KeboolaApiError(
                message=f"Invalid timeout {timeout}. Must be greater than 0.",
                status_code=0,
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
        return timeout

    def _plan_table_loads(
        self,
        client: Any,
        requested: str | None,
        tables: list[str],
        workspace_id: int,
        branch_id: int | None,
    ) -> list[LoadTablePlan]:
        """Resolve the per-table load type before anything is sent.

        Table details are fetched serially and BEFORE the load is enqueued, so
        a typo'd table ID fails the whole run cleanly instead of half-loading
        the workspace. They are fetched only when they can change the outcome:
        for the auto decision (eligibility + size) and for an explicit COPY
        (size guard). An explicit clone/view needs neither.
        """
        if requested in (LOAD_TYPE_CLONE.lower(), LOAD_TYPE_VIEW.lower()):
            wire_type = LOAD_TYPE_CLONE if requested == "clone" else LOAD_TYPE_VIEW
            return [
                LoadTablePlan(
                    table_id=table_id,
                    load_type=wire_type,
                    data_size_bytes=None,
                    clone_ineligible_reason=None,
                )
                for table_id in tables
            ]

        if requested == "copy":
            return [
                LoadTablePlan(
                    table_id=table_id,
                    load_type=LOAD_TYPE_COPY,
                    data_size_bytes=coerce_data_size_bytes(
                        client.get_table_detail(table_id, branch_id=branch_id)
                    ),
                    clone_ineligible_reason=None,
                )
                for table_id in tables
            ]

        workspace = client.get_workspace(workspace_id, branch_id)
        backend = str((workspace.get("connection") or {}).get("backend") or "")
        return [
            plan_auto_load_type(
                backend,
                table_id,
                client.get_table_detail(table_id, branch_id=branch_id),
            )
            for table_id in tables
        ]

    @staticmethod
    def _enforce_copy_size_guard(
        plans: list[LoadTablePlan],
        force: bool,
        on_copy_guard: Callable[[list[LoadTablePlan]], bool] | None,
    ) -> None:
        """Refuse (or ask about) a COPY of a table over the size guard.

        Unknown sizes do not trip the guard: the guard exists to stop a
        surprise, and refusing on "the API did not tell us" would block loads
        that are perfectly small.
        """
        if force:
            return
        oversized = [
            plan
            for plan in plans
            if plan.load_type == LOAD_TYPE_COPY
            and (plan.data_size_bytes or 0) > WORKSPACE_LOAD_COPY_GUARD_BYTES
        ]
        if not oversized:
            return
        if on_copy_guard is not None and on_copy_guard(oversized):
            return

        listed = ", ".join(
            f"{plan.table_id} ({(plan.data_size_bytes or 0) / 1024**3:.1f} GB)"
            for plan in oversized
        )
        raise KeboolaApiError(
            message=(
                f"Refusing to COPY {len(oversized)} table(s) larger than "
                f"{WORKSPACE_LOAD_COPY_GUARD_BYTES / 1024**3:.0f} GB into the workspace: "
                f"{listed}. A COPY physically re-materializes the data (billed warehouse "
                "time); pass --force to proceed, or --load-type clone where the backend "
                "supports it."
            ),
            status_code=0,
            error_code=ErrorCode.WORKSPACE_LOAD_COPY_TOO_LARGE,
        )

    def execute_query(
        self,
        alias: str,
        workspace_id: int,
        sql: str,
        transactional: bool = False,
        full: bool = False,
        limit: int = QUERY_RESULTS_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """Execute SQL query in a workspace via Query Service.

        Submits the query, polls until complete, and fetches results for each
        statement. Two retrieval paths:

        * Default (``full=False``): the fast inline ``GET .../results`` path.
          Reads the already-computed result set as JSON (no warehouse UNLOAD /
          CSV-file materialization), paginated up to ``limit`` rows. Each
          statement carries structured ``columns`` + ``rows`` and a synthesized
          ``csv_data`` (drop-in for the legacy CSV-string consumers).
        * ``full=True``: the legacy ``GET .../export?fileType=csv`` path, which
          materializes the *complete* result set as a CSV file. Slower, but not
          capped at ``limit`` -- use it when you need every row.

        Args:
            alias: Project alias.
            workspace_id: Workspace ID.
            sql: SQL statement(s) to execute.
            transactional: Whether to wrap in a transaction.
            full: Fetch the complete result set via the CSV export path instead
                of the fast inline path.
            limit: Max rows to fetch via the fast inline path (ignored when
                ``full`` is True).

        Returns:
            Dict with query results.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        branch_id = self._resolve_branch_id(alias, project)

        client = self._client_factory(project.stack_url, project.token)
        try:
            # Submit query
            query_job = client.submit_query(
                branch_id=branch_id,
                workspace_id=workspace_id,
                statements=[sql],
                transactional=transactional,
            )

            query_job_id = str(query_job.get("queryJobId", query_job.get("id", "")))

            # Wait for completion
            completed_job = client.wait_for_query_job(query_job_id)

            # Fetch results for each statement
            results: list[dict[str, Any]] = []
            statements = completed_job.get("statements", [])
            for stmt in statements:
                stmt_id = str(stmt.get("id", ""))
                status = stmt.get("status", "")
                num_rows = stmt.get("numberOfRows", stmt.get("resultRows", 0))
                result_entry: dict[str, Any] = {
                    "statement_id": stmt_id,
                    "status": status,
                    "rows_affected": num_rows,
                }

                # Only statements that produced a result set carry rows.
                if status == "completed" and num_rows > 0:
                    self._attach_statement_results(
                        result_entry, client, query_job_id, stmt_id, full=full, limit=limit
                    )

                results.append(result_entry)

            return {
                "project_alias": alias,
                "workspace_id": workspace_id,
                "branch_id": branch_id,
                "query_job_id": query_job_id,
                "status": completed_job.get("status", ""),
                "statements": results,
                "message": f"Query executed in workspace {workspace_id}.",
            }
        finally:
            client.close()

    @staticmethod
    def _attach_statement_results(
        result_entry: dict[str, Any],
        client: Any,
        query_job_id: str,
        stmt_id: str,
        *,
        full: bool,
        limit: int,
    ) -> None:
        """Populate a statement's result entry, fast inline path or full export.

        Failures are swallowed (debug-logged): a result-fetch error must not
        sink an otherwise-successful query -- the statement still reports its
        status and row count, just without the data payload.
        """
        try:
            if full:
                result_entry["csv_data"] = client.export_query_results(query_job_id, stmt_id)
                return
            inline = _collect_inline_results(client, query_job_id, stmt_id, limit)
            result_entry["columns"] = inline.columns
            result_entry["rows"] = inline.rows
            result_entry["row_count"] = len(inline.rows)
            result_entry["total_rows"] = inline.total_rows
            result_entry["truncated"] = inline.truncated
            # Synthesize csv_data so legacy consumers (web UI table/export, CLI
            # preview) keep working without a format change.
            result_entry["csv_data"] = _rows_to_csv(inline.columns, inline.rows)
        except KeboolaApiError:
            logger.debug("Could not fetch results for statement %s", stmt_id)

    def create_from_transformation(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        row_id: str | None = None,
        backend: str | None = None,
        preserve: bool = False,
    ) -> dict[str, Any]:
        """Create a workspace from a transformation config.

        Reads the transformation configuration, extracts input table mappings,
        creates a config-tied workspace, and loads the input tables.

        Args:
            alias: Project alias.
            component_id: Transformation component ID (e.g. keboola.snowflake-transformation).
            config_id: Configuration ID.
            row_id: Optional row ID for row-based transformations.
            backend: Workspace backend. Auto-detected from project if None.
            preserve: If True, keep existing tables in the workspace during load.

        Returns:
            Dict with workspace details and loaded tables.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        branch_id = self._resolve_branch_id(alias, project)

        client = self._client_factory(project.stack_url, project.token)
        try:
            effective_backend = backend or self._detect_backend(client)

            # Read the transformation config
            config_data = client.get_config_detail(component_id, config_id)

            # Extract input mapping from configuration
            configuration = config_data.get("configuration", {})

            # If row_id specified, find the row
            if row_id:
                rows = config_data.get("rows", [])
                target_row = None
                for row in rows:
                    if str(row.get("id", "")) == str(row_id):
                        target_row = row
                        break
                if target_row is None:
                    raise ConfigError(
                        f"Row '{row_id}' not found in config '{config_id}' "
                        f"of component '{component_id}'."
                    )
                configuration = target_row.get("configuration", {})

            storage = configuration.get("storage", {})
            input_tables = storage.get("input", {}).get("tables", [])

            if not input_tables:
                raise ConfigError(
                    f"No input tables found in transformation config '{config_id}'. "
                    "The configuration may not have input mapping defined."
                )

            # Create config-tied workspace
            key_pair = _workspace_key_pair_for_backend(effective_backend)
            ws_data = client.create_config_workspace(
                branch_id=branch_id,
                component_id=component_id,
                config_id=config_id,
                backend=effective_backend,
                login_type=_workspace_login_type_for_backend(effective_backend),
                public_key=key_pair.public_pem if key_pair else None,
            )

            workspace_id = ws_data.get("id")
            # create_config_workspace always returns an id for the new workspace
            assert workspace_id is not None
            connection = ws_data.get("connection", {})

            # Build table load definitions from input mapping
            table_defs: list[dict[str, Any]] = []
            source_tables: list[str] = []
            for table in input_tables:
                source = table.get("source", "")
                destination = table.get("destination", "")
                if source:
                    source_tables.append(source)
                    entry: dict[str, Any] = {"source": source, "destination": destination}
                    # Pass through columns, where_column, where_values if present
                    if table.get("columns"):
                        entry["columns"] = table["columns"]
                    if table.get("where_column"):
                        entry["where_column"] = table["where_column"]
                    if table.get("where_values"):
                        entry["where_values"] = table["where_values"]
                    table_defs.append(entry)

            # Load tables into workspace
            if table_defs:
                client.load_workspace_tables(
                    workspace_id, table_defs, branch_id=branch_id, preserve=preserve
                )

            credential_label = "private key" if key_pair else "password"
            result = {
                "project_alias": alias,
                "workspace_id": workspace_id,
                "branch_id": branch_id,
                "component_id": component_id,
                "config_id": config_id,
                "row_id": row_id,
                "backend": connection.get("backend", backend),
                "host": connection.get("host", ""),
                "warehouse": connection.get("warehouse", ""),
                "database": connection.get("database", ""),
                "schema": connection.get("schema", ""),
                "user": connection.get("user", ""),
                "password": connection.get("password", ""),
                "tables_loaded": source_tables,
                "message": f"Workspace {workspace_id} created from transformation "
                f"'{config_id}' with {len(source_tables)} table(s) loaded. "
                f"Save the {credential_label} -- it cannot be retrieved later!",
            }
            if key_pair is not None:
                result["private_key"] = key_pair.private_pem
            return result
        finally:
            client.close()
