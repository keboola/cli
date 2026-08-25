"""Push-time link backfill for the sync service (Phase C + Phase D).

Extracted from ``sync_service.py`` to keep that file under control. These are
free functions that take the ``SyncService`` as their first argument (for the
handful of on-disk helpers they need -- ``_read_config_file``,
``_write_config_file``, ``_compute_config_hashes``) rather than methods, so the
typing stays explicit and the binding logic is testable in isolation.

- **Phase C** (:func:`resolve_variable_bindings`): rebind a transformation's
  ``variables_id`` / ``variables_values_id`` placeholders to the ULIDs created
  this push.
- **Phase D** (:func:`resolve_flow_task_bindings`): remap ``keboola.flow`` task
  ``configId``s to the ULIDs created this push.

Both run after the create passes, PUT the corrected config, rewrite the local
``_config.yml``, and refresh the manifest hashes so a re-push is clean.
"""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING, Any

from ..errors import ErrorCode, KeboolaApiError
from ..sync.code_extraction import merge_code_files
from ..sync.config_format import local_config_to_api
from ..sync.manifest import Manifest
from ._sync_baseline import apply_stamp, config_baseline
from ._sync_models import (
    FLOW_COMPONENT_ID,
    VARIABLES_COMPONENT_ID,
    CreatedConfig,
    FlowBindingResult,
    VariableBindingResult,
)
from ._sync_push_ops import guard_script_shape

if TYPE_CHECKING:
    from .sync_service import SyncService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase C: transformation -> variables links
# ---------------------------------------------------------------------------


def resolve_variable_bindings(
    service: SyncService,
    client: Any,
    *,
    created_configs: list[CreatedConfig],
    created_id_map: dict[tuple[str, str], str],
    created_row_id_map: dict[str, str],
    created_rows_by_parent: dict[str, list[str]],
    manifest: Manifest,
    branch_id: int | None,
) -> VariableBindingResult:
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
    result = VariableBindingResult()

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
        )
        # A missing-but-required values row already recorded an error.
        if vals_placeholder and row_ulid is None:
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
                    "message": str(exc),
                }
            )
            continue
        result.configs_rewritten += 1

    return result


def _resolve_variables_parent(
    *,
    created: CreatedConfig,
    vars_placeholder: str,
    created_id_map: dict[tuple[str, str], str],
    created_variables_ulids: list[str],
    errors: list[dict[str, str]],
) -> str | None:
    """Resolve a transformation's ``variables_id`` placeholder to a ULID.

    Returns the ULID, or ``None`` when there is nothing to backfill
    (already-bound path) or the link is ambiguous (an error is appended).
    """
    parent_ulid = created_id_map.get((VARIABLES_COMPONENT_ID, vars_placeholder))
    if parent_ulid is not None:
        return parent_ulid
    if not created_variables_ulids:
        # No variables config created this push: the link is either already
        # a ULID (UPDATE path) or points outside this push. Leave it.
        return None
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


def _resolve_variables_row(
    *,
    created: CreatedConfig,
    parent_ulid: str,
    vals_placeholder: str,
    created_row_id_map: dict[str, str],
    created_rows_by_parent: dict[str, list[str]],
    errors: list[dict[str, str]],
) -> str | None:
    """Resolve a transformation's ``variables_values_id`` placeholder.

    Returns the row ULID, or ``None`` when no values row was created (the link
    is then left unset) or the choice is ambiguous (an error is appended only
    when ``vals_placeholder`` was actually requested).
    """
    if vals_placeholder:
        mapped = created_row_id_map.get(vals_placeholder)
        if mapped is not None:
            return mapped
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

    response = client.update_config(
        component_id=created.component_id,
        config_id=created.config_id,
        configuration=configuration,
        change_description="Resolve variables link via kbagent sync push",
        branch_id=branch_id,
    )
    logger.info(
        "Resolved variables link for %s/%s -> variables_id=%s variables_values_id=%s",
        created.component_id,
        created.config_id,
        parent_ulid,
        row_ulid,
    )

    # Rewrite the local _configuration_extra to the ULIDs (pristine data:
    # no merged blocks leak into _config.yml).
    extra = local_data.setdefault("_configuration_extra", {})
    extra["variables_id"] = parent_ulid
    if row_ulid:
        extra["variables_values_id"] = row_ulid
    service._write_config_file(created.config_dir, local_data)

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
    target_branch = branch_id or 0
    for cfg in manifest.configurations:
        if (
            cfg.branch_id == target_branch
            and cfg.component_id == created.component_id
            and cfg.id == created.config_id
        ):
            cfg.metadata["pull_hash"] = hashes.file_hash
            cfg.metadata["pull_extra_hashes"] = hashes.extra_hashes
            apply_stamp(cfg.metadata, stamp)
            break


