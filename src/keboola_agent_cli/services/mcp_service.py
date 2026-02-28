"""MCP integration service - wraps keboola-mcp-server as subprocess.

Provides multi-project tool listing and execution via MCP protocol.
Read tools run across ALL projects in parallel; write tools target a single project.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ..config_store import ConfigStore
from ..constants import (
    DEFAULT_MCP_INIT_TIMEOUT,
    DEFAULT_MCP_TOOL_TIMEOUT,
    ENV_MCP_INIT_TIMEOUT,
    ENV_MCP_TOOL_TIMEOUT,
)
from ..errors import ConfigError
from ..models import ProjectConfig
from .base import BaseService, ClientFactory

logger = logging.getLogger(__name__)

# Timeout for individual MCP operations (seconds)
MCP_TOOL_TIMEOUT_SECONDS = int(
    os.environ.get(ENV_MCP_TOOL_TIMEOUT, DEFAULT_MCP_TOOL_TIMEOUT)
)
# Timeout for MCP session initialization (seconds)
MCP_INIT_TIMEOUT_SECONDS = int(
    os.environ.get(ENV_MCP_INIT_TIMEOUT, DEFAULT_MCP_INIT_TIMEOUT)
)

# Prefixes that indicate write/mutating tools
WRITE_PREFIXES = (
    "create_",
    "update_",
    "delete_",
    "add_",
    "remove_",
    "set_",
)

# Tools that auto-expand when a required param is missing.
# Maps tool_name -> (missing_param, resolve_tool) pairs.
# When the param is not provided, the resolve_tool is called first,
# then the target tool is called once per result item.
AUTO_EXPAND_TOOLS = {
    "list_tables": {
        "param": "bucket_id",
        "resolve_tool": "list_buckets",
        "resolve_key": "id",
    },
}


def _is_write_tool(tool_name: str) -> bool:
    """Determine if a tool name indicates a write/mutating operation."""
    return tool_name.startswith(WRITE_PREFIXES)


def detect_mcp_server_command() -> list[str] | None:
    """Detect the best way to run keboola-mcp-server.

    Checks in order:
    1. uvx keboola_mcp_server (if uvx is available)
    2. keboola_mcp_server (if installed as standalone command)
    3. python -m keboola_mcp_server (last resort)

    Returns:
        List of command parts, or None if no method is available.
    """
    if shutil.which("uvx"):
        return ["uvx", "keboola_mcp_server"]
    if shutil.which("keboola_mcp_server"):
        return ["keboola_mcp_server"]
    if shutil.which("python"):
        return ["python", "-m", "keboola_mcp_server"]
    return None


def _build_server_params(project: ProjectConfig) -> StdioServerParameters:
    """Build StdioServerParameters for a project's MCP server.

    Args:
        project: Project config with stack_url and token.

    Returns:
        StdioServerParameters configured for the project.

    Raises:
        ConfigError: If no MCP server command is available.
    """
    command_parts = detect_mcp_server_command()
    if command_parts is None:
        raise ConfigError(
            "Cannot find keboola-mcp-server. "
            "Install it with: pip install keboola-mcp-server (or: uvx keboola_mcp_server)"
        )

    return StdioServerParameters(
        command=command_parts[0],
        args=[*command_parts[1:], "--transport", "stdio"],
        env={
            "KBC_STORAGE_TOKEN": project.token,
            "KBC_STORAGE_API_URL": project.stack_url,
        },
    )


async def _connect_and_list_tools(
    project: ProjectConfig,
) -> list[dict[str, Any]]:
    """Connect to MCP server for a project and list available tools.

    Args:
        project: Project config.

    Returns:
        List of tool dicts with name, description, inputSchema.
    """
    params = _build_server_params(project)
    exit_stack = AsyncExitStack()

    logger.info("Starting MCP server for project %s", project.project_name or "unknown")
    try:
        read_stream, write_stream = await asyncio.wait_for(
            exit_stack.enter_async_context(
                stdio_client(params, errlog=subprocess.DEVNULL)
            ),
            timeout=MCP_INIT_TIMEOUT_SECONDS,
        )

        session = await exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await asyncio.wait_for(session.initialize(), timeout=MCP_INIT_TIMEOUT_SECONDS)

        response = await asyncio.wait_for(
            session.list_tools(), timeout=MCP_TOOL_TIMEOUT_SECONDS
        )

        tools = []
        for tool in response.tools:
            tools.append(
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "inputSchema": tool.inputSchema if tool.inputSchema else {},
                }
            )
        logger.info("MCP server listed %d tools", len(tools))
        return tools
    finally:
        logger.info("Closing MCP server session")
        await exit_stack.aclose()


def _parse_content(result: Any) -> list[Any]:
    """Parse MCP tool result content items into Python objects."""
    content_items = []
    for item in result.content:
        if hasattr(item, "text"):
            try:
                content_items.append(json.loads(item.text))
            except (json.JSONDecodeError, TypeError):
                content_items.append(item.text)
        else:
            content_items.append(str(item))
    return content_items


async def _open_session(
    project: ProjectConfig,
    exit_stack: AsyncExitStack,
) -> "ClientSession":
    """Open an MCP session for a project, managed by the given exit stack."""
    logger.info("Opening MCP session for project %s", project.project_name or "unknown")
    params = _build_server_params(project)

    read_stream, write_stream = await asyncio.wait_for(
        exit_stack.enter_async_context(
            stdio_client(params, errlog=subprocess.DEVNULL)
        ),
        timeout=MCP_INIT_TIMEOUT_SECONDS,
    )

    session = await exit_stack.enter_async_context(
        ClientSession(read_stream, write_stream)
    )
    await asyncio.wait_for(session.initialize(), timeout=MCP_INIT_TIMEOUT_SECONDS)
    return session


async def _connect_and_call_tool(
    project: ProjectConfig,
    tool_name: str,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    """Connect to MCP server for a project and call a specific tool.

    Args:
        project: Project config.
        tool_name: Name of the tool to call.
        tool_input: Input arguments for the tool.

    Returns:
        Dict with tool result content and error status.
    """
    exit_stack = AsyncExitStack()

    try:
        session = await _open_session(project, exit_stack)

        result = await asyncio.wait_for(
            session.call_tool(tool_name, tool_input),
            timeout=MCP_TOOL_TIMEOUT_SECONDS,
        )

        return {
            "content": _parse_content(result),
            "isError": bool(result.isError),
        }
    finally:
        await exit_stack.aclose()


async def _connect_and_auto_expand(
    project: ProjectConfig,
    tool_name: str,
    tool_input: dict[str, Any],
    expand_config: dict[str, str],
) -> dict[str, Any]:
    """Connect to MCP server and auto-expand a tool call.

    First calls the resolve_tool to get a list of items, then calls
    the target tool for each item, reusing one MCP session.

    Args:
        project: Project config.
        tool_name: Target tool name (e.g. "list_tables").
        tool_input: Base input for the target tool (without the auto-expanded param).
        expand_config: Dict with "param", "resolve_tool", "resolve_key".

    Returns:
        Dict with aggregated content and error status.
    """
    resolve_tool = expand_config["resolve_tool"]
    resolve_key = expand_config["resolve_key"]
    param_name = expand_config["param"]

    exit_stack = AsyncExitStack()

    try:
        session = await _open_session(project, exit_stack)

        # Step 1: Call resolve tool (e.g. list_buckets)
        resolve_result = await asyncio.wait_for(
            session.call_tool(resolve_tool, {}),
            timeout=MCP_TOOL_TIMEOUT_SECONDS,
        )

        if resolve_result.isError:
            return {
                "content": _parse_content(resolve_result),
                "isError": True,
            }

        # Extract IDs from resolve result
        resolve_items = _parse_content(resolve_result)
        item_ids = _extract_ids(resolve_items, resolve_key)

        if not item_ids:
            return {"content": [], "isError": False}

        # Step 2: Call target tool for each resolved ID
        all_content: list[Any] = []
        has_error = False

        for item_id in item_ids:
            call_input = {**tool_input, param_name: item_id}
            result = await asyncio.wait_for(
                session.call_tool(tool_name, call_input),
                timeout=MCP_TOOL_TIMEOUT_SECONDS,
            )

            content = _parse_content(result)
            if result.isError:
                has_error = True
            all_content.extend(content)

        return {"content": all_content, "isError": has_error}
    finally:
        await exit_stack.aclose()


def _extract_ids(content_items: list[Any], key: str) -> list[str]:
    """Extract unique ID values from parsed MCP tool content.

    Handles both list-of-dicts format and single-dict-with-list format.
    Deduplicates while preserving insertion order.
    """
    ids = []
    for item in content_items:
        if isinstance(item, list):
            for sub in item:
                if isinstance(sub, dict) and key in sub:
                    ids.append(str(sub[key]))
        elif isinstance(item, dict):
            if key in item:
                ids.append(str(item[key]))
            # Handle nested list in a single response dict
            for value in item.values():
                if isinstance(value, list):
                    for sub in value:
                        if isinstance(sub, dict) and key in sub:
                            ids.append(str(sub[key]))
    return list(dict.fromkeys(ids))


class McpService(BaseService):
    """Business logic for MCP tool operations across projects.

    Wraps keboola-mcp-server as subprocess via MCP SDK.
    Read tools execute across all projects in parallel.
    Write tools target a single project.

    Uses the same DI pattern as JobService/ConfigService.
    """

    def resolve_project(self, alias: str | None = None) -> tuple[str, ProjectConfig]:
        """Resolve a single project alias (or the default project).

        Args:
            alias: Project alias. If None, uses default_project.

        Returns:
            Tuple of (alias, ProjectConfig).

        Raises:
            ConfigError: If alias not found or no default project set.
        """
        config = self._config_store.load()

        if alias is None:
            alias = config.default_project
            if not alias:
                raise ConfigError(
                    "No default project set. Use 'kbagent project add' or specify --project."
                )

        if alias not in config.projects:
            raise ConfigError(f"Project '{alias}' not found.")

        return alias, config.projects[alias]

    def list_tools(
        self, aliases: list[str] | None = None
    ) -> dict[str, Any]:
        """List available MCP tools from the first reachable project.

        Tools are the same across projects (same MCP server), so we only
        need to query one project. Each tool is annotated with multi_project
        flag based on read/write classification.

        Args:
            aliases: Project aliases. Uses first available if None.

        Returns:
            Dict with "tools" list and "errors" list.

        Raises:
            ConfigError: If no projects are configured.
        """
        projects = self.resolve_projects(aliases)

        if not projects:
            raise ConfigError("No projects configured. Use 'kbagent project add' first.")

        # Try each project until one succeeds
        errors: list[dict[str, str]] = []
        for alias, project in projects.items():
            try:
                tools = asyncio.run(_connect_and_list_tools(project))
                # Annotate tools with multi_project flag
                annotated_tools = []
                for tool in tools:
                    tool["multi_project"] = not _is_write_tool(tool["name"])
                    annotated_tools.append(tool)

                return {"tools": annotated_tools, "errors": errors}
            except Exception as exc:
                errors.append(
                    {
                        "project_alias": alias,
                        "error_code": "MCP_ERROR",
                        "message": f"Failed to list tools: {exc}",
                    }
                )

        return {"tools": [], "errors": errors}

    def get_tool_schema(
        self, tool_name: str, aliases: list[str] | None = None
    ) -> dict[str, Any] | None:
        """Get the input schema for a specific tool.

        Fetches tool list from the first available project and finds the
        matching tool's inputSchema.

        Args:
            tool_name: Name of the tool.
            aliases: Project aliases to try.

        Returns:
            The tool's inputSchema dict, or None if tool not found.
        """
        result = self.list_tools(aliases=aliases)
        for tool in result.get("tools", []):
            if tool["name"] == tool_name:
                return tool.get("inputSchema", {})
        return None

    def validate_tool_input(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        aliases: list[str] | None = None,
    ) -> list[str]:
        """Validate tool input against the tool's schema.

        Checks that all required parameters are provided.
        Parameters that can be auto-expanded (see AUTO_EXPAND_TOOLS) are
        excluded from the missing list.

        Args:
            tool_name: Name of the tool.
            tool_input: Input arguments to validate.
            aliases: Project aliases to try for schema lookup.

        Returns:
            List of missing required parameter names. Empty if all OK.
        """
        schema = self.get_tool_schema(tool_name, aliases=aliases)
        if schema is None:
            return []  # Tool not found; call_tool will raise ConfigError

        required = schema.get("required", [])
        missing = [param for param in required if param not in tool_input]

        # Exclude auto-expandable params from missing list
        expand_config = AUTO_EXPAND_TOOLS.get(tool_name)
        if expand_config:
            auto_param = expand_config["param"]
            missing = [p for p in missing if p != auto_param]

        return missing

    def call_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
        alias: str | None = None,
    ) -> dict[str, Any]:
        """Call an MCP tool.

        For read tools (multi_project=true): runs across ALL projects in parallel,
        aggregates results with project_alias annotation.

        For write tools: runs on a single project (specified by alias or default).

        Args:
            tool_name: Name of the MCP tool to call.
            tool_input: Input arguments for the tool.
            alias: Project alias for write tools. Ignored for read tools
                   unless specified to limit scope.

        Returns:
            Dict with "results" list and "errors" list.

        Raises:
            ConfigError: If tool_name is not found in the available tool list.
        """
        if tool_input is None:
            tool_input = {}

        # Validate tool name exists in the MCP tool list
        tool_list_result = self.list_tools(aliases=[alias] if alias else None)
        known_tools = {t["name"] for t in tool_list_result.get("tools", [])}
        if known_tools and tool_name not in known_tools:
            raise ConfigError(
                f"Unknown MCP tool '{tool_name}'. "
                f"Use 'kbagent tool list' to see available tools."
            )

        is_write = _is_write_tool(tool_name)

        if is_write:
            return self._call_write_tool(tool_name, tool_input, alias)
        else:
            return self._call_read_tool(tool_name, tool_input, alias)

    def _call_write_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        alias: str | None,
    ) -> dict[str, Any]:
        """Execute a write tool on a single project."""
        resolved_alias, project = self.resolve_project(alias)

        try:
            result = asyncio.run(_connect_and_call_tool(project, tool_name, tool_input))
            result["project_alias"] = resolved_alias
            return {"results": [result], "errors": []}
        except Exception as exc:
            return {
                "results": [],
                "errors": [
                    {
                        "project_alias": resolved_alias,
                        "error_code": "MCP_ERROR",
                        "message": str(exc),
                    }
                ],
            }

    def _call_read_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        alias: str | None,
    ) -> dict[str, Any]:
        """Execute a read tool across projects in parallel.

        If the tool is in AUTO_EXPAND_TOOLS and the required param is missing,
        automatically resolves it by calling the resolve tool first.
        """
        projects = self.resolve_projects([alias]) if alias else self.resolve_projects()

        if not projects:
            raise ConfigError("No projects configured. Use 'kbagent project add' first.")

        # Check if auto-expand is needed
        expand_config = AUTO_EXPAND_TOOLS.get(tool_name)
        if expand_config and expand_config["param"] not in tool_input:
            return asyncio.run(
                self._gather_auto_expand_results(
                    projects, tool_name, tool_input, expand_config
                )
            )

        return asyncio.run(self._gather_read_results(projects, tool_name, tool_input))

    @staticmethod
    async def _gather_results(
        tasks: dict[str, "asyncio.Task[dict[str, Any]]"],
    ) -> dict[str, Any]:
        """Gather results from async tasks using asyncio.gather.

        Shared helper for both read and auto-expand gather operations.
        Uses asyncio.gather with return_exceptions=True for true concurrency.

        Args:
            tasks: Dict mapping project alias to asyncio.Task.

        Returns:
            Dict with "results" list and "errors" list.
        """
        aliases = list(tasks.keys())
        outcomes = await asyncio.gather(*tasks.values(), return_exceptions=True)

        all_results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []

        for alias, outcome in zip(aliases, outcomes):
            if isinstance(outcome, BaseException):
                errors.append(
                    {
                        "project_alias": alias,
                        "error_code": "MCP_ERROR",
                        "message": str(outcome),
                    }
                )
            else:
                outcome["project_alias"] = alias
                all_results.append(outcome)

        return {"results": all_results, "errors": errors}

    async def _gather_auto_expand_results(
        self,
        projects: dict[str, ProjectConfig],
        tool_name: str,
        tool_input: dict[str, Any],
        expand_config: dict[str, str],
    ) -> dict[str, Any]:
        """Run an auto-expanded tool across multiple projects in parallel.

        For each project, opens one MCP session, resolves the missing param
        by calling the resolve tool, then calls the target tool per item.
        """
        tasks = {}
        for a, project in projects.items():
            tasks[a] = asyncio.create_task(
                _connect_and_auto_expand(project, tool_name, tool_input, expand_config)
            )
        return await self._gather_results(tasks)

    async def _gather_read_results(
        self,
        projects: dict[str, ProjectConfig],
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> dict[str, Any]:
        """Run a read tool across multiple projects in parallel using asyncio.gather."""
        tasks = {}
        for a, project in projects.items():
            tasks[a] = asyncio.create_task(
                _connect_and_call_tool(project, tool_name, tool_input)
            )
        return await self._gather_results(tasks)

    def check_server_available(self) -> dict[str, Any]:
        """Check if MCP server is available (for doctor command).

        Returns:
            Dict with check status and message.
        """
        command = detect_mcp_server_command()
        if command is None:
            return {
                "check": "mcp_server",
                "name": "MCP server",
                "status": "warn",
                "message": (
                    "keboola-mcp-server not found. "
                    "Install with: pip install keboola-mcp-server "
                    "(or use uvx keboola_mcp_server). "
                    "MCP tool commands will not work without it."
                ),
            }

        return {
            "check": "mcp_server",
            "name": "MCP server",
            "status": "pass",
            "message": f"MCP server available via: {' '.join(command)}",
        }
