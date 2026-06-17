"""Manifest + local-file writeback after a push (extracted from sync_service.py).

Free functions that record a freshly-created config/row in the manifest, write
the API-assigned ULID + encrypted secrets back into the local ``_config.yml``,
and propagate ``KBC.*`` metadata. Only :func:`writeback_after_push` needs the
``SyncService`` (for its ``_write_config_file`` helper); the rest are pure.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import KeboolaApiError
from ..sync.manifest import ManifestConfigRow, ManifestConfiguration
from ._encryption import apply_encrypted_to_local
from ._sync_models import WritebackResult

if TYPE_CHECKING:
    from ..sync.manifest import Manifest
    from .sync_service import SyncService

logger = logging.getLogger(__name__)


def writeback_create_config_in_manifest(
    *,
    manifest: Manifest,
    component_id: str,
    branch_id: int | None,
    config_path_str: str,
    new_id: str,
    file_hash: str,
    cfg_hash: str,
) -> WritebackResult:
    """Record a freshly-created config in the manifest.

    If a placeholder entry already exists at ``(branch_id, component_id, path)``
    -- the FIIA / scaffold emit pattern -- update it in place, preserving any
    user-declared metadata (e.g. ``KBC.configuration.folderName``) and
    refreshing only the bookkeeping hashes. Otherwise append a new entry.

    Matching includes ``branch_id`` because a single manifest can hold entries
    from multiple branches in git-branching mode; matching on
    ``(component_id, path)`` alone would risk updating the wrong branch's entry
    when the same logical path exists under two branches.

    Returns a :class:`WritebackResult` carrying the entry and its pre-overwrite
    ``previous_id`` so the create pass can remap any child row parents /
    transformation variable links from the placeholder id to the freshly-assigned
    ULID.
    """
    target_branch = branch_id or 0
    for entry in manifest.configurations:
        if (
            entry.branch_id == target_branch
            and entry.component_id == component_id
            and entry.path == config_path_str
        ):
            previous_id = entry.id
            entry.id = new_id
            entry.metadata["pull_hash"] = file_hash
            entry.metadata["pull_config_hash"] = cfg_hash
            return WritebackResult(entry=entry, previous_id=previous_id)
    new_entry = ManifestConfiguration(
        branchId=target_branch,
        componentId=component_id,
        id=new_id,
        path=config_path_str,
        metadata={"pull_hash": file_hash, "pull_config_hash": cfg_hash},
    )
    manifest.configurations.append(new_entry)
    return WritebackResult(entry=new_entry, previous_id="")


def writeback_create_row_in_manifest(
    *,
    parent: ManifestConfiguration,
    row_path_str: str,
    new_row_id: str,
    file_hash: str,
    cfg_hash: str,
) -> ManifestConfigRow:
    """Record a freshly-created row under its parent in the manifest.

    Mirrors :func:`writeback_create_config_in_manifest` for rows: update any
    placeholder row entry in place, otherwise append.
    """
    for row in parent.rows:
        if row.path == row_path_str:
            row.id = new_row_id
            row.metadata["pull_hash"] = file_hash
            row.metadata["pull_config_hash"] = cfg_hash
            return row
    new_row = ManifestConfigRow(
        id=new_row_id,
        path=row_path_str,
        metadata={"pull_hash": file_hash, "pull_config_hash": cfg_hash},
    )
    parent.rows.append(new_row)
    return new_row


def propagate_kbc_metadata(
    client: Any,
    entry: ManifestConfiguration,
    branch_id: int | None,
) -> str | None:
    """POST any ``KBC.*`` keys from the manifest entry to the metadata API.

    Bookkeeping keys (``pull_hash``, ``pull_config_hash``, ...) live in the same
    metadata dict but are filtered by the ``KBC.`` prefix. Called only on CREATE;
    updates use ``kbagent config set-metadata`` explicitly. The metadata API
    stores configuration-level annotations only -- this is **not** a secret
    store; do not place tokens or passwords under ``KBC.*`` keys.

    Returns ``None`` on success (or when there are no KBC.* keys to propagate).
    Returns the API error message on a non-fatal write failure: the config is
    already created on the remote and the manifest writeback is complete, so a
    single failed metadata POST is reported back to the push loop as an
    accumulated error rather than aborting the rest of the push.
    """
    entries = [(key, str(value)) for key, value in entry.metadata.items() if key.startswith("KBC.")]
    if not entries:
        return None
    try:
        client.set_config_metadata(
            component_id=entry.component_id,
            config_id=entry.id,
            entries=entries,
            branch_id=branch_id,
        )
    except KeboolaApiError as exc:
        logger.warning(
            "Failed to propagate KBC.* metadata for %s/%s: %s",
            entry.component_id,
            entry.id,
            exc,
        )
        return exc.message
    return None


def writeback_after_push(
    service: SyncService,
    local_data: dict[str, Any],
    config_dir: Path,
    config_id: str,
    pushed_configuration: dict[str, Any],
) -> None:
    """Update local ``_config.yml`` after a successful push.

    Writes back the API-assigned ``_keboola.config_id`` (on first create) and
    the encrypted secret values (so local matches remote state).
    """
    keboola_meta = local_data.setdefault("_keboola", {})
    if config_id:
        keboola_meta["config_id"] = config_id

    pushed_params = pushed_configuration.get("parameters", {})
    local_params = local_data.get("parameters", {})
    if pushed_params and local_params:
        apply_encrypted_to_local(local_params, pushed_params)

    service._write_config_file(config_dir, local_data)
    logger.debug("Updated local config at %s after push", config_dir)
