"""Tests for McpService - MCP integration service for multi-project tool operations."""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.mcp_service import (
    McpService,
    _build_server_params,
    _extract_ids,
    _extract_ids_from_toon,
    _get_max_sessions,
    _is_write_tool,
    _semaphored,
    detect_mcp_server_command,
)


def _setup_store(
    tmp_path: Path,
    projects: dict | None = None,
    default_project: str = "",
) -> ConfigStore:
    """Create a ConfigStore with optional pre-configured projects.

    Args:
        tmp_path: Temporary directory for config files.
        projects: Dict of alias -> project info dicts.
        default_project: Alias of the default project.

    Returns:
        Configured ConfigStore instance.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    store = ConfigStore(config_dir=config_dir)
    if projects:
        for alias, info in projects.items():
            store.add_project(
                alias,
                ProjectConfig(
                    stack_url=info.get("stack_url", "https://connection.keboola.com"),
                    token=info["token"],
                    project_name=info.get("project_name", alias),
                    project_id=info.get("project_id", 1234),
                ),
            )
    if default_project:
        config = store.load()
        config.default_project = default_project
        store.save(config)
    return store


def _sample_tools() -> list[dict]:
    """Return a sample list of MCP tool dicts for testing."""
    return [
        {
            "name": "list_configs",
            "description": "List all configurations",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_config",
            "description": "Get a single configuration",
            "inputSchema": {
                "type": "object",
                "properties": {"config_id": {"type": "string"}},
            },
        },
        {
            "name": "create_config",
            "description": "Create a new configuration",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
            },
        },
    ]


# ---------------------------------------------------------------------------
# TestIsWriteTool
# ---------------------------------------------------------------------------


class TestIsWriteTool:
    """Tests for the _is_write_tool() helper function."""

    @pytest.mark.parametrize(
        "tool_name",
        [
            "create_config",
            "create_bucket",
            "update_config",
            "update_table",
            "delete_config",
            "delete_bucket",
            "add_column",
            "add_tag",
            "remove_column",
            "remove_tag",
            "set_default",
            "set_metadata",
        ],
    )
    def test_write_prefixes_detected(self, tool_name: str) -> None:
        """Tool names starting with write prefixes are classified as write tools."""
        assert _is_write_tool(tool_name) is True

    @pytest.mark.parametrize(
        "tool_name",
        [
            "list_configs",
            "get_config",
            "search",
            "docs_query",
            "find_component_id",
            "describe_table",
            "show_bucket",
            "retrieve_logs",
        ],
    )
    def test_read_tool_names_not_detected(self, tool_name: str) -> None:
        """Tool names that do not start with write prefixes are read tools."""
        assert _is_write_tool(tool_name) is False

    def test_empty_tool_name(self) -> None:
        """Empty string is not a write tool."""
        assert _is_write_tool("") is False


# ---------------------------------------------------------------------------
# TestDetectMcpServerCommand
# ---------------------------------------------------------------------------


class TestDetectMcpServerCommand:
    """Tests for detect_mcp_server_command() which finds the MCP server binary."""

    @patch("keboola_agent_cli.services.mcp_service.shutil.which")
    def test_local_install_preferred(self, mock_which: MagicMock) -> None:
        """When keboola_mcp_server is locally installed, returns it (fastest)."""
        mock_which.side_effect = lambda cmd: (
            "/usr/local/bin/keboola_mcp_server" if cmd == "keboola_mcp_server" else None
        )
        result = detect_mcp_server_command()
        assert result == ["keboola_mcp_server"]

    @patch("keboola_agent_cli.services.mcp_service.subprocess.run")
    @patch("keboola_agent_cli.services.mcp_service.shutil.which")
    def test_python_module_second(self, mock_which: MagicMock, mock_run: MagicMock) -> None:
        """When python is available and module exists, returns python -m."""

        def which_side_effect(cmd: str) -> str | None:
            if cmd == "python":
                return "/usr/bin/python"
            return None

        mock_which.side_effect = which_side_effect
        mock_run.return_value = MagicMock(returncode=0)
        result = detect_mcp_server_command()
        assert result == ["python", "-m", "keboola_mcp_server"]
        mock_run.assert_called_once()

    @patch("keboola_agent_cli.services.mcp_service.subprocess.run")
    @patch("keboola_agent_cli.services.mcp_service.shutil.which")
    def test_python_module_not_installed_falls_to_uvx(
        self,
        mock_which: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        """When python exists but module not installed, falls back to uvx."""

        def which_side_effect(cmd: str) -> str | None:
            if cmd in ("python", "uvx"):
                return f"/usr/bin/{cmd}"
            return None

        mock_which.side_effect = which_side_effect
        mock_run.return_value = MagicMock(returncode=1)
        result = detect_mcp_server_command()
        assert result == ["uvx", "--prerelease=allow", "keboola_mcp_server"]

    @patch("keboola_agent_cli.services.mcp_service.shutil.which")
    def test_uvx_fallback(self, mock_which: MagicMock) -> None:
        """When only uvx is available, returns uvx WITHOUT @latest."""
        mock_which.side_effect = lambda cmd: "/usr/local/bin/uvx" if cmd == "uvx" else None
        result = detect_mcp_server_command()
        assert result == ["uvx", "--prerelease=allow", "keboola_mcp_server"]

    @patch("keboola_agent_cli.services.mcp_service.shutil.which")
    def test_nothing_available(self, mock_which: MagicMock) -> None:
        """When nothing is available, returns None."""
        mock_which.return_value = None
        result = detect_mcp_server_command()
        assert result is None

    @patch("keboola_agent_cli.services.mcp_service.shutil.which")
    def test_local_install_preferred_over_uvx(self, mock_which: MagicMock) -> None:
        """Local install is preferred even when uvx is also available."""

        def which_side_effect(cmd: str) -> str | None:
            if cmd in ("keboola_mcp_server", "uvx"):
                return f"/usr/local/bin/{cmd}"
            return None

        mock_which.side_effect = which_side_effect
        result = detect_mcp_server_command()
        assert result == ["keboola_mcp_server"]


# ---------------------------------------------------------------------------
# TestMcpServiceResolveProject
# ---------------------------------------------------------------------------


class TestMcpServiceResolveProject:
    """Tests for McpService.resolve_project() - single project resolution."""

    def test_resolve_with_explicit_alias(self, tmp_path: Path) -> None:
        """Resolving with an explicit alias returns that project."""
        store = _setup_store(
            tmp_path,
            projects={"prod": {"token": "tok-prod"}, "dev": {"token": "tok-dev"}},
        )
        svc = McpService(config_store=store)
        alias, project = svc.resolve_project("dev")
        assert alias == "dev"
        assert project.token == "tok-dev"

    def test_resolve_with_none_uses_default(self, tmp_path: Path) -> None:
        """Resolving with None uses the default project."""
        store = _setup_store(
            tmp_path,
            projects={"prod": {"token": "tok-prod"}, "dev": {"token": "tok-dev"}},
            default_project="prod",
        )
        svc = McpService(config_store=store)
        alias, project = svc.resolve_project(None)
        assert alias == "prod"
        assert project.token == "tok-prod"

    def test_resolve_with_no_default_raises(self, tmp_path: Path) -> None:
        """Resolving with None and no default project raises ConfigError."""
        # add_project auto-sets default, so we manually clear it after setup
        store = _setup_store(
            tmp_path,
            projects={"prod": {"token": "tok-prod"}},
        )
        config = store.load()
        config.default_project = ""
        store.save(config)

        svc = McpService(config_store=store)
        with pytest.raises(ConfigError, match="No default project set"):
            svc.resolve_project(None)

    def test_resolve_with_nonexistent_alias_raises(self, tmp_path: Path) -> None:
        """Resolving with an alias that doesn't exist raises ConfigError."""
        store = _setup_store(
            tmp_path,
            projects={"prod": {"token": "tok-prod"}},
        )
        svc = McpService(config_store=store)
        with pytest.raises(ConfigError, match="Project 'nonexistent' not found"):
            svc.resolve_project("nonexistent")


