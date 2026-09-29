"""Human output for what ``sync push`` holds back (issue #792 G, H).

Split out of ``commands/sync.py``, which is at its size ceiling. Push lists
what it did not apply in every result: remote-side changes that need a
``sync pull`` (``skipped``) and the deletions it held back because
``--force`` was not given (``skipped_deletions``).
"""

from __future__ import annotations

from typing import Any

# Remote-side change types in the diff, labelled for human output.
REMOTE_CHANGE_LABELS = {
    "remote_modified": "~ REMOTE MODIFIED",
    "remote_deleted": "- REMOTE DELETED",
}


def print_push_skips(formatter: Any, result: dict[str, Any]) -> None:
    """Print the changes a push result did not apply, with the next step.

    A notice, not a result, so it goes to stderr like every other hint.
    """
    skipped_reason = result.get("skipped_reason")
    if skipped_reason:
        formatter.err_console.print(f"  [yellow]{skipped_reason}[/yellow]")
    deletions = result.get("skipped_deletions", [])
    if not deletions:
        return
    formatter.err_console.print(
        f"  [yellow]{len(deletions)} deletion(s) not applied:[/yellow] "
        f"{result.get('skipped_deletions_reason', '')}"
    )
    for change in deletions:
        kind = "row" if change.get("is_row") else "config"
        formatter.err_console.print(
            f"    - {change.get('component_id')}/{change.get('config_id')} "
            f"[dim]({kind})[/dim]  {change.get('config_name', '')}"
        )


def push_skips_one_liner(result: dict[str, Any]) -> str:
    """Short suffix for the ``--all-projects`` push line; empty when nothing was held back."""
    parts = []
    if result.get("skipped"):
        parts.append(f"[cyan]{result['skipped']} to pull first[/cyan]")
    if result.get("skipped_deletions"):
        parts.append(
            f"[yellow]{len(result['skipped_deletions'])} deletion(s) need --force[/yellow]"
        )
    return "".join(f", {part}" for part in parts)
