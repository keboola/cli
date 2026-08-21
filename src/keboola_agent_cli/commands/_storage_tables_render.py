"""Human-mode rendering for `kbagent storage tables`.

Lives outside ``commands/storage.py`` because that module is over its
file-size budget (see CONTRIBUTING.md > "File-size budgets"): presentation
detail is the part that carries no command wiring, so it is the part that
moves out.
"""

from __future__ import annotations

from typing import Any

from rich.table import Table


def format_used_by(used_by: list[dict[str, Any]]) -> str:
    """Render a table's `used_by` references as one cell.

    Names the referencing configurations rather than the raw mapping scope,
    because the question this column answers is "what breaks if this table
    changes?". One configuration reading AND writing the same table appears
    once, not twice.
    """
    if not used_by:
        return "-"
    labels = []
    for ref in used_by:
        label = f"{ref.get('component_id', '')}/{ref.get('config_id', '')}"
        if ref.get("row_id"):
            label += f"#{ref['row_id']}"
        labels.append(label)
    seen: set[str] = set()
    return ", ".join(x for x in labels if not (x in seen or seen.add(x)))


def render_tables(console: Any, tables: list[dict[str, Any]], include_usage: bool) -> None:
    """Print one Rich table per project.

    Grouping by project keeps a multi-project listing readable: without it,
    rows from different projects interleave with only the alias to tell them
    apart.
    """
    by_project: dict[str, list[dict[str, Any]]] = {}
    for t in tables:
        by_project.setdefault(t["project_alias"], []).append(t)

    for alias, proj_tables in by_project.items():
        table = Table(title=f"Tables - {alias}")
        table.add_column("Table ID", style="bold cyan")
        table.add_column("Rows", justify="right")
        table.add_column("Size", justify="right", style="dim")
        table.add_column("Last Import", style="dim")
        if include_usage:
            table.add_column("Used By", style="dim")

        for t in proj_tables:
            size_mb = t["data_size_bytes"] / (1024 * 1024) if t["data_size_bytes"] else 0
            last_import = t.get("last_import_date", "")
            if last_import and "T" in last_import:
                last_import = last_import.split("T")[0]
            row = [t["id"], str(t["rows_count"]), f"{size_mb:.1f} MB", last_import]
            if include_usage:
                row.append(format_used_by(t.get("used_by", [])))
            table.add_row(*row)

        console.print(table)
        console.print()
