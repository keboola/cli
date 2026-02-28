"""MCP tool commands - list and call tools from keboola-mcp-server.

Thin CLI layer: parses arguments, calls McpService, formats output.
No business logic belongs here.
"""

import json

import typer

from ..errors import ConfigError
from ..output import format_tool_result, format_tools_table
from ._helpers import emit_project_warnings, get_formatter, get_service

tool_app = typer.Typer(help="MCP tools - interact with Keboola via MCP server")


@tool_app.command("list")
def tool_list(
    ctx: typer.Context,
    project: str | None = typer.Option(
        None,
        "--project",
        help="Project alias to query tools from (uses first available if not set)",
    ),
) -> None:
    """List available MCP tools from the keboola-mcp-server."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "mcp_service")

    aliases = [project] if project else None

    try:
        result = service.list_tools(aliases=aliases)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        format_tools_table(formatter.console, result)
        emit_project_warnings(formatter, result)


@tool_app.command("call")
def tool_call(
    ctx: typer.Context,
    tool_name: str = typer.Argument(help="Name of the MCP tool to call"),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Project alias (required for write tools, optional for read tools)",
    ),
    tool_input: str | None = typer.Option(
        None,
        "--input",
        help="Tool input as JSON string (e.g. '{\"query\": \"test\"}')",
    ),
) -> None:
    """Call an MCP tool on keboola-mcp-server.

    Read tools (list_*, get_*, search, docs_query, find_*) run across ALL
    connected projects in parallel and return aggregated results.

    Write tools (create_*, update_*, delete_*, add_*) run on a single project.
    Use --project to specify the target, or the default project is used.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "mcp_service")

    # Parse tool input JSON
    parsed_input: dict = {}
    if tool_input:
        try:
            parsed_input = json.loads(tool_input)
        except json.JSONDecodeError as exc:
            formatter.error(
                message=f"Invalid JSON in --input: {exc}",
                error_code="INVALID_ARGUMENT",
            )
            raise typer.Exit(code=2) from None

        if not isinstance(parsed_input, dict):
            formatter.error(
                message="--input must be a JSON object (e.g. '{\"key\": \"value\"}')",
                error_code="INVALID_ARGUMENT",
            )
            raise typer.Exit(code=2) from None

    # Validate required parameters before calling tool across all projects
    try:
        missing = service.validate_tool_input(
            tool_name=tool_name,
            tool_input=parsed_input,
            aliases=[project] if project else None,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None

    if missing:
        params_str = ", ".join(missing)
        example_json = json.dumps({p: "..." for p in missing})
        formatter.error(
            message=(
                f"Missing required parameter(s) for '{tool_name}': {params_str}. "
                f"Use: kbagent tool call {tool_name} --input '{example_json}'"
            ),
            error_code="MISSING_PARAMETER",
        )
        raise typer.Exit(code=2) from None

    try:
        result = service.call_tool(
            tool_name=tool_name,
            tool_input=parsed_input,
            alias=project,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        format_tool_result(formatter.console, result)
        emit_project_warnings(formatter, result)
