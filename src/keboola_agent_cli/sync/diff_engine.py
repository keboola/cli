"""Diff engine for comparing local configs against remote API state.

Produces a changeset describing what needs to be created, updated,
or deleted when pushing local changes to Keboola.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from ..constants import DIFF_MAX_DEPTH, DIFF_MAX_LINES, ENCRYPTED_PLACEHOLDER
from .secrets import is_encrypted_value

# Keys that are internal bookkeeping and should be excluded from comparison.
# NOTE: _configuration_extra is NOT ignored -- it carries the actual config
# payload for components like keboola.flow (phases, tasks, conditions) that
# don't use the standard "parameters" key.
_IGNORED_KEYS: frozenset[str] = frozenset({"_keboola", "version"})


class ConfigChange:
    """Represents a single configuration or configuration-row change.

    ``change_type`` values:

    - ``"added"`` -- new local config/row not yet in remote
    - ``"modified"`` -- local changed, remote unchanged (safe to push)
    - ``"remote_modified"`` -- remote changed, local unchanged (run pull)
    - ``"conflict"`` -- both sides changed since last pull
    - ``"deleted"`` -- local file removed, wants to delete from remote

    Row changes set ``is_row=True`` and carry ``parent_config_id`` so
    ``sync push`` can dispatch them to the row-specific client methods
    (``create_config_row`` / ``update_config_row`` / ``delete_config_row``).
    """

    def __init__(
        self,
        change_type: str,
        component_id: str,
        config_id: str,  # empty string for new configs/rows
        config_name: str,
        path: str,
        local_data: dict[str, Any] | None = None,
        remote_data: dict[str, Any] | None = None,
        details: list[str] | None = None,
        is_row: bool = False,
        parent_config_id: str = "",
    ):
        self.change_type = change_type
        self.component_id = component_id
        self.config_id = config_id
        self.config_name = config_name
        self.path = path
        self.local_data = local_data
        self.remote_data = remote_data
        self.details = details or []
        self.is_row = is_row
        self.parent_config_id = parent_config_id

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSON output."""
        data: dict[str, Any] = {
            "change_type": self.change_type,
            "component_id": self.component_id,
            "config_id": self.config_id,
            "config_name": self.config_name,
            "path": self.path,
            "details": self.details,
        }
        if self.is_row:
            data["is_row"] = True
            data["parent_config_id"] = self.parent_config_id
        return data


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def normalize_for_comparison(obj: Any) -> Any:
    """Normalize a config dict for comparison.

    - Replace all encrypted values (KBC::*Secure::*) with a fixed placeholder
      to avoid false diffs from encryption nonces.
    - Sort dict keys for consistent hashing.
    - Strip ``_keboola`` metadata block (internal, not part of config content).
    - Strip ``version`` key (local format marker).
    - Strip ``_configuration_extra`` key (internal round-trip aid).

    Returns a deep copy -- the original object is never mutated.
    """
    return _normalize(copy.deepcopy(obj))


def _normalize(obj: Any) -> Any:
    """Recursively normalize *obj* in place (operates on a deep copy)."""
    if isinstance(obj, dict):
        # Remove ignored keys
        for key in _IGNORED_KEYS:
            obj.pop(key, None)

        # Recurse into remaining values, replacing encrypted strings
        normalized: dict[str, Any] = {}
        for key in sorted(obj.keys()):
            normalized[key] = _normalize(obj[key])
        return normalized

    if isinstance(obj, list):
        return [_normalize(item) for item in obj]

    if is_encrypted_value(obj):
        return ENCRYPTED_PLACEHOLDER

    return obj


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def config_hash(config_data: dict[str, Any]) -> str:
    """Compute a normalized hash of a config for fast change detection."""
    normalized = normalize_for_comparison(config_data)
    return hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Deep diff
# ---------------------------------------------------------------------------


