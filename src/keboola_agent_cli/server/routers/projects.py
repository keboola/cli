"""Project CRUD + status endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    alias: str
    stack_url: str
    token: str


class ProjectEdit(BaseModel):
    stack_url: str | None = None
    token: str | None = None
    new_alias: str | None = None
    dry_run: bool = False


class ProjectDescription(BaseModel):
    description: str


@router.get("")
def list_projects(registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    """All registered project aliases."""
    return {"projects": registry.project.list_projects()}


@router.post("")
def add_project(
    body: ProjectCreate, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Add a project. Verifies the storage token before persisting."""
    return registry.project.add_project(body.alias, body.stack_url, body.token)


@router.delete("/{alias}")
def remove_project(alias: str, registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    return registry.project.remove_project(alias)


@router.patch("/{alias}")
def edit_project(
    alias: str, body: ProjectEdit, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.project.edit_project(
        alias=alias,
        stack_url=body.stack_url,
        token=body.token,
        new_alias=body.new_alias,
        dry_run=body.dry_run,
    )


@router.get("/status")
def status(
    alias: str | None = None, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Connectivity check; pass ``?alias=`` to limit to one project."""
    aliases = [alias] if alias else None
    return {"status": registry.project.get_status(aliases=aliases)}


@router.get("/current")
def current(registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    return registry.project.current_project()


@router.post("/use/{alias}")
def use_project(alias: str, registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    return registry.project.use_project(alias)


@router.get("/{alias}/info")
def info(alias: str, registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    return registry.project.get_info(alias)


@router.get("/{alias}/description")
def get_description(
    alias: str, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.branch.get_project_description(alias)


@router.put("/{alias}/description")
def set_description(
    alias: str,
    body: ProjectDescription,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.branch.set_project_description(alias, body.description)
