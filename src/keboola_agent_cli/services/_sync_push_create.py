"""Bookkeeping of the configs one ``sync push`` creates (extracted from sync_service.py).

After ``push_create`` POSTed a config, :func:`record_created_config` records it
in the manifest, keeps the id maps the row pass and the link backfill
(Phase C/D) need, and saves the manifest at once (D17).

The checkpoint matters most on the promote path (``push --branch DEV`` with no
``dev/`` tree, so ``main/`` is the source): the source files keep their own
ids there (D19), so the manifest entry is the only local record that a config
was created on the dev branch. A crash between that create and the final
manifest save used to leave nothing behind, and the retry created the config
again. A lost response (the server created the config, the client never saw
the id) is still not recoverable: no server-side idempotency is used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..sync.branch_scope import PromotedFrom, branch_tree_path
from ..sync.manifest import Manifest, save_manifest
from ._sync_models import CreatedConfig
from ._sync_writeback import link_promote_entries, propagate_kbc_metadata, stamp_created_config

if TYPE_CHECKING:
    from .sync_service import SyncService


@dataclass
class CreateLedger:
    """What one push created, for the row pass and the link backfill.

    Attributes:
        created_id_map: ``(component_id, placeholder id)`` -> new id, captured
            before the manifest writeback overwrites a placeholder entry.
        promoted_id_map: ``(component_id, source-branch id)`` -> new id of a
            tracked config created on a branch that lacks it (promote path).
            Its rows still name the source id, and so do the flows and
            transformations promoted with it (D16).
        created_configs: The created configs, in creation order.
    """

    created_id_map: dict[tuple[str, str], str] = field(default_factory=dict)
    promoted_id_map: dict[tuple[str, str], str] = field(default_factory=dict)
    created_configs: list[CreatedConfig] = field(default_factory=list)

    def parent_id(self, component_id: str, parent_config_id: str) -> str:
        """The id a row change's parent has on the target after this push's creates."""
        key = (component_id, parent_config_id)
        return self.created_id_map.get(key, self.promoted_id_map.get(key, parent_config_id))


def record_created_config(
    service: SyncService,
    client: Any,
    ledger: CreateLedger,
    *,
    manifest: Manifest,
    project_root: Path,
    branch_path: str,
    branch_id: int | None,
    component_id: str,
    source_config_id: str,
    config_path_str: str,
    result: dict[str, Any],
    warnings: list[dict[str, Any]],
    errors: list[dict[str, str]],
) -> str:
    """Record one created config in the manifest and the ledger; return its new id.

    *source_config_id* is the id the diff change carried: the source-tree
    config a promote push created on the target, or ``""`` for a new config.
    On the promote path the new entry records ``promoted_from`` (D13) and the
    link backfill may rewrite only the remote payload, never the source files.
    """
    new_id = str(result.get("id", ""))
    promote = branch_path != branch_tree_path(manifest, branch_id)
    config_dir = project_root / branch_path / config_path_str
    writeback = stamp_created_config(
        client,
        manifest=manifest,
        component_id=component_id,
        branch_id=branch_id,
        config_path_str=config_path_str,
        new_id=new_id,
        hashes=service._compute_config_hashes(config_dir, component_id),
        response=result,
        warnings=warnings,
        promoted=PromotedFrom(branch_path, source_config_id) if promote else None,
    )
    if writeback.previous_id:
        ledger.created_id_map[(component_id, writeback.previous_id)] = new_id
    elif source_config_id:
        ledger.promoted_id_map[(component_id, source_config_id)] = new_id
    if not promote:
        link_promote_entries(
            manifest,
            tree=branch_path,
            component_id=component_id,
            path=config_path_str,
            config_id=new_id,
        )
    ledger.created_configs.append(
        CreatedConfig(
            component_id=component_id,
            config_id=new_id,
            config_dir=config_dir,
            write_local=not promote,
        )
    )
    metadata_error = propagate_kbc_metadata(client, writeback.entry, branch_id)
    if metadata_error is not None:
        # The config IS on the remote; only the follow-up metadata POST
        # failed. Accumulate like any other per-change error so the rest of
        # the push continues, and surface the original cause in the envelope.
        errors.append(
            {
                "change_type": "metadata_propagation",
                "component_id": component_id,
                "config_id": new_id,
                "message": metadata_error,
            }
        )
    save_manifest(project_root, manifest)  # checkpoint (D17)
    return new_id
