"""Internal helpers for :mod:`semantic_layer_service`.

Split out so the orchestrator class stays under the
CONTRIBUTING.md services hard ceiling (1,500 LOC). Each helper here is
pure-functional or operates on an externally-provided
:class:`MetastoreClient` -- there is no in-module state.

Helpers grouped by feature:

- Validation (basic + deep)        :func:`validate_basic`,
                                   :func:`validate_deep`
- Diff                             :func:`collect_side_from_file`,
                                   :func:`diff_one_type`,
                                   :func:`compare_attrs`
- Build (heuristic)                :func:`heuristic_generate_model`,
                                   :func:`fetch_table_schemas`
- Import / promote                 :func:`run_import_loop`,
                                   :func:`run_promote_loop`
- Export I/O                       :func:`write_snapshot_to_file`,
                                   :func:`default_export_path`,
                                   :func:`build_export_snapshot`
- Constants reused across modules  :data:`DIFF_IGNORED_KEYS`,
                                   :data:`PUSH_ORDER`
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import ConfigError, ErrorCode, KeboolaApiError

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..metastore_client import SemanticType
    from .storage_service import StorageService

# Re-exported from the main module to avoid a circular import; pulled in
# locally for type hints / runtime constants.

# Column reference regex (Snowflake-style ``"SCHEMA"."COLUMN"``).
_COLUMN_REF_RE = re.compile(r'"[^"]+"\."([^"]+)"')

# SUM(... PCT ...) heuristic.
_SUM_ON_PCT_RE = re.compile(r"\bSUM\s*\(\s*[^)]*\b(PCT|PERCENT|RATE)\b", re.IGNORECASE)

# AGG-on-STRING detection for --deep validate.
_AGG_ON_STRING_FUNCS = ("SUM", "AVG")

# Severity-band suffixes recognised by downstream pipelines.
_SEVERITY_SUFFIXES = ("_critical", "_warning", "_healthy", "_review")

# Keys ignored when deep-equality-comparing two items (the modelUUID is
# rewritten when promoting across projects, and the server-side
# timestamps drift every fetch).
DIFF_IGNORED_KEYS: tuple[str, ...] = (
    "modelUUID",
    "createdAt",
    "lastUpdated",
    "revision",
)

# Push order for both `import` and `promote` -- datasets before metrics
# (metric.dataset references a tableId), constraints last (their
# `metrics[]` list references metric names).
PUSH_ORDER: tuple[tuple[str, SemanticType], ...] = (
    ("datasets", "semantic-dataset"),
    ("metrics", "semantic-metric"),
    ("relationships", "semantic-relationship"),
    ("glossary", "semantic-glossary"),
    ("constraints", "semantic-constraint"),
)


@dataclass(frozen=True)
class WorkerResult:
    """Outcome of a per-table parallel fetch worker.

    Shared between the deep-validation pass and the build heuristic.
    Exactly one of ``detail`` / ``error`` is non-None.
    """

    table_id: str
    detail: dict[str, Any] | None
    error: str | None


# ── validate (basic + deep) ─────────────────────────────────────────


def validate_basic(
    *,
    datasets: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    constraints: list[dict[str, Any]],
    glossary: list[dict[str, Any]],
    errors: list[dict[str, str]],
    warnings: list[dict[str, str]],
) -> None:
    """Pure in-memory validation (no API calls).

    Mutates ``errors`` and ``warnings`` in place. See the orchestrator's
    docstring for the full list of checks.
    """
    # DUPLICATES per type (name)
    for label, items, key in (
        ("dataset", datasets, "name"),
        ("metric", metrics, "name"),
        ("relationship", relationships, "name"),
        ("constraint", constraints, "name"),
        ("glossary", glossary, "term"),
    ):
        seen: set[str] = set()
        dups: set[str] = set()
        for it in items:
            name = it.get(key, "")
            if not name:
                continue
            if name in seen:
                dups.add(name)
            seen.add(name)
        for dup in sorted(dups):
            errors.append(
                {"type": "DUPLICATE", "item": f"{label}:{dup}", "detail": f"duplicate {key}"}
            )

    dataset_tids = {d.get("tableId", "") for d in datasets if d.get("tableId")}
    dataset_names = {d.get("name", "") for d in datasets if d.get("name")}

    # DANGLING REL: from/to tableId must be in datasets
    for rel in relationships:
        for endpoint in ("from", "to"):
            tid = rel.get(endpoint, "")
            if tid and tid not in dataset_tids and tid not in dataset_names:
                errors.append(
                    {
                        "type": "DANGLING_RELATIONSHIP",
                        "item": rel.get("name", "?"),
                        "detail": f"{endpoint}={tid!r} not in model datasets",
                    }
                )

    # DANGLING METRIC: metric.dataset is a tableId
    for m in metrics:
        ds = m.get("dataset", "")
        if ds and ds not in dataset_tids and ds not in dataset_names:
            errors.append(
                {
                    "type": "DANGLING_METRIC",
                    "item": m.get("name", "?"),
                    "detail": f"dataset={ds!r} not in model datasets",
                }
            )

    # SUM ON PCT
    for m in metrics:
        sql = m.get("sql", "") or ""
        if _SUM_ON_PCT_RE.search(sql):
            warnings.append(
                {
                    "type": "SUM_ON_PCT",
                    "item": m.get("name", "?"),
                    "detail": "SUM() over a percentage/rate column is usually incorrect",
                }
            )

    # CONSTRAINT ORPHAN + SEVERITY SUFFIX
    metric_names = {m.get("name", "") for m in metrics if m.get("name")}
    for c in constraints:
        cname = c.get("name", "")
        for mname in c.get("metrics", []) or []:
            if mname not in metric_names:
                errors.append(
                    {
                        "type": "CONSTRAINT_ORPHAN",
                        "item": cname or "?",
                        "detail": f"references missing metric {mname!r}",
                    }
                )
        if cname and not any(cname.endswith(suf) for suf in _SEVERITY_SUFFIXES):
            warnings.append(
                {
                    "type": "SEVERITY_SUFFIX",
                    "item": cname,
                    "detail": (
                        "constraint name should end with one of "
                        "_critical/_warning/_healthy/_review for downstream parsing"
                    ),
                }
            )


def validate_deep(
    *,
    alias: str,
    storage: StorageService,
    datasets: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    errors: list[dict[str, str]],
    warnings: list[dict[str, str]],
) -> None:
    """Add deep checks that require a Snowflake schema fetch per dataset.

    Mutates ``errors`` and ``warnings`` in place.
    """

    def _worker(ds: dict[str, Any]) -> WorkerResult:
        tid = ds.get("tableId", "")
        if not tid:
            return WorkerResult(table_id=tid, detail=None, error="missing tableId")
        try:
            detail = storage.get_table_detail(alias, tid)
            return WorkerResult(table_id=tid, detail=detail, error=None)
        except (KeboolaApiError, ConfigError) as exc:
            return WorkerResult(table_id=tid, detail=None, error=str(exc))

    details_by_tid: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for outcome in pool.map(_worker, datasets):
            if outcome.error is not None:
                warnings.append(
                    {
                        "type": "DEEP_FETCH_FAILED",
                        "item": outcome.table_id or "?",
                        "detail": outcome.error,
                    }
                )
                continue
            if outcome.detail is not None:
                details_by_tid[outcome.table_id] = outcome.detail

    # PHANTOM FIELD (declared field not in actual columns)
    for ds in datasets:
        tid = ds.get("tableId", "")
        detail = details_by_tid.get(tid)
        if detail is None:
            continue
        actual_columns = set(detail.get("columns", []))
        declared = ds.get("fields", []) or []
        for field in declared:
            fname = field.get("name", "") if isinstance(field, dict) else str(field)
            if fname and fname not in actual_columns:
                errors.append(
                    {
                        "type": "PHANTOM_FIELD",
                        "item": f"{ds.get('name', '?')}.{fname}",
                        "detail": f"field {fname!r} not in storage table columns",
                    }
                )

    # METRIC PHANTOM + AGG ON STRING -- build a quick lookup: tableId -> {col_name: basetype}
    col_types_by_tid: dict[str, dict[str, str]] = {}
    for tid, detail in details_by_tid.items():
        cols: dict[str, str] = {}
        for col_info in detail.get("column_details", []) or []:
            cols[col_info.get("name", "")] = (col_info.get("type", "") or "").upper()
        col_types_by_tid[tid] = cols

    for m in metrics:
        sql = m.get("sql", "") or ""
        mdataset = m.get("dataset", "")
        col_types = col_types_by_tid.get(mdataset, {})
        refs = set(_COLUMN_REF_RE.findall(sql))
        if col_types:
            actual = set(col_types.keys())
            for ref in refs:
                if ref not in actual:
                    errors.append(
                        {
                            "type": "METRIC_PHANTOM",
                            "item": m.get("name", "?"),
                            "detail": (
                                f"column {ref!r} referenced in sql not in dataset {mdataset!r}"
                            ),
                        }
                    )
        # AGG ON STRING
        for func in _AGG_ON_STRING_FUNCS:
            pattern = re.compile(
                rf'\b{func}\s*\(\s*[^)]*?"([^"]+)"\."([^"]+)"',
                re.IGNORECASE,
            )
            for match in pattern.finditer(sql):
                col = match.group(2)
                col_type = col_types.get(col, "").upper()
                if col_type == "STRING":
                    errors.append(
                        {
                            "type": "AGG_ON_STRING",
                            "item": m.get("name", "?"),
                            "detail": f"{func}() on STRING column {col!r}",
                        }
                    )


# ── diff ────────────────────────────────────────────────────────────


def compare_attrs(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """Return the sorted list of attribute keys that differ between two items."""
    ignored = set(DIFF_IGNORED_KEYS)
    keys = (set(a) | set(b)) - ignored - {"id"}
    diff_keys: list[str] = []
    for key in sorted(keys):
        if a.get(key) != b.get(key):
            diff_keys.append(key)
    return diff_keys


def diff_one_type(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
    *,
    id_key: str,
) -> dict[str, Any]:
    """Compute added/removed/changed for one entity type."""
    left_by_key = {item.get(id_key, ""): item for item in left if item.get(id_key)}
    right_by_key = {item.get(id_key, ""): item for item in right if item.get(id_key)}

    added = sorted(k for k in right_by_key if k not in left_by_key)
    removed = sorted(k for k in left_by_key if k not in right_by_key)
    changed: list[dict[str, Any]] = []
    for key in sorted(set(left_by_key) & set(right_by_key)):
        diff_keys = compare_attrs(left_by_key[key], right_by_key[key])
        if diff_keys:
            changed.append({id_key: key, "diff_keys": diff_keys})
    return {"added": added, "removed": removed, "changed": changed}


def collect_side_from_file(file: Path) -> dict[str, Any]:
    """Resolve one diff side from a snapshot file.

    Returns the shape consumed by the diff orchestrator:
    ``{"ref": {"source": "file", "ref": <path>, "model": {...}},
       "data": {<plural>: [attrs_dict, ...]}}``.
    """
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise KeboolaApiError(
            message=f"Cannot read --file {file}: {exc}",
            error_code=ErrorCode.READ_ERROR,
        ) from exc
    except json.JSONDecodeError as exc:
        raise KeboolaApiError(
            message=f"File {file} is not valid JSON: {exc}",
            error_code=ErrorCode.INVALID_FORMAT,
        ) from exc

    def _attrs(items: list[Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in items or []:
            if isinstance(item, dict) and "attributes" in item:
                merged = dict(item.get("attributes") or {})
                merged["id"] = item.get("id", "")
                out.append(merged)
            elif isinstance(item, dict):
                out.append(item)
        return out

    return {
        "ref": {
            "source": "file",
            "ref": str(file),
            "model": payload.get("model", {}),
        },
        "data": {
            "datasets": _attrs(payload.get("datasets", [])),
            "metrics": _attrs(payload.get("metrics", [])),
            "relationships": _attrs(payload.get("relationships", [])),
            "constraints": _attrs(payload.get("constraints", [])),
            "glossary": _attrs(payload.get("glossary", [])),
        },
    }


# ── build (heuristic generator) ─────────────────────────────────────


def run_import_loop(
    client: Any,
    *,
    snapshot: dict[str, Any],
    target_model_uuid: str,
    existing_by_type: dict[Any, list[dict[str, Any]]],
    type_filter: set[str] | None,
    dry_run: bool,
    overwrite: bool,
) -> dict[str, Any]:
    """Replay a snapshot into the target model.

    Returns the per-type stats dict matching the orchestrator's contract.
    Push order is :data:`PUSH_ORDER`. Errors are accumulated per item;
    one failure does not abort the rest.
    """
    imported: dict[str, Any] = {}
    for plural, type_slug in PUSH_ORDER:
        if type_filter is not None and plural not in type_filter:
            continue
        source_items = snapshot.get(plural, []) or []
        id_key = "term" if plural == "glossary" else "name"
        existing_by_name: dict[str, dict[str, Any]] = {
            (item.get("attributes") or {}).get(id_key, ""): item
            for item in existing_by_type.get(type_slug, [])
            if (item.get("attributes") or {}).get(id_key)
        }

        per_type: dict[str, Any] = {
            "created": 0,
            "skipped": 0,
            "overwritten": 0,
            "failed": [],
        }
        for raw_item in source_items:
            if not isinstance(raw_item, dict):
                continue
            attrs = dict(raw_item.get("attributes") or {})
            attrs["modelUUID"] = target_model_uuid  # rewrite
            key = attrs.get(id_key, "")
            if not key:
                per_type["failed"].append({"name": "?", "reason": f"missing {id_key} on item"})
                continue

            if key in existing_by_name:
                if not overwrite:
                    per_type["skipped"] += 1
                    continue
                if dry_run:
                    per_type["overwritten"] += 1
                    continue
                try:
                    client.delete_item(type_slug, existing_by_name[key]["id"])
                    client.post_item(type_slug, name=key, data=attrs)
                    per_type["overwritten"] += 1
                except KeboolaApiError as exc:
                    per_type["failed"].append({"name": key, "reason": exc.message})
                continue

            if dry_run:
                per_type["created"] += 1
                continue
            try:
                client.post_item(type_slug, name=key, data=attrs)
                per_type["created"] += 1
            except KeboolaApiError as exc:
                per_type["failed"].append({"name": key, "reason": exc.message})

        imported[plural] = per_type
    return imported


def run_promote_loop(
    target_client: Any,
    *,
    src_children: dict[Any, list[dict[str, Any]]],
    tgt_children: dict[Any, list[dict[str, Any]]],
    target_model_uuid: str,
    type_filter: set[str] | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Run the additive + overwrite promote loop.

    NEW: POST to target. CHANGED: DELETE+POST. IDENTICAL: skip.
    Items only in target are never deleted (additive only).
    Returns ``{plural: {new, overwritten, identical, failed, changes}}``.
    """
    result: dict[str, Any] = {}
    for plural, type_slug in PUSH_ORDER:
        if type_filter is not None and plural not in type_filter:
            continue
        id_key = "term" if plural == "glossary" else "name"
        src_items = src_children.get(type_slug, []) or []
        tgt_by_key: dict[str, dict[str, Any]] = {
            (it.get("attributes") or {}).get(id_key, ""): it
            for it in tgt_children.get(type_slug, [])
            if (it.get("attributes") or {}).get(id_key)
        }

        stats: dict[str, Any] = {
            "new": 0,
            "overwritten": 0,
            "identical": 0,
            "failed": [],
            "changes": [],
        }

        for src in src_items:
            src_attrs = dict(src.get("attributes") or {})
            src_attrs["modelUUID"] = target_model_uuid  # rewrite
            key = src_attrs.get(id_key, "")
            if not key:
                stats["failed"].append({"name": "?", "reason": f"missing {id_key} on source item"})
                continue

            if key in tgt_by_key:
                diff_keys = compare_attrs(src_attrs, dict(tgt_by_key[key].get("attributes") or {}))
                if not diff_keys:
                    stats["identical"] += 1
                    continue
                if dry_run:
                    stats["overwritten"] += 1
                    stats["changes"].append({id_key: key, "diff_keys": diff_keys})
                    continue
                try:
                    target_client.delete_item(type_slug, tgt_by_key[key]["id"])
                    target_client.post_item(type_slug, name=key, data=src_attrs)
                    stats["overwritten"] += 1
                    stats["changes"].append({id_key: key, "diff_keys": diff_keys})
                except KeboolaApiError as exc:
                    stats["failed"].append({"name": key, "reason": exc.message})
                continue

            if dry_run:
                stats["new"] += 1
                continue
            try:
                target_client.post_item(type_slug, name=key, data=src_attrs)
                stats["new"] += 1
            except KeboolaApiError as exc:
                stats["failed"].append({"name": key, "reason": exc.message})

        result[plural] = stats
    return result


