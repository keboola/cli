"""Tombstone semantics for the removed mcp_tool agent action (v0.85.0).

The literal stays in ``ActionType`` on purpose so a persisted task
round-trips through load/save without being silently deleted; every
execution and creation path must nevertheless refuse it.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.server.agent_runner import run_task_once
from keboola_agent_cli.server.agents_store import (
    REMOVED_ACTION_MESSAGE,
    REMOVED_ACTION_TYPES,
    AgentAction,
    AgentStore,
    AgentTask,
    annotate_removed_action,
)
from keboola_agent_cli.services.doctor_service import DoctorService


def _mcp_task(name: str = "legacy") -> AgentTask:
    return AgentTask(
        name=name,
        enabled=True,
        action=AgentAction(type="mcp_tool", params={"tool": "get_jobs", "project": "padak"}),
    )


class TestRoundTrip:
    def test_mcp_tool_task_survives_unrelated_write(self, tmp_path) -> None:
        store = AgentStore(config_dir=tmp_path)
        store.save_tasks([_mcp_task()])
        # unrelated write: upsert a different task, then reload
        store.upsert_task(
            AgentTask(
                name="new", action=AgentAction(type="cli_command", params={"argv": ["version"]})
            )
        )
        names = {t.name for t in store.load_tasks()}
        assert "legacy" in names, "tombstone task must NOT be dropped by an unrelated save"

    def test_action_type_still_validates(self) -> None:
        assert _mcp_task().action.type == "mcp_tool"
        assert "mcp_tool" in REMOVED_ACTION_TYPES


class TestExecutionRefusal:
    def test_run_task_once_persists_error(self, tmp_path) -> None:
        store = AgentStore(config_dir=tmp_path)
        task = store.upsert_task(_mcp_task())
        run = asyncio.run(run_task_once(task, SimpleNamespace(), store))
        assert run.status == "error"
        assert run.error is not None and "REMOVED" in run.error
        # persisted, not just returned
        assert store.list_runs(task.id)[0].status == "error"


class TestSchedulerSkip:
    def test_tombstone_task_is_not_dispatchable_even_when_enabled(self) -> None:
        from keboola_agent_cli.server.agent_runner import is_dispatchable

        task = _mcp_task()
        assert task.enabled is True
        assert is_dispatchable(task) is False

    def test_live_types_are_dispatchable(self) -> None:
        from keboola_agent_cli.server.agent_runner import is_dispatchable

        t = AgentTask(
            name="x", action=AgentAction(type="cli_command", params={"argv": ["version"]})
        )
        assert is_dispatchable(t) is True


class TestAnnotate:
    def test_annotate_marks_removed(self) -> None:
        payload = _mcp_task().model_dump(mode="json")
        assert annotate_removed_action(payload)["deprecation"] == REMOVED_ACTION_MESSAGE

    def test_annotate_leaves_live_types_alone(self) -> None:
        t = AgentTask(
            name="x", action=AgentAction(type="cli_command", params={"argv": ["version"]})
        )
        assert "deprecation" not in annotate_removed_action(t.model_dump(mode="json"))


def _write_agents(config_dir: Path, tasks: list[dict[str, Any]]) -> None:
    """Write a raw agents.json (a bare JSON list, not an object)."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "agents.json").write_text(json.dumps(tasks), encoding="utf-8")


def _raw_task(task_id: str, action_type: str, **params: Any) -> dict[str, Any]:
    return {
        "id": task_id,
        "name": f"task {task_id}",
        "cron": "0 6 * * *",
        "enabled": True,
        "manual": False,
        "action": {"type": action_type, "params": params},
    }


def _doctor(config_dir: Path) -> DoctorService:
    """DoctorService over a real ConfigStore; the MCP probe is mocked out.

    ``_check_mcp_tool_tasks`` never touches the MCP service, but ``run_checks``
    does -- and a real ``McpService`` would go looking for a server subprocess.
    """
    return DoctorService(config_store=ConfigStore(config_dir=config_dir), mcp_service=MagicMock())


