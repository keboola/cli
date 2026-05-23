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


# ──────────────────────────────────────────────────────────────────────
# Tests for the read-side CLI surface beyond `list` (B-2 review follow-up).
# Each subcommand gets at least one happy-path + one NOT_FOUND case so the
# error-code mapping is locked in.
# ──────────────────────────────────────────────────────────────────────


def _make_runnable_task(tmp_path: Path) -> str:
    """Create a cli_command task and return its id (shared by `show` /
    `run-detail` / `run-events` test groups)."""
    created = _invoke(
        tmp_path,
        "agent",
        "create",
        "--name",
        "Showable",
        "--type",
        "cli_command",
        "--argv",
        "version",
        json_mode=True,
    )
    assert created.exit_code == 0, created.output
    return json.loads(created.output)["data"]["id"]


class TestAgentShow:
    """`agent show` (B-2 follow-up: explicit CliRunner coverage was missing)."""

    def test_show_existing_returns_task_payload(self, tmp_path: Path) -> None:
        task_id = _make_runnable_task(tmp_path)
        result = _invoke(tmp_path, "agent", "show", task_id, json_mode=True)
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["status"] == "ok"
        task = body["data"]
        assert task["id"] == task_id
        assert task["action"]["type"] == "cli_command"
        assert task["action"]["params"]["argv"] == ["version"]

    def test_show_unknown_task_returns_not_found(self, tmp_path: Path) -> None:
        result = _invoke(tmp_path, "agent", "show", "no-such-id", json_mode=True)
        assert result.exit_code == 1
        assert json.loads(result.output)["error"]["code"] == "NOT_FOUND"


class TestAgentRunDetail:
    """`agent run-detail` mirrors GET /agents/{id}/runs/{run_id} (B-2)."""

    @pytest.fixture
    def task_id_with_run(self, tmp_path: Path) -> tuple[str, str]:
        """Create a task, mock-execute it once, return (task_id, run_id)."""
        task_id = _make_runnable_task(tmp_path)

        async def _fake(t, _registry, store, **_kwargs) -> AgentRun:
            run = AgentRun(task_id=t.id, started_at="2026-01-01T00:00:00+00:00", status="ok")
            store.append_run(run)
            return run

        with patch("keboola_agent_cli.services.agent_service.run_task_once", side_effect=_fake):
            run_result = _invoke(tmp_path, "agent", "run", task_id, json_mode=True)
        assert run_result.exit_code == 0
        run_id = json.loads(run_result.output)["data"]["run_id"]
        return task_id, run_id

    def test_run_detail_returns_persisted_run(
        self, tmp_path: Path, task_id_with_run: tuple[str, str]
    ) -> None:
        task_id, run_id = task_id_with_run
        result = _invoke(tmp_path, "agent", "run-detail", task_id, run_id, json_mode=True)
        assert result.exit_code == 0
        body = json.loads(result.output)["data"]
        assert body["run_id"] == run_id
        assert body["task_id"] == task_id
        assert body["status"] == "ok"

    def test_run_detail_unknown_run_returns_not_found(self, tmp_path: Path) -> None:
        task_id = _make_runnable_task(tmp_path)
        result = _invoke(tmp_path, "agent", "run-detail", task_id, "bogus-run-id", json_mode=True)
        assert result.exit_code == 1
        assert json.loads(result.output)["error"]["code"] == "NOT_FOUND"


class TestAgentRunEvents:
    """`agent run-events` mirrors GET .../events (B-2)."""

    def test_run_events_for_cli_command_run_returns_not_found(self, tmp_path: Path) -> None:
        """cli_command runs don't carry an event timeline -- only ai_agent runs do."""
        task_id = _make_runnable_task(tmp_path)

        async def _fake(t, _registry, store, **_kwargs) -> AgentRun:
            run = AgentRun(task_id=t.id, started_at="2026-01-01T00:00:00+00:00", status="ok")
            store.append_run(run)
            return run

        with patch("keboola_agent_cli.services.agent_service.run_task_once", side_effect=_fake):
            run = _invoke(tmp_path, "agent", "run", task_id, json_mode=True)
        run_id = json.loads(run.output)["data"]["run_id"]

        result = _invoke(tmp_path, "agent", "run-events", task_id, run_id, json_mode=True)
        # cli_command never persists events -> service raises NOT_FOUND.
        assert result.exit_code == 1
        body = json.loads(result.output)
        assert body["error"]["code"] == "NOT_FOUND"
        assert "timeline" in body["error"]["message"].lower()


class TestAgentTest:
    """`agent test` mirrors POST /agents/test (ad-hoc, no persistence)."""

    def test_test_cli_command_succeeds_without_persistence(self, tmp_path: Path) -> None:
        """`agent test` with cli_command action runs the runner and returns
        the AgentRun envelope; nothing is appended to agents.json."""

        async def _fake(t, _registry, _store, **_kwargs) -> AgentRun:
            return AgentRun(
                task_id=t.id,
                started_at="2026-01-01T00:00:00+00:00",
                status="ok",
                output={"argv": ["kbagent", "version"], "exit_code": 0},
            )

        with patch("keboola_agent_cli.services.agent_service.run_task_once", side_effect=_fake):
            result = _invoke(
                tmp_path,
                "agent",
                "test",
                "--type",
                "cli_command",
                "--argv",
                "version",
                json_mode=True,
            )
        assert result.exit_code == 0, result.output
        run = json.loads(result.output)["data"]
        assert run["status"] == "ok"
        # The transient test run is NOT persisted into agents.json.
        listed = _invoke(tmp_path, "agent", "list", json_mode=True)
        assert json.loads(listed.output)["data"]["tasks"] == []

    def test_test_without_required_action_flags_exits_2(self, tmp_path: Path) -> None:
        """Missing --type triggers MISSING_PARAMETER (exit code 2)."""
        result = _invoke(tmp_path, "agent", "test", json_mode=True)
        assert result.exit_code == 2
        assert json.loads(result.output)["error"]["code"] == "MISSING_PARAMETER"


