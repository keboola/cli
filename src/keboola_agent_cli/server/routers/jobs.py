"""Job endpoints (list, detail, run, terminate) + SSE log/status streaming."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from ...constants import DEFAULT_LOG_TAIL_LINES, DEFAULT_POLL_STRATEGY
from ..dependencies import ServiceRegistry, get_registry
from ..sse import json_event

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobRun(BaseModel):
    component_id: str
    config_id: str
    config_row_ids: list[str] | None = None
    branch_id: int | None = None
    variable_values_id: str | None = None
    no_variables: bool = False


class JobTerminate(BaseModel):
    job_ids: list[str] | None = None
    status: str | None = None
    component_id: str | None = None
    config_id: str | None = None
    branch_id: int | None = None
    limit: int | None = None
    dry_run: bool = False


@router.get("")
def list_jobs(
    project: list[str] | None = Query(None),
    component_id: str | None = None,
    config_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.job.list_jobs(
        aliases=project,
        component_id=component_id,
        config_id=config_id,
        status=status,
        limit=limit,
    )


@router.get("/{project}/{job_id}")
def detail(
    project: str, job_id: str, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.job.get_job_detail(alias=project, job_id=job_id)


@router.post("/{project}/run")
def run(
    project: str,
    body: JobRun,
    wait: bool = False,
    timeout: float = 300.0,
    poll_strategy: str = DEFAULT_POLL_STRATEGY,
    log_tail_lines: int = DEFAULT_LOG_TAIL_LINES,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.job.run_job(
        alias=project,
        component_id=body.component_id,
        config_id=body.config_id,
        config_row_ids=body.config_row_ids,
        wait=wait,
        timeout=timeout,
        branch_id=body.branch_id,
        variable_values_id=body.variable_values_id,
        no_variables=body.no_variables,
        poll_strategy=poll_strategy,
        log_tail_lines=log_tail_lines,
    )


@router.post("/{project}/terminate")
def terminate(
    project: str, body: JobTerminate, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    if body.job_ids:
        return registry.job.terminate_jobs(
            alias=project, job_ids=body.job_ids, dry_run=body.dry_run
        )
    job_ids = registry.job.resolve_job_ids_by_filter(
        alias=project,
        status=body.status,
        component_id=body.component_id,
        config_id=body.config_id,
        branch_id=body.branch_id,
        limit=body.limit,
    )
    return registry.job.terminate_jobs(alias=project, job_ids=job_ids, dry_run=body.dry_run)


@router.get("/{project}/{job_id}/stream")
async def stream_job(
    project: str,
    job_id: str,
    poll_interval: float = 2.0,
    log_tail_lines: int = 50,
    registry: ServiceRegistry = Depends(get_registry),
) -> EventSourceResponse:
    """SSE stream of job status transitions and recent log events.

    Emits one event per poll while the job is non-terminal; emits a final
    ``status`` event when the job reaches a terminal state, then ends.
    """
    projects = registry.job.resolve_projects([project])
    proj = projects[project]

    async def gen() -> AsyncIterator[dict[str, str]]:
        last_status: str | None = None
        last_event_id: int | None = None
        while True:
            try:
                detail = await asyncio.to_thread(
                    registry.job.get_job_detail, alias=project, job_id=job_id
                )
            except Exception as exc:
                yield json_event({"error": str(exc)}, event="error")
                return
            current_status = str(detail.get("status") or "")
            if current_status != last_status:
                yield json_event({"status": current_status, "job": detail}, event="status")
                last_status = current_status

            run_id = str(detail.get("runId") or detail.get("id") or job_id)
            try:
                client = registry.job._client_factory(proj.stack_url, proj.token)
                try:
                    events = await asyncio.to_thread(
                        client.fetch_job_events, run_id, limit=log_tail_lines
                    )
                finally:
                    client.close()
            except Exception:
                events = []
            for ev in events or []:
                ev_id_raw = ev.get("id")
                try:
                    ev_id = int(ev_id_raw) if ev_id_raw is not None else None
                except (TypeError, ValueError):
                    ev_id = None
                if ev_id is None or last_event_id is None or ev_id > last_event_id:
                    yield json_event(ev, event="log")
                    if ev_id is not None and (last_event_id is None or ev_id > last_event_id):
                        last_event_id = ev_id

            if current_status in {"success", "error", "warning", "terminated", "cancelled"}:
                yield json_event({"final": current_status, "job": detail}, event="done")
                return
            await asyncio.sleep(poll_interval)

    return EventSourceResponse(gen())
