"""HTTP-layer tests for ``/v1/agent-studio/playbooks``.

Mirrors the smoke-test pattern from ``test_server_smoke.py``: an
in-process FastAPI ``TestClient`` against ``create_app`` with a fresh
``tmp_path`` for the config dir, so nothing touches the user's real
``~/.config/keboola-agent-cli`` directory.
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


def test_list_is_protected(client: TestClient) -> None:
    res = client.get("/v1/agent-studio/playbooks")
    assert res.status_code == 401


def test_list_starts_empty(client: TestClient) -> None:
    res = client.get("/v1/agent-studio/playbooks", headers=AUTH)
    assert res.status_code == 200
    assert res.json() == {"playbooks": []}


def test_create_then_list_round_trip(client: TestClient) -> None:
    create_payload = {
        "name": "Cross-source CRM Cleanup",
        "description": "Reconciles SF + HS + ZD contact records.",
    }
    created = client.post(
        "/v1/agent-studio/playbooks",
        headers=AUTH,
        json=create_payload,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == create_payload["name"]
    assert body["description"] == create_payload["description"]
    # Server stamps ID + timestamps; ignore any client values.
    assert body["id"]
    assert body["created_at"]
    assert body["updated_at"]
    assert body["status"] == "draft"
    assert body["revision"] == 1

    listed = client.get("/v1/agent-studio/playbooks", headers=AUTH).json()
    assert len(listed["playbooks"]) == 1
    assert listed["playbooks"][0]["id"] == body["id"]


def test_get_one_returns_full_body(client: TestClient) -> None:
    created = client.post(
        "/v1/agent-studio/playbooks",
        headers=AUTH,
        json={
            "name": "Sales Pipeline",
            "triggers": [{"type": "cron", "config": {"expression": "0 7 * * 1"}}],
        },
    ).json()
    res = client.get(f"/v1/agent-studio/playbooks/{created['id']}", headers=AUTH)
    assert res.status_code == 200
    body = res.json()
    assert body["triggers"] == [{"type": "cron", "config": {"expression": "0 7 * * 1"}}]


def test_get_one_missing_returns_404(client: TestClient) -> None:
    res = client.get("/v1/agent-studio/playbooks/does-not-exist", headers=AUTH)
    assert res.status_code == 404


def test_delete_removes_playbook(client: TestClient) -> None:
    created = client.post(
        "/v1/agent-studio/playbooks", headers=AUTH, json={"name": "Doomed"}
    ).json()
    res = client.delete(f"/v1/agent-studio/playbooks/{created['id']}", headers=AUTH)
    assert res.status_code == 204
    res2 = client.get(f"/v1/agent-studio/playbooks/{created['id']}", headers=AUTH)
    assert res2.status_code == 404


def test_delete_missing_returns_404(client: TestClient) -> None:
    res = client.delete("/v1/agent-studio/playbooks/ghost", headers=AUTH)
    assert res.status_code == 404


def test_create_rejects_missing_required_fields(client: TestClient) -> None:
    res = client.post("/v1/agent-studio/playbooks", headers=AUTH, json={})
    assert res.status_code == 422


def test_openapi_lists_playbook_routes(client: TestClient) -> None:
    """Without this guard, refactors that drop the router silently
    would not be caught until the React UI 404s in production."""

    spec = client.get("/openapi.json").json()
    paths = set(spec["paths"].keys())
    assert "/v1/agent-studio/playbooks" in paths
    assert "/v1/agent-studio/playbooks/{playbook_id}" in paths
