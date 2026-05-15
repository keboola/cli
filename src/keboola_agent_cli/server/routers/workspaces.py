"""Workspace endpoints (CRUD, password, load tables, SQL query)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


class WorkspaceCreate(BaseModel):
    name: str = ""
    backend: str | None = None
    read_only: bool = True
    ui_mode: bool = False


class WorkspaceLoad(BaseModel):
    tables: list[str]
    preserve: bool = False


class WorkspaceQuery(BaseModel):
    sql: str
    transactional: bool = False


class FromTransformation(BaseModel):
    component_id: str
    config_id: str
    row_id: str | None = None


class SqlHelperRequest(BaseModel):
    """Input for the /workspaces/sql/improve/stream endpoint.

    Mirrors :class:`PromptHelperRequest` from the agents router but carries
    workspace-specific context (project, backend, schema, visible buckets)
    so the meta-prompt the AI receives is grounded in the user's current
    workspace -- no generic 'write SQL' guesswork.
    """

    cli: str  # claude | codex | gemini -- same recipe as ai_agent runs
    goal: str
    project: str
    backend: str
    schema_name: str
    workspace_id: int | None = None
    draft_sql: str = ""
    bucket_ids: list[str] = []
    extra_args: list[str] = []


@router.get("")
def list_workspaces(
    project: list[str] | None = Query(None),
    orphaned: bool = False,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.workspace.list_workspaces(aliases=project, orphaned_only=orphaned)


@router.post("/{project}")
def create(
    project: str, body: WorkspaceCreate, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.workspace.create_workspace(
        alias=project,
        name=body.name,
        backend=body.backend,
        read_only=body.read_only,
        ui_mode=body.ui_mode,
    )


@router.get("/{project}/{workspace_id}")
def detail(
    project: str, workspace_id: int, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.workspace.get_workspace(alias=project, workspace_id=workspace_id)


@router.delete("/{project}/{workspace_id}")
def delete(
    project: str, workspace_id: int, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.workspace.delete_workspace(alias=project, workspace_id=workspace_id)


@router.post("/{project}/{workspace_id}/password")
def password(
    project: str, workspace_id: int, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.workspace.reset_password(alias=project, workspace_id=workspace_id)


@router.post("/{project}/{workspace_id}/load")
def load(
    project: str,
    workspace_id: int,
    body: WorkspaceLoad,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.workspace.load_tables(
        alias=project,
        workspace_id=workspace_id,
        tables=body.tables,
        preserve=body.preserve,
    )


@router.post("/{project}/{workspace_id}/query")
def query(
    project: str,
    workspace_id: int,
    body: WorkspaceQuery,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.workspace.execute_query(
        alias=project,
        workspace_id=workspace_id,
        sql=body.sql,
        transactional=body.transactional,
    )


def _sse(event: str, data: dict[str, Any]) -> bytes:
    """Encode a single SSE frame (event + data)."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode()


@router.post("/sql/improve/stream")
async def improve_sql_stream(
    body: SqlHelperRequest,
    registry: ServiceRegistry = Depends(get_registry),
) -> StreamingResponse:
    """Stream an AI-generated SQL query back to the workspace SQL editor.

    Mirrors /agents/prompt/improve/stream but with a SQL-specific meta-prompt
    that grounds the AI in the workspace's backend (snowflake/bigquery),
    default schema, and the visible bucket catalog the editor sidebar has
    already loaded. The AI is also told how to use INFORMATION_SCHEMA via
    `kbagent workspace query` for any discovery the bucket hint doesn't cover.

    Same SSE event protocol as the agent prompt helper (init/stdout/stderr/
    done) so the UI can reuse the streaming progress renderer.
    """
    from ..agent_runner import (
        build_sql_helper_meta_prompt,
        clean_sql_helper_response,
        stream_ai_agent_events,
    )

    goal = body.goal.strip()
    if not goal:
        raise HTTPException(status_code=400, detail="goal must not be empty")

    meta_prompt = build_sql_helper_meta_prompt(
        goal=goal,
        project=body.project,
        backend=body.backend,
        schema=body.schema_name,
        draft_sql=body.draft_sql,
        bucket_ids=body.bucket_ids or None,
    )
    params: dict[str, Any] = {
        "cli": body.cli,
        "prompt": meta_prompt,
        "extra_args": body.extra_args,
        # SQL helper prompts target ~10-30s for a simple SELECT, up to a minute
        # when the AI has to round-trip INFORMATION_SCHEMA. 180s cap matches
        # the prompt helper so a stuck CLI doesn't camp on the connection.
        "timeout": 180.0,
    }

    async def gen() -> AsyncIterator[bytes]:
        yield _sse(
            "init",
            {
                "kind": "sql_helper",
                "cli": body.cli,
                "project": body.project,
                "backend": body.backend,
                "goal_preview": goal[:200],
            },
        )
        try:
            async for evt in stream_ai_agent_events(registry, params):
                if evt["event"] == "done":
                    raw = str(evt["data"].get("response") or "")
                    cleaned = clean_sql_helper_response(raw)
                    enriched = {**evt["data"], "sql": cleaned, "raw_response": raw}
                    yield _sse("done", enriched)
                else:
                    yield _sse(evt["event"], evt["data"])
        except ValueError as exc:
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


@router.post("/{project}/from-transformation")
def from_transformation(
    project: str,
    body: FromTransformation,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.workspace.create_from_transformation(
        alias=project,
        component_id=body.component_id,
        config_id=body.config_id,
        row_id=body.row_id,
    )


@router.post("/gc")
def gc(
    project: list[str] | None = Query(None),
    dry_run: bool = False,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.workspace.gc_workspaces(aliases=project, dry_run=dry_run)
