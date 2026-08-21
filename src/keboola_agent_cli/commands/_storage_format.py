"""Human-mode rendering helpers for the ``kbagent storage`` group.

Lives in a private module because ``commands/storage.py`` is already past the
1,200-LOC commands-file ceiling (CONTRIBUTING.md > "File-size budgets"), so
rendering that grows -- such as the typed-table layout added for #621 -- moves
here rather than extending it further.
"""

from __future__ import annotations

from typing import Any

from rich.markup import escape
from rich.table import Table

from ..output import OutputFormatter


def _format_table_layout(definition: Any) -> list[tuple[str, str]]:
    """Render a typed table's partition/cluster layout as label/value rows.

    ``definition`` is the Storage API's typed-table descriptor: ``None`` for an
    untyped table, and on BigQuery it carries ``timePartitioning``,
    ``rangePartitioning`` and ``clustering``. Returns an empty list whenever
    there is no layout to show, so Snowflake and untyped-table output stays
    byte-identical.
    """
    if not isinstance(definition, dict):
        return []
    rows: list[tuple[str, str]] = []
    time_part = definition.get("timePartitioning") or {}
    if isinstance(time_part, dict) and time_part.get("type"):
        # `field` is absent for ingestion-time partitioning, where BigQuery
        # partitions on the load timestamp rather than on a column.
        field = time_part.get("field")
        rows.append(
            ("Partitioning", f"{time_part['type']} on {field}" if field else time_part["type"])
        )
    range_part = definition.get("rangePartitioning") or {}
    if isinstance(range_part, dict) and range_part.get("field"):
        rng = range_part.get("range") or {}
        bounds = (
            f" [{rng.get('start')}, {rng.get('end')}) step {rng.get('interval')}"
            if isinstance(rng, dict) and rng
            else ""
        )
        rows.append(("Range partitioning", f"{range_part['field']}{bounds}"))
    clustering = definition.get("clustering") or {}
    if isinstance(clustering, dict) and clustering.get("fields"):
        rows.append(("Clustering", ", ".join(str(f) for f in clustering["fields"])))
    return rows


def render_table_detail(formatter: OutputFormatter, result: dict[str, Any]) -> None:
    """Print the human-mode ``storage table-detail`` report.

    Layout rows appear only for a typed table that actually declares one, so
    Snowflake and untyped-table output is unchanged.
    """
    formatter.console.print(f"[bold]Table:[/bold] {result['table_id']}")
    formatter.console.print(f"  Name: {escape(result['display_name'] or result['name'])}")
    formatter.console.print(f"  Bucket: {result['bucket_id']}")
    formatter.console.print(f"  Rows: {result['rows_count']:,}")
    size_mb = result["data_size_bytes"] / (1024 * 1024)
    formatter.console.print(f"  Size: {size_mb:.2f} MB")
    if result["primary_key"]:
        formatter.console.print(f"  Primary key: {', '.join(result['primary_key'])}")
    for label, value in _format_table_layout(result.get("definition")):
        formatter.console.print(f"  {label}: {value}")
    if result["last_import_date"]:
        formatter.console.print(f"  Last import: {result['last_import_date']}")

    if result["column_details"]:
        formatter.console.print()
        table = Table(title="Columns")
        table.add_column("Name", style="bold cyan")
        table.add_column("Type", style="dim")
        table.add_column("Nullable", style="dim")
        for col in result["column_details"]:
            table.add_row(
                col["name"],
                col.get("type", ""),
                "yes" if col.get("nullable") else "",
            )
        formatter.console.print(table)
