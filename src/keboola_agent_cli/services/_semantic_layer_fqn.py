"""Semantic-layer dataset ``fqn``: derive it from Storage, and audit stored ones.

The ``fqn`` is the table-detail payload's ``sql_path`` -- the quoted table
location built from the owning bucket's Storage ``backendPath``.
"""

from __future__ import annotations

from typing import Any

from ..errors import ErrorCode, KeboolaApiError


def derive_dataset_fqn(table_id: str, detail: dict[str, Any]) -> str:
    """Return the ``fqn`` for ``table_id`` from its ``storage table-detail`` payload.

    Raises:
        KeboolaApiError: VALIDATION_ERROR when the payload carries no ``sql_path``
            (Storage reported no location, the backend is unsupported, or an
            identifier could not be quoted safely).
    """
    sql_path = detail.get("sql_path")
    if not sql_path:
        backend = detail.get("backend", "") or "unknown"
        raise KeboolaApiError(
            message=(
                f"Cannot derive the fqn of table {table_id!r}: Storage reported no "
                f"usable warehouse location (backend {backend!r}, backendPath "
                f"{detail.get('backend_path', [])!r}). Add this table with "
                "`semantic-layer add dataset --fqn FQN` instead."
            ),
            error_code=ErrorCode.VALIDATION_ERROR,
        )
    return str(sql_path)


def append_fqn_mismatch_warnings(
    datasets: list[dict[str, Any]],
    details_by_tid: dict[str, dict[str, Any]],
    warnings: list[dict[str, str]],
) -> None:
    """Warn about datasets whose stored ``fqn`` is not the table's Storage location.

    Datasets whose table detail could not be fetched, or whose location Storage
    does not report, are skipped.
    """
    for ds in datasets:
        detail = details_by_tid.get(ds.get("tableId", ""))
        expected = detail.get("sql_path") if detail else None
        stored = ds.get("fqn", "")
        if not expected or stored == expected:
            continue
        warnings.append(
            {
                "type": "FQN_MISMATCH",
                "item": ds.get("name", "?"),
                "detail": (
                    f"fqn {stored!r} does not match the table's warehouse location "
                    f"{expected!r}. Metrics whose sql embeds the old fqn need the same fix."
                ),
            }
        )
