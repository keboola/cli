"""A config directory moved by hand keeps its identity (issue #792 finding I).

A plain ``mv`` / ``git mv`` of a tracked config directory used to read as two
changes: the manifest entry had no file left (``deleted``) and the moved
directory carried an id that a manifest entry of the same tree claimed, which
is the fork-by-copy rule (``added``). ``push`` created a new config and
``push --force`` deleted the original -- job history, schedules and flow
references gone.

A **move** is detected before diff, push and pull scope the manifest:

- a tracked entry of the tree whose ``_config.yml`` is ABSENT (a file that is
  there but cannot be read -- invalid YAML -- is not absence: no move), whose
  component is not ignored and that was materialized once (it has a
  ``pull_hash``; a never-fetched entry is no move source);
- exactly ONE directory of the same tree that no manifest entry owns and whose
  ``_config.yml`` carries that entry's identity: the same ``_keboola``
  ``config_id`` and the same ``_keboola`` ``component_id``. The component is
  the logical one from the file, not the physical component directory, so a
  move into another component's directory is a move too (pull later moves it
  back where the naming template puts it).

The entry's path is relocated (rows are relative to it and follow), and the
config is then classified as usual against the entry's own baseline:
``unchanged``, ``modified``, ``conflict``, ``remote_modified`` or
``remote_deleted``. A move is never a change type of its own -- the change
carries ``moved_from`` / ``moved_to``, and the result lists ``moves``.

Promote entries (``promoted_from``, a dev-branch entry whose files are the
production entry's) share their source's directory and its identity: they are
relocated with it, ids, hashes and row mappings unchanged.

When the original is gone and TWO or more directories carry its identity
(``mv A B; cp -r B C``), nothing says which one is the config: the move is
**ambiguous**. Diff and push create nothing and delete nothing for it (also
with ``--force``), report a ``conflict`` change with ``ambiguous_move`` and
the candidate paths, and keep the manifest entry where it is; pull writes into
none of the directories. With the original still in place, a copy stays a
fork (``added``), as before.

Diff and ``push --dry-run`` only relocate in memory. Push applies the moves to
its own manifest and saves them with the rest of its bookkeeping -- also when
nothing else changed. A push that stops before it saves leaves the manifest
as it was; the next run detects the same move again.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..constants import CONFIG_FILENAME
from ..sync.branch_scope import config_key, entry_tree, find_branch_entry, promoted_from
from ..sync.manifest import Manifest, ManifestConfiguration, load_manifest, save_manifest
from ._sync_workspace import effective_ignored_components

if TYPE_CHECKING:
    from .sync_service import SyncService

MOVES_KEY = "moves"
AMBIGUOUS_MOVES_KEY = "ambiguous_moves"


@dataclass(frozen=True)
class ConfigMove:
    """One manifest entry relocated to the directory its config was moved to."""

    component_id: str
    config_id: str
    branch_id: int
    moved_from: str
    moved_to: str

    def to_dict(self) -> dict[str, Any]:
        """The ``moves[]`` record."""
        return {
            "component_id": self.component_id,
            "config_id": self.config_id,
            "branch_id": self.branch_id,
            "moved_from": self.moved_from,
            "moved_to": self.moved_to,
        }


@dataclass(frozen=True)
class AmbiguousMove:
    """A missing config directory whose identity several directories carry."""

    component_id: str
    config_id: str
    path: str
    candidates: tuple[str, ...]
    entry_keys: tuple[str, ...]

    def message(self) -> str:
        """Why nothing is pushed for it, and what to do."""
        return (
            f"The directory of config {self.config_id} ({self.path}) is gone and "
            f"{len(self.candidates)} directories carry its id: {', '.join(self.candidates)}. "
            "Push creates and deletes nothing for it, also with --force. Keep one of them "
            "(remove the others, or remove their _keboola.config_id to make them new "
            "configs), or use `kbagent config rename --directory`."
        )

    def change(self) -> dict[str, Any]:
        """The ``conflict`` change that holds it back in diff and push."""
        return {
            "change_type": "conflict",
            "component_id": self.component_id,
            "config_id": self.config_id,
            "config_name": "",
            "path": self.path,
            "details": [self.message()],
            "ambiguous_move": True,
            "candidates": list(self.candidates),
        }

    def to_dict(self) -> dict[str, Any]:
        """The ``ambiguous_moves[]`` record."""
        return {
            "component_id": self.component_id,
            "config_id": self.config_id,
            "path": self.path,
            "candidates": list(self.candidates),
            "message": self.message(),
        }


@dataclass
class MovePlan:
    """The moves of one tree, and the ambiguous ones held back."""

    moves: list[ConfigMove] = field(default_factory=list)
    ambiguous: list[AmbiguousMove] = field(default_factory=list)

    @property
    def held_keys(self) -> set[str]:
        """Config keys of the entries an ambiguous move holds."""
        return {key for item in self.ambiguous for key in item.entry_keys}

    @property
    def held_paths(self) -> set[str]:
        """Candidate directories of an ambiguous move: never created, never adopted."""
        return {path for item in self.ambiguous for path in item.candidates}

    def report(self, result: dict[str, Any]) -> dict[str, Any]:
        """Add the moves to a ``sync diff`` result (``moves``, ``ambiguous_moves``)."""
        moved = {(m.component_id, m.config_id): m for m in self.moves}
        for change in result["changes"]:
            move = moved.get((change["component_id"], change["config_id"]))
            if move is not None and not change.get("is_row"):
                change["moved_from"], change["moved_to"] = move.moved_from, move.moved_to
        held = self.held_keys
        result["remote_only"] = [
            r
            for r in result["remote_only"]
            if config_key(r["component_id"], r["config_id"]) not in held
        ]
        result["changes"] += [item.change() for item in self.ambiguous]
        summary = result["summary"]
        summary["remote_only"] = len(result["remote_only"])
        summary["conflict"] += len(self.ambiguous)
        summary["moved"] = len(self.moves)
        if self.moves:
            result[MOVES_KEY] = [m.to_dict() for m in self.moves]
        if self.ambiguous:
            result[AMBIGUOUS_MOVES_KEY] = [item.to_dict() for item in self.ambiguous]
        return result

    def pull_details(self) -> list[dict[str, str]]:
        """Pull ``details[]`` records: one ``moved`` per move, one ``skipped`` per ambiguous."""
        details = [
            {
                "action": "moved",
                "component_id": m.component_id,
                "config_id": m.config_id,
                "config_name": Path(m.moved_to).name,
                "path": m.moved_to,
                "old_path": m.moved_from,
            }
            for m in self.moves
        ]
        details += [
            {
                "action": "skipped",
                "component_id": item.component_id,
                "config_id": item.config_id,
                "config_name": Path(item.path).name,
                "path": item.path,
                "reason": "ambiguous move: " + item.message(),
            }
            for item in self.ambiguous
        ]
        return details


def _identity(entry: ManifestConfiguration) -> str:
    """The config id the entry's ``_config.yml`` carries (a promote entry: its source's)."""
    association = promoted_from(entry)
    if association is not None and association.config_id:
        return association.config_id
    return entry.id


def _carriers(
    service: SyncService,
    tree_dir: Path,
    owned: set[str],
    ignored: frozenset[str],
) -> dict[tuple[str, str], list[str]]:
    """Unowned config dirs of the tree by the ``(component_id, config_id)`` they carry."""
    found: dict[tuple[str, str], list[str]] = defaultdict(list)
    if not tree_dir.is_dir():
        return found
    for config_file in sorted(tree_dir.rglob(CONFIG_FILENAME)):
        rel = config_file.parent.relative_to(tree_dir)
        if not rel.parts or "rows" in rel.parts or rel.as_posix() in owned:
            continue
        data = service._read_config_file(config_file.parent)
        meta = data.get("_keboola") if isinstance(data, dict) else None
        if not isinstance(meta, dict) or not meta.get("config_id"):
            continue
        component_id = str(meta.get("component_id") or "")
        if component_id in ignored:
            continue
        found[(component_id, str(meta["config_id"]))].append(rel.as_posix())
    return found


def settle_moves(
    service: SyncService,
    manifest: Manifest,
    project_root: Path,
    tree: str,
    ignored: frozenset[str],
    *,
    drop_held: bool,
) -> MovePlan:
    """Detect the moves of *tree*, relocate their entries in *manifest* (in memory).

    *drop_held* removes the entries of an ambiguous move from *manifest* (diff:
    no delete is planned for them); pull keeps them and writes nothing for
    them (``MovePlan.held_keys``).
    """
    in_tree = [c for c in manifest.configurations if entry_tree(manifest, c) == tree]
    groups: dict[tuple[str, str, str], list[ManifestConfiguration]] = defaultdict(list)
    for entry in in_tree:
        if (
            entry.component_id in ignored
            or not entry.metadata.get("pull_hash")
            or (project_root / tree / entry.path / CONFIG_FILENAME).exists()
        ):
            continue
        groups[(entry.path, entry.component_id, _identity(entry))].append(entry)
    plan = MovePlan()
    if not groups:
        return plan
    carriers = _carriers(service, project_root / tree, {c.path for c in in_tree}, ignored)
    claimed: dict[str, int] = defaultdict(int)
    for _path, component_id, identity in groups:
        for candidate in carriers.get((component_id, identity), []):
            claimed[candidate] += 1
    for (path, component_id, identity), entries in groups.items():
        candidates = carriers.get((component_id, identity), [])
        if not candidates:
            continue  # a local delete
        if len(candidates) == 1 and claimed[candidates[0]] == 1:
            for entry in entries:
                plan.moves.append(
                    ConfigMove(entry.component_id, entry.id, entry.branch_id, path, candidates[0])
                )
                entry.path = candidates[0]
            continue
        plan.ambiguous.append(
            AmbiguousMove(
                component_id=component_id,
                config_id=identity,
                path=path,
                candidates=tuple(sorted(candidates)),
                entry_keys=tuple(config_key(e.component_id, e.id) for e in entries),
            )
        )
        if drop_held:
            manifest.configurations = [
                c for c in manifest.configurations if all(c is not e for e in entries)
            ]
    return plan


def status_moves(service: SyncService, manifest: Manifest, project_root: Path) -> list[dict]:
    """``sync status``: relocate the moves of every tree in memory, return their records.

    Status reads no remote, so it relocates every branch tree; an ambiguous
    move stays as it is (its entry ``deleted``, its candidates ``added``).
    """
    ignored = effective_ignored_components(manifest)
    trees = sorted({b.path for b in manifest.branches} or {"main"})
    return [
        move.to_dict()
        for tree in trees
        for move in settle_moves(
            service, manifest, project_root, tree, ignored, drop_held=False
        ).moves
    ]


def relocate(manifest: Manifest, diff_result: dict[str, Any]) -> int:
    """Apply the ``moves`` of a diff result to *manifest*; return how many applied."""
    applied = 0
    for move in diff_result.get(MOVES_KEY) or []:
        for entry in manifest.configurations:
            if (
                entry.component_id == move["component_id"]
                and entry.id == move["config_id"]
                and entry.branch_id == move["branch_id"]
                and entry.path == move["moved_from"]
            ):
                entry.path = move["moved_to"]
                applied += 1
    return applied


def persist_moves(project_root: Path, diff_result: dict[str, Any], *, dry_run: bool) -> None:
    """Save a push's moves when nothing else is pushed (an unchanged pure move)."""
    if dry_run or not diff_result.get(MOVES_KEY):
        return
    manifest = load_manifest(project_root)
    if relocate(manifest, diff_result):
        save_manifest(project_root, manifest)


def move_fields(diff_result: dict[str, Any]) -> dict[str, Any]:
    """The ``moves`` / ``ambiguous_moves`` of a diff, for every push result."""
    return {k: diff_result[k] for k in (MOVES_KEY, AMBIGUOUS_MOVES_KEY) if diff_result.get(k)}


def rename_entries(
    manifest: Manifest, component_id: str, config_id: str, branch_id: int | None
) -> list[ManifestConfiguration]:
    """The entries ``config rename --directory`` relocates (B8).

    The entry of *branch_id* (the first matching entry when that branch has
    none), and the promote entries that share its directory as their source.
    Empty when the manifest does not track the config.
    """
    target = find_branch_entry(manifest, component_id, config_id, branch_id)
    if target is None:
        target = next(
            (
                c
                for c in manifest.configurations
                if (c.component_id, c.id) == (component_id, config_id)
            ),
            None,
        )
    if target is None:
        return []
    tree = entry_tree(manifest, target)
    associates = [
        c
        for c in manifest.configurations
        if c is not target
        and c.component_id == component_id
        and c.path == target.path
        and entry_tree(manifest, c) == tree
        and _identity(c) == _identity(target)
    ]
    return [target, *associates]
