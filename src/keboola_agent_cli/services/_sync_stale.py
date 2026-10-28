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
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..constants import CONFIG_FILENAME
from ..sync.branch_scope import UNKNOWN_BASE_KEY
from ..sync.manifest import ManifestConfigRow, ManifestConfiguration
from ._sync_companions import config_locally_modified
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
    if config_dir.is_symlink():
        return True  # never traversed, never deleted through (round 11)
    by_path = {row.path: row for row in entry.rows}
    by_id = {row.id: row for row in entry.rows}
    for row in entry.rows:
        if _row_edited(service, config_dir / row.path / CONFIG_FILENAME, row.metadata):
            return True
    carriers: dict[str, list[Path]] = {}
    for row_file in _row_files(config_dir, other_configs):
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


def _row_files(config_dir: Path, other_configs: frozenset[Path]) -> list[Path]:
    """Every ``_config.yml`` below *config_dir* but its own, not entering another config's dir.

    ``Path.walk`` does not follow symbolic links: a symlinked dir is never
    entered (round 11). Another config's dir is recognised by the filesystem
    (:func:`same_dir`), never by spelling: on a case-insensitive filesystem
    ``Extractor/x`` IS ``extractor/x``.
    """
    found: list[Path] = []
    for root, dirs, files in config_dir.walk():
        dirs[:] = sorted(d for d in dirs if not any(same_dir(root / d, o) for o in other_configs))
        if root != config_dir and CONFIG_FILENAME in files:
            found.append(root / CONFIG_FILENAME)
    return found


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


def _row_edited(
    service: SyncService, row_file: Path, metadata: dict[str, Any], *, unknown_counts: bool = False
) -> bool:
    """True iff *row_file* exists and differs from its baseline (or, *unknown_counts*, has none provable)."""
    if not row_file.is_file():
        return False
    if unknown_counts and metadata.get(UNKNOWN_BASE_KEY):
        return True
    pull_hash = metadata.get("pull_hash", "")
    return bool(pull_hash) and service._file_hash(row_file) != pull_hash


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
    # A config dir nested in another one is that config's, not the outer one's work.
    other_configs = frozenset(branch_dir / e.path for e in entries if e.path)
    stale: list[StaleEntry] = []
    for entry in entries:
        if f"{entry.component_id}/{entry.id}" in remote_keys:
            continue
        action = "ignored" if entry.component_id in ignored_components else "removed"
        guarded = action == "removed" or entry.component_id == SANDBOXES_COMPONENT_ID
        modified = guarded and _entry_locally_modified(
            service, branch_dir / entry.path, entry, other_configs - {branch_dir / entry.path}
        )
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


def _kept_whole_reason(config_dir: Path, staying: dict[str, Path]) -> str | None:
    """Why a stale config dir is kept as it is (round 11), or ``None`` when it may go.

    A symlinked config dir is never deleted through. A dir holding a config
    that stays is never deleted with it.
    """
    if config_dir.is_symlink():
        return "the config directory is a symbolic link; kbagent does not delete through it"
    for path, nested in sorted(staying.items()):
        if contains(config_dir, nested):
            return (
                f"it contains the config directory {path}, which stays; a later pull removes "
                f"it once that config is gone"
            )
    return None


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
    # Config dirs that stay (written by this pull, or kept as edited). A stale
    # config whose dir holds one is kept whole and reported: nothing in doubt
    # is ever deleted, and a later pull removes it once the nested one is gone.
    staying = {
        path: branch_dir / path
        for path in live_paths | {s.entry.path for s in stale if s.locally_modified and not theirs}
        if path
    }
    for s in stale:
        config_dir = branch_dir / s.entry.path
        reason = _kept_whole_reason(config_dir, staying) if s.entry.path else None
        if reason is not None:
            new_configurations.append(s.entry)
            pull_details.append(
                {
                    "action": "skipped",
                    "component_id": s.entry.component_id,
                    "config_name": s.entry.path,
                    "path": s.entry.path,
                    "reason": reason,
                }
            )
            continue
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
    carried: set[str] | None = None,
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
        unknown_base = tracked.get(UNKNOWN_BASE_KEY, "")
        edited = row_file.exists() and (
            bool(unknown_base) or (bool(pull_hash) and service._file_hash(row_file) != pull_hash)
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
        if not dry_run and row_dir.is_dir():
            shutil.rmtree(row_dir)
    return kept
