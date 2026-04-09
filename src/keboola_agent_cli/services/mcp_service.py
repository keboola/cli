"""MCP integration service - wraps keboola-mcp-server.

Provides multi-project tool listing and execution via MCP protocol.
Read tools run across ALL projects in parallel; write tools target a single project.

Supports two transport modes:
- HTTP (default): Persistent server with per-request credentials via headers.
  One server serves all projects. Fastest for repeated calls.
- stdio: Subprocess per call. Fallback when HTTP transport is unavailable.
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
from mcp.client.streamable_http import streamablehttp_client

from ..constants import (
    DEFAULT_MCP_INIT_TIMEOUT,
    DEFAULT_MCP_MAX_SESSIONS,
    DEFAULT_MCP_TOOL_TIMEOUT,
    DEFAULT_MCP_TRANSPORT,
    ENV_CONVERSATION_ID,
    ENV_MCP_INIT_TIMEOUT,
    ENV_MCP_MAX_SESSIONS,
    ENV_MCP_TOOL_TIMEOUT,
    ENV_MCP_TRANSPORT,
)
from ..errors import ConfigError
from ..models import ProjectConfig
from ..permissions import PermissionEngine
from .base import BaseService

logger = logging.getLogger(__name__)


def _get_tool_timeout() -> int:
    """Get MCP tool timeout (seconds), reading env var at call time."""
    return int(os.environ.get(ENV_MCP_TOOL_TIMEOUT, DEFAULT_MCP_TOOL_TIMEOUT))


def _get_init_timeout() -> int:
    """Get MCP init timeout (seconds), reading env var at call time."""
    return int(os.environ.get(ENV_MCP_INIT_TIMEOUT, DEFAULT_MCP_INIT_TIMEOUT))


def _get_max_sessions() -> int:
    """Get max concurrent MCP sessions, reading env var at call time."""
    return int(os.environ.get(ENV_MCP_MAX_SESSIONS, DEFAULT_MCP_MAX_SESSIONS))


async def _semaphored(sem: asyncio.Semaphore, coro: Any) -> Any:
    """Run a coroutine under a semaphore to limit concurrency."""
    async with sem:
        return await coro


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
# Maps tool_name -> config dict. When the param is absent from user input,
# the resolve_tool is called first to collect IDs, then the target tool
# is called once with all resolved IDs as an array.
AUTO_EXPAND_TOOLS = {
    "get_tables": {
        "param": "bucket_ids",
        "resolve_tool": "get_buckets",
        "resolve_key": "id",
    },
}


def _is_write_tool(tool_name: str) -> bool:
    """Determine if a tool name indicates a write/mutating operation."""
    return tool_name.startswith(WRITE_PREFIXES)


def detect_mcp_server_command() -> list[str] | None:
    """Detect the best way to run keboola-mcp-server.

    Checks in order of speed (fastest first):
    1. keboola_mcp_server (local install or uv tool, ~1s startup)
    2. python -m keboola_mcp_server (installed in current env)
    3. uvx keboola_mcp_server (cached version, ~1s cached / ~4.5s uncached)

    Note: We intentionally do NOT use @latest with uvx because it forces
    a PyPI check on every invocation (~25s penalty). The cached version
    is used instead. Users can update manually with: uvx upgrade keboola_mcp_server

    Returns:
        List of command parts, or None if no method is available.
    """
    # 1. Local install or uv tool install (fastest: ~1s)
    if shutil.which("keboola_mcp_server"):
        return ["keboola_mcp_server"]
    # 2. python -m (if installed in current env)
    if shutil.which("python"):
        result = subprocess.run(
            ["python", "-c", "import keboola_mcp_server"],
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0:
            return ["python", "-m", "keboola_mcp_server"]
    # 3. uvx WITHOUT @latest (uses cached version: ~1s cached / ~4.5s uncached)
    if shutil.which("uvx"):
        return ["uvx", "--prerelease=allow", "keboola_mcp_server"]
    return None


def ensure_mcp_installed() -> dict[str, Any]:
    """Ensure keboola-mcp-server is installed as a fast local binary.

    If the binary is not directly available but uv is present, runs
    `uv tool install` to create a permanent binary in ~/.local/bin/.
    This eliminates the uvx per-call overhead (~0.2-4.5s).

    Returns:
        Dict with status info: method, installed (bool), message.
    """
    # Already available as direct binary
    if shutil.which("keboola_mcp_server"):
        return {
            "method": "binary",
            "installed": False,
            "message": "keboola_mcp_server already available in PATH",
        }

    # Already available as python module
    if shutil.which("python"):
        result = subprocess.run(
            ["python", "-c", "import keboola_mcp_server"],
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0:
            return {
                "method": "python_module",
                "installed": False,
                "message": "keboola_mcp_server available as Python module",
            }

    # Try uv tool install (creates permanent binary, ~1s startup vs ~4.5s uvx)
    if shutil.which("uv"):
        try:
            result = subprocess.run(
                ["uv", "tool", "install", "--prerelease=allow", "keboola-mcp-server"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                return {
                    "method": "uv_tool_install",
                    "installed": True,
                    "message": "Installed keboola-mcp-server via uv tool install",
                }
            # If already installed, uv tool install returns error
            if "already installed" in result.stderr.lower():
                return {
                    "method": "uv_tool_existing",
                    "installed": False,
                    "message": "keboola-mcp-server already installed via uv tool",
                }
            logger.warning("uv tool install failed: %s", result.stderr.strip())
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("uv tool install error: %s", exc)

    # Fall back to uvx availability check
    if shutil.which("uvx"):
        return {
            "method": "uvx_fallback",
            "installed": False,
            "message": "Using uvx (slower). Run 'uv tool install --prerelease=allow keboola-mcp-server' for faster startup",
        }

    return {
        "method": "not_found",
        "installed": False,
        "message": "keboola-mcp-server not found. Install with: pip install keboola-mcp-server",
    }


def _build_server_params(
    project: ProjectConfig,
    branch_id: str | None = None,
) -> StdioServerParameters:
    """Build StdioServerParameters for a project's MCP server.

    Args:
        project: Project config with stack_url and token.
        branch_id: Optional development branch ID to scope the MCP session.

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

    env: dict[str, str] = {
        "KBC_STORAGE_TOKEN": project.token,
        "KBC_STORAGE_API_URL": project.stack_url,
    }
    if branch_id is not None:
        env["KBC_BRANCH_ID"] = branch_id

    return StdioServerParameters(
        command=command_parts[0],
        args=[*command_parts[1:], "--transport", "stdio"],
        env=env,
    )


