"""Which on-disk branch tree the deep-lineage scan reads (issue #846).

``sync pull`` writes each configuration under ``<project>/<branch tree>/<path>``,
where the branch tree is the ``path`` of the manifest branch the entry belongs
to -- usually ``main``, but not always (a custom default branch directory, or a
dev branch tree after ``sync pull --branch``). The lineage scan used to
hardcode ``main``, so any other tree scanned as empty configurations.

The lineage scan reads the manifest as raw JSON, so this module is a tolerant
raw-dict twin of :func:`keboola_agent_cli.sync.branch_scope.branch_tree_path`
with the same semantics: ``None`` / ``0`` / an unregistered id resolve to the
default tree (``branches[0]``), a registered id to its own tree, and ``main``
is the last resort when the manifest lists no branches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_DEFAULT_TREE_FALLBACK = "main"


def default_tree(branches: list[dict[str, Any]]) -> str:
    """Return the production/default branch tree name (``branches[0]``)."""
    if branches and isinstance(branches[0], dict) and branches[0].get("path"):
        return str(branches[0]["path"])
    return _DEFAULT_TREE_FALLBACK


def branch_tree_for(branches: list[dict[str, Any]], branch_id: Any) -> str:
    """Map a manifest ``branchId`` to its on-disk tree name."""
    if branch_id is not None:
        for branch in branches:
            if isinstance(branch, dict) and branch.get("id") == branch_id and branch.get("path"):
                return str(branch["path"])
    return default_tree(branches)


@dataclass
class ScanEntry:
    """A manifest configuration entry and the branch tree its files live in."""

    entry: dict[str, Any]
    tree: str


@dataclass
class ScanSelection:
    """Manifest configurations the scan reads, and what it left out.

    Config FQNs carry no branch dimension, so scanning two branch trees at once
    would let one tree's snapshot of a config silently overwrite the other's.
    When entries span several trees only the default tree is scanned.
    """

    entries: list[ScanEntry] = field(default_factory=list)
    default_tree: str = _DEFAULT_TREE_FALLBACK
    skipped: int = 0
    skipped_trees: list[str] = field(default_factory=list)


def select_scan_entries(manifest: dict[str, Any]) -> ScanSelection:
    """Pick the configuration entries to scan from a raw manifest dict."""
    branches = manifest.get("branches") or []
    resolved = [
        ScanEntry(entry=entry, tree=branch_tree_for(branches, entry.get("branchId")))
        for entry in manifest.get("configurations", []) or []
    ]
    selection = ScanSelection(default_tree=default_tree(branches))
    if len({item.tree for item in resolved}) <= 1:
        selection.entries = resolved
        return selection

    selection.entries = [item for item in resolved if item.tree == selection.default_tree]
    others = [item.tree for item in resolved if item.tree != selection.default_tree]
    selection.skipped = len(others)
    selection.skipped_trees = sorted(set(others))
    return selection


def skipped_trees_warning(project_alias: str, selection: ScanSelection) -> str:
    """Human-readable warning for configs left out of a multi-tree manifest."""
    noun = "configuration" if selection.skipped == 1 else "configurations"
    return (
        f"Project '{project_alias}': skipped {selection.skipped} {noun} tracked on other "
        f"branch trees ({', '.join(selection.skipped_trees)}); lineage reads only the "
        f"default branch tree '{selection.default_tree}'. Config identities are not "
        "branch-qualified, so mixing trees would merge different snapshots. Re-pull the "
        "branch you want into its own directory to see its lineage."
    )
