"""Push-time link backfill for the sync service (Phase C + Phase D).

Extracted from ``sync_service.py`` to keep that file under control. These are
free functions that take the ``SyncService`` as their first argument (for the
handful of on-disk helpers they need -- ``_read_config_file``,
``_write_config_file``, ``_compute_config_hashes``) rather than methods, so the
typing stays explicit and the binding logic is testable in isolation.

- **Phase C** (:func:`resolve_transformation_bindings`): rebind a
  transformation's ``variables_id`` / ``variables_values_id`` placeholders and
  its ``shared_code_id`` / ``shared_code_row_ids`` (plus the ``{{<row id>}}``
  script placeholders) to the ULIDs created this push.
- **Phase D** (:func:`resolve_run_target_bindings`): remap ``keboola.flow`` and
  legacy ``keboola.orchestrator`` task ``configId``s and the
  ``keboola.scheduler`` target to the ULIDs created this push.

Both run after the create passes, PUT the corrected config, rewrite the local
``_config.yml``, and refresh the manifest hashes so a re-push is clean.
"""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import ErrorCode, KeboolaApiError
from ..sync.branch_scope import find_branch_entry
from ..sync.code_extraction import merge_code_files
from ..sync.config_format import api_config_to_local, local_config_to_api
from ..sync.diff_engine import config_hash
from ..sync.manifest import Manifest
from ._sync_baseline import apply_stamp, config_baseline
from ._sync_companions import record_companions
from ._sync_link_pending import (
    LinkMaps,
    build_link_maps,
    link_pending_entries,
    read_target,
    set_link_pending,
    validate_targets,
)
from ._sync_models import (
    AMBIGUOUS_ROW_PLACEHOLDERS_KEY,
    CREATED_FROM_KEY,
    FALLBACK_LINKS_KEY,
    FLOW_COMPONENT_ID,
    ORCHESTRATOR_COMPONENT_ID,
    SCHEDULER_COMPONENT_ID,
    SHARED_CODE_COMPONENT_ID,
    VARIABLES_COMPONENT_ID,
    CreatedConfig,
    FlowBindingResult,
    TargetCache,
    VariableBindingResult,
)
from ._sync_push_ops import guard_script_shape

if TYPE_CHECKING:
    from .sync_service import SyncService

logger = logging.getLogger(__name__)

# The platform replaces ``{{ <row id> }}`` in a script array element with the
# shared-code row's code (configuration-variables-resolver SharedCodeResolver:
# ``/{{([ a-zA-Z0-9_-]+)}}/``, the match trimmed). Other ``{{name}}``
# placeholders are variables and are left untouched.
_SHARED_CODE_PLACEHOLDER = re.compile(r"\{\{( *)([a-zA-Z0-9_-]+)( *)\}\}")

# The code files ``code_extraction`` writes a transformation's
# ``parameters.blocks`` scripts into; the placeholders live there on disk.
_SCRIPT_FILENAMES: tuple[str, ...] = ("transform.sql", "transform.py")

# The configs whose job is to run another config, mapped to the push-error
# ``change_type`` of a failed remap.
_RUN_TARGET_LINK_TYPES: dict[str, str] = {
    FLOW_COMPONENT_ID: "flow_task_link",
    ORCHESTRATOR_COMPONENT_ID: "flow_task_link",
    SCHEDULER_COMPONENT_ID: "schedule_target_link",
}

# Added to the error of a failed link PUT. The local files already carry the
# new ids and the manifest hash stays stale, so the next push sees a modified
# config and sends them.
_LINK_RETRY_HINT = (
    "The local files already hold the new ids: run `kbagent sync push` again to send them."
)


# ---------------------------------------------------------------------------
# Phase C: transformation -> variables / shared-code links
# ---------------------------------------------------------------------------


def resolve_transformation_bindings(
    service: SyncService,
    client: Any,
    *,
    created_configs: list[CreatedConfig],
    created_id_map: dict[tuple[str, str], str],
    created_row_id_map: dict[tuple[str, str], str],
    created_rows_by_parent: dict[str, list[str]],
    manifest: Manifest,
    branch_id: int | None,
    planned_claims: set[str] | None = None,
) -> VariableBindingResult:
    """Rebind transformation links to the configs and rows created this push.

    Runs the variables pass, then the shared-code pass (CLI-24). The second
    pass re-reads the local file that the first one rewrote, so a
    transformation that has both links gets both.
    """
    result = VariableBindingResult()
    _bind_variables(
        service,
        client,
        result,
        created_configs=created_configs,
        created_id_map=created_id_map,
        created_row_id_map=created_row_id_map,
        created_rows_by_parent=created_rows_by_parent,
        manifest=manifest,
        branch_id=branch_id,
        planned_claims=planned_claims or set(),
    )
    _bind_shared_code(
        service,
        client,
        result,
        created_configs=created_configs,
        created_id_map=created_id_map,
        created_row_id_map=created_row_id_map,
        manifest=manifest,
        branch_id=branch_id,
    )
    return result


