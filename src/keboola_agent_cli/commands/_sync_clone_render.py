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
        formatter.warning(f"  Bucket error: {berr.get('bucket_id')}: {berr.get('error')}")


def _format_clone_result(formatter: Any, result: dict[str, Any]) -> None:
    """Human-mode rendering for ``sync clone``."""
    status = result.get("status", "")
    overrides = (
        f"buckets={result.get('bucket_rewrites', 0)}, "
        f"variables={result.get('variable_overrides', 0)}, "
        f"renamed={result.get('renamed_instances', 0)}"
    )
    if status == "dry_run":
        summary = result.get("summary", {})
        formatter.console.print("[yellow]Dry run -- nothing pushed.[/yellow]")
        formatter.console.print(f"  Overrides applied: {overrides}")
        formatter.console.print(
            f"  Would create {summary.get('added', 0)} config(s) in "
            f"[cyan]{result.get('target_alias')}[/cyan]."
        )
        return
    errors = result.get("errors", [])
    # A bucket error is a failed item too: the count matches the error lines printed below.
    failed = len(errors) + len(result.get("bucket_errors", []))
    if status == "no_changes":
        if failed:
            formatter.console.print(
                f"[bold red]Failed:[/bold red] Already cloned into "
                f"[cyan]{result.get('target_alias')}[/cyan], {failed} failed"
            )
        else:
            formatter.console.print(
                f"[green]Already cloned[/green] -- no changes to push into "
                f"[cyan]{result.get('target_alias')}[/cyan]."
            )
        _print_clone_buckets(formatter, result)
        return
    headline = (
        f"Cloned into {result.get('target_alias')}: {result.get('created', 0)} created "
        f"({overrides}, flow_task_remaps={result.get('flow_task_remaps', 0)})"
    )
    if failed:
        formatter.console.print(f"[bold red]Failed:[/bold red] {headline}, {failed} failed")
    else:
        formatter.success(headline)
    _print_clone_buckets(formatter, result)
    for err in errors:
        formatter.warning(
            f"  Error: {err.get('change_type')} "
            f"{err.get('component_id')}/{err.get('config_id')}: {err.get('message')}"
        )
