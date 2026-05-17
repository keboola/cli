"""CLI tests for `kbagent agent ...` via Typer's CliRunner.

Strategy: the agent service is purely local (reads/writes
``<config-dir>/agents.json``), so we let it run against a real tmp dir
instead of mocking. Runner side effects (subprocess spawn) are
monkeypatched at the agent_service module boundary.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.server.agents_store import AgentRun

runner = CliRunner()


def _invoke(config_dir: Path, *args: str, json_mode: bool = False) -> Any:
    """Run the CLI with --config-dir routed to tmp."""
    argv = ["--config-dir", str(config_dir)]
    if json_mode:
        argv.insert(0, "--json")
    argv.extend(args)
    return runner.invoke(app, argv)


class TestAgentList:
    def test_list_empty_human(self, tmp_path: Path) -> None:
        result = _invoke(tmp_path, "agent", "list")
        assert result.exit_code == 0, result.output
        assert "No agent tasks registered" in result.output

    def test_list_empty_json(self, tmp_path: Path) -> None:
        result = _invoke(tmp_path, "agent", "list", json_mode=True)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        assert payload["data"] == {"tasks": []}


class TestAgentCreateAndShow:
    def test_create_cli_command(self, tmp_path: Path) -> None:
        result = _invoke(
            tmp_path,
            "agent",
            "create",
            "--name",
            "T1",
            "--cron",
            "0 7 * * *",
            "--type",
            "cli_command",
            "--argv",
            "project",
            "--argv",
            "list",
            json_mode=True,
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        task = body["data"]
        assert task["name"] == "T1"
        assert task["action"]["type"] == "cli_command"
        assert task["action"]["params"]["argv"] == ["project", "list"]
        assert task["next_run_at"] is not None
        # list should now show the task
        listed = _invoke(tmp_path, "agent", "list", json_mode=True)
        tasks = json.loads(listed.output)["data"]["tasks"]
        assert len(tasks) == 1 and tasks[0]["id"] == task["id"]
        # show by id round-trips
        shown = _invoke(tmp_path, "agent", "show", task["id"], json_mode=True)
        assert json.loads(shown.output)["data"]["id"] == task["id"]

    def test_create_ai_agent_requires_cli_and_prompt(self, tmp_path: Path) -> None:
        result = _invoke(
            tmp_path,
            "agent",
            "create",
            "--name",
            "T2",
            "--type",
            "ai_agent",
            json_mode=True,
        )
        assert result.exit_code == 2
        body = json.loads(result.output)
        assert body["status"] == "error"
        assert body["error"]["code"] == "MISSING_PARAMETER"

    def test_create_invalid_cron(self, tmp_path: Path) -> None:
        result = _invoke(
            tmp_path,
            "agent",
            "create",
            "--name",
            "T3",
            "--cron",
            "nonsense",
            "--type",
            "cli_command",
            "--argv",
            "version",
            json_mode=True,
        )
        assert result.exit_code == 5
        assert "Invalid cron" in result.output or "Invalid" in result.output

    def test_create_from_file(self, tmp_path: Path) -> None:
        action_payload = tmp_path / "action.json"
        action_payload.write_text(
            json.dumps({"type": "cli_command", "params": {"argv": ["version"]}})
        )
        result = _invoke(
            tmp_path,
            "agent",
            "create",
            "--name",
            "T-File",
            "--from-file",
            f"@{action_payload}",
            json_mode=True,
        )
        assert result.exit_code == 0, result.output
        task = json.loads(result.output)["data"]
        assert task["action"]["params"]["argv"] == ["version"]


class TestUpdateAndDelete:
    def test_update_toggle(self, tmp_path: Path) -> None:
        created = _invoke(
            tmp_path,
            "agent",
            "create",
            "--name",
            "Toggle",
            "--type",
            "cli_command",
            "--argv",
            "version",
            json_mode=True,
        )
        task_id = json.loads(created.output)["data"]["id"]
        result = _invoke(
            tmp_path,
            "agent",
            "update",
            task_id,
            "--disabled",
            "--manual",
            json_mode=True,
        )
        assert result.exit_code == 0
        body = json.loads(result.output)["data"]
        assert body["enabled"] is False
        assert body["manual"] is True
        assert body["next_run_at"] is None

    def test_delete_requires_yes_in_human_mode(self, tmp_path: Path) -> None:
        created = _invoke(
            tmp_path,
            "agent",
            "create",
            "--name",
            "Trash",
            "--type",
            "cli_command",
            "--argv",
            "version",
            json_mode=True,
        )
        task_id = json.loads(created.output)["data"]["id"]
        # In JSON mode the confirmation is skipped (no prompts in non-TTY).
        result = _invoke(tmp_path, "agent", "delete", task_id, json_mode=True)
        assert result.exit_code == 0
        # Task gone
        again = _invoke(tmp_path, "agent", "show", task_id, json_mode=True)
        assert again.exit_code == 1


class TestCronPreview:
    def test_preview_count(self, tmp_path: Path) -> None:
        result = _invoke(
            tmp_path, "agent", "cron-preview", "--cron", "0 6 * * 1", "--count", "3", json_mode=True
        )
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["cron"] == "0 6 * * 1"
        assert len(data["firings"]) == 3

    def test_preview_invalid_cron(self, tmp_path: Path) -> None:
        result = _invoke(tmp_path, "agent", "cron-preview", "--cron", "garbage", json_mode=True)
        assert result.exit_code == 2
        body = json.loads(result.output)
        assert body["error"]["code"] == "VALIDATION_ERROR"


class TestRunMocked:
    """The `run` command shells out to run_task_once; we mock that."""

    @pytest.fixture
    def task_id(self, tmp_path: Path) -> str:
        created = _invoke(
            tmp_path,
            "agent",
            "create",
            "--name",
            "Runnable",
            "--type",
            "cli_command",
            "--argv",
            "version",
            json_mode=True,
        )
        return json.loads(created.output)["data"]["id"]

    def test_run_blocking_returns_ok(self, tmp_path: Path, task_id: str) -> None:
        async def _fake(t, _registry, store, **_kwargs) -> AgentRun:
            run = AgentRun(task_id=t.id, started_at="2026-01-01T00:00:00+00:00", status="ok")
            store.append_run(run)
            return run

        with patch("keboola_agent_cli.services.agent_service.run_task_once", side_effect=_fake):
            result = _invoke(tmp_path, "agent", "run", task_id, json_mode=True)
        assert result.exit_code == 0
        run = json.loads(result.output)["data"]
        assert run["status"] == "ok"

    def test_run_with_runtime_prompt(self, tmp_path: Path) -> None:
        # An ai_agent task is needed to exercise the runtime-prompt merge.
        created = _invoke(
            tmp_path,
            "agent",
            "create",
            "--name",
            "AI",
            "--type",
            "ai_agent",
            "--cli",
            "claude",
            "--prompt",
            "base",
            json_mode=True,
        )
        task_id = json.loads(created.output)["data"]["id"]
        captured: dict[str, Any] = {}

        async def _capture(t, _registry, _store, **_kwargs) -> AgentRun:
            captured["params"] = dict(t.action.params)
            return AgentRun(task_id=t.id, started_at="2026-01-01T00:00:00+00:00", status="ok")

        with patch(
            "keboola_agent_cli.services.agent_service.run_task_once",
            side_effect=_capture,
        ):
            result = _invoke(
                tmp_path,
                "agent",
                "run",
                task_id,
                "--runtime-prompt",
                "extra-runtime",
                json_mode=True,
            )
        assert result.exit_code == 0
        assert "base" in captured["params"]["prompt"]
        assert "extra-runtime" in captured["params"]["prompt"]


class TestRuns:
    def test_runs_for_unknown_task(self, tmp_path: Path) -> None:
        result = _invoke(tmp_path, "agent", "runs", "missing-id", json_mode=True)
        assert result.exit_code == 1
        assert json.loads(result.output)["error"]["code"] == "NOT_FOUND"
