"""Changelog command -- show recent version history.

Thin CLI layer: reads changelog data and formats output.
"""

from __future__ import annotations

import re
from functools import partial

import typer
from rich.console import Console
from rich.text import Text

from ..changelog import DEFAULT_CHANGELOG_LIMIT, get_changelog, headline
from ._helpers import get_formatter

# Map each known prefix word to a Rich style.  Order does not matter; the
# regex below recognises the prefix regardless of trailing decorations like
# ``(#274)`` or `` (sec-20 follow-up)``.
_PREFIX_STYLES: dict[str, str] = {
    "breaking": "bold red",
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
    r"^(Plugin docs|Review fixes|Observability|Breaking|Security|Closed|Tests|"
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


def _print_bullet(console: Console, styled: Text, body_width: int) -> None:
    """Word-wrap a styled bullet and print it with a manual gutter.

    Bullet glyph on the first line, two-space indent on continuations.  This
    preserves per-span styling without the right-side padding Table cells emit.
    """
    for j, line in enumerate(styled.wrap(console, body_width)):
        # rstrip in place -- Rich keeps the word-break space at the end of each
        # wrapped row, which shows up as trailing whitespace on copy/paste.
        line.rstrip()
        # The gutter is a styleless Text so the body keeps its own spans; dim
        # applies only to the bullet glyph, not to everything that follows.
        row = Text()
        row.append("  • " if j == 0 else "    ", style="dim" if j == 0 else None)
        row.append_text(line)
        console.print(row)


def _format_changelog_human(console: Console, data: dict, *, full: bool) -> None:
    """Render the changelog.

    Default (``full=False``): one headline bullet per version -- the first
    note's first sentence, plus a dim ``(+N more)`` when a version carries
    extra notes.  ``full=True``: every note, word-wrapped in full.
    """
    # Body width = terminal width minus the 4-char bullet gutter.  Floor at
    # 40 to stay readable on pathologically narrow terminals and to handle
    # Console.width == 0 when stdout is piped to /dev/null.
    body_width = max(40, console.width - 4)
    entries = list(data["entries"].items())
    has_hidden_detail = False
    for i, (version, notes) in enumerate(entries):
        console.print(f"v{version}", style="bold cyan")
        if full:
            for note in notes:
                _print_bullet(console, _styled_note(note), body_width)
        else:
            head = headline(notes[0])
            styled = _styled_note(head)
            extra = len(notes) - 1
            if extra > 0:
                styled.append(f"  (+{extra} more)", style="dim")
            if extra > 0 or head != notes[0].strip():
                has_hidden_detail = True
            _print_bullet(console, styled, body_width)
        if i < len(entries) - 1:
            console.print("")
    if not full and has_hidden_detail:
        console.print("")
        console.print("Run with --full (-v) to see complete notes.", style="dim")


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
    full: bool = typer.Option(
        False,
        "--full",
        "-v",
        help="Show complete notes for each version (default: one-line summary).",
    ),
) -> None:
    """Show recent changelog (what changed in each version).

    By default each version is summarised as a single headline; pass --full
    (-v) for the complete notes.

    After auto-update, kbagent automatically prints "What's new" for the
    new version.  To see changes for a specific version manually, set
    KBAGENT_UPDATED_FROM to any older version:

        KBAGENT_UPDATED_FROM=0.17.0 kbagent version
    """
    formatter = get_formatter(ctx)
    entries = get_changelog(limit)
    # Bind ``full`` via partial so the JSON payload stays ``{"entries": ...}``
    # (the flag is a presentation concern, not data).
    formatter.output({"entries": entries}, partial(_format_changelog_human, full=full))