class TestDoctorTombstone:
    """`kbagent doctor` is the standing reminder: these tasks fail unattended."""

    def test_doctor_fails_on_tombstone_task(self, tmp_path) -> None:
        AgentStore(config_dir=tmp_path).save_tasks([_mcp_task()])
        check = _doctor(tmp_path)._check_mcp_tool_tasks()
        assert check["status"] == "fail"
        assert "docs/mcp-migration.md" in check["message"]
        assert check["details"]["removed_in"] == "0.85.0"

    def test_skips_without_agents_json(self, tmp_path) -> None:
        result = _doctor(tmp_path)._check_mcp_tool_tasks()
        assert result["status"] == "skip"
        assert result["check"] == "mcp_tool_tasks"

    def test_passes_when_no_task_uses_mcp_tool(self, tmp_path) -> None:
        _write_agents(tmp_path, [_raw_task("t1", "cli_command", argv=["job", "list"])])
        result = _doctor(tmp_path)._check_mcp_tool_tasks()
        assert result["status"] == "pass"
        assert "removed 'mcp_tool' action" in result["message"]

    def test_details_carry_the_native_command_per_task(self, tmp_path) -> None:
        _write_agents(tmp_path, [_raw_task("t1", "mcp_tool", tool="create_config")])
        entry = _doctor(tmp_path)._check_mcp_tool_tasks()["details"]["tasks"][0]
        assert entry["id"] == "t1"
        assert entry["tool"] == "create_config"
        assert entry["native_command"] == "kbagent config new"

    def test_unknown_tool_degrades_to_no_native_command(self, tmp_path) -> None:
        """A tool absent from the parity map must not invent a replacement."""
        _write_agents(tmp_path, [_raw_task("t1", "mcp_tool", tool="no_such_tool")])
        entry = _doctor(tmp_path)._check_mcp_tool_tasks()["details"]["tasks"][0]
        assert entry["native_command"] is None

    def test_long_lists_are_truncated_but_complete_in_details(self, tmp_path) -> None:
        _write_agents(
            tmp_path, [_raw_task(f"t{i}", "mcp_tool", tool="create_config") for i in range(8)]
        )
        result = _doctor(tmp_path)._check_mcp_tool_tasks()
        assert "+3 more" in result["message"]
        assert len(result["details"]["tasks"]) == 8

    def test_corrupt_agents_json_warns_instead_of_a_false_all_clear(self, tmp_path) -> None:
        """A file we cannot read must never render as "nothing to migrate".

        AgentStore.load_tasks() swallows a corrupt file and returns [], which is
        right for the scheduler and wrong here: reporting `pass` with "0 checked"
        would give a clean bill of health to someone whose tasks we cannot see.
        """
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "agents.json").write_text("{ not json", encoding="utf-8")
        result = _doctor(tmp_path)._check_mcp_tool_tasks()
        assert result["status"] == "warn"
        assert "could not be checked" in result["message"].lower()

    def test_non_list_agents_json_warns(self, tmp_path) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "agents.json").write_text('{"tasks": []}', encoding="utf-8")
        result = _doctor(tmp_path)._check_mcp_tool_tasks()
        assert result["status"] == "warn"
        assert "not a JSON list" in result["message"]

    def test_skipped_invalid_entries_are_reported_not_hidden(self, tmp_path) -> None:
        """load_tasks() drops entries that fail validation -- say so."""
        _write_agents(
            tmp_path,
            [_raw_task("good", "cli_command", argv=["job", "list"]), {"id": "bad", "no": "name"}],
        )
        result = _doctor(tmp_path)._check_mcp_tool_tasks()
        assert result["status"] == "warn"
        assert "1 of 2 entries" in result["message"]

    def test_check_is_registered_in_run_checks(self, tmp_path) -> None:
        _write_agents(tmp_path, [_raw_task("t1", "mcp_tool", tool="create_config")])
        checks = _doctor(tmp_path).run_checks()["checks"]
        assert any(c["check"] == "mcp_tool_tasks" for c in checks)


