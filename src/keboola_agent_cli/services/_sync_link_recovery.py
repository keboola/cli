"""Finish a transformation's variables / shared-code links in a later push (Devin 4400588270).

Phase C rebinds a created transformation's ``variables_id`` /
``variables_values_id`` and ``shared_code_id`` / ``shared_code_row_ids``
from the placeholders to the ids created in the same push. When the
referenced row failed to create (or the push died before Phase C), the
transformation kept the placeholders for good: the next push created the
row, but Phase C only looked at the configs created in that push.

Now a created config that carries such a link records ``link_pending`` in its
create checkpoint (like a flow, D9). After the in-push passes, every pending
transformation is finished from the durable maps of the manifest
(:func:`build_link_maps`), the same way Phase D recovers a flow (#929 D32):
the work is derived from the REMOTE configuration -- a link changed in the UI
since is no source-form id and stays -- every target it will name is read
first (:func:`validate_targets`, cached for the push), only its link keys are
rewritten, and only ``configuration`` is PUT. A target that is gone or cannot
be read is never written. The local file is rewritten only when it held what
the remote held. The flag is settled from the remote content: cleared once no
link is left in source form (also one repaired in the UI, with no write), kept
while one still names a config or row the target does not have.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import ErrorCode, KeboolaApiError
from ..sync.branch_scope import find_branch_entry
from ..sync.config_format import api_config_to_local
from ..sync.diff_engine import config_hash
from ..sync.manifest import Manifest, ManifestConfiguration
from ._sync_bindings import (
    SharedCodeLink,
    _local_hash,
    _refresh_binding_hashes,
    _repoint_shared_code,
    _rewrite_list_placeholders,
    _rewrite_script_files,
    put_configuration_only,
    resolve_transformation_bindings,
)
from ._sync_link_pending import (
    LinkMaps,
    build_link_maps,
    link_pending_entries,
    set_link_pending,
    validate_targets,
)
from ._sync_models import (
    FALLBACK_LINKS_KEY,
    RUN_TARGET_COMPONENTS,
    SHARED_CODE_COMPONENT_ID,
    VARIABLES_COMPONENT_ID,
    CreatedConfig,
    VariableBindingResult,
)

if TYPE_CHECKING:
    from ._sync_push_create import CreateLedger
    from .sync_service import SyncService

_LINK_CHANGE_TYPES = frozenset({"variable_link", "shared_code_link"})
_RECOVER_DESCRIPTION = "Resolve transformation links via kbagent sync push"


@dataclass
class TransformationLinks:
    """What one pending transformation's links resolve to now.

    Attributes:
        variables_id: The new ``variables_id``, or ``None`` when it stays.
        variables_values_id: The new ``variables_values_id``, or ``None``.
        shared_code: The re-pointed shared-code link, or ``None``.
        parents: Target ids of the variables / shared-code configs named.
        unresolved: One message per link that names a config or row the
            target does not have (yet).
    """

    variables_id: str | None = None
    variables_values_id: str | None = None
    shared_code: SharedCodeLink | None = None
    parents: set[str] = field(default_factory=set)
    unresolved: list[str] = field(default_factory=list)
    # A link to a target whose read failed: pending, reported by that read.
    blocked: bool = False

    @property
    def pending(self) -> bool:
        return bool(self.unresolved) or self.blocked

    @property
    def rewrites(self) -> bool:
        return bool(self.variables_id or self.variables_values_id or self.shared_code)


def resolve_transformation_links(
    service: SyncService,
    client: Any,
    *,
    ledger: CreateLedger,
    created_row_id_map: dict[tuple[str, str], str],
    created_rows_by_parent: dict[str, list[str]],
    manifest: Manifest,
    branch_id: int | None,
    source_dir: Path | None,
) -> VariableBindingResult:
    """Phase C: the in-push link backfill, then the pending links of earlier pushes.

    *ledger* carries this push's creates and its planned placeholder claims
    (:meth:`CreateLedger.plan`): a variables id any of them claims -- also one
    whose create failed -- is never bound to another config by the fallback.
    """
    created_configs = ledger.created_configs
    created_id_map = ledger.created_id_map
    result = resolve_transformation_bindings(
        service,
        client,
        created_configs=created_configs,
        created_id_map=created_id_map,
        created_row_id_map=created_row_id_map,
        created_rows_by_parent=created_rows_by_parent,
        manifest=manifest,
        branch_id=branch_id,
        planned_claims=ledger.claimed_ids(VARIABLES_COMPONENT_ID),
    )
    result.warnings.extend(ledger.row_warnings)
    result.targets.uncreated_claims = ledger.uncreated_claims()
    if source_dir is None:  # promote path: its creates are remapped in memory (D24)
        return result
    failed = {
        (str(e.get("component_id")), str(e.get("config_id")))
        for e in result.errors
        if e.get("change_type") in _LINK_CHANGE_TYPES
    }
    for key in failed:  # an in-push link that failed stays pending (no second error)
        entry = find_branch_entry(manifest, key[0], key[1], branch_id)
        result.pending_changed |= set_link_pending(entry, True)
    pending = [
        entry
        for entry in link_pending_entries(manifest, branch_id)
        if entry.component_id not in RUN_TARGET_COMPONENTS
        and (entry.component_id, entry.id) not in failed
    ]
    if not pending:
        return result
    maps = build_link_maps(
        manifest,
        branch_id,
        created_id_map=created_id_map,
        created_row_id_map=created_row_id_map,
        source_dir=source_dir,
        uncreated_claims=result.targets.uncreated_claims,
    )
    configs = [CreatedConfig(e.component_id, e.id, source_dir / e.path) for e in pending]
    # Recovery starts from the REMOTE (#929 D32): its links are what is
    # rewritten, and every target they name is read first (shared cache).
    remotes = {c.config_id: _read_remote(client, c, branch_id, result) for c in configs}
    referenced: set[str] = set()
    entries = {
        c.config_id: find_branch_entry(manifest, c.component_id, c.config_id, branch_id)
        for c in configs
    }
    for config_id, remote in remotes.items():
        if remote is not None:
            own = _with_fallbacks(maps, entries[config_id])
            referenced |= resolve_links(remote["configuration"], own).parents
    result.errors.extend(
        validate_targets(
            client,
            manifest,
            branch_id,
            maps,
            result.targets,
            referenced=referenced,
            fresh_rows=set(created_row_id_map.values()),
        )
    )
    for created in configs:
        remote = remotes[created.config_id]
        entry = entries[created.config_id]
        own = _with_fallbacks(maps, entry)
        still = remote is None or _recover(
            service, client, created, remote, manifest, branch_id, own, result
        )
        result.pending_changed |= set_link_pending(entry, still)
        if not still and entry is not None and entry.metadata.pop(FALLBACK_LINKS_KEY, None):
            result.pending_changed = True
    return result


def _with_fallbacks(maps: LinkMaps, entry: ManifestConfiguration | None) -> LinkMaps:
    """*maps* plus the configs the singleton fallback chose for this transformation's links.

    Recorded when the fallback bound a variables link whose values row could
    not be resolved yet (Astra): without it the id the link names maps to
    nothing, and recovery would settle a dead link.
    """
    links = (entry.metadata.get(FALLBACK_LINKS_KEY) if entry is not None else None) or {}
    if not links:
        return maps
    chosen: dict[tuple[str, str], str] = {}
    blocked: set[tuple[str, str]] = set()
    for key, target in links.items():
        if "/" not in str(key):
            continue
        component_id, named = str(key).split("/", 1)
        # Validated like a durable mapping: a chosen config that is gone or
        # cannot be read is never written -- the link stays pending and the
        # record is kept for the push that can (Astra).
        if (component_id, str(target)) in maps.failed:
            blocked.add((component_id, named))
        else:
            chosen[(component_id, named)] = str(target)
    return replace(
        maps,
        configs={**maps.configs, **chosen},
        uncreated=maps.uncreated | blocked,
        failed=maps.failed | blocked,
    )


def _read_remote(
    client: Any, created: CreatedConfig, branch_id: int | None, result: VariableBindingResult
) -> dict[str, Any] | None:
    """The remote of a pending transformation; ``None`` (it stays pending) when unreadable."""
    try:
        remote = client.get_config_detail(
            component_id=created.component_id, config_id=created.config_id, branch_id=branch_id
        )
    except KeboolaApiError as exc:
        result.errors.append(_error(created, ErrorCode.API_ERROR, str(exc)))
        return None
    if not isinstance(remote, dict) or not isinstance(remote.get("configuration"), dict):
        return None
    return remote


def _target_of(
    component_id: str, raw: Any, maps: LinkMaps, links: TransformationLinks
) -> str | None:
    """The target id a link names, or ``None`` when it is not a config of this tree."""
    if not isinstance(raw, (str, int)) or raw == "":
        return None
    key = (component_id, str(raw))
    target = maps.configs.get(key)
    if target is None and key in maps.uncreated:
        if key in maps.failed:  # its failed read is the push's one error for it
            links.blocked = True
        else:
            links.unresolved.append(f"{component_id} config {raw} is not created yet.")
        return None
    if target is None and key not in maps.target_rows:
        return None  # a config outside this tree: left as it is
    links.parents.add(target or str(raw))
    return target or str(raw)


def _row_of(
    component_id: str, parent: str, raw: str, maps: LinkMaps, links: TransformationLinks
) -> str | None:
    """The target row id of *raw* under *parent*: already there, mapped, or unresolved.

    A row the parent has on the target is in target form and never remapped --
    also not when a historical mapping names it as a placeholder (the user
    selected that live row), like the flow resolver.
    """
    known = maps.target_rows.get((component_id, parent))
    if known is not None and raw in known:
        return None
    mapped = maps.rows.get((parent, raw))
    if mapped is not None:
        return mapped
    if any(key[0] == parent and row == raw for key, row in maps.rows.items()):
        return None  # already the target row created under THIS parent
    if known is not None:
        links.unresolved.append(
            f"Row {raw} of {component_id} config {parent} is not on the target."
        )
    return None


def resolve_links(extra: dict[str, Any], maps: LinkMaps) -> TransformationLinks:
    """Resolve a transformation's link keys (local ``_configuration_extra``) against *maps*."""
    links = TransformationLinks()
    raw_vars = extra.get("variables_id")
    parent = _target_of(VARIABLES_COMPONENT_ID, raw_vars, maps, links)
    if parent is not None:
        if parent != str(raw_vars):
            links.variables_id = parent
        raw_vals = extra.get("variables_values_id")
        if isinstance(raw_vals, (str, int)) and raw_vals != "":
            row = _row_of(VARIABLES_COMPONENT_ID, parent, str(raw_vals), maps, links)
            if row is not None and row != str(raw_vals):
                links.variables_values_id = row
    raw_code = extra.get("shared_code_id")
    code = _target_of(SHARED_CODE_COMPONENT_ID, raw_code, maps, links)
    if code is not None:
        row_ids = extra.get("shared_code_row_ids")
        row_map: dict[str, str] = {}
        for raw_row in row_ids if isinstance(row_ids, list) else []:
            row = _row_of(SHARED_CODE_COMPONENT_ID, code, str(raw_row), maps, links)
            if row is not None and row != str(raw_row):
                row_map[str(raw_row)] = row
        if code != str(raw_code) or row_map:
            links.shared_code = SharedCodeLink(
                config_id=code, row_id_map=row_map, unmapped_row_ids=[]
            )
    return links


