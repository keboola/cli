"""Tests for the mcp_tool task detection surfaces (epic #390 phase 2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.mcp_parity import MCP_REMOVAL_VERSION, annotate_mcp_tool_deprecation
from keboola_agent_cli.services.doctor_service import DoctorService


def _write_agents(config_dir: Path, tasks: list[dict[str, Any]]) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    # agents.json is a bare JSON list, not an object with a "tasks" key.
    (config_dir / "agents.json").write_text(json.dumps(tasks), encoding="utf-8")


def _task(task_id: str, action_type: str, **params: Any) -> dict[str, Any]:
    return {
        "id": task_id,
        "name": f"task {task_id}",
        "cron": "0 6 * * *",
        "enabled": True,
        "manual": False,
        "action": {"type": action_type, "params": params},
    }


def _doctor(config_dir: Path) -> DoctorService:
    return DoctorService(ConfigStore(config_dir=config_dir))


class TestDoctorMcpToolTaskCheck:
    """`kbagent doctor` is the standing reminder for tasks that break silently."""

    def test_skips_without_agents_json(self, tmp_path):
        result = _doctor(tmp_path)._check_mcp_tool_tasks()
        assert result["status"] == "skip"
        assert result["check"] == "mcp_tool_tasks"

    def test_passes_when_no_task_uses_mcp_tool(self, tmp_path):
        _write_agents(tmp_path, [_task("t1", "cli_command", argv=["job", "list"])])
        result = _doctor(tmp_path)._check_mcp_tool_tasks()
        assert result["status"] == "pass"

    def test_warns_and_names_the_removal_version(self, tmp_path):
        _write_agents(tmp_path, [_task("t1", "mcp_tool", tool="create_config")])
        result = _doctor(tmp_path)._check_mcp_tool_tasks()
        assert result["status"] == "warn"
        assert MCP_REMOVAL_VERSION in result["message"]
        # The whole point: these run unattended and get no warning at removal.
        assert "no warning" in result["message"].lower()

    def test_details_carry_the_native_command_per_task(self, tmp_path):
        _write_agents(tmp_path, [_task("t1", "mcp_tool", tool="create_config")])
        result = _doctor(tmp_path)._check_mcp_tool_tasks()
        entry = result["details"]["tasks"][0]
        assert entry["id"] == "t1"
        assert entry["tool"] == "create_config"
        assert entry["native_command"] == "kbagent config new"

    def test_unknown_tool_degrades_to_no_native_command(self, tmp_path):
        """A tool absent from the parity map must not invent a replacement."""
        _write_agents(tmp_path, [_task("t1", "mcp_tool", tool="no_such_tool")])
        entry = _doctor(tmp_path)._check_mcp_tool_tasks()["details"]["tasks"][0]
        assert entry["native_command"] is None

    def test_long_lists_are_truncated_but_complete_in_details(self, tmp_path):
        _write_agents(
            tmp_path, [_task(f"t{i}", "mcp_tool", tool="create_config") for i in range(8)]
        )
        result = _doctor(tmp_path)._check_mcp_tool_tasks()
        assert "+3 more" in result["message"]
        assert len(result["details"]["tasks"]) == 8

    def test_corrupt_agents_json_warns_instead_of_a_false_all_clear(self, tmp_path):
        """A file we cannot read must never render as "nothing to migrate".

        AgentStore.load_tasks() swallows a corrupt file and returns [], which is
        right for the scheduler and wrong here: the earlier version of this
        check reported `pass` with "0 checked" for a damaged file, giving a
        clean bill of health to someone whose tasks it could not even see.
        """
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "agents.json").write_text("{ not json", encoding="utf-8")
        result = _doctor(tmp_path)._check_mcp_tool_tasks()
        assert result["status"] == "warn"
        assert "could not be checked" in result["message"].lower()

    def test_non_list_agents_json_warns(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "agents.json").write_text('{"tasks": []}', encoding="utf-8")
        result = _doctor(tmp_path)._check_mcp_tool_tasks()
        assert result["status"] == "warn"
        assert "not a JSON list" in result["message"]

    def test_skipped_invalid_entries_are_reported_not_hidden(self, tmp_path):
        """load_tasks() drops entries that fail validation -- say so."""
        _write_agents(
            tmp_path,
            [_task("good", "cli_command", argv=["job", "list"]), {"id": "bad", "no": "name"}],
        )
        result = _doctor(tmp_path)._check_mcp_tool_tasks()
        assert result["status"] == "warn"
        assert "1 of 2 entries" in result["message"]

    def test_check_is_registered_in_run_checks(self, tmp_path):
        _write_agents(tmp_path, [_task("t1", "mcp_tool", tool="create_config")])
        checks = _doctor(tmp_path).run_checks()["checks"]
        assert any(c["check"] == "mcp_tool_tasks" for c in checks)


class TestAgentListFooter:
    """The marker lives under the table, not inside the Type cell.

    An inline tag was truncated to "DEPRECA…" by Rich -- the Type column is one
    of seven and too narrow -- which is worse than not flagging it at all.
    """

    @staticmethod
    def _render(tasks: list[dict[str, Any]]) -> str:
        from io import StringIO

        from rich.console import Console

        from keboola_agent_cli.commands.agent import _render_tasks_table

        buf = StringIO()
        _render_tasks_table(Console(file=buf, width=200, force_terminal=False), {"tasks": tasks})
        return buf.getvalue()

    def test_footer_names_the_task_and_the_version(self):
        out = self._render([_task("nightly01", "mcp_tool", tool="create_config")])
        assert "nightly01" in out
        assert MCP_REMOVAL_VERSION in out
        assert "kbagent doctor" in out

    def test_no_footer_without_mcp_tool_tasks(self):
        out = self._render([_task("t1", "cli_command", argv=["job", "list"])])
        assert MCP_REMOVAL_VERSION not in out

    def test_type_cell_is_not_padded_with_a_truncatable_tag(self):
        """Guard against re-introducing the inline marker."""
        out = self._render([_task("t1", "mcp_tool", tool="create_config")])
        assert "DEPRECA" not in out.split("REMOVED")[0]


class TestAgentListDeprecationKey:
    """`--json` consumers must be able to find affected tasks without scraping."""

    def test_mcp_tool_task_gains_the_key(self):
        annotated = annotate_mcp_tool_deprecation(_task("t1", "mcp_tool", tool="create_config"))
        assert MCP_REMOVAL_VERSION in annotated["deprecation"]

    def test_serve_route_uses_the_same_annotator(self):
        """The Web UI reads /agents; its users never run `kbagent doctor`.

        Both front doors must flag the same tasks, which is why the annotator
        lives in mcp_parity rather than the command layer -- the server must not
        import commands/ to reach it.
        """
        import inspect

        from keboola_agent_cli.server.routers import agents as agents_router

        assert "annotate_mcp_tool_deprecation" in inspect.getsource(agents_router.list_tasks)

    def test_other_action_types_are_untouched(self):
        """Additive: every existing consumer must see a byte-identical payload."""
        for action_type in ("cli_command", "ai_agent"):
            payload = _task("t1", action_type)
            assert annotate_mcp_tool_deprecation(dict(payload)) == payload
