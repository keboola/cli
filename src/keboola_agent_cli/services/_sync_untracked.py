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
  baseline says which side changed), never an update to send. A remote row no
  local dir carries is never a delete: it is listed in ``remote_only_rows[]``
  (sync group 3, D5) -- it may as well have been added in the UI.
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
    branch_tree_path,
    find_untracked_rows,
)
from ..sync.diff_engine import ConfigChange
from ..sync.manifest import Manifest, ManifestConfigRow, ManifestConfiguration
from ._sync_models import ADOPTED_ROW_IDS_KEY

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
    carried: set[str] = set()
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
            carried.add(file_row_id)
            entry["row_id"] = file_row_id
            entry.pop("parent_path")
            if f"{prefix}{file_row_id}" not in remote_rows:
                found.gone_row_keys.add(f"{prefix}{file_row_id}")
        found.local_rows.append(entry)
    if not config_id:
        return
    # A remote row an adopted dir carries is compared with an unknown base:
    # equal is unchanged, different is a conflict. A remote row NO local dir
    # carries says nothing either way -- removed here, or added in the UI
    # since -- so it is not tracked: diff reports it under
    # ``remote_only_rows[]`` (D5), which push never creates or deletes.
    for key in remote_rows:
        if key.startswith(prefix) and key.removeprefix(prefix) in carried:
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
    branch_id: int | None,
    remote_rows: dict[str, dict[str, Any]],
    ignored_components: frozenset[str],
) -> list[ConfigChange]:
    """Add the untracked row dirs under tracked configs to *found*, then settle every identity.

    Without a row id on the target the row diff flags them ``added`` and push
    creates them; :func:`settle_row_identities` then decides, for these and
    the adopted configs' rows, which ones are a known row or ambiguous --
    the conflicts it returns join the changeset. A still-tracked ignored
    parent contributes none: push would re-add what pull refuses to fetch.
    Off the promote path, a row file's own id counts as its identity.
    """
    not_promoting = source_branch_path == branch_tree_path(manifest, branch_id)
    for untracked in find_untracked_rows(
        project_root,
        manifest,
        service._read_config_file,
        source_branch_path,
        entries,
        file_ids_as_source=not_promoting,
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
    return settle_row_identities(found, remote_rows, entries, adopt_ids=not_promoting)


def _file_row_id(entry: dict[str, Any]) -> str:
    data = entry.get("data")
    meta = data.get("_keboola") if isinstance(data, dict) else None
    return str(meta.get("row_id") or "") if isinstance(meta, dict) else ""


def _identity(entry: dict[str, Any]) -> str:
    """The row id a row dir stands for: its manifest id when tracked, else its file's id.

    A tracked row is the row the manifest says, whatever its file carries (a
    row created before push wrote ``row_id`` may carry a copied one).
    """
    if not entry.get("untracked") and entry.get("row_id"):
        return str(entry["row_id"])
    return _file_row_id(entry)


def _carriers(local_rows: list[dict[str, Any]]) -> dict[tuple[str, str, str], list[str]]:
    """``(component, parent id, row id)`` -> the row dirs that stand for that row."""
    paths: dict[tuple[str, str, str], list[str]] = {}
    for entry in local_rows:
        row_id = _identity(entry)
        if entry.get("parent_config_id") and row_id:
            key = (entry["component_id"], str(entry["parent_config_id"]), row_id)
            paths.setdefault(key, []).append(str(entry.get("path", "")))
    return paths


def _ambiguity_conflict(
    entry: dict[str, Any], key: tuple[str, str, str], carriers: list[str]
) -> ConfigChange:
    listed = ", ".join(sorted(carriers))
    return ConfigChange(
        change_type="conflict",
        component_id=entry["component_id"],
        config_id=key[2],
        config_name=entry.get("row_name", ""),
        path=str(entry.get("path", "")),
        details=[
            (
                f"row id {key[2]} is carried by {len(carriers)} row directories ({listed}). "
                f"Delete every directory that is not row {key[2]}: the one left with row id "
                f"{key[2]} is that row. To keep one as a NEW row instead, remove the "
                f"'row_id' line from its _config.yml."
            )
        ],
        is_row=True,
        parent_config_id=key[1],
    )


def settle_row_identities(
    found: UntrackedRows,
    remote_rows: dict[str, dict[str, Any]],
    entries: list[ManifestConfiguration],
    *,
    adopt_ids: bool,
) -> list[ConfigChange]:
    """Decide what each untracked row dir is, by the row id its file carries (round 5, 6).

    - Two or more row dirs of one config standing for the same row id (a
      tracked one by its manifest id, an untracked one by its file) say
      nothing about which one is the row: pull writes into neither (its
      ambiguous-id rule), push creates, updates and deletes none -- EVERY
      carrier becomes a ``conflict`` naming them all, and the id is taken out
      of the delete inference (a tracked row whose dir is gone while copies
      carry its id is never ``deleted``).
    - A row dir that ALONE carries an id the config knows -- a row of the
      remote config, a row the manifest tracks (its own dir gone), or an id
      recorded when the config was registered by its id (``adopted_row_ids``)
      -- IS that row (*adopt_ids*, any push but the promote path): diffed by
      id with an unknown base (a moved dir has no proven baseline at its new
      path), ``remote_deleted`` when the remote lost it,
      never POSTed as a new row. Resolving an ambiguity therefore never
      creates the row again, and the dir that lost its ``row_id`` is new.
    - Any other id is a copied placeholder: the row is new.

    A new config's row ids are placeholders and never count. Mutates
    ``found.local_rows`` (and its baselines); returns the conflicts.
    """
    known: dict[tuple[str, str], set[str]] = {}
    for cfg in entries:
        ids = {row.id for row in cfg.rows}
        ids.update(str(i) for i in cfg.metadata.get(ADOPTED_ROW_IDS_KEY) or [])
        known[(cfg.component_id, cfg.id)] = ids
    carriers = _carriers(found.local_rows)
    conflicts: list[ConfigChange] = []
    kept: list[dict[str, Any]] = []
    for entry in found.local_rows:
        row_id = _identity(entry)
        key = (entry["component_id"], str(entry.get("parent_config_id") or ""), row_id)
        remote_key = f"{key[0]}/{key[1]}/rows/{row_id}"
        if key[1] and row_id and len(carriers.get(key, [])) > 1:
            # Every carrier -- a tracked one too -- is a conflict, and the row
            # is never inferred deleted while dirs carry its id (round 7).
            conflicts.append(_ambiguity_conflict(entry, key, carriers[key]))
            found.tracked_row_keys.discard(remote_key)
            continue
        kept.append(entry)
        if not entry.get("untracked") or not key[1] or not row_id or not adopt_ids:
            continue
        if entry.get("row_id"):
            continue  # an adopted config's row: already diffed by its id
        if remote_key in remote_rows or row_id in known.get(key[:2], set()):
            # A lone carrier of a known id IS that row. At a path other than
            # its tracked one nothing proves its baseline: unknown, so any
            # difference is a conflict until a pull records it there (round 7).
            entry["row_id"] = row_id
            found.base_hashes[remote_key] = UNKNOWN_BASE_HASH
            if remote_key not in remote_rows:
                found.gone_row_keys.add(remote_key)
    found.local_rows[:] = kept
    return conflicts


def register_adopted_rows(entry: ManifestConfiguration, config_dir: Path) -> None:
    """Track the rows of a config being registered by its id, and remember every row id (D8)."""
    entry.rows = adopted_row_entries(config_dir)
    entry.metadata[ADOPTED_ROW_IDS_KEY] = sorted(
        {row_id for row_dir in _row_dirs(config_dir) if (row_id := _row_id_of(row_dir))}
    )


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
        # An id two dirs carry is ambiguous: neither is registered as the
        # row (register_adopted_rows still records the id), and the row diff
        # reports both as a conflict (settle_row_identities).
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
