"""Tests for the static Blueprint catalogue + router + fork action."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from keboola_agent_cli.agent_studio.blueprints_catalog import (
    BLUEPRINTS,
    get_blueprint,
    list_blueprints,
)
from keboola_agent_cli.agent_studio.models.blueprint import (
    BLUEPRINT_CATEGORIES,
    Blueprint,
)


def test_catalogue_is_non_empty() -> None:
    assert len(BLUEPRINTS) >= 5


def test_every_blueprint_has_a_known_category() -> None:
    for bp in BLUEPRINTS:
        assert bp.category in BLUEPRINT_CATEGORIES, (
            f"{bp.id} has category {bp.category!r} not in BLUEPRINT_CATEGORIES"
        )


def test_blueprint_ids_are_unique() -> None:
    ids = [bp.id for bp in BLUEPRINTS]
    assert len(ids) == len(set(ids))


def test_list_blueprints_all_returns_full_catalogue() -> None:
    assert len(list_blueprints()) == len(BLUEPRINTS)
    assert len(list_blueprints("All")) == len(BLUEPRINTS)


def test_list_blueprints_filters_by_category() -> None:
    cleanup = list_blueprints("Data Cleanup")
    assert len(cleanup) >= 1
    assert all(bp.category == "Data Cleanup" for bp in cleanup)


def test_list_blueprints_unknown_category_is_empty() -> None:
    assert list_blueprints("No Such Category") == []


def test_get_blueprint_known_and_unknown() -> None:
    assert get_blueprint("cross-source-crm-cleanup") is not None
    assert get_blueprint("does-not-exist") is None


def test_blueprint_model_round_trips() -> None:
    bp = Blueprint(
        id="x",
        name="X",
        category="Data Cleanup",
        description="desc",
        systems=["keboola.ex-salesforce"],
    )
    dumped = bp.model_dump(mode="json")
    assert Blueprint.model_validate(dumped) == bp


# ── router + fork tests ─────────────────────────────────────────────

if importlib.util.find_spec("fastapi") is None:  # pragma: no cover
    pytest.skip(
        "FastAPI not installed; run `uv pip install -e '.[server]'`",
        allow_module_level=True,
    )

from fastapi.testclient import TestClient  # noqa: E402

from keboola_agent_cli.server import create_app  # noqa: E402

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(config_dir=str(tmp_path), auth_token="test-token")
    return TestClient(app)


def test_list_route_requires_auth(client: TestClient) -> None:
    assert client.get("/v1/agent-studio/blueprints").status_code == 401


def test_list_route_returns_catalogue(client: TestClient) -> None:
    res = client.get("/v1/agent-studio/blueprints", headers=AUTH)
    assert res.status_code == 200
    body = res.json()
    assert len(body["blueprints"]) == len(BLUEPRINTS)


def test_list_route_filters_by_category(client: TestClient) -> None:
    res = client.get(
        "/v1/agent-studio/blueprints",
        headers=AUTH,
        params={"category": "Process Mining"},
    )
    assert res.status_code == 200
    cats = {bp["category"] for bp in res.json()["blueprints"]}
    assert cats == {"Process Mining"}


def test_get_one_blueprint(client: TestClient) -> None:
    res = client.get("/v1/agent-studio/blueprints/cross-source-crm-cleanup", headers=AUTH)
    assert res.status_code == 200
    assert res.json()["name"] == "Cross-source CRM Cleanup"


def test_get_unknown_blueprint_404(client: TestClient) -> None:
    res = client.get("/v1/agent-studio/blueprints/ghost", headers=AUTH)
    assert res.status_code == 404


def test_fork_creates_prefilled_playbook(client: TestClient) -> None:
    res = client.post(
        "/v1/agent-studio/blueprints/cross-source-crm-cleanup/fork",
        headers=AUTH,
    )
    assert res.status_code == 201
    pb = res.json()
    assert pb["name"] == "Cross-source CRM Cleanup"
    assert pb["status"] == "draft"
    assert "keboola.connection" in pb["connections"]
    assert "entity-resolution" in pb["skills"]
    assert pb["id"]  # server-issued

    # The forked Playbook is now in the library.
    listed = client.get("/v1/agent-studio/playbooks", headers=AUTH).json()
    assert any(p["id"] == pb["id"] for p in listed["playbooks"])


def test_fork_unknown_blueprint_404(client: TestClient) -> None:
    res = client.post("/v1/agent-studio/blueprints/ghost/fork", headers=AUTH)
    assert res.status_code == 404


def test_openapi_lists_blueprint_routes(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    paths = set(spec["paths"].keys())
    assert "/v1/agent-studio/blueprints" in paths
    assert "/v1/agent-studio/blueprints/{blueprint_id}" in paths
    assert "/v1/agent-studio/blueprints/{blueprint_id}/fork" in paths