def _bind_variables(
    service: SyncService,
    client: Any,
    result: VariableBindingResult,
    *,
    created_configs: list[CreatedConfig],
    created_id_map: dict[tuple[str, str], str],
    created_row_id_map: dict[tuple[str, str], str],
    created_rows_by_parent: dict[str, list[str]],
    manifest: Manifest,
    branch_id: int | None,
    planned_claims: set[str],
) -> None:
    """Rebind transformation -> variables links from placeholders to ULIDs.

    On a fresh CREATE the transformation config is POSTed with its
    ``_configuration_extra.variables_id`` / ``variables_values_id`` still set to
    the externally-authored placeholder strings (``config_format`` merges
    ``_configuration_extra`` into the API body verbatim). This pass, run after
    the variables config and its values row have been created, resolves each
    placeholder to the ULID assigned during this push, PUTs the corrected
    configuration body, then rewrites the local file and refreshes the manifest
    hashes so a re-push is clean (KFR-03).

    Resolution is a no-op when no ``keboola.variables`` config was created this
    push (the already-bound / UPDATE path). When the exact placeholder key
    misses but exactly one ``keboola.variables`` config was created this push,
    it binds to that one with a warning; zero or ambiguous (>1) matches
    accumulate an error rather than writing a broken link.
    """
    created_variables_ulids = [
        ulid
        for (component_id, _placeholder), ulid in created_id_map.items()
        if component_id == VARIABLES_COMPONENT_ID
    ]

    for created in created_configs:
        if created.component_id == VARIABLES_COMPONENT_ID:
            continue  # the variables config itself never carries a link
        local_data = service._read_config_file(created.config_dir)
        if local_data is None:
            continue
        extra = local_data.get("_configuration_extra")
        if not isinstance(extra, dict):
            continue
        vars_placeholder = extra.get("variables_id")
        if not vars_placeholder or not isinstance(vars_placeholder, str):
            continue
        raw_vals = extra.get("variables_values_id")
        vals_placeholder = raw_vals if isinstance(raw_vals, str) else ""

        parent_ulid = _resolve_variables_parent(
            created=created,
            vars_placeholder=vars_placeholder,
            created_id_map=created_id_map,
            created_variables_ulids=created_variables_ulids,
            errors=result.errors,
            may_fall_back=partial(
                _names_nothing,
                client,
                result.targets,
                manifest,
                branch_id,
                planned_claims,
                vars_placeholder,
            ),
        )
        if parent_ulid is None:
            continue

        row_ulid = _resolve_variables_row(
            created=created,
            parent_ulid=parent_ulid,
            vals_placeholder=vals_placeholder,
            created_row_id_map=created_row_id_map,
            created_rows_by_parent=created_rows_by_parent,
            errors=result.errors,
            parent_by_fallback=(VARIABLES_COMPONENT_ID, vars_placeholder) not in created_id_map,
            ambiguous_rows=_ambiguous_rows(manifest, parent_ulid, branch_id),
        )
        # A missing-but-required values row already recorded an error.
        if vals_placeholder and row_ulid is None:
            if (VARIABLES_COMPONENT_ID, vars_placeholder) not in created_id_map:
                # The fallback chose the parent: keep that choice for the push
                # that creates the row -- the id T names maps to nothing else.
                _remember_fallback(manifest, created, branch_id, vars_placeholder, parent_ulid)
            continue

        try:
            _apply_variable_binding(
                service,
                client,
                created=created,
                local_data=local_data,
                parent_ulid=parent_ulid,
                row_ulid=row_ulid,
                manifest=manifest,
                branch_id=branch_id,
                warnings=result.warnings,
            )
        except KeboolaApiError as exc:
            result.errors.append(
                {
                    "change_type": "variable_link",
                    "error_code": ErrorCode.VARIABLE_LINK_UNRESOLVED,
                    "component_id": created.component_id,
                    "config_id": created.config_id,
                    "message": f"{exc} {_LINK_RETRY_HINT}",
                }
            )
            continue
        result.configs_rewritten += 1


def _resolve_variables_parent(
    *,
    created: CreatedConfig,
    vars_placeholder: str,
    created_id_map: dict[tuple[str, str], str],
    created_variables_ulids: list[str],
    errors: list[dict[str, str]],
    may_fall_back: Callable[[], bool] | None = None,
) -> str | None:
    """Resolve a transformation's ``variables_id`` placeholder to a ULID.

    Returns the ULID, or ``None`` when there is nothing to backfill
    (already-bound path) or the link is ambiguous (an error is appended).
    The single-created-config fallback applies only when *may_fall_back*
    says the id names nothing: an id the target has, or a placeholder of a
    config created by an earlier push, is never redirected (Astra).
    """
    parent_ulid = created_id_map.get((VARIABLES_COMPONENT_ID, vars_placeholder))
    if parent_ulid is not None:
        return parent_ulid
    if not created_variables_ulids:
        # No variables config created this push: the link is either already
        # a ULID (UPDATE path) or points outside this push. Leave it.
        return None
    if may_fall_back is not None and not may_fall_back():
        return None  # an existing config or a mapped placeholder: never redirected
    if len(created_variables_ulids) == 1:
        parent_ulid = created_variables_ulids[0]
        logger.warning(
            "Transformation %s/%s variables_id placeholder %r did not match any "
            "created variables config; binding to the single keboola.variables "
            "config created this push (%s).",
            created.component_id,
            created.config_id,
            vars_placeholder,
            parent_ulid,
        )
        return parent_ulid
    errors.append(
        {
            "change_type": "variable_link",
            "error_code": ErrorCode.VARIABLE_LINK_UNRESOLVED,
            "component_id": created.component_id,
            "config_id": created.config_id,
            "message": (
                f"Cannot resolve variables_id placeholder {vars_placeholder!r}: "
                f"{len(created_variables_ulids)} keboola.variables configs were "
                "created this push and none matched by placeholder. Refusing to "
                "write an ambiguous variables link."
            ),
        }
    )
    return None


def _ambiguous_rows(manifest: Manifest, parent_id: str, branch_id: int | None) -> set[str]:
    """The row placeholders recorded as ambiguous under *parent_id* (``ambiguous_row_placeholders``)."""
    entry = find_branch_entry(manifest, VARIABLES_COMPONENT_ID, parent_id, branch_id)
    recorded = entry.metadata.get(AMBIGUOUS_ROW_PLACEHOLDERS_KEY) if entry is not None else None
    return {str(p) for p in recorded or []}


def _remember_fallback(
    manifest: Manifest,
    created: CreatedConfig,
    branch_id: int | None,
    variables_id: str,
    parent_ulid: str,
) -> None:
    """Record on the transformation's entry which config the fallback bound *variables_id* to."""
    entry = find_branch_entry(manifest, created.component_id, created.config_id, branch_id)
    if entry is not None:
        links = dict(entry.metadata.get(FALLBACK_LINKS_KEY) or {})
        links[f"{VARIABLES_COMPONENT_ID}/{variables_id}"] = parent_ulid
        entry.metadata[FALLBACK_LINKS_KEY] = links


