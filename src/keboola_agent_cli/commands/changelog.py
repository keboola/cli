"""Changelog command -- show recent version history.

Thin CLI layer: reads changelog data and formats output.
"""

from __future__ import annotations

import re

import typer
from rich.console import Console
from rich.text import Text

from ..changelog import DEFAULT_CHANGELOG_LIMIT, get_changelog
from ._helpers import get_formatter

# Map each known prefix word to a Rich style.  Order does not matter; the
# regex below recognises the prefix regardless of trailing decorations like
# ``(#274)`` or `` (sec-20 follow-up)``.
_PREFIX_STYLES: dict[str, str] = {
    "new": "bold green",
    "fix": "bold yellow",
    "change": "bold blue",
    "ux": "bold magenta",
    "note": "bold cyan",
    "security": "bold red",
    "closed": "bold blue",
    "tests": "dim",
    "plugin docs": "dim",
    "internal": "dim",
    "observability": "dim",
    "e2e": "dim",
    "review fixes": "dim",
    "why": "dim",
}

# Match a leading "Word:" or "Word (extra):" prefix on a changelog note.  The
# alternation is anchored to the longest match first so "Plugin docs" wins
# over "Plugin".  Case-insensitive; we look up the style by lowercase key.
_PREFIX_RE = re.compile(
    r"^(Plugin docs|Review fixes|Observability|Security|Closed|Tests|"
    r"Internal|Change|Note|Fix|New|UX|E2E|Why)"
    r"(\s*\([^)]*\))?"  # optional "(#274)" or "(sec-20 follow-up)" decoration
    r":\s+",
    re.IGNORECASE,
)

# Split text by inline backtick spans while preserving the delimiters so we
# can colour them.  ``"foo `bar` baz"`` -> ``["foo ", "`bar`", " baz"]``.
_BACKTICK_SPLIT = re.compile(r"(`[^`\n]+`)")


def _styled_note(note: str) -> Text:
    """Build a Rich ``Text`` for one changelog bullet.

    Prefix words (``New:``, ``Fix:``, ...) are coloured according to
    ``_PREFIX_STYLES``; inline ``backtick`` spans are rendered in cyan.
    """
    text = Text()
    m = _PREFIX_RE.match(note)
    if m:
        base = m.group(1).lower()
        style = _PREFIX_STYLES.get(base, "bold white")
        # Render the full prefix (including any "(#274)" decoration and the
        # trailing colon-space) in the prefix style so the eye latches onto
        # the action verb instantly.
        text.append(m.group(0).rstrip() + " ", style=style)
        rest = note[m.end() :]
    else:
        rest = note

    for part in _BACKTICK_SPLIT.split(rest):
        if not part:
            continue
        if part.startswith("`") and part.endswith("`") and len(part) >= 2:
            text.append(part[1:-1], style="cyan")
        else:
            text.append(part)
    return text


def _format_changelog_human(console: Console, data: dict) -> None:
    """Render the changelog as a styled, word-wrapped bullet list."""
    # Body width = terminal width minus the 4-char bullet gutter.  Floor at
    # 40 to stay readable on pathologically narrow terminals and to handle
    # Console.width == 0 when stdout is piped to /dev/null.
    body_width = max(40, console.width - 4)
    entries = list(data["entries"].items())
    for i, (version, notes) in enumerate(entries):
        console.print(f"v{version}", style="bold cyan")
        for note in notes:
            styled = _styled_note(note)
            # Word-wrap the styled Text into a list of Text lines, then
            # render each with a manual gutter: bullet on line 0, two-space
            # indent on continuations.  This preserves spans without the
            # right-side padding that Table cells emit.
            lines = styled.wrap(console, body_width)
            for j, line in enumerate(lines):
                # rstrip the wrapped line in place -- Rich preserves the
                # word-break space at the end of each wrapped row, which
                # shows up as trailing whitespace on copy/paste.
                line.rstrip()
                # Build the gutter as a styleless Text so the body line
                # keeps its own per-span styling.  Dim is applied only to
                # the bullet glyph itself, not to everything that follows.
                row = Text()
                if j == 0:
                    row.append("  • ", style="dim")
                else:
                    row.append("    ")
                row.append_text(line)
                console.print(row)
        if i < len(entries) - 1:
            console.print("")


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
