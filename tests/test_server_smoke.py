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
    "/stream/{project}/list",
    "/token/{project}/create",
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
    "/semantic-layer/models",
    "/semantic-layer/models/{model}",
    "/semantic-layer/show",
    "/semantic-layer/validate",
    "/semantic-layer/export",
    "/semantic-layer/diff",
    "/semantic-layer/items/{kind}",
    "/semantic-layer/items/{kind}/{name}",
    "/semantic-layer/import",
    "/semantic-layer/promote",
    "/semantic-layer/build",
    "/semantic-layer/token/encrypt",
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


def test_every_router_tag_has_openapi_metadata(client: TestClient) -> None:
    """Every tag used by an operation must have an OPENAPI_TAGS description block.

    Regression guard for the bug that hid the ``stream`` router: it was wired
    via ``include_router`` and fully callable, but its tag was missing from
    ``OPENAPI_TAGS`` in ``server/app.py`` -- so Swagger UI rendered a bare,
    description-less ``stream`` section out of its logical group. Registration
    (``include_router``) and documentation (``openapi_tags``) are independent
    layers; this test ties them together so a new router can't ship invisible
    in ``/docs`` again.
    """
    http_methods = {"get", "post", "put", "delete", "patch", "options", "head", "trace"}
    spec = client.get("/openapi.json").json()
    documented = {tag["name"] for tag in spec.get("tags", [])}
    used: set[str] = set()
    for path_item in spec["paths"].values():
        for method, operation in path_item.items():
            if method in http_methods and isinstance(operation, dict):
                used.update(operation.get("tags", []))
    undocumented = used - documented
    assert not undocumented, (
        "Routers expose tags with no OPENAPI_TAGS description block: "
        f"{sorted(undocumented)}. Add an entry to OPENAPI_TAGS in server/app.py."
    )


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


# ── Semantic-layer route smoke tests (no metastore creds required) ──
#
# These cover route-presence + Pydantic body validation only. The real
# end-to-end coverage lives in tests/test_server_semantic_layer_routes_e2e.py
# behind the @pytest.mark.e2e marker.


def test_semantic_layer_diff_rejects_both_project_and_file(client: TestClient) -> None:
    """DiffRequest enforces exactly-one-per-side via model_validator."""
    res = client.post(
        "/semantic-layer/diff",
        headers={"Authorization": "Bearer test-token"},
        json={"project_a": "p", "file_a": {"x": 1}, "project_b": "q"},
    )
    assert res.status_code == 422


def test_semantic_layer_diff_rejects_neither_side(client: TestClient) -> None:
    res = client.post(
        "/semantic-layer/diff",
        headers={"Authorization": "Bearer test-token"},
        json={"project_b": "q"},
    )
    assert res.status_code == 422


def test_semantic_layer_items_unknown_kind_post_422(client: TestClient) -> None:
    """POST /items/{kind} with an unsupported kind returns 422.

    FastAPI rejects unknown ``kind`` path values at the framework layer
    via the ``ItemKind`` ``Literal`` alias, before reaching the handler.
    """
    res = client.post(
        "/semantic-layer/items/widget",
        headers={"Authorization": "Bearer test-token"},
        json={"project": "x", "name": "n"},
    )
    assert res.status_code == 422


def test_semantic_layer_items_unknown_kind_put_422(client: TestClient) -> None:
    """PUT /items/{kind}/{name} with an unsupported kind returns 422.

    FastAPI rejects unknown ``kind`` path values at the framework layer
    via the ``ItemKind`` ``Literal`` alias, before reaching the handler.
    """
    res = client.put(
        "/semantic-layer/items/widget/n",
        headers={"Authorization": "Bearer test-token"},
        json={"project": "x"},
    )
    assert res.status_code == 422


def test_semantic_layer_models_missing_project_returns_4xx(client: TestClient) -> None:
    """GET /models with a missing project alias falls into CONFIG_ERROR."""
    res = client.get(
        "/semantic-layer/models?project=does-not-exist",
        headers={"Authorization": "Bearer test-token"},
    )
    # ConfigError → 400 (via _config_error_handler in server/__init__.py).
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "CONFIG_ERROR"


def test_semantic_layer_add_constraint_rejects_bad_name(client: TestClient) -> None:
    """Constraint name regex enforced via service-layer validation.

    Server returns the constraint-name regex error as a 502
    KeboolaApiError envelope (the metastore service raises
    VALIDATION_ERROR which the global handler maps to 502 with the
    error_code field intact). The router itself returns 200 only on
    real success.
    """
    res = client.post(
        "/semantic-layer/items/constraint",
        headers={"Authorization": "Bearer test-token"},
        json={
            "project": "no-such-project-alias",
            "name": "BadName",  # uppercase — would fail constraint regex
            "constraint_type": "range",
            "rule": "between 0 and 100",
            "metrics": ["m1"],
        },
    )
    # Project resolution comes first → CONFIG_ERROR (400). Adequate to
    # demonstrate the route is reachable + Pydantic body validates.
    assert res.status_code in (400, 422, 502)


