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
from ..sync.manifest import (
    Manifest,
    ManifestConfiguration,
    ManifestPendingRemoval,
    pending_removal_paths,
    save_manifest,
)
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


def _record_path(item: ManifestPendingRemoval) -> str:
    return f"{item.tree}/{item.path}"


def _warning(code: str, item: ManifestPendingRemoval, message: str) -> dict[str, Any]:
    return {"code": code, "path": _record_path(item), "message": message}


@dataclass
class RemovalJournal:
    """The directories one pull deletes, journalled before the delete (D10).

    Attributes:
        project_root: The sync working tree.
        tree: The pulled branch tree.
        pending: Paths (relative to *tree*) still recorded by an earlier pull
            after the recovery -- their delete failed again. Pull reserves
            them and never adopts a row from them (F5).
        planned: Records of the directories to delete, in planning order.
        failed: Records whose delete failed; the final save keeps them.
        warnings: ``warnings[]`` entries for the pull result.
    """

    project_root: Path
    tree: str
    pending: frozenset[str] = frozenset()
    planned: list[ManifestPendingRemoval] = field(default_factory=list)
    failed: list[ManifestPendingRemoval] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def for_pull(cls, project_root: Path, manifest: Manifest, tree: str) -> RemovalJournal:
        """The journal of a pull of *tree*, after the recovery ran."""
        return cls(project_root, tree, pending=pending_removal_paths(manifest, tree))

    def rows_pending_under(self, config_path: str) -> frozenset[str]:
        """Pending paths below *config_path*, relative to it (row directories)."""
        prefix = f"{config_path}/"
        return frozenset(p.removeprefix(prefix) for p in self.pending if p.startswith(prefix))

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

    def commit(self, manifest: Manifest, live: list[ManifestConfiguration] | None = None) -> None:
        """Checkpoint the manifest without the removed entries, then delete (D10).

        The checkpoint is *manifest* as it was before this pull, minus the
        entries and rows planned for removal, plus their records (and the
        records an earlier pull left). Right before each delete the directory
        is checked again (F6): an unsafe path, a symbolic link, a tracked
        config or row inside -- *live* holds the entries this pull wrote --
        or any change since it was planned keeps it, with a warning.
        """
        if not self.planned:
            return
        checkpoint = manifest.model_copy(deep=True)
        for item in self.planned:
            _drop_tracked(checkpoint, item)
        checkpoint.pending_removals = [*checkpoint.pending_removals, *self.planned]
        save_manifest(self.project_root, checkpoint)
        for item in self.planned:
            unsafe = unsafe_reason(self.project_root, checkpoint, item)
            reason = unsafe or _keep_reason(self.project_root, checkpoint, item, live or [])
            if reason is not None:
                self.warnings.append(_kept_warning(item, reason))
                continue
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


def unsafe_reason(
    project_root: Path, manifest: Manifest, item: ManifestPendingRemoval
) -> str | None:
    """Why *item* must never be acted on (F1), or ``None``.

    The paths must be plain relative paths (checked at load), *tree* a branch
    tree of this manifest, no directory from the project root down to the
    target a symbolic link, and the target inside its tree. A row must lie in
    the rows folder of its configuration in that tree.
    """
    if item.problem is not None:
        return item.problem
    if item.tree not in {branch.path for branch in manifest.branches}:
        return f"its tree {item.tree!r} is not a branch tree of this manifest"
    current = project_root
    for part in [*item.tree.split("/"), *item.path.split("/")]:
        current = current / part
        if current.is_symlink():
            return f"{current.relative_to(project_root).as_posix()} is a symbolic link"
    tree_dir = (project_root / item.tree).resolve()
    if project_root.resolve() not in tree_dir.parents:
        return "its tree resolves outside the project"
    if tree_dir not in (project_root / item.tree / item.path).resolve().parents:
        return "it resolves outside its tree"
    if item.kind == "row":
        rows_prefix = manifest.naming.config_row.split("{", 1)[0]
        parents = [
            c.path
            for c in manifest.configurations
            if entry_tree(manifest, c) == item.tree
            and c.component_id == item.component_id
            and c.id == item.config_id
        ]
        if not any(
            item.path.startswith(f"{p}/{rows_prefix}")
            and len(item.path) > len(f"{p}/{rows_prefix}")
            for p in parents
        ):
            return "it is not in the rows folder of its configuration"
    elif item.kind != "config":
        return f"its kind {item.kind!r} is unknown"
    return None


