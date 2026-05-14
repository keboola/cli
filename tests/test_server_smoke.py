"""Smoke test for the kbagent serve FastAPI app.

Verifies that:
- The app builds with no errors and registers all expected routers.
- Public health endpoints work without auth.
- Protected endpoints require Bearer auth.
- /openapi.json is reachable and lists at least one path per router.

Does NOT make real Keboola API calls -- it just exercises the FastAPI
wiring. Real-API behavior is covered by `tests/test_e2e.py` separately.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

if importlib.util.find_spec("fastapi") is None:  # pragma: no cover
    pytest.skip(
        "FastAPI not installed; run `uv pip install -e '.[server]'`", allow_module_level=True
    )

from fastapi.testclient import TestClient

from keboola_agent_cli.server import create_app

EXPECTED_ROUTER_PREFIXES = {
    "/health/ping",
    "/projects",
    "/configs",
    "/components",
    "/storage/buckets",
    "/jobs",
    "/branches",
    "/workspaces",
    "/flows",
    "/schedules",
    "/lineage/edges",
    "/sharing",
    "/data-apps",
    "/mcp/tools",
    "/kai/ping",
    "/encrypt/values",
    "/search",
    "/org/setup",
    "/members/{project}",
    "/version",
    "/changelog",
    "/doctor",
}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """Build the app against an empty config dir so no real HTTP calls leak out."""
    app = create_app(config_dir=str(tmp_path), auth_token="test-token")
    return TestClient(app)


def test_ping_is_public(client: TestClient) -> None:
    res = client.get("/health/ping")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_projects_requires_auth(client: TestClient) -> None:
    res = client.get("/projects")
    assert res.status_code == 401


def test_projects_with_auth_returns_empty_list(client: TestClient) -> None:
    res = client.get("/projects", headers={"Authorization": "Bearer test-token"})
    assert res.status_code == 200
    assert res.json() == {"projects": []}


def test_invalid_token_rejected(client: TestClient) -> None:
    res = client.get("/projects", headers={"Authorization": "Bearer wrong"})
    assert res.status_code == 401


def test_openapi_lists_all_routers(client: TestClient) -> None:
    res = client.get("/openapi.json")
    assert res.status_code == 200
    spec = res.json()
    paths = set(spec["paths"].keys())
    missing = EXPECTED_ROUTER_PREFIXES - paths
    assert not missing, f"OpenAPI is missing expected paths: {sorted(missing)}"


def test_org_setup_requires_manage_token(client: TestClient) -> None:
    res = client.post(
        "/org/setup",
        headers={"Authorization": "Bearer test-token"},
        json={"stack_url": "https://connection.keboola.com"},
    )
    assert res.status_code == 401
    body = res.json()
    assert "manage" in body["error"]["message"].lower()


def test_doctor_runs(client: TestClient) -> None:
    res = client.get("/doctor", headers={"Authorization": "Bearer test-token"})
    assert res.status_code == 200
    body = res.json()
    assert "checks" in body
    assert "summary" in body
