"""CLI commands for component discovery and inspection.

Thin CLI layer: parses arguments, calls ComponentService, formats output.
No business logic belongs here.
"""

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..constants import VALID_COMPONENT_TYPES
from ..errors import ConfigError, KeboolaApiError
from ._helpers import (
    check_cli_permission,
    emit_project_warnings,
    get_formatter,
    get_service,
    map_error_to_exit_code,
)

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
            error_code="INVALID_ARGUMENT",
        )
        raise typer.Exit(code=2)

    try:
        result = service.list_components(
            aliases=project,
            component_type=component_type,
            query=query,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
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
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
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
