"""Pure CLONE-vs-COPY decision for ``workspace load`` (issue #687).

A workspace load defaults to COPY: the backend physically re-materializes the
table inside the workspace schema, which costs warehouse time and storage and
is what makes loading a large table slow enough to blow past any client-side
poll budget. Snowflake and BigQuery can instead register a zero-copy CLONE,
which is near-instant and costs nothing extra -- but only when the table and
the workspace line up. kbagent therefore decides per table rather than sending
one blanket load type for the whole request.

The eligibility rules below mirror the server's ``LoadTypeDecider::canClone``.
Mirroring is deliberate and has a narrow job: it only picks the DEFAULT. An
explicitly requested ``--load-type clone`` / ``view`` is never pre-validated
here -- it is sent as asked, and an ineligible combination comes back as a
loud HTTP 400 naming the exact reason, which is strictly better than a
client-side guess that can drift from the server. The mirror exists so the
default can silently fall back to COPY instead of failing; a stale rule here
costs a slower load, never a wrong one.

kbagent never sends columns / filters / incremental in a workspace load, so
``canClone``'s "full load only" precondition is satisfied by construction and
is not re-checked.
"""

from dataclasses import dataclass
from typing import Any

# Wire values. The Storage API request validator matches the UPPERCASE enum
# members, so these are what go into the request body; the CLI/JSON surfaces
# speak the lowercase form.
LOAD_TYPE_CLONE = "CLONE"
LOAD_TYPE_COPY = "COPY"
LOAD_TYPE_VIEW = "VIEW"

# Only these two workspace backends implement a zero-copy clone at all.
CLONE_CAPABLE_WORKSPACE_BACKENDS = frozenset({"snowflake", "bigquery"})


@dataclass(frozen=True)
class LoadTablePlan:
    """How one table will be loaded, and why.

    Attributes:
        table_id: Storage table ID being loaded.
        load_type: Wire value -- ``CLONE`` / ``COPY`` / ``VIEW`` (uppercase).
        data_size_bytes: On-disk size from the table detail, or ``None`` when
            the detail was not fetched (explicit clone/view) or the API did
            not report it. ``None`` is "unknown", never "empty".
        clone_ineligible_reason: Short human reason the auto-decision fell
            back to COPY. ``None`` whenever no fallback happened -- i.e. for
            a CLONE, and for any explicitly requested load type.
    """

    table_id: str
    load_type: str
    data_size_bytes: int | None
    clone_ineligible_reason: str | None


def coerce_data_size_bytes(table_detail: dict[str, Any]) -> int | None:
    """Read ``dataSizeBytes`` off a table detail as an int, or ``None``.

    The Storage API has been seen to report the size as a numeric string, and
    omits the key entirely for some table shapes. A guessed 0 would silently
    disarm the large-COPY guard, so anything unparseable stays ``None``.
    """
    raw = table_detail.get("dataSizeBytes")
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _clone_ineligible_reason(workspace_backend: str, table_detail: dict[str, Any]) -> str | None:
    """Return why this table cannot be cloned, or ``None`` when it can.

    Mirrors ``LoadTypeDecider::canClone``; see the module docstring for why
    this is advisory-only.
    """
    backend = (workspace_backend or "").strip().lower()
    if backend not in CLONE_CAPABLE_WORKSPACE_BACKENDS:
        return f"workspace backend {backend or 'unknown'} does not support clone"

    bucket = table_detail.get("bucket") or {}
    table_backend = str(bucket.get("backend") or "").strip().lower()
    if table_backend != backend:
        return f"backend mismatch: table {table_backend or 'unknown'} vs workspace {backend}"

    if bucket.get("hasExternalSchema"):
        return "external schema bucket"

    # BigQuery cannot clone across the dataset boundary a linked bucket
    # introduces; Snowflake can, so this check is backend-specific.
    if backend == "bigquery" and bucket.get("isLinked"):
        return "linked bucket on BigQuery"

    if table_detail.get("isAlias"):
        if not table_detail.get("aliasColumnsAutoSync"):
            return "alias without column auto-sync"
        if table_detail.get("aliasFilter"):
            return "filtered alias"

    return None


def plan_auto_load_type(
    workspace_backend: str,
    table_id: str,
    table_detail: dict[str, Any],
) -> LoadTablePlan:
    """Decide CLONE (preferred) vs COPY (fallback) for one table.

    Args:
        workspace_backend: ``connection.backend`` of the target workspace.
        table_id: Storage table ID being loaded.
        table_detail: Raw ``GET /v2/storage/tables/{id}`` body.

    Returns:
        A :class:`LoadTablePlan`; ``clone_ineligible_reason`` is set only on
        the COPY fallback.
    """
    reason = _clone_ineligible_reason(workspace_backend, table_detail)
    return LoadTablePlan(
        table_id=table_id,
        load_type=LOAD_TYPE_COPY if reason else LOAD_TYPE_CLONE,
        data_size_bytes=coerce_data_size_bytes(table_detail),
        clone_ineligible_reason=reason,
    )
