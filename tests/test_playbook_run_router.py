"""HTTP-layer tests for PlaybookRun endpoints.

Covers POST /v1/agent-studio/playbooks/{id}/run (stub),
GET /v1/agent-studio/runs[?playbook_id=X], and
GET /v1/agent-studio/runs/{run_id}.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

if importlib.util.find_spec("fastapi") is None:  # pragma: no cover
    pytest.skip(
        "FastAPI not installed; run `uv pip install -e '.[server]'`",
        allow_module_level=True,
    )

from fastapi.testclient import TestClient

from keboola_agent_cli.server import create_app

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(config_dir=str(tmp_path), auth_token="test-token")
    return TestClient(app)


def _create_playbook(client: TestClient, name: str = "Test") -> str:
    res = client.post(
        "/v1/agent-studio/playbooks",
        headers=AUTH,
        json={"name": name},
    )
    assert res.status_code == 201
    return res.json()["id"]


def test_runs_list_protected(client: TestClient) -> None:
    res = client.get("/v1/agent-studio/runs")
    assert res.status_code == 401


def test_runs_list_starts_empty(client: TestClient) -> None:
    res = client.get("/v1/agent-studio/runs", headers=AUTH)
    assert res.status_code == 200
    assert res.json() == {"runs": []}


def test_post_run_for_missing_playbook_returns_404(client: TestClient) -> None:
    res = client.post("/v1/agent-studio/playbooks/ghost/run", headers=AUTH, json={})
    assert res.status_code == 404


def test_post_run_creates_done_stub(client: TestClient) -> None:
    pid = _create_playbook(client)
    res = client.post(f"/v1/agent-studio/playbooks/{pid}/run", headers=AUTH, json={})
    assert res.status_code == 201
    body = res.json()
    assert body["playbook_id"] == pid
    assert body["status"] == "done"
    assert body["playbook_revision"] == 1
    assert body["ended_at"]
    assert "stub" in (body["summary"] or "")
    assert body["objective_override"] is None


def test_post_run_propagates_objective_override(client: TestClient) -> None:
    pid = _create_playbook(client)
    res = client.post(
        f"/v1/agent-studio/playbooks/{pid}/run",
        headers=AUTH,
        json={"objective_override": "Only yesterday's deductions."},
    )
    assert res.status_code == 201
    assert res.json()["objective_override"] == "Only yesterday's deductions."


def test_runs_list_after_run(client: TestClient) -> None:
    pid = _create_playbook(client)
    run = client.post(f"/v1/agent-studio/playbooks/{pid}/run", headers=AUTH, json={}).json()

    res = client.get("/v1/agent-studio/runs", headers=AUTH)
    assert res.status_code == 200
    listed = res.json()["runs"]
    assert len(listed) == 1
    assert listed[0]["id"] == run["id"]


def test_runs_list_filters_by_playbook_id(client: TestClient) -> None:
    p1 = _create_playbook(client, "First")
    p2 = _create_playbook(client, "Second")
    client.post(f"/v1/agent-studio/playbooks/{p1}/run", headers=AUTH, json={})
    client.post(f"/v1/agent-studio/playbooks/{p2}/run", headers=AUTH, json={})
    client.post(f"/v1/agent-studio/playbooks/{p1}/run", headers=AUTH, json={})

    only_p1 = client.get("/v1/agent-studio/runs", headers=AUTH, params={"playbook_id": p1}).json()[
        "runs"
    ]
    assert len(only_p1) == 2
    assert {r["playbook_id"] for r in only_p1} == {p1}


def test_get_run_returns_full_body(client: TestClient) -> None:
    pid = _create_playbook(client)
    created = client.post(f"/v1/agent-studio/playbooks/{pid}/run", headers=AUTH, json={}).json()
    res = client.get(f"/v1/agent-studio/runs/{created['id']}", headers=AUTH)
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == created["id"]
    assert body["playbook_id"] == pid


def test_get_run_missing_returns_404(client: TestClient) -> None:
    res = client.get("/v1/agent-studio/runs/does-not-exist", headers=AUTH)
    assert res.status_code == 404


def test_openapi_lists_run_routes(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    paths = set(spec["paths"].keys())
    assert "/v1/agent-studio/playbooks/{playbook_id}/run" in paths
    assert "/v1/agent-studio/runs" in paths
    assert "/v1/agent-studio/runs/{run_id}" in paths
