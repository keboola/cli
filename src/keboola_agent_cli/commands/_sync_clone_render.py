"""Human output for ``sync clone``.

Split out of ``commands/sync.py``, which is at its size ceiling. The clone
result carries the push outcome, the bucket outcome, and ``warnings[]``: what
the target project still needs after the clone (CLI-24).
"""

from __future__ import annotations

from typing import Any

from rich.markup import escape


def print_clone_result(formatter: Any, result: dict[str, Any]) -> None:
    """Print a ``sync clone`` result, then its warnings on stderr.

    A warning message quotes config and task names, which can hold ``[...]``.
    It is escaped so Rich prints it as text instead of reading it as markup.
    """
    _format_clone_result(formatter, result)
    for warn in result.get("warnings", []):
        formatter.warning(f"  {escape(warn['message'])}")


def _print_clone_buckets(formatter: Any, result: dict[str, Any]) -> None:
    """Print the ``--create-buckets`` outcome (a no-op when it was not used)."""
    links = result.get("linked_buckets", [])
    if result.get("buckets_created") or result.get("buckets_skipped") or links:
        formatter.console.print(
            f"  Buckets: {result.get('buckets_created', 0)} created, {len(links)} linked, "
            f"{result.get('buckets_skipped', 0)} already present"
        )
    for link in links:
        formatter.console.print(
            f"  Linked {link.get('bucket_id')} -> project {link.get('source_project_id')} "
            f"bucket {link.get('source_bucket_id')}"
        )
    for berr in result.get("bucket_errors", []):
        formatter.item_error(f"bucket {berr.get('bucket_id')}", berr.get("message") or "")


def clone_summary(result: dict[str, Any]) -> str:
    """The headline text of ``sync clone`` (HO2); the command adds ``Success:`` or ``Failed:``."""
    status = result.get("status", "")
    target = escape(str(result.get("target_alias")))
    if status == "dry_run":
        added = result.get("summary", {}).get("added", 0)
        refused = len(result.get("errors", []))
        return (
            f"{added} config(s) planned, {refused} refused, {added - refused} would be "
            f"cloned into {target}."
        )
    if status == "no_changes":
        return f"0 config(s) cloned into {target}, already cloned."
    overrides = (
        f"buckets={result.get('bucket_rewrites', 0)}, "
        f"variables={result.get('variable_overrides', 0)}, "
        f"renamed={result.get('renamed_instances', 0)}"
    )
    return (
        f"{result.get('created', 0)} config(s) cloned into {target} "
        f"({overrides}, flow_task_remaps={result.get('flow_task_remaps', 0)})."
    )


def _format_clone_result(formatter: Any, result: dict[str, Any]) -> None:
    """Human-mode rendering for ``sync clone``; the headline follows it."""
    status = result.get("status", "")
    if status == "dry_run":
        formatter.console.print("[yellow]Dry run -- nothing pushed.[/yellow]")
        formatter.console.print(
            f"  Overrides applied: buckets={result.get('bucket_rewrites', 0)}, "
            f"variables={result.get('variable_overrides', 0)}, "
            f"renamed={result.get('renamed_instances', 0)}"
        )
        # The creates the real clone would refuse, each with its remedy.
        _print_errors(formatter, result)
        return
    _print_clone_buckets(formatter, result)
    if status == "no_changes":
        return
    _print_errors(formatter, result)


def _print_errors(formatter: Any, result: dict[str, Any]) -> None:
    for err in result.get("errors", []):
        formatter.item_error(
            f"{err.get('change_type')} {err.get('component_id')}/{err.get('config_id')}",
            err.get("message") or "",
        )


def clone_total(result: dict[str, Any], failed: int) -> int:
    """The items a clone headline counts: planned on a dry run, else done plus failed."""
    if result.get("status") == "dry_run":
        return result.get("summary", {}).get("added", 0) + len(result.get("bucket_errors", []))
    return result.get("created", 0) + failed
