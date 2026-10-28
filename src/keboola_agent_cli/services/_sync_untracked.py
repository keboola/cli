"""Rows under an untracked config directory, and the diff checks of a config dir.

``find_untracked_rows`` walks rows only under configs the manifest tracks. A
config directory the manifest does not track had its ``rows/`` ignored
altogether, which hid two things from ``sync diff`` and ``sync push``:

- **A new config's rows (D7).** A config authored with ``rows/`` was created
  WITHOUT its rows (push reported success, a second push created them). Its
  rows are now listed as ``added`` with ``parent_path`` -- the parent's
  directory, its only identity until its create returns an id -- so the whole
  plan is known before the first write and push creates them in the same run.
- **An adopted config's rows (D8).** A directory whose ``_config.yml`` carries
  the id of an existing remote config (no manifest entry: a lost manifest, a
  ``pull --branch`` that left the tree behind) is diffed against that config,
  but a row edited there was invisible. Its rows are now diffed by row id with
  an UNKNOWN base: a row that differs from the remote is a ``conflict`` (no
  baseline says which side changed), never an update to send, and a remote row
  with no file is a conflict too, never a delete.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ..constants import CONFIG_FILENAME
from ..errors import ConfigError
from ..sync.branch_scope import (
    UNKNOWN_BASE_ADOPTED,
    UNKNOWN_BASE_HASH,
    UNKNOWN_BASE_KEY,
    find_untracked_rows,
)
from ..sync.diff_engine import ConfigChange
from ..sync.manifest import Manifest, ManifestConfigRow, ManifestConfiguration

if TYPE_CHECKING:
    from .sync_service import SyncService


@dataclass
class UntrackedRows:
    """Row-diff inputs contributed by the untracked config directories."""

    local_rows: list[dict[str, Any]] = field(default_factory=list)
    tracked_row_keys: set[str] = field(default_factory=set)
    base_hashes: dict[str, str] = field(default_factory=dict)
    # Row keys of an adopted config whose local file carries a row id the
    # remote config no longer has: ``remote_deleted``, never re-created (R5).
    gone_row_keys: set[str] = field(default_factory=set)


def _row_dirs(config_dir: Path) -> list[Path]:
    rows_dir = config_dir / "rows"
    if not rows_dir.is_dir():
        return []
    return sorted(d for d in rows_dir.iterdir() if (d / CONFIG_FILENAME).is_file())


def collect_untracked_rows(
    service: SyncService,
    found: UntrackedRows,
    config_dir: Path,
    *,
    component_id: str,
    config_id: str,
    config_path: str,
    remote_rows: dict[str, dict[str, Any]],
) -> None:
    """Add the rows of one untracked config dir to *found*.

    *config_id* is ``""`` for a config push creates: every row is new, also
    one whose file carries an id copied from another config -- that id is a
    placeholder, not the row's source identity (S2): the row is created as
    authored and linked by its path. For an adopted config (*config_id* set) a
    row carrying an id is that row: diffed against the remote one, or
    ``remote_deleted`` when the remote no longer has it -- never POSTed again
    (R5, #792 H). Only a row without an id is new there.
    """
    prefix = f"{component_id}/{config_id}/rows/"
    for row_dir in _row_dirs(config_dir):
        data = service._read_config_file(row_dir)
        if not isinstance(data, dict):
            continue
        file_row_id = str((data.get("_keboola") or {}).get("row_id") or "")
        entry: dict[str, Any] = {
            "component_id": component_id,
            "parent_config_id": config_id,
            "row_id": "",
            "row_name": data.get("name", ""),
            "path": f"rows/{row_dir.name}",
            "data": data,
            "parent_path": config_path,
            "untracked": True,
            # A row id in a NEW config's row file was copied from another
            # config: a placeholder, never a source identity (S2). The row is
            # created as authored and linked by its path later (risk 83).
            "source_row_id": "",
        }
        if config_id and file_row_id:
            entry["row_id"] = file_row_id
            entry.pop("parent_path")
            if f"{prefix}{file_row_id}" not in remote_rows:
                found.gone_row_keys.add(f"{prefix}{file_row_id}")
        found.local_rows.append(entry)
    if not config_id:
        return
    # Every remote row of an adopted config is compared with an unknown base:
    # equal is unchanged, different -- or missing locally -- is a conflict.
    for key in remote_rows:
        if key.startswith(prefix):
            found.tracked_row_keys.add(key)
            found.base_hashes[key] = UNKNOWN_BASE_HASH


def collect_tracked_parent_rows(
    service: SyncService,
    found: UntrackedRows,
    project_root: Path,
    manifest: Manifest,
    source_branch_path: str,
    entries: list[ManifestConfiguration],
    *,
    file_ids_as_source: bool,
    ignored_components: frozenset[str],
) -> None:
    """Add the untracked row dirs under tracked configs (new rows) to *found*.

    They have no row id on the target yet, so the row diff flags them
    ``added`` and push creates them. A still-tracked ignored parent
    contributes none: push would re-add what pull refuses to fetch.
    """
    for untracked in find_untracked_rows(
        project_root,
        manifest,
        service._read_config_file,
        source_branch_path,
        entries,
        file_ids_as_source=file_ids_as_source,
    ):
        if untracked["component_id"] in ignored_components:
            continue
        found.local_rows.append(
            {
                "component_id": untracked["component_id"],
                "parent_config_id": untracked["parent_config_id"],
                "row_id": "",  # target diff identity: not on the target yet
                "source_row_id": untracked["source_row_id"],  # risk 71
                "row_name": untracked["row_name"],
                "path": untracked["path"],
                "data": untracked["data"],
                "untracked": True,
            }
        )


def _file_row_id(entry: dict[str, Any]) -> str:
    data = entry.get("data")
    meta = data.get("_keboola") if isinstance(data, dict) else None
    return str(meta.get("row_id") or "") if isinstance(meta, dict) else ""


def split_ambiguous_rows(local_rows: list[dict[str, Any]]) -> list[ConfigChange]:
    """Take the untracked row dirs whose row id another dir of the config carries out of the diff.

    Two row dirs of one config carrying the same row id (a copied row dir)
    say nothing about which one is the row: pull writes into neither (its
    ambiguous-id rule), and push must create neither -- a row that the
    remote deleted was POSTed again from the copy. Each untracked one becomes
    a ``conflict`` naming every dir that carries the id; a tracked dir keeps
    its own diff. A new config's row ids are placeholders and never count.
    Mutates *local_rows*; returns the conflicts.
    """
    paths: dict[tuple[str, str, str], list[str]] = {}
    for entry in local_rows:
        row_id = _file_row_id(entry)
        if entry.get("parent_config_id") and row_id:
            key = (entry["component_id"], str(entry["parent_config_id"]), row_id)
            paths.setdefault(key, []).append(str(entry.get("path", "")))
    conflicts: list[ConfigChange] = []
    kept: list[dict[str, Any]] = []
    for entry in local_rows:
        key = (entry["component_id"], str(entry.get("parent_config_id") or ""), _file_row_id(entry))
        carriers = paths.get(key, [])
        if not entry.get("untracked") or len(carriers) < 2:
            kept.append(entry)
            continue
        conflicts.append(
            ConfigChange(
                change_type="conflict",
                component_id=entry["component_id"],
                config_id=key[2],
                config_name=entry.get("row_name", ""),
                path=str(entry.get("path", "")),
                details=[
                    (
                        f"row id {key[2]} is carried by {len(carriers)} row directories "
                        f"({', '.join(sorted(carriers))}); keep one and remove the others' "
                        f"'row_id'"
                    )
                ],
                is_row=True,
                parent_config_id=key[1],
            )
        )
    local_rows[:] = kept
    return conflicts


def component_change_problem(
    entry_component_id: str, local_data: dict[str, Any], config_path: str
) -> str | None:
    """The refusal message for a tracked dir whose ``_keboola.component_id`` changed (D11).

    Companion files belong to a component: re-labelling a directory would
    silently hand its ``transform.sql`` / ``code.py`` to a component that
    ignores it and push the config under the wrong component.
    """
    file_component = (local_data.get("_keboola") or {}).get("component_id")
    if not file_component or file_component == entry_component_id:
        return None
    return (
        f"{config_path}: _config.yml says _keboola.component_id '{file_component}', but the "
        f"directory is tracked as a '{entry_component_id}' config"
    )


def raise_on_component_change(problems: list[str]) -> None:
    """Refuse a diff/push over a tracked directory relabelled to another component."""
    if not problems:
        return
    raise ConfigError(
        "Refusing to diff or push: "
        + "; ".join(problems)
        + ". A config cannot change its component. Restore the original "
        "_keboola.component_id, or move the files to a new directory without "
        "_keboola.config_id so push creates a new config of the other component."
    )


def _row_id_of(row_dir: Path) -> str:
    """The ``_keboola.row_id`` a row file carries, ``""`` when none or unreadable."""
    try:
        data = yaml.safe_load((row_dir / CONFIG_FILENAME).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return ""
    meta = data.get("_keboola") if isinstance(data, dict) else None
    return str(meta.get("row_id") or "") if isinstance(meta, dict) else ""


def adopted_row_entries(config_dir: Path) -> list[ManifestConfigRow]:
    """Manifest rows for the row dirs of a config dir being registered by its id (D8, T3).

    Every row dir carrying a row id is tracked with an UNKNOWN base: nothing
    proves which side changed it, so a difference is a conflict, never an
    update. That includes an id the remote config no longer has -- it stays
    ``remote_deleted``, which push never acts on. Left out, registration
    erased its identity and the next push created the deleted row again (T3).
    A row dir without an id stays untracked: the next push creates it.
    """
    rows: list[ManifestConfigRow] = []
    ids = [(row_dir, _row_id_of(row_dir)) for row_dir in _row_dirs(config_dir)]
    counts = Counter(row_id for _, row_id in ids)
    for row_dir, row_id in ids:
        # An id two dirs carry is ambiguous: neither is registered, and the
        # row diff reports both as a conflict (split_ambiguous_rows).
        if not row_id or counts[row_id] > 1:
            continue
        rows.append(
            ManifestConfigRow(
                id=row_id,
                path=f"rows/{row_dir.name}",
                metadata={
                    "pull_hash": "",
                    "pull_config_hash": "",
                    UNKNOWN_BASE_KEY: UNKNOWN_BASE_ADOPTED,
                },
            )
        )
    return rows
