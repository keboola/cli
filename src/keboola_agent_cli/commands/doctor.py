"""Doctor command - health check for CLI configuration and connectivity."""

import typer

from ..output import OutputFormatter


def _get_formatter(ctx: typer.Context) -> OutputFormatter:
    """Retrieve the OutputFormatter from the Typer context."""
    return ctx.obj["formatter"]


def doctor_command(ctx: typer.Context) -> None:
    """Run health checks on CLI configuration and project connectivity."""
    formatter = _get_formatter(ctx)
    formatter.output("Not yet implemented", lambda c, d: c.print(d))
