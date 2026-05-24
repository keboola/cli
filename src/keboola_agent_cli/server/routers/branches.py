"""Branch lifecycle and metadata endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_registry

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
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """List development branches across one or more projects. Mirrors `kbagent branch list`."""
    return registry.branch.list_branches(aliases=project)


@router.post("/{project}", summary="Create a branch")
def create(
    project: str, body: BranchCreate, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Create a new development branch. Mirrors `kbagent branch create`."""
    return registry.branch.create_branch(
        alias=project, name=body.name, description=body.description
    )


@router.post("/{project}/use", summary="Pin the active branch")
def use(
    project: str, body: BranchUse, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Set the active branch for the project. Mirrors `kbagent branch use`."""
    return registry.branch.set_active_branch(alias=project, branch_id=body.branch_id)


@router.post("/{project}/reset", summary="Reset to the default branch")
def reset(project: str, registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    """Clear branch pin and revert to default branch. Mirrors `kbagent branch reset`."""
    return registry.branch.reset_branch(alias=project)


@router.delete("/{project}/{branch_id}", summary="Delete a branch")
def delete(
    project: str, branch_id: int, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Delete a development branch. Mirrors `kbagent branch delete`."""
    return registry.branch.delete_branch(alias=project, branch_id=branch_id)


@router.get("/{project}/merge-url", summary="Get the branch merge URL")
def merge_url(
    project: str,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """UI URL for merging a branch into main. Mirrors `kbagent branch merge`."""
    return registry.branch.get_merge_url(alias=project, branch_id=branch_id)


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
