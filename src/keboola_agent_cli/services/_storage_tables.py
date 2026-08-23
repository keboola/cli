"""Row shaping for `StorageService.list_tables`.

Lives outside ``storage_service.py`` because that module is over its
file-size budget (CONTRIBUTING.md > "File-size budgets"): the API-to-output
field mapping carries no orchestration, so it is what moves out.
"""

from __future__ import annotations

from typing import Any


def normalize_table_rows(
    alias: str,
    raw_tables: list[dict[str, Any]],
    usage: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Map Storage API table records onto kbagent's output shape.

    Args:
        alias: Project alias, stamped on every row so a multi-project listing
            stays attributable.
        raw_tables: Table records as returned by the Storage API.
        usage: When given, ``{table_id: [reference, ...]}`` from
            ``collect_table_usage``; each row then carries a ``used_by`` key.
            None omits the key entirely, so a caller can tell "usage not
            requested" from "requested, nothing found".

    Returns:
        One output dict per input table, in input order.
    """
    rows: list[dict[str, Any]] = []
    for t in raw_tables:
        bucket = t.get("bucket")
        table_id = t.get("id", "")
        row: dict[str, Any] = {
            "project_alias": alias,
            "id": table_id,
            "name": t.get("name", ""),
            "display_name": t.get("displayName", t.get("name", "")),
            "bucket_id": bucket.get("id", "") if isinstance(bucket, dict) else "",
            # API may return null on empty tables; coerce to 0.
            "rows_count": t.get("rowsCount") or 0,
            "data_size_bytes": t.get("dataSizeBytes") or 0,
            "is_alias": t.get("isAlias", False),
            "last_import_date": t.get("lastImportDate", ""),
        }
        if usage is not None:
            row["used_by"] = usage.get(table_id, [])
        rows.append(row)
    return rows
