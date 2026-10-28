"""Config references on the promote path (D24).

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
  at once -- so no pass can undo another.
- A config created before the configs it references (same push) is fixed by
  :func:`finish_promote_rebind` after all creates, from the payload it was
  sent with: ``main/`` is not read again.
- A created config that references others is checkpointed with
  ``rebind_pending``; a push that dies before the rebind finishes leaves the
  flag, and the next promote push rebinds it without any local edit
  (:func:`rebind_pending_entries`).
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..constants import CONFIG_FILENAME
from ..errors import ErrorCode, KeboolaApiError
from ..sync.branch_scope import find_branch_entry, promoted_from, same_branch, scope_manifest
from ..sync.diff_engine import config_hash
from ..sync.manifest import Manifest
from ._sync_bindings import _rewrite_list_placeholders, task_config_ref
from ._sync_models import (
    FLOW_COMPONENT_ID,
    ORCHESTRATOR_COMPONENT_ID,
    PROMOTED_ROW_KEY,
    REBIND_PENDING_KEY,
    REBIND_SENT_HASH_KEY,
    SCHEDULER_COMPONENT_ID,
    SHARED_CODE_COMPONENT_ID,
    VARIABLES_COMPONENT_ID,
)
from ._sync_push_ops import push_update
from ._sync_writeback import promote_shadow, stamp_updated_config

if TYPE_CHECKING:
    from ._encryption import SecretCache
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


def payload_hash(name: Any, description: Any, configuration: Any) -> str:
    """Hash of a config as sent or as stored: name, description and body.

    Encrypted values are normalized away (``config_hash``), so the stored
    ciphertext of what we sent hashes like the payload itself.
    """
    return config_hash(
        {"name": name or "", "description": description or "", "configuration": configuration}
    )


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
        expect_creates: The push still creates configs or rows, so a payload
            sent now may name one of them by its source id (D24).
        hold: Called with ``(component_id, config_id, payload hash)`` before
            such an update is PUT: it checkpoints ``rebind_pending`` first.
    """

    configs: dict[tuple[str, str], str] = field(default_factory=dict)
    rows: dict[tuple[str, str], str] = field(default_factory=dict)
    sent: dict[tuple[str, str], SentPayload] = field(default_factory=dict)
    expect_creates: bool = False
    hold: Callable[[str, str, str], None] | None = None

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

    def before_update(
        self,
        component_id: str,
        config_id: str,
        payload: dict[str, Any],
        *,
        name: Any,
        description: Any,
    ) -> None:
        """Checkpoint ``rebind_pending`` before an update that may need a later rebind (D24).

        Before the PUT, not after it: a push that dies after the PUT would
        otherwise leave a sent payload naming a source id and no record that
        its links are unfinished. The payload hash lets the next push tell
        this PUT from a real change on the target.
        """
        if self.hold is not None and self.expect_creates and has_references(component_id, payload):
            self.hold(component_id, config_id, payload_hash(name, description, payload))


