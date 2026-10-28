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

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..constants import CONFIG_FILENAME
from ..errors import KeboolaApiError
from ..sync.branch_scope import UNKNOWN_BASE_KEY, promoted_from, same_branch
from ..sync.manifest import Manifest, ManifestConfigRow, ManifestConfiguration
from ._sync_models import (
    CREATED_FROM_KEY,
    LINK_PENDING_KEY,
    RUN_TARGET_COMPONENTS,
    SHARED_CODE_COMPONENT_ID,
    VARIABLES_COMPONENT_ID,
)

logger = logging.getLogger(__name__)

_LINK_COMPONENTS = frozenset({VARIABLES_COMPONENT_ID, SHARED_CODE_COMPONENT_ID})


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
        planned_rows: Rows planned under existing configs (see the field).
    """

    configs: dict[tuple[str, str], str] = field(default_factory=dict)
    rows: dict[tuple[str, str], str] = field(default_factory=dict)
    target_rows: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    uncreated: set[tuple[str, str]] = field(default_factory=set)
    # ``(parent id, source row id)`` of the rows this push planned to create
    # under an EXISTING config (S3): a reference to one that is still missing
    # (its create failed) keeps the referencing config pending.
    planned_rows: set[tuple[str, str]] = field(default_factory=set)
    # Target ids of the configs this push created (known to exist).
    created_ids: set[str] = field(default_factory=set)


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
    planned_rows: set[tuple[str, str]] | None = None,
) -> LinkMaps:
    """The maps one Phase-D pass resolves references with."""
    maps = LinkMaps(planned_rows=set(planned_rows or ()))
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
    maps.created_ids = set(created_id_map.values())
    return maps


def drop_gone_targets(
    client: Any,
    manifest: Manifest,
    branch_id: int | None,
    maps: LinkMaps,
    *,
    referenced: set[str],
    fresh_rows: set[str],
) -> None:
    """Keep only mappings to targets that still exist on the target branch (Devin 4400587856).

    A row (or config) created by an earlier push may have been deleted on the
    remote since; the manifest still names it until a pull reconciles it.
    Every config a durable row mapping points into is read from the target
    once: a row it no longer lists, or a config it answers 404 for, is no
    resolved target -- references to it stay pending, never rewritten to a
    dead id (#929's deleted-target rule). Only the configs the pass's
    references name (*referenced*, target ids) are read, and not one created
    this push; *fresh_rows* (created this push) are known to exist. A target
    that cannot be read proves nothing and is kept.
    """
    components = {entry.id: entry.component_id for entry in target_entries(manifest, branch_id)}
    for parent in sorted(referenced - maps.created_ids):
        component_id = components.get(parent)
        if component_id is None:
            continue
        try:
            detail = client.get_config_detail(
                component_id=component_id, config_id=parent, branch_id=branch_id
            )
        except KeboolaApiError as exc:
            if exc.status_code == 404:
                _forget_config(maps, component_id, parent)
            continue
        except Exception:  # an unreadable target proves nothing: keep its mappings
            logger.warning("Could not read %s/%s to check its rows", component_id, parent)
            continue
        rows = detail.get("rows") if isinstance(detail, dict) else None
        if not isinstance(rows, list):
            continue
        present = {str(row.get("id", "")) for row in rows if isinstance(row, dict)}
        maps.rows = {
            key: row
            for key, row in maps.rows.items()
            if key[0] != parent or row in present or row in fresh_rows
        }
        key = (component_id, parent)
        if key in maps.target_rows:
            maps.target_rows[key] = {r for r in maps.target_rows[key] if r in present}


def _forget_config(maps: LinkMaps, component_id: str, config_id: str) -> None:
    """A target config deleted on the remote: references to it stay pending."""
    for key, target in list(maps.configs.items()):
        if key[0] == component_id and target == config_id:
            del maps.configs[key]
            maps.uncreated.add(key)
    maps.uncreated.add((component_id, config_id))
    maps.rows = {key: row for key, row in maps.rows.items() if key[0] != config_id}
    maps.target_rows.pop((component_id, config_id), None)


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


def planned_row_refs(row_changes: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """``(parent id, source row id)`` of each row create a push plans under an existing config."""
    return {
        (str(c["parent_config_id"]), str(source))
        for c in row_changes
        if c["change_type"] == "added"
        and c.get("parent_config_id")
        and not c.get("parent_path")
        and (source := c.get("source_row_id") or c.get("config_id"))
    }


def carries_transformation_link(component_id: str, configuration: dict[str, Any]) -> bool:
    """True when a created config names a variables or shared-code config (it may be pending)."""
    if component_id in RUN_TARGET_COMPONENTS or component_id in _LINK_COMPONENTS:
        return False
    return any(
        isinstance(configuration.get(key), (str, int)) and configuration.get(key) != ""
        for key in ("variables_id", "shared_code_id")
    )
