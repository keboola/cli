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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import ErrorCode, KeboolaApiError
from ..sync.code_extraction import merge_code_files
from ..sync.config_format import local_config_to_api
from ..sync.manifest import Manifest
from ._sync_baseline import apply_stamp, config_baseline
from ._sync_models import (
    FLOW_COMPONENT_ID,
    ORCHESTRATOR_COMPONENT_ID,
    SCHEDULER_COMPONENT_ID,
    SHARED_CODE_COMPONENT_ID,
    VARIABLES_COMPONENT_ID,
    CreatedConfig,
    FlowBindingResult,
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
    created_row_id_map: dict[tuple[str, str], str],
    created_rows_by_parent: dict[str, list[str]],
    errors: list[dict[str, str]],
) -> str | None:
    """Resolve a transformation's ``variables_values_id`` placeholder.

    Returns the row ULID, or ``None`` when no values row was created (the link
    is then left unset) or the choice is ambiguous (an error is appended only
    when ``vals_placeholder`` was actually requested).
    """
    if vals_placeholder:
        mapped = created_row_id_map.get((parent_ulid, vals_placeholder))
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
    """

    references: int = 0
    config_row_ids: int = 0
    unmapped_rows: list[str] = field(default_factory=list)


def resolve_run_target_bindings(
    service: SyncService,
    client: Any,
    *,
    created_configs: list[CreatedConfig],
    created_id_map: dict[tuple[str, str], str],
    created_row_id_map: dict[tuple[str, str], str],
    manifest: Manifest,
    branch_id: int | None,
) -> FlowBindingResult:
    """Remap the configs that flows, orchestrations and schedules run (Phase D).

    A ``keboola.flow`` or legacy ``keboola.orchestrator`` config runs other
    configs via ``configuration.tasks[].task.configId`` (and optionally only
    some rows via ``task.configRowIds``); a ``keboola.scheduler`` config runs
    ``configuration.target.configurationId``. On disk these live under
    ``_configuration_extra``. When such a config is created in the same push
    as the configs it runs -- e.g. a ``sync clone`` of a reference project --
    those ids still point at the source ids. This pass (mirroring the Phase-C
    variable backfill) resolves each reference via ``created_id_map`` /
    ``created_row_id_map`` to the ULID assigned this push, PUTs the corrected
    config, rewrites the local ``_config.yml``, and refreshes the manifest
    hashes so a re-push is clean. It never activates a schedule with the
    Scheduler service.

    A no-op when no such config was created this push, or when no task or
    target references a config created this push (the id is left untouched --
    it may legitimately point at a pre-existing config).
    """
    result = FlowBindingResult()
    for created in created_configs:
        link_type = _RUN_TARGET_LINK_TYPES.get(created.component_id)
        if link_type is None:
            continue
        local_data = service._read_config_file(created.config_dir)
        if local_data is None:
            continue
        extra = local_data.get("_configuration_extra")
        if not isinstance(extra, dict):
            continue

        remap = remap_run_targets_in_place(
            created.component_id, extra, created_id_map, created_row_id_map
        )
        for message in remap.unmapped_rows:
            result.errors.append(
                {
                    "change_type": link_type,
                    "error_code": ErrorCode.LINK_UNRESOLVED,
                    "component_id": created.component_id,
                    "config_id": created.config_id,
                    "message": message,
                }
            )
        if not remap.references:
            continue

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
                {
                    "change_type": link_type,
                    "error_code": ErrorCode.API_ERROR,
                    "component_id": created.component_id,
                    "config_id": created.config_id,
                    "message": f"{exc} {_LINK_RETRY_HINT}",
                }
            )
            continue
        result.configs_rewritten += 1
        if created.component_id == SCHEDULER_COMPONENT_ID:
            result.schedule_targets += remap.references
        elif created.component_id == ORCHESTRATOR_COMPONENT_ID:
            result.orchestrator_tasks += remap.references
        else:
            result.flow_tasks += remap.references
        result.config_row_ids += remap.config_row_ids
    return result


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
) -> RunTargetRemap:
    """Rewrite the task ``configId``s / ``configRowIds`` or the schedule target in place.

    A reference is rewritten only when its ``(componentId, configId)`` matches
    an entry created this push. A task's ``configRowIds`` are then looked up
    under that new config only.
    """
    remap = RunTargetRemap()
    if component_id == SCHEDULER_COMPONENT_ID:
        remap.references = _remap_schedule_target(extra.get("target"), created_id_map)
        return remap
    tasks = extra.get("tasks")
    if not isinstance(tasks, list):
        return remap
    for task_entry in tasks:
        ref = task_config_ref(component_id, task_entry)
        if ref is None:
            continue
        new_id = created_id_map.get(ref)
        if not new_id or ref[1] == new_id:
            continue
        task_entry["task"]["configId"] = new_id
        remap.references += 1
        _remap_task_rows(task_entry, ref[0], new_id, created_row_id_map, remap)
    return remap


def _remap_task_rows(
    task_entry: dict[str, Any],
    component_id: str,
    new_config_id: str,
    created_row_id_map: dict[tuple[str, str], str],
    remap: RunTargetRemap,
) -> None:
    """Rewrite a task's ``configRowIds`` to the rows created under its new config.

    The task's config was created this push, so each of its rows is new too.
    A row id without a new row is kept and reported in ``remap.unmapped_rows``.
    """
    task = task_entry["task"]
    old_row_ids = task.get("configRowIds")
    if not isinstance(old_row_ids, list):
        return
    new_row_ids: list[Any] = []
    unmapped: list[str] = []
    for old_row_id in old_row_ids:
        new_row_id = created_row_id_map.get((new_config_id, str(old_row_id)))
        if new_row_id is None:
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
            "created in this push, so they keep the old ids. Set the row ids by hand."
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
