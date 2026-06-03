"""Reference-data (dimension-member) operations for ``SemanticLayerService``.

Extracted from :mod:`semantic_layer_service` (which composes these via thin
public methods) so that orchestrator stays under the CONTRIBUTING.md
file-size ceiling — mirroring the existing ``_semantic_layer_crud`` /
``_semantic_layer_internals`` / ``_semantic_layer_lookup`` split.

``semantic-reference-data`` is a per-dimension member store: one record per
dimension, holding the full member list in a ``members[]`` array (e.g. a
Chart of Accounts). It is deliberately kept OUT of ``build`` / ``export`` /
``diff`` / cascade / ``PUSH_ORDER`` — these are its full, self-contained
CRUD operations. Each ``run_*`` helper takes an ``open_client`` thunk and
owns the client lifecycle (same shape as ``_semantic_layer_lookup``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..errors import ErrorCode, KeboolaApiError
from ..metastore_client import MetastoreClient
from ._semantic_layer_internals import resolve_model_uuid

REFERENCE_DATA_TYPE = "semantic-reference-data"

OpenClient = Callable[[], MetastoreClient]


def unpack_record(
    alias: str,
    item: dict[str, Any],
    *,
    include_members: bool,
    include_project: bool = True,
) -> dict[str, Any]:
    """Project a raw metastore item into the CLI reference-data shape.

    ``include_project`` controls the top-level ``project`` key — the list
    summary omits it (the alias is already on the envelope), the detail shapes
    keep it.
    """
    attrs = item.get("attributes") or {}
    members = attrs.get("members") or []
    out: dict[str, Any] = {}
    if include_project:
        out["project"] = alias
    out.update(
        {
            "id": item.get("id", ""),
            "dimension_name": attrs.get("dimensionName", ""),
            "model_uuid": attrs.get("modelUUID", ""),
            "dataset_id": attrs.get("datasetId"),
            "description": attrs.get("description"),
            "member_count": len(members),
            "revision": (item.get("meta") or {}).get("revision"),
        }
    )
    if include_members:
        out["members"] = members
    return out


def find_by_dimension(
    client: MetastoreClient,
    dimension: str,
) -> dict[str, Any] | None:
    """Return the existing record for ``dimension`` (project-wide) or None.

    The ``semantic-reference-data`` envelope ``name`` (= the dimension) is
    unique **per project per type**, so the lookup is project-wide and
    independent of any model — this keeps ``set`` idempotent regardless of the
    ``--model`` passed (a record created under model A is still found, and
    PUT-replaced, when ``set --model B --dimension <same>`` runs, instead of
    taking the POST path and colliding with ``ALREADY_EXISTS``).
    """
    for item in client.list_items(REFERENCE_DATA_TYPE):
        if (item.get("attributes") or {}).get("dimensionName") == dimension:
            return item
    return None


def run_list(
    open_client: OpenClient,
    alias: str,
    model_name_or_uuid: str | None,
) -> dict[str, Any]:
    """List reference-data records (optionally scoped to one model).

    Returns ``{project, reference_data: [{id, dimension_name, model_uuid,
    dataset_id, member_count}]}``. Member lists are omitted from the summary
    — use :func:`run_get` for the full members.
    """
    client = open_client()
    try:
        model_uuid: str | None = None
        if model_name_or_uuid is not None:
            model_uuid, _ = resolve_model_uuid(client, model_name_or_uuid)
        raw = client.list_items(REFERENCE_DATA_TYPE, model_uuid)
    finally:
        client.close()
    records = [
        unpack_record(alias, item, include_members=False, include_project=False) for item in raw
    ]
    return {"project": alias, "reference_data": records}


def run_get(
    open_client: OpenClient,
    alias: str,
    *,
    record_id: str | None,
    dimension: str | None,
) -> dict[str, Any]:
    """Fetch one record by ``record_id``, or by ``dimension``.

    ``dimension`` is the project-unique key (the metastore envelope ``name``),
    so resolving by dimension is a project-wide lookup — no model needed.
    """
    if record_id is None and dimension is None:
        raise KeboolaApiError(
            message="Provide --id or --dimension.",
            error_code=ErrorCode.VALIDATION_ERROR,
        )
    client = open_client()
    try:
        if record_id is not None:
            item = client.get_item(REFERENCE_DATA_TYPE, record_id)
        elif dimension is not None:
            found = find_by_dimension(client, dimension)
            if found is None:
                raise KeboolaApiError(
                    message=(
                        f"No reference-data record for dimension {dimension!r} "
                        f"in project {alias!r}."
                    ),
                    error_code=ErrorCode.NOT_FOUND,
                )
            item = found
        else:  # pragma: no cover - the guard above guarantees one is set
            raise KeboolaApiError(
                message="Provide --id or --dimension.",
                error_code=ErrorCode.VALIDATION_ERROR,
            )
    finally:
        client.close()
    return unpack_record(alias, item, include_members=True)


def run_set(
    open_client: OpenClient,
    alias: str,
    model_name_or_uuid: str | None,
    *,
    dimension: str,
    members: list[dict[str, Any]],
    dataset_id: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Create or replace a reference-data record, keyed by ``dimension``.

    Idempotent on ``dimensionName`` (the project-unique envelope ``name``): an
    existing record is replaced in place via ``PUT`` (revision increments,
    history preserved), with ``modelUUID`` updated to the resolved model;
    otherwise a new record is ``POST``-ed. The lookup is project-wide, so
    ``set`` stays idempotent regardless of which ``--model`` is passed.
    """
    if not isinstance(members, list):
        raise KeboolaApiError(
            message="members must be a JSON array of member objects.",
            error_code=ErrorCode.VALIDATION_ERROR,
        )
    client = open_client()
    try:
        model_uuid, _ = resolve_model_uuid(client, model_name_or_uuid)
        data: dict[str, Any] = {
            "modelUUID": model_uuid,
            "dimensionName": dimension,
            "members": members,
        }
        if dataset_id:
            data["datasetId"] = dataset_id
        if description:
            data["description"] = description

        existing = find_by_dimension(client, dimension)
        if existing is not None:
            item = client.put_item(
                REFERENCE_DATA_TYPE,
                existing.get("id", ""),
                name=dimension,
                data=data,
            )
            action = "updated"
        else:
            item = client.post_item(REFERENCE_DATA_TYPE, name=dimension, data=data)
            action = "created"
    finally:
        client.close()
    result = unpack_record(alias, item, include_members=False)
    result["action"] = action
    return result


def run_delete(open_client: OpenClient, alias: str, record_id: str) -> dict[str, Any]:
    """Delete a reference-data record by UUID (server-side soft-delete)."""
    client = open_client()
    try:
        item = client.get_item(REFERENCE_DATA_TYPE, record_id)
        attrs = item.get("attributes") or {}
        client.delete_item(REFERENCE_DATA_TYPE, record_id)
    finally:
        client.close()
    return {
        "project": alias,
        "removed": {"id": record_id, "dimension_name": attrs.get("dimensionName", "")},
    }
