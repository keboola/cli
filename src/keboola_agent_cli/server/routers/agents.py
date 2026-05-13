"""Agent-task scheduling endpoints (CRUD + manual run + history + SSE stream)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..agent_runner import (
    compute_next_run,
    run_task_once,
    stream_ai_agent_events,
)
from ..agents_store import AgentAction, AgentRun, AgentTask, Trigger
from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/agents", tags=["agents"])


def _validate_trigger(
    store: Any, trigger: Trigger | None, *, owner_task_id: str | None = None
) -> None:
    """Reject obviously broken trigger configs at API boundary.

    - downstream task_id must exist
    - no self-loop (task triggering itself)

    Deeper cycle detection (A→B→A) is left to runtime safety; the value of
    a deep check here is low compared to the implementation cost.
    """
    if trigger is None:
        return
    if owner_task_id is not None and trigger.task_id == owner_task_id:
        raise HTTPException(
            status_code=422,
            detail="Trigger target cannot be the task itself (would self-loop).",
        )
    if store.get_task(trigger.task_id) is None:
        raise HTTPException(
            status_code=422,
            detail=f"Trigger target task '{trigger.task_id}' not found.",
        )


class AgentTaskCreate(BaseModel):
    name: str
    description: str = ""
    cron: str = "0 * * * *"
    manual: bool = False
    enabled: bool = True
    action: AgentAction
    trigger: Trigger | None = None


class AgentTaskUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    cron: str | None = None
    manual: bool | None = None
    enabled: bool | None = None
    action: AgentAction | None = None
    # Sentinel: pass an explicit JSON ``null`` to clear the trigger; omitting
    # the key (Pydantic default-unset) means "leave as is". FastAPI maps both
    # to ``None`` here, so we rely on ``model_fields_set`` below.
    trigger: Trigger | None = None


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
    _validate_trigger(store, body.trigger)
    task = AgentTask(
        name=body.name,
        description=body.description,
        cron=body.cron,
        manual=body.manual,
        enabled=body.enabled,
        action=body.action,
        trigger=body.trigger,
        # Manual tasks have no future cron firing; skip the croniter call so
        # we don't store a bogus next_run_at the UI would then display.
        next_run_at=None if body.manual else compute_next_run(body.cron),
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
    if body.manual is not None:
        task.manual = body.manual
        # Switching to manual nulls out next_run_at; switching back recomputes.
        task.next_run_at = None if body.manual else compute_next_run(task.cron)
    if body.enabled is not None:
        task.enabled = body.enabled
    if body.action is not None:
        task.action = body.action
    # ``trigger`` uses model_fields_set so we can distinguish "field absent"
    # (leave alone) from "explicit null" (clear chain). Pydantic v2 exposes
    # this via the model_fields_set attribute on the BaseModel instance.
    if "trigger" in body.model_fields_set:
        _validate_trigger(store, body.trigger, owner_task_id=task.id)
        task.trigger = body.trigger
    store.upsert_task(task)
    return task.model_dump(mode="json")


@router.delete("/{task_id}")
def delete_task(task_id: str, request: Request) -> dict[str, Any]:
    store = _store(request)
    if not store.delete_task(task_id):
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return {"status": "deleted", "id": task_id}


class RunNowBody(BaseModel):
    """Optional per-run input merged into the task's persisted action params.

    Used so manual tasks (``manual=True``) can receive ad-hoc runtime input
    — e.g. the AI Data Lab pattern where the persisted prompt says "read the
    user's question from runtime input", and each invocation passes a
    different question. The merge is one-shot (does not mutate the saved
    task); the next cron / chain firing sees only the persisted params.

    Per-action-type semantics:
    - ``ai_agent``: ``runtime_input.prompt`` (string) is appended to the
      persisted prompt as a labeled section so the AI sees both the
      operator's static instructions and the runtime ask.
    - ``cli_command``: ``runtime_input.argv`` (list of strings) is appended
      to the persisted argv list.
    - ``mcp_tool``: ``runtime_input`` (dict) is shallow-merged into the
      persisted MCP tool input, with runtime keys winning on conflict.
    """

    runtime_input: dict[str, Any] | None = None


def _merge_runtime_input(task: AgentTask, runtime_input: dict[str, Any] | None) -> AgentTask:
    """Return a shallow-copied task with runtime_input merged into its action.

    The original task is NOT mutated; we copy because ``run_task_once``
    persists ``task.last_run_at`` / ``next_run_at`` and we want those side
    effects to land on the real stored task, not the merged ghost.
    """
    if not runtime_input:
        return task
    merged_params = dict(task.action.params)
    if task.action.type == "ai_agent":
        extra = runtime_input.get("prompt")
        if isinstance(extra, str) and extra.strip():
            base_prompt = str(merged_params.get("prompt", ""))
            # Append rather than replace so the persisted instructions still
            # dominate; the runtime ask comes in as a clearly-labeled section.
            merged_params["prompt"] = (
                f"{base_prompt}\n\n[Operator's runtime input for this run]\n{extra.strip()}"
            )
    elif task.action.type == "cli_command":
        extra_argv = runtime_input.get("argv")
        if isinstance(extra_argv, list) and extra_argv:
            base_argv = list(merged_params.get("argv") or [])
            merged_params["argv"] = [*base_argv, *(str(a) for a in extra_argv)]
    elif task.action.type == "mcp_tool":
        # Shallow-merge into the tool input; runtime keys win.
        base_input = dict(merged_params.get("input") or {})
        base_input.update(runtime_input)
        merged_params["input"] = base_input
    merged_action = AgentAction(type=task.action.type, params=merged_params)
    # AgentTask.model_copy with update returns a shallow copy with the action
    # replaced — task.last_run_at / next_run_at on the *real* stored record
    # still update via run_task_once.upsert_task(task) because that path uses
    # the original task object passed in.
    return task.model_copy(update={"action": merged_action})


@router.post("/{task_id}/run")
async def run_now(
    task_id: str,
    request: Request,
    registry: ServiceRegistry = Depends(get_registry),
    body: RunNowBody | None = None,
) -> dict[str, Any]:
    """Trigger a task immediately (does not wait for the next cron tick).

    Blocking variant kept for compatibility / scripted callers.
    UI uses ``POST /agents/{task_id}/run/stream`` for live progress + attach.

    Optional ``runtime_input`` body (typically used by manual tasks) is
    merged into the task's persisted action params for this run only —
    e.g. the operator types an ad-hoc question into the UI, the persisted
    prompt is preserved, and the cron-driven next firing is unaffected.
    """
    store = _store(request)
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    runtime_input = body.runtime_input if body is not None else None
    task_for_run = _merge_runtime_input(task, runtime_input)
    run = await run_task_once(task_for_run, registry, store)
    return run.model_dump(mode="json")


@router.post("/{task_id}/run/stream")
async def run_now_stream(
    task_id: str,
    request: Request,
    registry: ServiceRegistry = Depends(get_registry),
) -> StreamingResponse:
    """Run the task with SSE event streaming, supporting late attach.

    If a run is already in flight for this task (someone else started it),
    we attach: replay the buffered events from the start, then tail live.
    If no run is active, we start one. Kill-on-empty: when every consumer
    disconnects, the runner is cancelled so we don't leak claude subprocesses.

    The final ``done`` event mirrors the AgentRun record persisted to disk;
    callers don't need to also GET ``/agents/{id}/runs`` to learn the
    outcome (though the persistent record is available there once the run
    finishes).
    """
    store = _store(request)
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    broadcaster = getattr(request.app.state, "run_broadcaster", None)
    if broadcaster is None:
        raise HTTPException(status_code=503, detail="Run broadcaster not installed.")

    async def gen() -> AsyncIterator[bytes]:
        # Lead with `init` so the client always gets a packet within the
        # SSE handshake window (no 30s+ silence before the agent emits).
        yield _sse(
            "init",
            {"task_id": task.id, "name": task.name, "action_type": task.action.type},
        )
        try:
            async for evt in broadcaster.start_or_attach(task, registry, store):
                yield _sse(evt["event"], evt["data"])
        except Exception as exc:
            yield _sse("done", {"status": "error", "error": str(exc)})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.get("/{task_id}/runs")
def list_runs(task_id: str, request: Request, limit: int = 50) -> dict[str, Any]:
    store = _store(request)
    runs = store.list_runs(task_id, limit=limit)
    return {"runs": [r.model_dump(mode="json") for r in runs]}


@router.get("/{task_id}/runs/{run_id}")
def get_run(task_id: str, run_id: str, request: Request) -> dict[str, Any]:
    """Fetch a single persisted run record by its run_id.

    Lighter than ``/runs?limit=...`` when the UI already has the run_id
    (e.g. clicking on a row from the runs list). Includes the ``summary``
    (model, tokens, cost, tool counts) and ``events_path`` so the caller
    knows whether a timeline can be replayed via ``/runs/{run_id}/events``.
    """
    store = _store(request)
    run = store.get_run(task_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return run.model_dump(mode="json")


@router.get("/{task_id}/runs/{run_id}/events")
def get_run_events(task_id: str, run_id: str, request: Request) -> dict[str, Any]:
    """Return the full event timeline for one finished run.

    Used by the detail drawer to "replay" a run with the same per-step
    UI shown during a live run. ``events`` mirrors the SSE stream shape
    one-for-one (each item has ``event`` + ``data`` + ``seq`` keys); the
    frontend renderer can treat live and replay sources interchangeably.

    Returns 404 if the run exists but no timeline was persisted (e.g. an
    older run from before v0.10.x), so the caller can fall back to the
    legacy ``output.response`` rendering.
    """
    store = _store(request)
    events = store.load_events(task_id, run_id)
    if events is None:
        raise HTTPException(
            status_code=404,
            detail=f"No event timeline persisted for run '{run_id}' (likely a pre-0.10 run)",
        )
    return {"events": events, "count": len(events)}


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


def _sse(event: str, data: Any) -> bytes:
    """Format one SSE message: `event: <name>\\ndata: <json>\\n\\n`."""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode()


@router.post("/test/stream")
async def test_action_stream(
    body: AgentTaskCreate,
    registry: ServiceRegistry = Depends(get_registry),
) -> StreamingResponse:
    """Stream a test-run as SSE.

    AI-agent runs (action.type == "ai_agent") emit one SSE event per line of
    claude/codex/gemini stdout (claude is JSONL via ``--output-format=stream-json``;
    codex / gemini emit raw text). Stderr is interleaved as ``stderr`` events.
    A final ``done`` event carries exit_code, elapsed_seconds, response_text,
    and full stderr -- so even if the client missed earlier events, the
    last one is self-contained.

    Non-streaming action types (``cli_command``, ``mcp_tool``) are wrapped:
    the full result is emitted as a single ``done`` event. This keeps the
    frontend code path uniform (always `/agents/test/stream`).
    """
    action_type = body.action.type

    async def gen() -> AsyncIterator[bytes]:
        # Always lead with an init event so the client knows the request
        # made it through (no 30-second silence if the AI takes a while
        # to start producing output).
        yield _sse(
            "init",
            {
                "name": body.name or "[preview]",
                "action_type": action_type,
                "cron": body.cron,
            },
        )
        if action_type == "ai_agent":
            try:
                async for evt in stream_ai_agent_events(registry, body.action.params):
                    yield _sse(evt["event"], evt["data"])
            except Exception as exc:
                yield _sse(
                    "done",
                    {"status": "error", "error": str(exc)},
                )
        else:
            # Reuse the existing non-stream dispatch for the other two
            # action types -- they emit no incremental events anyway.
            transient = AgentTask(
                name=body.name or "[preview]",
                description=body.description,
                cron=body.cron,
                enabled=False,
                action=body.action,
            )
            try:
                run = await run_task_once(transient, registry, _NullStore())
                yield _sse("done", run.model_dump(mode="json"))
            except Exception as exc:
                yield _sse("done", {"status": "error", "error": str(exc)})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            # Disable proxy buffering so events flush to the client
            # immediately (matches how the BFF's streamSSE helper
            # already passes through these headers).
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


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


class PromptHelperRequest(BaseModel):
    """Input for the /agents/prompt/improve/stream endpoint.

    The helper rewrites the user's plain-English goal (and any half-baked
    draft) into a polished prompt suitable for an AI-agent scheduled task.
    The output is streamed back as SSE so the UI can show progress, and the
    final ``done`` event carries the cleaned prompt body ready to drop into
    the task's prompt textarea.
    """

    cli: str  # claude | codex | gemini -- same recipe as ai_agent runs
    goal: str
    draft: str = ""
    project: str | None = None
    extra_args: list[str] = []


@router.post("/prompt/improve/stream")
async def improve_prompt_stream(
    body: PromptHelperRequest,
    registry: ServiceRegistry = Depends(get_registry),
) -> StreamingResponse:
    """Stream an AI-generated, polished prompt back to the UI as SSE events.

    The chosen CLI (claude / codex / gemini) is invoked exactly the same way
    a scheduled ai_agent run would invoke it -- via
    :func:`stream_ai_agent_events` -- but the *prompt* it receives is a
    meta-prompt asking it to rewrite the user's draft into a polished
    single-shot prompt. The UI consumes the same SSE event shapes
    (``init`` / ``stdout`` / ``stderr`` / ``done``) the test-stream
    endpoint emits, so it can reuse the live-progress renderer.

    The ``done`` event is enriched with a ``prompt`` field carrying the
    cleaned AI response (code fences and "Here is the prompt:" preambles
    stripped) so the frontend can drop it straight into the textarea.
    """
    from ..agent_runner import (
        build_prompt_helper_meta_prompt,
        clean_prompt_helper_response,
    )

    goal = body.goal.strip()
    if not goal:
        raise HTTPException(status_code=400, detail="goal must not be empty")
    meta_prompt = build_prompt_helper_meta_prompt(
        goal=goal,
        draft=body.draft,
        project=body.project,
    )
    params: dict[str, Any] = {
        "cli": body.cli,
        "prompt": meta_prompt,
        "extra_args": body.extra_args,
        # Helper prompts should finish in <60s; cap aggressively so a stuck
        # CLI doesn't hold the SSE connection for the default 10 minutes.
        "timeout": 180.0,
    }

    async def gen() -> AsyncIterator[bytes]:
        # Mirror the /test/stream init shape so the React side can reuse
        # AgentRunView verbatim.
        yield _sse(
            "init",
            {
                "kind": "prompt_helper",
                "cli": body.cli,
                "goal_preview": goal[:200],
            },
        )
        try:
            async for evt in stream_ai_agent_events(registry, params):
                if evt["event"] == "done":
                    raw = str(evt["data"].get("response") or "")
                    cleaned = clean_prompt_helper_response(raw)
                    enriched = {**evt["data"], "prompt": cleaned, "raw_response": raw}
                    yield _sse("done", enriched)
                else:
                    yield _sse(evt["event"], evt["data"])
        except ValueError as exc:
            # build_*_meta_prompt and stream_ai_agent_events raise ValueError
            # for bad CLI / empty prompt / malformed extra_args. Send a final
            # done event so the client doesn't hang waiting.
            yield _sse("done", {"status": "error", "error": str(exc)})
        except Exception as exc:
            yield _sse("done", {"status": "error", "error": str(exc)})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
