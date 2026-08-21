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

from ._column_descriptions import (
    collect_legacy_column_entries,
    description_from_column_meta,
    native_column_descriptions,
)


@dataclass(frozen=True)
class Descriptions:
    """The user-authored descriptions carried in a table's metadata list."""

    table: str = ""
    # Column name -> value of its LEGACY flat `KBC.column.*.description` key.
    # Only the legacy tier lives in the table's metadata list; the two tiers
    # that outrank it are read per column in `_column_details`.
    columns: dict[str, str] = field(default_factory=dict)


def _split_descriptions(raw_metadata: list[dict[str, Any]]) -> Descriptions:
    """Pull the user table description and legacy column descriptions out of metadata.

    Before 0.88.0 kbagent stored column descriptions as
    `KBC.column.{name}.description` rows in the *table's* metadata list, on the
    belief that Keboola had no user-writable column-level endpoint. It does --
    see `_column_descriptions.write_column_descriptions` (#624) -- so that shape
    is legacy: nothing but this CLI ever read it. It is still parsed here as the
    last-resort tier so already-documented projects keep rendering.
    """
    table_description = ""
    for entry in raw_metadata:
        if entry.get("key") == "KBC.description" and entry.get("provider") == "user":
            table_description = entry.get("value", "") or ""
    legacy = collect_legacy_column_entries(raw_metadata)
    return Descriptions(
        table=table_description,
        columns={col: (entry.get("value") or "") for col, entry in legacy.items()},
    )


def _column_details(
    columns: list[str],
    column_metadata: dict[str, list[dict[str, Any]]],
    legacy_descriptions: dict[str, str],
    native_descriptions: dict[str, str],
    source_column_metadata: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Build the per-column view from `columnMetadata`'s KBC.datatype.* rows.

    Types are read from `columnMetadata`, never from `definition` -- the latter
    has really been served as `[]` (see tests/test_storage_empty_definition.py).

    Descriptions resolve in three tiers (#624): the native `definition` field
    the Keboola UI writes and shows, then `KBC.description` in `columnMetadata`
    (where the backend mirrors that write, and the only place the MCP server
    looks), then the legacy flat key. An alias carries no `columnMetadata` of
    its own, so it falls back to the source table's -- the same lookup the MCP
    server performs.
    """
    details: list[dict[str, Any]] = []
    for col in columns:
        col_info: dict[str, Any] = {"name": col}
        entries = column_metadata.get(col, [])
        for entry in entries:
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
        meta_desc = description_from_column_meta(entries)
        if meta_desc is None:
            meta_desc = description_from_column_meta(source_column_metadata.get(col, []))
        description = native_descriptions.get(col) or meta_desc or legacy_descriptions.get(col)
        if description:
            col_info["description"] = description
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
    source_column_metadata: dict[str, list[dict[str, Any]]] = (
        (table.get("sourceTable") or {}).get("columnMetadata") or {} if table.get("isAlias") else {}
    )

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
            columns,
            table.get("columnMetadata", {}),
            descriptions.columns,
            native_column_descriptions(table),
            source_column_metadata,
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
        # Columns still carrying a legacy flat KBC.column.*.description key
        # (invisible to the Keboola UI and the MCP server). Always present,
        # empty when there are none; the CLI turns a non-empty list into a hint
        # to run `kbagent storage describe-migrate` (#624). Reporting only --
        # a read never rewrites what it finds.
        "legacy_column_descriptions": sorted(descriptions.columns),
    }
