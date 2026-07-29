"""Per-tool session firewall checks in ``kbagent tool call`` (issue #478).

The ``tool`` group callback checks only the coarse ``tool.call`` operation,
and the service-level check reads only the PERSISTED policy. The command
itself must therefore run the session engine against ``tool:<name>`` so a
session-only ``--deny-writes`` / ``--deny-destructive`` blocks individual
tools -- with the fail-closed classifier treating unknown names as
destructive.
"""

import json
from pathlib import Path

from typer.testing import CliRunner

from keboola_agent_cli.cli import app

from .helpers import setup_single_project

runner = CliRunner()


def _config_dir(tmp_path: Path) -> Path:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    setup_single_project(config_dir, token="901-55555-fakeTestTokenDoNotUseXXXXXXXX")
    return config_dir


class TestToolCallSessionFirewall:
    def test_deny_destructive_blocks_delete_tool(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "--json",
                "--config-dir",
                str(_config_dir(tmp_path)),
                "--deny-destructive",
                "tool",
                "call",
                "delete_bucket",
            ],
        )
        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        assert payload["error"]["code"] == "PERMISSION_DENIED"
        assert "tool:delete_bucket" in payload["error"]["message"]

    def test_deny_destructive_blocks_unknown_tool_fail_closed(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "--json",
                "--config-dir",
                str(_config_dir(tmp_path)),
                "--deny-destructive",
                "tool",
                "call",
                "frobnicate_project",
            ],
        )
        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        assert payload["error"]["code"] == "PERMISSION_DENIED"

    def test_deny_writes_blocks_run_job(self, tmp_path: Path) -> None:
        """run_job passed --deny-writes before 0.73.0 (classified read)."""
        result = runner.invoke(
            app,
            [
                "--json",
                "--config-dir",
                str(_config_dir(tmp_path)),
                "--deny-writes",
                "tool",
                "call",
                "run_job",
            ],
        )
        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        assert payload["error"]["code"] == "PERMISSION_DENIED"
