"""Cascade-delete helper for ``semantic-layer model delete``.

Extracted from :class:`SemanticLayerService.delete_model` to keep the
parent service file under the 1500 LOC hard ceiling (CONTRIBUTING.md
"File-size budgets"). The function operates on a metastore client +
already-fetched children and returns the public envelope; the service
method is now a thin orchestrator that resolves the project, builds
the client, fetches the children, and forwards.

The rollback semantics mirror
:func:`_semantic_layer_internals.push_built_model`: each child DELETE
is wrapped individually so sibling failures do not abort the cascade,
and on any failure the parent is **preserved** and a
:class:`KeboolaApiError` is raised carrying ``details.cascade`` so the
caller can re-run after fixing the underlying error.
"""

from __future__ import annotations

import logging
from typing import Any

from ..errors import ErrorCode, KeboolaApiError
from ..metastore_client import MetastoreClient, SemanticType
from ._semantic_layer_internals import PUSH_ORDER

logger = logging.getLogger(__name__)


def cascade_delete_model(
    client: MetastoreClient,
    *,
    alias: str,
    model_uuid: str,
    model_attrs: dict[str, Any],
    children: dict[SemanticType, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Cascade-delete a semantic-layer model and its children.

    Walks ``reversed(PUSH_ORDER)`` (constraints → glossary →
    relationships → metrics → datasets), calling
    :meth:`MetastoreClient.delete_item` per child before the parent.
    Constraints reference metrics by name and metrics reference dataset
    tableIds, so this order kills the references before their targets.

    On any child-DELETE failure the parent is preserved and a
    :class:`KeboolaApiError` is raised carrying ``details.cascade =
    {attempted, deleted, failures: [{type, id, name, error}],
    parent_deleted: False, model_uuid}``.

    On success returns the standard envelope with ``cascade.parent_deleted
    = True``. The legacy ``orphaned_children`` top-level key aliases
    ``cascade.deleted`` for back-compat and is deprecated; removal
    scheduled for v0.42.0.
    """
    deleted_counts: dict[str, int] = {plural: 0 for plural, _ in PUSH_ORDER}
    failures: list[dict[str, str]] = []

    for plural, type_slug in reversed(PUSH_ORDER):
        for item in children.get(type_slug, []) or []:
            child_id = str(item.get("id", "") or "")
            if not child_id:
                continue
            attrs = item.get("attributes") or {}
            child_name = attrs.get("name") or attrs.get("term") or ""
            try:
                client.delete_item(type_slug, child_id)
                deleted_counts[plural] += 1
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                # Broad catch mirrors the rollback semantics in
                # `_semantic_layer_internals.push_built_model` (the
                # prior-art rationale lives there): a non-API exception
                # (httpx transport error, etc.) must not abort the
                # cascade or mask sibling failures. Collect, log at
                # warning level, continue.
                err = (
                    exc.message
                    if isinstance(exc, KeboolaApiError)
                    else str(exc) or type(exc).__name__
                )
                failures.append(
                    {
                        "type": type_slug,
                        "id": child_id,
                        "name": child_name,
                        "error": err,
                    }
                )
                logger.warning(
                    "Cascade DELETE failed for %s id=%s name=%s: %s",
                    type_slug,
                    child_id,
                    child_name,
                    err,
                )

    if failures:
        # Skip the parent: a "no parent + some children" partial state
        # would still hit the exact 422 collision this fix is closing.
        # Surface the failures so the user can re-run.
        raise KeboolaApiError(
            message=(
                f"Cascade-delete for model "
                f"{model_attrs.get('name', '') or model_uuid!r} ({model_uuid}) "
                f"failed for {len(failures)} child(ren); parent preserved. "
                f"Re-run `kbagent semantic-layer model delete "
                f"--project {alias} --model {model_uuid}` "
                f"after resolving the underlying errors."
            ),
            error_code=ErrorCode.INTERNAL_ERROR,
            status_code=500,
            details={
                "cascade": {
                    "attempted": True,
                    "deleted": deleted_counts,
                    "failures": failures,
                    "parent_deleted": False,
                    "model_uuid": model_uuid,
                }
            },
        )

    client.delete_item("semantic-model", model_uuid)

    return {
        "project": alias,
        "deleted": {"id": model_uuid, "name": model_attrs.get("name", "")},
        "cascade": {
            "attempted": True,
            "deleted": deleted_counts,
            "failures": [],
            "parent_deleted": True,
        },
        # Back-compat: the key existed before #306 was fixed as a count
        # of *leaked* children. After the fix it counts *cascaded*
        # children. Same shape, opposite meaning — JSON consumers see
        # zeros instead of leaks on the happy path, which is the
        # behavior they always wanted.
        # DEPRECATED: scheduled for removal in v0.42.0; new callers
        # should read `cascade.deleted` (plus `cascade.attempted` /
        # `cascade.parent_deleted` / `cascade.failures` for the
        # partial-failure path). See changelog 0.41.11 + gotchas.md.
        "orphaned_children": deleted_counts,
    }
