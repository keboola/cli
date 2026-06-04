"""Flow + flow-schedule endpoints (conditional flows / keboola.flow only)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/flows", tags=["flows"])


class FlowCreate(BaseModel):
    name: str
    description: str = ""
    phases: list[dict[str, Any]] | None = None
    tasks: list[dict[str, Any]] | None = None
    branch_id: int | None = None


class FlowUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    phases: list[dict[str, Any]] | None = None
    tasks: list[dict[str, Any]] | None = None
    branch_id: int | None = None


class FlowSchedule(BaseModel):
    cron_tab: str
    timezone: str = "UTC"
    enabled: bool = True
    schedule_name: str | None = None
    branch_id: int | None = None


@router.get("", summary="List flows across projects")
def list_flows(
    project: list[str] | None = Query(None),
    branch_id: int | None = None,
    with_schedules: bool = False,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """List flows in one or more projects. Mirrors `kbagent flow list`."""
    return registry.flow.list_flows(
        aliases=project, branch_id=branch_id, with_schedules=with_schedules
    )


@router.get("/{project}/{config_id}", summary="Get flow detail")
def detail(
    project: str,
    config_id: str,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Fetch a single flow configuration. Mirrors `kbagent flow detail`."""
    return registry.flow.get_flow_detail(alias=project, config_id=config_id, branch_id=branch_id)


@router.post("/{project}", summary="Create a new flow")
def create(
    project: str, body: FlowCreate, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Create a new flow configuration. Mirrors `kbagent flow new`."""
    return registry.flow.create_flow(
        alias=project,
        name=body.name,
        description=body.description,
        phases=body.phases,
        tasks=body.tasks,
        branch_id=body.branch_id,
    )


@router.patch("/{project}/{config_id}", summary="Update an existing flow")
def update(
    project: str,
    config_id: str,
    body: FlowUpdate,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Update name, description, or phases/tasks of a flow. Mirrors `kbagent flow update`."""
    return registry.flow.update_flow(
        alias=project,
        config_id=config_id,
        name=body.name,
        description=body.description,
        phases=body.phases,
        tasks=body.tasks,
        branch_id=body.branch_id,
    )


@router.delete("/{project}/{config_id}", summary="Delete a flow")
def delete(
    project: str,
    config_id: str,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Delete a flow configuration. Mirrors `kbagent flow delete`."""
    return registry.flow.delete_flow(alias=project, config_id=config_id, branch_id=branch_id)


@router.get("/{project}/{config_id}/schedules", summary="List schedules for a flow")
def list_schedules(
    project: str,
    config_id: str,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """List cron schedules attached to a flow."""
    return registry.flow.list_flow_schedules(
        alias=project, config_id=config_id, branch_id=branch_id
    )


@router.post("/{project}/{config_id}/schedule", summary="Set a cron schedule on a flow")
def set_schedule(
    project: str,
    config_id: str,
    body: FlowSchedule,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Attach or update a cron schedule on a flow. Mirrors `kbagent flow schedule`."""
    return registry.flow.set_flow_schedule(
        alias=project,
        config_id=config_id,
        cron_tab=body.cron_tab,
        timezone=body.timezone,
        enabled=body.enabled,
        schedule_name=body.schedule_name,
        branch_id=body.branch_id,
    )


@router.delete("/{project}/{config_id}/schedule", summary="Remove a flow schedule")
def remove_schedule(
    project: str,
    config_id: str,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Remove the cron schedule from a flow. Mirrors `kbagent flow schedule-remove`."""
    return registry.flow.remove_flow_schedule(
        alias=project, config_id=config_id, branch_id=branch_id
    )
