"""Component discovery (list/detail/scaffold)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/components", tags=["components"])


@router.get("")
def list_components(
    project: str | None = None,
    type: str | None = None,
    query: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    aliases = [project] if project else None
    return registry.component.list_components(aliases=aliases, component_type=type, query=query)


@router.get("/{component_id}")
def detail(
    component_id: str,
    project: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    if project is None:
        project, _ = registry.project.resolve_pinned_alias(None)
    return registry.component.get_component_detail(alias=project, component_id=component_id)


@router.post("/{component_id}/scaffold")
def scaffold(
    component_id: str,
    project: str | None = None,
    name: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    if project is None:
        project, _ = registry.project.resolve_pinned_alias(None)
    return registry.component.generate_scaffold(alias=project, component_id=component_id, name=name)
