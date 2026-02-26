"""Project management commands - add, list, remove, edit, status."""

from typing import Optional

import typer

from ..output import OutputFormatter

project_app = typer.Typer(help="Manage connected Keboola projects")


def _get_formatter(ctx: typer.Context) -> OutputFormatter:
    """Retrieve the OutputFormatter from the Typer context."""
    return ctx.obj["formatter"]


@project_app.command("add")
def project_add(
    ctx: typer.Context,
    alias: str = typer.Option(..., help="Human-friendly name for this project"),
    url: str = typer.Option(
        "https://connection.keboola.com",
        help="Keboola stack URL",
    ),
    token: str = typer.Option(..., help="Storage API token"),
) -> None:
    """Add a new Keboola project connection."""
    formatter = _get_formatter(ctx)
    formatter.output("Not yet implemented", lambda c, d: c.print(d))


@project_app.command("list")
def project_list(ctx: typer.Context) -> None:
    """List all connected Keboola projects."""
    formatter = _get_formatter(ctx)
    formatter.output([], lambda c, d: c.print("Not yet implemented"))


@project_app.command("remove")
def project_remove(
    ctx: typer.Context,
    alias: str = typer.Option(..., help="Alias of the project to remove"),
) -> None:
    """Remove a Keboola project connection."""
    formatter = _get_formatter(ctx)
    formatter.output("Not yet implemented", lambda c, d: c.print(d))


@project_app.command("edit")
def project_edit(
    ctx: typer.Context,
    alias: str = typer.Option(..., help="Alias of the project to edit"),
    url: Optional[str] = typer.Option(None, help="New Keboola stack URL"),
    token: Optional[str] = typer.Option(None, help="New Storage API token"),
) -> None:
    """Edit an existing Keboola project connection."""
    formatter = _get_formatter(ctx)
    formatter.output("Not yet implemented", lambda c, d: c.print(d))


@project_app.command("status")
def project_status(
    ctx: typer.Context,
    project: Optional[str] = typer.Option(None, "--project", help="Check only this project (default: all)"),
) -> None:
    """Test connectivity to connected Keboola projects."""
    formatter = _get_formatter(ctx)
    formatter.output("Not yet implemented", lambda c, d: c.print(d))