class TestAgentPromptImprove:
    """`agent prompt-improve` mirrors POST /agents/prompt/improve/stream.

    The CLI command always streams (default `--stream` is on); we mock the
    underlying ``stream_ai_agent_events`` generator so the test does not
    actually spawn claude / codex / gemini.
    """

    def test_prompt_improve_no_stream_returns_cleaned_prompt(self, tmp_path: Path) -> None:
        async def _fake_stream(_registry, _params):
            yield {"event": "init", "data": {"cli": "claude"}}
            yield {
                "event": "done",
                "data": {
                    "status": "ok",
                    "response": "Polished prompt body here.",
                    "exit_code": 0,
                    "elapsed_seconds": 1.0,
                },
            }

        with patch(
            "keboola_agent_cli.services.agent_service.stream_ai_agent_events",
            side_effect=_fake_stream,
        ):
            result = _invoke(
                tmp_path,
                "agent",
                "prompt-improve",
                "--goal",
                "Summarise yesterday's failed jobs",
                "--no-stream",
                json_mode=True,
            )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)["data"]
        assert body["status"] == "ok"
        # `data.prompt` is the cleaned response body (the whole point of the helper).
        assert body["prompt"] == "Polished prompt body here."

    def test_prompt_improve_empty_goal_exits_2(self, tmp_path: Path) -> None:
        """Empty --goal is caught by the service and surfaces as VALIDATION_ERROR."""
        result = _invoke(tmp_path, "agent", "prompt-improve", "--goal", "   ", json_mode=True)
        assert result.exit_code == 2
        body = json.loads(result.output)
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert "must not be empty" in body["error"]["message"]


class TestAgentIdAlias:
    """Task/run IDs accept both the positional form and --id/--task-id/--run-id.

    Positional stays for terse interactive use; the flag aliases bring agent
    commands in line with the rest of the CLI (--job-id, --config-id, ...).
    """

    def _create(self, tmp_path: Path) -> str:
        result = _invoke(
            tmp_path,
            "agent",
            "create",
            "--name",
            "alias-target",
            "--manual",
            "--type",
            "cli_command",
            "--argv",
            "version",
            json_mode=True,
        )
        assert result.exit_code == 0, result.output
        return json.loads(result.output)["data"]["id"]

    def test_show_positional_still_works(self, tmp_path: Path) -> None:
        task_id = self._create(tmp_path)
        result = _invoke(tmp_path, "agent", "show", task_id, json_mode=True)
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["id"] == task_id

    def test_show_accepts_id_flag(self, tmp_path: Path) -> None:
        task_id = self._create(tmp_path)
        result = _invoke(tmp_path, "agent", "show", "--id", task_id, json_mode=True)
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["id"] == task_id

    def test_show_accepts_task_id_flag(self, tmp_path: Path) -> None:
        task_id = self._create(tmp_path)
        result = _invoke(tmp_path, "agent", "show", "--task-id", task_id, json_mode=True)
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["id"] == task_id

    def test_delete_accepts_id_flag(self, tmp_path: Path) -> None:
        task_id = self._create(tmp_path)
        result = _invoke(tmp_path, "agent", "delete", "--id", task_id, "--yes", json_mode=True)
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["status"] == "ok"

    def test_conflicting_positional_and_flag_exits_2(self, tmp_path: Path) -> None:
        task_id = self._create(tmp_path)
        result = _invoke(tmp_path, "agent", "show", task_id, "--id", "ffffffffffff", json_mode=True)
        assert result.exit_code == 2
        assert json.loads(result.output)["error"]["code"] == "INVALID_ARGUMENT"

    def test_missing_id_exits_2(self, tmp_path: Path) -> None:
        result = _invoke(tmp_path, "agent", "show", json_mode=True)
        assert result.exit_code == 2
        assert json.loads(result.output)["error"]["code"] == "MISSING_PARAMETER"

    def test_run_detail_accepts_run_id_flag(self, tmp_path: Path) -> None:
        """run-detail wires both --id and --run-id; a missing run resolves then 404s."""
        task_id = self._create(tmp_path)
        result = _invoke(
            tmp_path,
            "agent",
            "run-detail",
            "--id",
            task_id,
            "--run-id",
            "ffffffffffff",
            json_mode=True,
        )
        assert result.exit_code == 1
        assert json.loads(result.output)["error"]["code"] == "NOT_FOUND"

    def test_run_accepts_id_flag(self, tmp_path: Path) -> None:
        """run wires --id; a missing task resolves then 404s (avoids a real subprocess)."""
        result = _invoke(tmp_path, "agent", "run", "--id", "ffffffffffff", json_mode=True)
        assert result.exit_code == 1
        assert json.loads(result.output)["error"]["code"] == "NOT_FOUND"

    def test_runs_accepts_id_flag(self, tmp_path: Path) -> None:
        task_id = self._create(tmp_path)
        result = _invoke(tmp_path, "agent", "runs", "--id", task_id, json_mode=True)
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["runs"] == []
