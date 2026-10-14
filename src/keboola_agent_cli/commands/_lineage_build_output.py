"""Human-mode ``lineage build`` output for the AI status block (#847).

Kept out of ``commands/lineage.py``, which is over its file-size budget.
"""

from __future__ import annotations

from typing import Any

from rich.markup import escape

from ..output import OutputFormatter

# Bound the unresolved-outputs / missing-code-file listings in human mode;
# --json always carries the full lists.
_MAX_LISTED = 10


def print_ai_status(formatter: OutputFormatter, ai_status: dict[str, Any]) -> None:
    """Print what applying AI results and generating AI tasks did."""
    console = formatter.console
    if ai_status.get("ai_results_applied"):
        console.print(
            f"\n  AI results applied: {ai_status.get('ai_edges_added', 0)} edges added"
            f" ({ai_status.get('ai_reads_added', 0)} reads,"
            f" {ai_status.get('ai_writes_added', 0)} writes),"
            f" {ai_status.get('ai_columns_merged', 0)} columns merged,"
            f" {ai_status.get('ai_external_added', 0)} external systems"
        )
        if ai_status.get("ai_col_map_skipped"):
            console.print(
                f"  Column mappings skipped (input table not matched exactly):"
                f" {ai_status['ai_col_map_skipped']}"
            )
        _print_bounded(
            formatter,
            "AI outputs not resolved to a table",
            [
                f"{u['config_fqn']}: {u['table']} ({u['reason']}"
                + (f": {', '.join(u['candidates'])}" if u.get("candidates") else "")
                + ")"
                for u in ai_status.get("ai_outputs_unresolved") or []
            ],
        )
    if ai_status.get("ai_tasks_generated"):
        console.print(
            f"\n  [bold]AI tasks generated: {ai_status['ai_tasks_generated']}[/bold]"
            f" (already done: {ai_status.get('ai_already_done', 0)})"
        )
        console.print(f"  Task file: {escape(str(ai_status['ai_tasks_file']))}")
        console.print("  Next: let your AI agent process the tasks, then re-run this command.")
    _print_bounded(
        formatter,
        "AI tasks skipped, code file not found",
        [
            f"{m['config_fqn']}: {m['code_file']}"
            for m in ai_status.get("ai_tasks_missing_code_file") or []
        ],
    )


def _print_bounded(formatter: OutputFormatter, title: str, lines: list[str]) -> None:
    if not lines:
        return
    formatter.console.print(f"\n  [yellow]{title}: {len(lines)}[/yellow]")
    for line in lines[:_MAX_LISTED]:
        formatter.console.print(f"    {escape(line)}")
    if len(lines) > _MAX_LISTED:
        formatter.console.print(f"    ... and {len(lines) - _MAX_LISTED} more")
