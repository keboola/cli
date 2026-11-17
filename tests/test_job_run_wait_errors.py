"""A failure while polling a job that was already created (#725 follow-up).

``job run --wait`` and ``flow run --wait`` create the job first and then poll
it. A poll that fails (connection error, HTTP 5xx, read timeout, retries
exhausted, an unreadable response) used to bubble up unchanged: ``retryable``
could be true and no job id was reported, so an agent that retried started a
SECOND run. Every failure after the create now keeps its error code (so the
exit code is the same as before) but says ``retryable: false``, carries
``details.job_id`` and names ``kbagent job detail``.

Real commands, real JobService / FlowService, one mocked Keboola client.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner, Result

from helpers import setup_single_project
from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import ENV_AUTO_UPDATE
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.services.flow_service import FlowService
from keboola_agent_cli.services.job_service import JobService
from keboola_agent_cli.services.project_service import ProjectService

runner = CliRunner()
JOB_ID = "900"
JOB_DETAIL = f"kbagent job detail --project prod --job-id {JOB_ID}"

FLOW_CONFIGURATION: dict[str, Any] = {
    "phases": [
        {"id": "p1", "name": "P1", "next": [{"id": "a", "goto": "p2"}]},
        {"id": "p2", "name": "P2", "next": [{"id": "b", "goto": None}]},
    ],
    "tasks": [
        {"id": "t1", "name": "t1", "phase": "p1", "task": {"type": "job"}},
        {"id": "t2", "name": "t2", "phase": "p2", "task": {"type": "job"}},
    ],
}


def _connection_error() -> KeboolaApiError:
    return KeboolaApiError(
        message="Cannot connect to https://queue.keboola.com",
        status_code=0,
        error_code=ErrorCode.CONNECTION_ERROR,
        retryable=True,
    )


def _server_error() -> KeboolaApiError:
    return KeboolaApiError(
        message="API error 503 from https://queue.keboola.com: Service Unavailable",
        status_code=503,
        error_code=ErrorCode.API_ERROR,
        retryable=True,
    )


def _retry_exhausted() -> KeboolaApiError:
    return KeboolaApiError(
        message="Request failed after 3 retries",
        status_code=0,
        error_code=ErrorCode.RETRY_EXHAUSTED,
        retryable=True,
    )


# (poll failure, expected error code, expected exit code). The exit codes are
# the ones these codes mapped to before the fix (ERROR_CODE_TABLE): the fix
# changes `retryable`, the message and `details`, never the code.
POLL_FAILURES = [
    pytest.param(_connection_error(), ErrorCode.CONNECTION_ERROR, 4, id="connection-error"),
    pytest.param(_server_error(), ErrorCode.API_ERROR, 1, id="http-503"),
    pytest.param(_retry_exhausted(), ErrorCode.RETRY_EXHAUSTED, 4, id="retry-exhausted"),
    # Transport errors the HTTP layer does not translate reach the service raw.
    pytest.param(
        httpx.ReadError("connection reset by peer"), ErrorCode.CONNECTION_ERROR, 4, id="raw-httpx"
    ),
    pytest.param(
        json.JSONDecodeError("Expecting value", "<html>", 0),
        ErrorCode.API_ERROR,
        1,
        id="non-json-poll",
    ),
]


@pytest.fixture(autouse=True)
def _no_auto_update(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_AUTO_UPDATE, "false")


def _client(poll_failure: Exception) -> MagicMock:
    client = MagicMock()
    client.get_config_detail.return_value = {"id": "01flow", "configuration": FLOW_CONFIGURATION}
    client.create_job.return_value = {"id": JOB_ID, "status": "created", "isFinished": False}
    client.wait_for_queue_job.side_effect = poll_failure
    client.fetch_job_events.return_value = []
    return client


def _store(tmp_path: Path) -> ConfigStore:
    return setup_single_project(tmp_path / "cfg")


def _job_run(store: ConfigStore, client: MagicMock) -> Result:
    args = [
        "--json",
        "job",
        "run",
        "--project",
        "prod",
        "--component-id",
        "keboola.ex-db",
        "--config-id",
        "42",
        "--no-variables",
        "--wait",
    ]
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as mock_store,
        patch("keboola_agent_cli.cli.ProjectService") as mock_projects,
        patch("keboola_agent_cli.cli.JobService") as mock_jobs,
    ):
        mock_store.return_value = store
        mock_projects.return_value = ProjectService(config_store=store)
        mock_jobs.return_value = JobService(
            config_store=store, client_factory=lambda url, token: client
        )
        return runner.invoke(app, args)


def _flow_run(store: ConfigStore, client: MagicMock) -> Result:
    args = ["--json", "flow", "run", "--project", "prod", "--flow-id", "01flow"]
    args += ["--task-id", "t2", "--no-variables", "--wait"]
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as mock_store,
        patch("keboola_agent_cli.cli.FlowService") as mock_flow,
    ):
        mock_store.return_value = store
        mock_flow.return_value = FlowService(
            config_store=store, client_factory=lambda url, token: client
        )
        return runner.invoke(app, args)


def _assert_created_job_error(error: dict[str, Any], code: str) -> None:
    assert error["code"] == code
    assert error["retryable"] is False
    assert error["details"]["job_id"] == JOB_ID
    assert JOB_DETAIL in error["message"]
    assert "was created" in error["message"]
    assert "do not start it again" in error["message"].lower()


class TestJobRunWait:
    @pytest.mark.parametrize(("failure", "code", "exit_code"), POLL_FAILURES)
    def test_poll_failure_is_not_retryable_and_names_the_job(
        self, tmp_path: Path, failure: Exception, code: str, exit_code: int
    ) -> None:
        client = _client(failure)
        result = _job_run(_store(tmp_path), client)
        assert result.exit_code == exit_code, result.output
        _assert_created_job_error(json.loads(result.stdout)["error"], code)
        client.create_job.assert_called_once()
        client.kill_job.assert_not_called()

    def test_timeout_whose_kill_failed_is_not_retryable(self, tmp_path: Path) -> None:
        """The default `job run` timeout kills the job; when the kill fails it may still run."""
        client = _client(
            KeboolaApiError(
                message=f"Queue job {JOB_ID} did not complete within 600s",
                status_code=504,
                error_code=ErrorCode.QUEUE_JOB_TIMEOUT,
                retryable=True,
            )
        )
        client.kill_job.side_effect = _connection_error()
        client.get_job_detail.side_effect = _connection_error()
        with patch("keboola_agent_cli.services.job_service.JOB_TERMINATE_GRACE_SECONDS", 0):
            result = _job_run(_store(tmp_path), client)
        assert result.exit_code == 4, result.output
        _assert_created_job_error(json.loads(result.stdout)["error"], ErrorCode.QUEUE_JOB_TIMEOUT)
        client.create_job.assert_called_once()


def _queue_timeout() -> KeboolaApiError:
    return KeboolaApiError(
        message=f"Queue job {JOB_ID} did not complete within 5s",
        status_code=504,
        error_code=ErrorCode.QUEUE_JOB_TIMEOUT,
        retryable=True,
    )


def _queue_job_failed() -> KeboolaApiError:
    return KeboolaApiError(
        message=f"Queue job {JOB_ID} failed: boom",
        status_code=500,
        error_code=ErrorCode.QUEUE_JOB_FAILED,
        retryable=False,
    )


FAILED_JOB = {"id": JOB_ID, "status": "error", "isFinished": True, "result": {"tasks": []}}


class TestEnrichmentNeverLosesThePollError:
    """The log tail, the job read and the kill are best effort (#725 follow-up review A/B).

    When one of them breaks on an odd answer, the polling error survives with
    its code and exit code, ``retryable: false`` and ``details.job_id``.
    """

    def test_job_run_timeout_kill_ok_but_job_detail_is_a_list(self, tmp_path: Path) -> None:
        client = _client(_queue_timeout())
        client.kill_job.return_value = {"id": JOB_ID}
        client.get_job_detail.return_value = []
        with patch("keboola_agent_cli.services.job_service.JOB_TERMINATE_GRACE_SECONDS", 0):
            result = _job_run(_store(tmp_path), client)
        # The kill outcome is unknown: reported like a failed kill, exit 4.
        assert result.exit_code == 4, result.output
        _assert_created_job_error(json.loads(result.stdout)["error"], ErrorCode.QUEUE_JOB_TIMEOUT)

    def test_job_run_failed_job_with_null_event(self, tmp_path: Path) -> None:
        client = _client(_queue_job_failed())
        client.get_job_detail.return_value = FAILED_JOB
        client.fetch_job_events.return_value = [None]
        result = _job_run(_store(tmp_path), client)
        assert result.exit_code == 1, result.output
        error = json.loads(result.stdout)["error"]
        assert error["code"] == ErrorCode.QUEUE_JOB_FAILED
        assert error["retryable"] is False
        assert error["details"]["job_id"] == JOB_ID

    def test_flow_run_failed_job_with_null_event(self, tmp_path: Path) -> None:
        client = _client(_queue_job_failed())
        client.get_job_detail.return_value = FAILED_JOB
        client.fetch_job_events.return_value = [None]
        result = _flow_run(_store(tmp_path), client)
        assert result.exit_code == 1, result.output
        envelope = json.loads(result.stdout)
        assert envelope["error"]["code"] == ErrorCode.QUEUE_JOB_FAILED
        assert envelope["error"]["retryable"] is False
        assert envelope["error"]["details"]["job_id"] == JOB_ID
        assert "PARTIAL_RUN_IGNORES_CONDITIONS" in [w["code"] for w in envelope["warnings"]]

    def test_flow_run_timeout_with_list_job_detail_never_kills(self, tmp_path: Path) -> None:
        client = _client(_queue_timeout())
        client.get_job_detail.return_value = []
        result = _flow_run(_store(tmp_path), client)
        assert result.exit_code == 4, result.output
        error = json.loads(result.stdout)["error"]
        assert error["code"] == ErrorCode.QUEUE_JOB_TIMEOUT
        assert error["retryable"] is False
        assert error["details"]["job_id"] == JOB_ID
        client.kill_job.assert_not_called()

    def test_job_run_terminated_timeout_carries_job_id(self, tmp_path: Path) -> None:
        client = _client(_queue_timeout())
        client.kill_job.return_value = {"id": JOB_ID}
        client.get_job_detail.return_value = {
            "id": JOB_ID,
            "status": "terminated",
            "isFinished": True,
        }
        result = _job_run(_store(tmp_path), client)
        assert result.exit_code == 7, result.output
        error = json.loads(result.stdout)["error"]
        assert error["code"] == ErrorCode.JOB_TIMEOUT_TERMINATED
        assert error["retryable"] is False
        assert error["details"]["job_id"] == JOB_ID
        assert error["details"]["job"]["status"] == "terminated"
        assert "logTail" in error["details"]


class TestFlowRunWait:
    @pytest.mark.parametrize(("failure", "code", "exit_code"), POLL_FAILURES)
    def test_poll_failure_is_not_retryable_and_keeps_warnings(
        self, tmp_path: Path, failure: Exception, code: str, exit_code: int
    ) -> None:
        client = _client(failure)
        result = _flow_run(_store(tmp_path), client)
        assert result.exit_code == exit_code, result.output
        envelope = json.loads(result.stdout)
        _assert_created_job_error(envelope["error"], code)
        assert "PARTIAL_RUN_IGNORES_CONDITIONS" in [w["code"] for w in envelope["warnings"]]
        client.create_job.assert_called_once()
        client.kill_job.assert_not_called()


@pytest.mark.skipif(importlib.util.find_spec("fastapi") is None, reason="FastAPI not installed")
class TestFlowRunRoute:
    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param(_connection_error(), id="connection-error"),
            pytest.param(_server_error(), id="http-503"),
        ],
    )
    def test_poll_failure_body_is_not_retryable(self, tmp_path: Path, failure: Exception) -> None:
        from fastapi.testclient import TestClient

        from keboola_agent_cli.models import AppConfig, ProjectConfig
        from keboola_agent_cli.server import create_app

        token = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
        project = ProjectConfig(
            stack_url="https://connection.keboola.com", token=token, project_id=1
        )
        ConfigStore(config_dir=tmp_path).save(
            AppConfig(projects={"prod": project}, default_project="prod")
        )
        server = create_app(config_dir=str(tmp_path), auth_token="test-token")
        client = _client(failure)
        for service in vars(server.state.registry).values():
            for name in list(getattr(service, "__dict__", {})):
                if name.endswith("client_factory"):
                    setattr(service, name, lambda *_args, **_kwargs: client)
        with TestClient(server, raise_server_exceptions=False) as http:
            res = http.post(
                "/flows/prod/01flow/run?wait=true",
                json={"task_ids": ["t2"], "no_variables": True},
                headers={"Authorization": "Bearer test-token"},
            )
        assert res.status_code >= 500, res.text
        body = res.json()
        assert body["error"]["retryable"] is False
        assert body["error"]["details"]["job_id"] == JOB_ID
        assert JOB_DETAIL in body["error"]["message"]
        assert "PARTIAL_RUN_IGNORES_CONDITIONS" in [w["code"] for w in body["warnings"]]
        client.create_job.assert_called_once()
