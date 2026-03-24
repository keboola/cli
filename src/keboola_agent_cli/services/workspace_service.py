"""Workspace service - business logic for workspace lifecycle management.

Orchestrates workspace CRUD, table loading, SQL query execution via Query Service,
and high-level from-transformation workflow. Provides multi-project list and
single-project operations.
"""

import logging
from typing import Any

from ..errors import ConfigError, KeboolaApiError
from ..models import ProjectConfig
from .base import BaseService

logger = logging.getLogger(__name__)


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

    def create_workspace(
        self,
        alias: str,
        name: str = "",
        backend: str = "snowflake",
        read_only: bool = True,
        ui_mode: bool = False,
    ) -> dict[str, Any]:
        """Create a new workspace.

        Two modes:
        - Default (headless): fast (~1s) via Storage API. Not visible in Keboola UI.
        - UI mode (--ui): slower (~15s) via Queue job. Visible in UI Workspaces tab.

        IMPORTANT: Password is only available on creation (headless mode).
        In UI mode, password must be retrieved via 'workspace password' command.

        Args:
            alias: Project alias.
            name: Human-readable name for the workspace.
            backend: Workspace backend (snowflake, bigquery, etc.).
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
                    backend,
                )
            else:
                return self._create_workspace_direct(
                    client,
                    alias,
                    effective_name,
                    config_id,
                    branch_id,
                    backend,
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
        ws_data = client.create_config_workspace(
            branch_id=branch_id,
            component_id="keboola.sandboxes",
            config_id=config_id,
            backend=backend,
        )

        connection = ws_data.get("connection", {})
        return {
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
            "message": (
                f"Workspace '{name}' created in project '{alias}'. "
                "Save the password -- it cannot be retrieved later!"
            ),
        }

    def _create_workspace_via_job(
        self,
        client: Any,
        alias: str,
        name: str,
        config_id: str,
        backend: str,
    ) -> dict[str, Any]:
        """Create workspace via Queue job (slower, visible in UI)."""
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
                error_code="WORKSPACE_NOT_FOUND",
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

    def list_workspaces(
        self,
        aliases: list[str] | None = None,
    ) -> dict[str, Any]:
        """List workspaces across one or multiple projects.

        Args:
            aliases: Project aliases to query. None means all projects.

        Returns:
            Dict with "workspaces" and "errors" lists.
        """
        projects = self.resolve_projects(aliases)

        def worker(
            alias: str, project: ProjectConfig
        ) -> tuple[str, list[dict[str, Any]], bool] | tuple[str, dict[str, str]]:
            client = self._client_factory(project.stack_url, project.token)
            try:
                branch_id = self._resolve_branch_id(alias, project)
                raw_workspaces = client.list_workspaces(branch_id=branch_id)
                workspaces: list[dict[str, Any]] = []
                for ws in raw_workspaces:
                    connection = ws.get("connection", {})
                    workspaces.append(
                        {
                            "project_alias": alias,
                            "id": ws.get("id"),
                            "name": ws.get("name", ""),
                            "backend": connection.get("backend", ""),
                            "host": connection.get("host", ""),
                            "schema": connection.get("schema", ""),
                            "user": connection.get("user", ""),
                            "created": ws.get("created", ""),
                            "component_id": ws.get("component") or "",
                            "config_id": ws.get("configurationId") or "",
                        }
                    )
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

    def get_workspace(self, alias: str, workspace_id: int) -> dict[str, Any]:
        """Get workspace details (password NOT included).

        Args:
            alias: Project alias.
            workspace_id: Workspace ID.

        Returns:
            Dict with workspace details.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        branch_id = self._resolve_branch_id(alias, project)

        client = self._client_factory(project.stack_url, project.token)
        try:
            ws_data = client.get_workspace(workspace_id, branch_id=branch_id)
        finally:
            client.close()

        connection = ws_data.get("connection", {})
        return {
            "project_alias": alias,
            "workspace_id": ws_data.get("id"),
            "backend": connection.get("backend", ""),
            "host": connection.get("host", ""),
            "warehouse": connection.get("warehouse", ""),
            "database": connection.get("database", ""),
            "schema": connection.get("schema", ""),
            "user": connection.get("user", ""),
            "created": ws_data.get("created", ""),
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
    ) -> dict[str, Any]:
        """Load tables into a workspace.

        Builds table mapping from table IDs, using the last segment as the
        destination name. Waits for the async storage job to complete.

        Args:
            alias: Project alias.
            workspace_id: Workspace ID.
            tables: List of table IDs (e.g. "in.c-bucket.table-name").

        Returns:
            Dict with load job results.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        branch_id = self._resolve_branch_id(alias, project)

        # Build table load definitions
        table_defs: list[dict[str, Any]] = []
        for table_id in tables:
            # Use last part of table ID as destination name
            parts = table_id.split(".")
            destination = parts[-1] if parts else table_id
            table_defs.append(
                {
                    "source": table_id,
                    "destination": destination,
                }
            )

        client = self._client_factory(project.stack_url, project.token)
        try:
            job_result = client.load_workspace_tables(workspace_id, table_defs, branch_id=branch_id)
        finally:
            client.close()

        return {
            "project_alias": alias,
            "workspace_id": workspace_id,
            "tables_loaded": len(tables),
            "table_ids": tables,
            "job_id": job_result.get("id"),
            "job_status": job_result.get("status", ""),
            "message": f"Loaded {len(tables)} table(s) into workspace {workspace_id}.",
        }

    def execute_query(
        self,
        alias: str,
        workspace_id: int,
        sql: str,
        transactional: bool = False,
    ) -> dict[str, Any]:
        """Execute SQL query in a workspace via Query Service.

        Submits the query, polls until complete, and exports CSV results
        for each statement.

        Args:
            alias: Project alias.
            workspace_id: Workspace ID.
            sql: SQL statement(s) to execute.
            transactional: Whether to wrap in a transaction.

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

            # Export results for each statement
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

                # Try to export results if there are rows
                if status == "completed" and num_rows > 0:
                    try:
                        csv_data = client.export_query_results(query_job_id, stmt_id)
                        result_entry["csv_data"] = csv_data
                    except KeboolaApiError:
                        logger.debug("Could not export results for statement %s", stmt_id)

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

    def create_from_transformation(
        self,
        alias: str,
        component_id: str,
        config_id: str,
        row_id: str | None = None,
        backend: str = "snowflake",
    ) -> dict[str, Any]:
        """Create a workspace from a transformation config.

        Reads the transformation configuration, extracts input table mappings,
        creates a config-tied workspace, and loads the input tables.

        Args:
            alias: Project alias.
            component_id: Transformation component ID (e.g. keboola.snowflake-transformation).
            config_id: Configuration ID.
            row_id: Optional row ID for row-based transformations.
            backend: Workspace backend.

        Returns:
            Dict with workspace details and loaded tables.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        branch_id = self._resolve_branch_id(alias, project)

        client = self._client_factory(project.stack_url, project.token)
        try:
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
            ws_data = client.create_config_workspace(
                branch_id=branch_id,
                component_id=component_id,
                config_id=config_id,
                backend=backend,
            )

            workspace_id = ws_data.get("id")
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
                client.load_workspace_tables(workspace_id, table_defs, branch_id=branch_id)

            return {
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
                "message": (
                    f"Workspace {workspace_id} created from transformation "
                    f"'{config_id}' with {len(source_tables)} table(s) loaded. "
                    "Save the password -- it cannot be retrieved later!"
                ),
            }
        finally:
            client.close()
