"""REST tests for `POST /flows/{project}/{flow_id}/run` (#725).

Real app, real FlowService/JobService, one mocked Keboola client. Covers R2
(`task_ids: []` is 400, never a whole-flow run), R10 (warnings in the
`Kbagent-Warnings` header), R13 (unknown body fields and query parameters are
refused), D12 over REST (HTTP 200 with task results) and the route permission.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

if importlib.util.find_spec("fastapi") is None:  # pragma: no cover
    pytest.skip("FastAPI not installed", allow_module_level=True)

from fastapi.testclient import TestClient

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import AppConfig, PermissionPolicy, ProjectConfig
from keboola_agent_cli.server import create_app
from keboola_agent_cli.server.response_meta import WARNINGS_HEADER
from keboola_agent_cli.server.route_permissions import ROUTE_OPERATIONS

AUTH = {"Authorization": "Bearer test-token"}
TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
URL = "/flows/prod/01flow/run"

CONFIGURATION: dict[str, Any] = {
    "phases": [
        {"id": "p1", "name": "P1", "next": [{"id": "a", "goto": "p2"}]},
        {
            "id": "p2",
            "name": "P2",
            "next": [
                {"id": "c", "goto": "pfail", "condition": {"type": "const", "value": True}},
                {"id": "d", "goto": None},
            ],
        },
        {"id": "pfail", "name": "Fail"},
    ],
    "tasks": [
        {"id": "t1", "name": "t1", "phase": "p1", "task": {"type": "job"}},
        {"id": "t2", "name": "t2", "phase": "p2", "task": {"type": "job"}},
        {"id": "tf", "name": "tf", "phase": "pfail", "task": {"type": "job"}},
    ],
}


def _client() -> MagicMock:
    client = MagicMock()
    client.get_config_detail.return_value = {"id": "01flow", "configuration": CONFIGURATION}
    client.create_job.return_value = {"id": "900", "status": "created"}
    client.fetch_job_events.return_value = []
    return client


def _app(tmp_path: Path, client: MagicMock, policy: PermissionPolicy | None = None) -> Any:
    project = ProjectConfig(stack_url="https://connection.keboola.com", token=TOKEN, project_id=1)
    config = AppConfig(projects={"prod": project}, default_project="prod")
    if policy is not None:
        config.permissions = policy
    ConfigStore(config_dir=tmp_path).save(config)
    app = create_app(config_dir=str(tmp_path), auth_token="test-token")
    for service in vars(app.state.registry).values():
        for name in list(getattr(service, "__dict__", {})):
            if name.endswith("client_factory"):
                setattr(service, name, lambda *_args, **_kwargs: client)
    return app


def _post(app: Any, body: dict[str, Any], query: str = "") -> Any:
    with TestClient(app, raise_server_exceptions=False) as http:
        return http.post(URL + query, json=body, headers=AUTH)


def _warning_codes(res: Any) -> list[str]:
    return [w["code"] for w in json.loads(res.headers.get(WARNINGS_HEADER, "[]"))]


def test_route_is_classified_write() -> None:
    assert ROUTE_OPERATIONS[("POST", "/flows/{project}/{flow_id}/run")] == "flow.run"


@pytest.mark.parametrize("task_ids", [[], ["t1", " "]])
def test_empty_or_blank_task_ids_are_400(tmp_path: Path, task_ids: list[str]) -> None:
    client = _client()
    res = _post(_app(tmp_path, client), {"task_ids": task_ids})
    assert res.status_code == 400, res.text
    assert res.json()["error"]["code"] == "INVALID_ARGUMENT"
    client.create_job.assert_not_called()


def test_null_task_ids_is_whole_flow(tmp_path: Path) -> None:
    client = _client()
    res = _post(_app(tmp_path, client), {"task_ids": None})
    assert res.status_code == 200, res.text
    assert client.create_job.call_args.kwargs["only_flow_task_ids"] is None
    assert res.json()["partial"] is False


def test_partial_run_sends_selection_and_warning_header(tmp_path: Path) -> None:
    client = _client()
    res = _post(_app(tmp_path, client), {"from_phase_id": "p2"})
    assert res.status_code == 200, res.text
    assert client.create_job.call_args.kwargs["only_flow_task_ids"] == ["t2"]
    assert res.json()["id"] == "900"
    assert _warning_codes(res) == ["PARTIAL_RUN_IGNORES_CONDITIONS", "CONDITIONAL_PHASES_SKIPPED"]


def test_dry_run_in_body_posts_nothing(tmp_path: Path) -> None:
    client = _client()
    res = _post(_app(tmp_path, client), {"task_ids": ["t2"], "dry_run": True})
    assert res.status_code == 200, res.text
    assert res.json()["dry_run"] is True
    assert res.json()["selection"]["task_ids"] == ["t2"]
    client.create_job.assert_not_called()


def test_unknown_body_field_is_422(tmp_path: Path) -> None:
    client = _client()
    res = _post(_app(tmp_path, client), {"task_id": ["t2"]})
    assert res.status_code == 422
    client.create_job.assert_not_called()


@pytest.mark.parametrize("query", ["?dry_run=true", "?task_ids=t2", "?branch=0"])
def test_unknown_query_parameter_is_422(tmp_path: Path, query: str) -> None:
    client = _client()
    res = _post(_app(tmp_path, client), {"task_ids": ["t2"]}, query)
    assert res.status_code == 422, res.text
    client.create_job.assert_not_called()


def test_failed_task_returns_200_with_results(tmp_path: Path) -> None:
    client = _client()
    client.wait_for_queue_job.return_value = {
        "id": "900",
        "status": "success",
        "isFinished": True,
        "result": {"tasks": [{"id": "t2", "phase": "p2", "status": "error", "jobId": "5"}]},
    }
    res = _post(_app(tmp_path, client), {"task_ids": ["t2"]}, "?wait=true&timeout=30")
    assert res.status_code == 200, res.text
    assert res.json()["failed_task_ids"] == ["t2"]
    assert "SELECTED_TASK_FAILED" in _warning_codes(res)


@pytest.mark.parametrize("denied", ["flow.run", "job.run"])
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "run"])
def test_deny_policy_blocks_before_any_call(tmp_path: Path, denied: str, dry_run: bool) -> None:
    """A `job.run` deny stops the flow route too: it creates a Queue job."""
    client = _client()
    policy = PermissionPolicy(mode="allow", deny=[denied])
    res = _post(_app(tmp_path, client, policy), {"task_ids": ["t2"], "dry_run": dry_run})
    assert res.status_code == 403, res.text
    assert res.json()["error"]["code"] == "PERMISSION_DENIED"
    client.get_config_detail.assert_not_called()
    client.create_job.assert_not_called()


def test_allow_policy_still_runs(tmp_path: Path) -> None:
    client = _client()
    policy = PermissionPolicy(mode="allow", deny=["job.terminate"])
    res = _post(_app(tmp_path, client, policy), {"task_ids": ["t2"]})
    assert res.status_code == 200, res.text
    client.create_job.assert_called_once()


def test_parent_error_body_carries_warnings_and_task_results(tmp_path: Path) -> None:
    from keboola_agent_cli.errors import ErrorCode, KeboolaApiError

    client = _client()
    client.wait_for_queue_job.side_effect = KeboolaApiError(
        message="Queue job 900 failed", status_code=500, error_code=ErrorCode.QUEUE_JOB_FAILED
    )
    client.get_job_detail.return_value = {
        "id": "900",
        "status": "error",
        "isFinished": True,
        "result": {"tasks": [{"id": "t2", "phase": "p2", "status": "error", "jobId": "5"}]},
    }
    res = _post(_app(tmp_path, client), {"from_phase_id": "p2"}, "?wait=true")
    assert res.status_code == 502, res.text
    body = res.json()
    assert body["error"]["code"] == "QUEUE_JOB_FAILED"
    codes = [w["code"] for w in body["warnings"]]
    assert "PARTIAL_RUN_IGNORES_CONDITIONS" in codes
    assert "SELECTED_TASK_FAILED" in codes
    assert body["error"]["details"]["failed_task_ids"] == ["t2"]


@pytest.mark.parametrize("body", [{"task_ids": ["t,1"]}, {"from_phase_id": "p,1"}])
def test_comma_in_selector_is_400(tmp_path: Path, body: dict[str, Any]) -> None:
    client = _client()
    res = _post(_app(tmp_path, client), body)
    assert res.status_code == 400, res.text
    assert res.json()["error"]["code"] == "INVALID_ARGUMENT"
    client.create_job.assert_not_called()
