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
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..constants import CONFIG_FILENAME, KEBOOLA_DIR_NAME
from ..sync.branch_scope import (
    entry_tree,
    pending_removal_paths,
    promoted_from,
    resolve_removal,
)
from ..sync.manifest import (
    Manifest,
    ManifestConfiguration,
    ManifestPendingRemoval,
    PendingPaths,
    save_manifest,
)
from ._sync_stale import holds, same_dir, scan_owned_tree

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
        pending_rows: ``(component_id, config_id, row folder)`` of those
            records that are rows: pull hides the row folder by its
            configuration's identity, wherever this pull puts that
            configuration (a rename in the same pull, D1).
        planned: Records of the directories to delete, in planning order.
        retained: Records still recorded after the commit -- a delete that
            failed, or a folder kept because it changed (G1); the final save
            keeps them, so a kept folder never loses both its manifest entry
            and its record.
        warnings: ``warnings[]`` entries for the pull result.
    """

    project_root: Path
    tree: str
    pending: PendingPaths = field(default_factory=PendingPaths)
    pending_rows: frozenset[tuple[str, str, str]] = frozenset()
    planned: list[ManifestPendingRemoval] = field(default_factory=list)
    retained: list[ManifestPendingRemoval] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def for_pull(cls, project_root: Path, manifest: Manifest, tree: str) -> RemovalJournal:
        """The journal of a pull of *tree*, after the recovery ran."""
        rows = frozenset(
            (item.component_id, item.config_id, item.row_path)
            for item in manifest.pending_removals
            if item.tree == tree and item.kind == "row" and item.row_path and item.problem is None
        )
        return cls(
            project_root, tree, pending=pending_removal_paths(manifest, tree), pending_rows=rows
        )

    def rows_hidden(self, component_id: str, config_id: str) -> frozenset[str]:
        """Row folders (relative to the configuration) whose delete is still pending (D1)."""
        return frozenset(
            row for comp, cid, row in self.pending_rows if (comp, cid) == (component_id, config_id)
        )

    def plan(
        self,
        path: str,
        kind: str,
        *,
        component_id: str,
        config_id: str,
        row_id: str = "",
        row_path: str = "",
    ) -> None:
        """Record *path* (relative to the tree) for deletion; nothing is deleted yet.

        A row also records *row_path*, its folder relative to its
        configuration: the record then follows the configuration's moves.
        """
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
                rowPath=row_path,
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
        or any change since it was planned keeps it, with a warning. A folder
        inside another planned one goes with it (G1, :func:`_settle`). The
        checkpoint also takes the folders this pull already moved (renames):
        only the ``path`` of those entries, no other work of the pull -- so
        every row record validates against the checkpoint alone after a
        restart (D2).
        """
        if not self.planned:
            return
        checkpoint = manifest.model_copy(deep=True)
        _take_moves(checkpoint, live or [], self.tree)
        for item in self.planned:
            _drop_tracked(checkpoint, item)
        checkpoint.pending_removals = [*checkpoint.pending_removals, *self.planned]
        save_manifest(self.project_root, checkpoint)
        settled = _settle(self.project_root, checkpoint, self.planned, live or [])
        self.retained += settled.retained
        self.warnings += settled.warnings


def _take_moves(checkpoint: Manifest, live: list[ManifestConfiguration], tree: str) -> None:
    """Give each entry of *tree* the path this pull already moved its folder to (D2)."""
    moved = {(c.component_id, c.id): c.path for c in live if entry_tree(checkpoint, c) == tree}
    for cfg in checkpoint.configurations:
        if entry_tree(checkpoint, cfg) != tree:
            continue
        path = moved.get((cfg.component_id, cfg.id))
        if path and path != cfg.path:
            cfg.path = path


def relocate_records(
    manifest: Manifest, records: list[ManifestPendingRemoval]
) -> list[ManifestPendingRemoval]:
    """*records* at their configurations' current folders in *manifest* (D1), for a save."""
    return [resolve_removal(manifest, item) for item in records]


