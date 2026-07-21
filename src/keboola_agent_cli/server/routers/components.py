"""Component discovery (list/detail/scaffold) and synchronous actions."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/components", tags=["components"])


class SyncActionRequest(BaseModel):
    project: str | None = None
    config_id: str | None = None
    row_id: str | None = None
    branch_id: int | None = None
    config_data: dict[str, Any] | None = None
    timeout: float | None = None


@router.get("", summary="List components")
def list_components(
    project: str | None = None,
    type: str | None = None,
    query: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Browse available components, optionally filtered. Mirrors `kbagent component list`."""
    aliases = [project] if project else None
    return registry.component.list_components(aliases=aliases, component_type=type, query=query)


@router.get("/{component_id}", summary="Get component detail")
def detail(
    component_id: str,
    project: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Full component metadata including config schema. Mirrors `kbagent component detail`."""
    if project is None:
        project, _ = registry.project.resolve_pinned_alias(None)
    return registry.component.get_component_detail(alias=project, component_id=component_id)


@router.post("/{component_id}/scaffold", summary="Scaffold a new component config")
def scaffold(
    component_id: str,
    project: str | None = None,
    name: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Generate a starter configuration for the component. Mirrors `kbagent config new`."""
    if project is None:
        project, _ = registry.project.resolve_pinned_alias(None)
    return registry.component.generate_scaffold(alias=project, component_id=component_id, name=name)


@router.post("/{component_id}/actions/{action}", summary="Run a synchronous component action")
def sync_action(
    component_id: str,
    action: str,
    body: SyncActionRequest,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Run a synchronous component action (e.g. testConnection, getTables).

    Mirrors `kbagent component sync-action`. Either ``config_id`` (stored
    configuration, optionally shallow-merged with ``row_id``) or an explicit
    ``config_data`` payload is required -- the service enforces this and a
    violation surfaces as ConfigError (HTTP 400).
    """
    project = body.project
    if project is None:
        project, _ = registry.project.resolve_pinned_alias(None)
    return registry.component.run_sync_action(
        alias=project,
        component_id=component_id,
        action=action,
        config_id=body.config_id,
        row_id=body.row_id,
        branch_id=body.branch_id,
        config_data_override=body.config_data,
        timeout=body.timeout,
    )
