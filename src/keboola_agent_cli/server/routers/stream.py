"""Data Streams endpoints -- 1:1 mirror of the `kbagent stream` command group.

Every operation hits the Stream control-plane API authenticated with the
per-project Storage API token that the service resolves from config -- so,
unlike the `feature` router, no extra per-request token header is required.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/stream", tags=["stream"])


class CreateSourceBody(BaseModel):
    name: str
    source_type: str = "otlp"
    branch_id: str | None = None
    if_not_exists: bool = False
    reveal: bool = False
    provision_sinks: bool = True


class DeleteSourceBody(BaseModel):
    source_id: str
    branch_id: str | None = None
    dry_run: bool = False


@router.get("/{project}/list", summary="List Data Streams sources")
def list_sources(
    project: str,
    branch: str | None = Query(None, description="Branch ref (default branch if unset)"),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """List sources in `project`. Mirrors `kbagent stream list`."""
    return registry.stream.list_sources(alias=project, branch_id=branch)


@router.post("/{project}/create-source", summary="Create an OTLP/HTTP source")
def create_source(
    project: str,
    body: CreateSourceBody,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Create a source and return its (masked) endpoint. Mirrors
    `kbagent stream create-source`. Pass `reveal=true` to include the secret.
    """
    return registry.stream.create_source(
        alias=project,
        name=body.name,
        source_type=body.source_type,
        branch_id=body.branch_id,
        if_not_exists=body.if_not_exists,
        reveal=body.reveal,
        provision_sinks=body.provision_sinks,
    )


@router.get("/{project}/detail", summary="Source endpoints + destination")
def source_detail(
    project: str,
    source_id: str | None = Query(None, description="Source id (or use name)"),
    name: str | None = Query(None, description="Look up the source by name"),
    branch: str | None = Query(None, description="Branch ref (default branch if unset)"),
    reveal: bool = Query(False, description="Include the full endpoint incl. secret"),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Assemble one source's endpoints/protocol/destination. Mirrors
    `kbagent stream detail`. Secret is masked unless `reveal=true`.
    """
    return registry.stream.get_source_detail(
        alias=project,
        source_id=source_id,
        name=name,
        branch_id=branch,
        reveal=reveal,
    )


@router.post("/{project}/delete", summary="Delete a source (destructive)")
def delete_source(
    project: str,
    body: DeleteSourceBody,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Delete a source. Pass `dry_run=true` to preview. Mirrors
    `kbagent stream delete`.
    """
    return registry.stream.delete_source(
        alias=project,
        source_id=body.source_id,
        branch_id=body.branch_id,
        dry_run=body.dry_run,
    )
