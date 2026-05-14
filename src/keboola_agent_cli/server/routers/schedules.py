"""Schedule discovery endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/schedules", tags=["schedules"])


@router.get("")
def list_schedules(
    project: list[str] | None = Query(None),
    enabled_only: bool = False,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.schedule.list_schedules(
        aliases=project, enabled_only=enabled_only, branch_id=branch_id
    )


@router.get("/{project}/{schedule_id}")
def detail(
    project: str,
    schedule_id: str,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.schedule.get_schedule_detail(
        alias=project, schedule_id=schedule_id, branch_id=branch_id
    )


@router.get("/find/query")
def find(
    cron_window: str | None = None,
    not_run_since: int | None = None,
    project: list[str] | None = Query(None),
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.schedule.find_schedules(
        cron_window=cron_window,
        not_run_since_days=not_run_since,
        aliases=project,
        branch_id=branch_id,
    )
