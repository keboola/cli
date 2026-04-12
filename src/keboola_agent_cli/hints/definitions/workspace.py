"""Hint definitions for workspace commands."""

from .. import HintRegistry
from ..models import ClientCall, CommandHint, HintStep, ServiceCall

# ── workspace create ───────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="workspace.create",
        description="Create a new SQL workspace",
        steps=[
            HintStep(
                comment="Create workspace (headless mode)",
                client=ClientCall(
                    method="create_config_workspace",
                    args={"backend": "{backend}"},
                    result_var="workspace",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="WorkspaceService",
                    service_module="workspace_service",
                    method="create_workspace",
                    args={
                        "alias": "{project}",
                        "name": "{name}",
                        "backend": "{backend}",
                        "read_only": "{read_only}",
                    },
                ),
            ),
        ],
        notes=[
            "Service layer handles sandbox config creation + workspace provisioning.",
            "With --ui flag, creates via job run (slower, ~15s) for UI visibility.",
        ],
    )
)

# ── workspace list ─────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="workspace.list",
        description="List workspaces across projects",
        steps=[
            HintStep(
                comment="List workspaces",
                client=ClientCall(
                    method="list_workspaces",
                    args={"branch_id": "{branch}"},
                    result_var="workspaces",
                    result_hint="list[dict]",
                ),
                service=ServiceCall(
                    service_class="WorkspaceService",
                    service_module="workspace_service",
                    method="list_workspaces",
                    args={"aliases": "{project}"},
                ),
            ),
        ],
    )
)

# ── workspace detail ───────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="workspace.detail",
        description="Show workspace detail",
        steps=[
            HintStep(
                comment="Get workspace detail",
                client=ClientCall(
                    method="get_workspace",
                    args={"workspace_id": "{workspace_id}"},
                    result_var="workspace",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="WorkspaceService",
                    service_module="workspace_service",
                    method="get_workspace",
                    args={"alias": "{project}", "workspace_id": "{workspace_id}"},
                ),
            ),
        ],
    )
)

# ── workspace delete ───────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="workspace.delete",
        description="Delete a workspace and its config",
        steps=[
            HintStep(
                comment="Delete workspace",
                client=ClientCall(
                    method="delete_workspace",
                    args={"workspace_id": "{workspace_id}"},
                    result_var="result",
                ),
                service=ServiceCall(
                    service_class="WorkspaceService",
                    service_module="workspace_service",
                    method="delete_workspace",
                    args={"alias": "{project}", "workspace_id": "{workspace_id}"},
                ),
            ),
        ],
        notes=["Service layer also cleans up the associated sandbox configuration."],
    )
)

# ── workspace password ─────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="workspace.password",
        description="Reset workspace password",
        steps=[
            HintStep(
                comment="Reset workspace password",
                client=ClientCall(
                    method="reset_workspace_password",
                    args={"workspace_id": "{workspace_id}"},
                    result_var="credentials",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="WorkspaceService",
                    service_module="workspace_service",
                    method="reset_password",
                    args={"alias": "{project}", "workspace_id": "{workspace_id}"},
                ),
            ),
        ],
    )
)

# ── workspace load ─────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="workspace.load",
        description="Load tables into a workspace",
        steps=[
            HintStep(
                comment="Load tables into workspace",
                client=ClientCall(
                    method="load_workspace_tables",
                    args={"workspace_id": "{workspace_id}", "tables": "{tables}"},
                    result_var="result",
                ),
                service=ServiceCall(
                    service_class="WorkspaceService",
                    service_module="workspace_service",
                    method="load_tables",
                    args={
                        "alias": "{project}",
                        "workspace_id": "{workspace_id}",
                        "tables": "{tables}",
                        "preserve": "{preserve}",
                    },
                ),
            ),
        ],
        notes=["Tables format: 'bucket.table' or 'bucket.table/dest_name'."],
    )
)

# ── workspace query ────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="workspace.query",
        description="Execute SQL query in a workspace",
        steps=[
            HintStep(
                comment="Submit SQL query",
                client=ClientCall(
                    method="submit_query",
                    args={"workspace_id": "{workspace_id}", "sql": "{sql}"},
                    result_var="query_job",
                ),
                service=ServiceCall(
                    service_class="WorkspaceService",
                    service_module="workspace_service",
                    method="execute_query",
                    args={
                        "alias": "{project}",
                        "workspace_id": "{workspace_id}",
                        "sql": "{sql}",
                        "transactional": "{transactional}",
                    },
                ),
            ),
            HintStep(
                comment="Poll until query completes",
                client=ClientCall(
                    method="wait_for_query_job",
                    args={"query_job_id": 'query_job["id"]'},
                    result_var="query_job",
                ),
                kind="poll_loop",
                poll_interval=1.0,
                poll_condition='query_job.get("status") not in ("finished", "error")',
            ),
        ],
        notes=[
            "Uses the Query Service API (query.keboola.com).",
            "Service layer handles poll + result export in one call.",
        ],
    )
)

# ── workspace from-transformation ──────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="workspace.from-transformation",
        description="Create a workspace pre-loaded with transformation inputs",
        steps=[
            HintStep(
                comment="Create workspace from transformation config",
                client=ClientCall(
                    method="get_config_detail",
                    args={"component_id": "{component_id}", "config_id": "{config_id}"},
                    result_var="config",
                ),
                service=ServiceCall(
                    service_class="WorkspaceService",
                    service_module="workspace_service",
                    method="create_from_transformation",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                        "config_id": "{config_id}",
                        "row_id": "{row_id}",
                        "backend": "{backend}",
                    },
                ),
            ),
        ],
        notes=[
            "Service layer reads transformation input mapping, creates workspace, "
            "and loads input tables automatically.",
        ],
    )
)
