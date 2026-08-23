"""CLI commands for component discovery and inspection.

Thin CLI layer: parses arguments, calls ComponentService, formats output.
No business logic belongs here.
"""

import json

import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from ..config_store import ConfigStore
from ..constants import VALID_COMPONENT_TYPES
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..services.component_service import DOCUMENTATION_SOURCE_STORAGE_CATALOG
from ._helpers import (
    check_cli_permission,
    emit_project_warnings,
    get_formatter,
    get_service,
    map_error_to_exit_code,
    resolve_branch,
)
from .config import _parse_json_input

component_app = typer.Typer(help="Discover and inspect Keboola components")


@component_app.callback(invoke_without_command=True)
def _component_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "component")


def _format_components_table(console: Console, data: dict) -> None:
    """Render a Rich table of components.

    Args:
        console: Rich Console instance.
        data: Dict with "components" list and optionally "errors" list.
    """
    components = data.get("components", [])
    errors = data.get("errors", [])
    is_search = data.get("query") is not None

    for err in errors:
        console.print(
            f"[bold yellow]Warning:[/bold yellow] Project [bold]{err['project_alias']}[/bold]: "
            f"{err['message']}"
        )

    if not components:
        if not errors:
            console.print(
                "No components found. Use [bold]kbagent project add[/bold] to connect a project first."
            )
        else:
            console.print("No components retrieved (all projects failed).")
        return

    table = Table(title="Components")
    table.add_column("Component ID", style="bold cyan")
    table.add_column("Name")
    table.add_column("Type", style="dim")
    table.add_column("Categories")
    if is_search:
        table.add_column("Score", justify="right")

    for comp in components:
        categories = ", ".join(comp.get("categories", []))
        row = [
            comp.get("component_id", ""),
            comp.get("component_name", ""),
            comp.get("component_type", ""),
            categories,
        ]
        if is_search:
            row.append(str(comp.get("score", "")))
        table.add_row(*row)

    console.print(table)
    console.print()


def _format_component_detail(console: Console, data: dict) -> None:
    """Render detailed component information as a Rich Panel.

    Args:
        console: Rich Console instance.
        data: Component detail dict from the service.
    """
    name = data.get("component_name", "Unknown")
    component_id = data.get("component_id", "")
    component_type = data.get("component_type", "")
    description = data.get("description", "")
    long_description = data.get("long_description", "")
    categories = ", ".join(data.get("categories", []))
    documentation_url = data.get("documentation_url", "")

    lines = [
        f"[bold]Component ID:[/bold] {component_id}",
        f"[bold]Name:[/bold] {name}",
        f"[bold]Type:[/bold] {component_type}",
    ]
    if categories:
        lines.append(f"[bold]Categories:[/bold] {categories}")
    if description:
        lines.append(f"[bold]Description:[/bold] {description}")
    if long_description:
        lines.append(f"\n[bold]Long Description:[/bold]\n{long_description}")
    if documentation_url:
        lines.append(f"[bold]Documentation:[/bold] {documentation_url}")

    # Show schema summary if present
    schema_summary = data.get("schema_summary", {})
    if schema_summary:
        prop_count = schema_summary.get("property_count", 0)
        req_count = schema_summary.get("required_count", 0)
        has_rows = schema_summary.get("has_row_schema", False)
        schema_parts = []
        if prop_count:
            schema_parts.append(f"{prop_count} properties ({req_count} required)")
        if has_rows:
            schema_parts.append("row-based")
        if schema_parts:
            lines.append(f"[bold]Schema:[/bold] {', '.join(schema_parts)}")

    examples_count = data.get("examples_count", 0)
    if examples_count:
        lines.append(f"[bold]Examples:[/bold] {examples_count} root config example(s)")

    # Say so when the AI Service did not index this component: otherwise the
    # missing schema/examples read as "this component has none" rather than
    # "this view cannot show them".
    if data.get("documentation_source") == DOCUMENTATION_SOURCE_STORAGE_CATALOG:
        lines.append(
            "\n[yellow]Source:[/yellow] project Storage catalog -- the Keboola AI Service "
            "has no documentation indexed for this component, so its configuration schema "
            "and examples are unavailable."
        )

    panel = Panel("\n".join(lines), title=f"Component - {name}", expand=False)
    console.print(panel)


