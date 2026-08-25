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
    """

    in_tree: list[ManifestConfiguration]
    never_fetched: list[dict[str, str]]
    orphaned: list[dict[str, Any]]
    claims: dict[str, list[Claim]]

    @property
    def tracked_keys(self) -> set[str]:
        """Keys diff/push treat as tracked -- source tree only."""
        return {config_key(cfg.component_id, cfg.id) for cfg in self.in_tree}

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
) -> TreeScope:
    """Partition ``manifest.configurations`` around *source_branch_path*.

    Args:
        manifest: Loaded manifest.
        project_root: Sync working-tree root.
        source_branch_path: Tree diff/push reads local files from.
        remote_keys: Config keys that exist on the TARGET branch remote. Used
            only to word the orphan hint -- an entry that exists on the target
            needs a ``sync pull`` to re-target the manifest, one that does not
            needs ``branch merge`` to be promoted first.
        ignored_components: Effective ignored-component set for this operation
            (``ALWAYS_IGNORED_COMPONENTS`` plus the manifest's
            ``ignoredComponents``). Entries for these components are dropped
            from EVERY partition -- see below.

    Returns:
        A :class:`TreeScope`.
    """
    in_tree: list[ManifestConfiguration] = []
    never_fetched: list[dict[str, str]] = []
    orphaned: list[dict[str, Any]] = []
    claims: dict[str, list[Claim]] = {}

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
        config_file = project_root / tree_path / cfg.path / CONFIG_FILENAME
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

        if tree_path == source_branch_path:
            in_tree.append(cfg)
            continue

        orphaned.append(
            _other_branch_record(cfg, tree_path=tree_path, exists_on_target=key in remote_keys)
        )

    return TreeScope(
        in_tree=in_tree,
        never_fetched=never_fetched,
        orphaned=orphaned,
        claims=claims,
    )


def _other_branch_record(
    cfg: ManifestConfiguration,
    *,
    tree_path: str,
    exists_on_target: bool,
) -> dict[str, Any]:
    """Build the report record for an entry tracked on another branch."""
    if exists_on_target:
        hint = (
            f"tracked on branch {cfg.branch_id} ('{tree_path}/'), which is not the branch "
            f"being synced -- run 'kbagent sync pull' to re-target the manifest"
        )
    else:
        hint = (
            f"tracked on branch {cfg.branch_id} ('{tree_path}/') and does not exist on the "
            f"target branch -- use 'kbagent branch merge' to promote it, or run "
            f"'kbagent sync pull' to re-target the manifest"
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
    ``rows/new-row``), and ``data`` (the loaded dict).
    """
    added: list[dict[str, Any]] = []
    for cfg in manifest.configurations:
        branch_path = branch_tree_path(manifest, cfg.branch_id)
        if only_branch_path is not None and branch_path != only_branch_path:
            continue
        parent_dir = project_root / branch_path / cfg.path
        rows_dir = parent_dir / "rows"
        if not rows_dir.is_dir():
            continue
        tracked_row_paths = {row.path for row in cfg.rows}
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
                }
            )
    return added
