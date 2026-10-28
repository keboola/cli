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

An ``ignored`` entry gets the same keep rules (issue #927, D8): adding a
component to ``ignoredComponents`` used to delete its directories with every
unpushed edit in them. Now a directory pull cannot prove unchanged -- edited,
or with no baseline to compare (an adopted config, a row of unknown base) --
is kept with its manifest entry unchanged (row baselines and promote
associations included) and reported ``skipped``; an unchanged one is cleaned
up with its entry, as #689 designed. ``--theirs`` does not delete kept work
either: ignoring a component is no evidence that its remote content is gone.
While ignored the entry stays out of diff and push; once un-ignored its files
are compared with the preserved baseline, so a remote change made meanwhile
is a conflict, never a new baseline. The one exception is a shared SQL
workspace (CLI-25, ``syncWorkspaces`` turned off): ``--theirs`` deletes it.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..constants import CONFIG_FILENAME
from ..sync.branch_scope import UNKNOWN_BASE_ADOPTED, UNKNOWN_BASE_KEY
from ..sync.manifest import ManifestConfigRow, ManifestConfiguration
from ._sync_companions import companion_baseline, config_locally_modified, current_companions
from ._sync_workspace import SANDBOXES_COMPONENT_ID

if TYPE_CHECKING:
    from .sync_service import SyncService

logger = logging.getLogger(__name__)

REMOTE_DELETED_REASON = "locally modified, deleted on remote"
WORKSPACE_SYNC_OFF_REASON = (
    "locally modified, workspace sync is off; set syncWorkspaces and push, or pull --theirs"
)
IGNORED_COMPONENT_REASON = (
    "locally modified or not provably unchanged, and {component_id} is in ignoredComponents; "
    "remove it from ignoredComponents to sync this config again, or delete the directory "
    "yourself"
)


@dataclass
class StaleEntry:
    """A manifest entry the current pull no longer produces."""

    entry: ManifestConfiguration
    action: str  # "removed" (gone from the remote) or "ignored" (component ignored)
    locally_modified: bool
    # A shared SQL workspace swept only because ``syncWorkspaces`` is off
    # (CLI-25) -- not because ``ignoredComponents`` names keboola.sandboxes.
    workspace_sync_off: bool = False

    @property
    def keep_reason(self) -> str:
        """The ``skipped`` reason pull reports when it keeps this edited entry."""
        if self.action == "removed":
            return REMOTE_DELETED_REASON
        if self.workspace_sync_off:
            return WORKSPACE_SYNC_OFF_REASON
        return IGNORED_COMPONENT_REASON.format(component_id=self.entry.component_id)

    @property
    def theirs_deletes(self) -> bool:
        """``--theirs`` deletes this entry's dir even when it holds local work (D8).

        Only for a config the remote deleted and for a shared SQL workspace of
        a tree whose workspace sync is off (the CLI-25 rule). A component named
        in ``ignoredComponents`` -- keboola.sandboxes included -- never: ignoring
        it is no evidence that its remote content is gone.
        """
        return self.action == "removed" or self.workspace_sync_off


def _entry_locally_modified(
    service: SyncService,
    config_dir: Path,
    entry: ManifestConfiguration,
    other_configs: frozenset[Path] = frozenset(),
) -> bool:
    """True iff ``_config.yml``, a companion file or a row file changed since pull.

    Without a recorded ``pull_hash`` there is no base to compare against, so
    the entry is not treated as modified (same conservatism as the force-pull
    conflict guard).
    """
    # A row kept with an unknown base (D22) counts as edited -- its file may
    # hold edits pull could not tell from a stale copy -- also when the parent
    # file or the parent's baseline is gone, so this check comes first.
    if any(
        row.metadata.get(UNKNOWN_BASE_KEY) and (config_dir / row.path / CONFIG_FILENAME).exists()
        for row in entry.rows
    ):
        return True
    # _config.yml and the companion set, by the pull rule: an edited companion
    # that survives a deleted _config.yml is local work too (D6, R12) -- the
    # sweep deleted it with the directory before.
    if config_locally_modified(service, config_dir, entry.component_id, entry.metadata):
        return True
    return _rows_locally_modified(service, config_dir, entry, other_configs)


def _rows_locally_modified(
    service: SyncService,
    config_dir: Path,
    entry: ManifestConfiguration,
    other_configs: frozenset[Path] = frozenset(),
) -> bool:
    """True iff any row dir under *config_dir* holds work a delete would lose (rounds 8-10).

    Every tracked row is checked at its actual manifest path, whatever the
    row naming (``naming.configRow`` may be ``records/{config_row_name}`` or
    nested). Every other ``_config.yml`` below the config dir is found by a
    walk that stops at another manifest config's directory (*other_configs*:
    that subtree has its own baseline and its own sweep decision). Those files
    are grouped by the row id they carry:

    - one that carries no tracked row's id, or the id of a tracked row whose
      own dir still exists, is user content without a baseline (a new row, a
      copy) -- kept;
    - two or more carrying the id of a tracked row whose dir is gone: which
      one is the row is unknown -- kept;
    - the one dir carrying it is that row, moved, judged by its baseline (an
      unknown base counts as edited).
    """
    tree = scan_owned_tree(config_dir, other_configs)
    if tree.is_link or tree.symlinks:
        return True  # never traversed or decided through a link (rounds 11, 12)
    by_path = {row.path: row for row in entry.rows}
    by_id = {row.id: row for row in entry.rows}
    for row in entry.rows:
        if _row_edited(service, config_dir / row.path / CONFIG_FILENAME, row.metadata):
            return True
    carriers: dict[str, list[Path]] = {}
    for row_file in tree.row_files:
        if row_file.parent.relative_to(config_dir).as_posix() in by_path:
            continue  # a tracked row at its own path: checked above
        data = service._read_config_file(row_file.parent)
        meta = data.get("_keboola") if isinstance(data, dict) else None
        row_id = str(meta.get("row_id") or "") if isinstance(meta, dict) else ""
        moved = by_id.get(row_id)
        if moved is None or (config_dir / moved.path / CONFIG_FILENAME).exists():
            return True  # no tracked row it stands for: user content
        carriers.setdefault(row_id, []).append(row_file)
    for row_id, files in carriers.items():
        if len(files) > 1:
            return True  # several dirs carry the id: which is the row is unknown
        if _row_edited(service, files[0], by_id[row_id].metadata, unknown_counts=True):
            return True
    return False


@dataclass(frozen=True)
class OwnedTree:
    """What one config dir holds of its own (round 12) -- the one helper both the
    "locally modified" classification and the sweep's delete decision read.

    Attributes:
        is_link: The config dir itself is a symbolic link.
        row_files: Every ``_config.yml`` below it but its own.
        symlinks: Every symbolic link below it (files and dirs alike).
    """

    is_link: bool
    row_files: list[Path] = field(default_factory=list)
    symlinks: list[Path] = field(default_factory=list)


def scan_owned_tree(config_dir: Path, other_configs: frozenset[Path]) -> OwnedTree:
    """Walk *config_dir* without entering another config's dir or following any link.

    ``Path.walk`` does not follow symbolic links (a symlinked dir is listed,
    never entered); every link found is recorded -- kbagent does not decide
    ownership through links. Another config's dir is recognised by the
    filesystem (:func:`same_dir`), never by spelling: on a case-insensitive
    filesystem ``Extractor/x`` IS ``extractor/x``. A symlinked config dir is
    not walked at all.
    """
    if config_dir.is_symlink():
        return OwnedTree(is_link=True)
    tree = OwnedTree(is_link=False)
    if not config_dir.is_dir():
        return tree
    for root, dirs, files in config_dir.walk():
        tree.symlinks.extend(root / name for name in [*dirs, *files] if (root / name).is_symlink())
        dirs[:] = sorted(
            d
            for d in dirs
            if not (root / d).is_symlink() and not any(same_dir(root / d, o) for o in other_configs)
        )
        if root != config_dir and CONFIG_FILENAME in files:
            tree.row_files.append(root / CONFIG_FILENAME)
    return tree


def same_dir(a: Path, b: Path) -> bool:
    """True iff *a* and *b* are the same existing directory entry (``os.path.samefile``)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def contains(outer: Path, inner: Path) -> bool:
    """True iff *inner* lies inside *outer*, judged by the filesystem (round 11).

    Walks up *inner*'s own path and compares each ancestor with *outer* by
    :func:`same_dir` -- case-insensitive filesystems included, and without
    resolving *inner* through a symlink (a symlinked dir inside *outer* is
    inside it).
    """
    return any(same_dir(parent, outer) for parent in inner.parents)


def holds(outer: Path, inner: Path) -> bool:
    """True iff the dir *inner* is *outer* or inside it, by path OR by real location (rounds 12, 13).

    By path: an ancestor of *inner* is *outer* (:func:`contains`). By real
    location: *inner* resolved through every link lies inside *outer*
    resolved -- a config reached as ``alias -> parent/child`` is inside
    ``parent``.
    """
    # The same directory under another spelling or through a link (``b -> a``)
    # is held too: deleting it would delete the staying config's files (round 13).
    if same_dir(outer, inner) or contains(outer, inner):
        return True
    real_inner = Path(os.path.realpath(inner))
    real_outer = Path(os.path.realpath(outer))
    return real_inner == real_outer or any(
        same_dir(parent, real_outer) for parent in real_inner.parents
    )


def _row_edited(
    service: SyncService, row_file: Path, metadata: dict[str, Any], *, unknown_counts: bool = False
) -> bool:
    """True iff *row_file* exists and nothing proves it unchanged since the last pull.

    A file with no recorded ``pull_hash`` is never proven unchanged -- with or
    without an ``unknown_base`` marker, at its manifest path or at a moved
    carrier (a row-create writeback interrupted before its baseline was saved
    leaves such a row): a sweep that deleted it deleted local work. With
    *unknown_counts*, an ``unknown_base`` marker counts as edited even when a
    hash is recorded.
    """
    if not row_file.is_file():
        return False
    pull_hash = metadata.get("pull_hash", "")
    if not pull_hash or (unknown_counts and metadata.get(UNKNOWN_BASE_KEY)):
        return True
    return service._file_hash(row_file) != pull_hash


def find_stale_entries(
    service: SyncService,
    entries: list[ManifestConfiguration],
    components: list[dict[str, Any]],
    branch_dir: Path,
    ignored_components: frozenset[str],
    explicitly_ignored: list[str] | frozenset[str] = frozenset(),
) -> list[StaleEntry]:
    """Manifest entries absent from the fresh (non-ignored) remote listing.

    *explicitly_ignored* is the manifest's own ``ignoredComponents``: a
    keboola.sandboxes entry ignored without being named there is swept
    because workspace sync is off (CLI-25), which keeps its ``--theirs`` rule.
    """
    remote_keys = {
        f"{component.get('id', '')}/{cfg.get('id', '')}"
        for component in components
        if component.get("id", "") not in ignored_components
        for cfg in component.get("configurations", [])
    }
    # A config dir nested in another one is that config's, not the outer one's work.
    other_configs = frozenset(branch_dir / e.path for e in entries if e.path)
    stale: list[StaleEntry] = []
    for entry in entries:
        if f"{entry.component_id}/{entry.id}" in remote_keys:
            continue
        action = "ignored" if entry.component_id in ignored_components else "removed"
        config_dir = branch_dir / entry.path
        # Every stale entry is checked, an ignored one too (#927); for an
        # ignored one "no baseline" counts as local work (D8).
        modified = (action == "ignored" and _unproven(service, config_dir, entry)) or (
            _entry_locally_modified(service, config_dir, entry, other_configs - {config_dir})
        )
        workspace_sync_off = (
            action == "ignored"
            and entry.component_id == SANDBOXES_COMPONENT_ID
            and SANDBOXES_COMPONENT_ID not in explicitly_ignored
        )
        stale.append(
            StaleEntry(
                entry=entry,
                action=action,
                locally_modified=modified,
                workspace_sync_off=workspace_sync_off,
            )
        )
    return stale


def _unproven(service: SyncService, config_dir: Path, entry: ManifestConfiguration) -> bool:
    """True iff *config_dir* holds a file whose baseline cannot prove it unchanged (D8).

    :func:`_entry_locally_modified` reads a missing ``pull_hash`` as "not
    modified" -- the conservative reading for a pull that writes the remote
    over the file. For a sweep that DELETES the file it is the wrong one:
    without a baseline nothing proves the file holds no local work (an
    adopted config, a create checkpoint, a legacy companion map, a row
    without a hash). The config's own files and each row are judged
    independently: a parent without a baseline and without its
    ``_config.yml`` must not hide a surviving row that has no baseline either.
    """
    if _parent_unproven(service, config_dir, entry):
        return True
    return any(
        not (row.metadata or {}).get("pull_hash")
        and (config_dir / row.path / CONFIG_FILENAME).exists()
        for row in entry.rows
    )


def _parent_unproven(service: SyncService, config_dir: Path, entry: ManifestConfiguration) -> bool:
    """True iff the config's own ``_config.yml`` or a companion cannot be proven unchanged."""
    companions = current_companions(service, config_dir, entry.component_id)
    if not entry.metadata.get("pull_hash"):
        return (config_dir / CONFIG_FILENAME).exists() or bool(companions)
    base = companion_baseline(entry.component_id, entry.metadata)
    return not base.known and bool(set(companions) - set(base.hashes))


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


SYMLINK_ROOT_REASON = "the config directory is a symbolic link; kbagent does not delete through it"
SYMLINK_INSIDE_REASON = (
    "it contains a symbolic link; kbagent does not decide ownership through links"
)


def _keep_reasons(
    stale: list[StaleEntry], branch_dir: Path, live_paths: set[str], *, theirs: bool
) -> dict[int, str]:
    """Pass 1 of the sweep: why each stale entry is kept, by index (round 12).

    Decided for ALL entries before anything is deleted, so the outcome never
    depends on manifest order. Kept: a symlinked config dir, one with a link
    anywhere inside it, and an edited one (unless ``--theirs`` may delete it,
    D8) -- then, to a fixed point, every config dir that holds a config that stays (written by
    this pull or kept here), by path or by real location (:func:`holds`).
    """
    others = frozenset(branch_dir / p for p in live_paths | {x.entry.path for x in stale} if p)
    reasons: dict[int, str] = {}
    for index, item in enumerate(stale):
        if not item.entry.path:
            continue
        config_dir = branch_dir / item.entry.path
        tree = scan_owned_tree(config_dir, others - {config_dir})
        if tree.is_link:
            reasons[index] = SYMLINK_ROOT_REASON
        elif tree.symlinks:
            reasons[index] = SYMLINK_INSIDE_REASON
        elif item.locally_modified and not (theirs and item.theirs_deletes):
            reasons[index] = item.keep_reason
    changed = True
    while changed:
        changed = False
        staying = {p for p in live_paths if p} | {stale[i].entry.path for i in reasons}
        for index, item in enumerate(stale):
            if index in reasons or not item.entry.path:
                continue
            config_dir = branch_dir / item.entry.path
            holder = next(
                (
                    p
                    for p in sorted(staying)
                    if p != item.entry.path and holds(config_dir, branch_dir / p)
                ),
                None,
            )
            if holder is not None:
                reasons[index] = (
                    f"it contains the config directory {holder}, which stays; a later pull "
                    f"removes it once that config is gone"
                )
                changed = True
    return reasons


def apply_stale_sweep(
    stale: list[StaleEntry],
    branch_dir: Path,
    *,
    theirs: bool,
    dry_run: bool,
    new_configurations: list[ManifestConfiguration],
    pull_details: list[dict[str, str]],
) -> None:
    """Report stale entries and delete their directories, safely, in two passes (round 12).

    Pass 1 (:func:`_keep_reasons`) decides every keep: an edited entry
    (unless ``--theirs`` may delete it, D8), a symlinked config dir or one
    with a link inside, and a dir that holds a config that stays. A kept entry's manifest entry is
    carried over unchanged (the manifest still matches disk) and it is
    reported ``skipped`` with its reason. Pass 2 deletes the rest.
    ``--force`` never reaches an edited entry -- the conflict guard has
    already aborted. A path an entry of this pull owns is never deleted.
    """
    live_paths = {c.path for c in new_configurations}
    reasons = _keep_reasons(stale, branch_dir, live_paths, theirs=theirs)
    for index, item in enumerate(stale):
        if index in reasons:
            new_configurations.append(item.entry)
            pull_details.append(
                {
                    "action": "skipped",
                    "component_id": item.entry.component_id,
                    "config_name": item.entry.path,
                    "path": item.entry.path,
                    "reason": reasons[index],
                }
            )
            continue
        pull_details.append(
            {
                "action": item.action,
                "component_id": item.entry.component_id,
                "config_name": "",
                "path": item.entry.path,
            }
        )
        if not dry_run and item.entry.path and item.entry.path not in live_paths:
            _remove_dir(branch_dir / item.entry.path, branch_dir)


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
    carried: set[str] | None = None,
    deleted_rows: dict[str, str] | None = None,
) -> list[ManifestConfigRow]:
    """Handle the tracked rows of one config that are gone from the remote.

    Pull rebuilds the row entries from the remote listing, so a row deleted on
    the remote used to lose its manifest entry while its directory stayed on
    disk. The next diff read that directory as a new row, and push created it
    again under a new id (issue #792 H). Now an unedited row directory is
    deleted (``removed``). An edited one is kept with its entry (``skipped``),
    so diff keeps reporting it as ``remote_deleted``; ``--theirs`` deletes it.

    *carried* holds the ids whose own dir is gone while several dirs carry
    them: such an entry is always kept.

    *deleted_rows* receives, for every row removed, its id and last
    ``pull_config_hash``: the record a later promote push weighs a target row
    with the same id against (D7).

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
        # A row kept with an unknown base (D22) counts as edited: pull cannot
        # tell its file from the remote's last state, so it never deletes it.
        # So does a row with no recorded hash (group 3 round 3): nothing
        # proves its file unchanged. Kept, it is marked unknown-base, so diff
        # reports it ``remote_deleted`` and push never creates it again.
        unknown_base = tracked.get(UNKNOWN_BASE_KEY, "") or (
            "" if pull_hash else UNKNOWN_BASE_ADOPTED
        )
        edited = row_file.exists() and (
            bool(unknown_base) or service._file_hash(row_file) != pull_hash
        )
        # Its dir is gone but copies carry its id (*carried*): which one is
        # the row is unknown, so the entry is kept -- and every copy -- until
        # the user resolves it (round 7). ``--theirs`` does not decide it either.
        edited = edited or row_id in (carried or set())
        path = f"{rel_path}/{tracked['path']}"
        detail = {"component_id": row_key_prefix.split("/", 1)[0], "config_name": "", "path": path}
        if edited and (not theirs or row_id in (carried or set())):
            kept.append(
                ManifestConfigRow(
                    id=row_id,
                    path=tracked["path"],
                    metadata={
                        "pull_hash": pull_hash,
                        "pull_config_hash": tracked["pull_config_hash"],
                        **({UNKNOWN_BASE_KEY: unknown_base} if unknown_base else {}),
                    },
                )
            )
            pull_details.append({**detail, "action": "skipped", "reason": REMOTE_DELETED_REASON})
            continue
        pull_details.append({**detail, "action": "removed"})
        if deleted_rows is not None:
            deleted_rows[row_id] = tracked["pull_config_hash"]
        if not dry_run and row_dir.is_dir():
            shutil.rmtree(row_dir)
    return kept