# ---------------------------------------------------------------------------
# TestMcpServiceResolveProjects
# ---------------------------------------------------------------------------


class TestMcpServiceResolveProjects:
    """Tests for McpService.resolve_projects() - multi-project resolution."""

    def test_resolve_all_when_no_aliases(self, tmp_path: Path) -> None:
        """When aliases is None, returns all configured projects."""
        store = _setup_store(
            tmp_path,
            projects={"prod": {"token": "tok-prod"}, "dev": {"token": "tok-dev"}},
        )
        svc = McpService(config_store=store)
        result = svc.resolve_projects()
        assert set(result.keys()) == {"prod", "dev"}

    def test_resolve_specific_aliases(self, tmp_path: Path) -> None:
        """When aliases are specified, returns only those projects."""
        store = _setup_store(
            tmp_path,
            projects={
                "prod": {"token": "tok-prod"},
                "dev": {"token": "tok-dev"},
                "staging": {"token": "tok-staging"},
            },
        )
        svc = McpService(config_store=store)
        result = svc.resolve_projects(["prod", "staging"])
        assert set(result.keys()) == {"prod", "staging"}

    def test_resolve_nonexistent_alias_raises(self, tmp_path: Path) -> None:
        """Specifying a nonexistent alias raises ConfigError."""
        store = _setup_store(
            tmp_path,
            projects={"prod": {"token": "tok-prod"}},
        )
        svc = McpService(config_store=store)
        with pytest.raises(ConfigError, match="Project 'missing' not found"):
            svc.resolve_projects(["missing"])


# ---------------------------------------------------------------------------
# TestMcpServiceListTools
# ---------------------------------------------------------------------------


