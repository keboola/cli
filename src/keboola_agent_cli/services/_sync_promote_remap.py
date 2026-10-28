"""Config references on the promote path (D24, D29, D30).

``sync push --branch DEV`` with no ``dev/`` tree reads ``main/``, whose files
name PRODUCTION ids -- a flow's task ``configId``, a transformation's
``variables_id`` / ``shared_code_id``, a schedule's target. A config the
promote push created on the dev branch has another id there. The source files
keep the production ids (D19, D23), so every payload sent to the dev branch is
remapped in memory on EVERY promote push, create and update alike:

- :class:`PromoteRemap` holds source id -> target id for the configs (from the
  ``promoted_from`` of every promote entry of the target branch, plus this
  push's creates) and for their rows (``promoted_from_id`` of each row a
  promote push created), and rewrites one payload for every reference kind
  at once, each reference once from its source id.
- A config sent before a config it references was created (same push) is
  fixed by :func:`finish_promote_rebind` after all creates, from its original
  payload: ``main/`` is not read again.
- Such a config records ``rebind_pending``: the list of source references it
  names that this push creates (D30), checkpointed before its PUT. The flag is
  cleared only when every listed reference maps to a target id. A push that
  dies, or whose dependency create fails, leaves it; a later promote push
  reads the config FROM THE TARGET, rewrites only those references and PUTs
  only ``configuration`` (D29) -- every other change made on the branch since
  survives by construction.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import ErrorCode, KeboolaApiError
from ..sync.branch_scope import find_branch_entry, promoted_from, same_branch, scope_manifest
from ..sync.config_format import api_config_to_local
from ..sync.diff_engine import config_hash
from ..sync.manifest import Manifest
from ._data_app_bodies import DATA_APP_COMPONENT_ID
from ._sync_baseline import apply_stamp, config_baseline
from ._sync_bindings import _rewrite_list_placeholders, task_config_ref
from ._sync_models import (
    FLOW_COMPONENT_ID,
    ORCHESTRATOR_COMPONENT_ID,
    PROMOTED_ROW_KEY,
    REBIND_PENDING_KEY,
    SCHEDULER_COMPONENT_ID,
    SHARED_CODE_COMPONENT_ID,
    VARIABLES_COMPONENT_ID,
)
from ._sync_writeback import promote_shadow, stamp_updated_config

if TYPE_CHECKING:
    from ._sync_push_create import CreateTarget
    from .sync_service import SyncService


_RUN_TARGET_COMPONENTS = frozenset(
    {FLOW_COMPONENT_ID, ORCHESTRATOR_COMPONENT_ID, SCHEDULER_COMPONENT_ID}
)
_LINK_KEYS = ("variables_id", "shared_code_id")


def has_references(component_id: str, configuration: dict[str, Any]) -> bool:
    """True iff the payload carries a reference kind the remap rewrites."""
    if component_id in _RUN_TARGET_COMPONENTS:
        return True
    return any(configuration.get(key) for key in _LINK_KEYS)


@dataclass
class SentPayload:
    """What one config was sent to the target with, and where its files are.

    ``original`` is the payload before any remap (source ids, secrets
    encrypted); every remap starts from it, so a reference is mapped exactly
    once from its source identity, never again from an id already mapped.
    """

    config_dir: Path
    config_path: str
    original: dict[str, Any]
    configuration: dict[str, Any]


@dataclass
class PromoteRemap:
    """Source id -> target id for one promote push, and the payloads it sent.

    Attributes:
        configs: ``(component_id, source id)`` -> target config id.
        rows: ``(target parent id, source row id)`` -> target row id.
        sent: ``(component_id, target id)`` -> the payload a config that
            references others was sent with this push.
        expected: Source keys of the configs this push plans to create.
        expected_rows: Source ids of the rows this push plans to create.
        hold: Called with ``(component_id, config_id, pending references)``
            before an update naming one of them is PUT: it checkpoints
            ``rebind_pending`` first.
    """

    configs: dict[tuple[str, str], str] = field(default_factory=dict)
    rows: dict[tuple[str, str], str] = field(default_factory=dict)
    sent: dict[tuple[str, str], SentPayload] = field(default_factory=dict)
    expected: set[tuple[str, str]] = field(default_factory=set)
    expected_rows: set[str] = field(default_factory=set)
    hold: Callable[[str, str, list[list[str]]], None] | None = None

    def plan(self, changes: list[dict[str, Any]]) -> None:
        """Note what this push creates: a reference to it resolves only after the create."""
        added = [c for c in changes if c["change_type"] == "added"]
        self.expected = {
            (c["component_id"], c["config_id"])
            for c in added
            if not c.get("is_row") and c.get("config_id")
        }
        self.expected_rows = {
            str(c["config_id"]) for c in added if c.get("is_row") and c.get("config_id")
        }

    def pending_refs(self, component_id: str, original: dict[str, Any]) -> list[list[str]]:
        """The source references in *original* to configs or rows this push creates.

        ``[component, config id]`` or ``[component, config id, row id]``. They
        may still name the source on the target after the first PUT; the list
        is what ``rebind_pending`` records (D30).
        """
        refs: list[list[str]] = []
        for (component, config_id), row_ids in _references(component_id, original):
            if (component, config_id) in self.expected:
                refs.append([component, config_id])
            refs += [[component, config_id, r] for r in row_ids if r in self.expected_rows]
        return refs

    def resolved(self, refs: list[Any]) -> bool:
        """True iff every listed source reference maps to a target id (D30)."""
        for ref in refs:
            if not isinstance(ref, list) or len(ref) < 2:
                continue
            key = (str(ref[0]), str(ref[1]))
            if len(ref) == 2:
                if key not in self.configs:
                    return False
            elif (self.configs.get(key, key[1]), str(ref[2])) not in self.rows:
                return False
        return True

    def subset(self, refs: list[Any]) -> PromoteRemap:
        """A remap that rewrites only the listed source references."""
        sub = PromoteRemap()
        for ref in refs:
            if not isinstance(ref, list) or len(ref) < 2:
                continue
            key = (str(ref[0]), str(ref[1]))
            parent = self.configs.get(key, key[1])
            if len(ref) == 2 and key in self.configs:
                sub.configs[key] = parent
            elif len(ref) == 3 and (parent, str(ref[2])) in self.rows:
                sub.rows[(parent, str(ref[2]))] = self.rows[(parent, str(ref[2]))]
        return sub

    def refresh(
        self,
        manifest: Manifest,
        branch_id: int | None,
        created_configs: dict[tuple[str, str], str],
        created_rows: dict[tuple[str, str], str],
    ) -> None:
        """Rebuild the id maps from the target's promote entries and this push's creates."""
        configs: dict[tuple[str, str], str] = {}
        rows: dict[tuple[str, str], str] = {}
        for entry in manifest.configurations:
            if not same_branch(manifest, entry.branch_id, branch_id):
                continue
            association = promoted_from(entry)
            if association is not None and association.config_id not in ("", entry.id):
                configs[(entry.component_id, association.config_id)] = entry.id
            for row in entry.rows:
                source = str((row.metadata or {}).get(PROMOTED_ROW_KEY) or "")
                if source and source != row.id:
                    rows[(entry.id, source)] = row.id
        configs.update(created_configs)
        rows.update(created_rows)
        self.configs, self.rows = configs, rows

    def apply(self, component_id: str, configuration: dict[str, Any]) -> dict[str, Any]:
        """A copy of *configuration* with every known source id replaced.

        *configuration* must hold SOURCE ids (an original payload): every
        reference is looked up once, by its source identity. Applying it to an
        already remapped payload could map a target id again when source and
        target ids overlap.
        """
        out = copy.deepcopy(configuration)
        if component_id in _RUN_TARGET_COMPONENTS:
            self._remap_run_targets(component_id, out)
        self._remap_variables(out)
        self._remap_shared_code(out)
        if out == configuration:
            return configuration
        return out

    def _remap_run_targets(self, component_id: str, out: dict[str, Any]) -> None:
        """Map a schedule's target, or each task's ``configId`` and ``configRowIds``.

        A task's rows are mapped under the config it runs on the target, each
        from its source row id, once.
        """
        if component_id == SCHEDULER_COMPONENT_ID:
            target = out.get("target")
            if isinstance(target, dict) and isinstance(target.get("componentId"), str):
                key = (target["componentId"], str(target.get("configurationId", "")))
                if key in self.configs:
                    target["configurationId"] = self.configs[key]
            return
        tasks = out.get("tasks")
        for task_entry in tasks if isinstance(tasks, list) else []:
            ref = task_config_ref(component_id, task_entry)
            if ref is None:
                continue
            task = task_entry["task"]
            parent = self.configs.get(ref, ref[1])
            if parent != ref[1]:
                task["configId"] = parent
            row_ids = task.get("configRowIds")
            if isinstance(row_ids, list):
                task["configRowIds"] = [
                    self.rows.get((parent, str(row_id)), row_id) for row_id in row_ids
                ]

    def _remap_variables(self, out: dict[str, Any]) -> None:
        variables_id = out.get("variables_id")
        if not isinstance(variables_id, (str, int)) or variables_id == "":
            return
        parent = self.configs.get((VARIABLES_COMPONENT_ID, str(variables_id)))
        if parent is not None:
            out["variables_id"] = parent
        values_id = out.get("variables_values_id")
        if isinstance(values_id, (str, int)) and values_id != "":
            new_values = self.rows.get((parent or str(variables_id), str(values_id)))
            if new_values is not None:
                out["variables_values_id"] = new_values

    def _remap_shared_code(self, out: dict[str, Any]) -> None:
        shared_code_id = out.get("shared_code_id")
        if not isinstance(shared_code_id, (str, int)) or shared_code_id == "":
            return
        parent = self.configs.get((SHARED_CODE_COMPONENT_ID, str(shared_code_id)))
        if parent is not None:
            out["shared_code_id"] = parent
        target = parent or str(shared_code_id)
        row_ids = out.get("shared_code_row_ids")
        listed = row_ids if isinstance(row_ids, list) else []
        row_map = {
            str(old): self.rows[(target, str(old))]
            for old in listed
            if (target, str(old)) in self.rows
        }
        if not row_map:
            return
        out["shared_code_row_ids"] = [row_map.get(str(r), r) for r in listed]
        if "parameters" in out:
            out["parameters"] = _rewrite_list_placeholders(out["parameters"], row_map)

    def record(
        self,
        component_id: str,
        config_id: str,
        *,
        config_dir: Path,
        config_path: str,
        original: dict[str, Any],
        configuration: dict[str, Any],
    ) -> None:
        """Keep the payload a referencing config was sent with, for the final rebind."""
        if has_references(component_id, original):
            self.sent[(component_id, config_id)] = SentPayload(
                config_dir=config_dir,
                config_path=config_path,
                original=original,
                configuration=configuration,
            )

    def before_update(self, component_id: str, config_id: str, original: dict[str, Any]) -> None:
        """Checkpoint ``rebind_pending`` before an update naming a config this push creates.

        Before the PUT, not after it: a push that dies after the PUT would
        otherwise leave a target config naming a source id and no record that
        its links are unfinished.
        """
        refs = self.pending_refs(component_id, original)
        if self.hold is not None and refs:
            self.hold(component_id, config_id, refs)


def finish_promote_rebind(
    service: SyncService,
    client: Any,
    remap: PromoteRemap,
    *,
    manifest: Manifest,
    target: CreateTarget,
    warnings: list[dict[str, Any]],
    errors: list[dict[str, str]],
    diff_changes: dict[tuple[str, str], str],
    ignored_components: frozenset[str],
) -> bool:
    """Finish the references of a promote push; True when the manifest changed.

    Runs after every create of the push:

    1. Each payload sent this push that the final id map still changes is PUT
       again -- its original (source ids, secrets encrypted) remapped with the
       final map; ``main/`` is not read again, and no id is mapped twice.
    2. A config still ``rebind_pending`` from an earlier push (D29) is read
       from the target, only its listed references are rewritten, and only
       ``configuration`` is PUT -- when anything changed. Its name,
       description, enabled state and every other field changed on the
       branch since survive. A config deleted on the target, or of an ignored
       component, drops the flag; one out of the diff's scope keeps it.

    A flag is cleared only when every listed reference maps to a target id
    (D30); a failed PUT or a dependency not created yet keeps it.
    """
    branch_id = target.branch_id
    for (component_id, config_id), sent in remap.sent.items():
        payload = _with_target_identity(
            component_id, remap.apply(component_id, sent.original), sent
        )
        if payload != sent.configuration:
            try:
                response = _put_configuration(client, component_id, config_id, payload, branch_id)
            except KeboolaApiError as exc:
                errors.append(_link_error(component_id, config_id, exc))
                continue
            _stamp(
                service, client, manifest, target, component_id, config_id, sent, response, warnings
            )
        _settle(manifest, remap, component_id, config_id, branch_id, warnings)
    pending = pending_rebinds(manifest, branch_id)
    eligible = {
        (cfg.component_id, cfg.id)
        for cfg in scope_manifest(
            manifest,
            target.project_root,
            target.branch_path,
            set(),
            ignored_components=ignored_components,
            target_branch_id=branch_id,
        ).in_tree
    }
    for component_id, config_id, refs in pending:
        key = (component_id, config_id)
        if key in remap.sent:
            continue
        if component_id in ignored_components or diff_changes.get(key) == "remote_deleted":
            _clear_pending(manifest, component_id, config_id, branch_id)
            continue
        if key not in eligible:
            continue
        try:
            _rebind_on_target(
                client, manifest, remap.subset(refs), component_id, config_id, branch_id
            )
        except KeboolaApiError as exc:
            errors.append(_link_error(component_id, config_id, exc))
            continue
        _settle(manifest, remap, component_id, config_id, branch_id, warnings)
    return bool(pending or remap.sent)


def _put_configuration(
    client: Any, component_id: str, config_id: str, configuration: dict[str, Any], branch_id: Any
) -> Any:
    """PUT only ``configuration``: name, description and enabled state stay as they are."""
    return client.update_config(
        component_id=component_id,
        config_id=config_id,
        configuration=configuration,
        change_description="Remap promoted config IDs via kbagent sync push",
        branch_id=branch_id,
    )


def _rebind_on_target(
    client: Any,
    manifest: Manifest,
    listed: PromoteRemap,
    component_id: str,
    config_id: str,
    branch_id: int | None,
) -> None:
    """Rewrite the listed references of the TARGET's config and PUT it if changed (D29).

    The baseline moves to the response only when the target held exactly what
    the baseline described before this PUT: then the PUT is the only change.
    Otherwise it stays, so a change made on the branch keeps showing in diff.
    """
    remote = client.get_config_detail(
        component_id=component_id, config_id=config_id, branch_id=branch_id
    )
    configuration = (remote or {}).get("configuration")
    if not isinstance(configuration, dict):
        return
    rewritten = listed.apply(component_id, configuration)
    if rewritten == configuration:
        return
    response = _put_configuration(client, component_id, config_id, rewritten, branch_id)
    entry = find_branch_entry(manifest, component_id, config_id, branch_id)
    before = config_hash(api_config_to_local(component_id, remote, config_id))
    if entry is not None and entry.metadata.get("pull_config_hash") == before:
        stamp = config_baseline(
            client,
            component_id=component_id,
            config_id=config_id,
            branch_id=branch_id,
            response=response,
        )
        apply_stamp(entry.metadata, stamp)


def _settle(
    manifest: Manifest,
    remap: PromoteRemap,
    component_id: str,
    config_id: str,
    branch_id: int | None,
    warnings: list[dict[str, Any]],
) -> None:
    """Clear ``rebind_pending`` when all its references resolved; else keep it and warn."""
    entry = find_branch_entry(manifest, component_id, config_id, branch_id)
    if entry is None or REBIND_PENDING_KEY not in entry.metadata:
        return
    refs = entry.metadata[REBIND_PENDING_KEY]
    if remap.resolved(refs if isinstance(refs, list) else []):
        entry.metadata.pop(REBIND_PENDING_KEY, None)
        return
    warnings.append(_dependency_warning(component_id, config_id))


def _with_target_identity(
    component_id: str, payload: dict[str, Any], sent: SentPayload
) -> dict[str, Any]:
    """Carry the target's own identity fields from the first PUT into a rebuilt payload.

    A data app's ``parameters.id`` names its Data Science app; the original
    holds the source app, the first PUT the target's (``_keep_target_app_id``
    / the create). The final backfill must not send the source app back.
    """
    if component_id != DATA_APP_COMPONENT_ID:
        return payload
    target_app = (sent.configuration.get("parameters") or {}).get("id")
    params = payload.get("parameters")
    if not target_app or not isinstance(params, dict) or params.get("id") == target_app:
        return payload
    payload = copy.deepcopy(payload)
    payload["parameters"]["id"] = target_app
    return payload


def _references(
    component_id: str, configuration: dict[str, Any]
) -> list[tuple[tuple[str, str], list[str]]]:
    """``((component_id, config_id), [row ids])`` of every reference in a source payload."""
    refs: list[tuple[tuple[str, str], list[str]]] = []
    if component_id == SCHEDULER_COMPONENT_ID:
        target = configuration.get("target")
        if isinstance(target, dict) and isinstance(target.get("componentId"), str):
            refs.append(((target["componentId"], str(target.get("configurationId", ""))), []))
    elif component_id in _RUN_TARGET_COMPONENTS:
        tasks = configuration.get("tasks")
        for task_entry in tasks if isinstance(tasks, list) else []:
            ref = task_config_ref(component_id, task_entry)
            if ref is not None:
                row_ids = task_entry["task"].get("configRowIds")
                refs.append((ref, [str(r) for r in row_ids] if isinstance(row_ids, list) else []))
    for key, component, rows_key in (
        ("variables_id", VARIABLES_COMPONENT_ID, "variables_values_id"),
        ("shared_code_id", SHARED_CODE_COMPONENT_ID, "shared_code_row_ids"),
    ):
        value = configuration.get(key)
        if isinstance(value, (str, int)) and value != "":
            rows = configuration.get(rows_key)
            row_list = rows if isinstance(rows, list) else ([rows] if rows else [])
            refs.append(((component, str(value)), [str(r) for r in row_list]))
    return refs


def _dependency_warning(component_id: str, config_id: str) -> dict[str, str]:
    return {
        "change_type": "promote_link_pending",
        "component_id": component_id,
        "config_id": config_id,
        "message": (
            f"{component_id}/{config_id} still names a config or row that does not exist on "
            f"the branch yet; the 'kbagent sync push --branch' that creates it sends the link."
        ),
    }


def hold_before_put(
    client: Any,
    manifest: Manifest,
    target: CreateTarget,
    checkpoint: Callable[[str], None],
    component_id: str,
    config_id: str,
    refs: list[list[str]],
) -> None:
    """Record ``rebind_pending`` (the references still to resolve) and checkpoint (D30).

    The target entry may not exist yet (the first write of a branched config
    on the promote path); its shadow is recorded here, so the flag has a home.
    """
    entry = find_branch_entry(
        manifest, component_id, config_id, target.branch_id
    ) or promote_shadow(
        client,
        manifest,
        component_id=component_id,
        config_id=config_id,
        branch_id=target.branch_id,
        source_tree=target.branch_path,
    )
    if entry is None:
        return
    entry.metadata[REBIND_PENDING_KEY] = refs
    checkpoint(f"{component_id}/{config_id}")


def _link_error(component_id: str, config_id: str, exc: KeboolaApiError) -> dict[str, str]:
    return {
        "change_type": "promote_link",
        "error_code": ErrorCode.LINK_UNRESOLVED,
        "component_id": component_id,
        "config_id": config_id,
        "message": f"{exc} The next 'kbagent sync push --branch' sends the links again.",
    }


def _stamp(
    service: SyncService,
    client: Any,
    manifest: Manifest,
    target: CreateTarget,
    component_id: str,
    config_id: str,
    sent: SentPayload,
    response: Any,
    warnings: list[dict[str, Any]],
) -> None:
    stamp_updated_config(
        client,
        manifest=manifest,
        component_id=component_id,
        config_id=config_id,
        branch_id=target.branch_id,
        config_path_str=sent.config_path,
        hashes=service._compute_config_hashes(sent.config_dir, component_id),
        response=response,
        warnings=warnings,
        source_tree=target.branch_path,
    )


def _clear_pending(
    manifest: Manifest, component_id: str, config_id: str, branch_id: int | None
) -> None:
    entry = find_branch_entry(manifest, component_id, config_id, branch_id)
    if entry is not None:
        entry.metadata.pop(REBIND_PENDING_KEY, None)


def pending_rebinds(manifest: Manifest, branch_id: int | None) -> list[tuple[str, str, list[Any]]]:
    """``(component_id, config_id, references)`` of the target's entries still to rebind."""
    return [
        (entry.component_id, entry.id, _listed(entry.metadata[REBIND_PENDING_KEY]))
        for entry in manifest.configurations
        if REBIND_PENDING_KEY in entry.metadata
        and same_branch(manifest, entry.branch_id, branch_id)
    ]


def _listed(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def diff_decisions(changes: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """Config change type by ``(component_id, config_id)`` from a diff's changes."""
    return {
        (c["component_id"], c["config_id"]): c["change_type"]
        for c in changes
        if not c.get("is_row") and c.get("config_id")
    }
