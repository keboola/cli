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

from collections.abc import Collection
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
# ``reason`` of a pull skip for a local ``_config.yml`` that cannot be parsed.
UNREADABLE_SKIP_REASON = "local _config.yml cannot be parsed; fix it, or pull --theirs"


@dataclass
class LocalFileCheck:
    """The invalid ``_config.yml`` files of one tree.

    Attributes:
        held_paths: Config dir paths (relative to the branch dir) whose
            config or one of whose rows has an invalid file.
        held_keys: ``"{component}/{config}"`` keys of the tracked configs
            among them.
        errors: One ``errors[]`` entry per invalid file.
    """

    held_paths: set[str] = field(default_factory=set)
    held_keys: set[str] = field(default_factory=set)
    errors: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class ConfigYaml:
    """A parsed ``_config.yml``: its mapping, or why it cannot be used.

    Attributes:
        data: The parsed mapping; ``None`` when the file is unusable.
        problem: Why it is unusable (``""`` when *data* is set).
    """

    data: dict[str, Any] | None
    problem: str = ""


@dataclass(frozen=True)
class FileOwner:
    """The config a ``_config.yml`` belongs to.

    Attributes:
        path: The config dir path, relative to the branch dir.
        is_row: The file is one of that config's rows.
    """

    path: str
    is_row: bool


@dataclass(frozen=True)
class FileIdentity:
    """Who a config dir is, best effort (``component_id`` ``unknown`` if no one knows)."""

    component_id: str
    config_id: str


def load_config_yaml(config_file: Path) -> ConfigYaml:
    """Parse *config_file*; unusable: not UTF-8, not valid YAML, or not a mapping (empty too)."""
    try:
        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return ConfigYaml(None, f"is not valid YAML ({first})")
    if not isinstance(data, dict):
        found = "an empty file" if data is None else f"a {type(data).__name__}"
        return ConfigYaml(None, f"must be a YAML mapping, found {found}")
    return ConfigYaml(data)


def _owner(branch_dir: Path, rel_parts: tuple[str, ...], tracked: set[str]) -> FileOwner:
    """The config a ``_config.yml`` dir belongs to, decided by structure.

    A row dir is ``<config dir>/rows/<row>``: its grandparent is a config --
    tracked, or holding a ``_config.yml``. A ``rows`` segment alone says
    nothing: a config named "Rows" lives in ``.../rows`` (Devin 4416004404).
    """
    path = "/".join(rel_parts)
    if path in tracked or len(rel_parts) < 3 or rel_parts[-2] != _ROWS_DIR:
        return FileOwner(path, is_row=False)
    parent = "/".join(rel_parts[:-2])
    if parent in tracked or (branch_dir / parent / CONFIG_FILENAME).is_file():
        return FileOwner(parent, is_row=True)
    return FileOwner(path, is_row=False)


def _identity(
    branch_dir: Path, owner: str, tracked: dict[str, ManifestConfiguration]
) -> FileIdentity:
    """Who the config at *owner* is: its manifest entry, else its ``_keboola`` footer."""
    entry = tracked.get(owner)
    if entry is not None:
        return FileIdentity(entry.component_id, entry.id)
    owner_file = branch_dir / owner / CONFIG_FILENAME
    data = load_config_yaml(owner_file).data if owner_file.is_file() else None
    meta = (data or {}).get("_keboola")
    if isinstance(meta, dict):
        return FileIdentity(
            str(meta.get("component_id") or "unknown"), str(meta.get("config_id") or "")
        )
    return FileIdentity("unknown", "")


def _is_ignored(owner: str, identity: FileIdentity, ignored: Collection[str]) -> bool:
    """An ignored component's dir, by its identity or by the path's component segment.

    The segment (``<type>/<component_id>/<name>``) still says whose dir it is
    when its ``_config.yml`` cannot be read and no scoped entry names it
    (Devin 4416004912).
    """
    parts = owner.split("/")
    segment = parts[1] if len(parts) >= 2 else ""
    return identity.component_id in ignored or segment in ignored


def check_local_files(
    project_root: Path,
    branch_path: str,
    configs: list[ManifestConfiguration],
    ignored: Collection[str] = (),
) -> LocalFileCheck:
    """Find every unusable ``_config.yml`` of one branch tree (configs and rows).

    Args:
        project_root: The sync working tree.
        branch_path: The branch tree diff reads from, the prefix of every
            reported ``path``.
        configs: The manifest entries of that tree, for each file's identity.
        ignored: Ignored components: diff and push never read their files.
    """
    check = LocalFileCheck()
    branch_dir = project_root / branch_path
    if not branch_dir.is_dir():
        return check
    tracked = {c.path: c for c in configs}
    for config_file in sorted(branch_dir.rglob(CONFIG_FILENAME)):
        rel_parts = config_file.parent.relative_to(branch_dir).parts
        if not rel_parts:
            continue  # the branch-level _config.yml
        owner = _owner(branch_dir, rel_parts, set(tracked))
        identity = _identity(branch_dir, owner.path, tracked)
        if _is_ignored(owner.path, identity, ignored):
            continue
        parsed = load_config_yaml(config_file)
        problem = parsed.problem
        if parsed.data is not None:
            unknown = unknown_local_keys(parsed.data, identity.component_id, is_row=owner.is_row)
            if unknown:
                problem = (
                    f"has top-level key(s) sync does not know: {', '.join(unknown)}. "
                    f"Push would drop them. Put extra configuration keys under "
                    f"_configuration_extra, or remove them"
                )
        if not problem:
            continue
        path = "/".join((branch_path, *rel_parts, CONFIG_FILENAME))
        check.held_paths.add(owner.path)
        check.errors.append(
            {
                "change_type": INVALID_CHANGE_TYPE,
                "component_id": identity.component_id,
                "config_id": identity.config_id,
                "path": path,
                "error_code": LOCAL_FILE_INVALID_CODE,
                "message": (
                    f"{path} {problem}. Nothing is pushed, updated or deleted for this "
                    f"configuration until the file is fixed."
                ),
            }
        )
    check.held_keys = {f"{c.component_id}/{c.id}" for c in configs if c.path in check.held_paths}
    return check


def drop_held(changes: list[Any], held_keys: set[str]) -> list[Any]:
    """*changes* without those of a held config or of its rows."""

    def key(change: Any) -> str:
        config_id = change.parent_config_id if change.is_row else change.config_id
        return f"{change.component_id}/{config_id}"

    return [c for c in changes if key(c) not in held_keys]
