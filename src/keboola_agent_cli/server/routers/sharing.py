"""Bucket sharing endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/sharing", tags=["sharing"])


class ShareBucket(BaseModel):
    bucket_id: str
    type: str
    target_project_ids: list[int] | None = None
    target_users: list[str] | None = None


class LinkBucket(BaseModel):
    source_project_id: int
    bucket_id: str
    name: str | None = None


@router.get("")
def list_shared(
    project: list[str] | None = Query(None),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.sharing.list_shared(aliases=project)


@router.get("/edges")
def edges(
    project: list[str] | None = Query(None),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.lineage.get_lineage(aliases=project)


@router.post("/{project}/share")
def share(
    project: str, body: ShareBucket, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.sharing.share(
        alias=project,
        bucket_id=body.bucket_id,
        sharing_type=body.type,
        target_project_ids=body.target_project_ids,
        target_users=body.target_users,
    )


@router.post("/{project}/unshare/{bucket_id:path}")
def unshare(
    project: str, bucket_id: str, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.sharing.unshare(alias=project, bucket_id=bucket_id)


@router.post("/{project}/link")
def link(
    project: str, body: LinkBucket, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.sharing.link(
        alias=project,
        source_project_id=body.source_project_id,
        source_bucket_id=body.bucket_id,
        name=body.name,
    )


@router.post("/{project}/unlink/{bucket_id:path}")
def unlink(
    project: str, bucket_id: str, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.sharing.unlink(alias=project, bucket_id=bucket_id)