def _remove(tree_dir: Path, item: ManifestPendingRemoval) -> None:
    """Delete *item*'s directory; a config's now-empty parents go too, never the tree or above."""
    directory = tree_dir / item.path
    if not directory.is_dir():
        return
    shutil.rmtree(directory)
    logger.info("Removed directory: %s", directory)
    if item.kind == "row":
        return
    parent = directory.parent
    while tree_dir in parent.parents and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def _failed_warning(item: ManifestPendingRemoval, exc: OSError) -> dict[str, Any]:
    return _warning(
        "PENDING_REMOVAL_FAILED",
        item,
        f"{_record_path(item)} could not be deleted ({exc}). Its manifest entry is gone; "
        "diff and push ignore the directory until the next `kbagent sync pull` deletes it.",
    )


def _kept_warning(item: ManifestPendingRemoval, reason: str) -> dict[str, Any]:
    return _warning(
        "PENDING_REMOVAL_KEPT",
        item,
        f"{_record_path(item)} was not deleted: {reason}. A pull removed its config or row; "
        "the directory is now untracked. Delete it yourself if you do not need it.",
    )


def _tracked_dirs(
    tree_dir: Path, entries: list[ManifestConfiguration], manifest: Manifest, tree: str
) -> frozenset[Path]:
    """Every tracked config and row directory of *tree* (for the ownership check)."""
    dirs: set[Path] = set()
    for cfg in entries:
        if not cfg.path or entry_tree(manifest, cfg) != tree:
            continue
        dirs.add(tree_dir / cfg.path)
        dirs.update(tree_dir / cfg.path / row.path for row in cfg.rows if row.path)
    return frozenset(dirs)


def _keep_reason(
    project_root: Path,
    manifest: Manifest,
    item: ManifestPendingRemoval,
    live: list[ManifestConfiguration],
) -> str | None:
    """Why the recorded directory must not be deleted now, or ``None`` (D11, R15, R16, F5, F6).

    Kept: a symbolic link in it, a tracked config or row directory at or
    inside it (one re-tracked since, by a pull or an older kbagent), or any
    change since pull decided.
    """
    tree_dir = project_root / item.tree
    directory = tree_dir / item.path
    others = _tracked_dirs(tree_dir, [*manifest.configurations, *live], manifest, item.tree)
    owned = scan_owned_tree(directory, others)
    if owned.is_link or owned.symlinks:
        return KEPT_REASON_LINK
    holder = next((o for o in sorted(others) if holds(directory, o)), None)
    if holder is not None:
        return f"it holds the tracked directory {holder.relative_to(tree_dir).as_posix()}"
    if _file_hash(directory) != item.file_hash or tree_digest(directory) != item.tree_hash:
        return KEPT_REASON_CHANGED
    return None


def recover_pending_removals(
    project_root: Path, manifest: Manifest, *, dry_run: bool
) -> list[dict[str, Any]]:
    """Finish or abandon the removals an earlier pull left (D11); the ``warnings[]`` entries.

    Runs at the start of a pull, before any path is allocated. A record with
    an unsafe path is dropped and never acted on (F1). A directory still
    exactly as recorded is deleted; any other is kept as an untracked
    directory and reported. Each record is dropped, except one whose delete
    fails again: it stays recorded (F2). The manifest is saved right away, so
    a pull that stops later (a conflict) does not leave the records hiding a
    kept directory from diff. A dry run changes nothing and lists the records
    it did not process.
    """
    if not manifest.pending_removals:
        return []
    if dry_run:
        return [
            _warning(
                "PENDING_REMOVALS_NOT_PROCESSED",
                item,
                f"{_record_path(item)} is recorded for deletion by an interrupted pull; a dry "
                "run does not process it. A real `kbagent sync pull` deletes it if it is "
                "unchanged, else keeps it untracked.",
            )
            for item in manifest.pending_removals
        ]
    warnings: list[dict[str, Any]] = []
    remaining: list[ManifestPendingRemoval] = []
    for item in manifest.pending_removals:
        unsafe = unsafe_reason(project_root, manifest, item)
        if unsafe is not None:
            warnings.append(
                _warning(
                    "PENDING_REMOVAL_INVALID",
                    item,
                    f"A pending removal record was dropped and nothing deleted: {unsafe}.",
                )
            )
            continue
        directory = project_root / item.tree / item.path
        if not directory.exists():
            continue  # deleted before the crash
        reason = _keep_reason(project_root, manifest, item, [])
        if reason is not None:
            warnings.append(_kept_warning(item, reason))
            continue
        try:
            _remove(project_root / item.tree, item)
        except OSError as exc:
            remaining.append(item)
            warnings.append(_failed_warning(item, exc))
    manifest.pending_removals = remaining
    save_manifest(project_root, manifest)
    return warnings
