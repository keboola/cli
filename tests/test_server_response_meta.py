"""Contract cases of `kbagent serve` for spec V2, V3, V4, V8 and V9 (check rule `serve.targets-reported`).

- V2: the 26 routes whose CLI command applies the active branch use it when
  ``branch_id`` is omitted, and production with ``branch_id: 0``.
- V3: a response carries the ``targets`` record in ``Kbagent-Targets`` and
  the warnings in ``Kbagent-Warnings``. An error body carries both as keys.
- V4: the error object has the CLI error fields, and its HTTP status follows
  the error-code table for every code.
- V8: the three routes that change the saved choice say that it is shared.
- V9: the four partial-update routes take ``PATCH``. Their ``PUT`` warns.

Real config on disk with an active branch, real registry and services. Only
the Keboola API clients are one mock, so no request leaves the process.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

if importlib.util.find_spec("fastapi") is None:  # pragma: no cover
    pytest.skip(
        "FastAPI not installed; run `uv pip install -e '.[server]'`", allow_module_level=True
    )

from fastapi import Depends
from fastapi.testclient import TestClient

from keboola_agent_cli.commands._helpers import DEPRECATIONS
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import (
    ERROR_CODE_TABLE,
    ConfigError,
    ErrorCode,
    KeboolaApiError,
    SessionAuthUnsupportedError,
)
from keboola_agent_cli.models import AppConfig, ProjectConfig, TokenVerifyResponse
from keboola_agent_cli.server import create_app
from keboola_agent_cli.server.dependencies import require_permission
from keboola_agent_cli.server.response_meta import (
    TARGETS_HEADER,
    WARNINGS_HEADER,
    http_status_for,
)

AUTH = {"Authorization": "Bearer test-token"}
TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
ACTIVE_BRANCH = 777
DEFAULT_BRANCH = 1
TABLE = "in.c-main.orders"
CONFIG = "/configs/prod/keboola.ex-http/42"


def _api_client() -> MagicMock:
    client = MagicMock()
    client.get_config_detail.return_value = {"id": "42", "name": "cfg", "configuration": {}}
    client.list_dev_branches.return_value = [
        {"id": DEFAULT_BRANCH, "isDefault": True},
        {"id": ACTIVE_BRANCH, "isDefault": False},
    ]
    client.get_table_detail.return_value = {"id": TABLE, "columns": ["id"], "bucket": {}}
    client.create_job.return_value = {"id": "9001"}
    client.list_jobs.return_value = []
    client.list_components.return_value = []
    client.verify_token.return_value = TokenVerifyResponse(
        token_id="1", token_description="t", project_id=4242, project_name="p", owner_name="o"
    )
    return client


def _save_config(config_dir: Path, *, active_branch: int | None) -> None:
    project = ProjectConfig(
        stack_url="https://connection.keboola.com",
        token=TOKEN,
        project_id=4242,
        active_branch_id=active_branch,
        active_branch_name="feature-x" if active_branch else None,
    )
    other = ProjectConfig(stack_url="https://connection.keboola.com", token=TOKEN, project_id=5)
    ConfigStore(config_dir=config_dir).save(
        AppConfig(projects={"prod": project, "other": other}, default_project="prod")
    )


def _app(config_dir: Path, api_client: MagicMock) -> Any:
    app = create_app(config_dir=str(config_dir), auth_token="test-token")
    # Every Keboola API client of every service is the one mock.
    for service in vars(app.state.registry).values():
        for name in list(getattr(service, "__dict__", {})):
            if name.endswith("client_factory"):
                setattr(service, name, lambda *_args, **_kwargs: api_client)
    return app


@pytest.fixture
def api_client() -> MagicMock:
    return _api_client()


@pytest.fixture
def app(tmp_path: Path, api_client: MagicMock) -> Any:
    _save_config(tmp_path, active_branch=ACTIVE_BRANCH)
    return _app(tmp_path, api_client)


def _targets(res: Any) -> list[dict[str, Any]]:
    """The targets of a response: the header, and on an error the body key.

    The 500 of an unhandled error comes from the outermost Starlette layer,
    after the middleware that adds the header, so it has only the body key.
    """
    header = json.loads(res.headers.get(TARGETS_HEADER, "[]"))
    if res.status_code < 400:
        return header
    body = res.json().get("targets", [])
    if res.status_code != 500:
        assert body == header
    return body


def _warnings(res: Any) -> list[dict[str, Any]]:
    return json.loads(res.headers.get(WARNINGS_HEADER, "[]"))


def _send(app: Any, method: str, path: str, kwargs: dict[str, Any]) -> Any:
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.request(method, path, headers=AUTH, **kwargs)


def _with_branch(kwargs: dict[str, Any], branch_id: int) -> dict[str, Any]:
    """``kwargs`` with ``branch_id`` in the place where the route reads it."""
    out = {key: dict(value) if isinstance(value, dict) else value for key, value in kwargs.items()}
    for place in ("json", "data", "params"):
        if place in out:
            out[place]["branch_id"] = branch_id
            return out
    out["params"] = {"branch_id": branch_id}
    return out


def _csv() -> dict[str, Any]:
    return {"file": ("orders.csv", b"id\n1\n", "text/csv")}


# The 26 routes whose CLI command applies the active branch (V2, D4). No
# `branch_id` in any of them.
ACTIVE_BRANCH_ROUTES = [
    pytest.param("POST", "/storage/buckets/prod", {"json": {"stage": "in", "name": "b"}}, id="bucket-create"),
    pytest.param("DELETE", "/storage/buckets/prod", {"params": {"bucket_id": "in.c-b", "dry_run": True}}, id="bucket-delete"),
    pytest.param("POST", "/storage/buckets/prod/in.c-b/describe", {"json": {"description": "d"}}, id="bucket-describe"),
    pytest.param("GET", f"/storage/table-download/prod/{TABLE}", {}, id="table-download"),
    pytest.param("POST", "/storage/tables/prod", {"json": {"bucket_id": "in.c-main", "name": "t", "columns": ["id"]}}, id="table-create"),
    pytest.param("POST", "/storage/tables/prod/upload", {"data": {"table_id": TABLE}, "files": "csv"}, id="table-upload"),
    pytest.param("DELETE", "/storage/tables/prod", {"params": {"table_id": TABLE, "dry_run": True}}, id="table-delete"),
    pytest.param("POST", "/storage/tables/prod/truncate", {"params": {"table_id": TABLE, "dry_run": True}}, id="table-truncate"),
    pytest.param("DELETE", f"/storage/columns/prod/{TABLE}", {"params": {"column": "a", "dry_run": True}}, id="column-delete"),
    pytest.param("POST", f"/storage/tables/prod/{TABLE}/snapshots", {"json": {}}, id="snapshot-create"),
    pytest.param("POST", "/storage/table-from-snapshot/prod", {"json": {"snapshot_id": "1", "bucket_id": "in.c-main", "name": "t2"}, "params": {"dry_run": True}}, id="table-from-snapshot"),
    pytest.param("POST", f"/storage/tables/prod/{TABLE}/describe", {"json": {"description": "d"}}, id="table-describe"),
    pytest.param("POST", f"/storage/columns/prod/{TABLE}/describe", {"json": {"columns": {"id": "d"}}}, id="column-describe"),
    pytest.param("POST", "/storage/columns/prod/describe-migrate", {"json": {"dry_run": True}}, id="describe-migrate"),
    pytest.param("POST", f"/storage/columns/prod/{TABLE}", {"params": {"column": "c"}}, id="column-add"),
    pytest.param("POST", "/storage/files/upload", {"data": {"project": "prod"}, "files": "csv"}, id="file-upload"),
    pytest.param("POST", "/storage/files/prod/load-to-table", {"json": {"file_id": 1, "table_id": TABLE}}, id="file-load"),
    pytest.param("POST", "/jobs/prod/run", {"json": {"component_id": "keboola.ex-http", "config_id": "42"}}, id="job-run"),
    pytest.param("POST", "/jobs/prod/terminate", {"json": {"status": "processing", "dry_run": True}}, id="job-terminate"),
    pytest.param("POST", "/transformations/prod", {"json": {"name": "t", "sql": "SELECT 1", "component_id": "keboola.snowflake-transformation", "dry_run": True}}, id="transformation-create"),
    pytest.param("GET", "/transformations/prod/42", {"params": {"component_id": "keboola.snowflake-transformation"}}, id="transformation-show"),
    pytest.param("PATCH", "/transformations/prod/42", {"json": {"change_description": "c", "component_id": "keboola.snowflake-transformation", "dry_run": True}}, id="transformation-edit"),
    pytest.param("GET", f"{CONFIG}/variables", {}, id="variables-get"),
    pytest.param("PUT", f"{CONFIG}/variables", {"json": {"variables": {"a": "b"}, "replace": True}}, id="variables-replace"),
    pytest.param("DELETE", f"{CONFIG}/variables", {}, id="variables-clear"),
    pytest.param("POST", "/components/keboola.ex-http/actions/testConnection", {"json": {"project": "prod", "config_data": {}}}, id="sync-action"),
]  # fmt: skip


def _request_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {**kwargs, "files": _csv()} if kwargs.get("files") == "csv" else kwargs


def _branch_records(res: Any) -> list[tuple[Any, ...]]:
    return [
        (t["project_alias"], t["project_source"], t["branch_id"], t["branch_source"])
        for t in _targets(res)
        if t["role"] == "target"
    ]


def test_the_list_has_the_26_routes() -> None:
    assert len(ACTIVE_BRANCH_ROUTES) == 26


@pytest.mark.parametrize(("method", "path", "kwargs"), ACTIVE_BRANCH_ROUTES)
def test_omitted_branch_id_uses_the_active_branch(
    app: Any, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    res = _send(app, method, path, _request_kwargs(kwargs))

    assert res.status_code != 422, res.text
    assert ("prod", "explicit", ACTIVE_BRANCH, "active_branch") in _branch_records(res), res.text


@pytest.mark.parametrize(("method", "path", "kwargs"), ACTIVE_BRANCH_ROUTES)
def test_branch_id_0_is_production(
    app: Any, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    res = _send(app, method, path, _request_kwargs(_with_branch(kwargs, 0)))

    records = _branch_records(res)
    assert records, res.text
    assert all(branch_source == "production" for *_, branch_source in records), records


def test_without_an_active_branch_the_route_uses_production(
    tmp_path: Path, api_client: MagicMock
) -> None:
    _save_config(tmp_path, active_branch=None)
    res = _send(
        _app(tmp_path, api_client),
        "POST",
        "/jobs/prod/run",
        {"json": {"component_id": "keboola.ex-http", "config_id": "42"}},
    )

    assert res.status_code == 200, res.text
    assert _branch_records(res) == [("prod", "explicit", None, "production")]
    assert api_client.create_job.call_args.kwargs["branch_id"] is None


def test_job_run_sends_the_active_branch_to_the_queue(app: Any, api_client: MagicMock) -> None:
    res = _send(
        app,
        "POST",
        "/jobs/prod/run",
        {"json": {"component_id": "keboola.ex-http", "config_id": "42"}},
    )

    assert res.status_code == 200, res.text
    assert api_client.create_job.call_args.kwargs["branch_id"] == ACTIVE_BRANCH


def test_a_fan_out_read_reports_every_project_in_the_header(app: Any) -> None:
    """The fan-out workers resolve the branch in their own threads and add to the request record."""
    res = _send(app, "GET", "/configs", {"params": {"all_projects": True}})

    assert res.status_code == 200, res.text
    assert set(_branch_records(res)) == {
        ("prod", "all", ACTIVE_BRANCH, "active_branch"),
        ("other", "all", None, "production"),
    }


# --------------------------------------------------------------------------- #
# T1, T2 over REST: a list route without `project` reads the active project
# --------------------------------------------------------------------------- #

LIST_ROUTES = [
    "/configs",
    "/configs/search?query=x",
    "/jobs",
    "/storage/buckets",
    "/storage/tables",
    "/flows",
    "/schedules",
    "/schedules/find/query",
    "/data-apps",
    "/workspaces",
    "/branches",
    "/components",
    "/sharing",
    "/sharing/edges",
    "/lineage/edges",
    "/notifications",
    "/billing/credits",
    "/search?query=x",
    "/projects/status",
    "/token/list",
]


@pytest.mark.parametrize("path", LIST_ROUTES)
def test_a_list_route_without_project_reads_the_active_project(app: Any, path: str) -> None:
    res = _send(app, "GET", path, {})

    assert res.status_code == 200, res.text
    assert {(t["project_alias"], t["project_source"]) for t in _targets(res)} == {
        ("prod", "active_project")
    }


@pytest.mark.parametrize("path", LIST_ROUTES)
def test_project_with_all_projects_is_a_usage_error(app: Any, path: str) -> None:
    key = "alias" if path == "/projects/status" else "project"
    route, _, query = path.partition("?")
    params = dict(part.split("=") for part in query.split("&") if part)
    res = _send(app, "GET", route, {"params": {**params, key: "prod", "all_projects": True}})

    assert res.status_code == 400, res.text
    assert res.json()["error"]["code"] == "USAGE_ERROR"


def test_a_list_route_without_an_active_project_is_a_config_error(
    tmp_path: Path, api_client: MagicMock
) -> None:
    _save_config(tmp_path, active_branch=None)
    store = ConfigStore(config_dir=tmp_path)
    config = store.load()
    config.default_project = ""
    store.save(config)
    res = _send(_app(tmp_path, api_client), "GET", "/jobs", {})

    assert res.status_code == 400, res.text
    assert res.json()["error"]["code"] == "CONFIG_ERROR"


def test_a_failed_project_keeps_http_200(app: Any, api_client: MagicMock) -> None:
    api_client.list_jobs.side_effect = KeboolaApiError("down", error_code=ErrorCode.API_ERROR)
    res = _send(app, "GET", "/jobs", {"params": {"all_projects": True}})

    assert res.status_code == 200, res.text
    assert {e["project_alias"] for e in res.json()["errors"]} == {"prod", "other"}


# --------------------------------------------------------------------------- #
# T12 over REST: a destructive route on the active branch warns
# --------------------------------------------------------------------------- #

ACTIVE_CHOICE = "TARGET_FROM_ACTIVE_CHOICE"


def _codes(res: Any) -> list[str]:
    return [w["code"] for w in _warnings(res)]


def test_a_destructive_route_on_the_active_branch_warns(app: Any) -> None:
    res = _send(app, "DELETE", "/storage/tables/prod", {"params": {"table_id": TABLE}})

    assert res.status_code == 200, res.text
    (entry,) = [w for w in _warnings(res) if w["code"] == ACTIVE_CHOICE]
    assert entry["hint"] == f"branch_id={ACTIVE_BRANCH}"
    assert str(ACTIVE_BRANCH) in entry["message"]


def test_a_flag_escalation_counts_as_destructive(app: Any) -> None:
    res = _send(
        app, "PUT", f"{CONFIG}/variables", {"json": {"variables": {"a": "b"}, "replace": True}}
    )

    assert ACTIVE_CHOICE in _codes(res), res.text


def test_an_explicit_branch_id_gives_no_warning(app: Any) -> None:
    res = _send(
        app,
        "DELETE",
        "/storage/tables/prod",
        {"params": {"table_id": TABLE, "branch_id": ACTIVE_BRANCH}},
    )

    assert res.status_code == 200, res.text
    assert ACTIVE_CHOICE not in _codes(res)


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        pytest.param(
            "POST", "/storage/buckets/prod", {"json": {"stage": "in", "name": "b"}}, id="write"
        ),
        pytest.param("GET", f"{CONFIG}/variables", {}, id="read"),
        pytest.param(
            "PATCH", f"{CONFIG}/variables", {"json": {"variables": {"a": "b"}}}, id="merge"
        ),
    ],
)
def test_a_write_or_read_route_gives_no_warning(
    app: Any, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    res = _send(app, method, path, kwargs)

    assert ACTIVE_CHOICE not in _codes(res), res.text


def test_each_request_has_its_own_record(app: Any) -> None:
    with TestClient(app) as client:
        first = client.post(
            "/jobs/prod/run",
            headers=AUTH,
            json={"component_id": "keboola.ex-http", "config_id": "42"},
        )
        second = client.get("/health/ping", headers=AUTH)

    assert _targets(first)
    assert TARGETS_HEADER not in second.headers
    assert WARNINGS_HEADER not in second.headers


def test_concurrent_requests_keep_their_own_records(app: Any, api_client: MagicMock) -> None:
    """Requests that run at the same time in the threadpool do not share a record."""

    def slow_job(**_kwargs: Any) -> dict[str, str]:
        time.sleep(0.05)  # keep the requests in flight together
        return {"id": "9001"}

    api_client.create_job.side_effect = slow_job
    body = {"component_id": "keboola.ex-http", "config_id": "42"}

    async def run_all() -> list[Any]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await asyncio.gather(
                *[
                    client.post(f"/jobs/{alias}/run", headers=AUTH, json=body)
                    for alias in ["prod", "other"] * 4
                ]
            )

    responses = asyncio.run(run_all())

    for alias, res in zip(["prod", "other"] * 4, responses, strict=True):
        assert res.status_code == 200, res.text
        assert {t["project_alias"] for t in _targets(res)} == {alias}


def test_an_error_body_carries_targets_and_the_cli_error_fields(
    app: Any, api_client: MagicMock
) -> None:
    api_client.create_job.side_effect = KeboolaApiError(
        "Config not found", status_code=404, error_code=ErrorCode.NOT_FOUND, retryable=False
    )
    res = _send(
        app,
        "POST",
        "/jobs/prod/run",
        {"json": {"component_id": "keboola.ex-http", "config_id": "42"}},
    )

    assert res.status_code == 404
    body = res.json()
    assert body["status"] == "error"
    assert ("prod", "explicit", ACTIVE_BRANCH, "active_branch") in _branch_records(res)
    assert body["error"] == {
        "code": "NOT_FOUND",
        "error_type": "not_found",
        "message": "Config not found",
        "project": "prod",
        "retryable": False,
    }


def test_an_error_keeps_retryable_and_details(app: Any, api_client: MagicMock) -> None:
    api_client.create_job.side_effect = KeboolaApiError(
        "Busy", status_code=503, error_code=ErrorCode.API_ERROR, retryable=True, details={"a": 1}
    )
    res = _send(
        app,
        "POST",
        "/jobs/prod/run",
        {"json": {"component_id": "keboola.ex-http", "config_id": "42"}},
    )

    assert res.status_code == 502
    assert res.json()["error"]["retryable"] is True
    assert res.json()["error"]["details"] == {"a": 1}


# --------------------------------------------------------------------------- #
# V4: the HTTP status of every error code follows the error-code table
# --------------------------------------------------------------------------- #

# The statuses the server answered before the table decided them. They stay.
KEPT_STATUSES = {
    ErrorCode.SESSION_EXPIRED: 401,
    ErrorCode.SESSION_NOT_FOUND: 401,
    ErrorCode.INVALID_ARGUMENT: 400,
    ErrorCode.WORKSPACE_LOAD_COPY_TOO_LARGE: 400,
    ErrorCode.MR_MERGE_CONFLICT: 409,
    ErrorCode.MR_NOT_READY_TO_MERGE: 409,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.CONFIG_ERROR: 400,
    ErrorCode.API_ERROR: 502,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.INTERNAL_ERROR: 500,
}


def _expected_status(code: str) -> int:
    if code == ErrorCode.UNAUTHORIZED:
        return 401  # the bearer check of the server
    if code == ErrorCode.INTERNAL_ERROR:
        return 500  # an unhandled error
    entry = ERROR_CODE_TABLE[code]
    by_type = {
        "validation": 400,
        "configuration": 400,
        "authentication": 401,
        "authorization": 403,
        "not_found": 404,
        "conflict": 409,
    }
    if entry.error_type in by_type:
        return by_type[entry.error_type]
    return 400 if entry.exit_code == 2 else 502


@pytest.mark.parametrize("code", sorted(KEPT_STATUSES))
def test_the_former_statuses_stay(code: ErrorCode) -> None:
    assert http_status_for(code) == KEPT_STATUSES[code]


@pytest.fixture(scope="module")
def raising_client(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """One app with a probe route that raises the code named in its path."""
    config_dir = tmp_path_factory.mktemp("codes")
    _save_config(config_dir, active_branch=None)
    app = _app(config_dir, _api_client())

    # A probe route with its own permission check, so the route table lets it through.
    @app.get("/_raise/{project}/{code}", dependencies=[Depends(require_permission("project.list"))])
    def _raise(project: str, code: str) -> None:
        raise KeboolaApiError("boom", error_code=ErrorCode(code))

    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("code", sorted(ErrorCode))
def test_every_code_answers_the_status_of_its_table_entry(
    raising_client: Any, code: ErrorCode
) -> None:
    res = raising_client.get(f"/_raise/prod/{code.value}", headers=AUTH)

    assert res.status_code == _expected_status(code)
    error = res.json()["error"]
    assert error["code"] == code
    assert error["error_type"] == ERROR_CODE_TABLE[code].error_type
    assert error["project"] == "prod"


def test_a_code_answers_one_status_from_either_exception(tmp_path: Path) -> None:
    """AUTH_NOT_SUPPORTED_ON_STACK was 502 as KeboolaApiError and 400 as ConfigError."""
    _save_config(tmp_path, active_branch=None)
    app = _app(tmp_path, _api_client())

    # A probe route with its own permission check, so the route table lets it through.
    read = [Depends(require_permission("project.list"))]

    @app.get("/_api", dependencies=read)
    def _api() -> None:
        raise KeboolaApiError("no", error_code=ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK)

    @app.get("/_config", dependencies=read)
    def _config() -> None:
        raise SessionAuthUnsupportedError("stream")

    @app.get("/_plain", dependencies=read)
    def _plain() -> None:
        raise ConfigError("bad")

    with TestClient(app) as client:
        statuses = {
            path: client.get(path, headers=AUTH).status_code
            for path in ("/_api", "/_config", "/_plain")
        }

    assert statuses == {"/_api": 400, "/_config": 400, "/_plain": 400}


# --------------------------------------------------------------------------- #
# V8: the saved-choice routes say that the choice is shared
# --------------------------------------------------------------------------- #


def _shared_warning(res: Any, config_dir: Path) -> None:
    (entry,) = _warnings(res)
    assert entry["code"] == "ACTIVE_CHOICE_SHARED"
    assert entry["config_dir"] == str(config_dir)
    assert str(config_dir) in entry["message"]


def test_project_use_says_the_choice_is_shared(app: Any, tmp_path: Path) -> None:
    res = _send(app, "POST", "/projects/use/other", {})

    assert res.status_code == 200, res.text
    assert res.json()["active_project_set"] == "other"
    _shared_warning(res, tmp_path)


def test_branch_use_and_reset_say_the_choice_is_shared(
    app: Any, api_client: MagicMock, tmp_path: Path
) -> None:
    api_client.list_dev_branches.return_value = [
        {"id": DEFAULT_BRANCH, "isDefault": True, "name": "Main"},
        {"id": 888, "isDefault": False, "name": "other"},
    ]
    use = _send(app, "POST", "/branches/prod/use", {"json": {"branch_id": 888}})
    reset = _send(app, "POST", "/branches/prod/reset", {})

    assert use.status_code == 200, use.text
    assert use.json()["active_branch_set"] == 888
    _shared_warning(use, tmp_path)
    assert reset.status_code == 200, reset.text
    assert reset.json()["active_branch_cleared"] == 888
    _shared_warning(reset, tmp_path)


# --------------------------------------------------------------------------- #
# V9: PATCH for the partial updates, PUT deprecated
# --------------------------------------------------------------------------- #

MR_BODY = {"title": "New title"}
SL_BODY = {"project": "prod", "new_description": "d"}
PARTIAL_UPDATES = [
    pytest.param(f"{CONFIG}/variables", {"variables": {"a": "b"}}, id="variables"),
    pytest.param("/data-apps/prod/123/secrets", {"secrets": {"#k": "v"}, "dry_run": True}, id="secrets"),
    pytest.param("/merge-requests/prod/7", MR_BODY, id="merge-request"),
    pytest.param("/semantic-layer/items/metric/revenue", SL_BODY, id="semantic-layer-item"),
]  # fmt: skip


def _mock_partial_update_services(app: Any) -> None:
    registry = app.state.registry
    for name in ("variables", "data_app", "merge_request", "semantic_layer"):
        setattr(registry, name, MagicMock())
    registry.variables.set_variables.return_value = {"ok": True}
    registry.data_app.set_data_app_secrets.return_value = {"ok": True}
    registry.merge_request.update_merge_request.return_value = {"ok": True}
    registry.semantic_layer.edit_metric.return_value = {"ok": True}


@pytest.mark.parametrize(("path", "body"), PARTIAL_UPDATES)
def test_patch_updates_without_a_warning(app: Any, path: str, body: dict[str, Any]) -> None:
    _mock_partial_update_services(app)
    res = _send(app, "PATCH", path, {"json": body})

    assert res.status_code == 200, res.text
    assert WARNINGS_HEADER not in res.headers


@pytest.mark.parametrize(("path", "body"), PARTIAL_UPDATES)
def test_put_partial_update_is_refused_naming_patch(
    app: Any, path: str, body: dict[str, Any]
) -> None:
    """1.0.0 removed the PUT form of a partial update (C3): 405, and the error names PATCH."""
    _mock_partial_update_services(app)
    res = _send(app, "PUT", path, {"json": body})

    assert res.status_code == 405, res.text
    message = res.json()["error"]["message"]
    assert "was removed in 1.0.0" in message
    assert "Use PATCH" in message
    for name in ("variables", "data_app", "merge_request", "semantic_layer"):
        assert getattr(app.state.registry, name).method_calls == []


@pytest.mark.parametrize(("path", "body"), PARTIAL_UPDATES)
def test_patch_refuses_an_undeclared_field(app: Any, path: str, body: dict[str, Any]) -> None:
    _mock_partial_update_services(app)
    res = _send(app, "PATCH", path, {"json": {**body, "no_such_field": 1}})

    assert res.status_code == 422, res.text


def test_variables_patch_merges_and_put_with_replace_replaces(app: Any) -> None:
    _mock_partial_update_services(app)
    merge = _send(app, "PATCH", f"{CONFIG}/variables", {"json": {"variables": {"a": "b"}}})
    replace = _send(
        app, "PUT", f"{CONFIG}/variables", {"json": {"variables": {"a": "b"}, "replace": True}}
    )
    refused = _send(
        app, "PATCH", f"{CONFIG}/variables", {"json": {"variables": {"a": "b"}, "replace": True}}
    )

    calls = app.state.registry.variables.set_variables.call_args_list
    assert [call.kwargs["replace"] for call in calls] == [False, True]
    assert WARNINGS_HEADER not in merge.headers
    assert WARNINGS_HEADER not in replace.headers
    assert refused.status_code == 422


def test_every_rest_form_in_the_registry_matches_the_app(app: Any) -> None:
    """A removed route is gone or keeps another form; a removed body field is not in the schema."""
    spec = app.openapi()
    paths = spec["paths"]
    rest_forms = {form: e for form, e in DEPRECATIONS.items() if form.split()[0].isupper()}
    assert len(rest_forms) == 6
    for form, entry in rest_forms.items():
        method, path, *field = form.split()
        assert entry.removed_in, form
        if field:
            ref = paths[path][method.lower()]["requestBody"]["content"]["application/json"]
            schema = spec["components"]["schemas"][ref["schema"]["$ref"].rsplit("/", 1)[1]]
            assert field[0] not in schema["properties"], form
        elif entry.replacement and entry.replacement.startswith("PATCH "):
            assert "patch" in paths[path], form
        else:
            assert path not in paths, form


def test_removed_route_names_its_replacement(app: Any) -> None:
    """`GET /branches/{project}/merge-url` was removed with `branch merge` (C3)."""
    res = _send(app, "GET", "/branches/prod/merge-url", {})

    assert res.status_code in (404, 405), res.text
    message = res.json()["error"]["message"]
    assert "was removed in 1.0.0" in message
    assert "/merge-requests" in message


def test_variables_patch_dry_run_writes_nothing(app: Any) -> None:
    _mock_partial_update_services(app)
    variables = app.state.registry.variables
    variables.preview_set_variables.return_value = {"dry_run": True}
    res = _send(
        app, "PATCH", f"{CONFIG}/variables", {"json": {"variables": {"a": "b"}, "dry_run": True}}
    )

    assert res.status_code == 200, res.text
    assert res.json() == {"dry_run": True}
    variables.set_variables.assert_not_called()
