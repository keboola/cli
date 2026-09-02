"""Flow + flow-schedule endpoints (conditional flows / keboola.flow only)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ...constants import (
    DEFAULT_JOB_RUN_TIMEOUT,
    DEFAULT_LOG_TAIL_LINES,
    DEFAULT_POLL_STRATEGY,
)
from ...services.flow_service import FLOW_COMPONENT_ID, get_flow_examples
from ...services.flow_validation import find_unreachable_phases, validate_conditional_flow
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


class FlowRun(BaseModel):
    """Body for a (possibly partial) flow run.

    ``from_phase`` and ``only_task_ids`` are mutually exclusive; with neither,
    this is an ordinary full flow run. With either, the Queue API runs ONLY the
    selected tasks and IGNORES the flow's phase conditions -- see
    ``commands/_flow_run.py`` for why that distinction matters.
    """

    from_phase: str | None = None
    only_task_ids: list[str] | None = None
    branch_id: int | None = None


class FlowValidate(BaseModel):
    phases: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    project: str | None = None


# NOTE: /validate, /examples, and /{project}/schema are declared BEFORE the
# /{project} and /{project}/{config_id} routes -- FastAPI matches in
# declaration order, so the literal segments must win over the path
# parameters.


@router.post("/validate", summary="Validate a conditional-flow definition")
def validate(
    body: FlowValidate, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Validate phases/tasks (schema + semantic checks). Mirrors `kbagent flow validate`.

    With ``project`` the live keboola.flow JSON Schema is fetched from the stack
    for structural validation; a fetch failure degrades to semantic-only and is
    recorded in ``notes``. Without ``project`` only semantic checks run.
    """
    schema: dict[str, Any] | None = None
    notes: list[str] = []
    if body.project:
        fetch = registry.flow.fetch_flow_schema(body.project)
        schema = fetch.schema
        if schema is None:
            notes.append(f"structural schema validation skipped: {fetch.reason}")
    else:
        notes.append(
            "structural schema validation skipped: no schema source "
            "(pass 'project' to fetch the live schema from the stack)"
        )
    errors = validate_conditional_flow(body.phases, body.tasks, schema)
    warnings = [
        f"Phase '{pid}' is unreachable from the entry phase"
        for pid in find_unreachable_phases(body.phases)
    ]
    return {"valid": not errors, "errors": errors, "warnings": warnings, "notes": notes}


@router.get("/examples", summary="Show bundled example flow configurations")
def examples(component_id: str = FLOW_COMPONENT_ID) -> dict[str, Any]:
    """Bundled example flow configurations (offline, no project needed).

    Mirrors `kbagent flow examples`. Supported component ids: ``keboola.flow``
    (conditional, default) and ``keboola.orchestrator`` (legacy, informational
    only). An unknown component id is a 400.
    """
    try:
        flow_examples = get_flow_examples(component_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "component_id": component_id,
        "count": len(flow_examples),
        "examples": flow_examples,
    }


@router.get("/{project}/schema", summary="Fetch the live conditional-flow JSON Schema")
def get_schema(project: str, registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    """Dump the keboola.flow JSON Schema served by the stack. Mirrors `kbagent flow schema --full`."""
    fetch = registry.flow.fetch_flow_schema(project)
    if fetch.schema is None:
        raise HTTPException(
            status_code=502,
            detail=f"Could not fetch the conditional-flow schema: {fetch.reason}",
        )
    return {"format": "json-schema", "schema": fetch.schema}


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


@router.get("/{project}/{config_id}/triggers", summary="List every trigger kbagent can see")
def list_triggers(
    project: str,
    config_id: str,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Cron schedules AND table triggers for a flow.

    Unlike `/schedules`, the response states what was NOT checked: consumers
    must read `cross_project_triggers_checked` before concluding a flow has no
    trigger (issue #714).
    """
    return registry.flow.get_flow_triggers(alias=project, config_id=config_id, branch_id=branch_id)


@router.post("/{project}/{config_id}/run", summary="Run a flow, optionally only part of it")
def run(
    project: str,
    config_id: str,
    body: FlowRun,
    dry_run: bool = False,
    wait: bool = False,
    timeout: float = DEFAULT_JOB_RUN_TIMEOUT,
    poll_strategy: str = DEFAULT_POLL_STRATEGY,
    log_tail_lines: int = DEFAULT_LOG_TAIL_LINES,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Run a flow, optionally limited to a phase or an explicit task allowlist.

    Mirrors `kbagent flow run`. `dry_run=true` resolves the selection and
    returns it without creating a job; it requires a selector, since a full
    run has nothing to preview.
    """
    if body.from_phase and body.only_task_ids:
        raise HTTPException(
            status_code=400,
            detail="from_phase and only_task_ids are mutually exclusive.",
        )
    selection: dict[str, Any] | None = None
    if body.from_phase or body.only_task_ids:
        selection = registry.flow.resolve_flow_task_ids(
            alias=project,
            config_id=config_id,
            from_phase=body.from_phase,
            only_task_ids=body.only_task_ids,
            branch_id=body.branch_id,
        )
    elif dry_run:
        raise HTTPException(
            status_code=400,
            detail="dry_run requires from_phase or only_task_ids.",
        )
    if dry_run and selection is not None:
        return {**selection, "dry_run": True}

    result = registry.job.run_job(
        alias=project,
        component_id=FLOW_COMPONENT_ID,
        config_id=config_id,
        wait=wait,
        timeout=timeout,
        branch_id=body.branch_id,
        poll_strategy=poll_strategy,
        log_tail_lines=log_tail_lines,
        only_flow_task_ids=selection["task_ids"] if selection else None,
    )
    if selection:
        result["flow_task_selection"] = selection
    return result


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
