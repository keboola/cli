"""Human output for what ``sync push`` holds back (issue #792 G, H).

Split out of ``commands/sync.py``, which is at its size ceiling. Push lists
what it did not apply in every result: remote-side changes that need a
``sync pull`` (``skipped``) and the deletions it held back because
``--force`` was not given (``skipped_deletions``). The same notice prints the
result's ``warnings[]`` (e.g. the SQL editor sessions a workspace delete
removes, CLI-25).
"""

from __future__ import annotations

from typing import Any

from rich.markup import escape

# Remote-side change types in the diff, labelled for human output.
REMOTE_CHANGE_LABELS = {
    "remote_modified": "~ REMOTE MODIFIED",
    "remote_deleted": "- REMOTE DELETED",
}


def print_push_skips(formatter: Any, result: dict[str, Any]) -> None:
    """Print the changes a push result did not apply, with the next step, and its warnings.

    A notice, not a result, so it goes to stderr like every other hint. A
    warning message quotes config names and paths, which can hold ``[...]``;
    it is escaped so Rich prints it as text instead of reading it as markup.
    """
    for warn in result.get("warnings", []):
        formatter.warning(f"  {escape(str(warn.get('message', '')))}")
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


def emit_companion_warnings(formatter: Any, result: dict[str, Any]) -> None:
    """Move a sync result's ``companion_warnings`` to the command's warnings.

    Each becomes a ``warnings[]`` entry of the envelope with its ``code``
    (``UNOWNED_COMPANION``, ``COMPANION_KEPT``) in ``--json``, and a warning
    line on stderr in human mode -- also when diff finds nothing to change
    or push has nothing to push, which is exactly when a file sync ignores
    or kept would otherwise go unnoticed.

    An ``--all-projects`` result (``projects``: alias -> result) emits every
    project's entries, each naming its project -- a ``project`` field, and the
    alias in front of the human message (S4).
    """
    _emit(formatter, result, "")
    for alias, project_result in (result.get("projects") or {}).items():
        if isinstance(project_result, dict):
            _emit(formatter, project_result, str(alias))


def _emit(formatter: Any, result: dict[str, Any], project: str) -> None:
    for entry in result.pop("companion_warnings", None) or []:
        extra = {k: v for k, v in entry.items() if k not in ("code", "message")}
        if project:
            extra["project"] = project
        message = f"{project}: {entry['message']}" if project else entry["message"]
        formatter.warning(escape(message), code=entry["code"], **extra)
