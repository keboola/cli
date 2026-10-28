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

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..sync.branch_scope import PROMOTED_FROM_KEY, PromotedFrom, branch_tree_path
from ..sync.manifest import Manifest, save_manifest
from ._sync_baseline import apply_stamp, config_baseline
from ._sync_models import REBIND_PENDING_KEY, CreatedConfig, CreateOutcome
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


def checkpoint_manifest(project_root: Path, manifest: Manifest, created: str) -> None:
    """Save the manifest after the create of *created*, or stop the push."""
    try:
        save_manifest(project_root, manifest)
    except OSError as exc:
        raise ManifestCheckpointError(
            f"{created} was created, but the manifest could not be saved ({exc}). Push "
            f"stopped before any further write. Fix the cause (permissions, disk space) "
            f"before the next push: the next push does not create {created} again only "
            f"when the manifest records it or its local file carries its id."
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

    def parent_id(self, component_id: str, parent_config_id: str) -> str:
        """The id a row change's parent has on the target after this push's creates."""
        key = (component_id, parent_config_id)
        return self.created_id_map.get(key, self.promoted_id_map.get(key, parent_config_id))


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
    try:
        checkpoint_manifest(target.project_root, manifest, f"{component_id}/{new_id}")
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
    entry.metadata["pull_extra_hashes"] = hashes.extra_hashes
    apply_stamp(entry.metadata, stamp)

    if writeback.previous_id:
        ledger.created_id_map[(component_id, writeback.previous_id)] = new_id
    elif source_config_id:
        ledger.promoted_id_map[(component_id, source_config_id)] = new_id
    if not promote:
        link_promote_entries(
            manifest,
            tree=target.branch_path,
            component_id=component_id,
            path=config_path_str,
            config_id=new_id,
        )
        link_promote_rows(manifest, tree=target.branch_path, source=entry)
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
