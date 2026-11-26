"""Plain env vars of a data app: the keys without ``#`` in ``parameters.dataApp.secrets``.

The block holds two kinds of keys. A key with ``#`` is an encrypted secret
(``data-app secrets-*``). A key without ``#`` is a plain env var, stored in
clear (``data-app env-*``). The runtime strips the ``#``, replaces ``-`` with
``_`` and uppercases the key, so ``#API_KEY`` and ``API_KEY`` become the same
env var. A write that would put both kinds under one env var name is refused.

Split out of ``data_app_service.py`` to keep that module under its file-size
budget. The functions take the ``DataAppService`` and use its config helpers.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..errors import ErrorCode, KeboolaApiError
from ._data_app_bodies import (
    DATA_APP_COMPONENT_ID,
    RESERVED_RUNTIME_ENV_VARS,
    _derive_runtime_env_var_name,
    _has_control_chars,
    _redact_storage_config,
)

if TYPE_CHECKING:
    from .data_app_service import DataAppService

# A plain env-var key: the shape of a secret key without the ``#``.
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

KIND_SECRET = "secret"
KIND_ENV = "env"

# Default and ceiling of ``data-app env-list --limit``.
DEFAULT_ENV_LIST_LIMIT = 100


@dataclass(frozen=True)
class KeyCollision:
    """A key to write whose runtime env var name is taken by a key of the other kind."""

    new_key: str
    existing_key: str
    env_var: str


@dataclass(frozen=True)
class LoadedConfig:
    """The ``keboola.data-apps`` Storage config of an app, read for a read-modify-write."""

    config_id: str
    version: str
    body: dict[str, Any]
    storage_client: Any


@contextmanager
def _open_config(
    service: DataAppService, *, alias: str, app_id: str, branch_id: int | None
) -> Iterator[LoadedConfig]:
    """Read the app's Storage config; close both clients on exit."""
    project = service.resolve_projects([alias])[alias]
    ds_client = service._ds_client_factory(project.stack_url, project.token)
    storage_client = service._client_factory(project.stack_url, project.token)
    try:
        config_id, envelope, body = service._load_data_app_storage_config(
            ds_client=ds_client,
            storage_client=storage_client,
            app_id=str(app_id),
            branch_id=branch_id,
        )
        yield LoadedConfig(
            config_id=config_id,
            version=str(envelope.get("version", "") or ""),
            body=body,
            storage_client=storage_client,
        )
    finally:
        ds_client.close()
        storage_client.close()


def key_kind(key: str) -> str:
    """``secret`` for a ``#`` key, ``env`` for a plain key."""
    return KIND_SECRET if key.startswith("#") else KIND_ENV


def secret_key_for(key: str) -> str:
    """The stored key of a secret: ``#`` added when the caller left it out."""
    return key if key.startswith("#") else f"#{key}"


def _invalid(message: str) -> KeboolaApiError:
    return KeboolaApiError(
        message=message, status_code=0, error_code=ErrorCode.INVALID_ARGUMENT, retryable=False
    )


def validate_env_key(key: str, secrets_command: str) -> None:
    """Refuse a ``#`` key (a secret) or a key that is not a valid env-var name."""
    if not isinstance(key, str):
        raise _invalid(f"Invalid env var key {key!r}: the key must be text.")
    if key.startswith("#"):
        raise _invalid(
            f"'{key}' is an encrypted secret key ('#' prefix). The env-* commands manage plain "
            f"env vars only. Use `kbagent data-app {secrets_command}` for secrets."
        )
    if not ENV_KEY_PATTERN.match(key) or _has_control_chars(key):
        raise _invalid(
            f"Invalid env var key '{key}'. The key must match [A-Za-z][A-Za-z0-9_-]{{0,63}} so "
            "the runtime env var name (uppercase, '-' to '_') is valid."
        )


def find_collisions(existing: dict[str, Any], new_keys: list[str]) -> list[KeyCollision]:
    """Keys in ``new_keys`` whose env var name an existing key of the other kind has."""
    collisions: list[KeyCollision] = []
    for new_key in new_keys:
        env_var = _derive_runtime_env_var_name(new_key)
        for existing_key in existing:
            if key_kind(existing_key) == key_kind(new_key):
                continue
            if _derive_runtime_env_var_name(existing_key) == env_var:
                collisions.append(KeyCollision(new_key, existing_key, env_var))
    return collisions


