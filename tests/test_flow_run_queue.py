"""Layer tests for `flow run` (#725): the Queue body and the JobService pass-throughs.

- ``create_job`` sends ``onlyFlowTaskIds`` only for a non-empty list, so every
  other job body stays byte-identical (D13).
- ``run_job(kill_on_timeout=False)`` leaves the remote job running on a local
  timeout (spec LR2, D11), while the default keeps the ``job run`` kill.
- ``resolve_variable_values_from_configuration`` reads only the variable rows (D10).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from helpers import setup_single_project
from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.services.job_service import JobService

_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
_BASE = "https://connection.keboola.com"
_JOBS_URL = "https://queue.keboola.com/jobs"


class TestCreateJobBody:
    def test_only_flow_task_ids_sent(self, httpx_mock) -> None:
        httpx_mock.add_response(url=_JOBS_URL, method="POST", json={"id": 1}, status_code=201)
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            client.create_job(
                component_id="keboola.flow", config_id="9", only_flow_task_ids=["t2", "t3"]
            )
        body = json.loads(httpx_mock.get_request().content)
        assert body["onlyFlowTaskIds"] == ["t2", "t3"]

    @pytest.mark.parametrize("value", [None, []])
    def test_absent_or_empty_list_leaves_body_unchanged(self, httpx_mock, value) -> None:
        httpx_mock.add_response(url=_JOBS_URL, method="POST", json={"id": 1}, status_code=201)
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            client.create_job(component_id="keboola.flow", config_id="9", only_flow_task_ids=value)
        body = json.loads(httpx_mock.get_request().content)
        assert body == {"component": "keboola.flow", "config": "9", "mode": "run"}


def _timeout_client() -> MagicMock:
    client = MagicMock()
    client.create_job.return_value = {"id": "77", "status": "created"}
    client.wait_for_queue_job.side_effect = KeboolaApiError(
        message="Queue job 77 did not complete within 1s",
        status_code=504,
        error_code=ErrorCode.QUEUE_JOB_TIMEOUT,
        retryable=True,
    )
    client.kill_job.return_value = {"id": "77"}
    client.get_job_detail.return_value = {"id": "77", "status": "terminated", "isFinished": True}
    client.fetch_job_events.return_value = []
    return client


class TestTimeoutPolicy:
    def test_no_kill_on_timeout(self, tmp_path: Path) -> None:
        client = _timeout_client()
        service = JobService(setup_single_project(tmp_path), lambda url, token: client)
        with pytest.raises(KeboolaApiError) as exc:
            service.run_job(
                alias="prod",
                component_id="keboola.flow",
                config_id="9",
                wait=True,
                timeout=1,
                no_variables=True,
                kill_on_timeout=False,
                check_flow=False,
            )
        assert exc.value.error_code == ErrorCode.QUEUE_JOB_TIMEOUT
        assert exc.value.retryable is False
        assert "kbagent job detail --project prod --job-id 77" in exc.value.message
        assert exc.value.details == {"job_id": "77"}
        client.kill_job.assert_not_called()

    def test_job_run_default_still_kills(self, tmp_path: Path) -> None:
        client = _timeout_client()
        service = JobService(setup_single_project(tmp_path), lambda url, token: client)
        with pytest.raises(KeboolaApiError) as exc:
            service.run_job(
                alias="prod",
                component_id="keboola.ex-http",
                config_id="9",
                wait=True,
                timeout=1,
                no_variables=True,
            )
        assert exc.value.error_code == ErrorCode.JOB_TIMEOUT_TERMINATED
        client.kill_job.assert_called_once_with("77")

    def test_only_flow_task_ids_reach_create_job(self, tmp_path: Path) -> None:
        client = _timeout_client()
        service = JobService(setup_single_project(tmp_path), lambda url, token: client)
        service.run_job(
            alias="prod",
            component_id="keboola.flow",
            config_id="9",
            no_variables=True,
            only_flow_task_ids=["t1"],
            check_flow=False,
        )
        assert client.create_job.call_args.kwargs["only_flow_task_ids"] == ["t1"]


class TestVariablesFromConfiguration:
    def test_no_link_is_none_without_any_call(self) -> None:
        client = MagicMock()
        assert JobService.resolve_variable_values_from_configuration(client, {}) is None
        client.get_config_detail.assert_not_called()
        client.list_config_rows.assert_not_called()

    def test_explicit_values_id_wins(self) -> None:
        client = MagicMock()
        configuration = {"variables_id": "5", "variables_values_id": "row-2"}
        assert (
            JobService.resolve_variable_values_from_configuration(client, configuration) == "row-2"
        )
        client.list_config_rows.assert_not_called()

    def test_first_row_on_branch(self) -> None:
        client = MagicMock()
        client.list_config_rows.return_value = [{"id": "row-1"}, {"id": "row-2"}]
        values_id = JobService.resolve_variable_values_from_configuration(
            client, {"variables_id": "5"}, branch_id=0
        )
        assert values_id == "row-1"
        client.list_config_rows.assert_called_once_with(
            component_id="keboola.variables", config_id="5", branch_id=0
        )
        client.get_config_detail.assert_not_called()
