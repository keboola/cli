"""Agent-task scheduling endpoints (CRUD + manual run + history)."""

from __future__ import annotations

from datetime import UTC
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..agent_runner import compute_next_run, run_task_once
from ..agents_store import AgentAction, AgentRun, AgentTask
from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/agents", tags=["agents"])


class AgentTaskCreate(BaseModel):
    name: str
    description: str = ""
    cron: str = "0 * * * *"
    enabled: bool = True
    action: AgentAction


class AgentTaskUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    cron: str | None = None
    enabled: bool | None = None
    action: AgentAction | None = None


def _store(request: Request):
    store = getattr(request.app.state, "agent_store", None)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="Agent scheduler is not enabled on this server.",
        )
    return store


@router.get("")
def list_tasks(request: Request) -> dict[str, Any]:
    store = _store(request)
    tasks = store.load_tasks()
    return {"tasks": [t.model_dump(mode="json") for t in tasks]}


@router.post("")
def create_task(body: AgentTaskCreate, request: Request) -> dict[str, Any]:
    store = _store(request)
    task = AgentTask(
        name=body.name,
        description=body.description,
        cron=body.cron,
        enabled=body.enabled,
        action=body.action,
        next_run_at=compute_next_run(body.cron),
    )
    saved = store.upsert_task(task)
    return saved.model_dump(mode="json")


@router.get("/{task_id}")
def get_task(task_id: str, request: Request) -> dict[str, Any]:
    store = _store(request)
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return task.model_dump(mode="json")


@router.patch("/{task_id}")
def update_task(task_id: str, body: AgentTaskUpdate, request: Request) -> dict[str, Any]:
    store = _store(request)
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    if body.name is not None:
        task.name = body.name
    if body.description is not None:
        task.description = body.description
    if body.cron is not None:
        task.cron = body.cron
        task.next_run_at = compute_next_run(body.cron)
    if body.enabled is not None:
        task.enabled = body.enabled
    if body.action is not None:
        task.action = body.action
    store.upsert_task(task)
    return task.model_dump(mode="json")


@router.delete("/{task_id}")
def delete_task(task_id: str, request: Request) -> dict[str, Any]:
    store = _store(request)
    if not store.delete_task(task_id):
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return {"status": "deleted", "id": task_id}


@router.post("/{task_id}/run")
async def run_now(
    task_id: str,
    request: Request,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Trigger a task immediately (does not wait for the next cron tick)."""
    store = _store(request)
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    run = await run_task_once(task, registry, store)
    return run.model_dump(mode="json")


@router.get("/{task_id}/runs")
def list_runs(task_id: str, request: Request, limit: int = 50) -> dict[str, Any]:
    store = _store(request)
    runs = store.list_runs(task_id, limit=limit)
    return {"runs": [r.model_dump(mode="json") for r in runs]}


@router.post("/test")
async def test_action(
    body: AgentTaskCreate,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Execute an action ad-hoc -- no persistence, no scheduling.

    Used by the React "Run preview" button so users can validate an action
    before saving the task. The result mirrors what a real run would
    produce, but nothing is written to ``agents.json`` or run history.
    """
    # Build a transient task. Reuse run_task_once so the dispatch logic
    # (mcp_tool / cli_command / ai_agent) is the same code path that
    # the scheduler uses -- prevents test-time and live-time divergence.
    transient = AgentTask(
        name=body.name or "[preview]",
        description=body.description,
        cron=body.cron,
        enabled=False,
        action=body.action,
    )
    # Use a throwaway in-memory store so run_task_once's persistence side
    # effects (append_run, upsert_task) write to /dev/null.
    store = _NullStore()
    run: AgentRun = await run_task_once(transient, registry, store)
    return run.model_dump(mode="json")


class _NullStore:
    """Drop-in replacement for AgentStore for one-off /test runs.

    Implements the two methods run_task_once calls -- ``append_run`` and
    ``upsert_task`` -- as no-ops so neither the run record nor the task's
    last_run_at update touch disk.
    """

    def append_run(self, _run: Any) -> None:
        return None

    def upsert_task(self, task: AgentTask) -> AgentTask:
        return task


@router.get("/cron/preview")
def cron_preview(cron: str, count: int = 5) -> dict[str, Any]:
    """Preview the next ``count`` firings of a cron expression. Validates syntax."""
    from datetime import datetime

    from croniter import croniter

    try:
        it = croniter(cron, datetime.now(UTC))
        firings = []
        for _ in range(max(1, min(count, 20))):
            dt = it.get_next(datetime)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            firings.append(dt.isoformat())
        return {"cron": cron, "firings": firings}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {exc}") from exc
