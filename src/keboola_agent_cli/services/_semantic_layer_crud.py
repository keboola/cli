"""CRUD helpers for :mod:`semantic_layer_service`.

Split out so the orchestrator class stays under the CONTRIBUTING.md
services hard ceiling (1,500 LOC). Each helper here operates on an
externally-provided :class:`MetastoreClient`; the class methods in the
main module are thin orchestrators that resolve credentials + the model
UUID, then delegate.

Edits are a metastore ``PATCH`` that carries only the changed keys (issue #833):
the server merges them into the stored record one top-level key at a time, under
a row lock, so a concurrent edit of another key is never overwritten -- the
whole-record ``PUT`` used before lost it. The item keeps its ID, scope, grants,
pending elevation request and revision history. The server validates each patched
key against the schema, and inside the same transaction the merged record's
references and the schema rules that combine several fields (go-monorepo
AJDA-3460, 2026-10-16). It does NOT re-check ``required`` or
``additionalProperties`` on the merged record; that is safe because a PATCH from
here only sets keys and never removes one (no ``null`` values). An edit whose every
given value equals the value just read sends nothing and reports
``status: "unchanged"`` (spec R6).
``import --overwrite`` and ``promote`` keep the ``PUT``: they must make the
target equal to the source, and a PATCH would keep target-only keys.

Helpers:

- :func:`edit_metric_with_cascade` -- metric rename + constraint cascade
- :func:`scan_orphan_constraints` -- pre-deletion orphan scan for metric
- :data:`REMOVE_KINDS` -- accepted kinds for ``remove_item`` /
  ``preview_remove``
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..errors import ErrorCode, KeboolaApiError
from .base import item_failure

if TYPE_CHECKING:
    from ..metastore_client import MetastoreClient, SemanticType


# Kinds accepted by `remove_item` / `preview_remove`. Relationship and
# glossary were added in iter-4 (NB-5) -- neither is referenced by
# other entities, so removal cannot orphan anything.
REMOVE_KINDS: tuple[str, ...] = (
    "metric",
    "dataset",
    "constraint",
    "relationship",
    "glossary",
)


@dataclass(frozen=True)
class PatchOutcome:
    """Result of :func:`_patch_changes`: the item after the edit and whether it was written."""

    item: dict[str, Any]
    changed: bool


def code_metric(name: str) -> str:
    """Derive the CODE_METRIC token from a metric name.

    Used in the rename-cascade prompt so the operator can audit
    downstream SQL joins that key on the literal CODE_METRIC value.
    """
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def edit_metric_with_cascade(
    client: MetastoreClient,
    *,
    model_uuid: str,
    current_name: str,
    new_name: str | None,
    new_sql: str | None,
    new_dataset: str | None,
    new_description: str | None,
    assume_yes: bool,
    is_tty: bool,
    confirm_cb: Any,
) -> dict[str, Any]:
    """Body of :meth:`SemanticLayerService.edit_metric`.

    Resolves the target metric, computes the constraint cascade list,
    enforces TTY/--yes guards, then PATCHes the changed keys of the metric
    and the ``metrics`` key of each cascaded constraint individually so
    per-item failures don't poison the rest.
    """
    metrics = client.list_items("semantic-metric", model_uuid)
    target = next(
        (m for m in metrics if (m.get("attributes") or {}).get("name") == current_name),
        None,
    )
    if target is None:
        raise KeboolaApiError(
            message=f"Metric '{current_name}' not found in this model.",
            error_code=ErrorCode.NOT_FOUND,
        )
    effective_new_name = new_name if new_name is not None else current_name
    cascade_required: list[dict[str, Any]] = []
    if new_name is not None and new_name != current_name:
        constraints = client.list_items("semantic-constraint", model_uuid)
        for c in constraints:
            cattrs = c.get("attributes") or {}
            if current_name in (cattrs.get("metrics") or []):
                cascade_required.append(c)

        if cascade_required and not assume_yes:
            names = ", ".join(
                (c.get("attributes") or {}).get("name", "?") for c in cascade_required
            )
            old_code = code_metric(current_name)
            new_code = code_metric(effective_new_name)
            msg = (
                f"Renaming metric {current_name!r} to {effective_new_name!r} will "
                f"cascade to {len(cascade_required)} constraint(s): {names}. "
                f"WARNING: downstream SQL joining on CODE_METRIC must be "
                f"updated manually ({old_code!r} -> {new_code!r}). Proceed?"
            )
            if not is_tty:
                raise KeboolaApiError(
                    message=msg + " Pass --yes to bypass.",
                    error_code=ErrorCode.VALIDATION_ERROR,
                )
            if confirm_cb is None or not confirm_cb(msg):
                raise KeboolaApiError(
                    message="Aborted by user.",
                    error_code=ErrorCode.VALIDATION_ERROR,
                )

    outcome = _patch_changes(
        client,
        "semantic-metric",
        target,
        id_key="name",
        changes={
            "name": new_name,
            "sql": new_sql,
            "dataset": new_dataset,
            "description": new_description,
        },
    )

    # Cascade constraints individually (each is independent --
    # report per-constraint success/failure). Only `metrics` is sent, so a
    # concurrent edit of the constraint's other keys survives.
    cascaded: list[dict[str, Any]] = []
    for c in cascade_required:
        cattrs = c.get("attributes") or {}
        cmetrics = [
            effective_new_name if x == current_name else x for x in cattrs.get("metrics") or []
        ]
        cname = cattrs.get("name", "")
        try:
            cascaded_item = client.patch_item(
                "semantic-constraint", c["id"], data={"metrics": cmetrics}
            )
            cascaded.append({"constraint": cname, "status": "updated", "id": cascaded_item["id"]})
        except KeboolaApiError as exc:
            cascaded.append(item_failure(exc, constraint=cname, status="failed"))

    failed_cascades = [entry for entry in cascaded if entry["status"] == "failed"]
    partial_state = bool(failed_cascades)
    return {
        "status": "updated" if outcome.changed or cascaded else "unchanged",
        "updated": outcome.item,
        "cascaded_constraints": cascaded,
        "rollback": None,
        "partial_state": partial_state,
        "recovery_hint": (
            (
                f"{len(failed_cascades)} cascade constraint(s) failed to repoint "
                f"to '{effective_new_name}'. Run "
                "`kbagent semantic-layer validate` to surface the dangling "
                "references, then re-run each failed cascade via "
                "`kbagent semantic-layer edit constraint --new-metric ...`."
            )
            if partial_state
            else None
        ),
    }


def validate_constraint_attrs(
    *,
    name_re: re.Pattern[str],
    constraint_types: tuple[str, ...],
    severities: tuple[str, ...],
    name: str | None = None,
    constraint_type: str | None = None,
    severity: str | None = None,
) -> None:
    """Validate constraint attributes locally before POST/edit.

    Each arg is optional -- the helper only checks the non-None ones.
    Used by ``add_constraint`` and the entry path of ``edit_constraint``.
    """
    if name is not None and not name_re.match(name):
        raise KeboolaApiError(
            message=(
                f"Constraint name {name!r} does not match the server-enforced "
                f"regex {name_re.pattern} "
                "(lowercase ASCII letter, then letters/digits/underscores). "
                "Example: 'npm_critical'."
            ),
            error_code=ErrorCode.VALIDATION_ERROR,
        )
    if constraint_type is not None and constraint_type not in constraint_types:
        raise KeboolaApiError(
            message=(
                f"--constraint-type must be one of {list(constraint_types)}, "
                f"got {constraint_type!r}."
            ),
            error_code=ErrorCode.VALIDATION_ERROR,
        )
    if severity is not None and severity not in severities:
        raise KeboolaApiError(
            message=(f"--severity must be one of {list(severities)}, got {severity!r}."),
            error_code=ErrorCode.VALIDATION_ERROR,
        )


def find_target_for_remove(
    client: MetastoreClient,
    *,
    kind: str,
    model_uuid: str,
    name: str,
    type_alias: dict[str, Any],
) -> tuple[dict[str, Any], SemanticType, str]:
    """Resolve the target item for ``preview_remove`` / ``remove_item``.

    Returns ``(target_item, type_slug, id_key)``. Raises NOT_FOUND if the
    target is missing or VALIDATION_ERROR if ``kind`` is unknown.
    """
    if kind not in REMOVE_KINDS:
        raise KeboolaApiError(
            message=f"remove kind must be one of {'|'.join(REMOVE_KINDS)}, got {kind!r}.",
            error_code=ErrorCode.VALIDATION_ERROR,
        )
    type_slug = type_alias[kind]
    id_key = "term" if kind == "glossary" else "name"
    items = client.list_items(type_slug, model_uuid)
    target = next(
        (i for i in items if (i.get("attributes") or {}).get(id_key) == name),
        None,
    )
    if target is None:
        raise KeboolaApiError(
            message=f"{kind} '{name}' not found in this model.",
            error_code=ErrorCode.NOT_FOUND,
        )
    return target, type_slug, id_key


def edit_simple(
    client: MetastoreClient,
    item_type: SemanticType,
    *,
    items: list[dict[str, Any]],
    id_key: str,
    current_key: str,
    overrides: dict[str, Any],
    not_found_label: str,
) -> dict[str, Any]:
    """Generic body for edit_dataset / edit_constraint / edit_relationship /
    edit_glossary (no cascade).

    Args:
        items: Pre-fetched list of items of ``item_type`` in the model.
        id_key: Identity key (``name`` for most types, ``term`` for glossary).
        current_key: Current identity value.
        overrides: Mapping of attribute keys -> new values to apply
            (only non-None entries should be present). The new effective
            identity key is computed from the overrides.
        not_found_label: Human-readable label for the NOT_FOUND error.

    Returns the same envelope shape as ``edit_metric_with_cascade``:
    ``{status, updated, cascaded_constraints: [], rollback, partial_state: False,
    recovery_hint: None}``. No-cascade variants never enter a partial
    state, but carry the keys for envelope uniformity.
    """
    target = next(
        (i for i in items if (i.get("attributes") or {}).get(id_key) == current_key),
        None,
    )
    if target is None:
        raise KeboolaApiError(
            message=f"{not_found_label} '{current_key}' not found in this model.",
            error_code=ErrorCode.NOT_FOUND,
        )
    outcome = _patch_changes(client, item_type, target, id_key=id_key, changes=overrides)
    return {
        "status": "updated" if outcome.changed else "unchanged",
        "updated": outcome.item,
        "cascaded_constraints": [],
        "rollback": None,
        "partial_state": False,
        "recovery_hint": None,
    }


def _patch_changes(
    client: MetastoreClient,
    item_type: SemanticType,
    target: dict[str, Any],
    *,
    id_key: str,
    changes: dict[str, Any],
) -> PatchOutcome:
    """PATCH every given key unless all equal the read values.

    ``None`` means "not given". When every given value equals the value just
    read, nothing is written (no new revision): the edit is then ordered at the
    moment of the read, and the item as read is returned with ``changed`` False.
    Otherwise ALL given keys are sent, also the equal ones: dropping only those
    would let a concurrent edit win one key and lose another, a state no
    serial order of the two edits produces. The identity key (``name``, or
    ``term`` for glossary) is sent together with the envelope ``name`` so the
    two never disagree.
    """
    attrs = target.get("attributes") or {}
    data = {k: v for k, v in changes.items() if v is not None}
    if all(attrs.get(k) == v for k, v in data.items()):
        return PatchOutcome(item=target, changed=False)
    patched = client.patch_item(item_type, target["id"], name=data.get(id_key), data=data)
    return PatchOutcome(item=patched, changed=True)


def scan_orphan_constraints(
    client: MetastoreClient,
    *,
    model_uuid: str,
    metric_name: str,
) -> list[dict[str, Any]]:
    """Find every constraint whose ``metrics[]`` references ``metric_name``.

    Returns a list of ``{name, metrics}`` dicts -- one per
    soon-to-be-orphaned constraint. Empty when the metric is unreferenced.
    """
    constraints = client.list_items("semantic-constraint", model_uuid)
    orphans: list[dict[str, Any]] = []
    for c in constraints:
        cattrs = c.get("attributes") or {}
        if metric_name in (cattrs.get("metrics") or []):
            orphans.append(
                {
                    "name": cattrs.get("name", ""),
                    "metrics": list(cattrs.get("metrics") or []),
                }
            )
    return orphans
