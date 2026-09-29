"""Stale-entry sweep for ``sync pull`` (issue #792 findings A and C).

A *stale* manifest entry is one the fresh remote listing no longer produces:
the config was deleted on the remote (``removed``) or its component is now
ignored (``ignored``, issue #689). Pull drops such entries and deletes their
directories. Two data-loss paths used to hide in that sweep:

- **A** -- a config deleted and re-created under the same name got the NEW
  config written into the OLD directory (paths are chosen per pull), and the
  sweep then deleted that same directory; the next ``sync push`` deleted the
  live new config remotely. Fixed twice over: :func:`reserved_paths` keeps a
  new config from landing on a stale entry's directory, and the sweep never
  deletes a path this pull wrote.
- **C** -- a directory carrying un-pushed local edits was deleted because
  its remote vanished. Now plain pull preserves it (entry kept, reported as
  ``skipped``), ``--force`` aborts with SYNC_CONFLICT, and only ``--theirs``
  (remote wins) still deletes it.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..constants import CONFIG_FILENAME
from ..sync.manifest import ManifestConfiguration
from ._sync_baseline import extras_modified

if TYPE_CHECKING:
    from .sync_service import SyncService

logger = logging.getLogger(__name__)

REMOTE_DELETED_REASON = "locally modified, deleted on remote"


@dataclass
class StaleEntry:
    """A manifest entry the current pull no longer produces."""

    entry: ManifestConfiguration
    action: str  # "removed" (gone from the remote) or "ignored" (component ignored)
    locally_modified: bool


def _entry_locally_modified(
    service: SyncService, config_dir: Path, entry: ManifestConfiguration
) -> bool:
    """True iff ``_config.yml``, a companion file or a row file changed since pull.

    Without a recorded ``pull_hash`` there is no base to compare against, so
    the entry is not treated as modified (same conservatism as the force-pull
    conflict guard).
    """
    pull_hash = entry.metadata.get("pull_hash", "")
    config_file = config_dir / CONFIG_FILENAME
    if not pull_hash or not config_file.exists():
        return False
    if service._file_hash(config_file) != pull_hash:
        return True
    if extras_modified(service, config_dir, entry.metadata.get("pull_extra_hashes") or {}):
        return True
    for row in entry.rows:
        row_hash = row.metadata.get("pull_hash", "")
        row_file = config_dir / row.path / CONFIG_FILENAME
        if row_hash and row_file.exists() and service._file_hash(row_file) != row_hash:
            return True
    return False


def find_stale_entries(
    service: SyncService,
    entries: list[ManifestConfiguration],
    components: list[dict[str, Any]],
    branch_dir: Path,
    ignored_components: frozenset[str],
) -> list[StaleEntry]:
    """Manifest entries absent from the fresh (non-ignored) remote listing."""
    remote_keys = {
        f"{component.get('id', '')}/{cfg.get('id', '')}"
        for component in components
        if component.get("id", "") not in ignored_components
        for cfg in component.get("configurations", [])
    }
    stale: list[StaleEntry] = []
    for entry in entries:
        if f"{entry.component_id}/{entry.id}" in remote_keys:
            continue
        action = "ignored" if entry.component_id in ignored_components else "removed"
        modified = action == "removed" and _entry_locally_modified(
            service, branch_dir / entry.path, entry
        )
        stale.append(StaleEntry(entry=entry, action=action, locally_modified=modified))
    return stale


def reserved_paths(stale: list[StaleEntry], branch_dir: Path) -> set[str]:
    """Stale paths still on disk -- a new config must not be written there (A)."""
    return {s.entry.path for s in stale if s.entry.path and (branch_dir / s.entry.path).exists()}


def remote_deleted_conflicts(stale: list[StaleEntry]) -> list[dict[str, str]]:
    """``--force`` conflicts: locally edited configs whose remote was deleted (C)."""
    return [
        {
            "scope": "config",
            "component_id": s.entry.component_id,
            "config_id": s.entry.id,
            "config_name": "",
            "path": s.entry.path,
            "reason": "deleted on remote",
        }
        for s in stale
        if s.locally_modified
    ]


def _remove_dir(orphan_dir: Path, branch_dir: Path) -> None:
    """rmtree ``orphan_dir`` and prune now-empty parents up to ``branch_dir``."""
    if not (orphan_dir.exists() and orphan_dir.is_dir()):
        return
    shutil.rmtree(orphan_dir)
    logger.info("Removed orphaned directory: %s", orphan_dir)
    parent = orphan_dir.parent
    while parent != branch_dir and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
        logger.info("Removed empty parent directory: %s", parent)
        parent = parent.parent


def apply_stale_sweep(
    stale: list[StaleEntry],
    branch_dir: Path,
    *,
    theirs: bool,
    dry_run: bool,
    new_configurations: list[ManifestConfiguration],
    pull_details: list[dict[str, str]],
) -> None:
    """Report stale entries and delete their directories, safely.

    A locally edited ``removed`` entry is preserved unless ``--theirs``: its
    manifest entry is carried over unchanged (so the manifest still matches
    disk) and it is reported as ``skipped``. ``--force`` never reaches that
    branch -- the conflict guard has already aborted. A path an entry of this
    pull owns (written or kept by the fetch loop) is never deleted.
    """
    live_paths = {c.path for c in new_configurations}
    for s in stale:
        if s.locally_modified and not theirs:
            new_configurations.append(s.entry)
            pull_details.append(
                {
                    "action": "skipped",
                    "component_id": s.entry.component_id,
                    "config_name": s.entry.path,
                    "path": s.entry.path,
                    "reason": REMOTE_DELETED_REASON,
                }
            )
            continue
        pull_details.append(
            {
                "action": s.action,
                "component_id": s.entry.component_id,
                "config_name": "",
                "path": s.entry.path,
            }
        )
        if not dry_run and s.entry.path and s.entry.path not in live_paths:
            _remove_dir(branch_dir / s.entry.path, branch_dir)
