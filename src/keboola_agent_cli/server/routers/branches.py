"""Branch lifecycle and metadata endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ...services.base import resolve_fanout_aliases
from ...services.branch_service import active_branch_not_stored_warning
from ..dependencies import ALL_PROJECTS_HELP, ServiceRegistry, get_registry
from ..response_meta import add_warning, warn_active_choice_shared

router = APIRouter(prefix="/branches", tags=["branches"])


class BranchCreate(BaseModel):
    name: str
    description: str = ""


class BranchUse(BaseModel):
    branch_id: int


class MetadataSet(BaseModel):
    value: str


@router.get("", summary="List branches")
def list_branches(
    project: list[str] | None = Query(None),
    all_projects: bool = Query(False, description=ALL_PROJECTS_HELP),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """List development branches across one or more projects. Mirrors `kbagent branch list`."""
    aliases = resolve_fanout_aliases(registry.config_store, project, all_projects)
    return registry.branch.list_branches(aliases=aliases)


@router.post("/{project}", summary="Create a branch")
def create(
    project: str, body: BranchCreate, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Create a new development branch. Mirrors `kbagent branch create`.

    On a project from environment variables the branch is created but not
    activated (``activated: false``) and a ``Kbagent-Warnings`` entry
    ``ACTIVE_BRANCH_NOT_STORED`` (hint ``branch_id=<id>``) says to pass ``branch_id``.
    """
    result = registry.branch.create_branch(
        alias=project, name=body.name, description=body.description
    )
    if not result["activated"]:
        add_warning(
            **active_branch_not_stored_warning(
                result["project_alias"], result["branch_id"], rest=True
            )
        )
    return result


@router.post(
    "/{project}/use",
    summary="Set the active branch (shared with every CLI call and REST client)",
)
def use(
    project: str, body: BranchUse, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Set the active branch for the project. Mirrors `kbagent branch use`.

    The choice is saved in the configuration directory, so every CLI call and
    every REST client that uses the directory uses it too, and every route
    without ``branch_id`` on this project then uses it (spec V2, V8). The
    response carries ``active_branch_set`` and a ``Kbagent-Warnings`` entry
    ``ACTIVE_CHOICE_SHARED`` that names the directory.
    """
    result = registry.branch.set_active_branch(alias=project, branch_id=body.branch_id)
    warn_active_choice_shared(registry.config_store.config_dir, "active branch")
    return {**result, "active_branch_set": body.branch_id}


@router.post(
    "/{project}/reset",
    summary="Clear the active branch (shared with every CLI call and REST client)",
)
def reset(project: str, registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    """Clear the active branch, so commands use production. Mirrors `kbagent branch reset`.

    Shared like ``POST /branches/{project}/use`` (spec V8). The response
    carries ``active_branch_cleared`` (the cleared branch ID, null when none
    was set) and a ``Kbagent-Warnings`` entry ``ACTIVE_CHOICE_SHARED``.
    """
    result = registry.branch.reset_branch(alias=project)
    stored = registry.config_store.get_project(result["project_alias"])
    if stored is None or not stored.ephemeral:
        # An environment project stores nothing, so nothing is shared.
        warn_active_choice_shared(registry.config_store.config_dir, "active branch")
    return {**result, "active_branch_cleared": result.get("previous_branch_id")}


@router.delete("/{project}/{branch_id}", summary="Delete a branch")
def delete(
    project: str,
    branch_id: int,
    dry_run: bool = False,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Delete a development branch. Mirrors `kbagent branch delete`.

    No confirmation over REST: the request is the consent, as with `--yes`.
    """
    return registry.branch.delete_branch(alias=project, branch_id=branch_id, dry_run=dry_run)


@router.get("/{project}/metadata", summary="List branch metadata")
def metadata_list(
    project: str,
    branch_id: int | str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """List all metadata keys for a branch. Mirrors `kbagent branch metadata-list`."""
    return registry.branch.list_branch_metadata(
        alias=project, branch_id=branch_id if branch_id is not None else "default"
    )


@router.get("/{project}/metadata/{key}", summary="Get a branch metadata value")
def metadata_get(
    project: str,
    key: str,
    branch_id: int | str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Fetch a single branch metadata entry. Mirrors `kbagent branch metadata-get`."""
    return registry.branch.get_branch_metadata(
        alias=project, key=key, branch_id=branch_id if branch_id is not None else "default"
    )


@router.put("/{project}/metadata/{key}", summary="Set a branch metadata value")
def metadata_set(
    project: str,
    key: str,
    body: MetadataSet,
    branch_id: int | str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Write a branch metadata entry. Mirrors `kbagent branch metadata-set`."""
    return registry.branch.set_branch_metadata(
        alias=project,
        key=key,
        value=body.value,
        branch_id=branch_id if branch_id is not None else "default",
    )


@router.delete("/{project}/metadata/{metadata_id}", summary="Delete a branch metadata entry")
def metadata_delete(
    project: str,
    metadata_id: int,
    branch_id: int | str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Remove a branch metadata entry by id. Mirrors `kbagent branch metadata-delete`."""
    return registry.branch.delete_branch_metadata(
        alias=project,
        metadata_id=metadata_id,
        branch_id=branch_id if branch_id is not None else "default",
    )