def _drop_tracked(manifest: Manifest, item: ManifestPendingRemoval) -> None:
    """Remove the entry (or the row) *item* records from *manifest*.

    A config's folder also takes every promote entry whose files it is (a
    dev-branch config promoted from it, ``promoted_from``): with its source
    folder gone such an entry would read as a local delete, and a
    ``push --branch DEV --force`` after a crash would delete the dev config
    (#1005 K1). The final save of a pull drops it the same way.
    """
    in_tree = [c for c in manifest.configurations if entry_tree(manifest, c) == item.tree]
    if item.kind == "row":
        for cfg in in_tree:
            if cfg.component_id == item.component_id and cfg.id == item.config_id:
                cfg.rows = [r for r in cfg.rows if r.id != item.row_id]
        return
    drop = {
        id(c)
        for c in in_tree
        if (c.component_id == item.component_id and c.id == item.config_id and c.path == item.path)
        or (
            promoted_from(c) is not None
            and (c.path == item.path or c.path.startswith(f"{item.path}/"))
        )
    }
    manifest.configurations = [c for c in manifest.configurations if id(c) not in drop]


def unsafe_reason(
    project_root: Path,
    manifest: Manifest,
    item: ManifestPendingRemoval,
    live: list[ManifestConfiguration] | None = None,
) -> str | None:
    """Why *item* must never be acted on (F1), or ``None``.

    The paths must be plain relative paths (checked at load), *tree* a branch
    tree of this manifest, and the target inside its tree. A row must lie in
    the rows folder of its configuration in that tree (*live*: the entries the
    running pull wrote, which a re-targeted tree's configuration is). Such a
    record is malformed: it is dropped. The structure (tree, kind, a row in
    its rows folder) is checked first, on the path as written (I2); only
    then does a symbolic link on the way stop the check
    (:func:`link_on_the_way`): such a record is valid, its folder is only not
    deleted now (H2). Without a link the resolved path must stay inside.
    """
    if item.problem is not None:
        return item.problem
    if item.tree not in {branch.path for branch in manifest.branches}:
        return f"its tree {item.tree!r} is not a branch tree of this manifest"
    if item.kind == "row":
        rows_prefix = manifest.naming.config_row.split("{", 1)[0]
        parents = [
            c.path
            for c in [*manifest.configurations, *(live or [])]
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
    if link_on_the_way(project_root, item) is not None:
        return None  # resolving through the link says nothing; the caller keeps it (H2)
    tree_dir = (project_root / item.tree).resolve()
    if project_root.resolve() not in tree_dir.parents:
        return "its tree resolves outside the project"
    if tree_dir not in (project_root / item.tree / item.path).resolve().parents:
        return "it resolves outside its tree"
    return None


def link_on_the_way(project_root: Path, item: ManifestPendingRemoval) -> str | None:
    """The first directory from the project root down to *item*'s folder that is a link (F1, H2)."""
    current = project_root
    for part in [*item.tree.split("/"), *item.path.split("/")]:
        current = current / part
        if current.is_symlink():
            return f"{current.relative_to(project_root).as_posix()} is a symbolic link"
    return None


QUARANTINE_DIR = "removing"


class QuarantineNotEmptied(OSError):
    """The folder left its tree, but deleting it from the quarantine failed (#1005)."""


QUARANTINE_IGNORE = ".gitignore"


class QuarantineUnsafe(OSError):
    """The quarantine is not a plain folder of the project; nothing is moved into it (K2)."""


def quarantine_dir(project_root: Path) -> Path:
    """Where pull moves a folder right before deleting it: ``.keboola/removing`` (#1005)."""
    return project_root / KEBOOLA_DIR_NAME / QUARANTINE_DIR


def quarantine_problem(project_root: Path) -> str | None:
    """Why the quarantine must not be used, or ``None`` (#1005 K2).

    ``.keboola`` and ``.keboola/removing`` must each be a real directory (or
    not exist yet) -- never a symbolic link, never anything else -- and
    resolve inside the project: a linked quarantine would make a pull move
    folders out of the project or delete unrelated files.
    """
    current = project_root
    for part in (KEBOOLA_DIR_NAME, QUARANTINE_DIR):
        current = current / part
        if current.is_symlink():
            return f"{current.relative_to(project_root).as_posix()} is a symbolic link"
        if current.exists() and not current.is_dir():
            return f"{current.relative_to(project_root).as_posix()} is not a folder"
    root = project_root.resolve()
    if root not in quarantine_dir(project_root).resolve().parents:
        return f"{KEBOOLA_DIR_NAME}/{QUARANTINE_DIR} resolves outside the project"
    return None


def _ensure_quarantine(project_root: Path) -> Path:
    """The checked quarantine folder, created with a self-ignoring ``.gitignore`` (K2, K3)."""
    problem = quarantine_problem(project_root)
    if problem is not None:
        raise QuarantineUnsafe(f"the removal quarantine is not usable: {problem}")
    root = quarantine_dir(project_root)
    root.mkdir(parents=True, exist_ok=True)
    _ensure_ignore_file(root / QUARANTINE_IGNORE)
    return root


_IGNORE_RULE = b"*\n"


def _ensure_ignore_file(ignore: Path) -> None:
    """Make *ignore* a regular file holding ``*``, never through a link (K3).

    A link (dangling or not) or anything but a regular file makes the
    quarantine unusable (:class:`QuarantineUnsafe`): writing through it could
    create or change a file outside the project. A regular file with other
    content is rewritten in place. A new one is created exclusively and
    without following a link.
    """
    if ignore.is_symlink() or (ignore.exists() and not ignore.is_file()):
        raise QuarantineUnsafe(
            f"the removal quarantine is not usable: {ignore.name} in it is not a regular file"
        )
    if ignore.is_file():
        if ignore.read_bytes() == _IGNORE_RULE:
            return
        flags = os.O_WRONLY | os.O_TRUNC
    else:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(ignore, flags, 0o644)
    except OSError as exc:
        raise QuarantineUnsafe(
            f"the removal quarantine is not usable: {ignore.name} could not be written ({exc})"
        ) from exc
    with os.fdopen(fd, "wb") as handle:
        handle.write(_IGNORE_RULE)


def _move_aside(directory: Path, target: Path) -> None:
    """Move *directory* to *target* in one rename (same file system, the project)."""
    os.replace(directory, target)


def _remove(project_root: Path, item: ManifestPendingRemoval) -> None:
    """Delete *item*'s folder; a config's now-empty parents go too, never the tree or above.

    The folder is first renamed into the quarantine (one atomic step), then
    deleted there (#1005). So the tree holds either the whole folder or none
    of it: a delete that stops half-way (a permission error, a crash) never
    leaves a partly deleted folder that the next pull takes for an edit and
    keeps forever. A failed rename leaves the folder whole (``OSError``); a
    failed delete in the quarantine raises :class:`QuarantineNotEmptied` --
    the next pull empties the quarantine (:func:`empty_quarantine`). An
    unusable quarantine (a link, outside the project) refuses before any move
    (:class:`QuarantineUnsafe`): the folder stays whole and recorded. So does
    a folder on another file system, which cannot be renamed into it.
    """
    tree_dir = project_root / item.tree
    directory = tree_dir / item.path
    if not directory.is_dir():
        return
    target = _ensure_quarantine(project_root) / uuid.uuid4().hex
    _move_aside(directory, target)
    logger.info("Removed directory: %s", directory)
    if item.kind != "row":
        parent = directory.parent
        while tree_dir in parent.parents and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    try:
        shutil.rmtree(target)
    except OSError as exc:
        raise QuarantineNotEmptied(str(exc)) from exc


def empty_quarantine(project_root: Path) -> list[dict[str, Any]]:
    """Delete what an earlier pull moved aside and could not delete (#1005).

    Everything in the quarantine is a folder pull itself moved out of a tree
    after every check -- never a user's folder -- so it is deleted as it is.
    A folder that still cannot be deleted is reported and tried again by the
    next pull. A quarantine that is a link or resolves outside the project is
    never touched (K2); its ``.gitignore`` stays (K3).
    """
    root = quarantine_dir(project_root)
    if not root.exists() and not root.is_symlink():
        return []
    problem = quarantine_problem(project_root)
    if problem is not None:
        return [
            {
                "code": "PENDING_REMOVAL_FAILED",
                "path": f"{KEBOOLA_DIR_NAME}/{QUARANTINE_DIR}",
                "message": (
                    f"The removal quarantine was not emptied: {problem}. Nothing in it or "
                    "behind it was touched; fix the folder by hand."
                ),
            }
        ]
    warnings: list[dict[str, Any]] = []
    for leftover in sorted(root.iterdir()):
        if leftover.name == QUARANTINE_IGNORE:
            continue
        try:
            if leftover.is_dir() and not leftover.is_symlink():
                shutil.rmtree(leftover)
            else:
                leftover.unlink()
        except OSError as exc:
            warnings.append(_quarantine_warning(leftover, project_root, exc))
    return warnings


def _quarantine_warning(path: Path, project_root: Path, exc: OSError) -> dict[str, Any]:
    rel = path.relative_to(project_root).as_posix()
    return {
        "code": "PENDING_REMOVAL_FAILED",
        "path": rel,
        "message": (
            f"A removed folder was moved to {rel} but could not be deleted there ({exc}). "
            "It is outside the sync tree, so diff and push never see it; the next "
            "`kbagent sync pull` deletes it, or delete it yourself."
        ),
    }


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
        "the folder stays recorded for deletion, so diff and push ignore it and every pull "
        "checks it again. Delete it, or move it out of the sync tree to keep its content.",
    )


