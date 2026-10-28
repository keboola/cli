"""Durable id maps for the Phase-D link backfill outside the promote path (D9).

Phase D rewrites the configs a flow, orchestration or schedule runs when they
were created in the same push (a ``sync clone``, a tree of placeholder
entries): placeholder id -> the id the create returned. It used the current
push's in-memory maps only. A flow created in a push whose referenced row
failed (or a push that died between the creates and the backfill) kept the
placeholder ids for good: the next push created the row, but no longer knew
the flow pointed at it.

Now every config and row created from a placeholder records it
(``created_from_id``), and a created run-target config records
``link_pending`` in its own create checkpoint. Each push rebuilds the maps
from the manifest, so a flow still pending is finished by the next push once
the missing config or row exists -- without another edit. The flag is cleared
when every reference resolves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..constants import CONFIG_FILENAME
from ..sync.branch_scope import UNKNOWN_BASE_KEY, promoted_from, same_branch
from ..sync.manifest import Manifest, ManifestConfigRow, ManifestConfiguration
from ._sync_models import CREATED_FROM_KEY, LINK_PENDING_KEY


@dataclass
class LinkMaps:
    """Placeholder -> target ids known to one push, from the manifest and its creates.

    Attributes:
        configs: ``(component_id, placeholder id)`` -> target config id.
        rows: ``(target parent id, placeholder row id)`` -> target row id.
        target_rows: ``(component_id, target config id)`` -> ids of its rows
            that exist on the target (a placeholder row whose create failed is
            not one of them).
        uncreated: ``(component_id, placeholder id)`` of configs push still
            has to create: a reference to one stays pending.
    """

    configs: dict[tuple[str, str], str] = field(default_factory=dict)
    rows: dict[tuple[str, str], str] = field(default_factory=dict)
    target_rows: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    uncreated: set[tuple[str, str]] = field(default_factory=set)


def target_entries(manifest: Manifest, branch_id: int | None) -> list[ManifestConfiguration]:
    """The entries of *branch_id* outside the promote path."""
    return [
        entry
        for entry in manifest.configurations
        if same_branch(manifest, entry.branch_id, branch_id) and promoted_from(entry) is None
    ]


def _on_target(row: ManifestConfigRow) -> bool:
    """True unless the row is a placeholder push has not created yet."""
    meta = row.metadata or {}
    return bool(meta.get("pull_hash") or meta.get(CREATED_FROM_KEY) or meta.get(UNKNOWN_BASE_KEY))


def build_link_maps(
    manifest: Manifest,
    branch_id: int | None,
    *,
    created_id_map: dict[tuple[str, str], str],
    created_row_id_map: dict[tuple[str, str], str],
    source_dir: Path | None,
) -> LinkMaps:
    """The maps one Phase-D pass resolves references with."""
    maps = LinkMaps()
    for entry in target_entries(manifest, branch_id):
        source = str(entry.metadata.get(CREATED_FROM_KEY) or "")
        if source:
            maps.configs[(entry.component_id, source)] = entry.id
        maps.target_rows[(entry.component_id, entry.id)] = {
            row.id for row in entry.rows if _on_target(row)
        }
        for row in entry.rows:
            row_source = str((row.metadata or {}).get(CREATED_FROM_KEY) or "")
            if row_source:
                maps.rows[(entry.id, row_source)] = row.id
        if (
            not source
            and not entry.metadata.get("pull_hash")
            and source_dir is not None
            and (source_dir / entry.path / CONFIG_FILENAME).exists()
        ):
            maps.uncreated.add((entry.component_id, entry.id))
    maps.configs.update(created_id_map)
    maps.rows.update(created_row_id_map)
    return maps


def link_pending_entries(manifest: Manifest, branch_id: int | None) -> list[ManifestConfiguration]:
    """The target's run-target configs whose references are not all resolved yet."""
    return [e for e in target_entries(manifest, branch_id) if e.metadata.get(LINK_PENDING_KEY)]


def set_link_pending(entry: ManifestConfiguration | None, pending: bool) -> bool:
    """Set or clear ``link_pending`` on *entry*; True when that changed it."""
    if entry is None or bool(entry.metadata.get(LINK_PENDING_KEY)) == pending:
        return False
    if pending:
        entry.metadata[LINK_PENDING_KEY] = True
    else:
        entry.metadata.pop(LINK_PENDING_KEY, None)
    return True
