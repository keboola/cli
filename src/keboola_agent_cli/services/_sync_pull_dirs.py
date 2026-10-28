"""Which entries and directories one ``sync pull`` may use (extracted from sync_service.py).

A manifest entry belongs to exactly one branch. A dev branch created from
production holds the same config ids, so pull reads only the entries of the
branch it pulls (:func:`partition_pull_entries`); another branch's entry is
never looked up, never swept as stale and never deletes a file.

Pull also never writes a config or row into a directory something else owns
(D14): every path already owned on disk is reserved before the first new
directory is allocated. And a directory pull rediscovers by the id in its
file keeps its local edits when pull has no baseline for it (D15).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ..constants import CONFIG_FILENAME
from ..sync.branch_scope import (
    UNKNOWN_BASE_ADOPTED,
    UNKNOWN_BASE_KEY,
    branch_tree_path,
    find_untracked_configs,
    is_production,
    promoted_from,
    same_branch,
)
from ..sync.config_format import api_row_to_local
from ..sync.diff_engine import config_hash
from ..sync.manifest import Manifest, ManifestConfigRow, ManifestConfiguration
from ..sync.naming import config_row_path
from ._sync_models import (
    ADOPTED_ROW_IDS_KEY,
    AMBIGUOUS_ROW_PLACEHOLDERS_KEY,
    AUTHORED_ROW_KEY,
    CREATED_FROM_KEY,
    DELETED_ROWS_KEY,
    FALLBACK_LINKS_KEY,
    LINK_PENDING_KEY,
    PROMOTED_ROW_KEY,
    PROMOTED_SOURCE_KEY,
    REBIND_PENDING_KEY,
    RETIRED_ROWS_KEY,
)
from ._sync_stale import sweep_stale_rows

if TYPE_CHECKING:
    from .sync_service import SyncService

# Pull ``details[].reason`` for a kept row file that only carries the row id.
ADOPTED_ROW_SKIP_REASON = "row locally modified, not tracked in the manifest"


@dataclass
class PullEntries:
    """``manifest.configurations`` split for a pull of one branch.

    Attributes:
        own: Entries of the pulled branch -- the only ones pull looks up,
            refreshes, or sweeps as stale.
        kept: Promote entries of other branches (``push --branch DEV`` with no
            ``dev/`` tree) whose source tree exists -- whichever branch is
            pulled (D27). Pull carries them over (:func:`finalize_kept_entries`):
            they map a directory to its config on the other branch, and
            dropping them made the next promote create that config again.
    """

    own: list[ManifestConfiguration]
    kept: list[ManifestConfiguration]
    # Other branches' entries this pull drops (a re-target), and the rows of
    # the pulled tree an earlier re-target dropped (D6, :func:`retired_rows`).
    dropped: list[ManifestConfiguration] = field(default_factory=list)
    retired: dict[str, dict[str, str]] = field(default_factory=dict)

    def retired_rows_for(
        self, own_keys: set[str], components: list[dict[str, Any]]
    ) -> dict[str, dict[str, str]]:
        """The pulled tree's retired rows the remote deleted, of configs with no entry here (D6).

        Keyed like pull's ``existing_rows`` (``"{component}/{config}/{row}"``):
        pull then judges such a row by its own last baseline in this tree --
        an unedited row the remote deleted is removed, an edited one kept and
        reported -- instead of leaving its file behind as a new row. Only rows
        the fresh listing no longer has: a row still on the remote keeps the
        rediscovery rule (D15, D22) -- its file is kept with an UNKNOWN base,
        so any difference is a conflict, and two dirs carrying its id adopt
        neither. The retired baseline is deletion provenance, never a way to
        turn a rediscovered row into a proven, pushable one.
        """
        listed = {
            f"{component.get('id', '')}/{cfg.get('id', '')}/{row.get('id', '')}"
            for component in components
            for cfg in component.get("configurations", [])
            for row in cfg.get("rows", [])
        }
        return {
            key: dict(value)
            for key, value in self.retired.items()
            if key.rsplit("/", 1)[0] not in own_keys and key not in listed
        }

    def retire(
        self,
        manifest: Manifest,
        project_root: Path,
        tree: str,
        pulled: list[ManifestConfiguration],
    ) -> None:
        """Record the dropped entries' rows for their trees; consume the pulled tree's (D6).

        The pulled tree keeps the records of configs this pull did not rebuild
        (one skipped as locally modified, say); a rebuilt config's are now in
        its entry. A dropped entry's rows replace the records of its config in
        its own tree. Every tree's records are then pruned
        (:func:`_prune_retired`), so they never grow without bound.
        """
        rebuilt = {f"{c.component_id}/{c.id}" for c in pulled}
        records = {branch.path: retired_rows(manifest, branch.path) for branch in manifest.branches}
        records[tree] = {
            k: v for k, v in self.retired.items() if k.rsplit("/", 1)[0] not in rebuilt
        }
        for entry in self.dropped:
            own_tree = branch_tree_path(manifest, entry.branch_id)
            if own_tree == tree:
                continue
            bucket = records.setdefault(own_tree, {})
            prefix = f"{entry.component_id}/{entry.id}/"
            for key in [k for k in bucket if k.startswith(prefix)]:
                del bucket[key]
            for row in entry.rows:
                bucket[f"{prefix}{row.id}"] = {
                    "path": row.path,
                    "pull_hash": str(row.metadata.get("pull_hash", "") or ""),
                    "pull_config_hash": str(row.metadata.get("pull_config_hash", "") or ""),
                    UNKNOWN_BASE_KEY: str(row.metadata.get(UNKNOWN_BASE_KEY) or ""),
                }
        tracked = {f"{c.component_id}/{c.id}" for c in manifest.configurations}
        for branch in manifest.branches:
            kept = _prune_retired(records.get(branch.path, {}), tracked, project_root / branch.path)
            if kept:
                branch.metadata[RETIRED_ROWS_KEY] = kept
            else:
                branch.metadata.pop(RETIRED_ROWS_KEY, None)

    def hold_ignored_promotes(self, ignored_components: frozenset[str]) -> None:
        """Move the pulled branch's promote entries of ignored components to *kept* (risk 79).

        Pull never drops a promote entry because its component is ignored,
        whichever branch it is on: the stale sweep would, and once un-ignored
        the next promote created the config on its branch again. Kept, it is
        carried through untouched (:func:`finalize_kept_entries`).
        """
        held = [
            e
            for e in self.own
            if e.component_id in ignored_components and promoted_from(e) is not None
        ]
        self.own = [e for e in self.own if e not in held]
        self.kept.extend(held)

    def kept_paths_in(self, tree: str) -> set[str]:
        """Paths of the kept entries whose files live in *tree* (reserved by a pull of it)."""
        paths = set()
        for entry in self.kept:
            association = promoted_from(entry)
            if association is None or association.tree == tree:
                paths.add(entry.path)
        return paths


def free_path(base: str, suffix: str, taken: set[str]) -> str:
    """*base*, else ``base-suffix``, else ``base-suffix-2``, ... -- the first not in *taken*.

    Every candidate is checked (D26): a suffixed path can be owned too.
    """
    candidate, attempt = base, 1
    while candidate in taken:
        candidate = f"{base}-{suffix}" if attempt == 1 else f"{base}-{suffix}-{attempt}"
        attempt += 1
    return candidate


def partition_pull_entries(
    manifest: Manifest, project_root: Path, branch_id: int | None, branch_dir_name: str
) -> PullEntries:
    """Split the manifest into the pulled branch's entries and the ones to keep.

    Another branch's entry that is neither is dropped, as before: its files
    live in its own tree, which this pull re-targets away from (issue #649).
    An entry written before promote entries recorded ``promoted_from`` is kept
    when its file is in the pulled tree and not in its own.
    """
    own: list[ManifestConfiguration] = []
    kept: list[ManifestConfiguration] = []
    dropped: list[ManifestConfiguration] = []
    for entry in manifest.configurations:
        if same_branch(manifest, entry.branch_id, branch_id):
            own.append(entry)
            continue
        # A promote entry of ANY other branch is kept while its source tree
        # exists (D27): pulling a third branch must not lose the mapping, or
        # the next promote creates the config on its branch again.
        association = promoted_from(entry)
        if association is not None:
            if (project_root / association.tree).is_dir():
                kept.append(entry)
            continue
        registered = any(b.id == entry.branch_id for b in manifest.branches)
        own_tree = (
            branch_tree_path(manifest, entry.branch_id)
            if registered or is_production(manifest, entry.branch_id)
            else None
        )
        here = project_root / branch_dir_name / entry.path / CONFIG_FILENAME
        if here.exists() and (
            own_tree is None
            or not (project_root / own_tree / entry.path / CONFIG_FILENAME).exists()
        ):
            kept.append(entry)
        elif own_tree is not None and own_tree != branch_dir_name:
            dropped.append(entry)  # its rows are retired for its tree (D6)
    return PullEntries(
        own=own,
        kept=kept,
        dropped=dropped,
        retired=retired_rows(manifest, branch_dir_name),
    )


def retired_rows(manifest: Manifest, tree: str) -> dict[str, dict[str, str]]:
    """The rows of *tree* a re-targeting pull dropped, by ``"{component}/{config}/{row}"`` (D6).

    ``sync pull --branch DEV`` re-targets the manifest and drops production's
    entries; their files stay in ``main/``. The next production pull then
    rediscovered each config by the id in its file -- but not its rows: a row
    production deleted meanwhile was left on disk with no entry, and the next
    push created it again (walk seed 210). The dropped rows' bookkeeping is
    kept in that tree's branch metadata, so the next pull of the tree judges
    every such row by its own last baseline there.
    """
    for branch in manifest.branches:
        if branch.path == tree:
            raw = branch.metadata.get(RETIRED_ROWS_KEY)
            if isinstance(raw, dict):
                return {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}
    return {}


def _prune_retired(
    records: dict[str, dict[str, str]], tracked: set[str], tree_dir: Path
) -> dict[str, dict[str, str]]:
    """*records* minus those nothing can need any more (bounded growth, D6).

    A record goes only when BOTH hold: its config is in no manifest entry of
    any tree (a pull that rebuilt it consumed it already; one whose remote
    config is gone never rebuilds it), and no row dir under *tree_dir*
    carries one of that config's retired row ids. While a carrier survives
    on disk the record is what tells a later pull that its row was deleted
    on the remote, so it is never pruned then.
    """
    if not records:
        return {}
    carried: set[tuple[str, str]] = set()
    if tree_dir.is_dir():
        for row_file in tree_dir.rglob(CONFIG_FILENAME):
            if "rows" not in row_file.relative_to(tree_dir).parts[:-1]:
                continue
            try:
                data = yaml.safe_load(row_file.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                continue
            meta = data.get("_keboola") if isinstance(data, dict) else None
            if isinstance(meta, dict) and meta.get("row_id"):
                carried.add((str(meta.get("component_id") or ""), str(meta["row_id"])))
    by_config: dict[str, list[str]] = {}
    for key in records:
        parent, _, row_id = key.rpartition("/")
        by_config.setdefault(parent, []).append(row_id)
    keep = {
        parent
        for parent, row_ids in by_config.items()
        if parent in tracked
        or any((parent.split("/", 1)[0], row_id) in carried for row_id in row_ids)
    }
    return {key: value for key, value in records.items() if key.rpartition("/")[0] in keep}


def finalize_kept_entries(
    kept: list[ManifestConfiguration],
    pulled: list[ManifestConfiguration],
    branch_dir: Path,
    ignored_components: frozenset[str],
) -> list[ManifestConfiguration]:
    """The promote entries to carry over after a pull wrote *pulled*.

    A promote entry follows its source config when this pull moved that
    config's directory (a rename, D13a), so the next promote still finds it
    and does not create the config on the branch again. An entry of an ignored
    component is carried untouched: the ignore filter keeps it out of diff and
    push, and it is back in scope, still associated, once the component is
    un-ignored -- dropping it made the next promote create a duplicate (risk
    75). It is dropped -- its files are never touched -- only when its source
    config is gone from the manifest and its directory is gone too (D13c).
    """
    paths = {(c.component_id, c.id): c.path for c in pulled}
    carried: list[ManifestConfiguration] = []
    for entry in kept:
        if entry.component_id in ignored_components:
            carried.append(entry)  # untouched until the component is un-ignored
            continue
        association = promoted_from(entry)
        if association is not None and branch_dir.parent / association.tree != branch_dir:
            carried.append(entry)  # its source tree was not pulled: unchanged (D27)
            continue
        source = association.config_id if association is not None else ""
        if source and (entry.component_id, source) in paths:
            entry.path = paths[(entry.component_id, source)]
            carried.append(entry)
        elif (branch_dir / entry.path / CONFIG_FILENAME).exists():
            carried.append(entry)
    return carried


@dataclass
class IdDirs:
    """Untracked directories that carry an id in their ``_config.yml``.

    Attributes:
        by_id: id key -> the one directory carrying it.
        paths: Every such directory, ambiguous ones included (all reserved).
        ambiguous: id key -> the directories carrying it, when more than one
            does (a copied directory). Pull adopts none of them.
    """

    by_id: dict[str, str] = field(default_factory=dict)
    paths: set[str] = field(default_factory=set)
    ambiguous: dict[str, list[str]] = field(default_factory=dict)

    def add(self, key: str, path: str) -> None:
        """Record that *path* carries *key*."""
        self.paths.add(path)
        if key in self.ambiguous:
            self.ambiguous[key].append(path)
        elif key in self.by_id:
            self.ambiguous[key] = [self.by_id.pop(key), path]
        else:
            self.by_id[key] = path


def scan_config_dirs(
    service: SyncService, project_root: Path, manifest: Manifest, branch_dir_name: str
) -> IdDirs:
    """Untracked config dirs of the pulled tree, by ``"component/config_id"``.

    A config pull has no entry for -- the manifest was re-targeted to another
    branch since this tree was pulled, or the entry was lost -- still has its
    directory here, and the directory's ``_config.yml`` says which config it
    is. Pull writes into that directory instead of a new one named after the
    config: a name changed since (on either side) used to leave the old
    directory behind as an untracked copy that push then created again.
    """
    found = IdDirs()
    for item in find_untracked_configs(
        project_root, manifest, service._read_config_file, only_branch_path=branch_dir_name
    ):
        if item["config_id"]:
            found.add(f"{item['component_id']}/{item['config_id']}", item["path"])
        else:
            found.paths.add(item["path"])  # authored, never pushed: reserved too (D26)
    return found


def scan_row_dirs(service: SyncService, config_dir: Path) -> IdDirs:
    """Row dirs of a config dir, by ``row_id``; ``paths`` holds every row dir.

    Every existing row directory is reserved, with or without an id: a new
    remote row named like a row authored locally (no id yet) must not be
    written into it.
    """
    found = IdDirs()
    rows_dir = config_dir / "rows"
    if not rows_dir.is_dir():
        return found
    for row_dir in sorted(rows_dir.iterdir()):
        if not (row_dir / CONFIG_FILENAME).exists():
            continue
        rel = f"rows/{row_dir.name}"
        meta = (service._read_config_file(row_dir) or {}).get("_keboola") or {}
        row_id = str(meta.get("row_id") or "")
        if row_id:
            found.add(row_id, rel)
        else:
            found.paths.add(rel)
    return found


def ambiguous_id_warnings(dirs: IdDirs, *, parent: str = "") -> list[dict[str, Any]]:
    """Pull ``warnings[]`` entries for ids carried by more than one directory."""
    kind = "row" if parent else "config"
    return [
        {
            "change_type": f"ambiguous_{kind}_id",
            "id": f"{parent}/rows/{key}" if parent else key,
            "paths": sorted(paths),
            "message": (
                f"{len(paths)} local {kind} directories carry {kind} id {key}: "
                f"{', '.join(sorted(paths))}. Pull wrote into none of them; keep one "
                f"and remove the others' '{'row_id' if parent else 'config_id'}'."
            ),
        }
        for key, paths in sorted(dirs.ambiguous.items())
    ]


def carry_promote_metadata(
    old: ManifestConfiguration | None,
    metadata: dict[str, Any],
    pulled: RowPullResult,
) -> None:
    """Carry an entry's promote bookkeeping into the entry a pull rebuilt for it.

    A pending rebind, the source config the entry was promoted from and its
    rows' source ids survive a pull of the entry's branch, so a later push
    still finishes the rewrite. ``promoted_from`` itself is not carried: the
    pull has just written the entry's files into its own tree.

    The record of rows the remote deleted (``deleted_rows``, D7) is merged:
    the old record, plus the rows this pull removed, minus any id the remote
    lists again (a rollback brought it back).
    """
    rows = pulled.rows
    recorded = old.metadata.get(DELETED_ROWS_KEY) if old is not None else None
    deleted = dict(recorded) if isinstance(recorded, dict) else {}
    deleted.update(pulled.deleted_rows)
    for row in rows:
        deleted.pop(row.id, None)
    if deleted:
        metadata[DELETED_ROWS_KEY] = deleted
    if old is None:
        return
    # Also the placeholder a config was created from and an unfinished link
    # backfill (D9): a pull between the failed push and the next one must
    # not lose what the next one needs to finish the links.
    for key in (
        REBIND_PENDING_KEY,
        CREATED_FROM_KEY,
        LINK_PENDING_KEY,
        ADOPTED_ROW_IDS_KEY,
        AMBIGUOUS_ROW_PLACEHOLDERS_KEY,
        FALLBACK_LINKS_KEY,
    ):
        if old.metadata.get(key):
            metadata[key] = old.metadata[key]
    association = promoted_from(old)
    source = association.config_id if association is not None else ""
    source = source or str(old.metadata.get(PROMOTED_SOURCE_KEY) or "")
    if source:
        metadata[PROMOTED_SOURCE_KEY] = source
    old_rows = {row.id: row.metadata for row in old.rows}
    for row in rows:
        for key in (PROMOTED_ROW_KEY, AUTHORED_ROW_KEY, CREATED_FROM_KEY):
            if key in old_rows.get(row.id, {}):
                row.metadata[key] = old_rows[row.id][key]


@dataclass
class RowPullResult:
    """The manifest rows of one pulled config and what the pull wrote."""

    rows: list[ManifestConfigRow]
    files_written: int = 0
    rows_pulled: int = 0
    # Row id -> last ``pull_config_hash`` of each row removed because the
    # remote deleted it (D7, ``deleted_rows`` in the config's metadata).
    deleted_rows: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RowPullContext:
    """The config whose rows one pull writes, and how."""

    component_id: str
    config_id: str
    config_name: str
    rel_path: str
    config_dir: Path
    row_naming: str
    adopt: bool
    theirs: bool
    dry_run: bool


def pull_config_rows(
    service: SyncService,
    ctx: RowPullContext,
    remote_rows: list[dict[str, Any]],
    existing_rows: dict[str, dict[str, str]],
    pull_details: list[dict[str, str]],
    warnings: list[dict[str, Any]],
) -> RowPullResult:
    """Write a config's rows; each gets its own 3-way diff state.

    ``pull_hash`` / ``pull_config_hash`` per row let ``sync push`` detect
    per-row local modifications and push only changed rows. A row pull has no
    baseline for (a rediscovered config, ``ctx.adopt``, or a row dir carrying
    the id with no manifest row) keeps a local file that differs from the
    remote, like the #792 E config guard; only ``--theirs`` overwrites it. Its
    manifest row then has no baseline, so diff reports it ``modified``.
    """
    result = RowPullResult(rows=[])
    dirs = scan_row_dirs(service, ctx.config_dir)
    warnings += ambiguous_id_warnings(dirs, parent=f"{ctx.component_id}/{ctx.config_id}")
    # A row dir carrying an id is adopted by that id under a tracked parent
    # too, unless a manifest row owns its path (D22 guards a differing file).
    prefix = f"{ctx.component_id}/{ctx.config_id}/"
    carried = _follow_moved_rows(ctx.config_dir, existing_rows, prefix, dirs)
    owned = {v["path"] for k, v in existing_rows.items() if k.startswith(prefix)}
    adopted = {rid: path for rid, path in dirs.by_id.items() if path not in owned}
    used: set[str] = set()
    for row in remote_rows:
        row_id = str(row.get("id", ""))
        row_name = row.get("name", "untitled")
        existing = existing_rows.get(f"{ctx.component_id}/{ctx.config_id}/{row_id}")
        if existing:
            row_rel_path = existing["path"]
        elif row_id in adopted:
            row_rel_path = adopted[row_id]
        else:
            row_rel_path = free_path(
                config_row_path(ctx.row_naming, row_name), row_id[:8], used | dirs.paths
            )
        used.add(row_rel_path)
        row_dir = ctx.config_dir / row_rel_path
        result.rows.append(
            _pull_row(service, ctx, row, row_rel_path, row_dir, existing, pull_details, result)
        )
    # A tracked row that is gone from the remote (issue #792 H).
    result.rows += sweep_stale_rows(
        service,
        ctx.config_dir,
        existing_rows,
        f"{ctx.component_id}/{ctx.config_id}/",
        {str(row.get("id", "")) for row in remote_rows},
        theirs=ctx.theirs,
        dry_run=ctx.dry_run,
        rel_path=ctx.rel_path,
        pull_details=pull_details,
        carried=carried,
        deleted_rows=result.deleted_rows,
    )
    return result


def _follow_moved_rows(
    config_dir: Path, existing_rows: dict[str, dict[str, str]], prefix: str, dirs: IdDirs
) -> set[str]:
    """Point each tracked row whose dir is gone at the one dir that now carries its id (round 7).

    A row dir moved (renamed) by the user is that row at its new path: pull
    then writes into it, and the stale sweep judges it there -- unedited and
    gone on the remote, it is removed; edited, it is kept and reported.
    Before, the sweep looked at the old path only, dropped the entry and left
    the moved dir, which the next push created as a new row. Returns the ids
    of tracked rows whose dir is gone while two or more dirs carry the id:
    those entries are never swept (the id is never forgotten while a dir
    carries it).
    """
    ambiguous: set[str] = set()
    owned = {v["path"] for k, v in existing_rows.items() if k.startswith(prefix)}
    for key, tracked in existing_rows.items():
        row_id = key.removeprefix(prefix)
        if row_id == key:
            continue
        path = current_row_path(config_dir, row_id, tracked["path"], dirs, owned)
        if path is None:
            ambiguous.add(row_id)
        else:
            tracked["path"] = path
    return ambiguous


def current_row_path(
    config_dir: Path, row_id: str, tracked_path: str, dirs: IdDirs, owned: set[str]
) -> str | None:
    """Where a tracked row's dir is now: its manifest path, or the one dir moved there.

    The one rule pull and the ``pull --force`` preflight share (Devin
    4400588073): a tracked row whose own dir is gone is at the one dir that
    carries its id and no other tracked row owns. ``None`` when several dirs
    carry the id (ambiguous); the manifest path when it exists or nothing
    carries the id.
    """
    if (config_dir / tracked_path / CONFIG_FILENAME).exists():
        return tracked_path
    if row_id in dirs.ambiguous:
        return None
    moved = dirs.by_id.get(row_id)
    return moved if moved is not None and moved not in owned else tracked_path


def _pull_row(
    service: SyncService,
    ctx: RowPullContext,
    row: dict[str, Any],
    row_rel_path: str,
    row_dir: Path,
    existing: dict[str, str] | None,
    pull_details: list[dict[str, str]],
    result: RowPullResult,
) -> ManifestConfigRow:
    """Write (or keep) one row file and return its manifest row."""
    row_local = api_row_to_local(row, ctx.component_id)
    row_api_cfg_hash = config_hash(row_local)
    row_file = row_dir / CONFIG_FILENAME
    old_file_hash = existing["pull_hash"] if existing else ""
    old_cfg_hash = existing["pull_config_hash"] if existing else ""
    detail = {
        "action": "skipped",
        "component_id": ctx.component_id,
        "config_name": f"{ctx.config_name}/{row.get('name', 'untitled')}",
        "path": f"{ctx.rel_path}/{row_rel_path}",
    }
    # Runs even under ``--force``: a force-pull reaching a modified row has
    # passed the conflict guard (remote unchanged), so preserve the row rather
    # than re-stamp its baseline from the edited file and strand the edits.
    # ``--theirs`` disables the preserve: remote wins.
    if not ctx.theirs and row_file.exists():
        if old_file_hash and service._file_hash(row_file) != old_file_hash:
            pull_details.append({**detail, "reason": "row locally modified"})
            return _row_entry(row, row_rel_path, old_file_hash, old_cfg_hash)
        local = service._read_config_file(row_dir)
        if not old_file_hash and local is not None and config_hash(local) != row_api_cfg_hash:
            # No baseline: pull cannot tell a stale file from an edited one. It
            # keeps the file, and the row's base stays unknown (D22) -- never a
            # baseline stamped from the remote, which made push send the stale
            # file over a remote edit.
            pull_details.append({**detail, "reason": ADOPTED_ROW_SKIP_REASON})
            entry = _row_entry(row, row_rel_path, "", "")
            entry.metadata[UNKNOWN_BASE_KEY] = UNKNOWN_BASE_ADOPTED
            return entry
    if (
        existing
        and old_cfg_hash
        and old_cfg_hash == row_api_cfg_hash
        and row_file.exists()
        and (not ctx.theirs or service._file_hash(row_file) == old_file_hash)
    ):
        # Idempotent: remote unchanged since last pull, file untouched. The
        # existence guard keeps a new directory (first pull of a dev branch
        # that clones main -- same hash, no file yet) from being skipped.
        return _row_entry(row, row_rel_path, service._file_hash(row_file), row_api_cfg_hash)
    # New or remote-changed row: write the file, and say so (D5) -- a row
    # added in the UI used to arrive without a word.
    pull_details.append(
        {
            **detail,
            "action": "updated" if existing or row_file.exists() else "new",
            "row_id": str(row.get("id", "")),
        }
    )
    file_hash = ""
    if not ctx.dry_run:
        service._write_config_file(row_dir, row_local)
        file_hash = service._file_hash(row_file) if row_file.exists() else ""
    result.files_written += 1
    result.rows_pulled += 1
    return _row_entry(row, row_rel_path, file_hash, row_api_cfg_hash)


def _row_entry(
    row: dict[str, Any], row_rel_path: str, file_hash: str, cfg_hash: str
) -> ManifestConfigRow:
    return ManifestConfigRow(
        id=str(row.get("id", "")),
        path=row_rel_path,
        metadata={"pull_hash": file_hash, "pull_config_hash": cfg_hash},
    )
