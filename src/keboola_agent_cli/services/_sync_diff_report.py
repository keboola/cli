"""The ``sync diff`` result: remote-only configs and rows, deletes, summary.

Extracted from ``SyncService.diff`` (sync group 3). Builds what diff returns
once both changesets are computed:

- ``remote_only[]`` -- configs on the remote that no local file or manifest
  entry accounts for (new in the UI, not pulled yet);
- ``remote_only_rows[]`` (D5) -- the same for rows: a row added in the UI under
  a config the tree tracks. It used to be invisible: the row has no manifest
  entry, so the row diff never saw it, and ``sync pull`` wrote it without a
  word (finding remote-row-add-invisible). It is not a ``changes[]`` item --
  ``change_type`` gains no value -- and push never creates or deletes it;
- the three-way settlement of local deletes (:mod:`._sync_deletes`);
- ``summary`` -- whose ``unchanged`` counts local configs with no
  config-level change. It used to subtract row changes from a config count
  and could go negative.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from ..sync.diff_engine import ConfigChange
from ._sync_deletes import DeleteInputs, DeleteTarget, settle_deletions


@dataclass
class DiffSides:
    """Both sides of one diff, as ``SyncService.diff`` collected them.

    Attributes:
        local_configs: Local config entries (tracked and adopted).
        local_rows: Local row entries after their identities were settled.
        remote_configs: Remote configs by ``"{component}/{config}"``.
        remote_rows: Remote rows by ``"{component}/{config}/rows/{row}"``.
        tracked_keys: Config keys the manifest tracks in the source tree.
        tracked_row_keys: Row keys the row diff tracks.
        never_fetched_keys: Config keys registered but never materialized.
    """

    local_configs: list[dict[str, Any]]
    local_rows: list[dict[str, Any]]
    remote_configs: dict[str, dict[str, Any]]
    remote_rows: dict[str, dict[str, Any]]
    tracked_keys: set[str]
    tracked_row_keys: set[str]
    never_fetched_keys: set[str]


@dataclass
class DiffExtras:
    """The report-only parts of a diff result."""

    never_fetched: list[dict[str, str]] = field(default_factory=list)
    orphaned: list[dict[str, Any]] = field(default_factory=list)
    companion_warnings: list[dict[str, str]] = field(default_factory=list)


def _local_keys(sides: DiffSides) -> set[str]:
    """Config keys local files or manifest entries account for."""
    return {
        f"{e['component_id']}/{e['config_id']}" for e in sides.local_configs if e.get("config_id")
    } | sides.tracked_keys


def remote_only_configs(sides: DiffSides) -> list[dict[str, str]]:
    """Remote configs not accounted for locally (new on the server, not yet pulled)."""
    local_keys = _local_keys(sides)
    remote_only: list[dict[str, str]] = []
    for remote_key, remote_data in sides.remote_configs.items():
        if remote_key in local_keys or remote_key in sides.never_fetched_keys:
            continue
        component_id, _, config_id = remote_key.partition("/")
        remote_only.append(
            {
                "component_id": component_id,
                "config_id": config_id,
                "config_name": remote_data.get("name", ""),
            }
        )
    return remote_only


def remote_only_rows(sides: DiffSides, changeset: list[ConfigChange]) -> list[dict[str, str]]:
    """Remote rows of a local config that nothing local accounts for (D5).

    Accounted for: a row the manifest (or an adopted dir) tracks, a local row
    dir that is that row, and any row a change already names. A row of a
    remote-only config is not listed: the config is.
    """
    parents = _local_keys(sides)
    accounted = set(sides.tracked_row_keys)
    accounted |= {
        f"{r['component_id']}/{r['parent_config_id']}/rows/{r['row_id']}"
        for r in sides.local_rows
        if r.get("row_id") and r.get("parent_config_id")
    }
    accounted |= {
        f"{c.component_id}/{c.parent_config_id}/rows/{c.config_id}"
        for c in changeset
        if c.is_row and c.config_id
    }
    rows: list[dict[str, str]] = []
    for key, remote_row in sides.remote_rows.items():
        parent, _, row_id = key.rpartition("/rows/")
        if key in accounted or parent not in parents:
            continue
        component_id, _, config_id = parent.partition("/")
        rows.append(
            {
                "component_id": component_id,
                "parent_config_id": config_id,
                "row_id": row_id,
                "row_name": remote_row.get("name", ""),
            }
        )
    return rows


def _unchanged(changeset: list[ConfigChange], local_configs: list[dict[str, Any]]) -> int:
    """Local configs with no config-level change (rows are not counted)."""
    changed = {
        (c.component_id, c.config_id or c.path)
        for c in changeset
        if not c.is_row and c.change_type != "deleted"
    }
    return sum(
        1
        for e in local_configs
        if (e["component_id"], e.get("config_id") or e.get("path", "")) not in changed
    )


def diff_report(
    changeset: list[ConfigChange],
    sides: DiffSides,
    deletes: DeleteInputs,
    target: DeleteTarget,
    extras: DiffExtras,
) -> dict[str, Any]:
    """Settle the local deletes and build the ``sync diff`` result dict."""
    remote_only = remote_only_configs(sides)
    deletes = replace(deletes, remote_only_rows=remote_only_rows(sides, changeset))
    settle_deletions(changeset, deletes, target)

    def count(change_type: str) -> int:
        return sum(1 for c in changeset if c.change_type == change_type)

    return {
        "changes": [c.to_dict() for c in changeset],
        **({"companion_warnings": extras.companion_warnings} if extras.companion_warnings else {}),
        "remote_only": remote_only,
        "remote_only_rows": deletes.remote_only_rows,
        "never_fetched": extras.never_fetched,
        "orphaned": extras.orphaned,
        "summary": {
            "added": count("added"),
            "modified": count("modified"),
            "remote_modified": count("remote_modified"),
            "conflict": count("conflict"),
            "deleted": count("deleted"),
            "remote_deleted": count("remote_deleted"),
            "unchanged": _unchanged(changeset, sides.local_configs),
            "remote_only": len(remote_only),
            "remote_only_rows": len(deletes.remote_only_rows),
            "never_fetched": len(extras.never_fetched),
            "orphaned": len(extras.orphaned),
        },
    }