def push_built_model(
    client: Any,
    *,
    generated: dict[str, Any],
    model_name_or_uuid: str | None,
    resolve_model_fn: Callable[[Any, str], tuple[str, dict[str, Any]]],
    keep_on_failure: bool = False,
) -> tuple[dict[str, int], str, dict[str, Any] | None]:
    """Push a heuristic-generated model + children to the metastore.

    Returns ``(counts, model_uuid, model_item)`` where ``model_item``
    is the freshly-created model record (``None`` when updating an
    existing model). ``counts`` maps each plural to the number of
    successfully POSTed children.

    Rollback semantics (issue #295): on any POST failure during the
    children loop, every successfully-POSTed child is DELETEd in
    reverse order, then the model itself is DELETEd **only when this
    call created it** (i.e. ``model_name_or_uuid`` was None). The
    original exception is re-raised wrapped in a KeboolaApiError that
    carries the rollback summary in ``details``. Pass
    ``keep_on_failure=True`` to skip cleanup for forensic inspection.
    """
    if model_name_or_uuid is None:
        # Create the model.
        model_attrs: dict[str, Any] = {
            "name": generated["name"],
            "sql_dialect": generated.get("sql_dialect", "Snowflake"),
        }
        if generated.get("description"):
            model_attrs["description"] = generated["description"]
        model_item = client.post_item("semantic-model", name=generated["name"], data=model_attrs)
        model_uuid = model_item["id"]
        model_created_here = True
    else:
        model_uuid, _ = resolve_model_fn(client, model_name_or_uuid)
        model_item = None
        model_created_here = False

    posted_children: list[tuple[SemanticType, str, str]] = []
    counts: dict[str, int] = {plural: 0 for plural, _ in PUSH_ORDER}
    # Sentinel: the except clause references `plural` and `name` to tag the
    # failing row in the wrapped error message; pre-init so they're defined
    # even on the rare path where the loop never started a row (e.g. the
    # first POST raised before either was set inside the loop body).
    plural = ""
    name = ""
    try:
        for plural, type_slug in PUSH_ORDER:
            for item in generated.get(plural, []) or []:
                # Resolve `name` BEFORE `dict(item)` so an exception during
                # the dict-copy (or in the POST itself) tags the wrapped
                # error with the current row, not the previous one.
                name = (
                    (item.get("name") if isinstance(item, dict) else "")
                    or (item.get("term") if isinstance(item, dict) else "")
                    or ""
                )
                if not name:
                    continue
                attrs = dict(item)
                attrs["modelUUID"] = model_uuid
                posted = client.post_item(type_slug, name=name, data=attrs)
                posted_id = str(posted.get("id", "") or "") if isinstance(posted, dict) else ""
                if not posted_id:
                    # Defensive: the metastore always returns `id` in a
                    # dict body, but a proxy / middleware that mangles
                    # the response would otherwise leave this child
                    # untrackable for rollback. Skip the counts increment
                    # so the visible state matches what we can actually
                    # clean up.
                    logger.warning(
                        "POST %s name=%s returned no id (response type=%s); "
                        "skipping rollback tracking",
                        type_slug,
                        name,
                        type(posted).__name__,
                    )
                    continue
                posted_children.append((type_slug, posted_id, name))
                counts[plural] += 1
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        # Catch broadly: client.post_item can raise httpx.RequestError
        # subclasses (ReadError, ProtocolError, ...) that are NOT
        # KeboolaApiError; without the rollback path on those, the partial
        # state this PR exists to clean up would still leak. Re-raise of
        # KeyboardInterrupt / SystemExit above keeps Ctrl-C honest.
        api_exc = exc if isinstance(exc, KeboolaApiError) else None
        error_code = api_exc.error_code if api_exc else ErrorCode.INTERNAL_ERROR
        status_code = api_exc.status_code if api_exc else 500
        exc_message = api_exc.message if api_exc else str(exc) or type(exc).__name__

        if keep_on_failure:
            logger.info(
                "build_model push failed for %s/%s; preserving %d posted children + model %s "
                "(--keep-on-failure set)",
                plural,
                name,
                len(posted_children),
                model_uuid,
            )
            raise KeboolaApiError(
                message=(
                    f"build_model push failed at {plural}/{name!r}: {exc_message}. "
                    f"Partial state preserved (--keep-on-failure): {len(posted_children)} "
                    f"children posted, model {model_uuid} kept."
                ),
                error_code=error_code,
                status_code=status_code,
                details={
                    "rollback": {
                        "attempted": False,
                        "reason": "keep_on_failure",
                        "posted_children": len(posted_children),
                        "model_created_here": model_created_here,
                        "model_uuid": model_uuid,
                    }
                },
            ) from exc
        deleted = 0
        failed_deletes: list[dict[str, str]] = []
        for type_slug, child_id, child_name in reversed(posted_children):
            try:
                client.delete_item(type_slug, child_id)
                deleted += 1
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as del_exc:
                # Same rationale as the outer broad catch: a non-API
                # exception from delete_item must not abort the cleanup or
                # mask the original POST failure. logger.warning (no
                # traceback) keeps log volume bounded when the metastore
                # is fully down -- 200+ ERROR-level stack traces would
                # otherwise drown out the original POST failure. The
                # consolidated summary line below carries the full count.
                failed_deletes.append(
                    {
                        "type": type_slug,
                        "id": child_id,
                        "name": child_name,
                        "error": (
                            del_exc.message
                            if isinstance(del_exc, KeboolaApiError)
                            else str(del_exc) or type(del_exc).__name__
                        ),
                    }
                )
                logger.warning(
                    "Rollback DELETE failed for %s id=%s name=%s: %s",
                    type_slug,
                    child_id,
                    child_name,
                    failed_deletes[-1]["error"],
                )
        model_deleted = False
        model_delete_error: str | None = None
        if model_created_here:
            try:
                client.delete_item("semantic-model", model_uuid)
                model_deleted = True
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as del_exc:
                model_delete_error = (
                    del_exc.message
                    if isinstance(del_exc, KeboolaApiError)
                    else str(del_exc) or type(del_exc).__name__
                )
                # logger.exception here (with traceback) because there's
                # only ever ONE model-delete; the volume concern that
                # applies to the per-child loop does not.
                logger.exception(
                    "Rollback DELETE of created model %s failed during build_model cleanup",
                    model_uuid,
                )
        if failed_deletes:
            logger.error(
                "build_model rollback summary: %d/%d child DELETEs failed during cleanup",
                len(failed_deletes),
                len(posted_children),
            )
        raise KeboolaApiError(
            message=(
                f"build_model push failed at {plural}/{name!r}: {exc_message}. "
                f"Rollback deleted {deleted}/{len(posted_children)} child(ren)"
                + (
                    f" and the model ({model_uuid})."
                    if model_deleted
                    else (
                        f"; model ({model_uuid}) delete failed: {model_delete_error}."
                        if model_created_here
                        else f"; existing model ({model_uuid}) preserved."
                    )
                )
            ),
            error_code=error_code,
            status_code=status_code,
            details={
                "rollback": {
                    "attempted": True,
                    "posted_children": len(posted_children),
                    "deleted": deleted,
                    "failed_deletes": failed_deletes,
                    "model_created_here": model_created_here,
                    "model_deleted": model_deleted,
                    "model_delete_error": model_delete_error,
                    "model_uuid": model_uuid,
                }
            },
        ) from exc
    return counts, model_uuid, model_item