async def _connect_and_list_tools(
    project: ProjectConfig,
    branch_id: str | None = None,
) -> list[dict[str, Any]]:
    """Connect to MCP server for a project and list available tools.

    Args:
        project: Project config.
        branch_id: Optional development branch ID.

    Returns:
        List of tool dicts with name, description, inputSchema.
    """
    params = _build_server_params(project, branch_id=branch_id)
    exit_stack = AsyncExitStack()

    logger.info("Starting MCP server for project %s", project.project_name or "unknown")
    try:
        read_stream, write_stream = await asyncio.wait_for(
            exit_stack.enter_async_context(stdio_client(params, errlog=subprocess.DEVNULL)),
            timeout=_get_init_timeout(),
        )

        session = await exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
        await asyncio.wait_for(session.initialize(), timeout=_get_init_timeout())

        response = await asyncio.wait_for(session.list_tools(), timeout=_get_tool_timeout())

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
    branch_id: str | None = None,
) -> "ClientSession":
    """Open an MCP session for a project, managed by the given exit stack."""
    logger.info("Opening MCP session for project %s", project.project_name or "unknown")
    params = _build_server_params(project, branch_id=branch_id)

    read_stream, write_stream = await asyncio.wait_for(
        exit_stack.enter_async_context(stdio_client(params, errlog=subprocess.DEVNULL)),
        timeout=_get_init_timeout(),
    )

    session = await exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
    await asyncio.wait_for(session.initialize(), timeout=_get_init_timeout())
    return session


async def _connect_and_call_tool(
    project: ProjectConfig,
    tool_name: str,
    tool_input: dict[str, Any],
    branch_id: str | None = None,
) -> dict[str, Any]:
    """Connect to MCP server for a project and call a specific tool.

    Args:
        project: Project config.
        tool_name: Name of the tool to call.
        tool_input: Input arguments for the tool.
        branch_id: Optional development branch ID.

    Returns:
        Dict with tool result content and error status.
    """
    exit_stack = AsyncExitStack()

    try:
        session = await _open_session(project, exit_stack, branch_id=branch_id)

        result = await asyncio.wait_for(
            session.call_tool(tool_name, tool_input),
            timeout=_get_tool_timeout(),
        )

        return {
            "content": _parse_content(result),
            "isError": bool(result.isError),
        }
    finally:
        await exit_stack.aclose()


