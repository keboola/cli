"""Context command - provides usage instructions for AI agents."""

import typer

from ..output import OutputFormatter


def _get_formatter(ctx: typer.Context) -> OutputFormatter:
    """Retrieve the OutputFormatter from the Typer context."""
    return ctx.obj["formatter"]


def context_command(ctx: typer.Context) -> None:
    """Show usage instructions for AI agents interacting with Keboola."""
    formatter = _get_formatter(ctx)
    formatter.output("Not yet implemented", lambda c, d: c.print(d))