def synthesize_role_classified_fields(
    storage: StorageService,
    alias: str,
    table_id: str,
    classify_role: Callable[[str, str], str],
) -> list[dict[str, Any]]:
    """Fetch storage schema for ``table_id`` and synthesise ``fields[]`` with role heuristics.

    Used by ``add_dataset --deep-fields``. Returns the list of
    ``{name, type, role}`` dicts (one per column); empty when the
    storage table has no columns.
    """
    detail = storage.get_table_detail(alias, table_id)
    fields: list[dict[str, Any]] = []
    for col in detail.get("column_details", []) or []:
        cname = col.get("name", "")
        basetype = col.get("type", "") or col.get("native_type", "")
        fields.append(
            {
                "name": cname,
                "type": basetype,
                "role": classify_role(cname, basetype),
            }
        )
    return fields


def fetch_table_schemas(
    storage: StorageService,
    alias: str,
    table_ids: list[str],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """Fetch storage schemas for a list of tableIds in parallel.

    Returns ``(schemas_by_tid, fetch_errors)`` where ``fetch_errors`` is
    a list of ``{"table_id", "error"}`` dicts -- one per missing /
    failed table. Errors are returned (not raised) so the build pass
    can decide whether to abort.
    """

    def _worker(tid: str) -> WorkerResult:
        try:
            detail = storage.get_table_detail(alias, tid)
            return WorkerResult(table_id=tid, detail=detail, error=None)
        except (KeboolaApiError, ConfigError) as exc:
            return WorkerResult(table_id=tid, detail=None, error=str(exc))

    schemas_by_tid: dict[str, dict[str, Any]] = {}
    fetch_errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for outcome in pool.map(_worker, table_ids):
            if outcome.error is not None:
                fetch_errors.append({"table_id": outcome.table_id, "error": outcome.error})
                continue
            if outcome.detail is not None:
                schemas_by_tid[outcome.table_id] = outcome.detail
    return schemas_by_tid, fetch_errors


def resolve_model_uuid(
    client: Any,
    model_name_or_uuid: str | None,
) -> tuple[str, dict[str, Any]]:
    """Resolve a model selector to ``(uuid, attributes_dict)``.

    See :meth:`SemanticLayerService._resolve_model` for the contract.
    Lives here so the orchestrator class stays under the file-size
    budget; the class method is a one-line delegate.

    Raises ``ConfigError`` when the selector is ambiguous or missing.
    """
    models = client.list_items("semantic-model")
    if not models:
        raise ConfigError(
            "Project has no semantic-layer models. Use "
            "'kbagent semantic-layer model create' to create one."
        )

    if model_name_or_uuid is None:
        if len(models) == 1:
            return models[0]["id"], dict(models[0].get("attributes") or {})
        names = ", ".join(sorted((m.get("attributes") or {}).get("name", "?") for m in models))
        raise ConfigError(f"Project has {len(models)} models — specify --model. Available: {names}")

    # Try UUID match first (exact ID match)
    for m in models:
        if m.get("id") == model_name_or_uuid:
            return m["id"], dict(m.get("attributes") or {})

    # Then name match
    name_matches = [
        m for m in models if (m.get("attributes") or {}).get("name") == model_name_or_uuid
    ]
    if len(name_matches) == 1:
        return name_matches[0]["id"], dict(name_matches[0].get("attributes") or {})
    if len(name_matches) > 1:
        raise ConfigError(
            f"Multiple models found with name '{model_name_or_uuid}'. Specify the UUID instead."
        )

    names = ", ".join(sorted((m.get("attributes") or {}).get("name", "?") for m in models))
    raise ConfigError(f"Model '{model_name_or_uuid}' not found. Available: {names}")


def unpack_attrs_with_id(
    items: list[dict[str, Any]], *, id_field: str = "_id"
) -> list[dict[str, Any]]:
    """Flatten ``{type, id, attributes}`` items to attributes dicts + injected id field.

    Used by validate_model and other callers that need attribute-only
    lists with the server id preserved (under ``_id`` by default so it
    doesn't collide with the attribute namespace).
    """
    return [dict(i.get("attributes") or {}, **{id_field: i.get("id", "")}) for i in items]


def unpack_children_by_plural(
    raw_by_type: dict[Any, list[dict[str, Any]]],
    *,
    id_field: str = "id",
) -> dict[str, list[dict[str, Any]]]:
    """Convert ``raw_by_type[semantic-X]`` lists to attribute dicts keyed by plural.

    Returns a dict with keys ``datasets``, ``metrics``, ``relationships``,
    ``constraints``, ``glossary`` -- ready to splat into a result envelope.
    """
    return {
        "datasets": unpack_attrs_with_id(
            raw_by_type.get("semantic-dataset", []), id_field=id_field
        ),
        "metrics": unpack_attrs_with_id(raw_by_type.get("semantic-metric", []), id_field=id_field),
        "relationships": unpack_attrs_with_id(
            raw_by_type.get("semantic-relationship", []), id_field=id_field
        ),
        "constraints": unpack_attrs_with_id(
            raw_by_type.get("semantic-constraint", []), id_field=id_field
        ),
        "glossary": unpack_attrs_with_id(
            raw_by_type.get("semantic-glossary", []), id_field=id_field
        ),
    }


def default_export_path(model_name: str) -> Path:
    """Default ``./sl_export_{model_name}_{YYYYMMDD_HHMMSS}.json`` path."""
    ts = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(model_name)) or "model"
    return Path.cwd() / f"sl_export_{safe_name}_{ts}.json"


