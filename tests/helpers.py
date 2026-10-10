"""Shared test helper functions for Keboola Agent CLI tests.

Contains factory functions for creating mock clients and pre-configured
ConfigStore instances. Used across multiple test files to avoid duplication.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import parse_qsl, urlencode

import httpx

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.models import ProjectConfig, TokenVerifyResponse


def metastore_scope_available(url: str, token: str) -> bool:
    """Probe whether a project has a usable metastore scope (E2E preflight).

    "Failed to create project scope" is the metastore's blanket answer when it
    cannot build a project scope for the caller -- most commonly its
    master-token gate rejecting a valid non-master token with a 401 (issue
    #711; kbagent reclassifies that to ``MISSING_MASTER_TOKEN``), historically
    also seen as a 502 on some deployments. Either way it is an environment
    limitation, not a test failure. The semantic-layer E2E suites call this
    and ``pytest.skip()`` when it returns False, so they skip cleanly instead
    of reporting a wall of false-positive failures.
    """
    from keboola_agent_cli.metastore_client import SEMANTIC_TYPES, MetastoreClient

    try:
        with MetastoreClient(stack_url=url, token=token) as mc:
            mc.list_items(SEMANTIC_TYPES[0])  # ty: ignore[invalid-argument-type]  # probe; str vs SemanticType Literal
        return True
    except KeboolaApiError as exc:
        # Skip cleanly when the scope is genuinely unavailable (502 / "scope")
        # OR the metastore host is simply unreachable (network / DNS -- e.g. a
        # malformed or accidentally doubled stack URL). Both mean "no usable
        # metastore here"; raising would turn one preflight failure into a wall
        # of errors across every dependent test.
        msg = (exc.message or "").lower()
        if exc.status_code == 502 or "scope" in msg or "cannot connect" in msg:
            return False
        raise


def make_mock_client(
    project_name: str = "Test Project",
    project_id: int = 1234,
    token_description: str = "My Token",
    org_id: int | None = None,
    org_name: str | None = None,
) -> MagicMock:
    """Create a mock KeboolaClient that returns a successful verify_token response.

    Used by test_cli.py, test_services.py, and other test files that need
    a mock client with a working verify_token.
    """
    mock_client = MagicMock()
    mock_client.verify_token.return_value = TokenVerifyResponse(
        token_id="12345",
        token_description=token_description,
        project_id=project_id,
        project_name=project_name,
        owner_name=project_name,
        org_id=org_id,
        org_name=org_name,
    )
    return mock_client


def make_failing_client(error: KeboolaApiError) -> MagicMock:
    """Create a mock KeboolaClient whose verify_token raises the given error."""
    mock_client = MagicMock()
    mock_client.verify_token.side_effect = error
    return mock_client


def setup_single_project(
    tmp_config_dir: Path,
    alias: str = "prod",
    stack_url: str = "https://connection.keboola.com",
    token: str = "901-xxx",
    project_name: str = "Production",
    project_id: int = 258,
) -> ConfigStore:
    """Create a ConfigStore with a single project configured.

    Used by test_base_service.py, test_lineage_service.py, and other test files
    that need a pre-configured ConfigStore with one project.
    """
    store = ConfigStore(config_dir=tmp_config_dir)
    store.add_project(
        alias,
        ProjectConfig(
            stack_url=stack_url,
            token=token,
            project_name=project_name,
            project_id=project_id,
        ),
    )
    return store


def setup_two_projects(tmp_config_dir: Path) -> ConfigStore:
    """Create a ConfigStore with two projects (prod and dev) configured.

    Used by test_base_service.py, test_lineage_service.py, and other test files
    that need a pre-configured ConfigStore with two projects.
    """
    store = ConfigStore(config_dir=tmp_config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-xxx",
            project_name="Production",
            project_id=258,
        ),
    )
    store.add_project(
        "dev",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="7012-yyy",
            project_name="Development",
            project_id=7012,
        ),
    )
    return store


# ---------------------------------------------------------------------------
# API call-count recording (issue #802)
# ---------------------------------------------------------------------------

# One recorded HTTP call: (METHOD, path) -- host stripped, query optional --
# plus the X-StorageApi-Token value when the caller asks for it.
ApiCall = tuple[str, ...]

# Status returned for a request no route matches. Deliberately NOT a
# retryable status (429/5xx): an unmatched call must fail fast, never sleep
# through the client's backoff.
UNMATCHED_ROUTE_STATUS = 418


def _sorted_query(target: str) -> str:
    """Rebuild ``path?query`` with the query parsed and its pairs sorted.

    Parameter order and percent-encoding then do not matter: two targets are
    equal only when they carry the same parameters with the same values.
    """
    path, _, query = target.partition("?")
    if not query:
        return path
    pairs = sorted(parse_qsl(query, keep_blank_values=True))
    return f"{path}?{urlencode(pairs, safe='[],')}"


def _call_of(
    request: httpx.Request, *, include_query: bool, include_token: bool = False
) -> ApiCall:
    path = request.url.path
    if include_query and request.url.query:
        path = _sorted_query(f"{path}?{request.url.query.decode()}")
    if include_token:
        return (request.method, path, request.headers.get("X-StorageApi-Token", ""))
    return (request.method, path)


def mock_api_routes(httpx_mock: Any, routes: Mapping[ApiCall, Any]) -> None:
    """Answer every HTTP request from a ``{(METHOD, path): json_body}`` table.

    Routing ignores host and query string, so one table serves the Storage
    and Queue hosts alike. The callback is reusable and optional: the same
    route may be hit any number of times (that count is what the call-count
    tests assert), and a command that makes zero calls does not trip
    pytest-httpx's "response never requested" teardown check. A request no
    route matches gets HTTP ``UNMATCHED_ROUTE_STATUS`` so it surfaces as a
    command error and as an unexpected entry in the recorded calls.
    """

    def _respond(request: httpx.Request) -> httpx.Response:
        key = _call_of(request, include_query=False)
        if key not in routes:
            return httpx.Response(
                UNMATCHED_ROUTE_STATUS,
                json={"error": f"no mocked route for {key[0]} {key[1]}"},
            )
        return httpx.Response(200, json=routes[key])

    httpx_mock.add_callback(_respond, is_reusable=True, is_optional=True)


def recorded_api_calls(
    httpx_mock: Any, *, include_query: bool = False, include_token: bool = False
) -> list[ApiCall]:
    """Every HTTP request the mock saw, as ``(METHOD, path[, token])`` in send order."""
    return [
        _call_of(r, include_query=include_query, include_token=include_token)
        for r in httpx_mock.get_requests()
    ]


def assert_api_calls(
    httpx_mock: Any,
    expected: Sequence[ApiCall],
    *,
    include_query: bool = False,
    include_token: bool = False,
    ordered: bool = True,
) -> None:
    """Assert the exact number AND sequence of HTTP calls a command made.

    ``include_query=True`` compares the query too, parsed and sorted on both
    sides, so the expected literal may list parameters in any order.
    ``include_token=True`` adds each call's ``X-StorageApi-Token`` as a third
    element -- use it in multi-project tests, where the path alone cannot show
    which project a call was for. ``ordered=False`` compares sorted multisets
    -- use it where calls are issued from a thread pool (multi-project
    fan-out) and the send order is not deterministic. The count is exact
    either way: a duplicate call fails.
    """
    actual = recorded_api_calls(
        httpx_mock, include_query=include_query, include_token=include_token
    )
    if include_query:
        expected = [(call[0], _sorted_query(call[1]), *call[2:]) for call in expected]
    if not ordered:
        actual, expected = sorted(actual), sorted(expected)
    assert actual == list(expected), (
        f"API calls changed ({len(actual)} made, {len(expected)} expected).\n"
        f"actual:   {actual}\nexpected: {list(expected)}"
    )