def _names_nothing(
    client: Any,
    targets: TargetCache,
    manifest: Manifest,
    branch_id: int | None,
    planned_claims: set[str],
    variables_id: str,
) -> bool:
    """True when NOBODY claims *variables_id* and the target does not have it.

    Only such a reference -- a typo, a placeholder no config carries -- may
    be bound to the single variables config created this push. Claimed is
    an id that is a manifest entry's current id (a placeholder entry whose
    create failed keeps it), a ``created_from_id``, or a placeholder this
    push planned for an untracked dir (*planned_claims*, also when its
    create failed): binding such a link elsewhere could never be repaired
    by the retry of that create (Astra). Existence is read through the
    push's target cache; only a 404 proves the id names nothing.
    """
    claimed = set(planned_claims)
    for entry in manifest.configurations:
        if entry.component_id == VARIABLES_COMPONENT_ID:
            claimed |= {entry.id, str(entry.metadata.get(CREATED_FROM_KEY) or "")}
    if variables_id in claimed:
        return False
    return read_target(client, targets, VARIABLES_COMPONENT_ID, variables_id, branch_id).gone


def _resolve_variables_row(
    *,
    created: CreatedConfig,
    parent_ulid: str,
    vals_placeholder: str,
    created_row_id_map: dict[tuple[str, str], str],
    created_rows_by_parent: dict[str, list[str]],
    errors: list[dict[str, str]],
    parent_by_fallback: bool = True,
    ambiguous_rows: set[str] | None = None,
) -> str | None:
    """Resolve a transformation's ``variables_values_id`` placeholder.

    Returns the row ULID, or ``None`` when no values row was created (the link
    is then left unset) or the choice is ambiguous (an error is appended only
    when ``vals_placeholder`` was actually requested).

    The single-sibling fallback serves a link that names no row (empty
    ``variables_values_id``), or one under a parent that was itself the
    fallback (its row id names a config nobody claims, so no row either).
    An explicit row id under a parent matched exactly -- ``prod-ref`` whose
    create failed, or one two new rows carry -- is never re-pointed to a
    sibling (Astra): it stays, with an error, and the transformation stays
    pending until that row's own placeholder resolves.
    """
    if vals_placeholder:
        mapped = created_row_id_map.get((parent_ulid, vals_placeholder))
        if mapped is not None:
            return mapped
        # An ambiguous placeholder never resolves by itself -- also not under a
        # parent the fallback chose, where one surviving sibling would win (Astra).
        if not parent_by_fallback or vals_placeholder in (ambiguous_rows or set()):
            errors.append(
                {
                    "change_type": "variable_link",
                    "error_code": ErrorCode.VARIABLE_LINK_UNRESOLVED,
                    "component_id": created.component_id,
                    "config_id": created.config_id,
                    "message": (
                        f"variables_values_id {vals_placeholder!r} names no row created "
                        f"under variables config {parent_ulid} (its create failed, or "
                        "several rows carry it). The link is left as it is; the push "
                        "that creates that row sets it."
                    ),
                }
            )
            return None
    siblings = created_rows_by_parent.get(parent_ulid, [])
    if len(siblings) == 1:
        row_ulid = siblings[0]
        if vals_placeholder:
            logger.warning(
                "Transformation %s/%s variables_values_id placeholder %r did not "
                "match a created row; binding to the single row created under "
                "variables config %s.",
                created.component_id,
                created.config_id,
                vals_placeholder,
                parent_ulid,
            )
        return row_ulid
    if vals_placeholder:
        errors.append(
            {
                "change_type": "variable_link",
                "error_code": ErrorCode.VARIABLE_LINK_UNRESOLVED,
                "component_id": created.component_id,
                "config_id": created.config_id,
                "message": (
                    f"Cannot resolve variables_values_id placeholder "
                    f"{vals_placeholder!r}: {len(siblings)} rows were created under "
                    f"variables config {parent_ulid}. Refusing to write an "
                    "ambiguous values link."
                ),
            }
        )
    return None


def _apply_variable_binding(
    service: SyncService,
    client: Any,
    *,
    created: CreatedConfig,
    local_data: dict[str, Any],
    parent_ulid: str,
    row_ulid: str | None,
    manifest: Manifest,
    branch_id: int | None,
    warnings: list[dict[str, Any]],
) -> None:
    """PUT the resolved variables link, rewrite local, refresh manifest hashes.

    ``local_data`` is the pristine on-disk ``_config.yml`` dict; a deep copy is
    code-merged to build the full PUT body so blocks/code stay only in their
    companion files. Uses :meth:`KeboolaClient.update_config` (PUT) directly --
    **not** ``set_variables``, which would create a *second* variables config.

    The local file is rewritten even when the PUT fails, and the manifest hash
    is then left as it was. The next push sees a modified config and sends the
    link again.
    """
    merged = copy.deepcopy(local_data)
    merge_code_files(created.component_id, merged, created.config_dir)
    _name, _description, configuration = local_config_to_api(merged)
    # This backfill PUTs the WHOLE configuration again, so it is the LAST write
    # a freshly-created transformation receives -- an unguarded body here would
    # undo the normalization ``push_create`` just applied.
    configuration = guard_script_shape(
        created.component_id, configuration, warnings, config_id=created.config_id
    )
    configuration["variables_id"] = parent_ulid
    if row_ulid:
        configuration["variables_values_id"] = row_ulid

    # Rewrite the local _configuration_extra to the ULIDs (pristine data:
    # no merged blocks leak into _config.yml).
    extra = local_data.setdefault("_configuration_extra", {})
    extra["variables_id"] = parent_ulid
    if row_ulid:
        extra["variables_values_id"] = row_ulid
    try:
        response = client.update_config(
            component_id=created.component_id,
            config_id=created.config_id,
            configuration=configuration,
            change_description="Resolve variables link via kbagent sync push",
            branch_id=branch_id,
        )
    finally:
        service._write_config_file(created.config_dir, local_data)
    logger.info(
        "Resolved variables link for %s/%s -> variables_id=%s variables_values_id=%s",
        created.component_id,
        created.config_id,
        parent_ulid,
        row_ulid,
    )

    # config_hash includes _configuration_extra, so refresh the stored
    # hashes from the post-rewrite disk state or sync diff sees a conflict.
    _refresh_binding_hashes(
        service,
        client,
        created=created,
        manifest=manifest,
        branch_id=branch_id,
        response=response,
        warnings=warnings,
    )


