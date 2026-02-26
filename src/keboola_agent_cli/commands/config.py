"""Configuration browsing commands - list and detail."""

from typing import Optional

import typer

from ..output import OutputFormatter

config_app = typer.Typer(help="Browse and inspect configurations")


def _get_formatter(ctx: typer.Context) -> OutputFormatter:
    """Retrieve the OutputFormatter from the Typer context."""
    return ctx.obj["formatter"]


@config_app.command("list")
def config_list(
    ctx: typer.Context,
    project: Optional[list[str]] = typer.Option(None, "--project", help="Project alias (can be repeated)"),
    component_type: Optional[str] = typer.Option(
        None,
        "--component-type",
        help="Filter by component type: extractor, writer, transformation, application",
    ),
    component_id: Optional[str] = typer.Option(None, "--component-id", help="Filter by specific component ID"),
) -> None:
    """List configurations from connected projects."""
    formatter = _get_formatter(ctx)
    formatter.output("Not yet implemented", lambda c, d: c.print(d))


@config_app.command("detail")
def config_detail(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    component_id: str = typer.Option(..., "--component-id", help="Component ID"),
    config_id: str = typer.Option(..., "--config-id", help="Configuration ID"),
) -> None:
    """Show detailed information about a specific configuration."""
    formatter = _get_formatter(ctx)
    formatter.output("Not yet implemented", lambda c, d: c.print(d))