def _tracked_dirs(
    tree_dir: Path, entries: list[ManifestConfiguration], manifest: Manifest, tree: str
) -> frozenset[Path]:
    """Every existing tracked config and row directory of *tree* (for the ownership check).

    A tracked path with no directory on disk holds nothing (a nested config
    the user deleted must not keep its outer folder). A promote entry
    (``promoted_from``: a dev-branch config whose files are this tree's
    folder) is no owner either (#1005): the folder belongs to the tree's own
    config, and when that config is deleted on its branch the folder goes --
    the promote entry is then dropped with it (its source folder is gone), as
    a production pull always did.
    """
    dirs: set[Path] = set()
    for cfg in entries:
        if not cfg.path or entry_tree(manifest, cfg) != tree:
            continue
        if promoted_from(cfg) is not None:
            continue  # a promote reference to the folder, not its owner (#1005)
        dirs.add(tree_dir / cfg.path)
        dirs.update(tree_dir / cfg.path / row.path for row in cfg.rows if row.path)
    return frozenset(d for d in dirs if d.is_dir())


@dataclass(frozen=True)
class Keep:
    """Why a recorded folder is not deleted now.

    Attributes:
        reason: The text of the warning.
        tracked: The folder itself is a tracked config or row directory again
            (re-tracked since): its manifest entry records it, so the pending
            record is dropped. Any other kept folder keeps its record (G1).
    """

    reason: str
    tracked: bool = False


