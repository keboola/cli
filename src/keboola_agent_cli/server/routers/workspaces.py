"""Workspace endpoints (CRUD, password, load tables, SQL query)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ...constants import AI_SQL_HELPER_TIMEOUT
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
    # When set, the helper is fixing a failed query: the goal is short
    # ("Fix this query"), draft_sql holds the failing SQL, and failed_error
    # is the warehouse error message. The meta-prompt switches mode so the
    # AI focuses on the error instead of writing from scratch.
    failed_error: str = ""


@router.get("", summary="List workspaces across projects")
def list_workspaces(
    project: list[str] | None = Query(None),
    orphaned: bool = False,
    branch: int | None = Query(
        None,
        description=(
            "Dev branch ID. Requires exactly one project. Without branch, the "
            "production endpoint is used regardless of any pinned active branch "
            "(read-command convention, mirrors `storage buckets`)."
        ),
    ),
    qs_compatible: bool = Query(
        False,
        description=(
            "Filter to RO + whitelisted-loginType workspaces (the canonical "
            "data-app shape). See QUERY_SERVICE_COMPATIBLE_LOGIN_TYPES."
        ),
    ),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """List workspaces in one or more projects. Mirrors `kbagent workspace list`."""
    if branch is not None and (project is None or len(project) != 1):
        raise HTTPException(
            status_code=400,
            detail="branch requires exactly one project (branch ID is per-project)",
        )
    return registry.workspace.list_workspaces(
        aliases=project,
        orphaned_only=orphaned,
        branch_id=branch,
        qs_compatible_only=qs_compatible,
    )


@router.post("/{project}", summary="Create a workspace")
def create(
    project: str, body: WorkspaceCreate, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Create a new workspace in a project. Mirrors `kbagent workspace create`."""
    return registry.workspace.create_workspace(
        alias=project,
        name=body.name,
        backend=body.backend,
        read_only=body.read_only,
        ui_mode=body.ui_mode,
    )


@router.get("/{project}/{workspace_id}", summary="Get workspace detail")
def detail(
    project: str,
    workspace_id: int,
    branch: int | None = Query(
        None,
        description=(
            "Dev branch ID. Without branch, the production endpoint is used "
            "regardless of any pinned active branch (read-command convention)."
        ),
    ),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Fetch detail for a single workspace. Mirrors `kbagent workspace detail`."""
    return registry.workspace.get_workspace(
        alias=project, workspace_id=workspace_id, branch_id=branch
    )


@router.delete("/{project}/{workspace_id}", summary="Delete a workspace")
def delete(
    project: str, workspace_id: int, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Delete a workspace by id. Mirrors `kbagent workspace delete`."""
    return registry.workspace.delete_workspace(alias=project, workspace_id=workspace_id)


@router.post("/{project}/{workspace_id}/password", summary="Reset workspace password")
def password(
    project: str, workspace_id: int, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Reset and return the workspace password. Mirrors `kbagent workspace password`."""
    return registry.workspace.reset_password(alias=project, workspace_id=workspace_id)


@router.post("/{project}/{workspace_id}/load", summary="Load tables into a workspace")
def load(
    project: str,
    workspace_id: int,
    body: WorkspaceLoad,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Load Storage tables into a workspace. Mirrors `kbagent workspace load`."""
    return registry.workspace.load_tables(
        alias=project,
        workspace_id=workspace_id,
        tables=body.tables,
        preserve=body.preserve,
    )


@router.post("/{project}/{workspace_id}/query", summary="Run SQL in a workspace")
def query(
    project: str,
    workspace_id: int,
    body: WorkspaceQuery,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Execute a SQL statement against the workspace. Mirrors `kbagent workspace query`."""
    return registry.workspace.execute_query(
        alias=project,
        workspace_id=workspace_id,
        sql=body.sql,
        transactional=body.transactional,
    )


def _sse(event: str, data: dict[str, Any]) -> bytes:
    """Encode a single SSE frame (event + data)."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode()


@router.post("/sql/improve/stream", summary="Stream AI SQL helper (SSE)")
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
        failed_error=body.failed_error or None,
    )
    params: dict[str, Any] = {
        "cli": body.cli,
        "prompt": meta_prompt,
        "extra_args": body.extra_args,
        "timeout": AI_SQL_HELPER_TIMEOUT,
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
                # Surface the full meta-prompt so the UI's "Show prompt"
                # transparency panel can render exactly what claude saw —
                # users debugging a bad suggestion need this to tell whether
                # the goal was misunderstood or whether the AI just ignored
                # the workspace context.
                "meta_prompt": meta_prompt,
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


@router.post("/{project}/from-transformation", summary="Create workspace from a transformation")
def from_transformation(
    project: str,
    body: FromTransformation,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Spin up a workspace based on a transformation configuration. Mirrors `kbagent workspace from-transformation`."""
    return registry.workspace.create_from_transformation(
        alias=project,
        component_id=body.component_id,
        config_id=body.config_id,
        row_id=body.row_id,
    )


@router.post("/gc", summary="Garbage-collect orphaned workspaces")
def gc(
    project: list[str] | None = Query(None),
    dry_run: bool = False,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Clean up orphaned workspaces across projects. Mirrors `kbagent workspace gc`."""
    return registry.workspace.gc_workspaces(aliases=project, dry_run=dry_run)
