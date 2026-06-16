"""Pure helpers for the ``sync clone`` composite (#426).

A clone copies a *reference* synced project tree into a fresh target, applies
declarative overrides, and re-points the manifest so a subsequent ``sync push``
CREATEs everything fresh in the target project. Cloning into a **fresh** target
project needs no id surgery: the reference's config ids do not exist in the
target remote, so the diff classifies every config as ``added`` and the push
assigns new ULIDs -- and because ``created_id_map`` is keyed by the reference id
(the manifest entry's id before writeback), the Phase-C variable links and the
Phase-D flow task ``configId``s remap reference->ULID automatically.

These functions are deliberately side-effecting but **pure of API calls**: they
only touch the on-disk tree + the in-memory manifest, so they are unit-testable
without a client. ``SyncService.clone_project`` orchestrates them and drives the
diff/push.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml

from ..constants import CONFIG_FILENAME

# Duplicated here (not imported from sync_service) to avoid a circular import --
# clone.py is imported *by* sync_service.
VARIABLES_COMPONENT_ID = "keboola.variables"

# Default branch dir name when a config's branch is not listed in the manifest
# (non-git-branching projects pull a single "main/" tree).
_DEFAULT_BRANCH_DIR = "main"


def branch_path_map(manifest: Any) -> dict[int, str]:
    """Map ``branch_id -> on-disk branch dir name`` from the manifest branches."""
    return {b.id: b.path for b in manifest.branches}


def _read_yaml(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        yaml.dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


def copy_reference_tree(source_dir: Path, target_dir: Path) -> None:
    """Copy a reference project tree to a fresh target dir (incl. code files).

    Raises:
        FileExistsError: if ``target_dir`` already exists (the caller decides
            whether an existing target means "already cloned, just push").
    """
    if target_dir.exists():
        raise FileExistsError(f"Target directory already exists: {target_dir}")
    shutil.copytree(source_dir, target_dir)


def repoint_manifest_project(manifest: Any, *, project_id: int, api_host: str) -> None:
    """Re-point the manifest's project block at the clone's target project."""
    manifest.project.id = project_id
    manifest.project.api_host = api_host


def _config_dir(target_dir: Path, branch_map: dict[int, str], branch_id: int, path: str) -> Path:
    return target_dir / branch_map.get(branch_id, _DEFAULT_BRANCH_DIR) / path


def _remap_bucket_in_table_id(table_id: Any, bucket_map: dict[str, str]) -> Any:
    """Rewrite the bucket prefix of a storage table id via ``bucket_map``.

    A table id is ``<stage>.<bucket>.<table>`` whose bucket id is
    ``<stage>.<bucket>``. A bucket-level reference (``in.c-foo``) is mapped
    whole; a table reference (``in.c-foo.users``) has only its bucket prefix
    swapped. Unmatched / non-string values pass through unchanged.
    """
    if not isinstance(table_id, str):
        return table_id
    for old, new in bucket_map.items():
        if table_id == old:
            return new
        if table_id.startswith(old + "."):
            return new + table_id[len(old) :]
    return table_id


def _rewrite_buckets_in_config(config_dir: Path, bucket_map: dict[str, str]) -> int:
    config_file = config_dir / CONFIG_FILENAME
    if not config_file.exists():
        return 0
    data = _read_yaml(config_file)
    rewrites = 0
    for section, key in (("input", "source"), ("output", "destination")):
        mapping = data.get(section)
        if not isinstance(mapping, dict):
            continue
        for table in mapping.get("tables") or []:
            if not isinstance(table, dict) or key not in table:
                continue
            new_value = _remap_bucket_in_table_id(table[key], bucket_map)
            if new_value != table[key]:
                table[key] = new_value
                rewrites += 1
    if rewrites:
        _write_yaml(config_file, data)
    return rewrites


def apply_bucket_map(target_dir: Path, manifest: Any, bucket_map: dict[str, str]) -> int:
    """Rewrite bucket ids in every config's storage input/output mappings.

    Returns the number of table references rewritten. A no-op when
    ``bucket_map`` is empty.
    """
    if not bucket_map:
        return 0
    branch_map = branch_path_map(manifest)
    rewrites = 0
    for cfg in manifest.configurations:
        rewrites += _rewrite_buckets_in_config(
            _config_dir(target_dir, branch_map, cfg.branch_id, cfg.path), bucket_map
        )
        for row in cfg.rows:
            rewrites += _rewrite_buckets_in_config(
                _config_dir(target_dir, branch_map, cfg.branch_id, row.path), bucket_map
            )
    return rewrites


def _override_values_in_row(row_dir: Path, variable_values: dict[str, str]) -> int:
    config_file = row_dir / CONFIG_FILENAME
    if not config_file.exists():
        return 0
    data = _read_yaml(config_file)
    # keboola.variables rows hoist their ``values`` array to the YAML top level.
    values = data.get("values")
    if not isinstance(values, list):
        return 0
    overridden = 0
    for item in values:
        if isinstance(item, dict) and item.get("name") in variable_values:
            new_value = str(variable_values[item["name"]])
            if item.get("value") != new_value:
                item["value"] = new_value
                overridden += 1
    if overridden:
        _write_yaml(config_file, data)
    return overridden


def apply_variable_values(target_dir: Path, manifest: Any, variable_values: dict[str, str]) -> int:
    """Override ``keboola.variables`` row values by variable name.

    Returns the number of values overridden. A no-op when ``variable_values``
    is empty. Values are written as strings (the Keboola variables contract).
    """
    if not variable_values:
        return 0
    branch_map = branch_path_map(manifest)
    overridden = 0
    for cfg in manifest.configurations:
        if cfg.component_id != VARIABLES_COMPONENT_ID:
            continue
        for row in cfg.rows:
            overridden += _override_values_in_row(
                _config_dir(target_dir, branch_map, cfg.branch_id, row.path), variable_values
            )
    return overridden


def apply_instance_rename(target_dir: Path, manifest: Any, renames: dict[str, str]) -> int:
    """Rename config-path prefixes on disk and in the manifest.

    ``renames`` maps an old path prefix to a new one (e.g.
    ``{"extractor/keboola.ex-http/Acme": "extractor/keboola.ex-http/Globex"}``).
    For every config (and its rows) whose manifest ``path`` equals the prefix or
    starts with ``prefix + "/"``, the on-disk subtree is moved once and the
    manifest paths are rewritten. Returns the number of configs whose path
    changed. A no-op when ``renames`` is empty.
    """
    if not renames:
        return 0
    branch_map = branch_path_map(manifest)
    moved: set[tuple[str, str]] = set()
    renamed = 0
    for old, new in renames.items():
        for cfg in manifest.configurations:
            if not (cfg.path == old or cfg.path.startswith(old + "/")):
                continue
            branch_dir = branch_map.get(cfg.branch_id, _DEFAULT_BRANCH_DIR)
            move_key = (branch_dir, old)
            if move_key not in moved:
                src = target_dir / branch_dir / old
                dst = target_dir / branch_dir / new
                if src.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dst))
                moved.add(move_key)
            cfg.path = new + cfg.path[len(old) :]
            for row in cfg.rows:
                if row.path == old or row.path.startswith(old + "/"):
                    row.path = new + row.path[len(old) :]
            renamed += 1
    return renamed