async def _connect_validate_and_call(
    project: ProjectConfig,
    tool_name: str,
    tool_input: dict[str, Any],
    branch_id: str | None = None,
) -> dict[str, Any]:
    """Open ONE MCP session: validate tool name + schema, then call the tool.

    Eliminates the need for a separate validate_tool_input() + call_tool()
    sequence, saving one full subprocess spawn.

    Args:
        project: Project config.
        tool_name: Name of the MCP tool to call.
        tool_input: Input arguments for the tool.
        branch_id: Optional development branch ID.

    Returns:
        Dict with "content", "isError", and "tool_schema" keys.

    Raises:
        ConfigError: If tool_name is not found in the available tool list.
        ConfigError: If required parameters are missing (not auto-expandable).
    """
    exit_stack = AsyncExitStack()

    try:
        session = await _open_session(project, exit_stack, branch_id=branch_id)

        # Step 1: list_tools to validate tool name and get schema
        response = await asyncio.wait_for(session.list_tools(), timeout=_get_tool_timeout())

        known_tools = {t.name for t in response.tools}
        if tool_name not in known_tools:
            raise ConfigError(
                f"Unknown MCP tool '{tool_name}'. Use 'kbagent tool list' to see available tools."
            )

        # Step 2: validate required params
        schema: dict[str, Any] = {}
        for tool in response.tools:
            if tool.name == tool_name:
                schema = tool.inputSchema if tool.inputSchema else {}
                break

        required = schema.get("required", [])
        missing = [param for param in required if param not in tool_input]

        # Exclude auto-expandable params from missing list
        expand_config = AUTO_EXPAND_TOOLS.get(tool_name)
        if expand_config:
            auto_param = expand_config["param"]
            missing = [p for p in missing if p != auto_param]

        if missing:
            import json as _json

            params_str = ", ".join(missing)
            example_json = _json.dumps({p: "..." for p in missing})
            raise ConfigError(
                f"Missing required parameter(s) for '{tool_name}': {params_str}. "
                f"Use: kbagent tool call {tool_name} --input '{example_json}'"
            )

        # Step 3: call the tool in the same session
        result = await asyncio.wait_for(
            session.call_tool(tool_name, tool_input),
            timeout=_get_tool_timeout(),
        )

        return {
            "content": _parse_content(result),
            "isError": bool(result.isError),
        }
    finally:
        await exit_stack.aclose()


def _get_transport_mode() -> str:
    """Get configured MCP transport mode ('http' or 'stdio')."""
    return os.environ.get(ENV_MCP_TRANSPORT, DEFAULT_MCP_TRANSPORT)


def _build_http_headers(
    project: ProjectConfig,
    branch_id: str | None = None,
) -> dict[str, str]:
    """Build HTTP headers for per-request project credentials."""
    headers = {
        "X-Storage-Token": project.token,
        "X-Storage-API-URL": project.stack_url,
    }
    if branch_id:
        headers["X-Branch-ID"] = branch_id
    conversation_id = os.environ.get(ENV_CONVERSATION_ID, "")
    if conversation_id:
        headers["X-Conversation-ID"] = conversation_id
    return headers


async def _http_list_tools(
    base_url: str,
    project: ProjectConfig,
    branch_id: str | None = None,
) -> list[dict[str, Any]]:
    """List tools via HTTP transport (persistent server).

    Args:
        base_url: Base URL of the persistent MCP server.
        project: Project config for authentication headers.
        branch_id: Optional development branch ID.

    Returns:
        List of tool dicts with name, description, inputSchema.
    """
    headers = _build_http_headers(project, branch_id)
    url = f"{base_url}/mcp"

    async with streamablehttp_client(url=url, headers=headers) as (
        read_stream,
        write_stream,
        _,
    ):
        session = ClientSession(read_stream, write_stream)
        async with session:
            await asyncio.wait_for(session.initialize(), timeout=_get_init_timeout())

            response = await asyncio.wait_for(session.list_tools(), timeout=_get_tool_timeout())

            tools = []
            for tool in response.tools:
                tools.append(
                    {
                        "name": tool.name,
                        "description": tool.description or "",
                        "inputSchema": tool.inputSchema if tool.inputSchema else {},
                    }
                )
            return tools