def finish_promote_rebind(
    service: SyncService,
    client: Any,
    remap: PromoteRemap,
    *,
    manifest: Manifest,
    target: CreateTarget,
    known_secrets: SecretCache | None,
    warnings: list[dict[str, Any]],
    errors: list[dict[str, str]],
    diff_changes: dict[tuple[str, str], str],
    ignored_components: frozenset[str],
    allow_plaintext_fallback: bool = False,
) -> bool:
    """Finish the references of a promote push; True when the manifest changed.

    Runs after every create of the push, so a reference to a config created
    later in the same push resolves too:

    1. A config still flagged ``rebind_pending`` from an earlier push that
       died before its rebind is sent again (built from its files, remapped)
       -- only when this push's diff allows it (*diff_changes*: config change
       type by ``(component_id, config_id)``). A remote change or a conflict
       keeps the flag and is reported, never overwritten -- unless the target
       holds exactly the payload this push's own interrupted PUT sent
       (``rebind_sent_hash``). A config deleted on the target drops the flag
       (no PUT to a gone id, no endless 404s); so does an ignored component
       (ignoring is the user's decision). Only configs the diff's scope
       holds are eligible at all.
    2. Each payload sent this push that the final id map still changes is PUT
       again -- its original (source ids, secrets encrypted) remapped in
       memory with the final map; ``main/`` is not read again, so nothing
       undoes a remap, and no id is mapped twice.

    A failed PUT keeps the flag, so the next promote push rebinds again.
    """
    branch_id = target.branch_id
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
    for component_id, config_id, path in pending:
        key = (component_id, config_id)
        if key in remap.sent:
            continue
        decision = diff_changes.get(key)
        if component_id in ignored_components or decision == "remote_deleted":
            _clear_pending(manifest, component_id, config_id, branch_id)
            continue
        if key not in eligible:
            continue
        if decision in ("conflict", "remote_modified") and not _target_holds_our_put(
            client, manifest, component_id, config_id, branch_id
        ):
            warnings.append(_pending_warning(component_id, config_id, decision))
            continue
        if not (target.project_root / target.branch_path / path / CONFIG_FILENAME).exists():
            continue
        try:
            response = push_update(
                service,
                client,
                component_id,
                config_id,
                path,
                target.project_root,
                manifest,
                branch_id,
                allow_plaintext_fallback=allow_plaintext_fallback,
                warnings=warnings,
                known_secrets=known_secrets,
                remap=remap,
            )
        except KeboolaApiError as exc:
            errors.append(_link_error(component_id, config_id, exc))
            continue
        _stamp(service, client, manifest, target, component_id, config_id, path, response, warnings)
        _clear_pending(manifest, component_id, config_id, branch_id)
    for (component_id, config_id), sent in remap.sent.items():
        payload = remap.apply(component_id, sent.original)
        if payload != sent.configuration:
            try:
                response = client.update_config(
                    component_id=component_id,
                    config_id=config_id,
                    configuration=payload,
                    change_description="Remap promoted config IDs via kbagent sync push",
                    branch_id=branch_id,
                )
            except KeboolaApiError as exc:
                errors.append(_link_error(component_id, config_id, exc))
                continue
            _stamp(
                service,
                client,
                manifest,
                target,
                component_id,
                config_id,
                sent.config_path,
                response,
                warnings,
            )
        _clear_pending(manifest, component_id, config_id, branch_id)
    return bool(pending or remap.sent)


def _pending_warning(component_id: str, config_id: str, decision: str) -> dict[str, str]:
    return {
        "change_type": "promote_link_pending",
        "component_id": component_id,
        "config_id": config_id,
        "message": (
            f"{component_id}/{config_id} still names source config ids on the branch, but "
            f"it changed there since ({decision}); its links are not sent over that "
            f"change. Run 'kbagent sync pull --branch <id>' to reconcile it."
        ),
    }


def hold_before_put(
    client: Any,
    manifest: Manifest,
    target: CreateTarget,
    checkpoint: Callable[[str], None],
    component_id: str,
    config_id: str,
    sent_hash: str,
) -> None:
    """Record ``rebind_pending`` + the hash of the payload about to be PUT, and checkpoint.

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
    entry.metadata[REBIND_PENDING_KEY] = True
    entry.metadata[REBIND_SENT_HASH_KEY] = sent_hash
    checkpoint(f"{component_id}/{config_id}")


def _target_holds_our_put(
    client: Any, manifest: Manifest, component_id: str, config_id: str, branch_id: int | None
) -> bool:
    """True iff the target config is exactly what an interrupted push of ours PUT."""
    entry = find_branch_entry(manifest, component_id, config_id, branch_id)
    sent_hash = (entry.metadata.get(REBIND_SENT_HASH_KEY) if entry else "") or ""
    if not sent_hash:
        return False
    try:
        remote = client.get_config_detail(
            component_id=component_id, config_id=config_id, branch_id=branch_id
        )
    except KeboolaApiError:
        return False
    if not isinstance(remote, dict):
        return False
    stored = payload_hash(
        remote.get("name"), remote.get("description"), remote.get("configuration")
    )
    return stored == sent_hash


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
    path: str,
    response: Any,
    warnings: list[dict[str, Any]],
) -> None:
    stamp_updated_config(
        client,
        manifest=manifest,
        component_id=component_id,
        config_id=config_id,
        branch_id=target.branch_id,
        config_path_str=path,
        hashes=service._compute_config_hashes(
            target.project_root / target.branch_path / path, component_id
        ),
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
        entry.metadata.pop(REBIND_SENT_HASH_KEY, None)


def pending_rebinds(manifest: Manifest, branch_id: int | None) -> list[tuple[str, str, str]]:
    """``(component_id, config_id, path)`` of the target's entries still to rebind."""
    return [
        (entry.component_id, entry.id, entry.path)
        for entry in manifest.configurations
        if entry.metadata.get(REBIND_PENDING_KEY)
        and same_branch(manifest, entry.branch_id, branch_id)
    ]


def diff_decisions(changes: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """Config change type by ``(component_id, config_id)`` from a diff's changes."""
    return {
        (c["component_id"], c["config_id"]): c["change_type"]
        for c in changes
        if not c.get("is_row") and c.get("config_id")
    }