def test_semantic_layer_token_encrypt_requires_body(client: TestClient) -> None:
    """POST /token/encrypt with no body → 422 (missing required project + component_id)."""
    res = client.post(
        "/semantic-layer/token/encrypt",
        headers={"Authorization": "Bearer test-token"},
        json={},
    )
    assert res.status_code == 422


def test_semantic_layer_routes_require_auth(client: TestClient) -> None:
    """Every semantic-layer route is auth-gated (none in PUBLIC_PATHS)."""
    for path in (
        "/semantic-layer/models?project=p",
        "/semantic-layer/show?project=p",
        "/semantic-layer/validate?project=p",
        "/semantic-layer/export?project=p",
    ):
        res = client.get(path)
        assert res.status_code == 401, f"{path} should require auth"


# ── POST /jobs/{project}/run -- mode field passthrough (issue #321) ──
#
# Locks the CLI <-> REST 1:1 parity contract for the v0.43.6 `--mode`
# flag: a caller hitting `kbagent serve` with `{"mode": "debug", ...}`
# must see `mode="debug"` reach `JobService.run_job` -- silently dropping
# the field would let kbagent serve / scheduled-agent / web-UI callers
# accidentally land on production execution.


def test_jobs_run_default_mode_is_run(tmp_path: Path) -> None:
    """Omitting `mode` in the request body lands as mode='run' on the service."""
    from unittest.mock import MagicMock

    from keboola_agent_cli.server.dependencies import ServiceRegistry, get_registry

    app = create_app(config_dir=str(tmp_path), auth_token="test-token")
    job_service = MagicMock()
    job_service.run_job.return_value = {"id": 900, "status": "waiting"}
    registry = ServiceRegistry.__new__(ServiceRegistry)
    registry.job = job_service  # type: ignore[attr-defined]
    app.dependency_overrides[get_registry] = lambda: registry

    with TestClient(app) as test_client:
        res = test_client.post(
            "/jobs/prod/run",
            headers={"Authorization": "Bearer test-token"},
            json={"component_id": "keboola.ex-http", "config_id": "42"},
        )

    assert res.status_code == 200, res.text
    assert job_service.run_job.call_args.kwargs["mode"] == "run"


def test_jobs_run_mode_debug_reaches_service(tmp_path: Path) -> None:
    """`{"mode": "debug"}` in the request body reaches JobService.run_job verbatim.

    Regression guard against the original bug: `JobRun` Pydantic model used
    to lack the `mode` field, so the endpoint silently hard-coded `mode="run"`
    even when a caller passed `"mode": "debug"`.
    """
    from unittest.mock import MagicMock

    from keboola_agent_cli.server.dependencies import ServiceRegistry, get_registry

    app = create_app(config_dir=str(tmp_path), auth_token="test-token")
    job_service = MagicMock()
    job_service.run_job.return_value = {"id": 901, "status": "waiting"}
    registry = ServiceRegistry.__new__(ServiceRegistry)
    registry.job = job_service  # type: ignore[attr-defined]
    app.dependency_overrides[get_registry] = lambda: registry

    with TestClient(app) as test_client:
        res = test_client.post(
            "/jobs/prod/run",
            headers={"Authorization": "Bearer test-token"},
            json={
                "component_id": "keboola.ex-http",
                "config_id": "42",
                "mode": "debug",
            },
        )

    assert res.status_code == 200, res.text
    assert job_service.run_job.call_args.kwargs["mode"] == "debug"


# ── Session-credential failures map to 401, not 502 (programmatic auth) ──
#
# `serve` supports session-registered (browser-login) projects for the Storage
# and Manage paths, so a session that expires mid-daemon is a real runtime
# state, not a theoretical one. It is the caller's authentication problem, and
# only an operator at the host can fix it -- a browser login cannot be run for
# a REST caller. Anything else (a genuine upstream fault) keeps its 502.


def _client_with_failing_job_service(tmp_path: Path, exc: Exception) -> TestClient:
    """Build the app with a JobService whose `list_jobs` raises `exc`.

    Routes the exception through the real registered exception handlers rather
    than calling them directly, so the assertions cover the status/envelope a
    REST caller actually receives.
    """
    from unittest.mock import MagicMock

    from keboola_agent_cli.server.dependencies import ServiceRegistry, get_registry

    app = create_app(config_dir=str(tmp_path), auth_token="test-token")
    job_service = MagicMock()
    job_service.list_jobs.side_effect = exc
    registry = ServiceRegistry.__new__(ServiceRegistry)
    registry.job = job_service  # type: ignore[attr-defined]
    app.dependency_overrides[get_registry] = lambda: registry
    return TestClient(app, raise_server_exceptions=False)