def deep_diff(
    local: dict[str, Any],
    remote: dict[str, Any],
    path: str = "",
) -> list[str]:
    """Compute human-readable diff descriptions between two config dicts.

    Returns a list of strings like:

    - ``"parameters.api_url changed: 'old' -> 'new'"``
    - ``"output.tables[0].destination added"``
    - ``"description removed"``

    Skips encrypted values (shows ``'changed (encrypted)'`` instead of
    actual values).  Limits comparison depth to ``DIFF_MAX_DEPTH`` levels
    and caps the output at ``DIFF_MAX_LINES`` entries.
    """
    results: list[str] = []
    _deep_diff_recurse(
        normalize_for_comparison(local),
        normalize_for_comparison(remote),
        path=path,
        depth=0,
        results=results,
    )
    return results[:DIFF_MAX_LINES]


def _deep_diff_recurse(
    local_val: Any,
    remote_val: Any,
    *,
    path: str,
    depth: int,
    results: list[str],
) -> None:
    """Recursive worker for :func:`deep_diff`."""
    # Early exit when we've already collected enough detail lines.
    if len(results) >= DIFF_MAX_LINES:
        return

    if type(local_val) != type(remote_val):  # noqa: E721
        results.append(_format_changed(path, remote_val, local_val))
        return

    if isinstance(local_val, dict) and isinstance(remote_val, dict):
        _diff_dicts(local_val, remote_val, path=path, depth=depth, results=results)
        return

    if isinstance(local_val, list) and isinstance(remote_val, list):
        _diff_lists(local_val, remote_val, path=path, depth=depth, results=results)
        return

    # Scalar comparison
    if local_val != remote_val:
        results.append(_format_changed(path, remote_val, local_val))


def _diff_dicts(
    local_dict: dict[str, Any],
    remote_dict: dict[str, Any],
    *,
    path: str,
    depth: int,
    results: list[str],
) -> None:
    """Compare two dicts key by key."""
    all_keys = sorted(set(local_dict.keys()) | set(remote_dict.keys()))

    for key in all_keys:
        if len(results) >= DIFF_MAX_LINES:
            return

        child_path = f"{path}.{key}" if path else key
        in_local = key in local_dict
        in_remote = key in remote_dict

        if in_local and not in_remote:
            results.append(f"{child_path} added")
            continue

        if not in_local and in_remote:
            results.append(f"{child_path} removed")
            continue

        # Both sides have the key -- recurse if within depth budget.
        if depth < DIFF_MAX_DEPTH:
            _deep_diff_recurse(
                local_dict[key],
                remote_dict[key],
                path=child_path,
                depth=depth + 1,
                results=results,
            )
        else:
            # Beyond depth limit, fall back to equality check.
            if local_dict[key] != remote_dict[key]:
                results.append(f"{child_path} changed")


def _diff_lists(
    local_list: list[Any],
    remote_list: list[Any],
    *,
    path: str,
    depth: int,
    results: list[str],
) -> None:
    """Compare two lists element by element."""
    if len(local_list) != len(remote_list):
        results.append(f"{path} list length changed: {len(remote_list)} -> {len(local_list)}")
        return

    for idx, (local_item, remote_item) in enumerate(zip(local_list, remote_list, strict=True)):
        if len(results) >= DIFF_MAX_LINES:
            return

        child_path = f"{path}[{idx}]"

        if depth < DIFF_MAX_DEPTH:
            _deep_diff_recurse(
                local_item,
                remote_item,
                path=child_path,
                depth=depth + 1,
                results=results,
            )
        else:
            if local_item != remote_item:
                results.append(f"{child_path} changed")


def _format_changed(path: str, old_val: Any, new_val: Any) -> str:
    """Format a single scalar change, masking encrypted placeholders."""
    label = path if path else "(root)"

    if old_val == ENCRYPTED_PLACEHOLDER or new_val == ENCRYPTED_PLACEHOLDER:
        return f"{label} changed (encrypted)"

    return f"{label} changed: {_repr_short(old_val)} -> {_repr_short(new_val)}"


def _repr_short(value: Any, max_length: int = 60) -> str:
    """Short repr of a value, truncated if too long."""
    text = repr(value)
    if len(text) > max_length:
        return text[: max_length - 3] + "..."
    return text


# ---------------------------------------------------------------------------
# Changeset computation
# ---------------------------------------------------------------------------


