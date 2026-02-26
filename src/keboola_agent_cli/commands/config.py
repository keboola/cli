"""Configuration browsing commands - list and detail.

Thin CLI layer: parses arguments, calls ConfigService, formats output.
No business logic belongs here.
"""

import typer

from ..errors import ConfigError, KeboolaApiError
from ..output import OutputFormatter, format_config_detail, format_configs_table
from ..services.config_service import ConfigService

config_app = typer.Typer(help="Browse and inspect configurations")

VALID_COMPONENT_TYPES = ["extractor", "writer", "transformation", "application"]


def _get_formatter(ctx: typer.Context) -> OutputFormatter:
    """Retrieve the OutputFormatter from the Typer context."""
    return ctx.obj["formatter"]


def _get_service(ctx: typer.Context) -> ConfigService:
    """Retrieve the ConfigService from the Typer context."""
    return ctx.obj["config_service"]


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
    formatter = _get_formatter(ctx)
    service = _get_service(ctx)

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

        # Show error warnings on stderr too
        for err in result.get("errors", []):
            formatter.warning(f"Project '{err['project_alias']}': {err['message']}")


@config_app.command("detail")
def config_detail(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    component_id: str = typer.Option(..., "--component-id", help="Component ID"),
    config_id: str = typer.Option(..., "--config-id", help="Configuration ID"),
) -> None:
    """Show detailed information about a specific configuration."""
    formatter = _get_formatter(ctx)
    service = _get_service(ctx)

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
        if exc.error_code == "INVALID_TOKEN":
            exit_code = 3
        elif exc.error_code in ("TIMEOUT", "CONNECTION_ERROR", "RETRY_EXHAUSTED"):
            exit_code = 4
        else:
            exit_code = 1
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            project=project,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None
