"""Lineage commands - analyze cross-project data flow via bucket sharing.

Thin CLI layer: parses arguments, calls LineageService, formats output.
No business logic belongs here.
"""

import typer

from ..errors import ConfigError
from ..output import format_lineage_table
from ._helpers import (
    check_cli_permission,
    emit_hint,
    emit_project_warnings,
    get_formatter,
    get_service,
    should_hint,
)

lineage_app = typer.Typer(help="Analyze cross-project data lineage via bucket sharing")


@lineage_app.callback(invoke_without_command=True)
def _lineage_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "lineage")


@lineage_app.command("show")
def lineage_show(
    ctx: typer.Context,
    project: list[str] | None = typer.Option(
        None,
        "--project",
        help="Project alias to query (can be repeated for multiple projects)",
    ),
) -> None:
    """Show cross-project data lineage via bucket sharing."""
    if should_hint(ctx):
        emit_hint(ctx, "lineage.show", project=project)
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "lineage_service")

    try:
        result = service.get_lineage(aliases=project)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        format_lineage_table(formatter.console, result)
        emit_project_warnings(formatter, result)


@lineage_app.callback(invoke_without_command=True)
def lineage_callback(ctx: typer.Context) -> None:
    """Default to 'show' when no subcommand is given."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(lineage_show, ctx=ctx, project=None)
