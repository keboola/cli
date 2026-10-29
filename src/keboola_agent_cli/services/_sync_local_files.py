"""Strict reading of the local ``_config.yml`` files ``sync diff`` / ``sync push`` use.

A ``_config.yml`` that is not valid YAML (or not a mapping) used to be read as
"no file": a tracked config then classified as ``deleted`` and ``push --force``
deleted it on the remote (#827 item 9), an untracked one was skipped without a
word. A top-level key sync does not know (``storage:``, ``runtime:``) was
dropped by ``local_config_to_api`` just as silently (customer feedback 2.3).

:func:`check_local_files` scans the tree diff reads from once, before the
diff. Every such file is an ``errors[]`` entry (``SYNC_LOCAL_FILE_INVALID``)
and its config -- with all its rows -- is held: diff reports it, push neither
creates, updates nor deletes it, and the command exits 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..constants import CONFIG_FILENAME
from ..errors import ErrorCode
from ..sync.config_format import unknown_local_keys
from ..sync.manifest import ManifestConfiguration

LOCAL_FILE_INVALID_CODE = ErrorCode.SYNC_LOCAL_FILE_INVALID.value
# ``change_type`` of the ``errors[]`` entry, so push renders it like any other.
INVALID_CHANGE_TYPE = "invalid_local_file"
_ROWS_DIR = "rows"


@dataclass
class LocalFileCheck:
    """The invalid ``_config.yml`` files of one tree.

    Attributes:
        held_paths: Config dir paths (relative to the branch dir) whose
            config or one of whose rows has an invalid file.
        errors: One ``errors[]`` entry per invalid file.
    """

    held_paths: set[str] = field(default_factory=set)
    errors: list[dict[str, str]] = field(default_factory=list)

    def held_keys(self, configs: list[ManifestConfiguration]) -> set[str]:
        """``"{component}/{config}"`` keys of the tracked configs that are held."""
        return {f"{c.component_id}/{c.id}" for c in configs if c.path in self.held_paths}


def load_config_yaml(config_file: Path) -> tuple[dict[str, Any] | None, str]:
    """Parse *config_file*: ``(data, "")``, or ``(None, problem)`` when unusable.

    Unusable: not UTF-8, not valid YAML, or anything but a mapping (an
    empty file included).
    """
    try:
        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return None, f"is not valid YAML ({first})"
    if not isinstance(data, dict):
        found = "an empty file" if data is None else f"a {type(data).__name__}"
        return None, f"must be a YAML mapping, found {found}"
    return data, ""


def _owner(rel_parts: tuple[str, ...]) -> tuple[str, bool]:
    """The config dir path a ``_config.yml`` dir belongs to, and whether it is a row."""
    if _ROWS_DIR in rel_parts:
        return "/".join(rel_parts[: rel_parts.index(_ROWS_DIR)]), True
    return "/".join(rel_parts), False


def _identity(
    branch_dir: Path, owner: str, tracked: dict[str, ManifestConfiguration]
) -> tuple[str, str]:
    """``(component_id, config_id)`` of the config at *owner*, best effort."""
    entry = tracked.get(owner)
    if entry is not None:
        return entry.component_id, entry.id
    owner_file = branch_dir / owner / CONFIG_FILENAME
    data = load_config_yaml(owner_file)[0] if owner_file.is_file() else None
    meta = (data or {}).get("_keboola")
    if isinstance(meta, dict):
        return str(meta.get("component_id") or "unknown"), str(meta.get("config_id") or "")
    return "unknown", ""


def check_local_files(
    branch_dir: Path, branch_path: str, configs: list[ManifestConfiguration]
) -> LocalFileCheck:
    """Find every unusable ``_config.yml`` under *branch_dir* (configs and rows).

    Args:
        branch_dir: The tree diff reads from (``project_root / branch_path``).
        branch_path: Its name, the prefix of every reported ``path``.
        configs: The manifest entries of that tree, for each file's identity.
    """
    check = LocalFileCheck()
    if not branch_dir.is_dir():
        return check
    tracked = {c.path: c for c in configs}
    for config_file in sorted(branch_dir.rglob(CONFIG_FILENAME)):
        rel_parts = config_file.parent.relative_to(branch_dir).parts
        if not rel_parts:
            continue  # the branch-level _config.yml
        owner, is_row = _owner(rel_parts)
        component_id, config_id = _identity(branch_dir, owner, tracked)
        data, problem = load_config_yaml(config_file)
        if data is not None:
            unknown = unknown_local_keys(data, component_id, is_row=is_row)
            if unknown:
                problem = (
                    f"has top-level key(s) sync does not know: {', '.join(unknown)}. "
                    f"Push would drop them. Put extra configuration keys under "
                    f"_configuration_extra, or remove them"
                )
        if not problem:
            continue
        path = "/".join((branch_path, *rel_parts, CONFIG_FILENAME))
        check.held_paths.add(owner)
        check.errors.append(
            {
                "change_type": INVALID_CHANGE_TYPE,
                "component_id": component_id,
                "config_id": config_id,
                "path": path,
                "error_code": LOCAL_FILE_INVALID_CODE,
                "message": (
                    f"{path} {problem}. Nothing is pushed, updated or deleted for this "
                    f"configuration until the file is fixed."
                ),
            }
        )
    return check


def drop_held(changes: list[Any], held_keys: set[str]) -> list[Any]:
    """*changes* without those of a held config or of its rows."""

    def key(change: Any) -> str:
        config_id = change.parent_config_id if change.is_row else change.config_id
        return f"{change.component_id}/{config_id}"

    return [c for c in changes if key(c) not in held_keys]
