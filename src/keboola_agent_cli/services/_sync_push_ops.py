"""Per-change push CRUD operations (create/update/delete config + rows).

Extracted from sync_service.py. ``push()`` calls :func:`push_create`,
:func:`push_update`, and :func:`push_row_change`; the row dispatcher fans out to
the create/update/delete row helpers. Each reads a local ``_config.yml``,
encrypts ``#``-prefixed secrets (fail-closed), POSTs/PUTs/DELETEs, then writes
the API-assigned id + encrypted secrets back to disk and refreshes the manifest
hashes. Free functions taking the ``SyncService`` for the on-disk helpers it owns
(``_read_config_file`` / ``_file_hash`` / ``_resolve_source_branch_path``).
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..constants import CONFIG_FILENAME
from ..errors import ErrorCode, KeboolaApiError
from ..sync.code_extraction import merge_code_files
from ..sync.config_format import local_config_to_api, local_row_to_api
from ..sync.manifest import Manifest, ManifestConfiguration
from ._encryption import encrypt_secrets_in_config
from ._sync_baseline import apply_stamp, row_baseline
from ._sync_writeback import writeback_after_push, writeback_create_row_in_manifest

if TYPE_CHECKING:
    from .sync_service import SyncService

logger = logging.getLogger(__name__)


def push_row_change(
    service: SyncService,
    client: Any,
    *,
    change_type: str,
    component_id: str,
    parent_config_id: str,
    row_id: str,
    row_path_str: str,
    project_root: Path,
    manifest: Manifest,
    branch_id: int | None,
    allow_plaintext_fallback: bool = False,
    warnings: list[dict[str, str]] | None = None,
) -> str | None:
    """Dispatch a single row-level change (added/modified/deleted) to the API.

    ``#``-prefixed secrets in the row's configuration are encrypted via
    :func:`encrypt_secrets_in_config` before POST/PUT (same fail-closed semantics
    as parent configs). Mutates ``manifest`` in place; the caller is responsible
    for persisting it.

    ``parent_config_id`` must already be the *effective* parent id: on a fresh
    CREATE the caller remaps the diff-time placeholder to the API-assigned ULID
    before dispatch, so both the manifest parent lookup and
    ``create_config_row(config_id=...)`` hit the real config (KFR-05).

    ``warnings`` accumulates non-fatal baseline-stamping warnings (issue #686)
    for the push envelope; when the API state cannot be read back after the
    write, the row's ``pull_config_hash`` is left untouched rather than
    recomputed from disk.

    Returns the API-assigned row id on ``added`` (so the caller can map
    placeholder -> ULID for variable-link backfill), else ``None``.
    """
    parent = next(
        (
            c
            for c in manifest.configurations
            if c.component_id == component_id and c.id == parent_config_id
        ),
        None,
    )
    if parent is None and change_type != "deleted":
        raise KeboolaApiError(
            message=(
                f"Cannot push row {row_id}: parent config {component_id}/"
                f"{parent_config_id} is not tracked in the manifest."
            ),
            status_code=0,
            error_code=ErrorCode.PARENT_CONFIG_NOT_TRACKED,
        )

    project_id = manifest.project.id if manifest.project else None

    if change_type == "deleted":
        _push_delete_row(
            client,
            component_id=component_id,
            parent_config_id=parent_config_id,
            row_id=row_id,
            parent=parent,
            branch_id=branch_id,
        )
        return None

    # added / modified both read a local row file and encrypt-then-push.
    assert parent is not None  # guarded above for non-deleted change_types
    source_branch_path = service._resolve_source_branch_path(manifest, project_root, branch_id)
    row_dir = project_root / source_branch_path / parent.path / row_path_str

    if change_type == "added":
        return _push_create_row(
            service,
            client,
            component_id=component_id,
            parent_config_id=parent_config_id,
            row_dir=row_dir,
            parent=parent,
            row_path_str=row_path_str,
            branch_id=branch_id,
            project_id=project_id,
            allow_plaintext_fallback=allow_plaintext_fallback,
            warnings=warnings,
        )

    if change_type == "modified":
        push_update_row(
            service,
            client,
            component_id=component_id,
            parent_config_id=parent_config_id,
            row_id=row_id,
            row_dir=row_dir,
            parent=parent,
            branch_id=branch_id,
            project_id=project_id,
            allow_plaintext_fallback=allow_plaintext_fallback,
            warnings=warnings,
        )
        return None

    raise ValueError(f"Unsupported row change_type: {change_type}")


def _push_create_row(
    service: SyncService,
    client: Any,
    *,
    component_id: str,
    parent_config_id: str,
    row_dir: Path,
    parent: ManifestConfiguration,
    row_path_str: str,
    branch_id: int | None,
    project_id: int | None,
    allow_plaintext_fallback: bool,
    warnings: list[dict[str, str]] | None = None,
) -> str:
    """POST a new row; record API-assigned id + hashes in the parent's row list.

    Returns the API-assigned row id.
    """
    local_data = service._read_config_file(row_dir)
    if local_data is None:
        raise FileNotFoundError(f"Row file not found: {row_dir / CONFIG_FILENAME}")

    pristine_data = copy.deepcopy(local_data)
    name, description, configuration = local_row_to_api(local_data, component_id)
    configuration = encrypt_secrets_in_config(
        client,
        project_id,
        component_id,
        configuration,
        allow_plaintext_fallback=allow_plaintext_fallback,
    )

    result = client.create_config_row(
        component_id=component_id,
        config_id=parent_config_id,
        name=name,
        configuration=configuration,
        description=description,
        is_disabled=bool(local_data.get("is_disabled", False)),
        branch_id=branch_id,
    )
    new_row_id = str(result.get("id", ""))
    logger.info("Created row %s/%s/%s", component_id, parent_config_id, new_row_id)

    # Write-back: encrypted secrets land in the local file so a subsequent diff
    # sees local == remote. ``config_id=""`` tells the shared helper to skip
    # writing a config_id into ``_keboola`` (rows use ``row_id``).
    writeback_after_push(service, pristine_data, row_dir, "", configuration)

    row_file = row_dir / CONFIG_FILENAME
    new_file_hash = service._file_hash(row_file) if row_file.exists() else ""
    stamp = row_baseline(
        client,
        component_id=component_id,
        config_id=parent_config_id,
        row_id=new_row_id,
        branch_id=branch_id,
        response=result,
    )
    row_entry = writeback_create_row_in_manifest(
        parent=parent,
        row_path_str=row_path_str,
        new_row_id=new_row_id,
        file_hash=new_file_hash,
        cfg_hash=stamp.cfg_hash,
    )
    apply_stamp(row_entry.metadata, stamp)
    if stamp.warning is not None and warnings is not None:
        warnings.append(stamp.warning)
    return new_row_id


def push_update_row(
    service: SyncService,
    client: Any,
    *,
    component_id: str,
    parent_config_id: str,
    row_id: str,
    row_dir: Path,
    parent: ManifestConfiguration,
    branch_id: int | None,
    project_id: int | None,
    allow_plaintext_fallback: bool,
    warnings: list[dict[str, str]] | None = None,
) -> None:
    """PUT an existing row; refresh its hashes in the parent's row list.

    The baseline comes from the API's own view of the row (issue #686), so a
    row disabled remotely whose local file carries no ``is_disabled`` key does
    not leave a permanent phantom diff behind.
    """
    local_data = service._read_config_file(row_dir)
    if local_data is None:
        raise FileNotFoundError(f"Row file not found: {row_dir / CONFIG_FILENAME}")

    pristine_data = copy.deepcopy(local_data)
    name, description, configuration = local_row_to_api(local_data, component_id)
    configuration = encrypt_secrets_in_config(
        client,
        project_id,
        component_id,
        configuration,
        allow_plaintext_fallback=allow_plaintext_fallback,
    )

    result = client.update_config_row(
        component_id=component_id,
        config_id=parent_config_id,
        row_id=row_id,
        name=name,
        configuration=configuration,
        description=description,
        change_description="Updated via kbagent sync push",
        # Explicit key in the local YAML pushes the state; an absent key
        # leaves the remote enabled/disabled state untouched (issue #467).
        is_disabled=(bool(local_data["is_disabled"]) if "is_disabled" in local_data else None),
        branch_id=branch_id,
    )
    logger.info("Updated row %s/%s/%s", component_id, parent_config_id, row_id)

    writeback_after_push(service, pristine_data, row_dir, "", configuration)

    row_file = row_dir / CONFIG_FILENAME
    new_file_hash = service._file_hash(row_file) if row_file.exists() else ""
    stamp = row_baseline(
        client,
        component_id=component_id,
        config_id=parent_config_id,
        row_id=row_id,
        branch_id=branch_id,
        response=result,
    )
    if stamp.warning is not None and warnings is not None:
        warnings.append(stamp.warning)
    for r in parent.rows:
        if r.id == row_id:
            r.metadata["pull_hash"] = new_file_hash
            apply_stamp(r.metadata, stamp)
            break


def _push_delete_row(
    client: Any,
    *,
    component_id: str,
    parent_config_id: str,
    row_id: str,
    parent: ManifestConfiguration | None,
    branch_id: int | None,
) -> None:
    """DELETE a row; prune it from the parent's row list in the manifest."""
    client.delete_config_row(
        component_id=component_id,
        config_id=parent_config_id,
        row_id=row_id,
        branch_id=branch_id,
    )
    if parent is not None:
        parent.rows = [r for r in parent.rows if r.id != row_id]
    logger.info("Deleted row %s/%s/%s", component_id, parent_config_id, row_id)


def push_create(
    service: SyncService,
    client: Any,
    component_id: str,
    config_path_str: str,
    project_root: Path,
    manifest: Manifest,
    branch_id: int | None,
    *,
    allow_plaintext_fallback: bool = False,
) -> dict[str, Any] | None:
    """Create a new config from a local _config.yml file."""
    branch_path = service._resolve_source_branch_path(manifest, project_root, branch_id)
    config_dir = project_root / branch_path / config_path_str
    local_data = service._read_config_file(config_dir)
    if local_data is None:
        return None

    # Preserve pristine data for writeback (merge_code_files mutates local_data
    # by injecting parameters.blocks which should not end up in _config.yml).
    pristine_data = copy.deepcopy(local_data)

    # Merge code files (transform.sql, transform.py, code.py) back into config
    merge_code_files(component_id, local_data, config_dir)

    name, description, configuration = local_config_to_api(local_data)

    # Encrypt #-prefixed secrets before sending to API
    project_id = manifest.project.id if manifest.project else None
    configuration = encrypt_secrets_in_config(
        client,
        project_id,
        component_id,
        configuration,
        allow_plaintext_fallback=allow_plaintext_fallback,
    )

    result = client.create_config(
        component_id=component_id,
        name=name,
        configuration=configuration,
        description=description,
        branch_id=branch_id,
        is_disabled=bool(local_data.get("is_disabled", False)),
    )
    new_config_id = result.get("id", "")
    logger.info("Created config %s/%s (ID: %s)", component_id, name, new_config_id)

    # Write back: update local file with config_id + encrypted secrets. Use
    # pristine_data so blocks/code stay only in their code files.
    writeback_after_push(service, pristine_data, config_dir, new_config_id, configuration)

    return result


def push_update(
    service: SyncService,
    client: Any,
    component_id: str,
    config_id: str,
    config_path_str: str,
    project_root: Path,
    manifest: Manifest,
    branch_id: int | None,
    *,
    allow_plaintext_fallback: bool = False,
) -> dict[str, Any]:
    """Update an existing config from a local _config.yml file.

    Returns the API response so the caller can stamp the manifest baseline
    from the remote's own view of the config (issue #686).
    """
    branch_path = service._resolve_source_branch_path(manifest, project_root, branch_id)
    config_dir = project_root / branch_path / config_path_str
    local_data = service._read_config_file(config_dir)
    if local_data is None:
        raise FileNotFoundError(f"Config file not found: {config_dir / CONFIG_FILENAME}")

    # Preserve pristine data for writeback (merge_code_files mutates local_data
    # by injecting parameters.blocks which should not end up in _config.yml).
    pristine_data = copy.deepcopy(local_data)

    # Merge code files (transform.sql, transform.py, code.py) back into config
    merge_code_files(component_id, local_data, config_dir)

    name, description, configuration = local_config_to_api(local_data)

    # Encrypt #-prefixed secrets before sending to API
    project_id = manifest.project.id if manifest.project else None
    configuration = encrypt_secrets_in_config(
        client,
        project_id,
        component_id,
        configuration,
        allow_plaintext_fallback=allow_plaintext_fallback,
    )

    result = client.update_config(
        component_id=component_id,
        config_id=config_id,
        name=name,
        configuration=configuration,
        description=description,
        change_description="Updated via kbagent sync push",
        # Explicit key in the local YAML pushes the state; an absent key
        # leaves the remote enabled/disabled state untouched (issue #467).
        is_disabled=(bool(local_data["is_disabled"]) if "is_disabled" in local_data else None),
        branch_id=branch_id,
    )
    logger.info("Updated config %s/%s", component_id, config_id)

    # Write back: update local file with encrypted secrets. Use pristine_data so
    # blocks/code stay only in their code files.
    writeback_after_push(service, pristine_data, config_dir, config_id, configuration)

    return result if isinstance(result, dict) else {}
