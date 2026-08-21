"""Human-mode rendering for `kbagent storage table-detail` (issue #621).

Lives in a private module because `commands/storage.py` is past the commands
file-size ceiling and is grandfathered at its recorded size (CONTRIBUTING.md >
"File-size budgets") -- it may shrink, not grow.

Beyond the columns table this renders the BigQuery *physical layout* the
Storage API reports under `definition`. Two shapes drive the guards here:

* `definition` is present on every table-detail response, typed or not, so the
  layout block keys off the layout fields themselves. A Snowflake or untyped
  table prints exactly what it printed before.
* `definition` has really been served as `[]` rather than an object (see
  tests/test_storage_empty_definition.py -- it broke the Go CLI's decoder), so
  the value is type-checked before any `.get()` reaches it.

`format_time_partitioning` / `format_range_partitioning` are shared with
`storage create-table`'s result output on purpose: verifying a repartition means
diffing what create-table said it applied against what table-detail reads back,
and that comparison is only trustworthy if both print the same string for the
same layout.
"""

from __future__ import annotations

from typing import Any

from rich.markup import escape
from rich.table import Table

from ..output import OutputFormatter


def format_time_partitioning(time_partitioning: dict[str, Any]) -> str:
    """Render a `timePartitioning` block, e.g. `DAY on created_at`."""
    field = time_partitioning.get("field")
    suffix = f" on {field}" if field else " (ingestion time)"
    return f"{time_partitioning.get('type')}{suffix}"


def format_range_partitioning(range_partitioning: dict[str, Any]) -> str:
    """Render a `rangePartitioning` block, e.g. `order_id [0, 1000000) step 1000`."""
    bounds = range_partitioning.get("range") or {}
    suffix = ""
    if bounds:
        suffix = f" [{bounds.get('start')}, {bounds.get('end')}) step {bounds.get('interval')}"
    return f"{range_partitioning.get('field')}{suffix}"


def render_table_layout(formatter: OutputFormatter, definition: Any) -> None:
    """Print the physical layout recorded in a table `definition`, if it has one.

    Prints nothing at all when the table has no partitioning or clustering --
    which is every Snowflake and every untyped table.
    """
    if not isinstance(definition, dict):
        return

    time_partitioning = definition.get("timePartitioning")
    if isinstance(time_partitioning, dict):
        formatter.console.print(
            f"  Time partitioning: {format_time_partitioning(time_partitioning)}"
        )
    range_partitioning = definition.get("rangePartitioning")
    if isinstance(range_partitioning, dict):
        formatter.console.print(
            f"  Range partitioning: {format_range_partitioning(range_partitioning)}"
        )
    clustering = definition.get("clustering")
    if isinstance(clustering, dict) and clustering.get("fields"):
        formatter.console.print(f"  Clustering: {', '.join(clustering['fields'])}")
    if definition.get("requirePartitionFilter"):
        # A query that omits a filter on the partition column fails outright, so
        # this is not trivia -- it changes how the table must be read.
        formatter.console.print("  Partition filter required: yes")
    partitions = definition.get("partitions")
    if isinstance(partitions, list) and partitions:
        # Count only. The API returns one entry per physical partition straight
        # from INFORMATION_SCHEMA.PARTITIONS -- a DAY-partitioned table with a
        # few years of history has thousands, which would bury everything above.
        # `--json` still carries the full list.
        formatter.console.print(f"  Partitions: {len(partitions):,}")


def render_table_detail(formatter: OutputFormatter, result: dict[str, Any]) -> None:
    """Print the full human-mode `storage table-detail` view."""
    formatter.console.print(f"[bold]Table:[/bold] {result['table_id']}")
    formatter.console.print(f"  Name: {escape(result['display_name'] or result['name'])}")
    formatter.console.print(f"  Bucket: {result['bucket_id']}")
    formatter.console.print(f"  Rows: {result['rows_count']:,}")
    size_mb = result["data_size_bytes"] / (1024 * 1024)
    formatter.console.print(f"  Size: {size_mb:.2f} MB")
    if result["primary_key"]:
        formatter.console.print(f"  Primary key: {', '.join(result['primary_key'])}")
    render_table_layout(formatter, result.get("definition"))
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
