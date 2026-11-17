"""Two-phase removal of stale config and row directories in ``sync pull`` (#827, item 11).

Pull deletes the directory of a config (or row) the remote no longer lists.
It used to delete it first and drop the manifest entry only in its final
save, so a pull that died in between left an entry pointing at a missing
directory. If the config (or row) was then restored in the UI, the next
``push --force`` read the missing directory as a local delete and deleted it
again.

Now the removal is journalled (:class:`RemovalJournal`):

1. While it plans, pull only RECORDS each directory to delete -- its tree,
   path, identity, the hash of its ``_config.yml`` and a digest of every file
   in it.
2. Before deleting anything, it saves the manifest WITHOUT those entries and
   rows and WITH the records (``pendingRemovals``).
3. It deletes the directories; its final save keeps only the records whose
   delete failed.

A record that survives (a crash, a failed delete) makes diff and push treat
the path and everything below it as absent (``sync.manifest.under_pending``):
never a move target, never an untracked config to create or adopt, never a
new row. The next pull finishes it FIRST, before it allocates any path
(:func:`recover_pending_removals`): the directory is deleted only while it is
still exactly what pull decided to delete -- same ``_config.yml``, same files,
no symbolic link, no other tracked config inside. Anything else (an edit made
after the decision, a config written there since) is kept as an untracked
directory and reported; the record is dropped either way.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..constants import CONFIG_FILENAME
from ..sync.branch_scope import entry_tree
from ..sync.manifest import Manifest, ManifestPendingRemoval, save_manifest
from ._sync_stale import holds, scan_owned_tree

logger = logging.getLogger(__name__)

KEPT_REASON_CHANGED = "it changed after the pull that removed its config decided to delete it"
KEPT_REASON_LINK = "it is or contains a symbolic link; kbagent does not delete through it"


def tree_digest(directory: Path) -> str:
    """Digest of every file below *directory* (paths and bytes), links not followed."""
    digest = hashlib.sha256()
    if not directory.is_dir() or directory.is_symlink():
        return ""
    for root, dirs, files in directory.walk():
        dirs[:] = sorted(d for d in dirs if not (root / d).is_symlink())
        for name in sorted(files):
            path = root / name
            digest.update(path.relative_to(directory).as_posix().encode("utf-8") + b"\0")
            digest.update(b"L" if path.is_symlink() else path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _file_hash(directory: Path) -> str:
    config_file = directory / CONFIG_FILENAME
    if not config_file.is_file():
        return ""
    return hashlib.sha256(config_file.read_bytes()).hexdigest()


@dataclass
class RemovalJournal:
    """The directories one pull deletes, journalled before the delete (D10).

    Attributes:
        project_root: The sync working tree.
        tree: The pulled branch tree.
        planned: Records of the directories to delete, in planning order.
        failed: Records whose delete failed; the final save keeps them.
        warnings: ``warnings[]`` entries for the pull result.
    """

    project_root: Path
    tree: str
    planned: list[ManifestPendingRemoval] = field(default_factory=list)
    failed: list[ManifestPendingRemoval] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def plan(
        self, path: str, kind: str, *, component_id: str, config_id: str, row_id: str = ""
    ) -> None:
        """Record *path* (relative to the tree) for deletion; nothing is deleted yet."""
        directory = self.project_root / self.tree / path
        if not directory.is_dir():
            return
        self.planned.append(
            ManifestPendingRemoval(
                tree=self.tree,
                path=path,
                kind=kind,
                componentId=component_id,
                configId=config_id,
                rowId=row_id,
                fileHash=_file_hash(directory),
                treeHash=tree_digest(directory),
            )
        )

    def commit(self, manifest: Manifest) -> None:
        """Checkpoint the manifest without the removed entries, then delete (D10).

        The checkpoint is *manifest* as it was before this pull, minus the
        entries and rows planned for removal, plus their records. The final
        save of the pull keeps :attr:`failed` only.
        """
        if not self.planned:
            return
        checkpoint = manifest.model_copy(deep=True)
        for item in self.planned:
            _drop_tracked(checkpoint, item)
        checkpoint.pending_removals = [*checkpoint.pending_removals, *self.planned]
        save_manifest(self.project_root, checkpoint)
        for item in self.planned:
            try:
                _remove(self.project_root / item.tree, item)
            except OSError as exc:
                self.failed.append(item)
                self.warnings.append(_failed_warning(item, exc))


def _drop_tracked(manifest: Manifest, item: ManifestPendingRemoval) -> None:
    """Remove the entry (or the row) *item* records from *manifest*."""
    in_tree = [c for c in manifest.configurations if entry_tree(manifest, c) == item.tree]
    if item.kind == "row":
        for cfg in in_tree:
            if cfg.component_id == item.component_id and cfg.id == item.config_id:
                cfg.rows = [r for r in cfg.rows if r.id != item.row_id]
        return
    drop = {
        id(c)
        for c in in_tree
        if c.component_id == item.component_id and c.id == item.config_id and c.path == item.path
    }
    manifest.configurations = [c for c in manifest.configurations if id(c) not in drop]


def _remove(tree_dir: Path, item: ManifestPendingRemoval) -> None:
    """Delete *item*'s directory; a config's now-empty parents up to the tree go too."""
    directory = tree_dir / item.path
    if not directory.is_dir():
        return
    shutil.rmtree(directory)
    logger.info("Removed directory: %s", directory)
    if item.kind == "row":
        return
    parent = directory.parent
    while parent != tree_dir and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def _failed_warning(item: ManifestPendingRemoval, exc: OSError) -> dict[str, Any]:
    return {
        "code": "PENDING_REMOVAL_FAILED",
        "path": f"{item.tree}/{item.path}",
        "message": (
            f"{item.tree}/{item.path} could not be deleted ({exc}). Its manifest entry is "
            "gone; diff and push ignore the directory until the next `kbagent sync pull` "
            "deletes it."
        ),
    }


def _keep_reason(
    project_root: Path, manifest: Manifest, item: ManifestPendingRemoval
) -> str | None:
    """Why the recorded directory must not be deleted now, or ``None`` (D11, R15, R16)."""
    tree_dir = project_root / item.tree
    directory = tree_dir / item.path
    others = frozenset(
        tree_dir / c.path
        for c in manifest.configurations
        if c.path and entry_tree(manifest, c) == item.tree
    )
    owned = scan_owned_tree(directory, others)
    if owned.is_link or owned.symlinks:
        return KEPT_REASON_LINK
    holder = next((o for o in sorted(others) if holds(directory, o)), None)
    if holder is not None:
        return f"it holds the tracked config directory {holder.relative_to(tree_dir).as_posix()}"
    if _file_hash(directory) != item.file_hash or tree_digest(directory) != item.tree_hash:
        return KEPT_REASON_CHANGED
    return None


def recover_pending_removals(
    project_root: Path, manifest: Manifest, *, dry_run: bool
) -> list[dict[str, Any]]:
    """Finish or abandon the removals an earlier pull left (D11); the ``warnings[]`` entries.

    Runs at the start of a pull, before any path is allocated. A directory
    still exactly as recorded is deleted; any other is kept as an untracked
    directory and reported. Each record is dropped, except one whose delete
    fails again. The manifest is saved right away, so a pull that stops later
    (a conflict) does not leave the records hiding a kept directory from
    diff. A dry run changes nothing.
    """
    if not manifest.pending_removals or dry_run:
        return []
    warnings: list[dict[str, Any]] = []
    remaining: list[ManifestPendingRemoval] = []
    for item in manifest.pending_removals:
        directory = project_root / item.tree / item.path
        if not directory.exists() and not directory.is_symlink():
            continue  # deleted before the crash
        reason = _keep_reason(project_root, manifest, item)
        if reason is not None:
            warnings.append(
                {
                    "code": "PENDING_REMOVAL_KEPT",
                    "path": f"{item.tree}/{item.path}",
                    "message": (
                        f"{item.tree}/{item.path} was not deleted: {reason}. An earlier pull "
                        "removed its config; the directory is now untracked. Delete it "
                        "yourself if you do not need it."
                    ),
                }
            )
            continue
        try:
            _remove(project_root / item.tree, item)
        except OSError as exc:
            remaining.append(item)
            warnings.append(_failed_warning(item, exc))
    manifest.pending_removals = remaining
    save_manifest(project_root, manifest)
    return warnings
