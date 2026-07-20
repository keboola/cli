"""MCP tool commands - list and call tools from keboola-mcp-server.

Thin CLI layer: parses arguments, calls McpService, formats output.
No business logic belongs here.

The whole ``tool`` group is DEPRECATED (epic #390 phase 2): every MCP tool
has a native kbagent command (see ``mcp_parity.MCP_TOOL_PARITY``). Both
subcommands surface the deprecation -- human mode on stderr, JSON mode via
an additive ``deprecation`` key -- without changing behavior.
"""

import json
import sys
from pathlib import Path

import typer

from ..config_store import ConfigStore
from ..errors import ConfigError, ErrorCode, PermissionDeniedError
from ..mcp_parity import deprecation_message, native_equivalent
from ..output import OutputFormatter, format_tool_result, format_tools_table
from ._helpers import (
    EXIT_PERMISSION_DENIED,
    check_cli_permission,
    emit_project_warnings,
    get_formatter,
    get_service,
    resolve_branch,
    validate_branch_requires_project,
)

tool_app = typer.Typer(help="MCP tools - interact with Keboola via MCP server")

# Generic deprecation banner for `tool list` (epic #390 phase 2). Per-tool
# messages for `tool call` come from mcp_parity.deprecation_message().
TOOL_LIST_DEPRECATION = (
    "The MCP passthrough is deprecated (epic #390); every tool has a native "
    "command -- see the cli_equivalent column."
)


@tool_app.callback(invoke_without_command=True)
def _tool_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "tool")


def _read_input(value: str, formatter: OutputFormatter) -> str:
    """Read tool input from a string, file (@path), or stdin (-).

    Supports:
    - Inline JSON string: '{"key": "value"}'
    - File reference: @payload.json or @/absolute/path.json
    - Stdin: -
    """
    if value == "-":
        return sys.stdin.read()

    if value.startswith("@"):
        file_path = Path(value[1:])
        if not file_path.is_file():
            formatter.error(
                message=f"Input file not found: {file_path}",
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
            raise typer.Exit(code=2) from None
        return file_path.read_text(encoding="utf-8")

    return value


def _resolve_branch_str(
    config_store: ConfigStore,
    formatter: OutputFormatter,
    project: str | None,
    branch: int | None,
) -> tuple[str | None, str | None]:
    """Resolve branch and return branch_id as string (for MCP service compatibility)."""
    proj, branch_id = resolve_branch(config_store, formatter, project, branch)
    return proj, str(branch_id) if branch_id is not None else None


@tool_app.command("list")
def tool_list(
    ctx: typer.Context,
    project: str | None = typer.Option(
        None,
        "--project",
        help="Project alias to query tools from (uses first available if not set)",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Development branch ID (requires --project or active branch)",
    ),
) -> None:
    """List available MCP tools from the keboola-mcp-server.

    DEPRECATED (epic #390): every tool maps to a native kbagent command --
    see the cli_equivalent column in the output.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "mcp_service")
    config_store: ConfigStore = ctx.obj["config_store"]

    validate_branch_requires_project(formatter, branch, project)

    # Auto-resolve active branch from config
    project, branch_str = _resolve_branch_str(config_store, formatter, project, branch)

    if branch_str and not project:
        formatter.error(
            message="--branch requires --project (branch ID is per-project)",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2) from None

    aliases = [project] if project else None

    try:
        result = service.list_tools(aliases=aliases, branch_id=branch_str)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    # Enrich each tool with its native CLI equivalent (epic #390). Done in the
    # command layer on purpose: the service dict stays byte-identical to what
    # the MCP server reports; cli_equivalent / deprecation are additive keys.
    for tool in result.get("tools", []):
        entry = native_equivalent(tool.get("name", ""))
        tool["cli_equivalent"] = entry.command if entry is not None else ""

    if formatter.json_mode:
        result["deprecation"] = TOOL_LIST_DEPRECATION
        formatter.output(result)
    else:
        format_tools_table(formatter.console, result)
        if result.get("tools"):
            formatter.console.print(f"[bold yellow]{TOOL_LIST_DEPRECATION}[/bold yellow]")
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
        help="Tool input as JSON string, @file.json, or - for stdin",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Development branch ID (forces single-project mode)",
    ),
) -> None:
    """Call an MCP tool on keboola-mcp-server.

    DEPRECATED (epic #390): prefer the native kbagent command for the tool
    (run `kbagent tool list` and read the cli_equivalent column).

    Read tools (list_*, get_*, search, docs_query, find_*) run across ALL
    connected projects in parallel and return aggregated results.

    Write tools (create_*, update_*, delete_*, add_*) run on a single project.
    Use --project to specify the target, or the default project is used.

    If an active branch is set for the project, it is used automatically.
    Use --branch to override or scope the call to a specific development branch.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "mcp_service")
    config_store: ConfigStore = ctx.obj["config_store"]

    # Per-tool session firewall check (issue #478): the group callback only
    # checks the coarse 'tool.call' operation, and the service-level check
    # sees just the PERSISTED policy -- so without this, a session-only
    # --deny-destructive would still let 'tool call delete_bucket' through.
    # classify_mcp_tool is fail-closed: unknown tool names count as
    # destructive and are blocked by --deny-writes / --deny-destructive.
    engine = ctx.obj.get("permission_engine")
    if engine is not None and engine.active:
        try:
            engine.check_or_raise(f"tool:{tool_name}")
        except PermissionDeniedError as exc:
            formatter.error(message=exc.message, error_code=ErrorCode.PERMISSION_DENIED)
            raise typer.Exit(code=EXIT_PERMISSION_DENIED) from None

    # Deprecation surface (epic #390): stderr warning in human mode (never
    # pollutes stdout); JSON mode injects the message into the success
    # envelope below instead (error paths carry no deprecation key).
    deprecation = deprecation_message(tool_name)
    formatter.warning(deprecation)

    validate_branch_requires_project(formatter, branch, project)

    # Auto-resolve active branch from config
    project, branch_str = _resolve_branch_str(config_store, formatter, project, branch)

    if branch_str and not project:
        formatter.error(
            message="--branch requires --project (branch ID is per-project)",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2) from None

    # Parse tool input JSON (supports inline JSON, @file.json, or - for stdin)
    parsed_input: dict = {}
    if tool_input:
        raw_json = _read_input(tool_input, formatter)
        try:
            parsed_input = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            formatter.error(
                message=f"Invalid JSON in --input: {exc}",
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
            raise typer.Exit(code=2) from None

        if not isinstance(parsed_input, dict):
            formatter.error(
                message='--input must be a JSON object (e.g. \'{"key": "value"}\')',
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
            raise typer.Exit(code=2) from None

    # Validate + call in a single MCP session (eliminates double subprocess spawn)
    try:
        result = service.validate_and_call_tool(
            tool_name=tool_name,
            tool_input=parsed_input,
            alias=project,
            branch_id=branch_str,
        )
    except ConfigError as exc:
        # ConfigError covers: unknown tool, missing params, config issues
        error_code = ErrorCode.CONFIG_ERROR
        exit_code = 5
        if "Missing required parameter" in exc.message:
            error_code = ErrorCode.MISSING_PARAMETER
            exit_code = 2
        elif "Unknown MCP tool" in exc.message:
            error_code = ErrorCode.CONFIG_ERROR
            exit_code = 5
        formatter.error(message=exc.message, error_code=error_code)
        raise typer.Exit(code=exit_code) from None

    if formatter.json_mode:
        result["deprecation"] = deprecation
        formatter.output(result)
    else:
        format_tool_result(formatter.console, result)
        emit_project_warnings(formatter, result)