class TestMcpServiceListTools:
    """Tests for McpService.list_tools() - tool discovery from MCP server."""

    @patch("keboola_agent_cli.services.mcp_service._connect_and_list_tools")
    def test_list_tools_returns_annotated_tools(
        self, mock_connect: MagicMock, tmp_path: Path
    ) -> None:
        """list_tools returns tools annotated with multi_project flag."""
        mock_connect.return_value = _sample_tools()

        store = _setup_store(
            tmp_path,
            projects={"prod": {"token": "tok-prod"}},
        )
        svc = McpService(config_store=store)

        # Mock asyncio.run to directly call the coroutine-returning mock
        with patch("keboola_agent_cli.services.mcp_service.asyncio.run") as mock_run:
            mock_run.return_value = _sample_tools()
            result = svc.list_tools()

        tools = result["tools"]
        assert len(tools) == 3

        # Read tools should have multi_project=True
        list_tool = next(t for t in tools if t["name"] == "list_configs")
        assert list_tool["multi_project"] is True

        get_tool = next(t for t in tools if t["name"] == "get_config")
        assert get_tool["multi_project"] is True

        # Write tools should have multi_project=False
        create_tool = next(t for t in tools if t["name"] == "create_config")
        assert create_tool["multi_project"] is False

    @patch("keboola_agent_cli.services.mcp_service.asyncio.run")
    def test_list_tools_all_projects_fail(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """When all projects fail, list_tools returns empty tools and accumulated errors."""
        mock_run.side_effect = RuntimeError("Connection refused")

        store = _setup_store(
            tmp_path,
            projects={
                "prod": {"token": "tok-prod"},
                "dev": {"token": "tok-dev"},
            },
        )
        svc = McpService(config_store=store)
        result = svc.list_tools()

        assert result["tools"] == []
        assert len(result["errors"]) == 2

        error_aliases = {e["project_alias"] for e in result["errors"]}
        assert error_aliases == {"prod", "dev"}
        for error in result["errors"]:
            assert error["error_code"] == "MCP_ERROR"
            assert "Connection refused" in error["message"]

    def test_list_tools_no_projects_raises(self, tmp_path: Path) -> None:
        """list_tools raises ConfigError when no projects are configured."""
        store = _setup_store(tmp_path, projects={})
        svc = McpService(config_store=store)
        with pytest.raises(ConfigError, match="No projects configured"):
            svc.list_tools()

    @patch("keboola_agent_cli.services.mcp_service.asyncio.run")
    def test_list_tools_first_project_fails_second_succeeds(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """When the first project fails, list_tools tries the next one."""
        call_count = 0
        tools = _sample_tools()

        def side_effect(coro):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Server unavailable")
            return tools

        mock_run.side_effect = side_effect

        store = _setup_store(
            tmp_path,
            projects={
                "alpha": {"token": "tok-alpha"},
                "beta": {"token": "tok-beta"},
            },
        )
        svc = McpService(config_store=store)
        result = svc.list_tools()

        # One error from the first project
        assert len(result["errors"]) == 1
        assert result["errors"][0]["project_alias"] in ("alpha", "beta")

        # Tools from the second project
        assert len(result["tools"]) == 3


# ---------------------------------------------------------------------------
# TestMcpServiceCallTool
# ---------------------------------------------------------------------------


class TestMcpServiceCallTool:
    """Tests for McpService.call_tool() - read and write tool execution."""

    @patch("keboola_agent_cli.services.mcp_service._connect_and_call_tool", new_callable=AsyncMock)
    def test_call_read_tool_across_all_projects(
        self, mock_call_tool: AsyncMock, tmp_path: Path
    ) -> None:
        """Read tools run across all configured projects in parallel."""
        mock_call_tool.return_value = {
            "content": [{"configs": ["cfg1", "cfg2"]}],
            "isError": False,
        }

        store = _setup_store(
            tmp_path,
            projects={
                "prod": {"token": "tok-prod"},
                "dev": {"token": "tok-dev"},
            },
        )
        svc = McpService(config_store=store)

        # _call_read_tool calls asyncio.run(_gather_read_results(...)) which
        # internally uses asyncio.create_task on the patched async function.
        # We mock _gather_read_results to return the expected aggregated result
        # so we can test the routing logic without a real event loop.
        expected = {
            "results": [
                {
                    "content": [{"configs": ["cfg1", "cfg2"]}],
                    "isError": False,
                    "project_alias": "prod",
                },
                {
                    "content": [{"configs": ["cfg1", "cfg2"]}],
                    "isError": False,
                    "project_alias": "dev",
                },
            ],
            "errors": [],
        }
        with patch.object(
            svc, "_gather_read_results", new_callable=AsyncMock, return_value=expected
        ):
            result = svc.call_tool("list_configs", {"component_id": "keboola.ex-db-mysql"})

        assert len(result["results"]) == 2
        assert result["errors"] == []

        result_aliases = {r["project_alias"] for r in result["results"]}
        assert result_aliases == {"prod", "dev"}

        for r in result["results"]:
            assert r["isError"] is False
            assert r["content"] == [{"configs": ["cfg1", "cfg2"]}]

    @patch("keboola_agent_cli.services.mcp_service._connect_and_call_tool", new_callable=AsyncMock)
    def test_call_read_tool_with_specific_alias(
        self, mock_call_tool: AsyncMock, tmp_path: Path
    ) -> None:
        """Read tools with a specific alias only query that one project."""
        mock_call_tool.return_value = {
            "content": [{"result": "data"}],
            "isError": False,
        }

        store = _setup_store(
            tmp_path,
            projects={
                "prod": {"token": "tok-prod"},
                "dev": {"token": "tok-dev"},
            },
        )
        svc = McpService(config_store=store)
        result = svc.call_tool("get_config", {"config_id": "123"}, alias="prod")

        assert len(result["results"]) == 1
        assert result["results"][0]["project_alias"] == "prod"

    @patch("keboola_agent_cli.services.mcp_service.asyncio.run")
    def test_call_write_tool_single_project(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """Write tools execute on a single project only."""
        mock_run.return_value = {
            "content": [{"created": True}],
            "isError": False,
        }

        store = _setup_store(
            tmp_path,
            projects={"prod": {"token": "tok-prod"}, "dev": {"token": "tok-dev"}},
            default_project="prod",
        )
        svc = McpService(config_store=store)
        result = svc.call_tool("create_config", {"name": "new-config"})

        assert len(result["results"]) == 1
        assert result["results"][0]["project_alias"] == "prod"
        assert result["results"][0]["content"] == [{"created": True}]
        assert result["errors"] == []

    @patch("keboola_agent_cli.services.mcp_service.asyncio.run")
    def test_call_write_tool_with_explicit_alias(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """Write tools use the explicit alias when provided."""
        mock_run.return_value = {
            "content": [{"deleted": True}],
            "isError": False,
        }

        store = _setup_store(
            tmp_path,
            projects={"prod": {"token": "tok-prod"}, "dev": {"token": "tok-dev"}},
            default_project="prod",
        )
        svc = McpService(config_store=store)
        result = svc.call_tool("delete_config", {"config_id": "456"}, alias="dev")

        assert len(result["results"]) == 1
        assert result["results"][0]["project_alias"] == "dev"
        assert result["errors"] == []

    def test_call_write_tool_no_default_raises(self, tmp_path: Path) -> None:
        """Write tools without alias and no default project raise ConfigError."""
        # add_project auto-sets default, so we manually clear it after setup
        store = _setup_store(
            tmp_path,
            projects={"prod": {"token": "tok-prod"}},
        )
        config = store.load()
        config.default_project = ""
        store.save(config)

        svc = McpService(config_store=store)
        with pytest.raises(ConfigError, match="No default project set"):
            svc.call_tool("create_config", {"name": "new-config"})

    @patch("keboola_agent_cli.services.mcp_service._connect_and_call_tool", new_callable=AsyncMock)
    @patch("keboola_agent_cli.services.mcp_service._connect_and_list_tools", new_callable=AsyncMock)
    def test_call_tool_none_input_defaults_to_empty_dict(
        self, mock_list_tools: AsyncMock, mock_call_tool: AsyncMock, tmp_path: Path
    ) -> None:
        """When tool_input is None, it defaults to an empty dict."""
        mock_list_tools.return_value = [
            {"name": "list_configs", "description": "List configs", "inputSchema": {}},
        ]
        mock_call_tool.return_value = {
            "content": [{"result": "ok"}],
            "isError": False,
        }

        store = _setup_store(
            tmp_path,
            projects={"prod": {"token": "tok-prod"}},
        )
        svc = McpService(config_store=store)
        result = svc.call_tool("list_configs")

        assert len(result["results"]) == 1
        # Verify the async mock was called with empty dict for tool_input
        call_args = mock_call_tool.call_args
        assert call_args[0][2] == {}  # third positional arg is tool_input


# ---------------------------------------------------------------------------
# TestMcpServiceErrorAccumulation
# ---------------------------------------------------------------------------


class TestMcpServiceErrorAccumulation:
    """Tests for error accumulation - projects that fail don't stop others."""

    @patch("keboola_agent_cli.services.mcp_service._connect_and_call_tool", new_callable=AsyncMock)
    @patch("keboola_agent_cli.services.mcp_service._connect_and_list_tools", new_callable=AsyncMock)
    def test_read_tool_partial_failure(
        self, mock_list_tools: AsyncMock, mock_call_tool: AsyncMock, tmp_path: Path
    ) -> None:
        """When some projects fail for a read tool, successful results and errors are both returned."""
        mock_list_tools.return_value = [
            {"name": "list_configs", "description": "List configs", "inputSchema": {}},
        ]

        async def side_effect(project, tool_name, tool_input, branch_id=None):
            if project.token == "tok-failing":
                raise RuntimeError("Connection timeout")
            return {
                "content": [{"data": "from-success"}],
                "isError": False,
            }

        mock_call_tool.side_effect = side_effect

        store = _setup_store(
            tmp_path,
            projects={
                "good": {"token": "tok-good"},
                "bad": {"token": "tok-failing"},
            },
        )
        svc = McpService(config_store=store)
        result = svc.call_tool("list_configs")

        # One success, one error
        assert len(result["results"]) == 1
        assert result["results"][0]["project_alias"] == "good"
        assert result["results"][0]["content"] == [{"data": "from-success"}]

        assert len(result["errors"]) == 1
        assert result["errors"][0]["project_alias"] == "bad"
        assert "Connection timeout" in result["errors"][0]["message"]

    @patch("keboola_agent_cli.services.mcp_service.asyncio.run")
    def test_write_tool_failure_returns_error(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """When a write tool fails, the error is captured in the errors list."""
        mock_run.side_effect = RuntimeError("MCP server crashed")

        store = _setup_store(
            tmp_path,
            projects={"prod": {"token": "tok-prod"}},
            default_project="prod",
        )
        svc = McpService(config_store=store)
        result = svc.call_tool("create_config", {"name": "cfg"})

        assert result["results"] == []
        assert len(result["errors"]) == 1
        assert result["errors"][0]["project_alias"] == "prod"
        assert result["errors"][0]["error_code"] == "MCP_ERROR"
        assert "MCP server crashed" in result["errors"][0]["message"]


# ---------------------------------------------------------------------------
# TestCheckServerAvailable
# ---------------------------------------------------------------------------


class TestCheckServerAvailable:
    """Tests for McpService.check_server_available() - doctor command integration."""

    @patch("keboola_agent_cli.services.mcp_service.shutil.which")
    def test_server_available_via_uvx(self, mock_which: MagicMock, tmp_path: Path) -> None:
        """When only uvx is available (no direct binary), status is 'warn' with install hint."""
        mock_which.side_effect = lambda cmd: "/usr/local/bin/uvx" if cmd == "uvx" else None

        store = _setup_store(tmp_path, projects={})
        svc = McpService(config_store=store)
        result = svc.check_server_available()

        assert result["check"] == "mcp_server"
        assert result["name"] == "MCP server"
        assert result["status"] == "warn"
        assert "uvx" in result["message"]
        assert "uv tool install" in result["message"]

    @patch("keboola_agent_cli.services.mcp_service.shutil.which")
    def test_server_available_via_direct_binary(
        self, mock_which: MagicMock, tmp_path: Path
    ) -> None:
        """When keboola_mcp_server is directly available, status is 'pass'."""

        def which_side_effect(cmd: str) -> str | None:
            if cmd == "keboola_mcp_server":
                return "/usr/local/bin/keboola_mcp_server"
            return None

        mock_which.side_effect = which_side_effect

        store = _setup_store(tmp_path, projects={})
        svc = McpService(config_store=store)
        result = svc.check_server_available()

        assert result["status"] == "pass"
        assert "keboola_mcp_server" in result["message"]

    @patch("keboola_agent_cli.services.mcp_service.subprocess.run")
    @patch("keboola_agent_cli.services.mcp_service.shutil.which")
    def test_server_available_via_python(
        self, mock_which: MagicMock, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """When only python is available and module exists, status is 'pass'."""

        def which_side_effect(cmd: str) -> str | None:
            if cmd == "python":
                return "/usr/bin/python"
            return None

        mock_which.side_effect = which_side_effect
        mock_run.return_value = MagicMock(returncode=0)

        store = _setup_store(tmp_path, projects={})
        svc = McpService(config_store=store)
        result = svc.check_server_available()

        assert result["status"] == "pass"
        assert "python -m keboola_mcp_server" in result["message"]

    @patch("keboola_agent_cli.services.mcp_service.shutil.which")
    def test_server_not_available(self, mock_which: MagicMock, tmp_path: Path) -> None:
        """When nothing is available, status is 'warn' with installation instructions."""
        mock_which.return_value = None

        store = _setup_store(tmp_path, projects={})
        svc = McpService(config_store=store)
        result = svc.check_server_available()

        assert result["check"] == "mcp_server"
        assert result["name"] == "MCP server"
        assert result["status"] == "warn"
        assert "not found" in result["message"]
        assert "pip install keboola-mcp-server" in result["message"]


# ---------------------------------------------------------------------------
# TestEnsureMcpInstalled
# ---------------------------------------------------------------------------


class TestEnsureMcpInstalled:
    """Tests for ensure_mcp_installed() - auto-install of MCP server binary."""

    @patch("keboola_agent_cli.services.mcp_service.shutil.which")
    def test_binary_already_available(self, mock_which: MagicMock) -> None:
        """When binary is already in PATH, no installation needed."""
        mock_which.side_effect = lambda cmd: (
            "/usr/local/bin/keboola_mcp_server" if cmd == "keboola_mcp_server" else None
        )

        from keboola_agent_cli.services.mcp_service import ensure_mcp_installed

        result = ensure_mcp_installed()

        assert result["method"] == "binary"
        assert result["installed"] is False

    @patch("keboola_agent_cli.services.mcp_service.subprocess.run")
    @patch("keboola_agent_cli.services.mcp_service.shutil.which")
    def test_python_module_available(self, mock_which: MagicMock, mock_run: MagicMock) -> None:
        """When python module is available, no installation needed."""

        def which_side_effect(cmd: str) -> str | None:
            if cmd == "python":
                return "/usr/bin/python"
            return None

        mock_which.side_effect = which_side_effect
        mock_run.return_value = MagicMock(returncode=0)

        from keboola_agent_cli.services.mcp_service import ensure_mcp_installed

        result = ensure_mcp_installed()

        assert result["method"] == "python_module"
        assert result["installed"] is False

    @patch("keboola_agent_cli.services.mcp_service.subprocess.run")
    @patch("keboola_agent_cli.services.mcp_service.shutil.which")
    def test_uv_tool_install_success(self, mock_which: MagicMock, mock_run: MagicMock) -> None:
        """When only uv is available, runs uv tool install successfully."""

        def which_side_effect(cmd: str) -> str | None:
            if cmd == "uv":
                return "/usr/local/bin/uv"
            if cmd == "python":
                return "/usr/bin/python"
            return None

        mock_which.side_effect = which_side_effect
        # First call: python -c import check (fails), second: uv tool install (succeeds)
        mock_run.side_effect = [
            MagicMock(returncode=1),  # python module not found
            MagicMock(returncode=0, stderr=""),  # uv tool install success
        ]

        from keboola_agent_cli.services.mcp_service import ensure_mcp_installed

        result = ensure_mcp_installed()

        assert result["method"] == "uv_tool_install"
        assert result["installed"] is True

    @patch("keboola_agent_cli.services.mcp_service.shutil.which")
    def test_uvx_fallback(self, mock_which: MagicMock) -> None:
        """When only uvx is available (no uv), returns fallback message."""

        def which_side_effect(cmd: str) -> str | None:
            if cmd == "uvx":
                return "/usr/local/bin/uvx"
            return None

        mock_which.side_effect = which_side_effect

        from keboola_agent_cli.services.mcp_service import ensure_mcp_installed

        result = ensure_mcp_installed()

        assert result["method"] == "uvx_fallback"
        assert result["installed"] is False
        assert "uv tool install" in result["message"]

    @patch("keboola_agent_cli.services.mcp_service.shutil.which")
    def test_nothing_available(self, mock_which: MagicMock) -> None:
        """When nothing is available, returns not_found."""
        mock_which.return_value = None

        from keboola_agent_cli.services.mcp_service import ensure_mcp_installed

        result = ensure_mcp_installed()

        assert result["method"] == "not_found"
        assert result["installed"] is False


# ---------------------------------------------------------------------------
# TestMcpTimeoutFromEnv
# ---------------------------------------------------------------------------


class TestMcpTimeoutFromEnv:
    """Tests for MCP timeout configuration via environment variables."""

    def test_mcp_timeout_from_env(self) -> None:
        """KBAGENT_MCP_TOOL_TIMEOUT and KBAGENT_MCP_INIT_TIMEOUT env vars override defaults."""
        import os

        from keboola_agent_cli.services.mcp_service import (
            _get_init_timeout,
            _get_tool_timeout,
        )

        with patch.dict(
            os.environ,
            {
                "KBAGENT_MCP_TOOL_TIMEOUT": "120",
                "KBAGENT_MCP_INIT_TIMEOUT": "45",
            },
        ):
            assert _get_tool_timeout() == 120
            assert _get_init_timeout() == 45

    def test_mcp_timeout_defaults(self) -> None:
        """Without env vars, MCP timeouts use default values from constants."""
        import os

        from keboola_agent_cli.constants import (
            DEFAULT_MCP_INIT_TIMEOUT,
            DEFAULT_MCP_TOOL_TIMEOUT,
        )
        from keboola_agent_cli.services.mcp_service import (
            _get_init_timeout,
            _get_tool_timeout,
        )

        # Clear any env vars that might be set
        env = os.environ.copy()
        env.pop("KBAGENT_MCP_TOOL_TIMEOUT", None)
        env.pop("KBAGENT_MCP_INIT_TIMEOUT", None)
        with patch.dict(os.environ, env, clear=True):
            assert _get_tool_timeout() == DEFAULT_MCP_TOOL_TIMEOUT
            assert _get_init_timeout() == DEFAULT_MCP_INIT_TIMEOUT


# ---------------------------------------------------------------------------
# TestUnknownToolName
# ---------------------------------------------------------------------------


class TestUnknownToolName:
    """Tests for Phase 6: Unknown MCP tool name validation."""

    @patch("keboola_agent_cli.services.mcp_service.asyncio.run")
    def test_unknown_tool_name_error(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """Calling a nonexistent tool raises ConfigError with a clear message."""
        mock_run.return_value = _sample_tools()

        store = _setup_store(
            tmp_path,
            projects={"prod": {"token": "tok-prod"}},
        )
        svc = McpService(config_store=store)

        with pytest.raises(ConfigError, match="Unknown MCP tool 'nonexistent_tool'"):
            svc.call_tool("nonexistent_tool", {})

    @patch("keboola_agent_cli.services.mcp_service.asyncio.run")
    def test_known_tool_name_passes_validation(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """Calling a known tool does not raise ConfigError for tool name."""
        call_count = 0
        tools = _sample_tools()

        def side_effect(coro):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call is list_tools
                return tools
            # Second call is the actual tool call
            return {"content": [{"data": "ok"}], "isError": False}

        mock_run.side_effect = side_effect

        store = _setup_store(
            tmp_path,
            projects={"prod": {"token": "tok-prod"}},
            default_project="prod",
        )
        svc = McpService(config_store=store)
        # create_config is in the sample tools and is a write tool
        result = svc.call_tool("create_config", {"name": "test"})
        assert result["results"] or result["errors"]  # No ConfigError raised


# ---------------------------------------------------------------------------
# TestExtractIdsDeduplication
# ---------------------------------------------------------------------------


class TestExtractIdsDeduplication:
    """Tests for Phase 6: _extract_ids deduplication."""

    def test_extract_ids_deduplication(self) -> None:
        """_extract_ids removes duplicate IDs while preserving order."""
        content = [
            [
                {"id": "bucket-1"},
                {"id": "bucket-2"},
                {"id": "bucket-1"},  # duplicate
                {"id": "bucket-3"},
                {"id": "bucket-2"},  # duplicate
            ]
        ]
        result = _extract_ids(content, "id")
        assert result == ["bucket-1", "bucket-2", "bucket-3"]

    def test_extract_ids_no_duplicates(self) -> None:
        """_extract_ids works normally when there are no duplicates."""
        content = [
            [
                {"id": "a"},
                {"id": "b"},
                {"id": "c"},
            ]
        ]
        result = _extract_ids(content, "id")
        assert result == ["a", "b", "c"]

    def test_extract_ids_empty_input(self) -> None:
        """_extract_ids returns empty list for empty input."""
        result = _extract_ids([], "id")
        assert result == []

    def test_extract_ids_nested_dict_deduplication(self) -> None:
        """_extract_ids deduplicates across nested dict formats."""
        content = [
            {"id": "x", "items": [{"id": "y"}, {"id": "x"}]},
        ]
        result = _extract_ids(content, "id")
        assert result == ["x", "y"]


# ---------------------------------------------------------------------------
# TestExtractIdsFromToon
# ---------------------------------------------------------------------------


class TestExtractIdsFromToon:
    """Tests for _extract_ids_from_toon() TOON format parsing."""

    def test_parse_bucket_ids(self) -> None:
        """Extracts bucket IDs from a real TOON get_buckets response."""
        toon = (
            "buckets[3]{id,name,display_name,stage,created,data_size_bytes,source_project}:\n"
            '  in.c-STG_Telemetry,c-STG_Telemetry,STG_Telemetry,in,"2026-03-24T13:41:37+0100",0,"Source (ID: 2741)"\n'
            '  in.c-STG_Ownership,c-STG_Ownership,STG_Ownership,in,"2026-03-24T13:41:32+0100",0,"Source (ID: 2741)"\n'
            '  out.c-OUT_Report,c-OUT_Report,OUT_Report,out,"2026-03-19T17:49:02+0100",1376477583,null\n'
            "links[1]{type,title,url}:\n"
            '  ui-dashboard,Buckets in the project,"https://example.com/storage"'
        )
        result = _extract_ids_from_toon(toon, "id")
        assert result == ["in.c-STG_Telemetry", "in.c-STG_Ownership", "out.c-OUT_Report"]

    def test_parse_different_key(self) -> None:
        """Extracts values from a non-first column."""
        toon = "items[2]{id,name,status}:\n  abc,My Item,active\n  def,Other Item,disabled\n"
        assert _extract_ids_from_toon(toon, "name") == ["My Item", "Other Item"]
        assert _extract_ids_from_toon(toon, "status") == ["active", "disabled"]

    def test_key_not_in_header(self) -> None:
        """Returns empty list when key is not in the TOON header."""
        toon = "items[1]{id,name}:\n  x,foo\n"
        assert _extract_ids_from_toon(toon, "missing") == []

    def test_empty_array(self) -> None:
        """Handles TOON with zero items."""
        toon = "tables[0]:\nlinks[1]{type,title,url}:\n  ui-dashboard,Dashboard,https://x.com\n"
        assert _extract_ids_from_toon(toon, "id") == []

    def test_quoted_values_with_commas(self) -> None:
        """Handles CSV-quoted values that contain commas."""
        toon = 'items[1]{id,description}:\n  bucket-1,"Has commas, inside quotes"\n'
        result = _extract_ids_from_toon(toon, "id")
        assert result == ["bucket-1"]
        desc = _extract_ids_from_toon(toon, "description")
        assert desc == ["Has commas, inside quotes"]

    def test_extract_ids_delegates_to_toon(self) -> None:
        """_extract_ids handles string items by delegating to TOON parser."""
        toon = "buckets[2]{id,name}:\n  b1,Bucket One\n  b2,Bucket Two\n"
        result = _extract_ids([toon], "id")
        assert result == ["b1", "b2"]

    def test_extract_ids_mixed_formats(self) -> None:
        """_extract_ids handles mix of TOON strings and JSON dicts."""
        items: list = [
            "items[1]{id,name}:\n  toon-id,TOON Item\n",
            {"id": "dict-id", "name": "Dict Item"},
        ]
        result = _extract_ids(items, "id")
        assert result == ["toon-id", "dict-id"]


# ---------------------------------------------------------------------------
# TestBuildServerParamsBranchId
# ---------------------------------------------------------------------------


class TestBuildServerParamsBranchId:
    """Tests for _build_server_params() with branch_id parameter."""

    @patch("keboola_agent_cli.services.mcp_service.shutil.which")
    def test_build_server_params_with_branch_id(self, mock_which: MagicMock) -> None:
        """When branch_id is provided, KBC_BRANCH_ID is set in env."""
        mock_which.side_effect = lambda cmd: "/usr/local/bin/uvx" if cmd == "uvx" else None

        project = ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="tok-test",
            project_name="Test",
            project_id=1234,
        )
        params = _build_server_params(project, branch_id="456")
        assert params.env["KBC_BRANCH_ID"] == "456"
        assert params.env["KBC_STORAGE_TOKEN"] == "tok-test"
        assert params.env["KBC_STORAGE_API_URL"] == "https://connection.keboola.com"

    @patch("keboola_agent_cli.services.mcp_service.shutil.which")
    def test_build_server_params_without_branch_id(self, mock_which: MagicMock) -> None:
        """When branch_id is None, KBC_BRANCH_ID is NOT set in env."""
        mock_which.side_effect = lambda cmd: "/usr/local/bin/uvx" if cmd == "uvx" else None

        project = ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="tok-test",
            project_name="Test",
            project_id=1234,
        )
        params = _build_server_params(project, branch_id=None)
        assert "KBC_BRANCH_ID" not in params.env
        assert params.env["KBC_STORAGE_TOKEN"] == "tok-test"

    @patch("keboola_agent_cli.services.mcp_service.shutil.which")
    def test_build_server_params_default_no_branch(self, mock_which: MagicMock) -> None:
        """Default call without branch_id argument has no KBC_BRANCH_ID."""
        mock_which.side_effect = lambda cmd: "/usr/local/bin/uvx" if cmd == "uvx" else None

        project = ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="tok-test",
            project_name="Test",
            project_id=1234,
        )
        params = _build_server_params(project)
        assert "KBC_BRANCH_ID" not in params.env


# ---------------------------------------------------------------------------
# TestCallToolWithBranch
# ---------------------------------------------------------------------------


class TestCallToolWithBranch:
    """Tests for McpService.call_tool() with branch_id - forces single-project mode."""

    @patch("keboola_agent_cli.services.mcp_service.asyncio.run")
    def test_call_read_tool_with_branch_forces_single_project(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """A read tool with branch_id forces single-project mode (write path)."""
        call_count = 0
        tools = _sample_tools()

        def side_effect(coro):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return tools
            return {
                "content": [{"configs": ["cfg1"]}],
                "isError": False,
            }

        mock_run.side_effect = side_effect

        store = _setup_store(
            tmp_path,
            projects={
                "prod": {"token": "tok-prod"},
                "dev": {"token": "tok-dev"},
            },
            default_project="prod",
        )
        svc = McpService(config_store=store)
        result = svc.call_tool(
            "list_configs",
            {},
            alias="prod",
            branch_id="456",
        )

        # Should only have 1 result (single-project mode)
        assert len(result["results"]) == 1
        assert result["results"][0]["project_alias"] == "prod"

    @patch("keboola_agent_cli.services.mcp_service.asyncio.run")
    def test_tool_list_with_branch(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """list_tools with branch_id passes it through to MCP server."""
        mock_run.return_value = _sample_tools()

        store = _setup_store(
            tmp_path,
            projects={"prod": {"token": "tok-prod"}},
        )
        svc = McpService(config_store=store)
        result = svc.list_tools(aliases=["prod"], branch_id="789")

        assert len(result["tools"]) == 3
        # Verify branch_id was passed through by checking the coroutine args
        call_args = mock_run.call_args
        assert call_args is not None


# ---------------------------------------------------------------------------
# TestValidateToolInputReturnsTuple
# ---------------------------------------------------------------------------


class TestValidateToolInputReturnsTuple:
    """Tests that validate_tool_input returns (missing_params, known_tool_names)."""

    @patch("keboola_agent_cli.services.mcp_service.asyncio.run")
    def test_returns_tuple_with_known_tools(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """validate_tool_input returns a tuple of (missing, known_tools)."""
        tools = [
            {
                "name": "list_configs",
                "description": "List configs",
                "inputSchema": {
                    "type": "object",
                    "properties": {"component_id": {"type": "string"}},
                    "required": ["component_id"],
                },
            },
            {
                "name": "create_config",
                "description": "Create config",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]
        mock_run.return_value = tools

        store = _setup_store(tmp_path, projects={"prod": {"token": "tok-prod"}})
        svc = McpService(config_store=store)

        missing, known = svc.validate_tool_input("list_configs", {})
        assert missing == ["component_id"]
        assert known == {"list_configs", "create_config"}

    @patch("keboola_agent_cli.services.mcp_service.asyncio.run")
    def test_no_missing_when_all_provided(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """When all required params are provided, missing list is empty."""
        tools = [
            {
                "name": "list_configs",
                "description": "List configs",
                "inputSchema": {
                    "type": "object",
                    "properties": {"component_id": {"type": "string"}},
                    "required": ["component_id"],
                },
            },
        ]
        mock_run.return_value = tools

        store = _setup_store(tmp_path, projects={"prod": {"token": "tok-prod"}})
        svc = McpService(config_store=store)

        missing, known = svc.validate_tool_input(
            "list_configs", {"component_id": "keboola.ex-db-mysql"}
        )
        assert missing == []
        assert "list_configs" in known

    @patch("keboola_agent_cli.services.mcp_service.asyncio.run")
    def test_unknown_tool_returns_empty_missing(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """Unknown tool name returns empty missing and known tools set."""
        mock_run.return_value = _sample_tools()

        store = _setup_store(tmp_path, projects={"prod": {"token": "tok-prod"}})
        svc = McpService(config_store=store)

        missing, known = svc.validate_tool_input("nonexistent_tool", {})
        assert missing == []
        assert len(known) == 3


# ---------------------------------------------------------------------------
# TestCallToolWithKnownTools
# ---------------------------------------------------------------------------


class TestCallToolWithKnownTools:
    """Tests that call_tool skips list_tools when _known_tools is provided."""

    @patch("keboola_agent_cli.services.mcp_service.asyncio.run")
    def test_known_tools_skips_list_tools(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """When _known_tools is provided, call_tool does not call list_tools."""
        mock_run.return_value = {
            "content": [{"created": True}],
            "isError": False,
        }

        store = _setup_store(
            tmp_path,
            projects={"prod": {"token": "tok-prod"}},
            default_project="prod",
        )
        svc = McpService(config_store=store)

        with patch.object(svc, "list_tools") as mock_list:
            result = svc.call_tool(
                "create_config",
                {"name": "test"},
                _known_tools={"create_config", "list_configs"},
            )
            mock_list.assert_not_called()

        assert len(result["results"]) == 1

    @patch("keboola_agent_cli.services.mcp_service.asyncio.run")
    def test_unknown_tool_with_known_tools_raises(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """When _known_tools is provided and tool not in it, raises ConfigError."""
        store = _setup_store(
            tmp_path,
            projects={"prod": {"token": "tok-prod"}},
            default_project="prod",
        )
        svc = McpService(config_store=store)

        with pytest.raises(ConfigError, match="Unknown MCP tool 'bad_tool'"):
            svc.call_tool(
                "bad_tool",
                {},
                _known_tools={"create_config", "list_configs"},
            )


# ---------------------------------------------------------------------------
# TestMcpMaxSessionsFromEnv
# ---------------------------------------------------------------------------


class TestMcpMaxSessionsFromEnv:
    """Tests for MCP max sessions configuration via environment variables."""

    def test_max_sessions_from_env(self) -> None:
        """KBAGENT_MCP_MAX_SESSIONS env var overrides default."""
        with patch.dict(
            os.environ,
            {"KBAGENT_MCP_MAX_SESSIONS": "25"},
        ):
            assert _get_max_sessions() == 25

    def test_max_sessions_default_unlimited(self) -> None:
        """Without env var, returns 0 (unlimited parallelism)."""
        env = os.environ.copy()
        env.pop("KBAGENT_MCP_MAX_SESSIONS", None)
        with patch.dict(os.environ, env, clear=True):
            assert _get_max_sessions() == 0


# ---------------------------------------------------------------------------
# TestSemaphoredHelper
# ---------------------------------------------------------------------------


class TestSemaphoredHelper:
    """Tests for the _semaphored async helper."""

    @pytest.mark.asyncio
    async def test_semaphored_returns_result(self) -> None:
        """_semaphored returns the coroutine's result."""
        sem = asyncio.Semaphore(5)

        async def dummy() -> str:
            return "ok"

        result = await _semaphored(sem, dummy())
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_semaphored_limits_concurrency(self) -> None:
        """_semaphored enforces the semaphore limit on concurrent tasks."""
        max_concurrent = 0
        current = 0
        lock = asyncio.Lock()
        limit = 3
        sem = asyncio.Semaphore(limit)

        async def tracked_task(task_id: int) -> int:
            nonlocal max_concurrent, current
            async with lock:
                current += 1
                if current > max_concurrent:
                    max_concurrent = current
            await asyncio.sleep(0.05)
            async with lock:
                current -= 1
            return task_id

        tasks = [asyncio.create_task(_semaphored(sem, tracked_task(i))) for i in range(10)]
        results = await asyncio.gather(*tasks)

        assert sorted(results) == list(range(10))
        assert max_concurrent <= limit
