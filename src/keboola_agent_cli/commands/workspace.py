"""Workspace commands - create, list, detail, delete, load, query, and from-transformation.

Thin CLI layer: parses arguments, calls WorkspaceService, formats output.
No business logic belongs here.
"""

from pathlib import Path

import typer

from ..errors import ConfigError, KeboolaApiError
from ..output import format_query_results, format_workspaces_table
from ._helpers import emit_project_warnings, get_formatter, get_service, map_error_to_exit_code

workspace_app = typer.Typer(help="Workspace lifecycle for SQL debugging")


@workspace_app.command("create")
def workspace_create(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias to create the workspace in",
    ),
    name: str = typer.Option(
        "",
        "--name",
        help="Name for the workspace (shown in Keboola UI)",
    ),
    backend: str = typer.Option(
        "snowflake",
        "--backend",
        help="Workspace backend (snowflake, bigquery, etc.)",
    ),
    read_only: bool = typer.Option(
        True,
        "--read-only/--no-read-only",
        help="Whether the workspace has read-only storage access",
    ),
    ui: bool = typer.Option(
        False,
        "--ui",
        help="Create via Queue job (slower ~15s, visible in Keboola UI)",
    ),
) -> None:
    """Create a new workspace.

    Default: fast headless mode via Storage API (~1s).
    With --ui: creates via Queue job (~15s), visible in Keboola UI Workspaces tab.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "workspace_service")

    try:
        result = service.create_workspace(
            alias=project, name=name, backend=backend, read_only=read_only, ui_mode=ui
        )
        formatter.output(
            result,
            lambda c, d: (
                c.print(f"[bold green]Success:[/bold green] {d['message']}"),
                c.print(f"\n[bold]Workspace ID:[/bold] {d['workspace_id']}"),
                c.print(f"[bold]Name:[/bold] {d.get('name', '')}"),
                c.print(f"[bold]Host:[/bold] {d['host']}"),
                c.print(f"[bold]Schema:[/bold] {d['schema']}"),
                c.print(f"[bold]User:[/bold] {d['user']}"),
                c.print(f"[bold yellow]Password:[/bold yellow] {d['password']}"),
                c.print(
                    "\n[bold yellow]Warning:[/bold yellow] Save the password now -- it cannot be retrieved later!"
                ),
            ),
        )
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None


@workspace_app.command("list")
def workspace_list(
    ctx: typer.Context,
    project: list[str] | None = typer.Option(
        None,
        "--project",
        help="Project alias to query (can be repeated for multiple projects)",
    ),
) -> None:
    """List workspaces from connected projects."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "workspace_service")

    try:
        result = service.list_workspaces(aliases=project)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        format_workspaces_table(formatter.console, result)
        emit_project_warnings(formatter, result)


@workspace_app.command("detail")
def workspace_detail(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    workspace_id: int = typer.Option(
        ...,
        "--workspace-id",
        help="Workspace ID",
    ),
) -> None:
    """Show workspace details (password NOT included)."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "workspace_service")

    try:
        result = service.get_workspace(alias=project, workspace_id=workspace_id)
        formatter.output(
            result,
            lambda c, d: (
                c.print(f"\n[bold]Workspace ID:[/bold] {d['workspace_id']}"),
                c.print(f"[bold]Project:[/bold] {d['project_alias']}"),
                c.print(f"[bold]Backend:[/bold] {d['backend']}"),
                c.print(f"[bold]Host:[/bold] {d['host']}"),
                c.print(f"[bold]Warehouse:[/bold] {d.get('warehouse', '')}"),
                c.print(f"[bold]Database:[/bold] {d.get('database', '')}"),
                c.print(f"[bold]Schema:[/bold] {d['schema']}"),
                c.print(f"[bold]User:[/bold] {d['user']}"),
                c.print(f"[bold]Created:[/bold] {d.get('created', '')}"),
            ),
        )
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None


@workspace_app.command("delete")
def workspace_delete(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    workspace_id: int = typer.Option(
        ...,
        "--workspace-id",
        help="Workspace ID to delete",
    ),
) -> None:
    """Delete a workspace."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "workspace_service")

    try:
        result = service.delete_workspace(alias=project, workspace_id=workspace_id)
        formatter.output(
            result,
            lambda c, d: c.print(f"[bold green]Success:[/bold green] {d['message']}"),
        )
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None