def refuse_collisions(
    collisions: list[KeyCollision], *, alias: str, app_id: str, writing: str
) -> None:
    """Raise ``INVALID_ARGUMENT`` naming the command that removes the old keys first.

    ``writing`` is the kind of the keys being written (``secret`` or ``env``).
    """
    if not collisions:
        return
    remove = "env-remove" if writing == KIND_SECRET else "secrets-remove"
    other = "plain env var" if writing == KIND_SECRET else "encrypted secret"
    pairs = ", ".join(
        f"'{c.new_key}' and the {other} '{c.existing_key}' (both are {c.env_var})"
        for c in collisions
    )
    keys = " ".join(f"--key '{c.existing_key}'" for c in collisions)
    raise KeboolaApiError(
        message=(
            f"Refused: env var name collision on data app {app_id}: {pairs}. Nothing was "
            f"written. Remove the {other} first: kbagent data-app {remove} --project {alias} "
            f"--app-id {app_id} {keys}"
        ),
        status_code=0,
        error_code=ErrorCode.INVALID_ARGUMENT,
        retryable=False,
        details={
            "collisions": [
                {"key": c.new_key, "existing_key": c.existing_key, "env_var": c.env_var}
                for c in collisions
            ]
        },
    )


def _entry(key: str, value: Any) -> dict[str, Any]:
    env_var = _derive_runtime_env_var_name(key)
    return {
        "key": key,
        "env_var": env_var,
        "value": value,
        "shadowed_by_runtime": env_var in RESERVED_RUNTIME_ENV_VARS,
    }


def _deploy_hint(alias: str, app_id: str) -> str:
    return f"kbagent data-app deploy --project {alias} --app-id {app_id} --wait"