def _apply_to_api(configuration: dict[str, Any], links: TransformationLinks) -> None:
    """Rewrite the link keys of an API-format configuration."""
    if links.variables_id:
        configuration["variables_id"] = links.variables_id
    if links.variables_values_id:
        configuration["variables_values_id"] = links.variables_values_id
    link = links.shared_code
    if link is not None:
        configuration["shared_code_id"] = link.config_id
        rows = configuration.get("shared_code_row_ids")
        if isinstance(rows, list):
            configuration["shared_code_row_ids"] = [link.row_id_map.get(str(r), r) for r in rows]
        if "parameters" in configuration:
            configuration["parameters"] = _rewrite_list_placeholders(
                configuration["parameters"], link.row_id_map
            )


def _apply_to_local(
    created: CreatedConfig, local_data: dict[str, Any], links: TransformationLinks
) -> None:
    extra = local_data.setdefault("_configuration_extra", {})
    if links.variables_id:
        extra["variables_id"] = links.variables_id
    if links.variables_values_id:
        extra["variables_values_id"] = links.variables_values_id
    if links.shared_code is not None:
        _repoint_shared_code(local_data, links.shared_code)
        _rewrite_script_files(created.config_dir, links.shared_code.row_id_map)


def _recover(
    service: SyncService,
    client: Any,
    created: CreatedConfig,
    remote: dict[str, Any],
    manifest: Manifest,
    branch_id: int | None,
    maps: LinkMaps,
    result: VariableBindingResult,
) -> bool:
    """Finish one pending transformation FROM ITS REMOTE CONFIGURATION; True while still pending.

    The links are resolved from the remote configuration -- a link changed in the UI
    since is not a source-form id and stays as it is -- against the
    validated *maps*. Pending is settled from that remote content: nothing
    left in source form (a link repaired in the UI included) clears it, with
    no write when nothing needs rewriting.
    """
    configuration = remote["configuration"]
    links = resolve_links(configuration, maps)
    for message in links.unresolved:
        result.errors.append(_error(created, ErrorCode.VARIABLE_LINK_UNRESOLVED, message))
    if not links.rewrites:
        return links.pending
    rewritten = copy.deepcopy(configuration)
    _apply_to_api(rewritten, links)
    local_data = service._read_config_file(created.config_dir)
    in_step = local_data is not None and _local_hash(created, local_data) == config_hash(
        api_config_to_local(created.component_id, remote, created.config_id)
    )
    try:
        response = put_configuration_only(
            client,
            created.component_id,
            created.config_id,
            rewritten,
            branch_id,
            _RECOVER_DESCRIPTION,
        )
    except KeboolaApiError as exc:
        result.errors.append(_error(created, ErrorCode.API_ERROR, str(exc)))
        return True
    result.configs_rewritten += 1
    if links.shared_code is not None:
        result.shared_code_links += 1
    if in_step and local_data is not None:
        _apply_to_local(created, local_data, links)
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
    return links.pending


def _error(created: CreatedConfig, error_code: ErrorCode, message: str) -> dict[str, str]:
    return {
        "change_type": "variable_link",
        "error_code": error_code,
        "component_id": created.component_id,
        "config_id": created.config_id,
        "message": f"{message} The link is finished by the next push once it exists.",
    }
