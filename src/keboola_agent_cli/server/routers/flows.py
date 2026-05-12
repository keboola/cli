"""Flow + flow-schedule endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/flows", tags=["flows"])

DEFAULT_FLOW_COMPONENT = "keboola.flow"


class FlowCreate(BaseModel):
    name: str
    component_id: str = DEFAULT_FLOW_COMPONENT
    description: str = ""
    phases: list[dict[str, Any]] | None = None
    tasks: list[dict[str, Any]] | None = None
    branch_id: int | None = None


class FlowUpdate(BaseModel):
    component_id: str = DEFAULT_FLOW_COMPONENT
    name: str | None = None
    description: str | None = None
    phases: list[dict[str, Any]] | None = None
    tasks: list[dict[str, Any]] | None = None
    branch_id: int | None = None


class FlowSchedule(BaseModel):
    component_id: str = DEFAULT_FLOW_COMPONENT
    cron_tab: str
    timezone: str = "UTC"
    enabled: bool = True
    schedule_name: str | None = None
    branch_id: int | None = None


@router.get("")
def list_flows(
    project: list[str] | None = Query(None),
    branch_id: int | None = None,
    with_schedules: bool = False,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.flow.list_flows(
        aliases=project, branch_id=branch_id, with_schedules=with_schedules
    )


@router.get("/{project}/{config_id}")
def detail(
    project: str,
    config_id: str,
    component_id: str = DEFAULT_FLOW_COMPONENT,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.flow.get_flow_detail(
        alias=project, component_id=component_id, config_id=config_id, branch_id=branch_id
    )


@router.post("/{project}")
def create(
    project: str, body: FlowCreate, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.flow.create_flow(
        alias=project,
        component_id=body.component_id,
        name=body.name,
        description=body.description,
        phases=body.phases,
        tasks=body.tasks,
        branch_id=body.branch_id,
    )


@router.patch("/{project}/{config_id}")
def update(
    project: str,
    config_id: str,
    body: FlowUpdate,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.flow.update_flow(
        alias=project,
        component_id=body.component_id,
        config_id=config_id,
        name=body.name,
        description=body.description,
        phases=body.phases,
        tasks=body.tasks,
        branch_id=body.branch_id,
    )


@router.delete("/{project}/{config_id}")
def delete(
    project: str,
    config_id: str,
    component_id: str = DEFAULT_FLOW_COMPONENT,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.flow.delete_flow(
        alias=project, component_id=component_id, config_id=config_id, branch_id=branch_id
    )


@router.get("/{project}/{config_id}/schedules")
def list_schedules(
    project: str,
    config_id: str,
    component_id: str = DEFAULT_FLOW_COMPONENT,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.flow.list_flow_schedules(
        alias=project, component_id=component_id, config_id=config_id, branch_id=branch_id
    )


@router.post("/{project}/{config_id}/schedule")
def set_schedule(
    project: str,
    config_id: str,
    body: FlowSchedule,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.flow.set_flow_schedule(
        alias=project,
        component_id=body.component_id,
        config_id=config_id,
        cron_tab=body.cron_tab,
        timezone=body.timezone,
        enabled=body.enabled,
        schedule_name=body.schedule_name,
        branch_id=body.branch_id,
    )


@router.delete("/{project}/{config_id}/schedule")
def remove_schedule(
    project: str,
    config_id: str,
    component_id: str = DEFAULT_FLOW_COMPONENT,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.flow.remove_flow_schedule(
        alias=project, component_id=component_id, config_id=config_id, branch_id=branch_id
    )