def _keep_reason(
    project_root: Path,
    manifest: Manifest,
    item: ManifestPendingRemoval,
    live: list[ManifestConfiguration],
) -> Keep | None:
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
        return Keep(KEPT_REASON_LINK)
    holder = next((o for o in sorted(others) if holds(directory, o)), None)
    if holder is not None:
        return Keep(
            f"it holds the tracked directory {holder.relative_to(tree_dir).as_posix()}",
            tracked=same_dir(holder, directory),
        )
    if _file_hash(directory) != item.file_hash or tree_digest(directory) != item.tree_hash:
        return Keep(KEPT_REASON_CHANGED)
    return None


def _inner_change(
    project_root: Path,
    manifest: Manifest,
    outer: ManifestPendingRemoval,
    items: list[ManifestPendingRemoval],
    live: list[ManifestConfiguration],
) -> Keep | None:
    """Why *outer* must stay because a recorded folder inside it changed since ITS snapshot (H3)."""
    for inner in items:
        if inner is outer or inner.tree != outer.tree:
            continue
        if not inner.path.startswith(f"{outer.path}/"):
            continue
        if not (project_root / inner.tree / inner.path).exists():
            continue
        keep = _keep_reason(project_root, manifest, inner, live)
        if keep is not None and not keep.tracked:
            return Keep(f"the folder {inner.path} inside it was not deleted: {keep.reason}")
    return None