@component_app.command("list")
def component_list(
    ctx: typer.Context,
    project: list[str] | None = typer.Option(
        None,
        "--project",
        help="Project alias to query (can be repeated for multiple projects)",
    ),
    component_type: str | None = typer.Option(
        None,
        "--type",
        help="Filter by component type: extractor, writer, transformation, application",
    ),
    query: str | None = typer.Option(
        None,
        "--query",
        "-q",
        help="Search query to filter components by name or description",
    ),
) -> None:
    """List available components from connected projects."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "component_service")

    # Validate component_type if provided
    if component_type and component_type not in VALID_COMPONENT_TYPES:
        formatter.error(
            message=f"Invalid component type '{component_type}'. "
            f"Valid types: {', '.join(VALID_COMPONENT_TYPES)}",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    try:
        result = service.list_components(
            aliases=project,
            component_type=component_type,
            query=query,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        _format_components_table(formatter.console, result)
        emit_project_warnings(formatter, result)


@component_app.command("detail")
def component_detail(
    ctx: typer.Context,
    component_id: str = typer.Option(
        ...,
        "--component-id",
        help="Component ID (e.g. keboola.ex-db-snowflake)",
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Project alias (uses first available if not set)",
    ),
) -> None:
    """Show detailed information about a specific component."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "component_service")

    try:
        result = service.get_component_detail(
            alias=project,
            component_id=component_id,
        )
        formatter.output(result, _format_component_detail)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            project=project or "",
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None


def _format_sync_action_result(console: Console, data: dict) -> None:
    """Render the sync action result as a JSON syntax panel.

    The result shape is action-specific (opaque dict or list), so a
    pretty-printed JSON block is the most honest human rendering.
    """
    action = data.get("action", "")
    component_id = data.get("component_id", "")
    syntax = Syntax(
        json.dumps(data.get("result"), indent=2, ensure_ascii=False),
        "json",
        theme="monokai",
    )
    panel = Panel(syntax, title=f"Sync action '{action}' - {component_id}", expand=False)
    console.print(panel)


@component_app.command("sync-action")
def component_sync_action(
    ctx: typer.Context,
    action_name: str = typer.Argument(
        ...,
        help="Sync action name (component-defined, e.g. testConnection, getTables)",
    ),
    component_id: str = typer.Option(
        ...,
        "--component-id",
        help="Component ID (e.g. keboola.ex-db-mysql)",
    ),
    config_id: str | None = typer.Option(
        None,
        "--config-id",
        help="Configuration ID whose stored configData to send (required unless --config-data)",
    ),
    row_id: str | None = typer.Option(
        None,
        "--row-id",
        help="Configuration row ID to shallow-merge over the root configuration",
    ),
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Run in a specific dev branch ID (defaults to active branch)",
    ),
    config_data: str | None = typer.Option(
        None,
        "--config-data",
        help="Explicit configData JSON: inline, @file.json, or - for stdin (skips config fetch)",
    ),
    timeout: int | None = typer.Option(
        None,
        "--timeout",
        help="Request timeout in seconds for the action call (long actions e.g. getTables)",
    ),
) -> None:
    """Run a synchronous component action such as testConnection.

    \b
    Valid action names are component-defined -- the API validates them
    server-side. By default the stored configuration (--config-id) is sent
    as configData; with --row-id the row configuration is shallow-merged
    over the root at the top level (row keys replace root keys wholesale,
    matching the MCP run_sync_action tool). Use --config-data to send an
    explicit payload instead.

    \b
    Examples:
      # Test a database extractor's stored credentials
      kbagent component sync-action testConnection \\
        --component-id keboola.ex-db-mysql --config-id 123456 --project prod

      # Run against a specific row's configuration
      kbagent component sync-action getTables \\
        --component-id keboola.ex-db-mysql --config-id 123456 --row-id 654321 --project prod

      # Send an explicit configData payload
      kbagent component sync-action testConnection \\
        --component-id keboola.ex-db-mysql --project prod \\
        --config-data '{"parameters": {"db": {"host": "example.com"}}}'
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "component_service")
    config_store: ConfigStore = ctx.obj["config_store"]

    if config_id is None and config_data is None:
        formatter.error(
            message="Either --config-id or --config-data is required.",
            error_code=ErrorCode.MISSING_PARAMETER,
        )
        raise typer.Exit(code=2)

    if row_id is not None and config_id is None:
        formatter.error(
            message="--row-id requires --config-id (rows belong to a configuration).",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    override: dict | None = None
    if config_data is not None:
        try:
            override = _parse_json_input(config_data)
        except (json.JSONDecodeError, FileNotFoundError) as exc:
            formatter.error(
                message=f"Invalid --config-data input: {exc}",
                error_code=ErrorCode.VALIDATION_ERROR,
            )
            raise typer.Exit(code=2) from None

    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    try:
        result = service.run_sync_action(
            alias=project,
            component_id=component_id,
            action=action_name,
            config_id=config_id,
            row_id=row_id,
            branch_id=effective_branch,
            config_data_override=override,
            timeout=timeout,
        )
        formatter.output(result, _format_sync_action_result)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            project=project,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None