def _refresh_binding_hashes(
    service: SyncService,
    client: Any,
    *,
    created: CreatedConfig,
    manifest: Manifest,
    branch_id: int | None,
    response: Any,
    warnings: list[dict[str, Any]],
) -> None:
    """Re-stamp a rebound config's manifest bookkeeping after the backfill PUT.

    ``config_hash`` includes ``_configuration_extra``, which both backfills
    rewrite, so the stored hashes must be refreshed or ``sync diff`` reports a
    conflict. The config hash comes from the API's view of the config it just
    wrote (issue #686); the file hashes describe the local files.
    """
    hashes = service._compute_config_hashes(created.config_dir, created.component_id)
    stamp = config_baseline(
        client,
        component_id=created.component_id,
        config_id=created.config_id,
        branch_id=branch_id,
        response=response,
    )
    if stamp.warning is not None:
        warnings.append(stamp.warning)
    # ``same_branch``: a push to branch None updates an entry recorded under the
    # default branch id (and the other way round) -- a raw ``==`` missed it.
    cfg = find_branch_entry(manifest, created.component_id, created.config_id, branch_id)
    if cfg is not None:
        cfg.metadata["pull_hash"] = hashes.file_hash
        record_companions(cfg.metadata, hashes.extra_hashes)
        apply_stamp(cfg.metadata, stamp)


# ---------------------------------------------------------------------------
# Phase C, second pass: transformation -> shared-code links (CLI-24)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SharedCodeLink:
    """A transformation's shared-code link, re-pointed to the ids created this push.

    ``config_id`` is the new ``shared_code_id``. ``row_id_map`` maps each old
    row id (in ``shared_code_row_ids`` and in the ``{{<row id>}}`` script
    placeholders) to its new id. ``unmapped_row_ids`` are listed rows that have
    no new row under the new shared-code config, for example because the row
    create failed.
    """

    config_id: str
    row_id_map: dict[str, str]
    unmapped_row_ids: list[str]


def _bind_shared_code(
    service: SyncService,
    client: Any,
    result: VariableBindingResult,
    *,
    created_configs: list[CreatedConfig],
    created_id_map: dict[tuple[str, str], str],
    created_row_id_map: dict[tuple[str, str], str],
    manifest: Manifest,
    branch_id: int | None,
) -> None:
    """Rebind transformation -> shared-code links from source ids to ULIDs.

    A transformation uses shared code through ``shared_code_id`` (the
    ``keboola.shared-code`` config), ``shared_code_row_ids`` (its rows) and one
    ``{{<row id>}}`` placeholder per row in its scripts. After a fresh CREATE
    (e.g. ``sync clone``) all three still carry the source ids, and the job
    fails to read the shared code. This pass sets them to the ids created this
    push, PUTs the corrected configuration, rewrites the local files and
    refreshes the manifest hashes, like the variables pass.

    A no-op when the transformation's shared-code config and rows were not
    created this push (they may be pre-existing configs). A listed row that
    cannot be re-pointed is an error, not a silent skip.
    """
    for created in created_configs:
        if created.component_id == SHARED_CODE_COMPONENT_ID:
            continue
        local_data = service._read_config_file(created.config_dir)
        if local_data is None:
            continue
        extra = local_data.get("_configuration_extra")
        if not isinstance(extra, dict):
            continue
        link = _resolve_shared_code_link(
            extra, created_id_map=created_id_map, created_row_id_map=created_row_id_map
        )
        if link is None:
            continue
        if link.unmapped_row_ids:
            unmapped = ", ".join(link.unmapped_row_ids)
            result.errors.append(
                {
                    "change_type": "shared_code_link",
                    "error_code": ErrorCode.LINK_UNRESOLVED,
                    "component_id": created.component_id,
                    "config_id": created.config_id,
                    "message": (
                        f"shared_code_row_ids {unmapped} have no row under shared-code config "
                        f"{link.config_id} created in this push, so they keep the old ids. "
                        "Check the shared-code rows and set the ids by hand."
                    ),
                }
            )
        try:
            _apply_shared_code_binding(
                service,
                client,
                created=created,
                local_data=local_data,
                link=link,
                manifest=manifest,
                branch_id=branch_id,
                warnings=result.warnings,
            )
        except KeboolaApiError as exc:
            result.errors.append(
                {
                    "change_type": "shared_code_link",
                    "error_code": ErrorCode.API_ERROR,
                    "component_id": created.component_id,
                    "config_id": created.config_id,
                    "message": f"{exc} {_LINK_RETRY_HINT}",
                }
            )
            continue
        result.configs_rewritten += 1
        result.shared_code_links += 1


def _resolve_shared_code_link(
    extra: dict[str, Any],
    *,
    created_id_map: dict[tuple[str, str], str],
    created_row_id_map: dict[tuple[str, str], str],
) -> SharedCodeLink | None:
    """Return the re-pointed shared-code link of a transformation, or ``None``.

    ``None`` when the transformation has no shared-code link or none of its
    ids was created this push. A row is looked up under the (new) shared-code
    config only, so two shared-code configs with the same row id cannot swap
    rows. When the shared-code config was created this push, every row it has
    is new too, so a listed row id without a new row is ``unmapped``.
    """
    raw_config_id = extra.get("shared_code_id")
    if not isinstance(raw_config_id, (str, int)) or raw_config_id == "":
        return None
    old_config_id = str(raw_config_id)
    config_id = created_id_map.get((SHARED_CODE_COMPONENT_ID, old_config_id), old_config_id)
    config_created = config_id != old_config_id
    old_row_ids = extra.get("shared_code_row_ids")
    row_id_map: dict[str, str] = {}
    unmapped_row_ids: list[str] = []
    for raw_row_id in old_row_ids if isinstance(old_row_ids, list) else []:
        old_row_id = str(raw_row_id)
        new_row_id = created_row_id_map.get((config_id, old_row_id))
        if new_row_id is None:
            if config_created:
                unmapped_row_ids.append(old_row_id)
        elif new_row_id != old_row_id:
            row_id_map[old_row_id] = new_row_id
    if not config_created and not row_id_map:
        return None
    return SharedCodeLink(
        config_id=config_id, row_id_map=row_id_map, unmapped_row_ids=unmapped_row_ids
    )