def build_export_snapshot(
    *,
    alias: str,
    model_uuid: str,
    model_attrs: dict[str, Any],
    raw_by_type: dict[Any, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Assemble the self-describing export envelope."""
    exported_at = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "exported_at": exported_at,
        "project": alias,
        "model": dict(model_attrs, id=model_uuid),
        "datasets": raw_by_type.get("semantic-dataset", []),
        "metrics": raw_by_type.get("semantic-metric", []),
        "relationships": raw_by_type.get("semantic-relationship", []),
        "constraints": raw_by_type.get("semantic-constraint", []),
        "glossary": raw_by_type.get("semantic-glossary", []),
    }


def write_snapshot_to_file(snapshot: dict[str, Any], output_path: Path) -> None:
    """Atomic-write a JSON snapshot with 0o644 perms and O_NOFOLLOW.

    O_NOFOLLOW refuses to follow a pre-existing symlink at the chosen
    path so a malicious --output (or a planted symlink in CWD) cannot
    redirect the write to a sensitive file.
    """
    payload = json.dumps(snapshot, indent=2).encode("utf-8")
    fd = os.open(
        str(output_path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
        0o644,
    )
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


# Map warehouse-native column types onto the closed set the metastore
# accepts for `fields[*].type`. Anything not matched falls through to
# "string" — safest default for legacy untyped Storage tables.
_FIELD_TYPE_MAP: dict[str, str] = {
    # strings
    "varchar": "string",
    "char": "string",
    "string": "string",
    "text": "string",
    "nvarchar": "string",
    "nchar": "string",
    # integers
    "int": "integer",
    "integer": "integer",
    "bigint": "integer",
    "smallint": "integer",
    "tinyint": "integer",
    "int64": "integer",  # BigQuery
    # decimals
    "decimal": "decimal",
    "numeric": "decimal",
    "number": "decimal",
    "float": "decimal",
    "double": "decimal",
    "real": "decimal",
    "money": "decimal",
    "float64": "decimal",  # BigQuery
    "bignumeric": "decimal",  # BigQuery
    # booleans
    "boolean": "boolean",
    "bool": "boolean",
    "bit": "boolean",
    # date / datetime
    "date": "date",
    "datetime": "datetime",
    "datetime2": "datetime",
    "timestamp": "datetime",
    "timestamptz": "datetime",
    "timestamp_ntz": "datetime",
    "timestamp_ltz": "datetime",
    "timestamp_tz": "datetime",
    # json
    "json": "json",
    "jsonb": "json",
    "variant": "json",
    "object": "json",
    "array": "json",
}


def _normalize_field_type(basetype: str) -> str:
    """Coerce a warehouse-native type into the metastore's closed vocabulary."""
    if not basetype:
        return "string"
    # strip parens (e.g. VARCHAR(255), DECIMAL(38, 9))
    head = basetype.split("(", 1)[0].strip().lower()
    return _FIELD_TYPE_MAP.get(head, "string")


def heuristic_generate_model(
    *,
    schemas: dict[str, dict[str, Any]],
    model_name: str,
    derive_fqn: Callable[[str], str],
    classify_role: Callable[[str, str], str],
) -> dict[str, Any]:
    """Deterministic stand-in for the AI generator (see ``build_model``).

    Builds: one dataset per table (with classified fields[]), one
    COUNT(*) metric per dataset as a placeholder, no relationships
    (cross-table FKs are not inferrable from columns alone), an empty
    constraints list, and a glossary entry per dataset.

    Accepts the FQN-derivation and role-classification helpers as
    callables so the helper module stays free of import-time coupling
    to the main service module's regex constants.
    """
    datasets: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    glossary: list[dict[str, Any]] = []

    for tid, detail in schemas.items():
        ds_name = (
            (detail.get("display_name") or detail.get("name") or tid.split(".")[-1])
            .replace(" ", "_")
            .lower()
        )
        fields: list[dict[str, Any]] = []
        for col in detail.get("column_details", []) or []:
            cname = col.get("name", "")
            basetype = col.get("type", "") or col.get("native_type", "")
            # Metastore validates field types against a closed lowercase set:
            #   {string, integer, decimal, boolean, date, datetime, json}
            # Storage returns warehouse-native types (VARCHAR, INTEGER, ...)
            # or empty strings for untyped legacy tables. Map both to the
            # metastore vocabulary; fall back to "string" for unknown /
            # empty types so the builder doesn't 422 on legacy buckets.
            fields.append(
                {
                    "name": cname,
                    "type": _normalize_field_type(basetype),
                    "role": classify_role(cname, basetype),
                }
            )
        datasets.append(
            {
                "name": ds_name,
                "tableId": tid,
                "fqn": derive_fqn(tid),
                "fields": fields,
                "description": detail.get("description", "") or "",
            }
        )
        metrics.append(
            {
                "name": f"{ds_name}_row_count",
                "sql": f"COUNT(*) FROM {derive_fqn(tid)}",
                "dataset": tid,
                "description": f"Row count of {ds_name}.",
            }
        )
        glossary.append(
            {
                "term": ds_name,
                "definition": f"Table {tid}: {detail.get('description', '') or 'auto-generated'}.",
            }
        )

    return {
        "name": model_name,
        "description": (
            f"Heuristic-generated model from {len(schemas)} table(s). "
            "Iterate with `kbagent semantic-layer add/edit`."
        ),
        "sql_dialect": "Snowflake",
        "datasets": datasets,
        "metrics": metrics,
        "relationships": [],
        "constraints": [],
        "glossary": glossary,
    }
