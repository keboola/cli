"""Branch-scoped view of a sync manifest (issue #649).

``sync diff`` / ``sync push`` read local configs from exactly ONE on-disk tree
-- the *source branch path* resolved by
:meth:`SyncService._resolve_source_branch_path`. The manifest, however, is a
flat list that may reference several trees at once: ``sync pull --branch <dev>``
re-targets every entry to the dev branch and materializes a ``<branch>/``
subtree, leaving the previously pulled ``main/`` tree orphaned on disk.

Mixing the two views is what made a production diff after a dev pull report the
whole orphaned ``main/`` tree as ``added`` (issue #649): the ids in those files
were "claimed" by manifest entries, but the claim came from a *different
branch*, and the claim check had no branch dimension. The same flat view made
dev-only configs (tracked on the dev branch, absent from production) diff
against production as ``added`` **with** an id.

This module supplies the missing branch dimension:

* :func:`branch_tree_path` maps any branch id to its on-disk tree name. It is
  the canonical normalizer -- production is spelled three different ways in the
  wild (``None`` from the CLI, ``0`` from a git-branching pull, and the default
  branch's numeric id from a plain pull) and all three must resolve to the same
  tree before anything is compared.
* :func:`scope_manifest` partitions ``manifest.configurations`` into the
  entries that belong to the source tree (the only ones diff/push may act on)
  and the ones that belong to another tree, and records which trees claim each
  config id.
* :func:`classify_untracked` decides what an untracked file in the source tree
  means, preserving the #482/#497 fork-by-copy contract.
* :func:`find_untracked_configs` / :func:`find_untracked_rows` walk the trees
  for files the manifest does not know about. They live here because "which
  branch tree may I look at" is the only question they ask.

Restricting the local side to the source tree is not merely tidy: ``push``
already reads every file through the source path (see
``_sync_push_ops.push_update``), so an entry diffed from another tree would be
pushed from a *different* file than the one that produced the classification --
or fail outright with ``FileNotFoundError``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..constants import CONFIG_FILENAME
from .manifest import Manifest, ManifestConfiguration

logger = logging.getLogger(__name__)

# Reads a ``_config.yml`` from a config directory, ``None`` when absent or
# unparseable (``SyncService._read_config_file``).
ConfigReader = Callable[[Path], "dict[str, Any] | None"]

# ``orphaned[].reason`` values (stable, part of the --json contract).
REASON_OTHER_BRANCH = "tracked_on_other_branch"
REASON_STALE_TREE = "stale_branch_tree"
REASON_PROMOTED = "promoted_from_this_tree"
REASON_PROMOTED_ELSEWHERE = "promoted_from_other_tree"

# ``classify_untracked`` verdicts.
VERDICT_CREATE = "create"
VERDICT_ADOPT = "adopt"
VERDICT_ORPHAN = "orphan"

_DEFAULT_TREE_FALLBACK = "main"


def config_key(component_id: str, config_id: str) -> str:
    """Build the ``"{component_id}/{config_id}"`` key used across the engine."""
    return f"{component_id}/{config_id}"


def branch_tree_path(manifest: Manifest, branch_id: int | None) -> str:
    """Return the on-disk tree name for *branch_id*.

    ``None`` means production, which lives in the default branch tree
    (``manifest.branches[0]``). An id that is not registered in the manifest
    falls back to the same tree -- that covers the legacy ``branchId: 0``
    production entries written by a git-branching pull, and any id that has not
    been through ``_ensure_branch_registered`` yet.
    """
    if branch_id is None:
        return manifest.branches[0].path if manifest.branches else _DEFAULT_TREE_FALLBACK
    for branch in manifest.branches:
        if branch.id == branch_id:
            return branch.path
    logger.warning(
        "Branch ID %s not found in manifest, falling back to default path",
        branch_id,
    )
    return manifest.branches[0].path if manifest.branches else _DEFAULT_TREE_FALLBACK


def is_production(manifest: Manifest, branch_id: int | None) -> bool:
    """True iff *branch_id* spells production: ``None``, ``0`` or the default branch id.

    The default branch is ``manifest.branches[0]`` (``sync init`` registers it
    first). Any other id is a dev branch, registered or not.
    """
    if not branch_id:
        return True
    return bool(manifest.branches) and branch_id == manifest.branches[0].id


def same_branch(manifest: Manifest, a: int | None, b: int | None) -> bool:
    """True iff branch ids *a* and *b* name the same branch.

    The one comparison every manifest site uses to decide whether an entry
    belongs to the branch it reads or writes: production is spelled ``None``,
    ``0`` or the default branch's numeric id, and a raw ``==`` would call those
    three different branches. Two dev-branch ids are the same branch only when
    they are equal. It deliberately does not compare tree paths:
    :func:`branch_tree_path` maps an id that is not registered in the manifest
    to the default tree, and two different unregistered ids (or one and
    production) would then compare equal -- a dev write matched the production
    entry.
    """
    a_prod, b_prod = is_production(manifest, a), is_production(manifest, b)
    if a_prod or b_prod:
        return a_prod and b_prod
    return a == b


def target_tree_is_source(manifest: Manifest, project_root: Path, branch_id: int | None) -> bool:
    """True iff the target branch's own tree is what diff/push read, even with no file left.

    Without a config file under the target tree, diff/push fall back to the
    default tree (the promote path, KFR-07 / #929). A dev-branch tree that is
    registered and still on disk is that branch's own checkout, though: only
    ``sync pull --branch`` (or ``config new --push`` on that branch) creates
    it -- a promote push registers the branch but never creates its directory.
    So an emptied checkout means "every config deleted": the push must delete,
    and once ``--force`` has deleted the last one (and its manifest entry), the
    next push must still read the empty tree instead of promoting ``main/``
    onto the branch again. A promote push written before the ``promoted_from``
    association was recorded left target-branch entries without one but no
    target directory, so it keeps reading ``main/``.
    """
    if is_production(manifest, branch_id):
        return False
    if not any(branch.id == branch_id for branch in manifest.branches):
        return False
    target_path = branch_tree_path(manifest, branch_id)
    if target_path == branch_tree_path(manifest, None):
        return False
    return (project_root / target_path).is_dir()


# Row metadata: the row's base is unknown (D22). ``adopted``: pull kept a local
# row file it has no baseline for, the row was on the branch then (D15).
# ``unread``: a promote shadow could not read the branch's rows, so whether
# the row is there at all is unknown. Diff calls any local/remote difference
# a conflict and a local delete a conflict; push never sends or deletes such
# a row; pull never overwrites or deletes it. ``pull --theirs``, or a pull
# that finds local == remote, gives it a baseline again.
UNKNOWN_BASE_KEY = "unknown_base"
UNKNOWN_BASE_ADOPTED = "adopted"
UNKNOWN_BASE_UNREAD = "unread"
# The 3-way base diff uses for such a row: equal to neither side.
UNKNOWN_BASE_HASH = "<unknown base>"

# Metadata key of a target-branch entry recorded by a promote push
# (``sync push --branch <dev>`` reading ``main/``): which tree the files live in
# and which source-tree config the entry stands for (issue #792 D).
PROMOTED_FROM_KEY = "promoted_from"


@dataclass(frozen=True)
class PromotedFrom:
    """Where a promote entry's files live: a source tree and a config there.

    ``config_id`` is the source-tree config the entry shadows, or ``""`` for a
    config authored in the source tree and never created on its branch (then
    the entry's ``path`` alone names the directory).
    """

    tree: str
    config_id: str

    def as_metadata(self) -> dict[str, str]:
        """The manifest metadata value recording this association."""
        return {"tree": self.tree, "config_id": self.config_id}


def promoted_from(cfg: ManifestConfiguration) -> PromotedFrom | None:
    """The promote association an entry records, or ``None``."""
    raw = cfg.metadata.get(PROMOTED_FROM_KEY)
    if not isinstance(raw, dict) or not raw.get("tree"):
        return None
    return PromotedFrom(tree=str(raw["tree"]), config_id=str(raw.get("config_id") or ""))


def entry_tree(manifest: Manifest, cfg: ManifestConfiguration) -> str:
    """The tree an entry's files live in: a promote entry's source tree, else its branch's."""
    association = promoted_from(cfg)
    return association.tree if association else branch_tree_path(manifest, cfg.branch_id)


def find_branch_entry(
    manifest: Manifest, component_id: str, config_id: str, branch_id: int | None
) -> ManifestConfiguration | None:
    """Return the entry of ``component_id/config_id`` on *branch_id*, if any.

    A dev branch created from production holds its configs under the SAME ids,
    so a lookup on ``(component_id, config_id)`` alone can return the entry of
    another branch -- and a write to the dev branch then re-stamped the
    production entry. Every write site looks its entry up through here.
    """
    for cfg in manifest.configurations:
        if (
            cfg.component_id == component_id
            and cfg.id == config_id
            and same_branch(manifest, cfg.branch_id, branch_id)
        ):
            return cfg
    return None


@dataclass(frozen=True)
class Claim:
    """A manifest entry laying claim to a config id, and the tree it sits in."""

    branch_id: int
    tree_path: str


@dataclass(frozen=True)
class TreeScope:
    """``manifest.configurations`` partitioned for one source tree.

    Attributes:
        in_tree: Entries whose files live in the source tree. These are the
            only entries diff/push may act on.
        never_fetched: Entries registered but never materialized (issue #472),
            excluded before any branch reasoning happens.
        orphaned: Report records for entries that belong to another tree.
        claims: ``config_key`` -> the claims held on it by *any* tree.
        on_target: The ``in_tree`` entries fetched from the push target branch
            itself (they carry a ``pull_hash``). Not the production entries a
            promote push reads from ``main/``, a hand-authored placeholder, or
            a ``sync clone`` copy: none of them was ever on the target.
    """

    in_tree: list[ManifestConfiguration]
    never_fetched: list[dict[str, str]]
    orphaned: list[dict[str, Any]]
    claims: dict[str, list[Claim]]
    on_target: list[ManifestConfiguration]

    @property
    def in_tree_paths(self) -> set[str]:
        """Source-tree directories an ``in_tree`` entry already accounts for.

        On the promote path a target-branch entry's files live in the source
        tree although its own tree is another one, so the untracked-file walk
        (which maps every entry to its own tree) would see its directory a
        second time.
        """
        return {cfg.path for cfg in self.in_tree}

    @property
    def tracked_keys(self) -> set[str]:
        """Keys diff/push treat as tracked -- source tree only."""
        return {config_key(cfg.component_id, cfg.id) for cfg in self.in_tree}

    @property
    def target_tracked_keys(self) -> set[str]:
        """Config keys tracked on the target branch (issue #792 H)."""
        return {config_key(cfg.component_id, cfg.id) for cfg in self.on_target}

    @property
    def target_tracked_row_keys(self) -> set[str]:
        """Keys of the rows fetched from the target branch (issue #792 H).

        A row carries its own ``pull_hash``: a row whose create failed under a
        freshly created parent has none and stays ``added``.
        """
        return {
            f"{config_key(cfg.component_id, cfg.id)}/rows/{row.id}"
            for cfg in self.on_target
            for row in cfg.rows
            if row.metadata
            and (
                row.metadata.get("pull_hash")
                or row.metadata.get(UNKNOWN_BASE_KEY) == UNKNOWN_BASE_ADOPTED
            )
        }

    @property
    def never_fetched_keys(self) -> set[str]:
        """Keys of entries that were never materialized on disk."""
        return {config_key(i["component_id"], i["config_id"]) for i in self.never_fetched}


def scope_manifest(
    manifest: Manifest,
    project_root: Path,
    source_branch_path: str,
    remote_keys: set[str],
    ignored_components: frozenset[str] = frozenset(),
    target_branch_id: int | None = None,
) -> TreeScope:
    """Partition ``manifest.configurations`` around *source_branch_path*.

    Args:
        manifest: Loaded manifest.
        project_root: Sync working-tree root.
        source_branch_path: Tree diff/push reads local files from.
        remote_keys: Config keys that exist on the TARGET branch remote. Used
            only to word the orphan hint -- an entry that exists on the target
            needs a ``sync pull`` to re-target the manifest, one that does not
            needs a merge of its branch first.
        ignored_components: Effective ignored-component set for this operation
            (``ALWAYS_IGNORED_COMPONENTS`` plus the manifest's
            ``ignoredComponents``). Entries for these components are dropped
            from EVERY partition -- see below.
        target_branch_id: Branch the API writes go to. Selects
            ``on_target``, and on the promote path (the target's own tree is
            not *source_branch_path*) :func:`_promote_scope`.

    Returns:
        A :class:`TreeScope`.
    """
    in_tree: list[ManifestConfiguration] = []
    never_fetched: list[dict[str, str]] = []
    orphaned: list[dict[str, Any]] = []
    claims: dict[str, list[Claim]] = {}
    on_target: list[ManifestConfiguration] = []
    promote = _promote_scope(manifest, project_root, source_branch_path, target_branch_id)

    for cfg in manifest.configurations:
        # Ignored-component guard (issue #689). The remote side of the diff
        # filters these out, so a manifest entry left behind by an older pull
        # -- or by the user adding a component to ``ignoredComponents`` and
        # running diff/push before the next pull -- has no remote counterpart.
        # ``compute_changeset`` reads that as "added" (keeping the existing
        # config id), so every push CREATES a duplicate of a live config, and
        # keeps doing so. Dropping the entry outright -- not into ``orphaned``:
        # it is not another branch's business either, and ignoring is a
        # deliberate choice rather than drift worth warning about -- keeps it
        # out of the changeset and out of the claim map.
        if cfg.component_id in ignored_components:
            continue

        tree_path = branch_tree_path(manifest, cfg.branch_id)
        key = config_key(cfg.component_id, cfg.id)

        # Never-fetched guard (issue #472): an entry with an EMPTY pull_hash and
        # no file on disk was registered but never materialized. Treating it as
        # tracked would classify it "deleted" and ``push --force`` would delete
        # a remote config nobody ever deleted. It also lays no claim on its id:
        # nothing local refers to it until the next ``sync pull``.
        # A promote entry's files are in its source tree, not its own (D13).
        association = promoted_from(cfg)
        files_tree = association.tree if association is not None else tree_path
        config_file = project_root / files_tree / cfg.path / CONFIG_FILENAME
        if not cfg.metadata.get("pull_hash") and not config_file.exists():
            never_fetched.append(
                {
                    "component_id": cfg.component_id,
                    "config_id": cfg.id,
                    "path": cfg.path,
                }
            )
            continue

        claims.setdefault(key, []).append(Claim(branch_id=cfg.branch_id, tree_path=tree_path))

        # Promote path (issue #792 D): the target-branch entry a previous
        # promote push recorded for a source-tree dir IS that dir's config on
        # the target, and it shadows the source tree's own (production) entry
        # for the same dir. Without this the production entry kept diffing as
        # ``added`` against the target and every promote push created another
        # copy of the same config. A promote entry whose directory is gone
        # stays in the tree: its config is a local delete (D13b).
        if promote.owns(manifest, cfg, target_branch_id):
            in_tree.append(cfg)
            if cfg.metadata.get("pull_hash"):
                on_target.append(cfg)
            continue
        if promote.shadows(cfg, tree_path, source_branch_path):
            continue

        # A promote entry whose files are in another tree (a pull of its branch
        # left it out, e.g. while its component was ignored) is not this tree's:
        # read here it has no file, and a deletion would delete the remote.
        if (
            tree_path == source_branch_path
            and association is not None
            and association.tree != source_branch_path
        ):
            orphaned.append(_promoted_elsewhere_record(cfg, association.tree, source_branch_path))
            continue

        if tree_path == source_branch_path:
            in_tree.append(cfg)
            if same_branch(manifest, cfg.branch_id, target_branch_id) and cfg.metadata.get(
                "pull_hash"
            ):
                on_target.append(cfg)
            continue

        if association is not None and association.tree == source_branch_path:
            orphaned.append(_promoted_record(cfg, tree_path=tree_path))
            continue
        orphaned.append(
            _other_branch_record(
                cfg,
                tree_path=tree_path,
                exists_on_target=key in remote_keys,
                source_tree=(
                    source_branch_path
                    if same_branch(manifest, cfg.branch_id, target_branch_id)
                    else None
                ),
            )
        )

    return TreeScope(
        in_tree=in_tree,
        never_fetched=never_fetched,
        orphaned=orphaned,
        claims=claims,
        on_target=on_target,
    )


@dataclass(frozen=True)
class _PromoteScope:
    """The promote entries of one diff and the source entries they shadow.

    Empty unless the target branch has no tree of its own, so the source tree
    (``main/``) is promoted to it (KFR-07). A promote entry maps one source dir
    to one config on the target (one local dir -> at most one remote config per
    branch), so the next diff/push compares that dir against it rather than
    against the production entry.
    """

    entry_keys: frozenset[tuple[str, str]]
    source_ids: frozenset[tuple[str, str]]
    source_paths: frozenset[tuple[str, str]]

    def owns(
        self, manifest: Manifest, cfg: ManifestConfiguration, target_branch_id: int | None
    ) -> bool:
        """True iff *cfg* is a promote entry of the target branch."""
        return (cfg.component_id, cfg.id) in self.entry_keys and same_branch(
            manifest, cfg.branch_id, target_branch_id
        )

    def shadows(self, cfg: ManifestConfiguration, tree_path: str, source_tree: str) -> bool:
        """True iff *cfg* is a source-tree entry a promote entry stands in for."""
        return tree_path == source_tree and (
            (cfg.component_id, cfg.id) in self.source_ids
            or (cfg.component_id, cfg.path) in self.source_paths
        )


def _promote_scope(
    manifest: Manifest,
    project_root: Path,
    source_branch_path: str,
    target_branch_id: int | None,
) -> _PromoteScope:
    """Collect the promote entries of the target branch (issue #792 D, D13).

    An entry is a promote entry when it records a :class:`PromotedFrom` naming
    the source tree: the association is explicit, so it survives a rename of
    the source dir (pull moves the entry with it) and the deletion of the
    source dir (the entry then diffs as a local delete). An entry written
    before the association was recorded counts when its path exists in the
    source tree, as before.
    """
    empty = _PromoteScope(frozenset(), frozenset(), frozenset())
    if is_production(manifest, target_branch_id):
        return empty
    if branch_tree_path(manifest, target_branch_id) == source_branch_path:
        return empty
    entry_keys: set[tuple[str, str]] = set()
    source_ids: set[tuple[str, str]] = set()
    source_paths: set[tuple[str, str]] = set()
    for cfg in manifest.configurations:
        if not same_branch(manifest, cfg.branch_id, target_branch_id):
            continue
        association = promoted_from(cfg)
        if association is None:
            if not (project_root / source_branch_path / cfg.path / CONFIG_FILENAME).exists():
                continue
            source_paths.add((cfg.component_id, cfg.path))
        elif association.tree != source_branch_path:
            continue
        elif association.config_id:
            source_ids.add((cfg.component_id, association.config_id))
        else:
            source_paths.add((cfg.component_id, cfg.path))
        entry_keys.add((cfg.component_id, cfg.id))
    return _PromoteScope(frozenset(entry_keys), frozenset(source_ids), frozenset(source_paths))


def _promoted_record(cfg: ManifestConfiguration, *, tree_path: str) -> dict[str, Any]:
    """Report record for a promote entry of another branch, seen from its source tree."""
    return {
        "component_id": cfg.component_id,
        "config_id": cfg.id,
        "path": cfg.path,
        "branch_id": cfg.branch_id,
        "branch_path": tree_path,
        "exists_on_target": False,
        "reason": REASON_PROMOTED,
        "hint": (
            f"this directory was promoted to branch {cfg.branch_id} with 'sync push "
            f"--branch {cfg.branch_id}'; that branch's record is not part of this diff "
            f"-- nothing to do"
        ),
    }


def _promoted_elsewhere_record(
    cfg: ManifestConfiguration, files_tree: str, source_tree: str
) -> dict[str, Any]:
    """Report record for a promote entry whose files are in a tree other than the source."""
    return {
        "component_id": cfg.component_id,
        "config_id": cfg.id,
        "path": cfg.path,
        "branch_id": cfg.branch_id,
        "branch_path": files_tree,
        "exists_on_target": True,
        "reason": REASON_PROMOTED_ELSEWHERE,
        "hint": (
            f"promoted to branch {cfg.branch_id} from '{files_tree}/'; its files are not in "
            f"'{source_tree}/', so this diff neither updates nor deletes it -- run 'kbagent "
            f"sync pull --branch {cfg.branch_id}' to track it in '{source_tree}/'"
        ),
    }


def _other_branch_record(
    cfg: ManifestConfiguration,
    *,
    tree_path: str,
    exists_on_target: bool,
    source_tree: str | None = None,
) -> dict[str, Any]:
    """Build the report record for an entry whose tree is not the one read.

    *source_tree* is set when the entry belongs to the branch being synced
    itself: its own tree was not read (it holds no config and is not a pulled
    checkout any more), so the hint must not call it another branch.
    """
    if source_tree is not None:
        hint = (
            f"tracked on branch {cfg.branch_id} ('{tree_path}/'), the branch being synced, "
            f"but '{tree_path}/' holds no config, so this run read '{source_tree}/' "
            f"instead -- restore the directory, or run 'kbagent sync pull --branch "
            f"{cfg.branch_id}' to re-create the tree"
        )
    elif exists_on_target:
        hint = (
            f"tracked on branch {cfg.branch_id} ('{tree_path}/'), which is not the branch "
            f"being synced -- run 'kbagent sync pull' to re-target the manifest"
        )
    else:
        hint = (
            f"tracked on branch {cfg.branch_id} ('{tree_path}/') and does not exist on the "
            f"target branch -- merge its branch first ('kbagent merge-request' or the "
            f"Keboola UI), or run 'kbagent sync pull' to re-target the manifest"
        )
    return {
        "component_id": cfg.component_id,
        "config_id": cfg.id,
        "path": cfg.path,
        "branch_id": cfg.branch_id,
        "branch_path": tree_path,
        "exists_on_target": exists_on_target,
        "reason": REASON_OTHER_BRANCH,
        "hint": hint,
    }


def classify_untracked(
    *,
    component_id: str,
    config_id: str,
    claims: dict[str, list[Claim]],
    source_branch_path: str,
    remote_keys: set[str],
) -> str:
    """Decide what an untracked file in the source tree means.

    Returns one of :data:`VERDICT_CREATE` (push creates a fresh config),
    :data:`VERDICT_ADOPT` (diff against the existing remote config carrying
    this id) or :data:`VERDICT_ORPHAN` (leftover of another branch's tree --
    report it, act on nothing).

    The branch dimension is what issue #649 added. A claim from an entry
    sitting in the SAME tree is the fork-by-copy case of issues #482/#497: the
    user duplicated a tracked config directory, so the copy must still CREATE
    (adopting it would overwrite the original remote config). A claim held only
    by another tree means the manifest was re-targeted by
    ``sync pull --branch`` and this file is an orphan of the tree it left
    behind -- adopting it is then correct, and when its id no longer resolves
    on the target branch there is nothing to adopt and nothing to create.
    """
    if not config_id:
        return VERDICT_CREATE

    key = config_key(component_id, config_id)
    held = claims.get(key, [])
    if any(claim.tree_path == source_branch_path for claim in held):
        return VERDICT_CREATE
    if key in remote_keys:
        return VERDICT_ADOPT
    if held:
        return VERDICT_ORPHAN
    return VERDICT_CREATE


def stale_tree_record(
    *,
    component_id: str,
    config_id: str,
    path: str,
    claims: dict[str, list[Claim]],
) -> dict[str, Any]:
    """Build the report record for an orphaned file in the source tree."""
    claimed_branch_ids = sorted(
        {claim.branch_id for claim in claims.get(config_key(component_id, config_id), [])}
    )
    return {
        "component_id": component_id,
        "config_id": config_id,
        "path": path,
        "claimed_branch_ids": claimed_branch_ids,
        "reason": REASON_STALE_TREE,
        "hint": (
            "left behind by a 'sync pull --branch' that re-targeted the manifest, and its id "
            "no longer exists on the target branch -- run 'kbagent sync pull' to refresh the "
            "tree, or delete the stale directory"
        ),
    }


def find_untracked_configs(
    project_root: Path,
    manifest: Manifest,
    read_config: ConfigReader,
    only_branch_path: str | None = None,
) -> list[dict[str, str]]:
    """Scan for ``_config.yml`` files that are not tracked in the manifest.

    When ``only_branch_path`` is given, scan exactly that branch subtree.
    ``diff`` / ``push`` pass the resolved *source* branch path (the tree push
    reads configs from) so that files belonging to a different branch's tree
    can never be classified as "added" for the target branch: after a branch
    switch, ``sync pull`` re-targets ``manifest.configurations`` to the new
    branch, orphaning the previous branch's tree on disk -- and every orphaned
    file used to surface as "added", making ``sync push`` create a duplicate
    config on the target branch for each of them (issue #482).

    Without ``only_branch_path`` (``sync status``), scan branch directories the
    user is actively working with: branches that already have tracked configs
    and the default branch (production). The default branch is always in scope
    so the documented "scaffold locally then push" workflow works on workspaces
    with empty ``manifest.configurations`` (issue #267, Bug B). Branches
    outside this scope are skipped to avoid phantom "added" configs from
    orphaned dev-branch directories left over from previous work.
    """
    tracked_paths: set[str] = set()
    in_scope_branch_ids: set[int] = set()
    for cfg in manifest.configurations:
        branch_path = branch_tree_path(manifest, cfg.branch_id)
        tracked_paths.add(str(project_root / branch_path / cfg.path))
        in_scope_branch_ids.add(cfg.branch_id)

    # Default branch is always in scope -- pushing a brand-new config
    # against production with empty configurations[] is a legitimate flow.
    if manifest.branches:
        in_scope_branch_ids.add(manifest.branches[0].id)

    added: list[dict[str, str]] = []
    for branch in manifest.branches:
        if only_branch_path is not None:
            if branch.path != only_branch_path:
                continue
        elif branch.id not in in_scope_branch_ids:
            continue
        branch_dir = project_root / branch.path
        if not branch_dir.exists():
            continue
        for config_file in branch_dir.rglob(CONFIG_FILENAME):
            config_dir = config_file.parent
            # Skip row-level configs (they're under rows/ subdirectory)
            if "rows" in config_dir.parts:
                continue
            # Skip branch-level _config.yml
            if config_dir == branch_dir:
                continue
            if str(config_dir) not in tracked_paths:
                local_data = read_config(config_dir)
                keboola_meta = local_data.get("_keboola", {}) if local_data else {}
                added.append(
                    {
                        "component_id": keboola_meta.get("component_id", "unknown"),
                        "config_id": keboola_meta.get("config_id", ""),
                        "path": config_dir.relative_to(project_root / branch.path).as_posix(),
                    }
                )

    return added


def find_untracked_rows(
    project_root: Path,
    manifest: Manifest,
    read_config: ConfigReader,
    only_branch_path: str | None = None,
    entries: list[ManifestConfiguration] | None = None,
    *,
    file_ids_as_source: bool = False,
) -> list[dict[str, Any]]:
    """Scan tracked config dirs for ``rows/*/_config.yml`` not in the manifest.

    Paralleling :func:`find_untracked_configs` at the row level. A user can
    drop a hand-crafted row directory under a tracked config's ``rows/``
    folder; this surfaces it so ``diff`` can flag it as ``"added"`` and
    ``push`` can POST it via ``create_config_row``.

    ``only_branch_path`` restricts the walk to one branch tree, mirroring
    :func:`find_untracked_configs`: a row under a parent tracked on ANOTHER
    branch would otherwise be POSTed onto the target branch's copy of that
    config (issue #649).

    Each entry contains ``component_id``, ``parent_config_id``, ``row_name``
    (from the loaded YAML), ``path`` (relative to the parent config dir, e.g.
    ``rows/new-row``), ``data`` (the loaded dict) and ``source_row_id`` (the
    row's id in the source config a promote entry shadows, else ``""``).

    ``entries`` walks exactly these parents, all read from ``only_branch_path``:
    ``diff`` passes its scoped ``in_tree`` entries, so on the promote path the
    rows of a target-branch entry are looked up in the source tree where its
    files are, and the rows of the production entry it shadows are not walked
    a second time.

    *file_ids_as_source* (any push but the promote path): a row file that
    carries a row id, under a config that is not a promote entry, keeps that
    id as its ``source_row_id`` -- the id a flow created in the same push
    selects it by, recorded as ``created_from_id`` when the row is created
    (D9, R6). On the promote path only a source entry's own rows count (risk 81).
    """
    added: list[dict[str, Any]] = []
    for cfg in manifest.configurations if entries is None else entries:
        if entries is not None and only_branch_path is not None:
            branch_path = only_branch_path
        else:
            branch_path = branch_tree_path(manifest, cfg.branch_id)
        if only_branch_path is not None and branch_path != only_branch_path:
            continue
        parent_dir = project_root / branch_path / cfg.path
        rows_dir = parent_dir / "rows"
        if not rows_dir.is_dir():
            continue
        tracked_row_paths = {row.path for row in cfg.rows}
        source_rows = _source_row_ids(manifest, cfg)
        for row_subdir in rows_dir.iterdir():
            if not row_subdir.is_dir():
                continue
            row_rel_path = f"rows/{row_subdir.name}"
            if row_rel_path in tracked_row_paths:
                continue
            if not (row_subdir / CONFIG_FILENAME).exists():
                continue
            local_data = read_config(row_subdir)
            if local_data is None:
                continue
            added.append(
                {
                    "component_id": cfg.component_id,
                    "parent_config_id": cfg.id,
                    "row_name": local_data.get("name", ""),
                    "path": row_rel_path,
                    "data": local_data,
                    "source_row_id": source_rows.get(row_rel_path, "")
                    or (
                        str((local_data.get("_keboola") or {}).get("row_id") or "")
                        if file_ids_as_source and promoted_from(cfg) is None
                        else ""
                    ),
                }
            )
    return added


def _source_row_ids(manifest: Manifest, cfg: ManifestConfiguration) -> dict[str, str]:
    """Row path -> row id in the source-tree config a promote entry shadows.

    A row added to that source config after the promote is untracked under the
    target entry but tracked, with its source id, under the source entry. The
    source id is kept apart from the target diff identity (risk 71): it is
    what the row map and ``promoted_from_id`` record once the row is created.
    """
    promoted = promoted_from(cfg)
    if promoted is None or not promoted.config_id:
        return {}
    source = promote_source(manifest, cfg, promoted)
    return {row.path: row.id for row in source.rows} if source is not None else {}


def promote_source(
    manifest: Manifest, cfg: ManifestConfiguration, promoted: PromotedFrom
) -> ManifestConfiguration | None:
    """The source-tree entry a promote entry stands for, or ``None``.

    Found on the branch registered for the source tree, by branch identity,
    never via :func:`branch_tree_path` -- that maps an unregistered id to the
    default tree (D28, risk 73). By the association's config id, or, while
    that is still empty (an authored config), by the entry's path.
    """
    trees = [b.id for b in manifest.branches if b.path == promoted.tree]
    if not trees:
        return None
    for source in manifest.configurations:
        if (
            source is not cfg
            and source.component_id == cfg.component_id
            and same_branch(manifest, source.branch_id, trees[0])
            and (source.id == promoted.config_id if promoted.config_id else source.path == cfg.path)
        ):
            return source
    return None
