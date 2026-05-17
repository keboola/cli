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


@router.get("", summary="List registered projects")
def list_projects(registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    """All registered project aliases."""
    return {"projects": registry.project.list_projects()}


@router.post("", summary="Add a project")
def add_project(
    body: ProjectCreate, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Add a project. Verifies the storage token before persisting."""
    return registry.project.add_project(body.alias, body.stack_url, body.token)


@router.delete("/{alias}", summary="Remove a project")
def remove_project(alias: str, registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    """Remove a project by alias. Mirrors `kbagent project remove`."""
    return registry.project.remove_project(alias)


@router.patch("/{alias}", summary="Edit a project")
def edit_project(
    alias: str, body: ProjectEdit, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Update stack URL, token, or alias of a project. Mirrors `kbagent project edit`."""
    return registry.project.edit_project(
        alias=alias,
        stack_url=body.stack_url,
        token=body.token,
        new_alias=body.new_alias,
        dry_run=body.dry_run,
    )


@router.get("/status", summary="Check project connectivity")
def status(
    alias: str | None = None, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Connectivity check; pass ``?alias=`` to limit to one project."""
    aliases = [alias] if alias else None
    return {"status": registry.project.get_status(aliases=aliases)}


@router.get("/current", summary="Get the active project")
def current(registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    """Currently pinned project alias. Mirrors `kbagent project current`."""
    return registry.project.current_project()


@router.post("/use/{alias}", summary="Switch the active project")
def use_project(alias: str, registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    """Pin a project as the current default. Mirrors `kbagent project use`."""
    return registry.project.use_project(alias)


@router.get("/{alias}/info", summary="Get project metadata")
def info(alias: str, registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    """Project metadata (stack, owner, tokens). Mirrors `kbagent project info`."""
    return registry.project.get_info(alias)


@router.get("/{alias}/description", summary="Get the project description")
def get_description(
    alias: str, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.branch.get_project_description(alias)


@router.put("/{alias}/description", summary="Set the project description")
def set_description(
    alias: str,
    body: ProjectDescription,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.branch.set_project_description(alias, body.description)
