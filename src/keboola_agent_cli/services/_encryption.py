"""Shared helpers for encrypting ``#``-prefixed configuration secrets.

Used by both :mod:`sync_service` (push-time encryption of row/parent configs)
and :mod:`variables_service` (encryption of values set via
``config variables-set``). Keeping the helpers here instead of on either
service keeps the encrypt-before-write contract in a single module and
avoids one service importing another's internals.
"""

from __future__ import annotations

import logging
from typing import Any

from ..errors import KeboolaApiError

logger = logging.getLogger(__name__)

_ENCRYPTED_PREFIX = "KBC::"


def is_secret_key(key: str) -> bool:
    """True if ``key`` is a Keboola secret key (starts with ``#``)."""
    return isinstance(key, str) and key.startswith("#")


def is_already_encrypted(value: Any) -> bool:
    """True if ``value`` is already encrypted (``KBC::`` prefix)."""
    return isinstance(value, str) and value.startswith(_ENCRYPTED_PREFIX)


def _is_secret_name_value_pair(item: Any) -> bool:
    """True if ``item`` is a row-hoisted secret entry like ``{"name": "#x", "value": "..."}``.

    ``keboola.variables`` rows store each variable as ``{name, value}`` inside
    a ``values`` list, so a ``#secret`` lives in the ``name`` field rather
    than as a dict key. This helper lets the walkers recognize that shape so
    the encryption contract holds for row-hoisted components without every
    caller having to pre-flatten. (``keboola.shared-code`` also hoists rows
    but its payload is ``code_content`` -- no ``name``/``value`` pairs --
    so this check never fires there, which is correct.)
    """
    return (
        isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and is_secret_key(item["name"])
        and isinstance(item.get("value"), str)
    )


def collect_secrets(obj: Any, path_prefix: str, result: dict[str, str]) -> None:
    """Recursively collect unencrypted ``#``-prefixed secret values.

    Builds a flat dict of ``{#path.key: plaintext_value}`` suitable for the
    Encryption API. The Encryption API requires every key to start with
    ``#``, so a nested key ``parameters.#api_token`` is flattened into
    ``#parameters.#api_token`` with the outer ``#`` added.

    Also recognizes the row-hoisted ``{"name": "#x", "value": "..."}`` list
    element shape used by ``keboola.variables``; see
    :func:`_is_secret_name_value_pair`.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            if is_secret_key(key) and isinstance(value, str) and not is_already_encrypted(value):
                encrypt_key = f"#{path_prefix}{key}" if path_prefix else key
                result[encrypt_key] = value
            elif isinstance(value, (dict, list)):
                child_prefix = f"{path_prefix}{key}." if path_prefix else f"{key}."
                collect_secrets(value, child_prefix, result)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if _is_secret_name_value_pair(item) and not is_already_encrypted(item["value"]):
                encrypt_key = f"#{path_prefix}[{i}].{item['name']}"
                result[encrypt_key] = item["value"]
            else:
                collect_secrets(item, f"{path_prefix}[{i}].", result)


def apply_encrypted(obj: Any, path_prefix: str, encrypted: dict[str, str]) -> None:
    """Recursively apply encrypted values back into ``obj`` by matching flattened keys."""
    if isinstance(obj, dict):
        for key in list(obj.keys()):
            value = obj[key]
            if is_secret_key(key) and isinstance(value, str) and not is_already_encrypted(value):
                encrypt_key = f"#{path_prefix}{key}" if path_prefix else key
                if encrypt_key in encrypted:
                    obj[key] = encrypted[encrypt_key]
            elif isinstance(value, (dict, list)):
                child_prefix = f"{path_prefix}{key}." if path_prefix else f"{key}."
                apply_encrypted(value, child_prefix, encrypted)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if _is_secret_name_value_pair(item) and not is_already_encrypted(item["value"]):
                encrypt_key = f"#{path_prefix}[{i}].{item['name']}"
                if encrypt_key in encrypted:
                    item["value"] = encrypted[encrypt_key]
            else:
                apply_encrypted(item, f"{path_prefix}[{i}].", encrypted)


def apply_encrypted_to_local(local: Any, pushed: Any) -> None:
    """Copy encrypted secret values from ``pushed`` config back into ``local``.

    Walks both dicts in parallel. Where ``pushed`` has an encrypted value for a
    ``#``-key, replaces the ``local`` plaintext with it so the on-disk file
    matches server state after a successful push. Also handles the row-hoisted
    ``{"name": "#x", "value": "..."}`` list element shape.
    """
    if isinstance(local, dict) and isinstance(pushed, dict):
        for key in local:
            if key not in pushed:
                continue
            if is_secret_key(key) and is_already_encrypted(pushed[key]):
                local[key] = pushed[key]
            elif isinstance(local[key], dict) and isinstance(pushed[key], dict):
                apply_encrypted_to_local(local[key], pushed[key])
            elif isinstance(local[key], list) and isinstance(pushed[key], list):
                for i in range(min(len(local[key]), len(pushed[key]))):
                    lo = local[key][i]
                    pu = pushed[key][i]
                    if (
                        _is_secret_name_value_pair(lo)
                        and isinstance(pu, dict)
                        and pu.get("name") == lo["name"]
                        and is_already_encrypted(pu.get("value"))
                    ):
                        lo["value"] = pu["value"]
                    else:
                        apply_encrypted_to_local(lo, pu)


def encrypt_secrets_in_config(
    client: Any,
    project_id: int | None,
    component_id: str,
    configuration: dict[str, Any],
    *,
    allow_plaintext_fallback: bool = False,
) -> dict[str, Any]:
    """Encrypt ``#``-prefixed secret values in ``configuration`` in place.

    Walks ``configuration`` recursively via :func:`collect_secrets`, sends the
    flat secret map to the Encryption API (``client.encrypt_values``), and
    writes the ciphertext back into the original dict via
    :func:`apply_encrypted`.

    Fail-closed by default: any encryption exception raises
    :class:`KeboolaApiError` with code ``ENCRYPTION_FAILED``. Set
    ``allow_plaintext_fallback=True`` to log-and-continue instead (for
    bootstrap/debug scenarios only).

    Args:
        client: HTTP client exposing ``encrypt_values(project_id, component_id, data)``.
        project_id: Keboola project ID. When falsy, encryption is skipped entirely
            (caller's responsibility to decide when that's safe).
        component_id: Component ID for the Encryption API scope.
        configuration: Config dict to encrypt in place (returned for chaining).
        allow_plaintext_fallback: When ``False`` (default), raise on encryption
            failure. When ``True``, log a warning and return the config with
            plaintext intact.
    """
    if not project_id:
        return configuration

    secrets: dict[str, str] = {}
    collect_secrets(configuration, "", secrets)
    if not secrets:
        return configuration

    try:
        encrypted = client.encrypt_values(
            project_id=project_id,
            component_id=component_id,
            data=secrets,
        )
        apply_encrypted(configuration, "", encrypted)
        logger.info("Encrypted %d secret value(s) for %s", len(encrypted), component_id)
    except Exception as exc:
        if allow_plaintext_fallback:
            logger.warning(
                "Failed to encrypt secrets for %s: %s (plaintext fallback allowed)",
                component_id,
                exc,
            )
        else:
            raise KeboolaApiError(
                message=(
                    f"Encryption failed for {component_id}: {exc}. "
                    f"Refusing to push plaintext secrets. "
                    f"Use --allow-plaintext-on-encrypt-failure to override."
                ),
                status_code=0,
                error_code="ENCRYPTION_FAILED",
            ) from exc

    return configuration