def rewrite_shared_code_placeholders(text: str, row_id_map: dict[str, str]) -> str:
    """Replace each ``{{<old row id>}}`` in ``text`` with ``{{<new row id>}}``.

    Keeps the spaces inside the braces. A placeholder whose id is not in
    ``row_id_map`` (a variable, or a row that was not re-pointed) is left as is.
    """

    def _swap(match: re.Match[str]) -> str:
        new_row_id = row_id_map.get(match.group(2))
        if new_row_id is None:
            return match.group(0)
        return "{{" + match.group(1) + new_row_id + match.group(3) + "}}"

    return _SHARED_CODE_PLACEHOLDER.sub(_swap, text)


def _rewrite_list_placeholders(node: Any, row_id_map: dict[str, str]) -> Any:
    """Rewrite the placeholders in every string list element under ``node``.

    The platform replaces shared code only inside arrays (a script is a list
    of statements), so a plain string value is never a placeholder.
    """
    if isinstance(node, dict):
        return {key: _rewrite_list_placeholders(value, row_id_map) for key, value in node.items()}
    if isinstance(node, list):
        return [
            rewrite_shared_code_placeholders(item, row_id_map)
            if isinstance(item, str)
            else _rewrite_list_placeholders(item, row_id_map)
            for item in node
        ]
    return node


def _repoint_shared_code(config_data: dict[str, Any], link: SharedCodeLink) -> None:
    """Set a local-format config's shared-code ids and inline script placeholders."""
    extra = config_data["_configuration_extra"]
    extra["shared_code_id"] = link.config_id
    old_row_ids = extra.get("shared_code_row_ids")
    if isinstance(old_row_ids, list):
        extra["shared_code_row_ids"] = [link.row_id_map.get(str(r), r) for r in old_row_ids]
    if "parameters" in config_data:
        config_data["parameters"] = _rewrite_list_placeholders(
            config_data["parameters"], link.row_id_map
        )


def _rewrite_script_files(config_dir: Path, row_id_map: dict[str, str]) -> None:
    """Rewrite the script placeholders in a config's extracted code files."""
    for filename in _SCRIPT_FILENAMES:
        path = config_dir / filename
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        rewritten = rewrite_shared_code_placeholders(text, row_id_map)
        if rewritten != text:
            path.write_text(rewritten, encoding="utf-8")


