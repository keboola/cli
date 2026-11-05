"""Human output for what ``sync push`` holds back (issue #792 G, H).

Split out of ``commands/sync.py``, which is at its size ceiling. Push lists
what it did not apply in every result: remote-side changes that need a
``sync pull`` (``skipped``) and the deletions it held back because
``--force`` was not given (``skipped_deletions``). The same notice prints the
result's ``warnings[]`` (e.g. the SQL editor sessions a workspace delete
removes, CLI-25).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.markup import escape

from ..services._sync_local_files import INVALID_CHANGE_TYPE

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
    for refused in result.get("refused_deletions", []):
        formatter.err_console.print(f"  [red]![/red] {escape(str(refused.get('message', '')))}")
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


def print_dry_run_refusals(formatter: Any, result: dict[str, Any]) -> None:
    """Print each change a dry run found the real run would refuse, with its remedy.

    The record's message names the file and what to do (``COMPANION_UNREADABLE``).
    """
    for err in result.get("errors", []):
        label = f"{err.get('change_type')} {err.get('component_id')}/{err.get('config_id') or ''}"
        formatter.item_error(label.rstrip("/"), err.get("message") or "")


def push_total(result: dict[str, Any]) -> int:
    """Every config change that a single-project ``sync push`` tried or would try."""
    if result.get("status") == "dry_run":
        summary = result.get("summary", {})
        planned = sum(summary.get(key, 0) for key in ("added", "modified", "deleted"))
        return planned + invalid_file_count(result)
    created = result.get("created", 0) + result.get("updated", 0) + result.get("deleted", 0)
    return created + len(result.get("errors", []))


@dataclass(frozen=True)
class DryRunCounts:
    """What a push dry run planned, what the real run would refuse, and the rest.

    Every refusal is a config create (``COMPANION_UNREADABLE``), so the
    executable creates are the planned ones minus the refused ones.
    """

    added: int
    modified: int
    deleted: int
    refused: int

    @property
    def planned(self) -> int:
        return self.added + self.modified + self.deleted

    @property
    def creates(self) -> int:
        return max(self.added - self.refused, 0)

    @property
    def executable(self) -> int:
        return self.planned - self.refused


def dry_run_counts(result: dict[str, Any]) -> DryRunCounts:
    summary = result.get("summary", {})
    return DryRunCounts(
        added=summary.get("added", 0),
        modified=summary.get("modified", 0),
        deleted=summary.get("deleted", 0),
        refused=len(result.get("errors", [])) - invalid_file_count(result),
    )


def invalid_file_count(result: dict[str, Any]) -> int:
    """The ``errors[]`` entries of configs held by an unusable ``_config.yml``."""
    return sum(1 for e in result.get("errors", []) if e.get("change_type") == INVALID_CHANGE_TYPE)


def dry_run_line(result: dict[str, Any]) -> str:
    """The per-project dry-run line (``--all-projects``): executable counts, then refusals."""
    n = dry_run_counts(result)
    refused = f", [red]{n.refused} refused[/red]" if n.refused else ""
    return f"Would create {n.creates}, update {n.modified}, delete {n.deleted}{refused}"


def dry_run_one_liner(result: dict[str, Any]) -> str:
    n = dry_run_counts(result)
    refused = f", [red]{n.refused} refused[/red]" if n.refused else ""
    return f"would: +{n.creates} ~{n.modified} -{n.deleted}{refused}"


def push_summary(result: dict[str, Any]) -> str:
    """The headline text of a single-project ``sync push``.

    A dry run counts what it planned, what the real run would refuse, and
    what would actually be pushed, separately.
    """
    status = result.get("status", "")
    if status == "no_changes":
        return "0 config(s) pushed, no changes."
    if status == "dry_run":
        n = dry_run_counts(result)
        return (
            f"{n.planned} config(s) planned, {n.refused} refused, {n.executable} would be "
            f"pushed: {n.creates} created, {n.modified} updated, {n.deleted} deleted."
        )
    created, updated = result.get("created", 0), result.get("updated", 0)
    deleted = result.get("deleted", 0)
    return (
        f"{created + updated + deleted} config(s) pushed: {created} created, "
        f"{updated} updated, {deleted} deleted."
    )
