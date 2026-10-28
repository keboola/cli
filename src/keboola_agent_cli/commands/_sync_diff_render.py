"""Human output for the remote-only rows of ``sync diff`` and the rows a push delete covers.

Split out of ``commands/sync.py``, which is at its size ceiling (sync group 3).
``remote_only_rows[]`` lists rows added on the remote (in the UI) under a
config the tree tracks (D5); a ``deleted`` config change lists the rows its
DELETE removes with it as ``covered_rows`` (D3).
"""

from __future__ import annotations

from typing import Any

from rich.markup import escape


def has_remote_only(result: dict[str, Any]) -> bool:
    """True iff a diff result lists a remote-only config or row."""
    return bool(result.get("remote_only") or result.get("remote_only_rows"))


def print_remote_only_rows(formatter: Any, result: dict[str, Any]) -> None:
    """Print the remote-only rows of a diff result (``sync pull`` fetches them)."""
    rows = result.get("remote_only_rows") or []
    if not rows:
        return
    formatter.console.print(
        f"[bold]Remote-only rows ({len(rows)} new, run 'sync pull' to fetch):[/bold]"
    )
    for row in rows:
        label = escape(f"{row['component_id']}/{row['parent_config_id']}/{row['row_name']}")
        formatter.console.print(f"  [cyan]+ NEW ROW {label}[/cyan]")


def covered_suffix(change: dict[str, Any]) -> str:
    """`` (with N rows)`` for a config delete that removes its rows too, else ``""``."""
    covered = change.get("covered_rows") or []
    return f" (with {len(covered)} row{'s' if len(covered) != 1 else ''})" if covered else ""
