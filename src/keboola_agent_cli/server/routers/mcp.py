"""MCP tool endpoints (list, call across projects).

DEPRECATED surface (epic #390 phase 2): every catalog tool has a native
CLI command and serve route -- see ``keboola_agent_cli.mcp_parity``. The
``/mcp/tools*`` operations are marked ``deprecated`` in OpenAPI and will
be removed together with the CLI ``tool`` group. ``/mcp/server-status``
stays (it reports embedded-server health, not tool passthrough).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/mcp", tags=["mcp"])


class ToolCall(BaseModel):
    input: dict[str, Any] | None = None
    project: str | None = None
    branch_id: str | None = None


@router.get("/tools", summary="List MCP tools", deprecated=True)
def list_tools(
    project: str | None = None,
    branch_id: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Discover every MCP tool exposed by `keboola-mcp-server` for one or
    all projects. Omit `project` to fan out across every registered alias.
    """
    aliases = [project] if project else None
    return registry.mcp.list_tools(aliases=aliases, branch_id=branch_id)


@router.get("/tools/{tool_name}/schema", summary="Fetch a tool's input schema", deprecated=True)
def tool_schema(
    tool_name: str,
    project: str | None = None,
    branch_id: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """JSON Schema for one MCP tool's `input` -- useful when wiring a form
    or pre-validating before `POST /mcp/tools/{name}/call`.
    """
    aliases = [project] if project else None
    schema = registry.mcp.get_tool_schema(tool_name=tool_name, aliases=aliases, branch_id=branch_id)
    return {"tool": tool_name, "input_schema": schema or {}}


@router.post("/tools/{tool_name}/call", summary="Call an MCP tool", deprecated=True)
def call_tool(
    tool_name: str, body: ToolCall, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Invoke one MCP tool. Input is validated against the tool's schema
    server-side before the MCP subprocess is spawned, so obvious mistakes
    fail fast with a 4xx instead of a cryptic MCP error.
    """
    return registry.mcp.validate_and_call_tool(
        tool_name=tool_name,
        tool_input=body.input,
        alias=body.project,
        branch_id=body.branch_id,
    )


@router.get("/server-status", summary="Check MCP server availability")
def server_status(registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    """Whether the bundled `keboola-mcp-server` subprocess is reachable
    (HTTP transport or stdio fallback, depending on env vars).
    """
    return registry.mcp.check_server_available()