def _apply_shared_code_binding(
    service: SyncService,
    client: Any,
    *,
    created: CreatedConfig,
    local_data: dict[str, Any],
    link: SharedCodeLink,
    manifest: Manifest,
    branch_id: int | None,
    warnings: list[dict[str, Any]],
) -> None:
    """PUT the re-pointed shared-code link, then rewrite the local files and hashes.

    The local files are rewritten even when the PUT fails, and the manifest
    hash is then left as it was. The next push sees a modified config and
    sends the link again.
    """
    merged = copy.deepcopy(local_data)
    merge_code_files(created.component_id, merged, created.config_dir)
    _repoint_shared_code(merged, link)
    _name, _description, configuration = local_config_to_api(merged)
    # The whole configuration is PUT again, so guard it like the variables pass.
    configuration = guard_script_shape(
        created.component_id, configuration, warnings, config_id=created.config_id
    )
    _repoint_shared_code(local_data, link)
    try:
        response = client.update_config(
            component_id=created.component_id,
            config_id=created.config_id,
            configuration=configuration,
            change_description="Resolve shared code link via kbagent sync push",
            branch_id=branch_id,
        )
    finally:
        service._write_config_file(created.config_dir, local_data)
        _rewrite_script_files(created.config_dir, link.row_id_map)
    logger.info(
        "Resolved shared code link for %s/%s -> shared_code_id=%s rows=%s",
        created.component_id,
        created.config_id,
        link.config_id,
        link.row_id_map,
    )
    _refresh_binding_hashes(
        service,
        client,
        created=created,
        manifest=manifest,
        branch_id=branch_id,
        response=response,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Phase D: flow / orchestrator task and schedule target remap (#426, CLI-24)
# ---------------------------------------------------------------------------


@dataclass
class RunTargetRemap:
    """What :func:`remap_run_targets_in_place` changed in one config.

    ``references`` counts the task ``configId``s (or the schedule target)
    rewritten, ``config_row_ids`` the task ``configRowIds`` entries rewritten.
    ``unmapped_rows`` holds one message per task whose row ids have no new row.
    ``unresolved`` counts references to a config push has not created yet.
    """

    references: int = 0
    config_row_ids: int = 0
    unmapped_rows: list[str] = field(default_factory=list)
    unresolved: int = 0

    @property
    def pending(self) -> bool:
        """True while a reference still names a config or row not created yet (D9)."""
        return bool(self.unmapped_rows or self.unresolved)


def resolve_run_target_bindings(
    service: SyncService,
    client: Any,
    *,
    created_configs: list[CreatedConfig],
    created_id_map: dict[tuple[str, str], str],
    created_row_id_map: dict[tuple[str, str], str],
    manifest: Manifest,
    branch_id: int | None,
    source_dir: Path | None = None,
    planned_rows: set[tuple[str, str]] | None = None,
    targets: TargetCache | None = None,
) -> FlowBindingResult:
    """Remap the configs that flows, orchestrations and schedules run (Phase D).

    A ``keboola.flow`` or legacy ``keboola.orchestrator`` config runs other
    configs via ``configuration.tasks[].task.configId`` (and optionally only
    some rows via ``task.configRowIds``); a ``keboola.scheduler`` config runs
    ``configuration.target.configurationId``. On disk these live under
    ``_configuration_extra``. When such a config is created in the same push
    as the configs it runs -- e.g. a ``sync clone`` of a reference project --
    those ids still point at the source ids. This pass (mirroring the Phase-C
    variable backfill) resolves each reference to the ULID assigned to that
    placeholder, PUTs the corrected config, rewrites the local
    ``_config.yml``, and refreshes the manifest hashes so a re-push is clean.
    It never activates a schedule with the Scheduler service.

    The maps come from this push's creates AND the manifest (D9): a config
    still ``link_pending`` from an earlier push (its referenced row failed, or
    the push died before this pass) is finished here too, from *source_dir*,
    once what it names exists. ``link_pending`` is cleared when every
    reference resolves and kept while one still names a config or row not
    created yet.

    A pending config is recovered from its REMOTE configuration (#929 D32):
    only the references it holds there in source form are rewritten -- one a
    user changed in the UI stays -- and every target the rewritten payload
    names is read first (*targets*, the push's cache shared with Phase C). A
    target that is gone or cannot be read is never written; whatever depends
    on it stays pending and is reported. The flag is settled from the remote
    content, so a link repaired in the UI clears it without a write.

    A no-op when no such config was created this push or is pending, or when
    no task or target references a config created from a placeholder (the id
    is left untouched -- it may legitimately point at a pre-existing config).
    """
    result = FlowBindingResult()
    maps = build_link_maps(
        manifest,
        branch_id,
        created_id_map=created_id_map,
        created_row_id_map=created_row_id_map,
        source_dir=source_dir,
        planned_rows=planned_rows,
        uncreated_claims=targets.uncreated_claims if targets is not None else None,
    )
    seen = {(c.component_id, c.config_id) for c in created_configs}
    recovering = [
        CreatedConfig(entry.component_id, entry.id, source_dir / entry.path)
        for entry in link_pending_entries(manifest, branch_id)
        if source_dir is not None
        and (entry.component_id, entry.id) not in seen
        and entry.component_id in _RUN_TARGET_LINK_TYPES
    ]
    # Recovery starts from the REMOTE (#929 D32): what a pending config holds
    # there is what is rewritten, and every target it will name is validated.
    remotes = {c.config_id: _read_pending(client, c, branch_id, result) for c in recovering}
    referenced = _referenced_targets(
        [(c, _local_extra(service, c)) for c in created_configs]
        + [(c, (remotes[c.config_id] or {}).get("configuration")) for c in recovering],
        maps,
    )
    result.errors.extend(
        validate_targets(
            client,
            manifest,
            branch_id,
            maps,
            targets or TargetCache(),
            referenced=referenced,
            fresh_rows=set(created_row_id_map.values()),
        )
    )
    for created in [*created_configs, *recovering]:
        link_type = _RUN_TARGET_LINK_TYPES.get(created.component_id)
        if link_type is None:
            continue
        if created in created_configs:
            pending = _bind_run_target(
                service, client, created, link_type, maps, manifest, branch_id, result
            )
        else:
            remote = remotes[created.config_id]
            pending = remote is None or _recover_run_target(
                service, client, created, remote, link_type, maps, manifest, branch_id, result
            )
        entry = find_branch_entry(manifest, created.component_id, created.config_id, branch_id)
        result.pending_changed |= set_link_pending(entry, pending)
    return result


def _read_pending(
    client: Any, created: CreatedConfig, branch_id: int | None, result: FlowBindingResult
) -> dict[str, Any] | None:
    """The remote of a pending config; ``None`` (an error, it stays pending) when unreadable."""
    try:
        remote = client.get_config_detail(
            component_id=created.component_id, config_id=created.config_id, branch_id=branch_id
        )
    except KeboolaApiError as exc:
        link_type = _RUN_TARGET_LINK_TYPES[created.component_id]
        result.errors.append(_link_error(link_type, created, ErrorCode.API_ERROR, str(exc)))
        return None
    return remote if isinstance(remote, dict) else None


def _local_extra(service: SyncService, created: CreatedConfig) -> Any:
    return (service._read_config_file(created.config_dir) or {}).get("_configuration_extra")


def _referenced_targets(configs: list[tuple[CreatedConfig, Any]], maps: LinkMaps) -> set[str]:
    """Target ids the run-target payloads in *configs* name, or will name once rewritten."""
    found: set[str] = set()
    for created, extra in configs:
        if created.component_id not in _RUN_TARGET_LINK_TYPES or not isinstance(extra, dict):
            continue
        refs: list[tuple[str, str]] = []
        target = extra.get("target")
        if isinstance(target, dict) and isinstance(target.get("componentId"), str):
            refs.append((target["componentId"], str(target.get("configurationId", ""))))
        tasks = extra.get("tasks")
        for task_entry in tasks if isinstance(tasks, list) else []:
            ref = task_config_ref(created.component_id, task_entry)
            if ref is not None:
                refs.append(ref)
        found.update(maps.configs.get(ref, ref[1]) for ref in refs)
    return found


def put_configuration_only(
    client: Any,
    component_id: str,
    config_id: str,
    configuration: dict[str, Any],
    branch_id: Any,
    change_description: str = "Remap linked config IDs via kbagent sync push",
) -> Any:
    """PUT only ``configuration``: name, description and enabled state stay as they are.

    The write of every reference recovery (#929 D29, D9): a change made on the
    remote since -- in the UI, say -- survives by construction.
    """
    return client.update_config(
        component_id=component_id,
        config_id=config_id,
        configuration=configuration,
        change_description=change_description,
        branch_id=branch_id,
    )


def _recover_run_target(
    service: SyncService,
    client: Any,
    created: CreatedConfig,
    remote: dict[str, Any],
    link_type: str,
    maps: LinkMaps,
    manifest: Manifest,
    branch_id: int | None,
    result: FlowBindingResult,
) -> bool:
    """Finish a ``link_pending`` config of an earlier push FROM THE REMOTE (D9, #929 D29/D32).

    The remote config may have changed since that push (an edit in the UI) --
    diff then withholds it -- so the local file is never sent. *remote* is the
    configuration read once for this pass; ONLY its references are rewritten, and only
    ``configuration`` is PUT. When the local file held exactly what the remote
    held, its references are rewritten the same way and both baselines move
    to the result; otherwise nothing local changes and diff shows the remote
    change for the next pull. Returns whether the config stays pending.
    """
    configuration = remote.get("configuration")
    if not isinstance(configuration, dict):
        return True
    rewritten = copy.deepcopy(configuration)
    remap = _remap_into(created, rewritten, maps, link_type, result)
    if not remap.references and not remap.config_row_ids:
        return remap.pending
    local_data = service._read_config_file(created.config_dir)
    in_step = local_data is not None and _local_hash(created, local_data) == config_hash(
        api_config_to_local(created.component_id, remote, created.config_id)
    )
    try:
        response = put_configuration_only(
            client, created.component_id, created.config_id, rewritten, branch_id
        )
    except KeboolaApiError as exc:
        result.errors.append(_link_error(link_type, created, ErrorCode.API_ERROR, str(exc)))
        return True
    _count(created.component_id, remap, result)
    extra = (local_data or {}).get("_configuration_extra")
    if in_step and local_data is not None and isinstance(extra, dict):
        remap_run_targets_in_place(
            created.component_id,
            extra,
            maps.configs,
            maps.rows,
            target_rows=maps.target_rows,
            uncreated=maps.uncreated,
            planned_rows=maps.planned_rows,
        )
        service._write_config_file(created.config_dir, local_data)
        _refresh_binding_hashes(
            service,
            client,
            created=created,
            manifest=manifest,
            branch_id=branch_id,
            response=response,
            warnings=result.warnings,
        )
    return remap.pending


def _local_hash(created: CreatedConfig, local_data: dict[str, Any]) -> str:
    merged = copy.deepcopy(local_data)
    merge_code_files(created.component_id, merged, created.config_dir)
    return config_hash(merged)


def _remap_into(
    created: CreatedConfig,
    extra: dict[str, Any],
    maps: LinkMaps,
    link_type: str,
    result: FlowBindingResult,
) -> RunTargetRemap:
    """Remap *extra* in place and report every task row id that stays unmapped."""
    remap = remap_run_targets_in_place(
        created.component_id,
        extra,
        maps.configs,
        maps.rows,
        target_rows=maps.target_rows,
        uncreated=maps.uncreated,
        planned_rows=maps.planned_rows,
    )
    for message in remap.unmapped_rows:
        result.errors.append(_link_error(link_type, created, ErrorCode.LINK_UNRESOLVED, message))
    return remap


def _link_error(
    link_type: str, created: CreatedConfig, error_code: ErrorCode, message: str
) -> dict[str, str]:
    return {
        "change_type": link_type,
        "error_code": error_code,
        "component_id": created.component_id,
        "config_id": created.config_id,
        "message": message,
    }


def _count(component_id: str, remap: RunTargetRemap, result: FlowBindingResult) -> None:
    result.configs_rewritten += 1
    if component_id == SCHEDULER_COMPONENT_ID:
        result.schedule_targets += remap.references
    elif component_id == ORCHESTRATOR_COMPONENT_ID:
        result.orchestrator_tasks += remap.references
    else:
        result.flow_tasks += remap.references
    result.config_row_ids += remap.config_row_ids


def _bind_run_target(
    service: SyncService,
    client: Any,
    created: CreatedConfig,
    link_type: str,
    maps: LinkMaps,
    manifest: Manifest,
    branch_id: int | None,
    result: FlowBindingResult,
) -> bool:
    """Remap a config created this push and PUT it when changed; return whether it stays pending.

    Created this push, so its remote copy is the local file: the file is
    remapped, PUT and stamped.
    """
    local_data = service._read_config_file(created.config_dir)
    extra = (local_data or {}).get("_configuration_extra")
    if local_data is None or not isinstance(extra, dict):
        return False
    remap = _remap_into(created, extra, maps, link_type, result)
    if not remap.references and not remap.config_row_ids:
        return remap.pending
    try:
        _apply_run_target_binding(
            service,
            client,
            created=created,
            local_data=local_data,
            manifest=manifest,
            branch_id=branch_id,
            warnings=result.warnings,
        )
    except KeboolaApiError as exc:
        result.errors.append(
            _link_error(link_type, created, ErrorCode.API_ERROR, f"{exc} {_LINK_RETRY_HINT}")
        )
        return True
    _count(created.component_id, remap, result)
    return remap.pending


def task_config_ref(component_id: str, task_entry: Any) -> tuple[str, str] | None:
    """Return the ``(componentId, configId)`` a flow or orchestrator task runs.

    ``None`` for a task that runs no config. A ``keboola.flow`` task is typed:
    only ``task.type == 'job'`` runs a config (notification and variable tasks
    do not). A legacy ``keboola.orchestrator`` task has no type; it runs a
    config when it has a non-empty ``task.configId`` (it can carry an inline
    ``configData`` instead).
    """
    if not isinstance(task_entry, dict):
        return None
    task = task_entry.get("task")
    if not isinstance(task, dict):
        return None
    if component_id == FLOW_COMPONENT_ID and task.get("type") != "job":
        return None
    comp = task.get("componentId")
    config_id = task.get("configId")
    if not isinstance(comp, str) or not isinstance(config_id, (str, int)) or config_id == "":
        return None
    return comp, str(config_id)


def remap_run_targets_in_place(
    component_id: str,
    extra: dict[str, Any],
    created_id_map: dict[tuple[str, str], str],
    created_row_id_map: dict[tuple[str, str], str],
    *,
    target_rows: dict[tuple[str, str], set[str]] | None = None,
    uncreated: set[tuple[str, str]] | None = None,
    planned_rows: set[tuple[str, str]] | None = None,
) -> RunTargetRemap:
    """Rewrite the task ``configId``s / ``configRowIds`` or the schedule target in place.

    A reference is rewritten only when its ``(componentId, configId)`` names
    a placeholder of a config created from it, this push or (D9) an earlier
    one. A reference already naming such a created config is in target form;
    its ``configRowIds`` are still resolved, under that config only: a row
    the config has on the target stays, a placeholder row maps to the row
    created from it. A reference to a config still to be created (*uncreated*)
    is counted ``unresolved``.
    """
    remap = RunTargetRemap()
    waiting = uncreated or set()
    if component_id == SCHEDULER_COMPONENT_ID:
        target = extra.get("target")
        if (
            isinstance(target, dict)
            and (
                target.get("componentId"),
                str(target.get("configurationId", "")),
            )
            in waiting
        ):
            remap.unresolved += 1
        remap.references = _remap_schedule_target(target, created_id_map)
        return remap
    tasks = extra.get("tasks")
    if not isinstance(tasks, list):
        return remap
    created_targets = {(comp, new_id) for (comp, _), new_id in created_id_map.items()}
    for task_entry in tasks:
        ref = task_config_ref(component_id, task_entry)
        if ref is None:
            continue
        if ref in waiting:
            remap.unresolved += 1
            continue
        created = created_id_map.get(ref) or (ref[1] if ref in created_targets else "")
        # An existing config's rows are remapped too, when one it selects was
        # created this push from a file carrying that id (R6); unknown ids of
        # an existing config are left alone -- they may be rows of its own.
        new_id = created or ref[1]
        if ref[1] != new_id:
            task_entry["task"]["configId"] = new_id
            remap.references += 1
        known = (target_rows or {}).get((ref[0], new_id), set())
        _remap_task_rows(
            task_entry,
            ref[0],
            new_id,
            created_row_id_map,
            remap,
            known,
            track_unmapped=bool(created),
            planned=planned_rows or set(),
        )
    return remap


def _remap_task_rows(
    task_entry: dict[str, Any],
    component_id: str,
    new_config_id: str,
    created_row_id_map: dict[tuple[str, str], str],
    remap: RunTargetRemap,
    known_rows: set[str],
    *,
    track_unmapped: bool = True,
    planned: set[tuple[str, str]] | None = None,
) -> None:
    """Rewrite a task's ``configRowIds`` to the rows created under its config.

    A row id the config already has on the target (*known_rows*) is kept. A
    row id without a row created from it is kept too, reported in
    ``remap.unmapped_rows``, and leaves the config pending (D9).
    """
    task = task_entry["task"]
    old_row_ids = task.get("configRowIds")
    if not isinstance(old_row_ids, list):
        return
    new_row_ids: list[Any] = []
    unmapped: list[str] = []
    for old_row_id in old_row_ids:
        # A row the config has on the target is never remapped -- also not
        # when a new row was created from a file carrying that same id.
        new_row_id = (
            None
            if str(old_row_id) in known_rows
            else created_row_id_map.get((new_config_id, str(old_row_id)))
        )
        if new_row_id is None:
            missing = str(old_row_id) not in known_rows
            if missing and (track_unmapped or (new_config_id, str(old_row_id)) in (planned or ())):
                unmapped.append(str(old_row_id))
            new_row_ids.append(old_row_id)
            continue
        new_row_ids.append(new_row_id)
        if new_row_id != str(old_row_id):
            remap.config_row_ids += 1
    task["configRowIds"] = new_row_ids
    if unmapped:
        listed = ", ".join(unmapped)
        remap.unmapped_rows.append(
            f"Task '{task_entry.get('name', '')}' (id {task_entry.get('id', '')}) lists "
            f"configRowIds {listed}, which have no row under {component_id}/{new_config_id} "
            "yet, so they keep the old ids. The next 'kbagent sync push' that creates the "
            "row sets them."
        )


def _remap_schedule_target(target: Any, created_id_map: dict[tuple[str, str], str]) -> int:
    """Rewrite a schedule's ``target.configurationId`` in place; return 1 if it changed."""
    if not isinstance(target, dict):
        return 0
    comp = target.get("componentId")
    config_id = target.get("configurationId")
    if not isinstance(comp, str) or not isinstance(config_id, (str, int)):
        return 0
    new_id = created_id_map.get((comp, str(config_id)))
    if not new_id or new_id == str(config_id):
        return 0
    target["configurationId"] = new_id
    return 1


def _apply_run_target_binding(
    service: SyncService,
    client: Any,
    *,
    created: CreatedConfig,
    local_data: dict[str, Any],
    manifest: Manifest,
    branch_id: int | None,
    warnings: list[dict[str, Any]],
) -> None:
    """PUT a remapped flow / orchestration / schedule, rewrite local ``_config.yml``, refresh hashes.

    ``local_data`` already carries the remapped ids (the caller mutated
    ``_configuration_extra`` in place). A deep copy is code-merged to build the
    PUT body (no-op for these components, which carry no code) so the API
    receives the corrected configuration. The local file is rewritten even when
    the PUT fails, and the manifest hash is then left as it was: the next push
    sees a modified config and sends the ids again.
    """
    merged = copy.deepcopy(local_data)
    merge_code_files(created.component_id, merged, created.config_dir)
    _name, _description, configuration = local_config_to_api(merged)

    try:
        response = client.update_config(
            component_id=created.component_id,
            config_id=created.config_id,
            configuration=configuration,
            change_description="Remap linked config IDs via kbagent sync push",
            branch_id=branch_id,
        )
    finally:
        service._write_config_file(created.config_dir, local_data)
    logger.info(
        "Remapped linked config IDs for %s/%s",
        created.component_id,
        created.config_id,
    )
    _refresh_binding_hashes(
        service,
        client,
        created=created,
        manifest=manifest,
        branch_id=branch_id,
        response=response,
        warnings=warnings,
    )
