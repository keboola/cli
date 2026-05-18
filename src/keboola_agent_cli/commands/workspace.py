"""Workspace commands - create, list, detail, delete, load, query, and from-transformation.

Thin CLI layer: parses arguments, calls WorkspaceService, formats output.
No business logic belongs here.
"""

from pathlib import Path

import typer
from rich.markup import escape

from ..config_store import ConfigStore
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..output import format_query_results, format_workspaces_table
from ._helpers import (
    check_cli_permission,
    emit_hint,
    emit_project_warnings,
    get_formatter,
    get_service,
    map_error_to_exit_code,
    resolve_branch,
    should_hint,
)

workspace_app = typer.Typer(help="Workspace lifecycle for SQL debugging")


@workspace_app.callback(invoke_without_command=True)
def _workspace_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "workspace")


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
    backend: str | None = typer.Option(
        None,
        "--backend",
        help="Workspace backend (auto-detected from project if omitted)",
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
    if should_hint(ctx):
        emit_hint(
            ctx,
            "workspace.create",
            project=project,
            name=name,
            backend=backend,
            read_only=read_only,
        )
        return
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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None


@workspace_app.command("list")
def workspace_list(
    ctx: typer.Context,
    project: list[str] | None = typer.Option(
        None,
        "--project",
        help="Project alias to query (can be repeated for multiple projects)",
    ),
    orphaned: bool = typer.Option(
        False,
        "--orphaned",
        help="Show only orphaned workspaces (keboola.sandboxes config missing)",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID. Read-only command -- ignores the alias's active branch "
        "by default (mirrors `storage buckets`); pass --branch to opt in. "
        "Requires exactly one --project.",
    ),
    qs_compatible: bool = typer.Option(
        False,
        "--qs-compatible",
        help="Show only workspaces whose loginType is known to work with the Query "
        "Service AND that are read-only -- the canonical shape for a data-app.",
    ),
) -> None:
    """List workspaces from connected projects.

    Branch handling: this is a read command and follows the same pattern as
    `storage buckets` / `config list` -- when an alias is pinned to a dev
    branch via `branch use`, the production endpoint is used (with a visible
    `Info: ...` banner) instead of silently scoping the listing to the
    pinned branch. Pass `--branch ID` to query a specific dev branch.

    Each workspace entry exposes `login_type`, `read_only` and
    `qs_compatible` so data-app developers can pick a Query-Service-compatible
    workspace without firing a probe query (closes #304).
    """
    if should_hint(ctx):
        emit_hint(ctx, "workspace.list", project=project, branch=branch)
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "workspace_service")
    config_store: ConfigStore = ctx.obj["config_store"]

    if branch is not None and (not project or len(project) != 1):
        formatter.error(
            message="--branch requires exactly one --project (branch ID is per-project)",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    effective_branch: int | None = branch
    if branch is None and project and len(project) == 1:
        _, effective_branch = resolve_branch(
            config_store, formatter, project[0], None, ignore_active_branch=True
        )

    try:
        result = service.list_workspaces(
            aliases=project,
            orphaned_only=orphaned,
            branch_id=effective_branch,
            qs_compatible_only=qs_compatible,
        )
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=exit_code) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID. Read-only command -- ignores the alias's active branch "
        "by default (mirrors `storage bucket-detail`); pass --branch to opt in.",
    ),
) -> None:
    """Show workspace details (password NOT included).

    Includes `login_type`, `read_only` and `qs_compatible` so callers can
    verify a workspace is Query-Service-compatible before issuing a query
    (closes #304).
    """
    if should_hint(ctx):
        emit_hint(
            ctx, "workspace.detail", project=project, workspace_id=workspace_id, branch=branch
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "workspace_service")
    config_store: ConfigStore = ctx.obj["config_store"]

    _, effective_branch = resolve_branch(
        config_store, formatter, project, branch, ignore_active_branch=True
    )

    try:
        result = service.get_workspace(
            alias=project, workspace_id=workspace_id, branch_id=effective_branch
        )
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
                c.print(
                    f"[bold]Login type:[/bold] {d.get('login_type', '') or '[dim](unknown)[/dim]'}"
                ),
                c.print(f"[bold]Read-only:[/bold] {'yes' if d.get('read_only') else 'no'}"),
                c.print(
                    f"[bold]Query Service compatible:[/bold] "
                    f"{'[green]yes[/green]' if d.get('qs_compatible') else '[yellow]no (loginType not on the confirmed whitelist; query may still work, try `kbagent workspace query`)[/yellow]'}"
                ),
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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
    if should_hint(ctx):
        emit_hint(ctx, "workspace.delete", project=project, workspace_id=workspace_id)
        return
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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
    if should_hint(ctx):
        emit_hint(ctx, "workspace.password", project=project, workspace_id=workspace_id)
        return
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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
    if should_hint(ctx):
        emit_hint(
            ctx,
            "workspace.load",
            project=project,
            workspace_id=workspace_id,
            tables=tables,
            preserve=preserve,
        )
        return
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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
    if should_hint(ctx):
        emit_hint(
            ctx,
            "workspace.query",
            project=project,
            workspace_id=workspace_id,
            sql=sql,
            transactional=transactional,
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "workspace_service")

    # Validate: exactly one of --sql or --file
    if sql and file:
        formatter.error(
            message="Specify either --sql or --file, not both.",
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2)
    if not sql and not file:
        formatter.error(
            message="Specify either --sql or --file.",
            error_code=ErrorCode.USAGE_ERROR,
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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None


@workspace_app.command("gc")
def workspace_gc(
    ctx: typer.Context,
    project: list[str] | None = typer.Option(
        None,
        "--project",
        help="Project alias to query (can be repeated). None = all projects.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="List orphaned workspaces without deleting them",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt",
    ),
) -> None:
    """Garbage-collect orphaned workspaces.

    An orphaned workspace is one backed by keboola.sandboxes whose
    sandbox config no longer exists. Running gc deletes those workspaces
    (and any lingering sandbox configs). Use --dry-run to preview first.
    """
    if should_hint(ctx):
        emit_hint(ctx, "workspace.gc", project=project, dry_run=dry_run)
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "workspace_service")

    if (
        not dry_run
        and not yes
        and not formatter.json_mode
        and not typer.confirm("Delete all orphaned workspaces in the selected project(s)?")
    ):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)

    try:
        result = service.gc_workspaces(aliases=project, dry_run=dry_run)
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=exit_code) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        formatter.console.print(result.get("message", ""))
        if dry_run:
            would_delete = result.get("would_delete", [])
            for ws in would_delete:
                formatter.console.print(
                    f"  [dim]would delete[/dim] workspace {ws['id']} "
                    f"([cyan]{escape(ws.get('name', ''))}[/cyan]) in '{escape(ws['project_alias'])}'"
                )
        else:
            for ws in result.get("deleted", []):
                formatter.console.print(
                    f"  [green]deleted[/green] workspace {ws['id']} in '{escape(ws['project_alias'])}'"
                )
            for err in result.get("errors", []):
                formatter.console.print(
                    f"  [red]error[/red] workspace {err.get('workspace_id', '?')}: {escape(err.get('error', ''))}"
                )


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
    backend: str | None = typer.Option(
        None,
        "--backend",
        help="Workspace backend (auto-detected from project if omitted)",
    ),
) -> None:
    """Create a workspace from a transformation config.

    Reads the transformation, creates a config-tied workspace, and loads
    all input tables. Returns credentials ready for SQL debugging.
    """
    if should_hint(ctx):
        emit_hint(
            ctx,
            "workspace.from-transformation",
            project=project,
            component_id=component_id,
            config_id=config_id,
            row_id=row_id,
            backend=backend,
        )
        return
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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
