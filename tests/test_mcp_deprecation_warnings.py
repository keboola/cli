"""Deprecation warnings for the MCP passthrough (epic #390 phase 2).

The whole ``tool`` group and the ``agent --type mcp_tool`` action flavour
are deprecated in favor of native commands (``mcp_parity.MCP_TOOL_PARITY``).
These tests pin the surfacing contract:

- human mode: yellow warning on STDERR only -- stdout stays byte-clean,
- JSON mode: additive ``deprecation`` key inside the success envelope's
  data payload (no key renamed/removed; error paths carry no key),
- ``tool list`` tools gain an additive ``cli_equivalent`` key / column.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app

from .helpers import setup_single_project

runner = CliRunner()


def _flat(text: str) -> str:
    """Collapse whitespace so Rich line-wrapping cannot break substring asserts."""
    return " ".join(text.split())


def _config_dir(tmp_path: Path) -> Path:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    setup_single_project(config_dir, token="901-55555-fakeTestTokenDoNotUseXXXXXXXX")
    return config_dir


SAMPLE_TOOL_RESULT = {
    "results": [
        {
            "content": [{"name": "bucket-a"}],
            "isError": False,
            "project_alias": "prod",
        },
    ],
    "errors": [],
}

SAMPLE_TOOLS = [
    {
        "name": "get_buckets",
        "description": "B",
        "inputSchema": {},
        "multi_project": True,
    },
    {
        "name": "totally_unknown_tool",
        "description": "U",
        "inputSchema": {},
        "multi_project": False,
    },
]


class TestToolCallDeprecation:
    """`tool call` surfaces the per-tool deprecation message."""

    def test_human_mode_warns_on_stderr_stdout_clean(self, tmp_path: Path) -> None:
        with patch("keboola_agent_cli.cli.McpService") as MockMcpService:
            mock_mcp = MagicMock()
            mock_mcp.validate_and_call_tool.return_value = SAMPLE_TOOL_RESULT
            MockMcpService.return_value = mock_mcp

            result = runner.invoke(
                app,
                ["--config-dir", str(_config_dir(tmp_path)), "tool", "call", "get_buckets"],
            )

        assert result.exit_code == 0, result.output
        stderr = _flat(result.stderr)
        assert "Warning:" in stderr
        assert "MCP passthrough is deprecated (epic #390)" in stderr
        assert "use `kbagent storage buckets` instead" in stderr
        # stdout must stay clean: result rendering only, no deprecation noise
        stdout = _flat(result.stdout)
        assert "deprecated" not in stdout
        assert "bucket-a" in stdout

    def test_json_mode_envelope_gains_deprecation_key(self, tmp_path: Path) -> None:
        with patch("keboola_agent_cli.cli.McpService") as MockMcpService:
            mock_mcp = MagicMock()
            mock_mcp.validate_and_call_tool.return_value = SAMPLE_TOOL_RESULT
            MockMcpService.return_value = mock_mcp

            result = runner.invoke(
                app,
                [
                    "--json",
                    "--config-dir",
                    str(_config_dir(tmp_path)),
                    "tool",
                    "call",
                    "get_buckets",
                ],
            )

        assert result.exit_code == 0, result.output
        # stdout must be a single valid JSON document
        payload = json.loads(result.stdout)
        assert payload["status"] == "ok"
        data = payload["data"]
        # additive key only: existing keys untouched
        assert data["results"][0]["project_alias"] == "prod"
        assert data["errors"] == []
        assert "storage buckets" in data["deprecation"]
        assert "epic #390" in data["deprecation"]
        # JSON mode never prints the warning separately
        assert "deprecated" not in result.stderr

    def test_json_mode_unmapped_tool_still_gets_message(self, tmp_path: Path) -> None:
        with patch("keboola_agent_cli.cli.McpService") as MockMcpService:
            mock_mcp = MagicMock()
            mock_mcp.validate_and_call_tool.return_value = SAMPLE_TOOL_RESULT
            MockMcpService.return_value = mock_mcp

            result = runner.invoke(
                app,
                [
                    "--json",
                    "--config-dir",
                    str(_config_dir(tmp_path)),
                    "tool",
                    "call",
                    "totally_unknown_tool",
                ],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert "no native equivalent yet" in payload["data"]["deprecation"]

    def test_error_path_carries_no_deprecation_key(self, tmp_path: Path) -> None:
        from keboola_agent_cli.errors import ConfigError

        with patch("keboola_agent_cli.cli.McpService") as MockMcpService:
            mock_mcp = MagicMock()
            mock_mcp.validate_and_call_tool.side_effect = ConfigError("Unknown MCP tool: nope")
            MockMcpService.return_value = mock_mcp

            result = runner.invoke(
                app,
                [
                    "--json",
                    "--config-dir",
                    str(_config_dir(tmp_path)),
                    "tool",
                    "call",
                    "get_buckets",
                ],
            )

        assert result.exit_code == 5
        payload = json.loads(result.stdout)
        assert payload["status"] == "error"
        assert "deprecation" not in payload
        assert "deprecation" not in payload["error"]


class TestToolListDeprecation:
    """`tool list` enriches tools with cli_equivalent + generic banner."""

    def test_json_tools_carry_cli_equivalent_and_envelope_deprecation(self, tmp_path: Path) -> None:
        with patch("keboola_agent_cli.cli.McpService") as MockMcpService:
            mock_mcp = MagicMock()
            mock_mcp.list_tools.return_value = {"tools": SAMPLE_TOOLS, "errors": []}
            MockMcpService.return_value = mock_mcp

            result = runner.invoke(
                app,
                ["--json", "--config-dir", str(_config_dir(tmp_path)), "tool", "list"],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "ok"
        data = payload["data"]
        tools = {t["name"]: t for t in data["tools"]}
        assert tools["get_buckets"]["cli_equivalent"] == "storage buckets"
        assert tools["totally_unknown_tool"]["cli_equivalent"] == ""
        # pre-existing keys untouched
        assert tools["get_buckets"]["multi_project"] is True
        assert data["errors"] == []
        assert "MCP passthrough is deprecated" in data["deprecation"]
        assert "cli_equivalent" in data["deprecation"]

    def test_human_mode_shows_column_and_banner(self, tmp_path: Path) -> None:
        with patch("keboola_agent_cli.cli.McpService") as MockMcpService:
            mock_mcp = MagicMock()
            # single short tool keeps the Rich table narrow enough that no
            # cell/header wraps at the default 80-column test console
            mock_mcp.list_tools.return_value = {
                "tools": [SAMPLE_TOOLS[0]],
                "errors": [],
            }
            MockMcpService.return_value = mock_mcp

            result = runner.invoke(
                app,
                ["--config-dir", str(_config_dir(tmp_path)), "tool", "list"],
            )

        assert result.exit_code == 0, result.output
        stdout = _flat(result.stdout)
        assert "CLI equivalent" in stdout
        assert "storage buckets" in stdout
        assert "MCP passthrough is deprecated" in stdout
        assert "see the cli_equivalent column" in stdout


class TestAgentMcpToolDeprecation:
    """`agent create/update` with an mcp_tool action warns without blocking."""

    def test_create_mcp_tool_human_warns_on_stderr(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "--config-dir",
                str(tmp_path),
                "agent",
                "create",
                "--name",
                "T-mcp",
                "--type",
                "mcp_tool",
                "--tool",
                "get_buckets",
            ],
        )
        assert result.exit_code == 0, result.output
        stderr = _flat(result.stderr)
        assert "Warning:" in stderr
        assert "agent action type 'mcp_tool' is deprecated (epic #390)" in stderr
        assert "--type cli_command" in stderr
        # creation is NOT blocked
        assert "Created" in result.stdout

    def test_create_mcp_tool_json_gains_deprecation_key(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "--json",
                "--config-dir",
                str(tmp_path),
                "agent",
                "create",
                "--name",
                "T-mcp",
                "--type",
                "mcp_tool",
                "--tool",
                "get_buckets",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "ok"
        task = payload["data"]
        assert task["action"]["type"] == "mcp_tool"
        assert "mcp_tool' is deprecated" in task["deprecation"]
        assert "deprecated" not in result.stderr

        # agents.json schema is unchanged: re-reading the task shows no
        # deprecation field was persisted
        shown = runner.invoke(
            app,
            ["--json", "--config-dir", str(tmp_path), "agent", "show", task["id"]],
        )
        assert shown.exit_code == 0, shown.output
        assert "deprecation" not in json.loads(shown.stdout)["data"]

    def test_create_cli_command_has_no_warning(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "--json",
                "--config-dir",
                str(tmp_path),
                "agent",
                "create",
                "--name",
                "T-cli",
                "--type",
                "cli_command",
                "--argv",
                "version",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert "deprecation" not in payload["data"]
        assert "deprecated" not in result.stderr

        human = runner.invoke(
            app,
            [
                "--config-dir",
                str(tmp_path),
                "agent",
                "create",
                "--name",
                "T-cli-2",
                "--type",
                "cli_command",
                "--argv",
                "version",
            ],
        )
        assert human.exit_code == 0, human.output
        assert "deprecated" not in human.stderr

    def test_update_mcp_tool_task_warns(self, tmp_path: Path) -> None:
        created = runner.invoke(
            app,
            [
                "--json",
                "--config-dir",
                str(tmp_path),
                "agent",
                "create",
                "--name",
                "T-upd",
                "--type",
                "mcp_tool",
                "--tool",
                "get_buckets",
            ],
        )
        task_id = json.loads(created.stdout)["data"]["id"]

        updated = runner.invoke(
            app,
            [
                "--json",
                "--config-dir",
                str(tmp_path),
                "agent",
                "update",
                task_id,
                "--name",
                "T-upd-renamed",
            ],
        )
        assert updated.exit_code == 0, updated.output
        payload = json.loads(updated.stdout)
        assert "mcp_tool' is deprecated" in payload["data"]["deprecation"]

        human = runner.invoke(
            app,
            [
                "--config-dir",
                str(tmp_path),
                "agent",
                "update",
                task_id,
                "--name",
                "T-upd-again",
            ],
        )
        assert human.exit_code == 0, human.output
        assert "mcp_tool' is deprecated" in _flat(human.stderr)

    def test_update_cli_command_task_has_no_warning(self, tmp_path: Path) -> None:
        created = runner.invoke(
            app,
            [
                "--json",
                "--config-dir",
                str(tmp_path),
                "agent",
                "create",
                "--name",
                "T-plain",
                "--type",
                "cli_command",
                "--argv",
                "version",
            ],
        )
        task_id = json.loads(created.stdout)["data"]["id"]

        updated = runner.invoke(
            app,
            [
                "--json",
                "--config-dir",
                str(tmp_path),
                "agent",
                "update",
                task_id,
                "--name",
                "T-plain-renamed",
            ],
        )
        assert updated.exit_code == 0, updated.output
        assert "deprecation" not in json.loads(updated.stdout)["data"]