@dataclass
class Settled:
    """What :func:`_settle` left: the records to keep and the ``warnings[]`` entries."""

    retained: list[ManifestPendingRemoval] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)


def _settle(
    project_root: Path,
    manifest: Manifest,
    items: list[ManifestPendingRemoval],
    live: list[ManifestConfiguration],
) -> Settled:
    """Delete each recorded folder that is still safe and unchanged; the rest stays recorded.

    Outermost first (G1): a folder inside another recorded folder of its tree
    goes with that folder's delete, after the outer one was checked against
    its own recorded content -- which includes the inner folder, so the inner
    one is never deleted first and taken for a change of the outer one. When
    the outer folder is kept, an inner one is checked on its own. Before the
    outer folder goes, every inner record is checked against its OWN snapshot
    (H3): an inner edit made between the two snapshots keeps both. A
    malformed or escaping record is dropped and never acted on (F1); a valid
    one whose folder is reached through a symbolic link is kept, recorded
    (H2); a folder that is now tracked again drops its record; a kept or
    undeletable folder keeps it.
    """
    out = Settled()
    deleted: list[ManifestPendingRemoval] = []
    items = [resolve_removal(manifest, item, live) for item in items]  # D1, D2
    for item in sorted(items, key=lambda i: (i.tree, i.path.count("/"), i.path)):
        if any(d.tree == item.tree and item.path.startswith(f"{d.path}/") for d in deleted):
            continue  # removed with its outer folder
        unsafe = unsafe_reason(project_root, manifest, item, live)
        if unsafe is not None:
            out.warnings.append(
                _warning(
                    "PENDING_REMOVAL_INVALID",
                    item,
                    f"A pending removal record was dropped and nothing deleted: {unsafe}.",
                )
            )
            continue
        link = link_on_the_way(project_root, item)
        if link is not None:  # valid, but not deleted through a link: still recorded (H2)
            out.warnings.append(_kept_warning(item, link))
            out.retained.append(item)
            continue
        if not (project_root / item.tree / item.path).exists():
            continue  # already gone
        keep = _keep_reason(project_root, manifest, item, live) or _inner_change(
            project_root, manifest, item, items, live
        )
        if keep is not None:
            out.warnings.append(_kept_warning(item, keep.reason))
            if not keep.tracked:
                out.retained.append(item)
            continue
        try:
            _remove(project_root, item)
        except QuarantineNotEmptied as exc:  # gone from the tree; the quarantine waits
            out.warnings.append(
                _quarantine_warning(quarantine_dir(project_root), project_root, exc)
            )
        except OSError as exc:
            out.retained.append(item)
            out.warnings.append(_failed_warning(item, exc))
            continue
        deleted.append(item)
    return out


def recover_pending_removals(
    project_root: Path, manifest: Manifest, *, dry_run: bool
) -> list[dict[str, Any]]:
    """Finish or abandon the removals an earlier pull left (D11); the ``warnings[]`` entries.

    Runs at the start of a pull, before any path is allocated. A record with
    an unsafe path is dropped and never acted on (F1). A directory still
    exactly as recorded is deleted; any other is kept and reported, and keeps
    its record (G1) -- unless it is tracked again. A delete that fails again
    keeps its record too (F2). The manifest is saved right away, so
    a pull that stops later (a conflict) does not leave the records hiding a
    kept directory from diff. First it empties the quarantine (#1005). A dry
    run changes nothing and lists the records it did not process.
    """
    if dry_run:
        if not manifest.pending_removals:
            return []
        return [
            _warning(
                "PENDING_REMOVALS_NOT_PROCESSED",
                item,
                f"{_record_path(item)} is recorded for deletion by an interrupted pull; a dry "
                "run does not process it. A real `kbagent sync pull` deletes it if it is "
                "unchanged, else keeps it recorded.",
            )
            for item in manifest.pending_removals
        ]
    leftovers = empty_quarantine(project_root)
    if not manifest.pending_removals:
        return leftovers
    settled = _settle(project_root, manifest, list(manifest.pending_removals), [])
    settled.warnings[:0] = leftovers
    manifest.pending_removals = settled.retained
    save_manifest(project_root, manifest)
    return settled.warnings