async def _http_call_tool(
    base_url: str,
    project: ProjectConfig,
    tool_name: str,
    tool_input: dict[str, Any],
    branch_id: str | None = None,
) -> dict[str, Any]:
    """Call a tool via HTTP transport (persistent server).

    Args:
        base_url: Base URL of the persistent MCP server.
        project: Project config for authentication headers.
        tool_name: Name of the tool to call.
        tool_input: Input arguments for the tool.
        branch_id: Optional development branch ID.

    Returns:
        Dict with tool result content and error status.
    """
    headers = _build_http_headers(project, branch_id)
    url = f"{base_url}/mcp"

    async with streamablehttp_client(url=url, headers=headers) as (
        read_stream,
        write_stream,
        _,
    ):
        session = ClientSession(read_stream, write_stream)
        async with session:
            await asyncio.wait_for(session.initialize(), timeout=_get_init_timeout())

            result = await asyncio.wait_for(
                session.call_tool(tool_name, tool_input),
                timeout=_get_tool_timeout(),
            )

            return {
                "content": _parse_content(result),
                "isError": bool(result.isError),
            }


async def _http_validate_and_call(
    base_url: str,
    project: ProjectConfig,
    tool_name: str,
    tool_input: dict[str, Any],
    branch_id: str | None = None,
) -> dict[str, Any]:
    """Validate and call a tool in one HTTP session (persistent server).

    Args:
        base_url: Base URL of the persistent MCP server.
        project: Project config for authentication headers.
        tool_name: Name of the MCP tool to call.
        tool_input: Input arguments for the tool.
        branch_id: Optional development branch ID.

    Returns:
        Dict with "content" and "isError" keys.

    Raises:
        ConfigError: If tool_name not found or required params missing.
    """
    headers = _build_http_headers(project, branch_id)
    url = f"{base_url}/mcp"

    async with streamablehttp_client(url=url, headers=headers) as (
        read_stream,
        write_stream,
        _,
    ):
        session = ClientSession(read_stream, write_stream)
        async with session:
            await asyncio.wait_for(session.initialize(), timeout=_get_init_timeout())

            # Step 1: list_tools for validation
            response = await asyncio.wait_for(session.list_tools(), timeout=_get_tool_timeout())

            known_tools = {t.name for t in response.tools}
            if tool_name not in known_tools:
                raise ConfigError(
                    f"Unknown MCP tool '{tool_name}'. "
                    f"Use 'kbagent tool list' to see available tools."
                )

            # Step 2: validate required params
            schema: dict[str, Any] = {}
            for tool in response.tools:
                if tool.name == tool_name:
                    schema = tool.inputSchema if tool.inputSchema else {}
                    break

            required = schema.get("required", [])
            missing = [param for param in required if param not in tool_input]

            expand_config = AUTO_EXPAND_TOOLS.get(tool_name)
            if expand_config:
                auto_param = expand_config["param"]
                missing = [p for p in missing if p != auto_param]

            if missing:
                import json as _json

                params_str = ", ".join(missing)
                example_json = _json.dumps({p: "..." for p in missing})
                raise ConfigError(
                    f"Missing required parameter(s) for '{tool_name}': {params_str}. "
                    f"Use: kbagent tool call {tool_name} --input '{example_json}'"
                )

            # Step 3: call the tool
            result = await asyncio.wait_for(
                session.call_tool(tool_name, tool_input),
                timeout=_get_tool_timeout(),
            )

            return {
                "content": _parse_content(result),
                "isError": bool(result.isError),
            }


async def _http_auto_expand(
    base_url: str,
    project: ProjectConfig,
    tool_name: str,
    tool_input: dict[str, Any],
    expand_config: dict[str, str],
    branch_id: str | None = None,
) -> dict[str, Any]:
    """Auto-expand a tool call via HTTP transport (persistent server).

    Same logic as _connect_and_auto_expand but over HTTP.
    """
    resolve_tool = expand_config["resolve_tool"]
    resolve_key = expand_config["resolve_key"]
    param_name = expand_config["param"]

    headers = _build_http_headers(project, branch_id)
    url = f"{base_url}/mcp"

    async with streamablehttp_client(url=url, headers=headers) as (
        read_stream,
        write_stream,
        _,
    ):
        session = ClientSession(read_stream, write_stream)
        async with session:
            await asyncio.wait_for(session.initialize(), timeout=_get_init_timeout())

            # Step 1: Call resolve tool
            resolve_result = await asyncio.wait_for(
                session.call_tool(resolve_tool, {}),
                timeout=_get_tool_timeout(),
            )

            if resolve_result.isError:
                return {
                    "content": _parse_content(resolve_result),
                    "isError": True,
                }

            resolve_items = _parse_content(resolve_result)
            item_ids = _extract_ids(resolve_items, resolve_key)

            if not item_ids:
                return {"content": [], "isError": False}

            # Step 2: Call target tool with all resolved IDs as array
            call_input = {**tool_input, param_name: list(item_ids)}
            result = await asyncio.wait_for(
                session.call_tool(tool_name, call_input),
                timeout=_get_tool_timeout(),
            )

            return {
                "content": _parse_content(result),
                "isError": bool(result.isError),
            }


