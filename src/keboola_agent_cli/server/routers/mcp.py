"""MCP tool endpoints (list, call across projects)."""

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


@router.get("/tools")
def list_tools(
    project: str | None = None,
    branch_id: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    aliases = [project] if project else None
    return registry.mcp.list_tools(aliases=aliases, branch_id=branch_id)


@router.get("/tools/{tool_name}/schema")
def tool_schema(
    tool_name: str,
    project: str | None = None,
    branch_id: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    aliases = [project] if project else None
    schema = registry.mcp.get_tool_schema(tool_name=tool_name, aliases=aliases, branch_id=branch_id)
    return {"tool": tool_name, "input_schema": schema or {}}


@router.post("/tools/{tool_name}/call")
def call_tool(
    tool_name: str, body: ToolCall, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    return registry.mcp.validate_and_call_tool(
        tool_name=tool_name,
        tool_input=body.input,
        alias=body.project,
        branch_id=body.branch_id,
    )


@router.get("/server-status")
def server_status(registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    return registry.mcp.check_server_available()