def set_env(
    service: DataAppService,
    *,
    alias: str,
    app_id: str,
    env: dict[str, str],
    branch_id: int | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Write plain env vars into ``parameters.dataApp.secrets`` (read-modify-write)."""
    if not env:
        raise _invalid("At least one --var 'KEY=VALUE' is required.")
    for key, value in env.items():
        validate_env_key(key, "secrets-set")
        if not isinstance(value, str):
            raise _invalid(f"Env var '{key}' value must be text; got {type(value).__name__}.")
    with _open_config(service, alias=alias, app_id=app_id, branch_id=branch_id) as loaded:
        existing = service._read_secrets_block(loaded.body)
        refuse_collisions(
            find_collisions(existing, list(env)), alias=alias, app_id=app_id, writing=KIND_ENV
        )
        changed = sorted(k for k, v in env.items() if existing.get(k) != v)
        updated = {**existing, **env}
        result: dict[str, Any] = {
            "project_alias": alias,
            "app_id": str(app_id),
            "config_id": loaded.config_id,
            "env_set": sorted(_derive_runtime_env_var_name(k) for k in changed),
            "env_unchanged": sorted(
                _derive_runtime_env_var_name(k) for k in env if k not in changed
            ),
            "shadowed_by_runtime": sorted(
                _derive_runtime_env_var_name(k)
                for k in env
                if _derive_runtime_env_var_name(k) in RESERVED_RUNTIME_ENV_VARS
            ),
        }
        version = loaded.version
        if dry_run:
            body = service._merge_secrets_into_body(loaded.body, updated)
            return {
                **result,
                "dry_run": True,
                "put_storage_config_preview": _redact_storage_config({"configuration": body}),
                "message": f"Dry run -- would write {len(changed)} env var(s). No API call made.",
            }
        if not changed:
            return {
                **result,
                "status": "unchanged",
                "config_version_before": version,
                "config_version_after": version,
                "deploy_required": False,
                "message": f"No change: the {len(env)} env var(s) already have these values.",
            }
        response = loaded.storage_client.update_config(
            component_id=DATA_APP_COMPONENT_ID,
            config_id=loaded.config_id,
            configuration=service._merge_secrets_into_body(loaded.body, updated),
            change_description=f"Set {len(changed)} env var(s) via kbagent data-app env-set",
            branch_id=branch_id,
        )
        return {
            **result,
            "status": "updated",
            "config_version_before": version,
            "config_version_after": str(response.get("version", "") or ""),
            "deploy_required": True,
            "next_step": _deploy_hint(alias, app_id),
            "message": (
                f"{len(changed)} env var(s) written. The running container keeps the old "
                "config until you redeploy."
            ),
        }


def list_env(
    service: DataAppService,
    *,
    alias: str,
    app_id: str,
    branch_id: int | None,
    limit: int,
) -> dict[str, Any]:
    """List the plain env vars (keys without ``#``) with their values."""
    with _open_config(service, alias=alias, app_id=app_id, branch_id=branch_id) as loaded:
        existing = service._read_secrets_block(loaded.body)
    plain = sorted(k for k in existing if key_kind(k) == KIND_ENV)
    entries = [_entry(k, existing[k]) for k in plain[:limit]]
    return {
        "project_alias": alias,
        "app_id": str(app_id),
        "config_id": loaded.config_id,
        "env": entries,
        "count": len(entries),
        "limit": limit,
        "has_more": len(plain) > limit,
    }


def get_env(
    service: DataAppService,
    *,
    alias: str,
    app_id: str,
    key: str,
    branch_id: int | None,
) -> dict[str, Any]:
    """Return ONE plain env var with its value."""
    validate_env_key(key, "secrets-get")
    with _open_config(service, alias=alias, app_id=app_id, branch_id=branch_id) as loaded:
        existing = service._read_secrets_block(loaded.body)
    if key not in existing:
        raise KeboolaApiError(
            message=(
                f"Env var '{key}' not found on data app {app_id} in project '{alias}'. "
                f"List the env vars: kbagent data-app env-list --project {alias} --app-id {app_id}"
            ),
            status_code=404,
            error_code=ErrorCode.NOT_FOUND,
            retryable=False,
        )
    return {
        "project_alias": alias,
        "app_id": str(app_id),
        "config_id": loaded.config_id,
        **_entry(key, existing[key]),
    }


def remove_env(
    service: DataAppService,
    *,
    alias: str,
    app_id: str,
    keys: list[str],
    branch_id: int | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Remove plain env vars. Idempotent: a missing key is ``not_found``, exit 0."""
    if not keys:
        raise _invalid("At least one --key 'KEY' is required.")
    for key in keys:
        validate_env_key(key, "secrets-remove")
    with _open_config(service, alias=alias, app_id=app_id, branch_id=branch_id) as loaded:
        existing = service._read_secrets_block(loaded.body)
        removed = sorted({k for k in keys if k in existing})
        result: dict[str, Any] = {
            "project_alias": alias,
            "app_id": str(app_id),
            "config_id": loaded.config_id,
            "not_found": sorted({k for k in keys if k not in existing}),
        }
        version = loaded.version
        updated = {k: v for k, v in existing.items() if k not in removed}
        if dry_run:
            body = service._merge_secrets_into_body(loaded.body, updated)
            return {
                **result,
                "dry_run": True,
                "to_remove": removed,
                "put_storage_config_preview": _redact_storage_config({"configuration": body}),
                "message": f"Dry run -- would remove {len(removed)} env var(s). No API call made.",
            }
        if not removed:
            return {
                **result,
                "removed": [],
                "status": "unchanged",
                "config_version_before": version,
                "config_version_after": version,
                "deploy_required": False,
                "message": f"No matching env vars to remove on data app {app_id}.",
            }
        response = loaded.storage_client.update_config(
            component_id=DATA_APP_COMPONENT_ID,
            config_id=loaded.config_id,
            configuration=service._merge_secrets_into_body(loaded.body, updated),
            change_description=f"Remove {len(removed)} env var(s) via kbagent data-app env-remove",
            branch_id=branch_id,
        )
        return {
            **result,
            "removed": removed,
            "status": "removed",
            "config_version_before": version,
            "config_version_after": str(response.get("version", "") or ""),
            "deploy_required": True,
            "next_step": _deploy_hint(alias, app_id),
            "message": (
                f"{len(removed)} env var(s) removed. The running container keeps the old "
                "config until you redeploy."
            ),
        }
