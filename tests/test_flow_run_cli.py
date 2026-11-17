"""CLI tests for `kbagent flow run` (#725): real command, real FlowService/JobService, mocked client.

Covers design risks R1 (blank --task-id), R3 (one flow read), R4 (timeout
never kills the job), R5 (non-success terminal statuses), R9 (variable
flags), R10 (warnings in the envelope), R11 (deny policy blocks --dry-run),
R12 (branch 0 / dev branch reach the read and the POST) and the dry run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner, Result

from helpers import setup_single_project
from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import ENV_AUTO_UPDATE
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import PermissionPolicy
from keboola_agent_cli.permissions import PermissionEngine
from keboola_agent_cli.services.flow_service import FlowService

runner = CliRunner()
FLOW_ID = "01flow"
ACTIVE_BRANCH = 7

COND = {"type": "operator", "operator": "ALL_TASKS_IN_PHASE", "phase": "p2", "operands": []}
FLOW_CONFIGURATION: dict[str, Any] = {
    "phases": [
        {"id": "p1", "name": "Phase 1", "next": [{"id": "a", "goto": "p2"}]},
        {
            "id": "p2",
            "name": "Phase 2",
            "next": [
                {"id": "fail", "goto": "pfail", "condition": COND},
                {"id": "b", "goto": "p3"},
            ],
        },
        {"id": "p3", "name": "Phase 3", "next": [{"id": "end", "goto": None}]},
        {"id": "pfail", "name": "On failure"},
    ],
    "tasks": [
        {"id": tid, "name": tid, "phase": phase, "enabled": True, "task": {"type": "job"}}
        for tid, phase in (("t1", "p1"), ("t2", "p2"), ("t3", "p3"), ("tf", "pfail"))
    ],
}


@pytest.fixture(autouse=True)
def _no_auto_update(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_AUTO_UPDATE, "false")


def _client(
    configuration: dict[str, Any] | None = None,
    finished: dict[str, Any] | None = None,
) -> MagicMock:
    client = MagicMock()
    client.get_config_detail.return_value = {
        "id": FLOW_ID,
        "configuration": configuration or FLOW_CONFIGURATION,
    }
    client.create_job.return_value = {"id": "900", "status": "created", "isFinished": False}
    client.wait_for_queue_job.return_value = finished or {
        "id": "900",
        "status": "success",
        "isFinished": True,
        "result": {"tasks": []},
    }
    client.fetch_job_events.return_value = []
    client.get_job_detail.return_value = {"id": "900", "status": "processing"}
    return client


def _store(tmp_path: Path, active_branch: int | None = None) -> ConfigStore:
    store = setup_single_project(tmp_path / "cfg")
    if active_branch is not None:
        config = store.load()
        config.projects["prod"].active_branch_id = active_branch
        store.save(config)
    return store


def _run(store: ConfigStore, client: MagicMock, args: list[str]) -> Result:
    service = FlowService(config_store=store, client_factory=lambda url, token: client)
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as mock_store,
        patch("keboola_agent_cli.cli.FlowService") as mock_flow,
    ):
        mock_store.return_value = store
        mock_flow.return_value = service
        return runner.invoke(app, args)


def _json(result: Result) -> dict[str, Any]:
    return json.loads(result.stdout)


BASE = ["--json", "flow", "run", "--project", "prod", "--flow-id", FLOW_ID]


class TestBlankSelectors:
    @pytest.mark.parametrize("extra", [["--task-id", " "], ["--task-id", "t1", "--task-id", ""]])
    def test_blank_task_id_exits_2_without_any_call(self, tmp_path: Path, extra: list[str]) -> None:
        client = _client()
        result = _run(_store(tmp_path), client, [*BASE, *extra])
        assert result.exit_code == 2, result.output
        assert _json(result)["error"]["code"] == ErrorCode.INVALID_ARGUMENT
        client.get_config_detail.assert_not_called()
        client.create_job.assert_not_called()

    def test_blank_from_phase_exits_2(self, tmp_path: Path) -> None:
        client = _client()
        result = _run(_store(tmp_path), client, [*BASE, "--from-phase-id", "  "])
        assert result.exit_code == 2
        client.create_job.assert_not_called()

    def test_comma_in_task_id_refused(self, tmp_path: Path) -> None:
        client = _client()
        result = _run(_store(tmp_path), client, [*BASE, "--task-id", "t1,t2"])
        assert result.exit_code == 2
        client.create_job.assert_not_called()

    def test_unknown_task_exits_2_before_post(self, tmp_path: Path) -> None:
        client = _client()
        result = _run(_store(tmp_path), client, [*BASE, "--task-id", "nope"])
        assert result.exit_code == 2
        assert "nope" in _json(result)["error"]["message"]
        client.create_job.assert_not_called()


class TestSelectionAndBody:
    def test_whole_flow_body_has_no_selection(self, tmp_path: Path) -> None:
        client = _client()
        result = _run(_store(tmp_path), client, BASE)
        assert result.exit_code == 0, result.output
        kwargs = client.create_job.call_args.kwargs
        assert kwargs["component_id"] == "keboola.flow"
        assert kwargs["only_flow_task_ids"] is None
        data = _json(result)
        assert data["data"]["partial"] is False
        assert "warnings" not in data

    def test_from_phase_sends_default_path_tasks_and_warns(self, tmp_path: Path) -> None:
        client = _client()
        result = _run(_store(tmp_path), client, [*BASE, "--from-phase-id", "p2"])
        assert result.exit_code == 0, result.output
        assert client.create_job.call_args.kwargs["only_flow_task_ids"] == ["t2", "t3"]
        data = _json(result)
        assert data["data"]["id"] == "900"
        assert data["data"]["selection"]["task_ids"] == ["t2", "t3"]
        codes = [w["code"] for w in data["warnings"]]
        assert codes == ["PARTIAL_RUN_IGNORES_CONDITIONS", "CONDITIONAL_PHASES_SKIPPED"]
        assert data["warnings"][1]["phase_ids"] == ["pfail"]

    def test_union_of_task_ids_and_phase(self, tmp_path: Path) -> None:
        client = _client()
        result = _run(
            _store(tmp_path),
            client,
            [*BASE, "--task-id", "t3", "--task-id", "t1", "--from-phase-id", "p3"],
        )
        assert result.exit_code == 0, result.output
        assert client.create_job.call_args.kwargs["only_flow_task_ids"] == ["t1", "t3"]

    def test_dry_run_reads_once_and_posts_nothing(self, tmp_path: Path) -> None:
        client = _client()
        result = _run(_store(tmp_path), client, [*BASE, "--from-phase-id", "p2", "--dry-run"])
        assert result.exit_code == 0, result.output
        data = _json(result)
        assert data["data"]["dry_run"] is True
        assert data["data"]["partial"] is True
        assert data["data"]["selection"]["phases"] == [
            {"id": "p2", "name": "Phase 2", "task_ids": ["t2"]},
            {"id": "p3", "name": "Phase 3", "task_ids": ["t3"]},
        ]
        assert [t["id"] for t in data["data"]["tasks"]] == ["t2", "t3"]
        assert {w["code"] for w in data["warnings"]} >= {"CONDITIONAL_PHASES_SKIPPED"}
        client.get_config_detail.assert_called_once()
        client.create_job.assert_not_called()
        client.list_config_rows.assert_not_called()

    def test_dry_run_human_warnings_on_stderr(self, tmp_path: Path) -> None:
        client = _client()
        args = [
            "flow",
            "run",
            "--project",
            "prod",
            "--flow-id",
            FLOW_ID,
            "--task-id",
            "t3",
            "--dry-run",
        ]
        result = _run(_store(tmp_path), client, args)
        assert result.exit_code == 0, result.output
        assert "1 task(s) would run" in result.stdout
        assert "transition conditions" in " ".join(result.stderr.split())


class TestBranch:
    def test_branch_zero_beats_active_branch_on_read_and_post(self, tmp_path: Path) -> None:
        client = _client()
        result = _run(
            _store(tmp_path, ACTIVE_BRANCH), client, [*BASE, "--task-id", "t1", "--branch", "0"]
        )
        assert result.exit_code == 0, result.output
        assert client.get_config_detail.call_args.kwargs["branch_id"] == 0
        assert client.create_job.call_args.kwargs["branch_id"] == 0

    def test_active_branch_reaches_read_and_post(self, tmp_path: Path) -> None:
        client = _client()
        result = _run(_store(tmp_path, ACTIVE_BRANCH), client, [*BASE, "--task-id", "t1"])
        assert result.exit_code == 0, result.output
        assert client.get_config_detail.call_args.kwargs["branch_id"] == ACTIVE_BRANCH
        assert client.create_job.call_args.kwargs["branch_id"] == ACTIVE_BRANCH

    def test_production_stays_production_when_a_branch_is_activated_mid_run(
        self, tmp_path: Path
    ) -> None:
        # Another terminal runs `branch use` after the flow was read for production:
        # the job must still go to production, the branch the selection describes.
        store = _store(tmp_path)
        client = _client()
        read = client.get_config_detail.side_effect
        detail = client.get_config_detail.return_value

        def _read_then_activate(*args: Any, **kwargs: Any) -> Any:
            config = store.load()
            config.projects["prod"].active_branch_id = ACTIVE_BRANCH
            store.save(config)
            return read(*args, **kwargs) if read else detail

        client.get_config_detail.side_effect = _read_then_activate
        result = _run(store, client, [*BASE, "--task-id", "t1"])
        assert result.exit_code == 0, result.output
        assert not client.get_config_detail.call_args.kwargs["branch_id"]
        assert not client.create_job.call_args.kwargs["branch_id"]

    def test_dev_branch_reaches_read_rows_and_post(self, tmp_path: Path) -> None:
        configuration = {**FLOW_CONFIGURATION, "variables_id": "55"}
        client = _client(configuration)
        client.list_config_rows.return_value = [{"id": "row-1"}]
        result = _run(_store(tmp_path), client, [*BASE, "--task-id", "t1", "--branch", "123"])
        assert result.exit_code == 0, result.output
        assert client.get_config_detail.call_args.kwargs["branch_id"] == 123
        assert client.list_config_rows.call_args.kwargs["branch_id"] == 123
        assert client.create_job.call_args.kwargs["branch_id"] == 123


class TestVariables:
    def test_auto_resolution_uses_the_single_flow_read(self, tmp_path: Path) -> None:
        configuration = {**FLOW_CONFIGURATION, "variables_id": "55"}
        client = _client(configuration)
        client.list_config_rows.return_value = [{"id": "row-1"}]
        result = _run(_store(tmp_path), client, [*BASE, "--from-phase-id", "p2"])
        assert result.exit_code == 0, result.output
        assert client.get_config_detail.call_count == 1
        assert client.create_job.call_args.kwargs["variable_values_id"] == "row-1"

    def test_explicit_values_id_skips_resolution(self, tmp_path: Path) -> None:
        configuration = {**FLOW_CONFIGURATION, "variables_id": "55"}
        client = _client(configuration)
        result = _run(_store(tmp_path), client, [*BASE, "--variable-values-id", "row-9"])
        assert result.exit_code == 0, result.output
        client.list_config_rows.assert_not_called()
        assert client.create_job.call_args.kwargs["variable_values_id"] == "row-9"

    def test_no_variables_skips_resolution(self, tmp_path: Path) -> None:
        configuration = {**FLOW_CONFIGURATION, "variables_id": "55"}
        client = _client(configuration)
        result = _run(_store(tmp_path), client, [*BASE, "--no-variables"])
        assert result.exit_code == 0, result.output
        client.list_config_rows.assert_not_called()
        assert client.create_job.call_args.kwargs["variable_values_id"] is None

    def test_both_flags_exit_2(self, tmp_path: Path) -> None:
        client = _client()
        result = _run(
            _store(tmp_path), client, [*BASE, "--variable-values-id", "row-9", "--no-variables"]
        )
        assert result.exit_code == 2
        client.create_job.assert_not_called()

    def test_blank_values_id_exit_2(self, tmp_path: Path) -> None:
        client = _client()
        result = _run(_store(tmp_path), client, [*BASE, "--variable-values-id", " "])
        assert result.exit_code == 2
        client.create_job.assert_not_called()


class TestWaitAndResults:
    def test_timeout_keeps_job_running(self, tmp_path: Path) -> None:
        client = _client()
        client.wait_for_queue_job.side_effect = KeboolaApiError(
            message="Queue job 900 did not complete within 5s",
            status_code=504,
            error_code=ErrorCode.QUEUE_JOB_TIMEOUT,
            retryable=True,
        )
        result = _run(
            _store(tmp_path), client, [*BASE, "--task-id", "t3", "--wait", "--timeout", "5"]
        )
        assert result.exit_code == 4, result.output
        error = _json(result)["error"]
        assert error["code"] == ErrorCode.QUEUE_JOB_TIMEOUT
        assert error["retryable"] is False
        assert "keeps running" in error["message"]
        assert "kbagent job detail --project prod --job-id 900" in error["message"]
        client.kill_job.assert_not_called()

    @pytest.mark.parametrize("timeout", ["0", "-1", "inf", "nan"])
    def test_bad_timeout_refused_before_post(self, tmp_path: Path, timeout: str) -> None:
        client = _client()
        result = _run(_store(tmp_path), client, [*BASE, "--wait", f"--timeout={timeout}"])
        assert result.exit_code == 2, result.output
        client.create_job.assert_not_called()

    def test_all_selected_tasks_succeeded_exit_0(self, tmp_path: Path) -> None:
        finished = {
            "id": "900",
            "status": "success",
            "isFinished": True,
            "result": {
                "tasks": [
                    {"id": "t2", "phase": "p2", "status": "success", "jobId": "1"},
                    {"id": "t3", "phase": "p3", "status": "success", "jobId": "2"},
                ]
            },
        }
        client = _client(finished=finished)
        result = _run(_store(tmp_path), client, [*BASE, "--from-phase-id", "p2", "--wait"])
        assert result.exit_code == 0, result.output
        data = _json(result)
        assert data["exit_code"] == 0
        assert data["data"]["failed_task_ids"] == []
        assert [r["status"] for r in data["data"]["task_results"]] == ["success", "success"]

    def test_success_parent_with_failed_task_exits_1(self, tmp_path: Path) -> None:
        finished = {
            "id": "900",
            "status": "success",
            "isFinished": True,
            "result": {
                "tasks": [
                    {"id": "t2", "phase": "p2", "status": "error", "jobId": "1"},
                    {"id": "t3", "phase": "p3", "status": "success", "jobId": "2"},
                ]
            },
        }
        client = _client(finished=finished)
        result = _run(_store(tmp_path), client, [*BASE, "--from-phase-id", "p2", "--wait"])
        assert result.exit_code == 1, result.output
        data = _json(result)
        assert data["exit_code"] == 1
        assert data["data"]["failed_task_ids"] == ["t2"]
        assert "SELECTED_TASK_FAILED" in [w["code"] for w in data["warnings"]]

    def test_terminated_parent_with_missing_task_exits_1(self, tmp_path: Path) -> None:
        finished = {
            "id": "900",
            "status": "terminated",
            "isFinished": True,
            "result": {"tasks": [{"id": "t2", "phase": "p2", "status": "success", "jobId": "1"}]},
        }
        client = _client(finished=finished)
        result = _run(_store(tmp_path), client, [*BASE, "--from-phase-id", "p2", "--wait"])
        assert result.exit_code == 1, result.output
        data = _json(result)["data"]
        assert data["failed_task_ids"] == ["t3"]
        assert data["task_results"][1]["status"] == "missing"

    def test_human_failure_headline_and_error_lines(self, tmp_path: Path) -> None:
        finished = {
            "id": "900",
            "status": "cancelled",
            "isFinished": True,
            "result": {"tasks": []},
        }
        client = _client(finished=finished)
        args = [
            "flow",
            "run",
            "--project",
            "prod",
            "--flow-id",
            FLOW_ID,
            "--task-id",
            "t3",
            "--wait",
        ]
        result = _run(_store(tmp_path), client, args)
        assert result.exit_code == 1, result.output
        assert "Error: task t3: missing" in result.stdout
        assert "Failed: 1 of 1 selected task(s)." in result.stdout

    def test_whole_flow_wait_does_not_check_tasks(self, tmp_path: Path) -> None:
        client = _client()
        result = _run(_store(tmp_path), client, [*BASE, "--wait"])
        assert result.exit_code == 0, result.output
        assert "task_results" not in _json(result)["data"]


class TestPermissions:
    def test_deny_writes_blocks_dry_run(self, tmp_path: Path) -> None:
        client = _client()
        result = _run(
            _store(tmp_path),
            client,
            [
                "--deny-writes",
                "--json",
                "flow",
                "run",
                "--project",
                "prod",
                "--flow-id",
                FLOW_ID,
                "--dry-run",
            ],
        )
        assert result.exit_code == 6, result.output
        client.get_config_detail.assert_not_called()

    def test_flow_run_is_write_and_also_needs_job_run(self) -> None:
        """`flow run` creates a Queue job, so a policy denying `job.run` stops it too."""
        engine = PermissionEngine(PermissionPolicy(mode="allow", deny=["job.run"]))
        assert engine.is_allowed("flow.run") is False
        engine = PermissionEngine(PermissionPolicy(mode="allow", deny=["cli:write"]))
        assert engine.is_allowed("flow.run") is False
        engine = PermissionEngine(PermissionPolicy(mode="allow", deny=["flow.run"]))
        assert engine.is_allowed("flow.run") is False
        assert engine.is_allowed("job.run") is True
        engine = PermissionEngine(PermissionPolicy(mode="deny", allow=["flow.run"]))
        assert engine.is_allowed("flow.run") is False
        engine = PermissionEngine(PermissionPolicy(mode="deny", allow=["flow.run", "job.run"]))
        assert engine.is_allowed("flow.run") is True

    @pytest.mark.parametrize("denied", ["job.run", "flow.run", "job.*"])
    @pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "run"])
    def test_persisted_deny_blocks_before_any_call(
        self, tmp_path: Path, denied: str, dry_run: bool
    ) -> None:
        store = _store(tmp_path)
        config = store.load()
        config.permissions = PermissionPolicy(mode="allow", deny=[denied])
        store.save(config)
        client = _client()
        args = [*BASE, "--task-id", "t2"] + (["--dry-run"] if dry_run else [])
        result = _run(store, client, args)
        assert result.exit_code == 6, result.output
        error = _json(result)["error"]
        assert error["code"] == ErrorCode.PERMISSION_DENIED
        if denied != "flow.run":
            assert "job.run" in error["message"]
        client.get_config_detail.assert_not_called()
        client.create_job.assert_not_called()

    def test_persisted_allow_policy_still_runs(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        config = store.load()
        config.permissions = PermissionPolicy(mode="allow", deny=["job.terminate"])
        store.save(config)
        client = _client()
        result = _run(store, client, [*BASE, "--task-id", "t2"])
        assert result.exit_code == 0, result.output
        client.create_job.assert_called_once()


def _job_failed() -> KeboolaApiError:
    return KeboolaApiError(
        message="Queue job 900 failed: phase p3 failed",
        status_code=500,
        error_code=ErrorCode.QUEUE_JOB_FAILED,
    )


FAILED_JOB = {
    "id": "900",
    "status": "error",
    "isFinished": True,
    "result": {
        "tasks": [
            {"id": "t2", "phase": "p2", "status": "success", "jobId": "1"},
            {"id": "t3", "phase": "p3", "status": "error", "jobId": "2"},
        ]
    },
}


class TestFixRound1:
    """Regression tests for review fix round 1 (F1, F2, F3, F5)."""

    def test_parent_error_keeps_code_and_carries_selection_warnings(self, tmp_path: Path) -> None:
        client = _client()
        client.wait_for_queue_job.side_effect = _job_failed()
        client.get_job_detail.return_value = FAILED_JOB
        result = _run(_store(tmp_path), client, [*BASE, "--from-phase-id", "p2", "--wait"])
        assert result.exit_code == 1, result.output
        envelope = _json(result)
        assert envelope["error"]["code"] == ErrorCode.QUEUE_JOB_FAILED
        codes = [w["code"] for w in envelope["warnings"]]
        assert "PARTIAL_RUN_IGNORES_CONDITIONS" in codes
        assert "CONDITIONAL_PHASES_SKIPPED" in codes

    def test_parent_error_details_carry_task_results(self, tmp_path: Path) -> None:
        client = _client()
        client.wait_for_queue_job.side_effect = _job_failed()
        client.get_job_detail.return_value = FAILED_JOB
        result = _run(_store(tmp_path), client, [*BASE, "--from-phase-id", "p2", "--wait"])
        assert result.exit_code == 1, result.output
        envelope = _json(result)
        details = envelope["error"]["details"]
        assert details["job_id"] == "900"
        assert details["failed_task_ids"] == ["t3"]
        assert [r["status"] for r in details["task_results"]] == ["success", "error"]
        assert "SELECTED_TASK_FAILED" in [w["code"] for w in envelope["warnings"]]

    def test_parent_error_read_failure_keeps_the_error(self, tmp_path: Path) -> None:
        client = _client()
        client.wait_for_queue_job.side_effect = _job_failed()
        # Both reads fail: the one in `run_job` and the retry in the flow layer.
        boom = KeboolaApiError(message="boom", status_code=500, error_code=ErrorCode.API_ERROR)
        client.get_job_detail.side_effect = [boom, boom]
        result = _run(_store(tmp_path), client, [*BASE, "--from-phase-id", "p2", "--wait"])
        assert result.exit_code == 1, result.output
        envelope = _json(result)
        assert envelope["error"]["code"] == ErrorCode.QUEUE_JOB_FAILED
        assert "failed_task_ids" not in envelope["error"]["details"]
        assert "PARTIAL_RUN_IGNORES_CONDITIONS" in [w["code"] for w in envelope["warnings"]]

    def test_enrichment_reuses_the_job_run_job_already_read(self, tmp_path: Path) -> None:
        client = _client()
        client.wait_for_queue_job.side_effect = _job_failed()
        client.get_job_detail.return_value = FAILED_JOB
        result = _run(_store(tmp_path), client, [*BASE, "--from-phase-id", "p2", "--wait"])
        assert result.exit_code == 1, result.output
        assert _json(result)["error"]["details"]["failed_task_ids"] == ["t3"]
        # One read, exactly as `job run` makes on QUEUE_JOB_FAILED: no second read.
        assert client.get_job_detail.call_count == 1

    @pytest.mark.parametrize(
        "bad_read",
        [None, json.JSONDecodeError("Expecting value", "<html>", 0), ["not", "an", "object"]],
        ids=["json-null", "non-json", "wrong-shape"],
    )
    def test_bad_enrichment_read_keeps_queue_job_failed(
        self, tmp_path: Path, bad_read: Any
    ) -> None:
        client = _client()
        client.wait_for_queue_job.side_effect = _job_failed()
        client.get_job_detail.side_effect = [bad_read, bad_read]
        result = _run(_store(tmp_path), client, [*BASE, "--from-phase-id", "p2", "--wait"])
        assert result.exit_code == 1, result.output
        envelope = _json(result)
        assert envelope["error"]["code"] == ErrorCode.QUEUE_JOB_FAILED
        assert envelope["error"]["details"]["job_id"] == "900"
        assert "failed_task_ids" not in envelope["error"]["details"]
        assert "PARTIAL_RUN_IGNORES_CONDITIONS" in [w["code"] for w in envelope["warnings"]]

    def test_timeout_carries_selection_warnings(self, tmp_path: Path) -> None:
        client = _client()
        client.wait_for_queue_job.side_effect = KeboolaApiError(
            message="Queue job 900 did not complete within 5s",
            status_code=504,
            error_code=ErrorCode.QUEUE_JOB_TIMEOUT,
            retryable=True,
        )
        result = _run(
            _store(tmp_path), client, [*BASE, "--task-id", "t3", "--wait", "--timeout", "5"]
        )
        assert result.exit_code == 4, result.output
        envelope = _json(result)
        assert envelope["error"]["code"] == ErrorCode.QUEUE_JOB_TIMEOUT
        assert "PARTIAL_RUN_IGNORES_CONDITIONS" in [w["code"] for w in envelope["warnings"]]

    def test_comma_in_from_phase_id_refused(self, tmp_path: Path) -> None:
        client = _client()
        result = _run(_store(tmp_path), client, [*BASE, "--from-phase-id", "p2,p3"])
        assert result.exit_code == 2, result.output
        assert _json(result)["error"]["code"] == ErrorCode.INVALID_ARGUMENT
        client.create_job.assert_not_called()

    def test_whole_flow_human_dry_run_says_tasks_could_run(self, tmp_path: Path) -> None:
        client = _client()
        args = ["flow", "run", "--project", "prod", "--flow-id", FLOW_ID, "--dry-run"]
        result = _run(_store(tmp_path), client, args)
        assert result.exit_code == 0, result.output
        text = " ".join(result.stdout.split())
        assert "could run" in text
        assert "conditions decide" in text
        assert "would run" not in text
