"""Configuration cloning for ``kbagent config clone`` (issue #587).

Lives outside ``config_service.py`` because that module is already over its
per-layer size budget (``make loc-check``), and the clone flow is
self-contained: it takes clients and resolved projects, and returns a result
envelope. ``ConfigService.clone_config`` is a thin delegation.

Why a command exists at all
---------------------------
There was no way to duplicate a configuration, so people rebuilt the body from
``config detail`` output -- typically by copying ``configuration["parameters"]``
and nothing else. A configuration's root also carries ``storage``, ``runtime``
and ``authorization``, and dropping one is silent: a lost
``runtime.parallelism`` makes Keboola fall back to ``parallelism: 1``, which
turned a 65-row writer from 20-at-a-time into strictly sequential -- 140
minutes instead of the expected 60-90, with nothing reported anywhere.

Two paths, for one reason: encryption
-------------------------------------
Within a project the Storage API can copy server-side
(``POST .../versions/{v}/create``), which duplicates the stored configuration
exactly -- every sibling key, every row -- and ``KBC::`` ciphertexts remain
decryptable because the project is unchanged.

Across projects that is not available, and more importantly not sufficient: a
Keboola ciphertext is bound to the project it was encrypted for. Copying it
verbatim yields a configuration that looks complete and fails at runtime. So
the cross-project path assembles the configuration itself, and refuses to
write until every encrypted value has been re-supplied in plaintext, which it
then encrypts in the TARGET project.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from ..errors import ConfigError
from ..json_utils import set_nested_value
from ..models import ProjectConfig
from ._encryption import find_encrypted_secret_paths

logger = logging.getLogger(__name__)


class _EncryptFn(Protocol):
    """The encrypt-before-write callable supplied by ``ConfigService``."""

    def __call__(
        self,
        client: Any,
        project: ProjectConfig,
        component_id: str,
        configuration: dict[str, Any] | None,
        *,
        allow_plaintext_fallback: bool,
    ) -> dict[str, Any] | None: ...


def is_same_project(source: ProjectConfig, target: ProjectConfig) -> bool:
    """True when both aliases point at one and the same Keboola project.

    Identity short-circuits the no-``--target-project`` case. Otherwise both
    the stack and the project id must match, and an unknown (``None``) id
    never counts as a match: two aliases with no id recorded could be
    different projects, and guessing "same" would skip the encrypted-value
    check that protects the cross-project path.
    """
    if source is target:
        return True
    return (
        source.stack_url == target.stack_url
        and source.project_id is not None
        and source.project_id == target.project_id
    )


def _apply_overrides(configuration: dict[str, Any], overrides: dict[str, str]) -> dict[str, Any]:
    """Apply ``--set path=value`` edits, returning a new configuration.

    ``set_nested_value`` deep-copies, so the source body is never mutated --
    which matters because the same dict is also reported in the dry-run
    envelope.
    """
    result = configuration
    for path, value in overrides.items():
        result = set_nested_value(result, path, value)
    return result


def _apply_secret_overrides(
    configuration: dict[str, Any], secrets: dict[str, str]
) -> dict[str, Any]:
    """Substitute re-supplied plaintext at the paths that held ciphertext.

    Written back as plaintext on purpose: the caller encrypts the assembled
    body in the target project immediately afterwards, so this value never
    reaches the API unencrypted.
    """
    result = configuration
    for path, value in secrets.items():
        result = set_nested_value(result, path, value)
    return result


def _clone_same_project(
    *,
    client: Any,
    component_id: str,
    config_id: str,
    source: dict[str, Any],
    name: str,
    description: str,
    set_overrides: dict[str, str],
    branch_id: int | None,
) -> dict[str, Any]:
    """Duplicate within one project using the server-side copy endpoint.

    Nothing is rebuilt here, which is the entire value: whatever the source
    holds -- sibling keys, rows, ciphertexts -- lands in the copy untouched.
    ``--set`` edits are applied afterwards as a normal update on the new
    configuration, so an override can never be the reason a key goes missing.
    """
    created = client.create_config_copy(
        component_id=component_id,
        config_id=config_id,
        version=source["version"],
        name=name,
        description=description,
        branch_id=branch_id,
    )
    new_id = str(created["id"])

    if set_overrides:
        # Re-read rather than patching the source body we already hold: the
        # copy is what we are editing, and only the API can tell us what it
        # actually contains.
        clone_detail = client.get_config_detail(component_id, new_id, branch_id=branch_id)
        patched = _apply_overrides(clone_detail.get("configuration") or {}, set_overrides)
        client.update_config(
            component_id=component_id,
            config_id=new_id,
            configuration=patched,
            change_description=f"config clone: applied {len(set_overrides)} override(s)",
            branch_id=branch_id,
        )

    return created


def _clone_cross_project(
    *,
    source_rows: list[dict[str, Any]],
    target_client: Any,
    target_project: ProjectConfig,
    component_id: str,
    configuration: dict[str, Any],
    name: str,
    description: str,
    target_branch_id: int | None,
    encrypt_fn: _EncryptFn,
    allow_plaintext_fallback: bool,
) -> dict[str, Any]:
    """Assemble the configuration in the target project, then copy its rows.

    Rows are created one by one because there is no bulk endpoint. Each row
    body is encrypted in the target project too -- a row can carry its own
    ``#``-secrets, and skipping them would write plaintext.
    """
    encrypted_body = encrypt_fn(
        target_client,
        target_project,
        component_id,
        configuration,
        allow_plaintext_fallback=allow_plaintext_fallback,
    )
    created = target_client.create_config(
        component_id=component_id,
        name=name,
        configuration=encrypted_body if encrypted_body is not None else {},
        description=description,
        branch_id=target_branch_id,
    )
    new_id = str(created["id"])

    copied_rows: list[dict[str, str]] = []
    for row in source_rows:
        row_body = encrypt_fn(
            target_client,
            target_project,
            component_id,
            row.get("configuration") or {},
            allow_plaintext_fallback=allow_plaintext_fallback,
        )
        created_row = target_client.create_config_row(
            component_id=component_id,
            config_id=new_id,
            name=row.get("name") or "",
            configuration=row_body if row_body is not None else {},
            description=row.get("description") or "",
            is_disabled=bool(row.get("isDisabled")),
            branch_id=target_branch_id,
        )
        copied_rows.append({"source_row_id": str(row.get("id")), "id": str(created_row["id"])})

    created["copied_rows"] = copied_rows
    return created


def clone_config(
    *,
    source_client: Any,
    source_project: ProjectConfig,
    source_alias: str,
    target_client: Any,
    target_project: ProjectConfig,
    target_alias: str,
    component_id: str,
    config_id: str,
    name: str,
    description: str = "",
    set_overrides: dict[str, str] | None = None,
    secret_overrides: dict[str, str] | None = None,
    branch_id: int | None = None,
    target_branch_id: int | None = None,
    dry_run: bool = False,
    allow_plaintext_fallback: bool = False,
    encrypt_fn: _EncryptFn,
) -> dict[str, Any]:
    """Clone a configuration, within one project or into another.

    Raises:
        ConfigError: On a cross-project clone whose source carries encrypted
            values that were not re-supplied via ``secret_overrides``. This is
            deliberately fatal rather than a warning: the resulting
            configuration would look complete and fail at runtime, in a
            different project from the one the operator is watching.
    """
    set_overrides = set_overrides or {}
    secret_overrides = secret_overrides or {}

    source = source_client.get_config_detail(component_id, config_id, branch_id=branch_id)
    source_body: dict[str, Any] = source.get("configuration") or {}
    source_rows: list[dict[str, Any]] = source.get("rows") or []
    cross_project = not is_same_project(source_project, target_project)

    # Ciphertext is project-scoped, so it only blocks the cross-project path.
    encrypted_paths = find_encrypted_secret_paths(source_body) if cross_project else []
    row_encrypted_paths = (
        [
            f"rows[{index}].{path}"
            for index, row in enumerate(source_rows)
            for path in find_encrypted_secret_paths(row.get("configuration") or {})
        ]
        if cross_project
        else []
    )
    all_encrypted = encrypted_paths + row_encrypted_paths
    missing = [path for path in all_encrypted if path not in secret_overrides]

    if missing and not dry_run:
        listed = "\n  - ".join(missing)
        raise ConfigError(
            f"Cannot clone into project '{target_alias}': the source configuration "
            f"holds {len(missing)} encrypted value(s) that no other project can decrypt.\n"
            f"  - {listed}\n"
            f"Re-supply each one with --secret PATH=VALUE (they are encrypted in the target "
            f"project on write), or clone within the source project instead."
        )

    planned_body = _apply_overrides(source_body, set_overrides)
    if cross_project:
        planned_body = _apply_secret_overrides(planned_body, secret_overrides)

    if dry_run:
        return {
            "dry_run": True,
            "mode": "cross-project" if cross_project else "same-project",
            "source_project": source_alias,
            "target_project": target_alias,
            "component_id": component_id,
            "source_config_id": config_id,
            "source_version": source.get("version"),
            "name": name,
            "description": description,
            "configuration": planned_body,
            "row_count": len(source_rows),
            "encrypted_paths": all_encrypted,
            "missing_secrets": missing,
            "branch_id": target_branch_id if cross_project else branch_id,
        }

    if cross_project:
        created = _clone_cross_project(
            source_rows=source_rows,
            target_client=target_client,
            target_project=target_project,
            component_id=component_id,
            configuration=planned_body,
            name=name,
            description=description,
            target_branch_id=target_branch_id,
            encrypt_fn=encrypt_fn,
            allow_plaintext_fallback=allow_plaintext_fallback,
        )
    else:
        created = _clone_same_project(
            client=source_client,
            component_id=component_id,
            config_id=config_id,
            source=source,
            name=name,
            description=description,
            set_overrides=set_overrides,
            branch_id=branch_id,
        )

    created["mode"] = "cross-project" if cross_project else "same-project"
    created["source_project"] = source_alias
    created["target_project"] = target_alias
    created["component_id"] = component_id
    created["source_config_id"] = config_id
    created["source_version"] = source.get("version")
    created["encrypted_paths"] = all_encrypted
    created.setdefault("copied_rows", [])
    return created
