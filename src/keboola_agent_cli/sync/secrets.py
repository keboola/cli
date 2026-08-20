"""Encrypted value detection for Keboola configurations.

Keboola stores secrets as encrypted markers (e.g.
``KBC::ProjectSecure::...``).  This module provides helpers to detect
such values and locate them inside arbitrarily nested configuration dicts.

Detection is deliberately *inclusive*: this is a detector, not an
authorization gate.  A marker that goes unrecognised is read as plaintext
and stops being redacted from diffs, so under-inclusion is the bug and
over-inclusion is harmless.  (The fail-closed whitelist that decides which
ciphertext kbagent will *write* is a separate, deliberately narrow one --
``ENCRYPTED_PASSWORD_PREFIXES`` in ``services/data_app_service.py``.)
"""

from __future__ import annotations

from typing import Any

# Source of truth for the cipher families: the platform's own registry,
# keboola/keboola-operator ``internal/encryptor/wrapper/registry.go`` (mirrored
# by the wrappers in keboola/object-encryptor), plus
# https://developers.keboola.com/overview/encryption/.
#
# Every scope exists once per cloud. The AWS/KMS form carries no suffix, Azure
# Key Vault appends ``KV`` and Google KMS appends ``GKMS`` -- so one scope is
# three prefixes: ``KBC::ProjectSecure::``, ``KBC::ProjectSecureKV::``,
# ``KBC::ProjectSecureGKMS::``. Listing only the AWS forms (as this module did
# until 0.86.0) made every GCP/Azure ciphertext read as plaintext here -- see
# issue #612, and #607 for the same defect in the data-app write path.
_CIPHER_SCOPES: tuple[str, ...] = (
    "Secure",
    "ComponentSecure",
    "ConfigSecure",
    "ProjectSecure",
    "ProjectWideSecure",
    "BranchTypeSecure",
    "BranchTypeConfigSecure",
    "ProjectWideBranchTypeSecure",
)

_CIPHER_CLOUD_SUFFIXES: tuple[str, ...] = ("", "KV", "GKMS")

# Pre-2019 ciphers. Note the ``==`` terminator -- these are not ``::``-delimited
# and have no per-cloud variants.
_LEGACY_CIPHER_PREFIXES: tuple[str, ...] = (
    "KBC::Encrypted==",
    "KBC::ComponentEncrypted==",
    "KBC::ComponentProjectEncrypted==",
)

ENCRYPTED_PREFIXES: tuple[str, ...] = (
    tuple(
        f"KBC::{scope}{suffix}::" for scope in _CIPHER_SCOPES for suffix in _CIPHER_CLOUD_SUFFIXES
    )
    + _LEGACY_CIPHER_PREFIXES
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