@workspace_app.command("password")
def workspace_password(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    workspace_id: int = typer.Option(
        ...,
        "--workspace-id",
        help="Workspace ID",
    ),
) -> None:
    """Reset workspace password and show the new one."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "workspace_service")

    try:
        result = service.reset_password(alias=project, workspace_id=workspace_id)
        formatter.output(
            result,
            lambda c, d: (
                c.print(f"[bold green]Success:[/bold green] {d['message']}"),
                c.print(f"\n[bold yellow]New Password:[/bold yellow] {d['password']}"),
            ),
        )
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None


@workspace_app.command("load")
def workspace_load(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    workspace_id: int = typer.Option(
        ...,
        "--workspace-id",
        help="Workspace ID",
    ),
    tables: list[str] = typer.Option(
        ...,
        "--tables",
        help="Table ID to load (can be repeated, e.g. in.c-bucket.table-name)",
    ),
    preserve: bool = typer.Option(
        False,
        "--preserve",
        help="Keep existing tables in the workspace (default: clear before loading)",
    ),
) -> None:
    """Load tables into a workspace.

    Waits for the async load job to complete.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "workspace_service")

    try:
        result = service.load_tables(
            alias=project, workspace_id=workspace_id, tables=tables, preserve=preserve
        )
        formatter.output(
            result,
            lambda c, d: c.print(f"[bold green]Success:[/bold green] {d['message']}"),
        )
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None


@workspace_app.command("query")
def workspace_query(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    workspace_id: int = typer.Option(
        ...,
        "--workspace-id",
        help="Workspace ID",
    ),
    sql: str | None = typer.Option(
        None,
        "--sql",
        help="SQL statement to execute",
    ),
    file: Path | None = typer.Option(
        None,
        "--file",
        help="Path to a .sql file to execute",
        exists=True,
        readable=True,
    ),
    transactional: bool = typer.Option(
        False,
        "--transactional",
        help="Wrap query in a transaction",
    ),
) -> None:
    """Execute SQL query in a workspace via Query Service.

    Provide SQL via --sql or --file (exactly one required).
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "workspace_service")

    # Validate: exactly one of --sql or --file
    if sql and file:
        formatter.error(
            message="Specify either --sql or --file, not both.",
            error_code="USAGE_ERROR",
        )
        raise typer.Exit(code=2)
    if not sql and not file:
        formatter.error(
            message="Specify either --sql or --file.",
            error_code="USAGE_ERROR",
        )
        raise typer.Exit(code=2)

    # Read SQL from file if needed
    effective_sql = sql if sql else file.read_text(encoding="utf-8")

    try:
        result = service.execute_query(
            alias=project,
            workspace_id=workspace_id,
            sql=effective_sql,
            transactional=transactional,
        )
        if formatter.json_mode:
            formatter.output(result)
        else:
            format_query_results(formatter.console, result)
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None


@workspace_app.command("from-transformation")
def workspace_from_transformation(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    component_id: str = typer.Option(
        ...,
        "--component-id",
        help="Transformation component ID (e.g. keboola.snowflake-transformation)",
    ),
    config_id: str = typer.Option(
        ...,
        "--config-id",
        help="Configuration ID",
    ),
    row_id: str | None = typer.Option(
        None,
        "--row-id",
        help="Optional row ID for row-based transformations",
    ),
    backend: str = typer.Option(
        "snowflake",
        "--backend",
        help="Workspace backend",
    ),
) -> None:
    """Create a workspace from a transformation config.

    Reads the transformation, creates a config-tied workspace, and loads
    all input tables. Returns credentials ready for SQL debugging.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "workspace_service")

    try:
        result = service.create_from_transformation(
            alias=project,
            component_id=component_id,
            config_id=config_id,
            row_id=row_id,
            backend=backend,
        )
        formatter.output(
            result,
            lambda c, d: (
                c.print(f"[bold green]Success:[/bold green] {d['message']}"),
                c.print(f"\n[bold]Workspace ID:[/bold] {d['workspace_id']}"),
                c.print(f"[bold]Host:[/bold] {d['host']}"),
                c.print(f"[bold]Schema:[/bold] {d['schema']}"),
                c.print(f"[bold]User:[/bold] {d['user']}"),
                c.print(f"[bold yellow]Password:[/bold yellow] {d['password']}"),
                c.print(f"[bold]Tables loaded:[/bold] {', '.join(d.get('tables_loaded', []))}"),
                c.print(
                    "\n[bold yellow]Warning:[/bold yellow] Save the password now -- it cannot be retrieved later!"
                ),
            ),
        )
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
