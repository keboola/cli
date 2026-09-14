"""Semantic-layer dataset ``fqn``: derive it from Storage, and audit stored ones.

A dataset's ``fqn`` is pasted verbatim into SQL by every downstream consumer
(Kai, data apps, AI SQL generation from semantic context), so it has to be the
table's real warehouse location. That location is read from the table-detail
payload's ``sql_path`` (built from the owning bucket's ``backendPath``) and is
never reconstructed from the tableId.
"""

from __future__ import annotations

from typing import Any

from ..errors import ErrorCode, KeboolaApiError

# Characters that terminate a quoted identifier on the supported backends.
_IDENTIFIER_QUOTES = ('"', "`")


def derive_dataset_fqn(table_id: str, detail: dict[str, Any]) -> str:
    """Return the ``fqn`` for ``table_id`` from its ``storage table-detail`` payload.

    Raises:
        KeboolaApiError: VALIDATION_ERROR when a location identifier contains a
            quote character, or when Storage reports no usable location for the
            table's backend. Both messages point at ``--fqn``.
    """
    segments = [*detail.get("backend_path", []), detail.get("name", "")]
    if any(quote in segment for segment in segments for quote in _IDENTIFIER_QUOTES):
        raise KeboolaApiError(
            message=(
                f"The warehouse location of table {table_id!r} contains a quote "
                "character, so it cannot be written as a quoted fqn safely. "
                "Pass the fqn explicitly with --fqn."
            ),
            error_code=ErrorCode.VALIDATION_ERROR,
        )
    sql_path = detail.get("sql_path")
    if not sql_path:
        backend = detail.get("backend", "") or "unknown"
        raise KeboolaApiError(
            message=(
                f"Cannot derive the fqn of table {table_id!r}: Storage reported no "
                f"usable warehouse location (backend {backend!r}, backendPath "
                f"{detail.get('backend_path', [])!r}). Pass the fqn explicitly with --fqn."
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

    A warning rather than an error: an ``fqn`` set on purpose with
    ``add dataset --fqn`` may legitimately point elsewhere (e.g. a view).
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
