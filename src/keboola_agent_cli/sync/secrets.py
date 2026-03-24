"""Encrypted value detection for Keboola configurations.

Keboola stores secrets as encrypted markers (e.g.
``KBC::ProjectSecure::...``).  This module provides helpers to detect
such values and locate them inside arbitrarily nested configuration dicts.
"""

from __future__ import annotations

from typing import Any

ENCRYPTED_PREFIXES: tuple[str, ...] = (
    "KBC::ProjectSecure::",
    "KBC::ComponentSecure::",
    "KBC::ConfigSecure::",
    "KBC::ProjectWideSecure::",
)


def is_encrypted_value(value: Any) -> bool:
    """Return ``True`` if *value* is a Keboola encrypted marker string."""
    if not isinstance(value, str):
        return False
    return any(value.startswith(prefix) for prefix in ENCRYPTED_PREFIXES)


def is_secret_key(key: str) -> bool:
    """Return ``True`` if *key* indicates an encrypted field.

    By Keboola convention, encrypted parameter keys start with ``#``.
    """
    return key.startswith("#")


def find_encrypted_paths(obj: Any, prefix: str = "") -> list[str]:
    """Walk *obj* recursively and return dot-separated paths of all encrypted values.

    Both *encrypted marker values* and *secret keys* (starting with ``#``)
    are reported.

    Examples::

        >>> find_encrypted_paths({"#token": "KBC::ProjectSecure::abc"})
        ['#token']
        >>> find_encrypted_paths({"a": {"#key": "val"}})
        ['a.#key']
    """
    paths: list[str] = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            current = f"{prefix}.{key}" if prefix else key
            if is_secret_key(key) or is_encrypted_value(value):
                paths.append(current)
            else:
                paths.extend(find_encrypted_paths(value, prefix=current))
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            current = f"{prefix}[{idx}]"
            paths.extend(find_encrypted_paths(item, prefix=current))

    return paths
