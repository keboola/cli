"""The ``sync clone`` composite orchestration (#426).

Extracted from ``sync_service.py``. ``SyncService.clone_project`` is a thin
delegator to :func:`clone_project` here; the pure, client-free override helpers
live in ``..sync.clone``. This function only orchestrates: validate the
reference, copy + re-point + parameterize the tree, run the fresh-target guard,
and push (so push Phase C/D remaps the flow/variable links).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ..errors import ConfigError
from ..sync.clone import (
    apply_bucket_map,
    apply_instance_rename,
    apply_variable_values,
    copy_reference_tree,
    repoint_manifest_project,
)
from ..sync.manifest import load_manifest, save_manifest

if TYPE_CHECKING:
    from .sync_service import SyncService


def clone_project(
    service: SyncService,
    source: str | Path,
    target_alias: str,
    target_dir: str | Path,
    *,
    overrides: dict[str, Any] | None = None,
    dry_run: bool = False,
    branch_override: int | None = None,
) -> dict[str, Any]:
    """Clone a reference synced project into a fresh target project (#426).

    Copies the reference tree at ``source`` into ``target_dir``, applies the
    declarative ``overrides`` (``bucket_map``, ``variable_values``,
    ``instance_rename``), re-points the manifest at ``target_alias``'s project,
    and pushes -- so every config is CREATEd fresh and its flow task / variable
    links are remapped reference->ULID by push Phase C/D.

    Cloning into a fresh target needs no id surgery: the reference's config ids
    do not exist in the target remote, so the diff classifies every config as
    ``added`` and ``created_id_map`` (keyed by the reference id) drives the link
    remaps.

    Idempotent: re-running with an existing ``target_dir`` skips the copy +
    overrides and just pushes, so a clean clone then reports ``no_changes`` /
    ``created: 0``.

    Args:
        service: The owning :class:`SyncService` (for ``resolve_projects`` /
            ``diff`` / ``push``).
        source: A reference synced project dir (must contain
            ``.keboola/manifest.json``).
        target_alias: The project alias to clone INTO (must be a fresh project on
            the first clone).
        target_dir: Where to materialise the clone.
        overrides: Optional dict with ``bucket_map`` (old->new bucket id),
            ``variable_values`` (var name->value), and ``instance_rename`` (old
            path prefix->new path prefix) keys.
        dry_run: Apply overrides + report the diff without pushing.
        branch_override: Optional target branch id.

    Returns:
        A dict matching ``CloneResult``: ``status``
        (``cloned`` | ``no_changes`` | ``dry_run``), ``target_alias``,
        ``target_dir``, ``created``, ``bucket_rewrites``, ``variable_overrides``,
        ``renamed_instances``, ``flow_task_remaps``, ``push`` (the underlying
        push result), ``errors``.

    Raises:
        ConfigError: ``source`` is not a synced project, ``target_alias`` is
            unknown, or (first clone) the target project already contains the
            reference's configs (not a fresh target).
    """
    overrides = overrides or {}
    bucket_map = overrides.get("bucket_map") or {}
    variable_values = overrides.get("variable_values") or {}
    instance_rename = overrides.get("instance_rename") or {}
    source_dir = Path(source)
    target_path = Path(target_dir)

    # Validate the reference is a synced project.
    try:
        load_manifest(source_dir)
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Source {source_dir} is not a synced project (no "
            ".keboola/manifest.json). Run `kbagent sync pull` there first."
        ) from exc

    target_project = service.resolve_projects([target_alias])[target_alias]

    bucket_rewrites = 0
    variable_overrides = 0
    renamed_instances = 0
    already_cloned = target_path.exists()

    if not already_cloned:
        copy_reference_tree(source_dir, target_path)
        manifest = load_manifest(target_path)
        repoint_manifest_project(
            manifest,
            project_id=target_project.project_id or 0,
            api_host=urlparse(target_project.stack_url).netloc,
        )
        bucket_rewrites = apply_bucket_map(target_path, manifest, bucket_map)
        variable_overrides = apply_variable_values(target_path, manifest, variable_values)
        renamed_instances = apply_instance_rename(target_path, manifest, instance_rename)
        save_manifest(target_path, manifest)

    override_counts = {
        "bucket_rewrites": bucket_rewrites,
        "variable_overrides": variable_overrides,
        "renamed_instances": renamed_instances,
    }

    if dry_run:
        diff_result = service.diff(target_alias, target_path, branch_override=branch_override)
        return {
            "status": "dry_run",
            "target_alias": target_alias,
            "target_dir": str(target_path),
            "summary": diff_result["summary"],
            **override_counts,
        }

    # Fresh-target guard: on a first clone every one of OUR configs must be a
    # CREATE. A non-'added' change means a reference id already exists in the
    # target -> not a fresh target; refuse rather than UPDATE a stranger's config.
    if not already_cloned:
        diff_result = service.diff(target_alias, target_path, branch_override=branch_override)
        collisions = [
            f"{c.get('component_id')}/{c.get('config_id')}"
            for c in diff_result["changes"]
            if c["change_type"] != "added"
        ]
        if collisions:
            raise ConfigError(
                "Clone requires a fresh target project, but these configs already "
                f"exist there: {', '.join(collisions[:10])}. Use a new/empty target "
                "project, or remove the existing configs first."
            )

    push_result = service.push(target_alias, target_path, branch_override=branch_override)
    status = "no_changes" if push_result.get("status") == "no_changes" else "cloned"
    return {
        "status": status,
        "target_alias": target_alias,
        "target_dir": str(target_path),
        "created": push_result.get("created", 0),
        "flow_task_remaps": push_result.get("flow_task_remaps", 0),
        "push": push_result,
        "errors": push_result.get("errors", []),
        **override_counts,
    }
