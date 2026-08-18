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
import re
from typing import Any, Protocol

from ..errors import ConfigError, KeboolaApiError
from ..json_utils import set_nested_value
from ..models import ProjectConfig
from ._encryption import find_encrypted_secret_paths, find_unencryptable_secret_paths

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


_ROW_PATH = re.compile(r"^rows\[(\d+)\]\.(.+)$")


def split_row_secret_overrides(
    secrets: dict[str, str],
) -> tuple[dict[str, str], dict[int, dict[str, str]]]:
    """Split ``--secret`` paths into parent-body ones and per-row ones.

    Row ciphertext is *detected* and reported as ``rows[N].<path>`` so the
    operator knows which row to fix, which means a re-supplied value arrives
    under that same prefix. It has to be routed back to that row: applying it
    to the parent body would leave the row carrying the source project's
    undecryptable ciphertext while the command reported success.

    Returns ``(parent_overrides, {row_index: {path: value}})``.
    """
    parent: dict[str, str] = {}
    per_row: dict[int, dict[str, str]] = {}
    for path, value in secrets.items():
        match = _ROW_PATH.match(path)
        if match:
            per_row.setdefault(int(match.group(1)), {})[match.group(2)] = value
        else:
            parent[path] = value
    return parent, per_row


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
    project: ProjectConfig,
    encrypt_fn: _EncryptFn,
    allow_plaintext_fallback: bool,
) -> dict[str, Any]:
    """Duplicate within one project using the server-side copy endpoint.

    Nothing is rebuilt here, which is the entire value: whatever the source
    holds -- sibling keys, rows, ciphertexts -- lands in the copy untouched.
    ``--set`` edits are applied afterwards as a normal update on the new
    configuration, so an override can never be the reason a key goes missing.

    The patched body still goes through the encrypt-before-write step: a
    ``--set 'parameters.db.#password=...'`` is expected traffic here (this is
    how you repoint a copy at another database), and every other config write
    path in this CLI pre-encrypts ``#``-prefixed values (issue #378). Skipping
    it would put the credential in Storage -- and in version history -- in the
    clear.
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
        encrypted = encrypt_fn(
            client,
            project,
            component_id,
            patched,
            allow_plaintext_fallback=allow_plaintext_fallback,
        )
        client.update_config(
            component_id=component_id,
            config_id=new_id,
            configuration=encrypted if encrypted is not None else patched,
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
    row_secret_overrides: dict[int, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Assemble the configuration in the target project, then copy its rows.

    Rows are created one by one because there is no bulk endpoint. Each row
    body gets its own re-supplied secrets substituted and is then encrypted in
    the target project -- a row can carry its own ``#``-secrets, and skipping
    either step would leave the row unusable or write plaintext.
    """
    row_secret_overrides = row_secret_overrides or {}
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
    for index, row in enumerate(source_rows):
        source_row_body: dict[str, Any] = row.get("configuration") or {}
        if index in row_secret_overrides:
            source_row_body = _apply_secret_overrides(source_row_body, row_secret_overrides[index])
        row_body = encrypt_fn(
            target_client,
            target_project,
            component_id,
            source_row_body,
            allow_plaintext_fallback=allow_plaintext_fallback,
        )
        try:
            created_row = target_client.create_config_row(
                component_id=component_id,
                config_id=new_id,
                name=row.get("name") or "",
                configuration=row_body if row_body is not None else {},
                description=row.get("description") or "",
                is_disabled=bool(row.get("isDisabled")),
                branch_id=target_branch_id,
            )
        except KeboolaApiError as exc:
            # There is no bulk row endpoint and no rollback, so a mid-way
            # failure leaves a half-populated configuration in the target
            # project. Name it and say how far we got -- the caller cannot
            # retry or clean up something it cannot identify.
            raise KeboolaApiError(
                message=(
                    f"{exc.message}\n"
                    f"PARTIAL CLONE: configuration '{new_id}' was created in the target "
                    f"project with {len(copied_rows)} of {len(source_rows)} row(s) copied "
                    f"before row '{row.get('name') or row.get('id')}' failed. Delete it "
                    f"with `kbagent config delete --component-id {component_id} "
                    f"--config-id {new_id}` and re-run, or add the missing rows by hand."
                ),
                status_code=exc.status_code,
                error_code=exc.error_code,
                retryable=exc.retryable,
            ) from exc
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

    # The server-side copy writes into the branch it reads from, so it cannot
    # honour a different target branch. Refuse rather than write to the wrong
    # branch silently -- the caller asked for something this path cannot do.
    if not cross_project and target_branch_id is not None and target_branch_id != branch_id:
        raise ConfigError(
            f"--target-branch {target_branch_id} cannot be honoured for a clone within one "
            f"project: the Storage API copies into the source's own branch "
            f"({branch_id if branch_id is not None else 'production'}). Clone without "
            f"--target-branch, or clone into a different project."
        )

    # Same project omits an empty description so the copy endpoint inherits the
    # source's; the cross-project path assembles the body itself, so it has to
    # carry that inheritance over explicitly or the copy comes out blank.
    effective_description = description or (source.get("description") or "")

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

    # Ciphertext under a plain (non-``#``) key has no supported round-trip:
    # the encrypt step keys off ``#`` names, so a replacement supplied here
    # would be written to the target project in the clear. Refuse instead --
    # leaking a credential is a worse failure than not cloning.
    unencryptable = (
        find_unencryptable_secret_paths(source_body)
        + [
            f"rows[{index}].{path}"
            for index, row in enumerate(source_rows)
            for path in find_unencryptable_secret_paths(row.get("configuration") or {})
        ]
        if cross_project
        else []
    )
    if unencryptable and not dry_run:
        listed = "\n  - ".join(unencryptable)
        raise ConfigError(
            f"Cannot clone into project '{target_alias}': the source holds "
            f"{len(unencryptable)} encrypted value(s) under a plain (non-'#') key, which "
            f"this CLI cannot re-encrypt -- supplying one would write it to "
            f"'{target_alias}' in PLAINTEXT.\n"
            f"  - {listed}\n"
            f"Encrypt the replacement yourself for the target project with "
            f"`kbagent encrypt values --project {target_alias} --component-id {component_id}` "
            f"and pass the ciphertext via --set, or clone within the source project instead."
        )

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

    # Row secrets are reported (and therefore re-supplied) under a `rows[N].`
    # prefix, but they belong to the row body, not the parent -- keep them
    # apart so each half is applied to the document it actually describes.
    parent_secrets, row_secrets = split_row_secret_overrides(secret_overrides)

    planned_body = _apply_overrides(source_body, set_overrides)
    if cross_project:
        planned_body = _apply_secret_overrides(planned_body, parent_secrets)

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
            description=effective_description,
            target_branch_id=target_branch_id,
            encrypt_fn=encrypt_fn,
            allow_plaintext_fallback=allow_plaintext_fallback,
            row_secret_overrides=row_secrets,
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
            project=source_project,
            encrypt_fn=encrypt_fn,
            allow_plaintext_fallback=allow_plaintext_fallback,
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
