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

An ``ignored`` entry is deleted even when edited, except a shared SQL
workspace (CLI-25): turning ``syncWorkspaces`` off must not delete SQL typed
into its ``_config.yml`` and never pushed. Plain pull and ``--force`` keep such
an entry and report it as ``skipped``; only ``--theirs`` deletes it.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..constants import CONFIG_FILENAME
from ..sync.branch_scope import branch_tree_path, find_untracked_configs
from ..sync.manifest import Manifest, ManifestConfigRow, ManifestConfiguration
from ._sync_baseline import extras_modified
from ._sync_workspace import SANDBOXES_COMPONENT_ID

if TYPE_CHECKING:
    from .sync_service import SyncService

logger = logging.getLogger(__name__)

REMOTE_DELETED_REASON = "locally modified, deleted on remote"
WORKSPACE_SYNC_OFF_REASON = (
    "locally modified, workspace sync is off; set syncWorkspaces and push, or pull --theirs"
)


@dataclass
class StaleEntry:
    """A manifest entry the current pull no longer produces."""

    entry: ManifestConfiguration
    action: str  # "removed" (gone from the remote) or "ignored" (component ignored)
    locally_modified: bool

    @property
    def keep_reason(self) -> str:
        """The ``skipped`` reason pull reports when it keeps this edited entry."""
        return REMOTE_DELETED_REASON if self.action == "removed" else WORKSPACE_SYNC_OFF_REASON


@dataclass
class PullEntries:
    """``manifest.configurations`` split for a pull of one branch tree.

    Attributes:
        own: Entries of the pulled branch -- the only ones pull looks up,
            refreshes, or sweeps as stale.
        kept: Entries of another branch that pull carries over unchanged:
            promote entries (``push --branch DEV`` with no ``dev/`` tree),
            whose files live in the pulled tree and none in their own. They
            map a directory to its config on the other branch; dropping them
            made the next promote create that config on the branch again.
    """

    own: list[ManifestConfiguration]
    kept: list[ManifestConfiguration]


def partition_pull_entries(
    manifest: Manifest, project_root: Path, branch_dir_name: str
) -> PullEntries:
    """Split the manifest into the pulled branch's entries and the ones to keep.

    Another branch's entry that is neither is dropped, as before: its files
    live in its own tree, which this pull re-targets away from (issue #649).
    Pull never deletes a file because of another branch's entry.
    """
    own: list[ManifestConfiguration] = []
    kept: list[ManifestConfiguration] = []
    for entry in manifest.configurations:
        tree = branch_tree_path(manifest, entry.branch_id)
        if tree == branch_dir_name:
            own.append(entry)
        elif (project_root / branch_dir_name / entry.path / CONFIG_FILENAME).exists() and not (
            project_root / tree / entry.path / CONFIG_FILENAME
        ).exists():
            kept.append(entry)
    return PullEntries(own=own, kept=kept)


def untracked_dirs_by_id(
    service: SyncService, project_root: Path, manifest: Manifest, branch_dir_name: str
) -> dict[str, str]:
    """``"component/config_id" -> path`` of the untracked dirs in the pulled tree.

    A config pull has no entry for -- the manifest was re-targeted to another
    branch since this tree was pulled, or the entry was lost -- still has its
    directory here, and the directory's ``_config.yml`` says which config it
    is. Pull writes into that directory instead of a new one named after the
    config: a name changed since (on either side) used to leave the old
    directory behind as an untracked copy that push then created again. An
    id carried by two directories (a copied dir) names neither.
    """
    seen: dict[str, list[str]] = {}
    for found in find_untracked_configs(
        project_root, manifest, service._read_config_file, only_branch_path=branch_dir_name
    ):
        if found["config_id"]:
            key = f"{found['component_id']}/{found['config_id']}"
            seen.setdefault(key, []).append(found["path"])
    return {key: paths[0] for key, paths in seen.items() if len(paths) == 1}


def row_dirs_by_id(service: SyncService, config_dir: Path) -> dict[str, str]:
    """``row_id -> "rows/<dir>"`` read from the row files of an untracked config dir."""
    found: dict[str, str] = {}
    rows_dir = config_dir / "rows"
    if not rows_dir.is_dir():
        return found
    for row_dir in sorted(rows_dir.iterdir()):
        meta = (service._read_config_file(row_dir) or {}).get("_keboola") or {}
        row_id = str(meta.get("row_id") or "")
        if row_id:
            found.setdefault(row_id, f"rows/{row_dir.name}")
    return found


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
        guarded = action == "removed" or entry.component_id == SANDBOXES_COMPONENT_ID
        modified = guarded and _entry_locally_modified(service, branch_dir / entry.path, entry)
        stale.append(StaleEntry(entry=entry, action=action, locally_modified=modified))
    return stale


def reserved_paths(stale: list[StaleEntry], branch_dir: Path) -> set[str]:
    """Stale paths still on disk -- a new config must not be written there (A)."""
    return {s.entry.path for s in stale if s.entry.path and (branch_dir / s.entry.path).exists()}


def remote_deleted_conflicts(stale: list[StaleEntry]) -> list[dict[str, str]]:
    """``--force`` conflicts: locally edited configs whose remote was deleted (C).

    An edited ignored workspace is no conflict (its remote still exists): it is
    kept like on a plain pull.
    """
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
        if s.locally_modified and s.action == "removed"
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
                    "reason": s.keep_reason,
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


def sweep_stale_rows(
    service: SyncService,
    config_dir: Path,
    existing_rows: dict[str, dict[str, str]],
    row_key_prefix: str,
    remote_row_ids: set[str],
    *,
    theirs: bool,
    dry_run: bool,
    rel_path: str,
    pull_details: list[dict[str, str]],
) -> list[ManifestConfigRow]:
    """Handle the tracked rows of one config that are gone from the remote.

    Pull rebuilds the row entries from the remote listing, so a row deleted on
    the remote used to lose its manifest entry while its directory stayed on
    disk. The next diff read that directory as a new row, and push created it
    again under a new id (issue #792 H). Now an unedited row directory is
    deleted (``removed``). An edited one is kept with its entry (``skipped``),
    so diff keeps reporting it as ``remote_deleted``; ``--theirs`` deletes it.

    Returns the entries to keep in the manifest.
    """
    kept: list[ManifestConfigRow] = []
    for key, tracked in existing_rows.items():
        row_id = key.removeprefix(row_key_prefix)
        if row_id == key or row_id in remote_row_ids:
            continue
        row_dir = config_dir / tracked["path"]
        row_file = row_dir / CONFIG_FILENAME
        pull_hash = tracked["pull_hash"]
        edited = bool(pull_hash) and row_file.exists() and service._file_hash(row_file) != pull_hash
        path = f"{rel_path}/{tracked['path']}"
        detail = {"component_id": row_key_prefix.split("/", 1)[0], "config_name": "", "path": path}
        if edited and not theirs:
            kept.append(
                ManifestConfigRow(
                    id=row_id,
                    path=tracked["path"],
                    metadata={
                        "pull_hash": pull_hash,
                        "pull_config_hash": tracked["pull_config_hash"],
                    },
                )
            )
            pull_details.append({**detail, "action": "skipped", "reason": REMOTE_DELETED_REASON})
            continue
        pull_details.append({**detail, "action": "removed"})
        if not dry_run and row_dir.is_dir():
            shutil.rmtree(row_dir)
    return kept
