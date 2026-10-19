"""`branch_id=0` over `kbagent serve` means production, also with an active branch (CLI-43).

The web UI sends `branch_id: 0` in its production view (CLI spec V2), so each
branch-aware route that the UI calls must pass 0 through as production and not
read it as "no branch_id" (which some routes resolve to the active branch). Real
config on disk with an active branch, real registry and services; only the
Keboola API clients are mocks, so the test sees the branch every API call gets.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

if importlib.util.find_spec("fastapi") is None:  # pragma: no cover
    pytest.skip(
        "FastAPI not installed; run `uv pip install -e '.[server]'`", allow_module_level=True
    )

from fastapi.testclient import TestClient

from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import AppConfig, ProjectConfig
from keboola_agent_cli.server import create_app
from keboola_agent_cli.server.agent_runner import build_local_ai_meta_prompt

AUTH = {"Authorization": "Bearer test-token"}
TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
ACTIVE_BRANCH = 777
DEFAULT_BRANCH = 1  # the numeric ID of production, for API paths that need one
TABLE = "in.c-main.orders"


def _client() -> MagicMock:
    client = MagicMock()
    client.get_config_detail.return_value = {"id": "42", "name": "cfg", "configuration": {}}
    client.list_dev_branches.return_value = [
        {"id": DEFAULT_BRANCH, "isDefault": True},
        {"id": ACTIVE_BRANCH, "isDefault": False},
    ]
    client.get_table_detail.return_value = {"id": TABLE, "columns": ["id"], "bucket": {}}
    client.create_job.return_value = {"id": "9001"}
    client.get_app.return_value = {"id": "123", "configId": "42"}
    client.list_apps.return_value = []
    return client


@pytest.fixture
def api_client() -> MagicMock:
    return _client()


@pytest.fixture
def app(tmp_path: Path, api_client: MagicMock) -> Any:
    project = ProjectConfig(
        stack_url="https://connection.keboola.com",
        token=TOKEN,
        project_id=4242,
        active_branch_id=ACTIVE_BRANCH,
        active_branch_name="feature-x",
    )
    ConfigStore(config_dir=tmp_path).save(AppConfig(projects={"prod": project}))
    app = create_app(config_dir=str(tmp_path), auth_token="test-token")
    # Every Keboola API client of every service is the one mock.
    for service in vars(app.state.registry).values():
        for name in list(getattr(service, "__dict__", {})):
            if name.endswith("client_factory"):
                setattr(service, name, lambda *_args, **_kwargs: api_client)
    return app


def _branches_sent(client: MagicMock) -> list[Any]:
    """The `branch_id` of every API client call that took one."""
    return [
        kwargs["branch_id"] for _name, _args, kwargs in client.mock_calls if "branch_id" in kwargs
    ]


# Each branch-aware route that the web UI calls, with `branch_id=0`.
UI_ROUTES = [
    pytest.param(
        "GET", "/configs", {"params": {"project": "prod", "branch_id": 0}}, id="config-list"
    ),
    pytest.param("GET", "/configs/trash/prod", {"params": {"branch_id": 0}}, id="config-trash"),
    pytest.param(
        "POST",
        "/configs/prod/keboola.ex-http/42/restore",
        {"params": {"branch_id": 0}},
        id="config-restore",
    ),
    pytest.param(
        "GET", "/configs/prod/keboola.ex-http/42", {"params": {"branch_id": 0}}, id="config-detail"
    ),
    pytest.param(
        "DELETE",
        "/configs/prod/keboola.ex-http/42",
        {"params": {"branch_id": 0, "dry_run": False}},
        id="config-delete",
    ),
    pytest.param(
        "POST",
        "/jobs/prod/run",
        {"json": {"component_id": "keboola.ex-http", "config_id": "42", "branch_id": 0}},
        id="job-run",
    ),
    pytest.param(
        "GET", "/data-apps", {"params": {"project": "prod", "branch_id": 0}}, id="data-app-list"
    ),
    pytest.param("GET", "/data-apps/prod/123", {"params": {"branch_id": 0}}, id="data-app-detail"),
    pytest.param(
        "GET",
        "/flows",
        {"params": {"project": "prod", "branch_id": 0, "with_schedules": True}},
        id="flow-list",
    ),
    pytest.param(
        "GET",
        "/flows/prod/42",
        {"params": {"component_id": "keboola.flow", "branch_id": 0}},
        id="flow-detail",
    ),
    pytest.param(
        "GET", "/schedules", {"params": {"project": "prod", "branch_id": 0}}, id="schedule-list"
    ),
    pytest.param(
        "GET",
        "/storage/buckets",
        {"params": {"project": "prod", "branch_id": 0}},
        id="storage-buckets",
    ),
    pytest.param(
        "GET",
        "/storage/tables",
        {"params": {"project": "prod", "branch_id": 0}},
        id="storage-tables",
    ),
    pytest.param(
        "GET",
        f"/storage/table-detail/prod/{TABLE}",
        {"params": {"branch_id": 0}},
        id="storage-table-detail",
    ),
    pytest.param(
        "GET",
        f"/storage/table-download/prod/{TABLE}",
        {"params": {"branch_id": 0}},
        id="storage-table-download",
    ),
    pytest.param(
        "POST",
        f"/storage/columns/prod/{TABLE}/describe",
        {"json": {"columns": {"id": "Order ID"}, "branch_id": 0}},
        id="storage-column-describe",
    ),
    pytest.param(
        "DELETE",
        "/storage/tables/prod",
        {"params": {"table_id": TABLE, "branch_id": 0, "force": True}},
        id="storage-table-delete",
    ),
]


@pytest.mark.parametrize(("method", "path", "request_kwargs"), UI_ROUTES)
def test_branch_id_0_acts_on_production_with_an_active_branch(
    app: Any, api_client: MagicMock, method: str, path: str, request_kwargs: dict[str, Any]
) -> None:
    with TestClient(app) as client:
        res = client.request(method, path, headers=AUTH, **request_kwargs)

    assert res.status_code == 200, res.text
    sent = _branches_sent(api_client)
    assert sent, f"{method} {path} made no API call with a branch_id"
    assert set(sent) <= {0, None, DEFAULT_BRANCH}, f"{method} {path} used branch {sent}"


def test_omitted_branch_id_uses_the_active_branch(app: Any, api_client: MagicMock) -> None:
    """The case the UI avoids: no branch_id on a route that applies the active branch."""
    with TestClient(app) as client:
        res = client.get("/configs", headers=AUTH, params={"project": "prod"})

    assert res.status_code == 200, res.text
    assert ACTIVE_BRANCH in _branches_sent(api_client)


@pytest.mark.parametrize(
    ("method", "path", "request_kwargs"),
    [
        pytest.param("GET", "/stream/prod/list", {"params": {"branch": "0"}}, id="stream-list"),
        pytest.param(
            "GET",
            "/stream/prod/detail",
            {"params": {"source_id": "src", "branch": "0"}},
            id="stream-detail",
        ),
        pytest.param(
            "POST",
            "/stream/prod/delete",
            {"json": {"source_id": "src", "branch_id": "0"}},
            id="stream-delete",
        ),
        pytest.param(
            "POST",
            "/stream/prod/create-source",
            {"json": {"name": "otel", "branch_id": "0", "provision_sinks": False}},
            id="stream-create-source",
        ),
    ],
)
def test_stream_branch_0_is_the_default_branch(
    app: Any, api_client: MagicMock, method: str, path: str, request_kwargs: dict[str, Any]
) -> None:
    with TestClient(app) as client:
        res = client.request(method, path, headers=AUTH, **request_kwargs)

    assert res.status_code == 200, res.text
    refs = {args[0] for _name, args, _kwargs in api_client.mock_calls if args}
    assert "default" in refs
    assert "0" not in refs


def test_job_run_with_branch_0_sends_no_branch_id_to_the_queue() -> None:
    client = KeboolaClient.__new__(KeboolaClient)
    response = MagicMock()
    response.json.return_value = {"id": "9001"}
    client._queue_request = MagicMock(return_value=response)  # type: ignore[method-assign]

    client.create_job(component_id="keboola.ex-http", config_id="42", branch_id=0)

    body = client._queue_request.call_args.kwargs["json"]
    assert "branchId" not in body


def test_local_ai_prompt_for_production_asks_for_branch_0() -> None:
    production = build_local_ai_meta_prompt(message="list configs", branch_id=0)
    unset = build_local_ai_meta_prompt(message="list configs", branch_id=None)
    dev = build_local_ai_meta_prompt(message="list configs", branch_id=456)

    assert "`--branch 0`" in production
    assert "--branch" not in unset
    assert "`--branch 456`" in dev
