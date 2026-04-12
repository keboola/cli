"""Hint definitions for MCP tool commands (list, call)."""

from .. import HintRegistry
from ..models import ClientCall, CommandHint, HintStep, ServiceCall

# ── tool list ──────────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="tool.list",
        description="List available MCP tools from keboola-mcp-server",
        steps=[
            HintStep(
                comment="List MCP tools",
                client=ClientCall(
                    method="list_tools",
                    args={"aliases": "{project}", "branch_id": "{branch}"},
                    client_type="mcp",
                    result_var="tools",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="McpService",
                    service_module="mcp_service",
                    method="list_tools",
                    args={"aliases": "{project}", "branch_id": "{branch}"},
                ),
            ),
        ],
        notes=[
            "MCP tools are served by keboola-mcp-server (installed separately).",
            "Client layer: use McpService directly (there is no raw HTTP equivalent).",
            "Tools are the same across projects — only one project is queried.",
        ],
    )
)

# ── tool call ──────────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="tool.call",
        description="Call an MCP tool on keboola-mcp-server",
        steps=[
            HintStep(
                comment="Validate and call MCP tool",
                client=ClientCall(
                    method="validate_and_call_tool",
                    args={
                        "tool_name": "{tool_name}",
                        "tool_input": "{input}",
                        "alias": "{project}",
                        "branch_id": "{branch}",
                    },
                    client_type="mcp",
                    result_var="result",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="McpService",
                    service_module="mcp_service",
                    method="validate_and_call_tool",
                    args={
                        "tool_name": "{tool_name}",
                        "tool_input": "{input}",
                        "alias": "{project}",
                        "branch_id": "{branch}",
                    },
                ),
            ),
        ],
        notes=[
            "MCP tools are served by keboola-mcp-server (installed separately).",
            "Client layer: use McpService directly (there is no raw HTTP equivalent).",
            "Read tools run across ALL projects in parallel.",
            "Write tools require --project to specify the target.",
        ],
    )
)
