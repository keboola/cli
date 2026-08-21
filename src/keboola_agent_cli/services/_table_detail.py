"""Pure assembly of the `storage table-detail` response payload.

`StorageService.get_table_detail()` is two things stacked: an I/O step (resolve
the project, fetch `GET /v2/storage/tables/{id}`) and a pure transform of the
resource into kbagent's stable field names. Only the first needs a client, so
the second lives here -- the split CONTRIBUTING.md prescribes for a service that
mixes orchestration with parsing, and it keeps `storage_service.py` shrinking
against its grandfathered file-size ceiling.

The transform is an explicit allowlist, which is deliberate (the API resource is
large and its legacy corners are not worth re-exporting) but has a failure mode
worth naming: a field the API adds is invisible until someone adds it here.
Issue #621 was exactly that -- `definition`, the only readable record of a
BigQuery table's partition/cluster layout, was dropped for as long as the
command existed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Descriptions:
    """The user-authored descriptions carried in a table's metadata list."""

    table: str = ""
    columns: dict[str, str] = field(default_factory=dict)


def _split_descriptions(raw_metadata: list[dict[str, Any]]) -> Descriptions:
    """Pull the user table description and per-column descriptions out of metadata.

    Keboola has no user-writable column-metadata endpoint, so column descriptions
    are stored as `KBC.column.{name}.description` rows in the *table's* metadata
    list rather than alongside the column.
    """
    table_description = ""
    col_descriptions: dict[str, str] = {}
    for entry in raw_metadata:
        key = entry.get("key", "")
        if key == "KBC.description" and entry.get("provider") == "user":
            table_description = entry.get("value", "") or ""
        elif key.startswith("KBC.column.") and key.endswith(".description"):
            col_name = key[len("KBC.column.") : -len(".description")]
            col_descriptions[col_name] = entry.get("value", "") or ""
    return Descriptions(table=table_description, columns=col_descriptions)


def _column_details(
    columns: list[str],
    column_metadata: dict[str, list[dict[str, Any]]],
    col_descriptions: dict[str, str],
) -> list[dict[str, Any]]:
    """Build the per-column view from `columnMetadata`'s KBC.datatype.* rows.

    Types are read from `columnMetadata`, never from `definition` -- the latter
    has really been served as `[]` (see tests/test_storage_empty_definition.py).
    """
    details: list[dict[str, Any]] = []
    for col in columns:
        col_info: dict[str, Any] = {"name": col}
        for entry in column_metadata.get(col, []):
            key = entry.get("key", "")
            value = entry.get("value", "")
            if key == "KBC.datatype.basetype":
                col_info["type"] = value
            elif key == "KBC.datatype.type":
                # Native backend type (e.g. "VARCHAR", "NUMBER", "TIMESTAMP_TZ")
                # -- distinct from the Keboola basetype it maps to.
                col_info["native_type"] = value
            elif key == "KBC.datatype.length":
                # Length as stored: "40", "18,2", "255", ...
                col_info["length"] = value
            elif key == "KBC.datatype.nullable":
                col_info["nullable"] = value == "1"
            elif key == "KBC.datatype.default":
                col_info["default"] = value
        if col in col_descriptions:
            col_info["description"] = col_descriptions[col]
        details.append(col_info)
    return details


def build_table_detail(alias: str, table_id: str, table: dict[str, Any]) -> dict[str, Any]:
    """Map a Storage API table resource onto kbagent's `table-detail` payload.

    Args:
        alias: Project alias the table was fetched from.
        table_id: The requested table ID, used when the resource omits its own.
        table: Raw `GET /v2/storage/tables/{id}` resource.

    Returns:
        The `storage table-detail` payload, `definition` included.
    """
    columns = table.get("columns", [])
    raw_metadata: list[dict[str, Any]] = table.get("metadata", [])
    descriptions = _split_descriptions(raw_metadata)

    return {
        "project_alias": alias,
        "table_id": table.get("id", table_id),
        "name": table.get("name", ""),
        "display_name": table.get("displayName", ""),
        "bucket_id": table.get("bucket", {}).get("id", ""),
        # Storage backend of the owning bucket (e.g. "snowflake",
        # "bigquery"). Consumers: the web UI keys BigQuery-only features
        # (repartition) off it, and type resolution picks the matching
        # INFORMATION_SCHEMA dialect for alias / linked tables.
        "backend": table.get("bucket", {}).get("backend", ""),
        "description": descriptions.table,
        "columns": columns,
        "column_details": _column_details(
            columns, table.get("columnMetadata", {}), descriptions.columns
        ),
        "primary_key": table.get("primaryKey", []),
        # API may return null on empty tables; coerce to 0.
        "rows_count": table.get("rowsCount") or 0,
        "data_size_bytes": table.get("dataSizeBytes") or 0,
        "is_alias": table.get("isAlias", False),
        "last_import_date": table.get("lastImportDate", ""),
        "last_change_date": table.get("lastChangeDate", ""),
        "created": table.get("created", ""),
        "metadata": raw_metadata,
        # Passed through verbatim (issue #621). Present on EVERY table-detail
        # response -- an untyped table gets one too -- so `None` here means the
        # stack omitted the key, never "this table is untyped". For a BigQuery
        # table this is the only readable record of the registered
        # timePartitioning / rangePartitioning / clustering layout, plus
        # `requirePartitionFilter` and an unbounded `partitions[]` list. Not
        # re-shaped: trimming an API field is the bug this key exists to fix.
        "definition": table.get("definition"),
    }
