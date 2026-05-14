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


@router.get("")
def list_branches(
    project: list[str] | None = Query(None),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.branch.list_branches(aliases=project)


@router.post("/{project}")
def create(
    project: str, body: BranchCreate, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.branch.create_branch(
        alias=project, name=body.name, description=body.description
    )


@router.post("/{project}/use")
def use(
    project: str, body: BranchUse, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.branch.set_active_branch(alias=project, branch_id=body.branch_id)


@router.post("/{project}/reset")
def reset(project: str, registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    return registry.branch.reset_branch(alias=project)


@router.delete("/{project}/{branch_id}")
def delete(
    project: str, branch_id: int, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.branch.delete_branch(alias=project, branch_id=branch_id)


@router.get("/{project}/merge-url")
def merge_url(
    project: str,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.branch.get_merge_url(alias=project, branch_id=branch_id)


@router.get("/{project}/metadata")
def metadata_list(
    project: str,
    branch_id: int | str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.branch.list_branch_metadata(alias=project, branch_id=branch_id)


@router.get("/{project}/metadata/{key}")
def metadata_get(
    project: str,
    key: str,
    branch_id: int | str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.branch.get_branch_metadata(alias=project, key=key, branch_id=branch_id)


@router.put("/{project}/metadata/{key}")
def metadata_set(
    project: str,
    key: str,
    body: MetadataSet,
    branch_id: int | str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.branch.set_branch_metadata(
        alias=project, key=key, value=body.value, branch_id=branch_id
    )


@router.delete("/{project}/metadata/{metadata_id}")
def metadata_delete(
    project: str,
    metadata_id: int,
    branch_id: int | str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.branch.delete_branch_metadata(
        alias=project, metadata_id=metadata_id, branch_id=branch_id
    )
