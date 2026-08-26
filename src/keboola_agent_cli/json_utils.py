"""JSON deep-merge and nested-path utilities.

Used by ``config update`` to patch configuration content without
losing sibling keys -- the exact problem that MCP server's
``update_config`` tool has (keboola/mcp-server#468).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

# Sentinel marking "the key does not exist on this side" in a DiffEntry --
# distinct from an explicit ``None`` value, which is a legal JSON value.
_ABSENT: Any = object()


def deep_merge(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *source* into *target* (non-mutating).

    Rules:
    * dict + dict → recursively merged
    * anything else → *source* wins (including list replaces list)

    Returns a new dict; neither *target* nor *source* is mutated.
    """
    result = copy.deepcopy(target)
    for key, src_value in source.items():
        if key in result and isinstance(result[key], dict) and isinstance(src_value, dict):
            result[key] = deep_merge(result[key], src_value)
        else:
            result[key] = copy.deepcopy(src_value)
    return result


def get_nested_value(obj: Any, path: str) -> Any:
    """Retrieve a value from a nested structure using a dot-separated path.

    Supports integer segments for list indexing (e.g. ``"tables.0.name"``).

    Raises ``KeyError`` or ``IndexError`` if the path does not exist.
    """
    for segment in path.split("."):
        if isinstance(obj, dict):
            obj = obj[segment]
        elif isinstance(obj, list):
            obj = obj[int(segment)]
        else:
            raise KeyError(f"Cannot traverse into {type(obj).__name__} with key '{segment}'")
    return obj


def set_nested_value(obj: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    """Set a value at a dot-separated path, creating intermediate dicts.

    Returns a deep-copied dict with the value set — *obj* is not mutated.

    Supports integer segments for list indexing on **existing** lists
    (new intermediate containers are always dicts) using dot-separated
    integers, e.g. ``"files.0.name"`` -- NOT bracket syntax like
    ``"files[0].name"``. Bracket syntax raises ``ValueError`` instead of
    being silently accepted: without this check, a path like ``"files[0]"``
    (no ``.`` in it) would pass straight through as a single segment and
    create a literal ``"files[0]"`` dict key instead of indexing into the
    list -- the exact silent-corruption bug this guard closes (issue #593).

    Raises:
        ValueError: If *path* contains ``[`` or ``]`` (bracket syntax).
        KeyError: If a segment cannot be traversed/set on the current
            container (e.g. an int segment against a dict, or a dict
            segment against a list).
    """
    if "[" in path or "]" in path:
        raise ValueError(
            f"Invalid path {path!r}: bracket syntax like 'files[0]' is not "
            "supported. Use dot-separated integer segments instead, e.g. "
            "'files.0'."
        )
    result = copy.deepcopy(obj)
    segments = path.split(".")
    current: Any = result
    for segment in segments[:-1]:
        if isinstance(current, dict):
            if segment not in current:
                current[segment] = {}
            current = current[segment]
        elif isinstance(current, list):
            current = current[int(segment)]
        else:
            raise KeyError(f"Cannot traverse into {type(current).__name__} with key '{segment}'")

    last = segments[-1]
    if isinstance(current, dict):
        current[last] = copy.deepcopy(value)
    elif isinstance(current, list):
        current[int(last)] = copy.deepcopy(value)
    else:
        raise KeyError(f"Cannot set key '{last}' on {type(current).__name__}")
    return result


@dataclass(frozen=True)
class DiffEntry:
    """One changed dot-separated path between two nested dicts.

    ``old`` / ``new`` hold the value on each side, or the module-private
    ``_ABSENT`` sentinel when the key does not exist there -- distinct from an
    explicit ``None``, which is a legal JSON value. Callers read the
    ``old_present`` / ``new_present`` properties instead of comparing against
    the sentinel.
    """

    path: str
    old: Any
    new: Any

    @property
    def old_present(self) -> bool:
        return self.old is not _ABSENT

    @property
    def new_present(self) -> bool:
        return self.new is not _ABSENT


def compute_diff_entries(
    old: dict[str, Any],
    new: dict[str, Any],
    path: str = "",
) -> list[DiffEntry]:
    """Compute changed paths between two dicts as structured entries.

    The recursive walk behind :func:`compute_diff` (which formats these
    entries for humans), exposed as data so callers can post-process paths --
    e.g. intersect two pairwise diffs into a three-way ``ours``/``theirs``/
    ``both`` classification (merge-request conflict presentation, DMD-1899).

    Nested dicts recurse; any other type mismatch or value change yields one
    entry for the whole path. Keys are visited in sorted order.
    """
    entries: list[DiffEntry] = []
    all_keys = sorted(set(list(old.keys()) + list(new.keys())))

    for key in all_keys:
        full_path = f"{path}.{key}" if path else key
        in_old = key in old
        in_new = key in new

        if in_old and in_new:
            old_val = old[key]
            new_val = new[key]
            if isinstance(old_val, dict) and isinstance(new_val, dict):
                entries.extend(compute_diff_entries(old_val, new_val, full_path))
            elif old_val != new_val:
                entries.append(DiffEntry(path=full_path, old=old_val, new=new_val))
        elif in_old and not in_new:
            entries.append(DiffEntry(path=full_path, old=old[key], new=_ABSENT))
        else:
            entries.append(DiffEntry(path=full_path, old=_ABSENT, new=new[key]))

    return entries


def compute_diff(
    old: dict[str, Any],
    new: dict[str, Any],
    path: str = "",
) -> list[str]:
    """Produce a human-readable list of changes between two dicts.

    A formatter over :func:`compute_diff_entries`. Each entry looks like:
        ``"parameters.tables.count: 5 -> 10"``
        ``"parameters.newKey: (absent) -> 'hello'"``
        ``"parameters.removed: 42 -> (absent)"``
    """
    changes: list[str] = []
    for entry in compute_diff_entries(old, new, path):
        old_s = _fmt(entry.old) if entry.old_present else "(absent)"
        new_s = _fmt(entry.new) if entry.new_present else "(absent)"
        changes.append(f"{entry.path}: {old_s} -> {new_s}")
    return changes


def _fmt(value: Any) -> str:
    """Format a value for diff display — truncate long representations."""
    if isinstance(value, str):
        s = repr(value)
    elif isinstance(value, dict):
        s = f"{{...}} ({len(value)} keys)"
    elif isinstance(value, list):
        s = f"[...] ({len(value)} items)"
    else:
        s = repr(value)
    max_len = 80
    return s if len(s) <= max_len else s[: max_len - 3] + "..."


def find_matches_in_json(
    obj: Any,
    match_fn: Any,
    path: str = "",
) -> list[str]:
    """Recursively walk a JSON-like object and return paths where match_fn(str_value) is True."""
    paths: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child_path = f"{path}.{key}" if path else key
            paths.extend(find_matches_in_json(value, match_fn, child_path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            child_path = f"{path}[{i}]"
            paths.extend(find_matches_in_json(item, match_fn, child_path))
    elif isinstance(obj, str):
        if match_fn(obj):
            paths.append(path)
    else:
        # Numbers, booleans -- convert to string for matching
        if obj is not None and match_fn(str(obj)):
            paths.append(path)
    return paths