class TestAgentListFooter:
    """The marker lives under the table, not inside the Type cell.

    An inline tag was truncated to "DEPRECA..." by Rich -- the Type column is
    one of seven and too narrow -- which is worse than not flagging it at all.
    """

    @staticmethod
    def _render(tasks: list[dict[str, Any]]) -> str:
        from io import StringIO

        from rich.console import Console

        from keboola_agent_cli.commands.agent import _render_tasks_table

        buf = StringIO()
        _render_tasks_table(Console(file=buf, width=200, force_terminal=False), {"tasks": tasks})
        return buf.getvalue()

    def test_footer_names_the_task_and_the_migration_doc(self) -> None:
        out = self._render([_raw_task("nightly01", "mcp_tool", tool="create_config")])
        assert "nightly01" in out
        assert "REMOVED" in out
        assert "docs/mcp-migration.md" in out

    def test_no_footer_without_mcp_tool_tasks(self) -> None:
        out = self._render([_raw_task("t1", "cli_command", argv=["job", "list"])])
        assert "docs/mcp-migration.md" not in out

    def test_type_cell_is_not_padded_with_a_truncatable_tag(self) -> None:
        """Guard against re-introducing the inline marker."""
        out = self._render([_raw_task("t1", "mcp_tool", tool="create_config")])
        assert "DEPRECA" not in out.split("REMOVED")[0]


class TestBroadcasterRefusal:
    """The UI "Run live" path refuses too -- and persists the refusal."""

    def test_ui_run_publishes_error_done_and_persists(self, tmp_path) -> None:
        from keboola_agent_cli.server.run_broadcaster import _ActiveRun

        store = AgentStore(config_dir=tmp_path)
        task = store.upsert_task(_mcp_task())
        active = _ActiveRun(task, SimpleNamespace(), store)
        asyncio.run(active._run())

        done = [e for e in active.events if e["event"] == "done"]
        assert len(done) == 1
        assert done[0]["data"]["status"] == "error"
        assert done[0]["data"]["error"] == REMOVED_ACTION_MESSAGE
        assert active.final_run is not None and active.final_run.status == "error"
        # persisted, not just broadcast
        assert store.list_runs(task.id)[0].status == "error"


class TestCreationRefusal:
    def test_agent_create_mcp_tool_exits_2(self, tmp_config_dir) -> None:
        from typer.testing import CliRunner

        from keboola_agent_cli.cli import app

        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "--config-dir",
                str(tmp_config_dir),
                "agent",
                "create",
                "--name",
                "x",
                "--type",
                "mcp_tool",
            ],
        )
        assert result.exit_code == 2
        assert "REMOVED" in result.output

    def test_agent_create_from_file_mcp_tool_exits_2(self, tmp_path, tmp_config_dir) -> None:
        import json

        from typer.testing import CliRunner

        from keboola_agent_cli.cli import app

        payload = tmp_path / "action.json"
        payload.write_text(json.dumps({"type": "mcp_tool", "params": {"tool": "get_jobs"}}))
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "--config-dir",
                str(tmp_config_dir),
                "agent",
                "create",
                "--name",
                "x",
                "--from-file",
                f"@{payload}",
            ],
        )
        assert result.exit_code == 2
        assert "REMOVED" in result.output

    def test_agent_create_dropped_mcp_flags_are_gone(self) -> None:
        """The mcp_tool-only flags are off the interface, not merely inert.

        Read from the command's parameter list rather than rendered ``--help``:
        Rich wraps and truncates help by terminal width, so absence from the
        text would prove nothing.
        """
        import typer.main

        from keboola_agent_cli.cli import app

        # Duck-typed (Any) rather than isinstance(click.Group): Typer builds its
        # own Command/Group subclasses, so a click.Group check is False at runtime.
        node: Any = typer.main.get_command(app)
        create = node.commands["agent"].commands["create"]
        options = {opt for param in create.params for opt in param.opts}
        for flag in ("--tool", "--mcp-project", "--mcp-branch", "--input"):
            assert flag not in options
