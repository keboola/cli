"""JSON deep-merge and nested-path utilities.

Used by ``config update`` to patch configuration content without
losing sibling keys -- the exact problem that MCP server's
``update_config`` tool has (keboola/mcp-server#468).
"""

from __future__ import annotations

import copy
from typing import Any


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
    (new intermediate containers are always dicts).
    """
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


def compute_diff(
    old: dict[str, Any],
    new: dict[str, Any],
    path: str = "",
) -> list[str]:
    """Produce a human-readable list of changes between two dicts.

    Each entry looks like:
        ``"parameters.tables.count: 5 -> 10"``
        ``"parameters.newKey: (absent) -> 'hello'"``
        ``"parameters.removed: 42 -> (absent)"``
    """
    changes: list[str] = []
    all_keys = sorted(set(list(old.keys()) + list(new.keys())))

    for key in all_keys:
        full_path = f"{path}.{key}" if path else key
        in_old = key in old
        in_new = key in new

        if in_old and in_new:
            old_val = old[key]
            new_val = new[key]
            if isinstance(old_val, dict) and isinstance(new_val, dict):
                changes.extend(compute_diff(old_val, new_val, full_path))
            elif old_val != new_val:
                changes.append(f"{full_path}: {_fmt(old_val)} -> {_fmt(new_val)}")
        elif in_old and not in_new:
            changes.append(f"{full_path}: {_fmt(old[key])} -> (absent)")
        else:
            changes.append(f"{full_path}: (absent) -> {_fmt(new[key])}")

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
