"""Re-enabling a config or row by deleting its ``is_disabled: true`` line.

Pull writes ``is_disabled: true`` only for a disabled config or row and omits
the key otherwise. Push sends the state only when the key is in the file: an
absent key leaves the remote state alone (issue #467), because a file written
by an older kbagent never carried the key and the remote may have been
disabled in the UI since. So the natural way to re-enable a pulled disabled
config -- deleting the line pull wrote -- pushed nothing, and diff then stayed
clean while the config stayed disabled.

The manifest therefore records which accepted baseline file held the line:
``pulled_disabled`` on a config or row entry is the ``pull_hash`` of the file
that explicitly contained ``is_disabled: true``. A missing key in the local
file then means "enabled" when the marker is valid, and nothing (the legacy
behavior) when it is not. The marker is bound to that hash, never a bare
``true``: a site that accepts another baseline file without re-deriving the
marker invalidates it, and the fallback is the safe legacy behavior. It is
derived from the FILE only, never from the remote: a legacy file without the
line next to a remote disabled in the UI must not be read as "the user deleted
the line" (that would re-enable the config on an unrelated push).
"""

from __future__ import annotations

from typing import Any

from ..sync.branch_scope import branch_tree_path, find_branch_entry, promoted_from, same_branch
from ..sync.manifest import Manifest, ManifestConfiguration

DISABLED_BASELINE_KEY = "pulled_disabled"


def file_disables(data: dict[str, Any] | None) -> bool:
    """True iff a config/row file's data explicitly disables it."""
    return isinstance(data, dict) and bool(data.get("is_disabled"))


def record_disabled(metadata: dict[str, Any], *, disabled: bool) -> None:
    """Derive the marker for the baseline file whose hash ``metadata`` just got.

    Call it right after ``pull_hash`` was set. *disabled* says whether that
    file explicitly disables the config or row (:func:`file_disables`).
    """
    file_hash = metadata.get("pull_hash")
    if disabled and file_hash:
        metadata[DISABLED_BASELINE_KEY] = file_hash
    else:
        metadata.pop(DISABLED_BASELINE_KEY, None)


def carry_disabled(source: dict[str, Any] | None, metadata: dict[str, Any]) -> None:
    """Copy the marker of a preserved baseline (same ``pull_hash``) onto *metadata*."""
    marker = (source or {}).get(DISABLED_BASELINE_KEY)
    if marker:
        metadata[DISABLED_BASELINE_KEY] = marker


def baseline_disabled(metadata: dict[str, Any] | None) -> bool:
    """True iff the entry's accepted baseline file contained ``is_disabled: true``."""
    if not metadata:
        return False
    marker = metadata.get(DISABLED_BASELINE_KEY)
    return bool(marker) and marker == metadata.get("pull_hash")


def pushed_disabled(local_data: dict[str, Any], baseline: dict[str, Any] | None) -> bool | None:
    """The ``is_disabled`` an update sends: the file's key, else the baseline's word.

    ``None`` leaves the remote state alone (issue #467). ``False`` when the key
    is gone from a file whose baseline explicitly had it: the user deleted the
    line, which re-enables.
    """
    if "is_disabled" in local_data:
        return bool(local_data["is_disabled"])
    return False if baseline_disabled(baseline) else None


def baseline_entry(
    manifest: Manifest,
    *,
    component_id: str,
    config_id: str,
    branch_id: int | None,
    source_tree: str,
    path: str,
) -> ManifestConfiguration | None:
    """The entry whose baseline describes the file a push to *branch_id* reads.

    Off the promote path: the branch's own entry. On the promote path (the
    push reads *source_tree*, another branch's tree): the branch's promote
    entry for that directory when it has one, else the source tree's own entry
    for it, found by branch identity and path -- the entry the push's diff
    compared the file against. Never "the first entry with this id": a dev
    branch made from production holds the same ids.
    """
    target = find_branch_entry(manifest, component_id, config_id, branch_id)
    if source_tree == branch_tree_path(manifest, branch_id):
        return target
    association = promoted_from(target) if target is not None else None
    if target is not None and association is not None and association.tree == source_tree:
        return target if target.path == path else None
    trees = [b.id for b in manifest.branches if b.path == source_tree]
    for cfg in manifest.configurations:
        if (
            trees
            and cfg.component_id == component_id
            and cfg.path == path
            and promoted_from(cfg) is None
            and same_branch(manifest, cfg.branch_id, trees[0])
        ):
            return cfg
    return None


def baseline_row(entry: ManifestConfiguration | None, row_path: str) -> dict[str, Any] | None:
    """Metadata of *entry*'s row in *row_path* (by path: a promote copy has other ids)."""
    if entry is None:
        return None
    for row in entry.rows:
        if row.path == row_path:
            return row.metadata
    return None