# ---------------------------------------------------------------------------
# Phase D: keboola.flow task configId remap (#426)
# ---------------------------------------------------------------------------


def resolve_flow_task_bindings(
    service: SyncService,
    client: Any,
    *,
    created_configs: list[CreatedConfig],
    created_id_map: dict[tuple[str, str], str],
    manifest: Manifest,
    branch_id: int | None,
) -> FlowBindingResult:
    """Remap keboola.flow task ``configId``s from source ids to ULIDs (Phase D).

    A ``keboola.flow`` config runs other configs via
    ``configuration.tasks[].task.configId`` (job-type tasks only; on disk these
    live under ``_configuration_extra.tasks``). When a flow is created in the
    same push as the configs it targets -- e.g. a ``sync clone`` of a reference
    project -- those task ``configId``s still point at the source config ids.
    This pass (mirroring the Phase-C variable backfill) resolves each job task's
    ``(componentId, configId)`` via ``created_id_map`` to the ULID assigned this
    push, PUTs the corrected flow, rewrites the local ``_config.yml``, and
    refreshes the manifest hashes so a re-push is clean.

    A no-op when no flow was created this push, or when no task references a
    config created this push (the id is left untouched -- it may legitimately
    point at a pre-existing config).
    """
    result = FlowBindingResult()
    for created in created_configs:
        if created.component_id != FLOW_COMPONENT_ID:
            continue
        local_data = service._read_config_file(created.config_dir)
        if local_data is None:
            continue
        extra = local_data.get("_configuration_extra")
        if not isinstance(extra, dict):
            continue
        tasks = extra.get("tasks")
        if not isinstance(tasks, list):
            continue

        remapped = remap_flow_tasks_in_place(tasks, created_id_map)
        if not remapped:
            continue

        try:
            _apply_flow_task_binding(
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
                {
                    "change_type": "flow_task_link",
                    "error_code": ErrorCode.API_ERROR,
                    "component_id": created.component_id,
                    "config_id": created.config_id,
                    "message": str(exc),
                }
            )
            continue
        result.configs_rewritten += 1
        result.tasks_remapped += remapped
    return result


def remap_flow_tasks_in_place(tasks: list[Any], created_id_map: dict[tuple[str, str], str]) -> int:
    """Rewrite job-task ``configId``s in a flow ``tasks`` list in place.

    Returns the number of task references actually remapped. Only
    ``task.type == 'job'`` entries carry a ``configId``; notification and
    variable tasks are skipped. A task is rewritten only when
    ``(componentId, configId)`` matches an entry created this push.
    """
    remapped = 0
    for task_entry in tasks:
        if not isinstance(task_entry, dict):
            continue
        task = task_entry.get("task")
        if not isinstance(task, dict) or task.get("type") != "job":
            continue
        comp = task.get("componentId")
        old_cfg = task.get("configId")
        if not isinstance(comp, str) or not isinstance(old_cfg, (str, int)):
            continue
        new_id = created_id_map.get((comp, str(old_cfg)))
        if new_id and str(old_cfg) != new_id:
            task["configId"] = new_id
            remapped += 1
    return remapped


def _apply_flow_task_binding(
    service: SyncService,
    client: Any,
    *,
    created: CreatedConfig,
    local_data: dict[str, Any],
    manifest: Manifest,
    branch_id: int | None,
    warnings: list[dict[str, Any]],
) -> None:
    """PUT a remapped flow, rewrite local ``_config.yml``, refresh hashes.

    ``local_data`` already carries the remapped task ``configId``s (the caller
    mutated ``_configuration_extra.tasks`` in place). A deep copy is code-merged
    to build the PUT body (no-op for flows, which carry no code) so the API
    receives the corrected ``configuration.tasks``.
    """
    merged = copy.deepcopy(local_data)
    merge_code_files(created.component_id, merged, created.config_dir)
    _name, _description, configuration = local_config_to_api(merged)

    response = client.update_config(
        component_id=created.component_id,
        config_id=created.config_id,
        configuration=configuration,
        change_description="Remap flow task configIds via kbagent sync push",
        branch_id=branch_id,
    )
    logger.info(
        "Remapped flow task configIds for %s/%s",
        created.component_id,
        created.config_id,
    )

    service._write_config_file(created.config_dir, local_data)
    _refresh_binding_hashes(
        service,
        client,
        created=created,
        manifest=manifest,
        branch_id=branch_id,
        response=response,
        warnings=warnings,
    )
