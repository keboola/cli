"""Table helpers of `StorageService`: row shaping, the create-table source, branch checks.

Lives outside ``storage_service.py`` because that module is over its
file-size budget (CONTRIBUTING.md > "File-size budgets"): pure helpers carry
no orchestration, so they are what moves out.
"""

from __future__ import annotations

from typing import Any

from ..errors import ErrorCode, KeboolaApiError

CLONE_TABLE_PRODUCTION_MESSAGE = (
    "clone-table copies a production table into a dev branch, so its target must "
    "be a dev branch; --branch 0 is production. Pass --branch <dev branch ID> "
    "(see 'kbagent branch list'), or create one with 'kbagent branch create'."
)


def refuse_production_clone(branch_id: int | None) -> None:
    """Refuse ``clone-table`` into production (``branch_id`` 0) before any API call.

    The pull endpoint exists only under ``/v2/storage/branch/{id}/``, and its
    source is always production, so production as the target has no meaning.

    Raises:
        KeboolaApiError: ``INVALID_ARGUMENT`` (HTTP 400 over ``kbagent serve``).
    """
    if branch_id == 0:
        raise KeboolaApiError(
            message=CLONE_TABLE_PRODUCTION_MESSAGE,
            status_code=400,
            error_code=ErrorCode.INVALID_ARGUMENT,
            retryable=False,
        )


def build_table_source(
    source_table_id: str | None,
    source_branch_id: int | None,
) -> dict[str, Any] | None:
    """Build the optional ``source`` object for the tables-definition endpoint.

    Returns ``None`` when no source is requested. ``branchId`` is only included
    when given and not 0; otherwise the API defaults it to the request branch.
    ``source_branch_id`` 0 is production (``--source-branch-id 0``): in a
    production request it is the request branch, and in a dev-branch request
    the caller sets the numeric ID of the default branch.
    """
    if source_table_id is None:
        return None
    if not source_table_id.strip():
        raise ValueError("--source-table-id must not be empty.")
    source: dict[str, Any] = {"tableId": source_table_id}
    if source_branch_id:
        source["branchId"] = source_branch_id
    return source


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