def compute_changeset(
    local_configs: list[dict[str, Any]],
    remote_configs: dict[str, dict[str, Any]],
    tracked_keys: set[str] | None = None,
    base_hashes: dict[str, str] | None = None,
    local_override_hashes: dict[str, str] | None = None,
) -> list[ConfigChange]:
    """3-way diff: compare local vs base (pull_hash) vs remote.

    Args:
        local_configs: List of dicts with keys:
            ``component_id``, ``config_id``, ``config_name``, ``path``, ``data``.
        remote_configs: Dict keyed by ``"{component_id}/{config_id}"`` with
            API config data (already converted to local format).
        tracked_keys: Optional set of ``"{component_id}/{config_id}"`` keys
            from the manifest.  Only manifest-tracked remote configs can be
            flagged as ``"deleted"``.
        base_hashes: Optional dict of ``"{component_id}/{config_id}"`` ->
            normalized config hash at last pull time.  Enables 3-way diff:

            - local changed, remote unchanged → ``"modified"`` (safe push)
            - local unchanged, remote changed → ``"remote_modified"`` (pull)
            - both changed → ``"conflict"``

            When ``None``, falls back to 2-way diff (any difference →
            ``"modified"``).
        local_override_hashes: Optional dict of ``"{component_id}/{config_id}"``
            -> hash to use for local side instead of computing from data.
            Used when local files haven't changed since pull to avoid lossy
            code-merge roundtrip.

    Returns:
        List of :class:`ConfigChange` objects.
    """
    changes: list[ConfigChange] = []
    seen_remote_keys: set[str] = set()

    for entry in local_configs:
        component_id: str = entry["component_id"]
        config_id: str = entry.get("config_id", "")
        config_name: str = entry.get("config_name", "")
        path: str = entry.get("path", "")
        local_data: dict[str, Any] = entry.get("data", {})

        remote_key = f"{component_id}/{config_id}" if config_id else ""

        # New config (no id yet, or not in remote)
        if not config_id or remote_key not in remote_configs:
            changes.append(
                ConfigChange(
                    change_type="added",
                    component_id=component_id,
                    config_id=config_id,
                    config_name=config_name,
                    path=path,
                    local_data=local_data,
                )
            )
            if remote_key:
                seen_remote_keys.add(remote_key)
            continue

        # Existing config -- 3-way compare
        seen_remote_keys.add(remote_key)
        remote_data: dict[str, Any] = remote_configs[remote_key]

        # Use override hash if available (avoids lossy code roundtrip)
        override_h = (local_override_hashes or {}).get(remote_key)
        local_h = override_h if override_h else config_hash(local_data)
        remote_h = config_hash(remote_data)

        if local_h == remote_h:
            # Unchanged -- skip
            continue

        # Determine change direction using base hash (pull-time snapshot)
        base_h = (base_hashes or {}).get(remote_key)
        if base_h is not None:
            local_changed = local_h != base_h
            remote_changed = remote_h != base_h
        else:
            # No base hash available -- fall back to 2-way
            local_changed = True
            remote_changed = False

        if local_changed and remote_changed:
            change_type = "conflict"
            details = deep_diff(local_data, remote_data)
        elif remote_changed and not local_changed:
            change_type = "remote_modified"
            details = deep_diff(remote_data, local_data)
        else:
            # local_changed (and not remote_changed), or fallback
            change_type = "modified"
            details = deep_diff(local_data, remote_data)

        changes.append(
            ConfigChange(
                change_type=change_type,
                component_id=component_id,
                config_id=config_id,
                config_name=config_name,
                path=path,
                local_data=local_data,
                remote_data=remote_data,
                details=details,
            )
        )

    # Detect deleted configs: remote configs that were previously tracked
    # in the manifest but whose local file was removed.
    for remote_key, remote_data in remote_configs.items():
        if remote_key in seen_remote_keys:
            continue

        # Only flag configs the manifest knows about.
        # Unknown remote configs are new on the server side.
        if tracked_keys is not None and remote_key not in tracked_keys:
            continue

        parts = remote_key.split("/", 1)
        component_id = parts[0] if len(parts) > 0 else ""
        config_id = parts[1] if len(parts) > 1 else ""

        changes.append(
            ConfigChange(
                change_type="deleted",
                component_id=component_id,
                config_id=config_id,
                config_name=remote_data.get("name", ""),
                path="",
                remote_data=remote_data,
            )
        )

    return changes


