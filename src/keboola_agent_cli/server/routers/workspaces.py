"""Workspace endpoints (CRUD, password, load tables, SQL query)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


class WorkspaceCreate(BaseModel):
    name: str = ""
    backend: str | None = None
    read_only: bool = True
    ui_mode: bool = False


class WorkspaceLoad(BaseModel):
    tables: list[str]
    preserve: bool = False


class WorkspaceQuery(BaseModel):
    sql: str
    transactional: bool = False


class FromTransformation(BaseModel):
    component_id: str
    config_id: str
    row_id: str | None = None


@router.get("")
def list_workspaces(
    project: list[str] | None = Query(None),
    orphaned: bool = False,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.workspace.list_workspaces(aliases=project, orphaned_only=orphaned)


@router.post("/{project}")
def create(
    project: str, body: WorkspaceCreate, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.workspace.create_workspace(
        alias=project,
        name=body.name,
        backend=body.backend,
        read_only=body.read_only,
        ui_mode=body.ui_mode,
    )


@router.get("/{project}/{workspace_id}")
def detail(
    project: str, workspace_id: int, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.workspace.get_workspace(alias=project, workspace_id=workspace_id)


@router.delete("/{project}/{workspace_id}")
def delete(
    project: str, workspace_id: int, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.workspace.delete_workspace(alias=project, workspace_id=workspace_id)


@router.post("/{project}/{workspace_id}/password")
def password(
    project: str, workspace_id: int, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.workspace.reset_password(alias=project, workspace_id=workspace_id)


@router.post("/{project}/{workspace_id}/load")
def load(
    project: str,
    workspace_id: int,
    body: WorkspaceLoad,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.workspace.load_tables(
        alias=project,
        workspace_id=workspace_id,
        tables=body.tables,
        preserve=body.preserve,
    )


@router.post("/{project}/{workspace_id}/query")
def query(
    project: str,
    workspace_id: int,
    body: WorkspaceQuery,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.workspace.execute_query(
        alias=project,
        workspace_id=workspace_id,
        sql=body.sql,
        transactional=body.transactional,
    )


@router.post("/{project}/from-transformation")
def from_transformation(
    project: str,
    body: FromTransformation,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.workspace.create_from_transformation(
        alias=project,
        component_id=body.component_id,
        config_id=body.config_id,
        row_id=body.row_id,
    )


@router.post("/gc")
def gc(
    project: list[str] | None = Query(None),
    dry_run: bool = False,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.workspace.gc_workspaces(aliases=project, dry_run=dry_run)
