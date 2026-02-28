"""Tests for McpService - MCP integration service for multi-project tool operations."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.mcp_service import (
    McpService,
    _is_write_tool,
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
    def test_uvx_available(self, mock_which: MagicMock) -> None:
        """When uvx is available, returns ['uvx', 'keboola_mcp_server']."""
        mock_which.side_effect = lambda cmd: "/usr/local/bin/uvx" if cmd == "uvx" else None
        result = detect_mcp_server_command()
        assert result == ["uvx", "keboola_mcp_server"]

    @patch("keboola_agent_cli.services.mcp_service.shutil.which")
    def test_keboola_mcp_server_available(self, mock_which: MagicMock) -> None:
        """When uvx is not available but keboola_mcp_server is, returns it directly."""
        def which_side_effect(cmd: str) -> str | None:
            if cmd == "keboola_mcp_server":
                return "/usr/local/bin/keboola_mcp_server"
            return None

        mock_which.side_effect = which_side_effect
        result = detect_mcp_server_command()
        assert result == ["keboola_mcp_server"]

    @patch("keboola_agent_cli.services.mcp_service.shutil.which")
    def test_python_fallback(self, mock_which: MagicMock) -> None:
        """When only python is available, returns python -m fallback."""
        def which_side_effect(cmd: str) -> str | None:
            if cmd == "python":
                return "/usr/bin/python"
            return None

        mock_which.side_effect = which_side_effect
        result = detect_mcp_server_command()
        assert result == ["python", "-m", "keboola_mcp_server"]

    @patch("keboola_agent_cli.services.mcp_service.shutil.which")
    def test_nothing_available(self, mock_which: MagicMock) -> None:
        """When nothing is available, returns None."""
        mock_which.return_value = None
        result = detect_mcp_server_command()
        assert result is None


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
    def test_list_tools_all_projects_fail(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
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
                {"content": [{"configs": ["cfg1", "cfg2"]}], "isError": False, "project_alias": "prod"},
                {"content": [{"configs": ["cfg1", "cfg2"]}], "isError": False, "project_alias": "dev"},
            ],
            "errors": [],
        }
        with patch.object(svc, "_gather_read_results", new_callable=AsyncMock, return_value=expected):
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
    def test_call_write_tool_single_project(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
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
    def test_call_write_tool_with_explicit_alias(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
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
    def test_call_tool_none_input_defaults_to_empty_dict(
        self, mock_call_tool: AsyncMock, tmp_path: Path
    ) -> None:
        """When tool_input is None, it defaults to an empty dict."""
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
    def test_read_tool_partial_failure(
        self, mock_call_tool: AsyncMock, tmp_path: Path
    ) -> None:
        """When some projects fail for a read tool, successful results and errors are both returned."""
        call_count = 0

        async def side_effect(project, tool_name, tool_input):
            nonlocal call_count
            call_count += 1
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
    def test_write_tool_failure_returns_error(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
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
        """When uvx is available, status is 'pass' with command info."""
        mock_which.side_effect = lambda cmd: "/usr/local/bin/uvx" if cmd == "uvx" else None

        store = _setup_store(tmp_path, projects={})
        svc = McpService(config_store=store)
        result = svc.check_server_available()

        assert result["check"] == "mcp_server"
        assert result["name"] == "MCP server"
        assert result["status"] == "pass"
        assert "uvx keboola_mcp_server" in result["message"]

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

    @patch("keboola_agent_cli.services.mcp_service.shutil.which")
    def test_server_available_via_python(
        self, mock_which: MagicMock, tmp_path: Path
    ) -> None:
        """When only python is available, status is 'pass' with python -m command."""
        def which_side_effect(cmd: str) -> str | None:
            if cmd == "python":
                return "/usr/bin/python"
            return None

        mock_which.side_effect = which_side_effect

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
# TestMcpTimeoutFromEnv
# ---------------------------------------------------------------------------


class TestMcpTimeoutFromEnv:
    """Tests for MCP timeout configuration via environment variables."""

    def test_mcp_timeout_from_env(self) -> None:
        """KBAGENT_MCP_TOOL_TIMEOUT and KBAGENT_MCP_INIT_TIMEOUT env vars override defaults."""
        import importlib
        import os

        import keboola_agent_cli.services.mcp_service as mcp_mod

        # Save original values
        orig_tool = mcp_mod.MCP_TOOL_TIMEOUT_SECONDS
        orig_init = mcp_mod.MCP_INIT_TIMEOUT_SECONDS

        try:
            with patch.dict(
                os.environ,
                {
                    "KBAGENT_MCP_TOOL_TIMEOUT": "120",
                    "KBAGENT_MCP_INIT_TIMEOUT": "45",
                },
            ):
                importlib.reload(mcp_mod)
                assert mcp_mod.MCP_TOOL_TIMEOUT_SECONDS == 120
                assert mcp_mod.MCP_INIT_TIMEOUT_SECONDS == 45
        finally:
            # Reload to restore original state
            importlib.reload(mcp_mod)

    def test_mcp_timeout_defaults(self) -> None:
        """Without env vars, MCP timeouts use default values from constants."""
        import importlib
        import os

        import keboola_agent_cli.services.mcp_service as mcp_mod
        from keboola_agent_cli.constants import (
            DEFAULT_MCP_INIT_TIMEOUT,
            DEFAULT_MCP_TOOL_TIMEOUT,
        )

        try:
            # Clear any env vars that might be set
            env = os.environ.copy()
            env.pop("KBAGENT_MCP_TOOL_TIMEOUT", None)
            env.pop("KBAGENT_MCP_INIT_TIMEOUT", None)
            with patch.dict(os.environ, env, clear=True):
                importlib.reload(mcp_mod)
                assert mcp_mod.MCP_TOOL_TIMEOUT_SECONDS == DEFAULT_MCP_TOOL_TIMEOUT
                assert mcp_mod.MCP_INIT_TIMEOUT_SECONDS == DEFAULT_MCP_INIT_TIMEOUT
        finally:
            importlib.reload(mcp_mod)
