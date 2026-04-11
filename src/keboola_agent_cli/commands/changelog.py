"""Changelog command -- show recent version history.

Thin CLI layer: reads changelog data and formats output.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.text import Text

from ..changelog import DEFAULT_CHANGELOG_LIMIT, get_changelog
from ._helpers import get_formatter


def _format_changelog_human(console: Console, data: dict) -> None:
    """Render changelog as styled terminal output."""
    text = Text()
    entries = data["entries"]
    for i, (version, notes) in enumerate(entries.items()):
        text.append(f"v{version}\n", style="bold cyan")
        for note in notes:
            text.append(f"  - {note}\n")
        if i < len(entries) - 1:
            text.append("\n")
    console.print(text)


def changelog_command(
    ctx: typer.Context,
    limit: int = typer.Option(
        DEFAULT_CHANGELOG_LIMIT,
        "--limit",
        "-n",
        help="Number of versions to show.",
        min=1,
        max=100,
    ),
) -> None:
    """Show recent changelog (what changed in each version).

    After auto-update, kbagent automatically prints "What's new" for the
    new version.  To see changes for a specific version manually, set
    KBAGENT_UPDATED_FROM to any older version:

        KBAGENT_UPDATED_FROM=0.17.0 kbagent version
    """
    formatter = get_formatter(ctx)
    entries = get_changelog(limit)
    formatter.output({"entries": entries}, _format_changelog_human)
