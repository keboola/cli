"""`kbagent serve` keeps the active project and the auth stack like the CLI (CLI-32).

Real config on disk and the real service registry, so each route runs the same
service code as its command: removing the active project clears it, `GET
/projects/current` reports the resolved project, and `GET /auth/status` without
`?stack=` takes the stack from ``KBAGENT_PROJECT`` before the active project.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

if importlib.util.find_spec("fastapi") is None:  # pragma: no cover
    pytest.skip(
        "FastAPI not installed; run `uv pip install -e '.[server]'`", allow_module_level=True
    )

from fastapi.testclient import TestClient

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import AppConfig, ProjectConfig
from keboola_agent_cli.server import create_app

AUTH = {"Authorization": "Bearer test-token"}
US = "https://connection.keboola.com"
EU = "https://connection.eu-central-1.keboola.com"
TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("KBAGENT_PROJECT", raising=False)
    projects = {
        "prod": ProjectConfig(stack_url=US, token=TOKEN, project_id=1),
        "eu": ProjectConfig(stack_url=EU, token=TOKEN, project_id=2),
        "dev": ProjectConfig(stack_url=US, token=TOKEN, project_id=3),
    }
    ConfigStore(config_dir=tmp_path).save(AppConfig(projects=projects, default_project="prod"))
    return create_app(config_dir=str(tmp_path), auth_token="test-token")


def test_delete_active_project_clears_it(app: Any, tmp_path: Path) -> None:
    with TestClient(app) as client:
        res = client.delete("/projects/prod", headers=AUTH)

    assert res.status_code == 200, res.text
    assert res.json()["active_project_cleared"] == "prod"
    assert ConfigStore(config_dir=tmp_path).load().default_project == ""


def test_delete_other_project_keeps_active(app: Any, tmp_path: Path) -> None:
    with TestClient(app) as client:
        res = client.delete("/projects/dev", headers=AUTH)

    assert res.status_code == 200, res.text
    assert res.json()["active_project_cleared"] is None
    assert ConfigStore(config_dir=tmp_path).load().default_project == "prod"


def test_bulk_delete_reports_cleared_active_project(app: Any, tmp_path: Path) -> None:
    with TestClient(app) as client:
        res = client.post("/projects/bulk-delete", json={"aliases": ["dev", "prod"]}, headers=AUTH)

    assert res.status_code == 200, res.text
    assert res.json()["active_project_cleared"] == "prod"
    assert ConfigStore(config_dir=tmp_path).load().default_project == ""


def test_bulk_delete_dry_run_changes_nothing(app: Any, tmp_path: Path) -> None:
    with TestClient(app) as client:
        res = client.post(
            "/projects/bulk-delete", json={"aliases": ["prod"], "dry_run": True}, headers=AUTH
        )

    assert res.status_code == 200, res.text
    assert res.json()["active_project_cleared"] is None
    assert ConfigStore(config_dir=tmp_path).load().default_project == "prod"


def test_current_reports_resolved_project(app: Any) -> None:
    with TestClient(app) as client:
        before = client.get("/projects/current", headers=AUTH).json()
        client.delete("/projects/prod", headers=AUTH)
        after = client.get("/projects/current", headers=AUTH).json()

    assert (before["resolved_alias"], before["resolved_source"]) == ("prod", "active_project")
    # Two projects left and none active: the chain has no answer.
    assert (after["resolved_alias"], after["resolved_source"]) == (None, "none")


def test_auth_status_takes_stack_from_env_project(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KBAGENT_PROJECT", "eu")
    with TestClient(app) as client:
        res = client.get("/auth/status", headers=AUTH)

    assert res.status_code == 200, res.text
    assert res.json()["stack_url"] == EU


def test_auth_status_explicit_stack_wins_over_env(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KBAGENT_PROJECT", "eu")
    with TestClient(app) as client:
        res = client.get("/auth/status?stack=prod", headers=AUTH)

    assert res.status_code == 200, res.text
    assert res.json()["stack_url"] == US


def test_auth_status_unregistered_env_project_is_config_error(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KBAGENT_PROJECT", "ghost")
    with TestClient(app) as client:
        res = client.get("/auth/status", headers=AUTH)

    assert res.status_code == 400
    assert res.json()["error"]["code"] == "CONFIG_ERROR"
