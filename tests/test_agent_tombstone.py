"""Tombstone semantics for the removed mcp_tool agent action (v0.85.0).

The literal stays in ``ActionType`` on purpose so a persisted task
round-trips through load/save without being silently deleted; every
execution and creation path must nevertheless refuse it.
"""

import asyncio
from types import SimpleNamespace

from keboola_agent_cli.server.agent_runner import run_task_once
from keboola_agent_cli.server.agents_store import (
    REMOVED_ACTION_MESSAGE,
    REMOVED_ACTION_TYPES,
    AgentAction,
    AgentStore,
    AgentTask,
    annotate_removed_action,
)


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


class TestDoctorTombstone:
    def test_doctor_fails_on_tombstone_task(self, tmp_path) -> None:
        from unittest.mock import MagicMock

        from keboola_agent_cli.services.doctor_service import DoctorService

        store_dir = tmp_path
        AgentStore(config_dir=store_dir).save_tasks([_mcp_task()])
        config_store = MagicMock()
        config_store.config_dir = store_dir
        svc = DoctorService(config_store=config_store, mcp_service=MagicMock())
        check = svc._check_mcp_tool_tasks()
        assert check["status"] == "fail"
        assert "docs/mcp-migration.md" in check["message"]
        assert check["details"]["removed_in"] == "0.85.0"


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

        create = typer.main.get_command(app).commands["agent"].commands["create"]
        options = {opt for param in create.params for opt in param.opts}
        for flag in ("--tool", "--mcp-project", "--mcp-branch", "--input"):
            assert flag not in options