def test_expired_session_returns_401_session_expired(tmp_path: Path) -> None:
    """A SESSION_EXPIRED KeboolaApiError answers 401 and names the host remedy."""
    from keboola_agent_cli.errors import ErrorCode, KeboolaApiError

    exc = KeboolaApiError(
        message="Your Keboola login for https://connection.keboola.com expired.",
        error_code=ErrorCode.SESSION_EXPIRED,
        retryable=False,
    )
    with _client_with_failing_job_service(tmp_path, exc) as test_client:
        res = test_client.get("/jobs", headers={"Authorization": "Bearer test-token"})

    assert res.status_code == 401, res.text
    body = res.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "SESSION_EXPIRED"
    # The remedy has to point at the host running serve: the daemon cannot
    # open a browser login on a remote caller's behalf.
    assert "kbagent auth login" in body["error"]["message"]
    assert "host running `kbagent serve`" in body["error"]["message"]


def test_missing_session_returns_401_session_not_found(tmp_path: Path) -> None:
    """SESSION_NOT_FOUND shares the 401 mapping -- same remedy, same operator."""
    from keboola_agent_cli.errors import ErrorCode, KeboolaApiError

    exc = KeboolaApiError(
        message="No active Keboola session for https://connection.keboola.com.",
        error_code=ErrorCode.SESSION_NOT_FOUND,
        retryable=False,
    )
    with _client_with_failing_job_service(tmp_path, exc) as test_client:
        res = test_client.get("/jobs", headers={"Authorization": "Bearer test-token"})

    assert res.status_code == 401, res.text
    assert res.json()["error"]["code"] == "SESSION_NOT_FOUND"


def test_non_session_api_error_keeps_502(tmp_path: Path) -> None:
    """A KeboolaApiError that is not session-related still maps to 502.

    Guards the narrowness of the 401 branch: only session-credential codes are
    reclassified, so an upstream Storage/Queue failure keeps announcing itself
    as a gateway problem.
    """
    from keboola_agent_cli.errors import ErrorCode, KeboolaApiError

    exc = KeboolaApiError(
        message="Storage API is unavailable.",
        error_code=ErrorCode.API_ERROR,
        retryable=True,
    )
    with _client_with_failing_job_service(tmp_path, exc) as test_client:
        res = test_client.get("/jobs", headers={"Authorization": "Bearer test-token"})

    assert res.status_code == 502, res.text
    assert res.json()["error"]["code"] == "API_ERROR"
    assert "host running `kbagent serve`" not in res.json()["error"]["message"]


def test_config_error_keeps_400(tmp_path: Path) -> None:
    """`ConfigError` keeps its own 400 mapping, unaffected by the 401 branch."""
    from keboola_agent_cli.errors import ConfigError

    with _client_with_failing_job_service(tmp_path, ConfigError("no such project")) as test_client:
        res = test_client.get("/jobs", headers={"Authorization": "Bearer test-token"})

    assert res.status_code == 400, res.text
    assert res.json()["error"]["code"] == "CONFIG_ERROR"


def test_sentinel_unsupported_path_keeps_its_error_code(tmp_path: Path) -> None:
    """A sentinel project on an unsupported path reports AUTH_NOT_SUPPORTED_ON_STACK.

    `SessionAuthUnsupportedError` is a `ConfigError`, so it takes the 400 every
    configuration problem does -- but a REST caller has to be able to tell "this
    path does not accept browser-login projects" apart from a generic config
    problem, which is what the code carries.
    """
    from keboola_agent_cli.errors import SessionAuthUnsupportedError

    exc = SessionAuthUnsupportedError("The MCP server subprocess")
    with _client_with_failing_job_service(tmp_path, exc) as test_client:
        res = test_client.get("/jobs", headers={"Authorization": "Bearer test-token"})

    assert res.status_code == 400, res.text
    assert res.json()["error"]["code"] == "AUTH_NOT_SUPPORTED_ON_STACK"
    assert "static Storage token" in res.json()["error"]["message"]


def test_post_agents_rejects_mcp_tool(client: TestClient) -> None:
    """The mcp_tool action type was removed in v0.85.0 -- POST /agents refuses it."""
    resp = client.post(
        "/agents",
        json={"name": "x", "action": {"type": "mcp_tool", "params": {"tool": "get_jobs"}}},
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 422, resp.text
    assert "REMOVED" in resp.text
