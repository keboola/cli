"""Bookkeeping of the configs one ``sync push`` creates (extracted from sync_service.py).

After ``push_create`` POSTed a config, :func:`record_created_config` records the
new id in the manifest and saves it at once (D25) -- before the local file is
written back, before any baseline is computed. Only then the local bookkeeping
follows, and the id maps the row pass and the link backfill need are kept.

The checkpoint matters most on the promote path (``push --branch DEV`` with no
``dev/`` tree, so ``main/`` is the source): the source files keep their own ids
there (D19), so the manifest entry is the only local record that a config was
created on the dev branch. A crash between that create and the end of the push
used to leave nothing behind, and the retry created the config again. A
checkpoint that cannot be written stops the push (:class:`ManifestCheckpointError`):
nothing else is sent. A lost response (the server created the config, the
client never saw the id) is still not recoverable: no server-side idempotency
is used.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..constants import CONFIG_HASH_VERSION, CONFIG_HASH_VERSION_KEY
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..sync.branch_scope import (
    PROMOTED_FROM_KEY,
    PromotedFrom,
    branch_tree_path,
    find_branch_entry,
    same_branch,
)
from ..sync.config_format import api_config_to_local
from ..sync.diff_engine import config_hash
from ..sync.manifest import Manifest, save_manifest
from ._sync_baseline import apply_stamp, config_baseline
from ._sync_companions import merge_with_remote, record_companions
from ._sync_link_pending import carries_transformation_link
from ._sync_models import (
    CREATED_FROM_KEY,
    LINK_PENDING_KEY,
    REBIND_PENDING_KEY,
    RUN_TARGET_COMPONENTS,
    CreatedConfig,
    CreateOutcome,
)
from ._sync_untracked import register_adopted_rows
from ._sync_writeback import (
    link_promote_entries,
    link_promote_rows,
    propagate_kbc_metadata,
    writeback_after_push,
    writeback_create_config_in_manifest,
)

if TYPE_CHECKING:
    from .sync_service import SyncService

logger = logging.getLogger(__name__)


class ManifestCheckpointError(ConfigError):
    """The manifest could not be saved right after a create; push stops (D25).

    Not an ordinary per-item error: every later write would widen the gap
    between what exists remotely and what the manifest records.
    """


def is_fatal_push_error(exc: Exception) -> bool:
    """True for a push error that must stop the push instead of being collected.

    An encryption failure (a partial push would leave a plaintext secret
    somewhere) and a create the manifest could not record (D25).
    """
    if isinstance(exc, ManifestCheckpointError):
        return True
    return isinstance(exc, KeboolaApiError) and exc.error_code == ErrorCode.ENCRYPTION_FAILED


def checkpoint_manifest(
    project_root: Path,
    manifest: Manifest,
    created: str,
    *,
    promote_branch_id: int | None = None,
) -> None:
    """Save the manifest after the create of *created*, or stop the push.

    On the promote path (*promote_branch_id*) the source file keeps its
    source id, so only the manifest knew the new id: the message names the
    pull that tracks the created config in the branch's own tree instead.
    """
    try:
        save_manifest(project_root, manifest)
    except OSError as exc:
        if promote_branch_id is not None:
            recovery = (
                f"{created} exists on dev branch {promote_branch_id}, but main/ keeps the "
                f"source id, so the next `push --branch {promote_branch_id}` would create it "
                f"again. Fix the cause (permissions, disk space), then run "
                f"`kbagent sync pull --branch {promote_branch_id}`: it tracks the created "
                f"config in that branch's own tree, and later pushes to the branch read it."
            )
        else:
            recovery = (
                f"Fix the cause (permissions, disk space) before the next push: the next "
                f"push does not create {created} again only when the manifest records it "
                f"or its local file carries its id."
            )
        raise ManifestCheckpointError(
            f"{created} was created, but the manifest could not be saved ({exc}). Push "
            f"stopped before any further write. {recovery}"
        ) from exc


@dataclass
class CreateLedger:
    """What one push created, for the row pass and the link backfill.

    Attributes:
        created_id_map: ``(component_id, placeholder id)`` -> new id, captured
            before the manifest writeback overwrites a placeholder entry.
        promoted_id_map: ``(component_id, source-branch id)`` -> new id of a
            tracked config created on a branch that lacks it (promote path).
            Its rows still name the source id.
        created_configs: The configs created outside the promote path, for
            the link backfill (Phase C/D), which rewrites their local files.
            Promote creates are remapped in memory instead (D24).
    """

    created_id_map: dict[tuple[str, str], str] = field(default_factory=dict)
    promoted_id_map: dict[tuple[str, str], str] = field(default_factory=dict)
    created_configs: list[CreatedConfig] = field(default_factory=list)
    # ``(component_id, config dir path)`` -> new id of every config this push
    # created: the identity of a new config's rows until then (D7). Two new
    # directories of one component both have the empty id.
    created_by_path: dict[tuple[str, str], str] = field(default_factory=dict)
    # ``(component_id, config dir path)`` -> the placeholder its file id is,
    # for an untracked new config dir; decided once, before any create.
    placeholders: dict[tuple[str, str], str] = field(default_factory=dict)
    # ``(component_id, config dir path)`` -> the warning of a dir whose file id
    # another new dir claims too: neither maps.
    ambiguous: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    # Placeholder-claim warnings of new rows (their creates have no hook).
    row_warnings: list[dict[str, Any]] = field(default_factory=list)

    def uncreated_claims(self) -> set[tuple[str, str]]:
        """``(component_id, placeholder)`` planned for a new dir this push did not create."""
        return {
            (component_id, placeholder)
            for (component_id, path), placeholder in self.placeholders.items()
            if (component_id, path) not in self.created_by_path
        }

    def claimed_ids(self, component_id: str) -> set[str]:
        """Every placeholder this push planned for a new *component_id* dir, mapped or not."""
        mapped = {p for (comp, _), p in self.placeholders.items() if comp == component_id}
        return mapped | {
            str(w["placeholder"]) for (comp, _), w in self.ambiguous.items() if comp == component_id
        }

    @classmethod
    def plan(
        cls,
        service: SyncService,
        manifest: Manifest,
        changes: list[dict[str, Any]],
        target: CreateTarget,
    ) -> CreateLedger:
        """The ledger of a push, with the placeholder of each untracked new config and row (Astra).

        A file id is a placeholder only when no entry of the manifest owns it
        -- as its current id or as the ``created_from_id`` of a config an
        earlier push created from it -- so a copy still carrying a created
        config's placeholder never steals it. Decided before any create, so
        the push's own creates cannot change the answer. Two new dirs carrying
        the same free id: neither maps, and both creates report it. Rows the
        same way, scoped by their parent (:func:`_plan_rows`).
        """
        ledger = cls()
        if target.branch_path != branch_tree_path(manifest, target.branch_id):
            return ledger  # the promote path maps by source ids (D24)
        owned = {(e.component_id, e.id) for e in manifest.configurations} | {
            (e.component_id, str(e.metadata[CREATED_FROM_KEY]))
            for e in manifest.configurations
            if e.metadata.get(CREATED_FROM_KEY)
        }
        tracked = {
            (e.component_id, e.path)
            for e in manifest.configurations
            if same_branch(manifest, e.branch_id, target.branch_id)
        }
        claims: dict[tuple[str, str], list[str]] = {}
        for change in changes:
            key = (change["component_id"], change.get("path", ""))
            if change.get("is_row") or change["change_type"] != "added" or key in tracked:
                continue
            data = service._read_config_file(target.project_root / target.branch_path / key[1])
            file_id = str(((data or {}).get("_keboola") or {}).get("config_id") or "")
            if file_id and (key[0], file_id) not in owned:
                claims.setdefault((key[0], file_id), []).append(key[1])
        for (component_id, file_id), paths in claims.items():
            if len(paths) == 1:
                ledger.placeholders[(component_id, paths[0])] = file_id
                continue
            for path in paths:
                ledger.ambiguous[(component_id, path)] = _ambiguity_warning(
                    component_id, path, file_id, paths, "config folders"
                )
        ledger.row_warnings = _plan_rows(service, manifest, changes, target)
        return ledger

    def parent_id(self, component_id: str, parent_config_id: str) -> str:
        """The id a row change's parent has on the target after this push's creates."""
        key = (component_id, parent_config_id)
        return self.created_id_map.get(key, self.promoted_id_map.get(key, parent_config_id))

    def row_parent(self, change: dict[str, Any]) -> str:
        """The target id of a row change's parent; raises when its parent was not created.

        A row under a new config directory names that directory
        (``parent_path``, empty ``parent_config_id``): its parent is the config
        this push created there. When that create failed, or never happened,
        the row is skipped with a dependency error -- the next push creates
        the parent and then the row.
        """
        component_id = change["component_id"]
        parent_config_id = change.get("parent_config_id", "")
        parent_path = change.get("parent_path", "")
        if parent_path and not parent_config_id:
            new_id = self.created_by_path.get((component_id, parent_path))
            if not new_id:
                raise KeboolaApiError(
                    message=(
                        f"Row {parent_path}/{change.get('path', '')} was not created: its "
                        f"config {component_id} at {parent_path} was not created in this push "
                        f"(see its error). The next 'kbagent sync push' creates both."
                    ),
                    status_code=0,
                    error_code=ErrorCode.PARENT_CONFIG_NOT_TRACKED,
                )
            return new_id
        return self.parent_id(component_id, parent_config_id)


@dataclass(frozen=True)
class CreateTarget:
    """Where one push writes: the source tree it reads and the branch it writes to."""

    project_root: Path
    branch_path: str
    branch_id: int | None


def record_created_config(
    service: SyncService,
    client: Any,
    ledger: CreateLedger,
    *,
    manifest: Manifest,
    target: CreateTarget,
    component_id: str,
    source_config_id: str,
    config_path_str: str,
    outcome: CreateOutcome,
    warnings: list[dict[str, Any]],
    errors: list[dict[str, str]],
) -> str:
    """Record one created config, checkpoint, then write back and stamp; return its id.

    *source_config_id* is the id the diff change carried: the source-tree
    config a promote push created on the target, or ``""`` for a new config.
    On the promote path the entry records ``promoted_from`` (D13) and, when
    its payload references other configs, ``rebind_pending`` (D24) -- both in
    the first checkpoint, so a crash right after leaves them on disk.
    """
    result = outcome.result
    new_id = str(result.get("id", ""))
    branch_id = target.branch_id
    promote = target.branch_path != branch_tree_path(manifest, branch_id)
    writeback = writeback_create_config_in_manifest(
        manifest=manifest,
        component_id=component_id,
        branch_id=branch_id,
        config_path_str=config_path_str,
        new_id=new_id,
        file_hash="",
        cfg_hash="",
        extra_hashes={},
    )
    entry = writeback.entry
    if promote:
        association = PromotedFrom(target.branch_path, source_config_id)
        entry.metadata[PROMOTED_FROM_KEY] = association.as_metadata()
        if outcome.pending_refs:
            entry.metadata[REBIND_PENDING_KEY] = outcome.pending_refs
    else:
        # Also in the create's own checkpoint (D9): the placeholder it was
        # created from, and -- for a flow, orchestration or schedule -- that its
        # references are not rewritten yet; Phase D clears the flag.
        placeholder = writeback.previous_id or ledger.placeholders.get(
            (component_id, config_path_str), ""
        )
        if placeholder and placeholder != new_id:
            entry.metadata[CREATED_FROM_KEY] = placeholder
        if (component_id, config_path_str) in ledger.ambiguous:
            warnings.append(ledger.ambiguous[(component_id, config_path_str)])
        if component_id in RUN_TARGET_COMPONENTS or carries_transformation_link(
            component_id, outcome.configuration
        ):
            entry.metadata[LINK_PENDING_KEY] = True
        # In the create's own checkpoint: a crash right after it never leaves
        # a promote entry unlinked (risk 76).
        link_promote_entries(
            manifest,
            tree=target.branch_path,
            component_id=component_id,
            path=config_path_str,
            config_id=new_id,
        )
        link_promote_rows(manifest, tree=target.branch_path, source=entry)
    try:
        checkpoint_manifest(
            target.project_root,
            manifest,
            f"{component_id}/{new_id}",
            promote_branch_id=branch_id if promote else None,
        )
    except ManifestCheckpointError as fatal:
        # Best effort: outside the promote path the file then carries the id,
        # and the next push adopts it instead of creating it again. A failure
        # here (the same full disk) must never replace the fatal error: the
        # push still stops, the secondary failure is only noted on it.
        try:
            writeback_after_push(
                service,
                outcome.pristine_data,
                outcome.config_dir,
                outcome.file_config_id,
                outcome.configuration,
            )
        except Exception as secondary:
            logger.warning(
                "Writing the created id into %s failed: %s", outcome.config_dir, secondary
            )
            fatal.add_note(f"The local file could not be updated either: {secondary}")
        raise fatal from fatal.__cause__

    ledger.created_by_path[(component_id, config_path_str)] = new_id
    writeback_after_push(
        service,
        outcome.pristine_data,
        outcome.config_dir,
        outcome.file_config_id,
        outcome.configuration,
    )
    hashes = service._compute_config_hashes(outcome.config_dir, component_id)
    stamp = config_baseline(
        client, component_id=component_id, config_id=new_id, branch_id=branch_id, response=result
    )
    if stamp.warning is not None:
        warnings.append(stamp.warning)
    entry.metadata["pull_hash"] = hashes.file_hash
    entry.metadata["pull_config_hash"] = stamp.cfg_hash
    record_companions(entry.metadata, hashes.extra_hashes)
    apply_stamp(entry.metadata, stamp)

    if not promote and entry.metadata.get(CREATED_FROM_KEY):
        ledger.created_id_map[(component_id, str(entry.metadata[CREATED_FROM_KEY]))] = new_id
    elif writeback.previous_id:
        ledger.created_id_map[(component_id, writeback.previous_id)] = new_id
    elif source_config_id:
        ledger.promoted_id_map[(component_id, source_config_id)] = new_id
    if not promote:
        ledger.created_configs.append(
            CreatedConfig(
                component_id=component_id, config_id=new_id, config_dir=outcome.config_dir
            )
        )
    metadata_error = propagate_kbc_metadata(client, entry, branch_id)
    if metadata_error is not None:
        # The config IS on the remote; only the follow-up metadata POST
        # failed. Accumulate like any other per-change error so the rest of
        # the push continues, and surface the original cause in the envelope.
        errors.append(
            {
                "change_type": "metadata_propagation",
                "component_id": component_id,
                "config_id": new_id,
                "message": metadata_error,
            }
        )
    return new_id


def register_adopted_parent(
    service: SyncService,
    client: Any,
    *,
    manifest: Manifest,
    target: CreateTarget,
    component_id: str,
    config_id: str,
    config_path: str,
) -> None:
    """Track an adopted config so a new row can be created under it (D8).

    The directory carries the id of an existing remote config but has no
    manifest entry. Its new row needs a tracked parent. The parent is
    registered only when its files equal the remote config right now -- then
    the remote IS the baseline, exactly what a pull would record -- with its
    existing rows tracked under an unknown base. A parent that differs has no
    baseline that says which side changed it (a conflict), so the row waits:
    ``sync pull`` first.
    """
    if find_branch_entry(manifest, component_id, config_id, target.branch_id) is not None:
        return
    config_dir = target.project_root / target.branch_path / config_path
    local = service._read_config_file(config_dir)
    remote = None
    if target.branch_path == branch_tree_path(manifest, target.branch_id) and local is not None:
        remote = client.get_config_detail(
            component_id=component_id, config_id=config_id, branch_id=target.branch_id
        )
    if local is not None and isinstance(remote, dict):
        remote_local = api_config_to_local(component_id, remote, config_id)
        merge_with_remote(component_id, local, config_dir, remote_local)
        remote_hash = config_hash(remote_local)
        if config_hash(local) == remote_hash:
            hashes = service._compute_config_hashes(config_dir, component_id)
            entry = writeback_create_config_in_manifest(
                manifest=manifest,
                component_id=component_id,
                branch_id=target.branch_id,
                config_path_str=config_path,
                new_id=config_id,
                file_hash=hashes.file_hash,
                cfg_hash=remote_hash,
                extra_hashes=hashes.extra_hashes,
            ).entry
            entry.metadata[CONFIG_HASH_VERSION_KEY] = CONFIG_HASH_VERSION
            record_companions(entry.metadata, hashes.extra_hashes)
            register_adopted_rows(entry, config_dir)
            return
    raise KeboolaApiError(
        message=(
            f"Cannot create a row under {component_id}/{config_id} ({config_path}): the "
            f"directory is not tracked in the manifest and differs from that config on the "
            f"remote, so its baseline is unknown. Run 'kbagent sync pull' first."
        ),
        status_code=0,
        error_code=ErrorCode.PARENT_CONFIG_NOT_TRACKED,
    )


def _ambiguity_warning(
    component_id: str, path: str, placeholder: str, paths: list[str], what: str
) -> dict[str, Any]:
    return {
        "code": "PLACEHOLDER_AMBIGUOUS",
        "component_id": component_id,
        "path": path,
        "placeholder": placeholder,
        "message": (
            f"{len(paths)} new {what} carry the id {placeholder!r} "
            f"({', '.join(sorted(paths))}); none of them is linked by it. "
            "Give each its own id, or set the links by hand."
        ),
    }


def _plan_rows(
    service: SyncService,
    manifest: Manifest,
    changes: list[dict[str, Any]],
    target: CreateTarget,
) -> list[dict[str, Any]]:
    """Decide, once and before any create, which new row dirs a placeholder maps to (Astra).

    A new row dir (no target identity yet) under a tracked config or a new
    config dir may carry a row id in its file. It is that row's placeholder,
    scoped to the parent, only when nothing under that parent owns it -- a
    row's current id or the ``created_from_id`` an earlier push recorded --
    and no other new dir under the parent carries it too. The decision is
    written into the change's ``source_row_id``, the one identity both the
    push's in-memory row map and the row's durable ``created_from_id`` read,
    so the two can never disagree. Returns the ambiguity warnings.
    """
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    owned: set[tuple[str, str, str]] = set()
    for change in changes:
        if not change.get("is_row") or change["change_type"] != "added" or change["config_id"]:
            continue
        component_id = change["component_id"]
        parent_id = change.get("parent_config_id", "")
        parent_path = change.get("parent_path", "")
        if parent_path and not parent_id:
            scope = f"path:{parent_path}"
            row_file = target.project_root / target.branch_path / parent_path / change["path"]
            data = service._read_config_file(row_file) or {}
            placeholder = str((data.get("_keboola") or {}).get("row_id") or "")
        else:
            scope = f"id:{parent_id}"
            placeholder = str(change.get("source_row_id") or "")
            entry = find_branch_entry(manifest, component_id, parent_id, target.branch_id)
            for row in entry.rows if entry is not None else []:
                owned.add((component_id, scope, row.id))
                source = (row.metadata or {}).get(CREATED_FROM_KEY)
                if source:
                    owned.add((component_id, scope, str(source)))
        change["source_row_id"] = ""
        if placeholder:
            groups.setdefault((component_id, scope, placeholder, parent_path), []).append(change)
    warnings: list[dict[str, Any]] = []
    for (component_id, scope, placeholder, parent_path), group in groups.items():
        if (component_id, scope, placeholder) in owned:
            continue  # already owned under this parent: never claimed again
        if len(group) == 1:
            group[0]["source_row_id"] = placeholder
            continue
        paths = [f"{parent_path or scope}/{c['path']}" for c in group]
        warnings.extend(
            _ambiguity_warning(component_id, path, placeholder, paths, "row folders")
            for path in paths
        )
    return warnings