def compute_row_changeset(
    local_rows: list[dict[str, Any]],
    remote_rows: dict[str, dict[str, Any]],
    tracked_row_keys: set[str] | None = None,
    base_hashes: dict[str, str] | None = None,
) -> list[ConfigChange]:
    """3-way diff for configuration rows, parallel to :func:`compute_changeset`.

    Row keys are ``"{component_id}/{parent_config_id}/rows/{row_id}"`` so they
    cannot collide with parent-config keys. Results carry ``is_row=True`` and
    ``parent_config_id`` so ``sync push`` can route them to
    ``client.create_config_row`` / ``update_config_row`` / ``delete_config_row``.

    Args:
        local_rows: List of dicts with keys:
            ``component_id``, ``parent_config_id``, ``row_id``,
            ``row_name``, ``path``, ``data``.
        remote_rows: Dict keyed by ``"{component_id}/{parent_config_id}/rows/{row_id}"``
            with API row data (already converted to local format via
            :func:`api_row_to_local`).
        tracked_row_keys: Optional set of row keys from the manifest.
            Only manifest-tracked remote rows can be flagged as ``"deleted"``.
        base_hashes: Optional dict of row_key -> normalized config hash at last
            pull time. Enables 3-way diff (same semantics as parent configs).

    Returns:
        List of :class:`ConfigChange` objects, each with ``is_row=True``.
    """
    changes: list[ConfigChange] = []
    seen_remote_keys: set[str] = set()

    for entry in local_rows:
        component_id: str = entry["component_id"]
        parent_config_id: str = entry.get("parent_config_id", "")
        row_id: str = entry.get("row_id", "")
        row_name: str = entry.get("row_name", "")
        path: str = entry.get("path", "")
        local_data: dict[str, Any] = entry.get("data", {})

        remote_key = (
            f"{component_id}/{parent_config_id}/rows/{row_id}"
            if row_id and parent_config_id
            else ""
        )

        # New row (no id yet, or not in remote)
        if not row_id or remote_key not in remote_rows:
            changes.append(
                ConfigChange(
                    change_type="added",
                    component_id=component_id,
                    config_id=row_id,
                    config_name=row_name,
                    path=path,
                    local_data=local_data,
                    is_row=True,
                    parent_config_id=parent_config_id,
                )
            )
            if remote_key:
                seen_remote_keys.add(remote_key)
            continue

        seen_remote_keys.add(remote_key)
        remote_data: dict[str, Any] = remote_rows[remote_key]

        local_h = config_hash(local_data)
        remote_h = config_hash(remote_data)

        if local_h == remote_h:
            continue

        base_h = (base_hashes or {}).get(remote_key)
        if base_h is not None:
            local_changed = local_h != base_h
            remote_changed = remote_h != base_h
        else:
            local_changed = True
            remote_changed = False

        if local_changed and remote_changed:
            change_type = "conflict"
            details = deep_diff(local_data, remote_data)
        elif remote_changed and not local_changed:
            change_type = "remote_modified"
            details = deep_diff(remote_data, local_data)
        else:
            change_type = "modified"
            details = deep_diff(local_data, remote_data)

        changes.append(
            ConfigChange(
                change_type=change_type,
                component_id=component_id,
                config_id=row_id,
                config_name=row_name,
                path=path,
                local_data=local_data,
                remote_data=remote_data,
                details=details,
                is_row=True,
                parent_config_id=parent_config_id,
            )
        )

    # Deleted rows: tracked in manifest but missing from local filesystem
    for remote_key, remote_data in remote_rows.items():
        if remote_key in seen_remote_keys:
            continue
        if tracked_row_keys is not None and remote_key not in tracked_row_keys:
            continue

        # remote_key shape: "{component_id}/{parent_config_id}/rows/{row_id}"
        comp_and_parent, _, row_id = remote_key.rpartition("/rows/")
        component_id, _, parent_config_id = comp_and_parent.partition("/")

        changes.append(
            ConfigChange(
                change_type="deleted",
                component_id=component_id,
                config_id=row_id,
                config_name=remote_data.get("name", ""),
                path="",
                remote_data=remote_data,
                is_row=True,
                parent_config_id=parent_config_id,
            )
        )

    return changes
