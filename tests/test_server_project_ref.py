"""`kbagent serve` takes a project ID where a route takes a project alias (CLI-22).

The app-wide dependency translates ``{project}`` / ``?project=``; the projects
router adds ``{alias}`` / ``?alias=`` and the auth router ``?stack=``. Real
config on disk, real registry, only the called service is a mock -- so the test
sees what the service receives.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

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


@dataclass
class AuthStatusStub:
    """A dataclass the auth routes can `asdict()`."""

    ok: bool = True


@pytest.fixture
def app(tmp_path: Path) -> Any:
    projects = {
        "prod": ProjectConfig(stack_url=US, token=TOKEN, project_id=4242),
        "other": ProjectConfig(stack_url=US, token=TOKEN, project_id=1),
        "a": ProjectConfig(stack_url=US, token=TOKEN, project_id=77),
        "b": ProjectConfig(stack_url=EU, token=TOKEN, project_id=77),
    }
    ConfigStore(config_dir=tmp_path).save(AppConfig(projects=projects))
    app = create_app(config_dir=str(tmp_path), auth_token="test-token")
    app.state.registry.branch = MagicMock()
    return app


def test_path_param_project_id_reaches_service_as_alias(app: Any) -> None:
    app.state.registry.branch.reset_branch.return_value = {"ok": True}
    with TestClient(app) as client:
        res = client.post("/branches/4242/reset", headers=AUTH)

    assert res.status_code == 200, res.text
    app.state.registry.branch.reset_branch.assert_called_once_with(alias="prod")


def test_query_param_project_ids_reach_service_as_aliases(app: Any) -> None:
    app.state.registry.branch.list_branches.return_value = {"branches": []}
    with TestClient(app) as client:
        res = client.get("/branches?project=4242&project=other", headers=AUTH)

    assert res.status_code == 200, res.text
    app.state.registry.branch.list_branches.assert_called_once_with(aliases=["prod", "other"])


def test_project_use_route_pins_the_alias(app: Any, tmp_path: Path) -> None:
    with TestClient(app) as client:
        res = client.post("/projects/use/4242", headers=AUTH)

    assert res.status_code == 200, res.text
    assert ConfigStore(config_dir=tmp_path).load().default_project == "prod"


def test_ambiguous_project_id_is_a_config_error(app: Any) -> None:
    with TestClient(app) as client:
        res = client.post("/branches/77/reset", headers=AUTH)

    assert res.status_code == 400
    error = res.json()["error"]
    assert error["code"] == "CONFIG_ERROR"
    assert "'a'" in error["message"]
    assert "'b'" in error["message"]
    app.state.registry.branch.reset_branch.assert_not_called()


@pytest.mark.parametrize("path", ["/auth/status", "/auth/projects"])
def test_auth_stack_query_project_id_reaches_service_as_alias(app: Any, path: str) -> None:
    auth = MagicMock()
    auth.status.return_value = AuthStatusStub()
    auth.list_project_candidates.return_value = AuthStatusStub()
    app.state.registry.auth = auth
    with TestClient(app) as client:
        res = client.get(f"{path}?stack=4242", headers=AUTH)

    assert res.status_code == 200, res.text
    called = auth.status if path == "/auth/status" else auth.list_project_candidates
    called.assert_called_once_with(stack="prod")


def test_auth_stack_url_is_left_alone(app: Any) -> None:
    auth = MagicMock()
    auth.status.return_value = AuthStatusStub()
    app.state.registry.auth = auth
    with TestClient(app) as client:
        res = client.get(f"/auth/status?stack={US}", headers=AUTH)

    assert res.status_code == 200, res.text
    auth.status.assert_called_once_with(stack=US)