async def _connect_and_auto_expand(
    project: ProjectConfig,
    tool_name: str,
    tool_input: dict[str, Any],
    expand_config: dict[str, str],
    branch_id: str | None = None,
) -> dict[str, Any]:
    """Connect to MCP server and auto-expand a tool call.

    First calls the resolve_tool to get a list of items, then calls
    the target tool for each item, reusing one MCP session.

    Args:
        project: Project config.
        tool_name: Target tool name (e.g. "get_tables").
        tool_input: Base input for the target tool (without the auto-expanded param).
        expand_config: Dict with "param", "resolve_tool", "resolve_key".
        branch_id: Optional development branch ID.

    Returns:
        Dict with aggregated content and error status.
    """
    resolve_tool = expand_config["resolve_tool"]
    resolve_key = expand_config["resolve_key"]
    param_name = expand_config["param"]

    exit_stack = AsyncExitStack()

    try:
        session = await _open_session(project, exit_stack, branch_id=branch_id)

        # Step 1: Call resolve tool (e.g. get_buckets)
        resolve_result = await asyncio.wait_for(
            session.call_tool(resolve_tool, {}),
            timeout=_get_tool_timeout(),
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

        # Step 2: Call target tool with all resolved IDs as array
        call_input = {**tool_input, param_name: list(item_ids)}
        result = await asyncio.wait_for(
            session.call_tool(tool_name, call_input),
            timeout=_get_tool_timeout(),
        )

        return {
            "content": _parse_content(result),
            "isError": bool(result.isError),
        }
    finally:
        await exit_stack.aclose()


def _extract_ids_from_toon(text: str, key: str) -> list[str]:
    """Extract ID values from a TOON-formatted string.

    Parses TOON array headers like ``items[N]{field1,field2,...}:`` to find
    the column position of *key*, then reads each indented data line using
    CSV parsing (handles quoted values with commas).
    """
    import csv
    import io
    import re

    ids: list[str] = []
    header_re = re.compile(r"\w+\[\d+\]\{([^}]+)\}:")
    field_index: int | None = None

    for line in text.split("\n"):
        m = header_re.search(line)
        if m:
            fields = [f.strip() for f in m.group(1).split(",")]
            try:
                field_index = fields.index(key)
            except ValueError:
                field_index = None
            continue

        if field_index is not None and line.startswith("  "):
            stripped = line.strip()
            if stripped:
                try:
                    values = next(csv.reader(io.StringIO(stripped)))
                    if len(values) > field_index:
                        ids.append(values[field_index])
                except Exception:
                    pass

    return ids


def _extract_ids(content_items: list[Any], key: str) -> list[str]:
    """Extract unique ID values from parsed MCP tool content.

    Handles JSON dicts/lists **and** TOON-formatted text strings.
    Deduplicates while preserving insertion order.
    """
    ids: list[str] = []
    for item in content_items:
        if isinstance(item, str):
            ids.extend(_extract_ids_from_toon(item, key))
        elif isinstance(item, list):
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

    Supports two transport modes:
    - HTTP (default): Uses a persistent server via McpServerManager.
      One server serves all projects with per-request credential headers.
    - stdio: Spawns a subprocess per MCP session. Fallback mode.

    Read tools execute across all projects in parallel.
    Write tools target a single project.

    Uses the same DI pattern as JobService/ConfigService.
    """

    def _get_server_url(self) -> str | None:
        """Get the persistent server URL if HTTP transport is configured.

        Returns:
            Base URL string if HTTP transport is active and server is running,
            None if stdio mode or server cannot be started.
        """
        if _get_transport_mode() != "http":
            return None

        try:
            from .mcp_transport import get_server_manager

            manager = get_server_manager()
            return manager.ensure_running()
        except Exception as exc:
            logger.warning("Failed to start persistent MCP server, falling back to stdio: %s", exc)
            return None

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
        self,
        aliases: list[str] | None = None,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        """List available MCP tools from the first reachable project.

        Tools are the same across projects (same MCP server), so we only
        need to query one project. Each tool is annotated with multi_project
        flag based on read/write classification.

        Args:
            aliases: Project aliases. Uses first available if None.
            branch_id: Optional development branch ID.

        Returns:
            Dict with "tools" list and "errors" list.

        Raises:
            ConfigError: If no projects are configured.
        """
        projects = self.resolve_projects(aliases)

        if not projects:
            raise ConfigError("No projects configured. Use 'kbagent project add' first.")

        # Try HTTP transport first, fall back to stdio
        server_url = self._get_server_url()

        errors: list[dict[str, str]] = []
        for alias, project in projects.items():
            try:
                if server_url:
                    tools = asyncio.run(_http_list_tools(server_url, project, branch_id=branch_id))
                else:
                    tools = asyncio.run(_connect_and_list_tools(project, branch_id=branch_id))
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
        self,
        tool_name: str,
        aliases: list[str] | None = None,
        branch_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get the input schema for a specific tool.

        Fetches tool list from the first available project and finds the
        matching tool's inputSchema.

        Args:
            tool_name: Name of the tool.
            aliases: Project aliases to try.
            branch_id: Optional development branch ID.

        Returns:
            The tool's inputSchema dict, or None if tool not found.
        """
        result = self.list_tools(aliases=aliases, branch_id=branch_id)
        for tool in result.get("tools", []):
            if tool["name"] == tool_name:
                return tool.get("inputSchema", {})
        return None

    def validate_tool_input(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        aliases: list[str] | None = None,
        branch_id: str | None = None,
    ) -> tuple[list[str], set[str]]:
        """Validate tool input against the tool's schema.

        Checks that all required parameters are provided.
        Parameters that can be auto-expanded (see AUTO_EXPAND_TOOLS) are
        excluded from the missing list.

        Also returns the set of known tool names so callers can pass it
        to call_tool() and avoid a redundant list_tools() MCP session.

        Args:
            tool_name: Name of the tool.
            tool_input: Input arguments to validate.
            aliases: Project aliases to try for schema lookup.
            branch_id: Optional development branch ID.

        Returns:
            Tuple of (missing_params, known_tool_names).
        """
        result = self.list_tools(aliases=aliases, branch_id=branch_id)
        known_tools = {t["name"] for t in result.get("tools", [])}

        # Find the schema for this tool
        schema: dict[str, Any] | None = None
        for tool in result.get("tools", []):
            if tool["name"] == tool_name:
                schema = tool.get("inputSchema", {})
                break

        if schema is None:
            return [], known_tools  # Tool not found; call_tool will raise ConfigError

        required = schema.get("required", [])
        missing = [param for param in required if param not in tool_input]

        # Exclude auto-expandable params from missing list
        expand_config = AUTO_EXPAND_TOOLS.get(tool_name)
        if expand_config:
            auto_param = expand_config["param"]
            missing = [p for p in missing if p != auto_param]

        return missing, known_tools

    def _check_tool_permission(self, tool_name: str) -> None:
        """Check if the MCP tool is allowed by the active permission policy.

        Raises:
            PermissionDeniedError: If the tool is blocked by the policy.
        """
        config = self._config_store.load()
        if config.permissions is None:
            return
        engine = PermissionEngine(config.permissions)
        engine.check_or_raise(f"tool:{tool_name}")

    def call_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
        alias: str | None = None,
        branch_id: str | None = None,
        _known_tools: set[str] | None = None,
    ) -> dict[str, Any]:
        """Call an MCP tool.

        For read tools (multi_project=true): runs across ALL projects in parallel,
        aggregates results with project_alias annotation.

        For write tools: runs on a single project (specified by alias or default).

        When branch_id is provided, forces single-project mode regardless of
        read/write classification (branch ID is per-project).

        Args:
            tool_name: Name of the MCP tool to call.
            tool_input: Input arguments for the tool.
            alias: Project alias for write tools. Ignored for read tools
                   unless specified to limit scope.
            branch_id: Optional development branch ID. Forces single-project mode.
            _known_tools: Optional set of known tool names from a prior
                validate_tool_input() call. When provided, skips the
                internal list_tools() call (saves one MCP subprocess).

        Returns:
            Dict with "results" list and "errors" list.

        Raises:
            ConfigError: If tool_name is not found in the available tool list.
        """
        self._check_tool_permission(tool_name)

        if tool_input is None:
            tool_input = {}

        # Validate tool name exists in the MCP tool list
        if _known_tools is not None:
            known_tools = _known_tools
        else:
            tool_list_result = self.list_tools(
                aliases=[alias] if alias else None,
                branch_id=branch_id,
            )
            known_tools = {t["name"] for t in tool_list_result.get("tools", [])}
        if known_tools and tool_name not in known_tools:
            raise ConfigError(
                f"Unknown MCP tool '{tool_name}'. Use 'kbagent tool list' to see available tools."
            )

        is_write = _is_write_tool(tool_name)

        # When branch_id is set, force single-project mode
        if branch_id is not None or is_write:
            return self._call_write_tool(tool_name, tool_input, alias, branch_id=branch_id)
        else:
            return self._call_read_tool(tool_name, tool_input, alias, branch_id=branch_id)

    def validate_and_call_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
        alias: str | None = None,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate and call an MCP tool in a single session (no double spawn).

        Opens ONE MCP session: validates tool name + required params from
        list_tools(), then calls the tool. This eliminates the separate
        validate_tool_input() + call_tool() round-trip.

        Prefers HTTP transport (persistent server) with fallback to stdio.

        For write tools and branch-scoped calls: single project.
        For read tools: still runs across all projects in parallel, but each
        project's session validates + calls in one go.

        Args:
            tool_name: Name of the MCP tool to call.
            tool_input: Input arguments for the tool.
            alias: Project alias for write tools / single-project mode.
            branch_id: Optional development branch ID. Forces single-project mode.

        Returns:
            Dict with "results" list and "errors" list.

        Raises:
            ConfigError: If tool_name not found or required params missing.
        """
        self._check_tool_permission(tool_name)

        if tool_input is None:
            tool_input = {}

        server_url = self._get_server_url()
        is_write = _is_write_tool(tool_name)

        # Single-project mode: write tools, branch-scoped, or explicit alias
        if branch_id is not None or is_write:
            resolved_alias, project = self.resolve_project(alias)
            try:
                # Check if auto-expand needed
                expand_config = AUTO_EXPAND_TOOLS.get(tool_name)
                if expand_config and expand_config["param"] not in tool_input:
                    if server_url:
                        result = asyncio.run(
                            _http_auto_expand(
                                server_url,
                                project,
                                tool_name,
                                tool_input,
                                expand_config,
                                branch_id=branch_id,
                            )
                        )
                    else:
                        result = asyncio.run(
                            _connect_and_auto_expand(
                                project,
                                tool_name,
                                tool_input,
                                expand_config,
                                branch_id=branch_id,
                            )
                        )
                else:
                    if server_url:
                        result = asyncio.run(
                            _http_validate_and_call(
                                server_url,
                                project,
                                tool_name,
                                tool_input,
                                branch_id=branch_id,
                            )
                        )
                    else:
                        result = asyncio.run(
                            _connect_validate_and_call(
                                project,
                                tool_name,
                                tool_input,
                                branch_id=branch_id,
                            )
                        )
                result["project_alias"] = resolved_alias
                return {"results": [result], "errors": []}
            except ConfigError:
                raise
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

        # Multi-project read: parallel validate+call across all projects
        projects = self.resolve_projects([alias]) if alias else self.resolve_projects()
        if not projects:
            raise ConfigError("No projects configured. Use 'kbagent project add' first.")

        # Check if auto-expand is needed
        expand_config = AUTO_EXPAND_TOOLS.get(tool_name)
        if expand_config and expand_config["param"] not in tool_input:
            return asyncio.run(
                self._gather_auto_expand_results(
                    projects,
                    tool_name,
                    tool_input,
                    expand_config,
                    branch_id=branch_id,
                    server_url=server_url,
                )
            )

        return asyncio.run(
            self._gather_validate_and_call_results(
                projects,
                tool_name,
                tool_input,
                branch_id=branch_id,
                server_url=server_url,
            )
        )

    async def _gather_validate_and_call_results(
        self,
        projects: dict[str, ProjectConfig],
        tool_name: str,
        tool_input: dict[str, Any],
        branch_id: str | None = None,
        server_url: str | None = None,
    ) -> dict[str, Any]:
        """Run validate+call across multiple projects in parallel.

        Each project opens one session that validates and calls the tool.
        Uses HTTP transport when server_url is available.
        """
        max_sessions = _get_max_sessions()
        sem = asyncio.Semaphore(max_sessions) if max_sessions > 0 else None
        tasks = {}
        for a, project in projects.items():
            if server_url:
                coro = _http_validate_and_call(
                    server_url,
                    project,
                    tool_name,
                    tool_input,
                    branch_id=branch_id,
                )
            else:
                coro = _connect_validate_and_call(
                    project,
                    tool_name,
                    tool_input,
                    branch_id=branch_id,
                )
            if sem is not None:
                coro = _semaphored(sem, coro)
            tasks[a] = asyncio.create_task(coro)
        return await self._gather_results(tasks)

    def _call_write_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        alias: str | None,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute a write tool on a single project."""
        resolved_alias, project = self.resolve_project(alias)
        server_url = self._get_server_url()

        try:
            if server_url:
                result = asyncio.run(
                    _http_call_tool(
                        server_url,
                        project,
                        tool_name,
                        tool_input,
                        branch_id=branch_id,
                    )
                )
            else:
                result = asyncio.run(
                    _connect_and_call_tool(
                        project,
                        tool_name,
                        tool_input,
                        branch_id=branch_id,
                    )
                )
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
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute a read tool across projects in parallel.

        If the tool is in AUTO_EXPAND_TOOLS and the required param is missing,
        automatically resolves it by calling the resolve tool first.
        """
        projects = self.resolve_projects([alias]) if alias else self.resolve_projects()

        if not projects:
            raise ConfigError("No projects configured. Use 'kbagent project add' first.")

        server_url = self._get_server_url()

        # Check if auto-expand is needed
        expand_config = AUTO_EXPAND_TOOLS.get(tool_name)
        if expand_config and expand_config["param"] not in tool_input:
            return asyncio.run(
                self._gather_auto_expand_results(
                    projects,
                    tool_name,
                    tool_input,
                    expand_config,
                    branch_id=branch_id,
                    server_url=server_url,
                )
            )

        return asyncio.run(
            self._gather_read_results(
                projects,
                tool_name,
                tool_input,
                branch_id=branch_id,
                server_url=server_url,
            )
        )

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

        for alias, outcome in zip(aliases, outcomes, strict=False):
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
        branch_id: str | None = None,
        server_url: str | None = None,
    ) -> dict[str, Any]:
        """Run an auto-expanded tool across multiple projects in parallel.

        For each project, opens one MCP session, resolves the missing param
        by calling the resolve tool, then calls the target tool per item.
        Uses HTTP transport when server_url is available.
        """
        max_sessions = _get_max_sessions()
        sem = asyncio.Semaphore(max_sessions) if max_sessions > 0 else None
        tasks = {}
        for a, project in projects.items():
            if server_url:
                coro = _http_auto_expand(
                    server_url,
                    project,
                    tool_name,
                    tool_input,
                    expand_config,
                    branch_id=branch_id,
                )
            else:
                coro = _connect_and_auto_expand(
                    project,
                    tool_name,
                    tool_input,
                    expand_config,
                    branch_id=branch_id,
                )
            if sem is not None:
                coro = _semaphored(sem, coro)
            tasks[a] = asyncio.create_task(coro)
        return await self._gather_results(tasks)

    async def _gather_read_results(
        self,
        projects: dict[str, ProjectConfig],
        tool_name: str,
        tool_input: dict[str, Any],
        branch_id: str | None = None,
        server_url: str | None = None,
    ) -> dict[str, Any]:
        """Run a read tool across multiple projects in parallel using asyncio.gather.

        Uses HTTP transport when server_url is available.
        """
        max_sessions = _get_max_sessions()
        sem = asyncio.Semaphore(max_sessions) if max_sessions > 0 else None
        tasks = {}
        for a, project in projects.items():
            if server_url:
                coro = _http_call_tool(
                    server_url,
                    project,
                    tool_name,
                    tool_input,
                    branch_id=branch_id,
                )
            else:
                coro = _connect_and_call_tool(
                    project,
                    tool_name,
                    tool_input,
                    branch_id=branch_id,
                )
            if sem is not None:
                coro = _semaphored(sem, coro)
            tasks[a] = asyncio.create_task(coro)
        return await self._gather_results(tasks)

    def check_server_available(self) -> dict[str, Any]:
        """Check if MCP server is available (for doctor command).

        Returns:
            Dict with check status, message, and transport info.
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

        transport_mode = _get_transport_mode()
        transport_info = f"transport={transport_mode}"

        # Detect if using slow uvx fallback
        is_uvx_fallback = command[0] == "uvx"
        if is_uvx_fallback:
            transport_info += ", using uvx (slower startup)"

        # If HTTP mode, check persistent server status
        if transport_mode == "http":
            try:
                from .mcp_transport import get_server_manager

                manager = get_server_manager()
                if manager.is_running:
                    transport_info += f", persistent server running on port {manager.port}"
                else:
                    transport_info += ", persistent server not yet started (lazy start)"
            except Exception:
                transport_info += ", persistent server unavailable (will fallback to stdio)"

        status = "pass"
        message = f"MCP server available via: {' '.join(command)} ({transport_info})"
        if is_uvx_fallback:
            status = "warn"
            message += (
                ". For faster startup, run: uv tool install --prerelease=allow keboola-mcp-server"
            )

        return {
            "check": "mcp_server",
            "name": "MCP server",
            "status": status,
            "message": message,
        }
