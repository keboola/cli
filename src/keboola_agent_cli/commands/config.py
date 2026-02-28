"""Configuration browsing commands - list and detail.

Thin CLI layer: parses arguments, calls ConfigService, formats output.
No business logic belongs here.
"""

import typer

from ..errors import ConfigError, KeboolaApiError
from ..output import format_config_detail, format_configs_table
from ._helpers import emit_project_warnings, get_formatter, get_service, map_error_to_exit_code

config_app = typer.Typer(help="Browse and inspect configurations")

VALID_COMPONENT_TYPES = ["extractor", "writer", "transformation", "application"]


@config_app.command("list")
def config_list(
    ctx: typer.Context,
    project: list[str] | None = typer.Option(
        None,
        "--project",
        help="Project alias to query (can be repeated for multiple projects)",
    ),
    component_type: str | None = typer.Option(
        None,
        "--component-type",
        help="Filter by component type: extractor, writer, transformation, application",
    ),
    component_id: str | None = typer.Option(
        None,
        "--component-id",
        help="Filter by specific component ID (e.g. keboola.ex-db-snowflake)",
    ),
) -> None:
    """List configurations from connected projects."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "config_service")

    # Validate component_type if provided
    if component_type and component_type not in VALID_COMPONENT_TYPES:
        formatter.error(
            message=f"Invalid component type '{component_type}'. "
            f"Valid types: {', '.join(VALID_COMPONENT_TYPES)}",
            error_code="INVALID_ARGUMENT",
        )
        raise typer.Exit(code=2)

    try:
        result = service.list_configs(
            aliases=project,
            component_type=component_type,
            component_id=component_id,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None

    # In JSON mode, include both configs and errors in the response
    if formatter.json_mode:
        formatter.output(result)
    else:
        # In human mode, show per-project errors as warnings and configs as table
        format_configs_table(formatter.console, result)
        emit_project_warnings(formatter, result)


@config_app.command("detail")
def config_detail(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    component_id: str = typer.Option(..., "--component-id", help="Component ID"),
    config_id: str = typer.Option(..., "--config-id", help="Configuration ID"),
) -> None:
    """Show detailed information about a specific configuration."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "config_service")

    try:
        result = service.get_config_detail(
            alias=project,
            component_id=component_id,
            config_id=config_id,
        )
        formatter.output(result, format_config_detail)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
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
