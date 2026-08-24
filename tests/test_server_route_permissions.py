"""Tests for the app-wide route firewall on ``kbagent serve`` (issue #655).

``tests/test_server_permissions.py`` pins the SEAM #677 built -- the engine on
``app.state``, ``require_permission``, and the 403 envelope. This file pins the
COVERAGE #655 asked for: every route on the app is classified, the
classification is checked on every request, and an unclassified route is
refused rather than silently exempted.

The completeness tests are the load-bearing ones. They are what makes the
runtime fail-closed branch unreachable in a released build: a route added
without a table entry fails here long before anyone meets its 403.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, ClassVar

import pytest

if importlib.util.find_spec("fastapi") is None:  # pragma: no cover
    pytest.skip(
        "FastAPI not installed; run `uv pip install -e '.[server]'`", allow_module_level=True
    )

from fastapi.testclient import TestClient

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import PermissionPolicy
from keboola_agent_cli.permissions import OPERATION_REGISTRY, SERVE_ONLY_OPERATIONS
from keboola_agent_cli.server import create_app
from keboola_agent_cli.server.dependencies import PERMISSION_DEPENDENCY_MARKER
from keboola_agent_cli.server.route_permissions import (
    ROUTE_OPERATIONS,
    UNGUARDED_PATHS,
    resolve_route_operation,
    unknown_operations,
)

TOKEN = "route-perm-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _persist_policy(config_dir: Path, policy: PermissionPolicy) -> None:
    store = ConfigStore(config_dir=config_dir)
    config = store.load()
    config.permissions = policy
    store.save(config)


def _app(tmp_path: Path, **kwargs: Any) -> Any:
    return create_app(config_dir=str(tmp_path), auth_token=TOKEN, **kwargs)


def _client(tmp_path: Path, **kwargs: Any) -> TestClient:
    return TestClient(_app(tmp_path, **kwargs))


def _iter_api_routes(app: Any) -> list[Any]:
    """Every real ``APIRoute`` on ``app``, expanding lazy router includes.

    FastAPI 0.137 stopped flattening ``include_router`` eagerly: ``app.routes``
    holds one ``_IncludedRouter`` proxy per include (35 of them here) instead of
    the 236 routes they stand for, and nothing materialises them -- not
    ``app.openapi()``, not ``TestClient`` startup. Request handling is
    unaffected (``request.scope["route"]`` is still the real ``APIRoute``, with
    its full path template), but a test that walks ``app.routes`` naively would
    audit four routes, find nothing wrong, and pass. A coverage test that can
    pass by seeing nothing is worse than no coverage test, hence the recursion.
    """
    stack, seen, routes = list(app.routes), set(), []
    while stack:
        route = stack.pop()
        if id(route) in seen:
            continue
        seen.add(id(route))
        inner = getattr(route, "original_router", None)
        if inner is not None:
            stack.extend(inner.routes)
            continue
        if getattr(route, "methods", None):
            routes.append(route)
    return routes


def _live_routes(app: Any) -> set[tuple[str, str]]:
    """Every (method, path template) the app answers, minus HEAD/OPTIONS."""
    return {
        (method, route.path)
        for route in _iter_api_routes(app)
        for method in route.methods
        if method not in ("HEAD", "OPTIONS")
    }


def _declares_inline_guard(app: Any, method: str, path: str) -> bool:
    for route in _iter_api_routes(app):
        if route.path != path or method not in route.methods:
            continue
        for dependant in getattr(route, "dependencies", ()) or ():
            call = getattr(dependant, "dependency", None)
            if getattr(call, PERMISSION_DEPENDENCY_MARKER, None) is not None:
                return True
    return False


class TestTableCoversTheLiveApp:
    """The three-way partition: mapped, exempt, or inline-guarded. No fourth case."""

    def test_every_route_is_classified(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        unclassified = sorted(
            f"{method} {path}"
            for method, path in _live_routes(app)
            if (method, path) not in ROUTE_OPERATIONS
            and path not in UNGUARDED_PATHS
            and not _declares_inline_guard(app, method, path)
        )
        assert unclassified == [], (
            "These routes have no permission classification and would be REFUSED at "
            "runtime. Add an entry to ROUTE_OPERATIONS in server/route_permissions.py, "
            "or list the path in UNGUARDED_PATHS if it is bootstrap surface: "
            f"{unclassified}"
        )

    def test_no_stale_entries(self, tmp_path: Path) -> None:
        """A table entry for a route that no longer exists is dead weight.

        Worse than dead: it reads as coverage during a security review while
        classifying nothing at all.
        """
        live = _live_routes(_app(tmp_path))
        stale = sorted(
            f"{method} {path}" for method, path in ROUTE_OPERATIONS if (method, path) not in live
        )
        assert stale == [], f"ROUTE_OPERATIONS entries matching no live route: {stale}"

    def test_every_operation_is_a_registry_key(self) -> None:
        """A typo'd operation silently defaults to 'write' and matches no exact pattern."""
        assert unknown_operations() == []

    def test_auth_routes_are_covered_by_their_inline_guards(self, tmp_path: Path) -> None:
        """#677's three routes stay enforced without a table entry."""
        app = _app(tmp_path)
        for method, path in [
            ("GET", "/auth/projects"),
            ("GET", "/auth/status"),
            ("POST", "/auth/register-projects"),
        ]:
            assert (method, path) not in ROUTE_OPERATIONS
            assert _declares_inline_guard(app, method, path), f"{method} {path} lost its guard"


class TestResolveRouteOperation:
    def test_exempt_path_resolves_to_none(self) -> None:
        assert resolve_route_operation("GET", "/health/ping") is None

    def test_mapped_route_resolves(self) -> None:
        assert resolve_route_operation(
            "DELETE", "/configs/{project}/{component_id}/{config_id}"
        ) == ("config.delete")

    def test_method_is_part_of_the_key(self) -> None:
        """Same path, different verb, different risk -- the classic mapping bug."""
        path = "/configs/{project}/{component_id}/{config_id}"
        assert resolve_route_operation("GET", path) == "config.detail"
        assert resolve_route_operation("DELETE", path) == "config.delete"

    def test_lowercase_method_is_accepted(self) -> None:
        assert resolve_route_operation("get", "/projects") == "project.list"

    def test_unmapped_route_resolves_to_none(self) -> None:
        assert resolve_route_operation("GET", "/nope/not/a/route") is None


class TestEnforcementOnRealRoutes:
    """The behaviour #655 reported missing, on routes it named."""

    def test_destructive_route_is_denied(self, tmp_path: Path) -> None:
        _persist_policy(tmp_path, PermissionPolicy(mode="allow", deny=["cli:destructive"]))
        response = _client(tmp_path).request(
            "DELETE", "/storage/buckets/demo", headers=AUTH, json={}
        )
        assert response.status_code == 403
        body = response.json()
        assert body["error"]["code"] == "PERMISSION_DENIED"
        assert "storage.delete-bucket" in body["error"]["message"]

    def test_read_route_still_allowed_under_a_destructive_deny(self, tmp_path: Path) -> None:
        """The firewall must stay surgical -- denying deletes cannot break listing."""
        _persist_policy(tmp_path, PermissionPolicy(mode="allow", deny=["cli:destructive"]))
        response = _client(tmp_path).get("/projects", headers=AUTH)
        assert response.status_code == 200

    def test_deny_writes_flag_blocks_a_write_route(self, tmp_path: Path) -> None:
        client = _client(tmp_path, deny_writes=True)
        response = client.post("/jobs/demo/run", headers=AUTH, json={})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "PERMISSION_DENIED"

    def test_deny_writes_flag_leaves_reads_alone(self, tmp_path: Path) -> None:
        client = _client(tmp_path, deny_writes=True)
        assert client.get("/version", headers=AUTH).status_code == 200

    def test_token_delete_is_denied_by_an_exact_pattern(self, tmp_path: Path) -> None:
        """Exact-name patterns work over REST, not just category ones."""
        _persist_policy(tmp_path, PermissionPolicy(mode="allow", deny=["token.delete"]))
        client = _client(tmp_path)
        assert client.post("/token/demo/delete", headers=AUTH, json={}).status_code == 403

    def test_default_install_denies_nothing(self, tmp_path: Path) -> None:
        """No policy configured -> the surface behaves exactly as before #655."""
        client = _client(tmp_path)
        assert client.get("/projects", headers=AUTH).status_code == 200
        assert client.get("/version", headers=AUTH).status_code == 200

    def test_mode_deny_still_serves_bootstrap_paths(self, tmp_path: Path) -> None:
        """A locked-down server must still be able to say who it is.

        Denying `/health/ping` would leave a client unable to distinguish a
        policy refusal from a dead process.
        """
        _persist_policy(tmp_path, PermissionPolicy(mode="deny", allow=[]))
        client = _client(tmp_path)
        assert client.get("/health/ping").status_code == 200
        assert client.get("/health/auth-info", headers=AUTH).status_code == 200

    def test_mode_deny_blocks_an_unlisted_read(self, tmp_path: Path) -> None:
        _persist_policy(tmp_path, PermissionPolicy(mode="deny", allow=["project.list"]))
        client = _client(tmp_path)
        assert client.get("/projects", headers=AUTH).status_code == 200
        assert client.get("/storage/buckets", headers=AUTH).status_code == 403


class TestUnmappedRouteFailsClosed:
    def test_route_added_without_a_table_entry_is_refused(self, tmp_path: Path) -> None:
        app = _app(tmp_path)

        @app.get("/_unclassified")
        def _unclassified() -> dict[str, str]:  # pragma: no cover - never reached
            return {"status": "ok"}

        response = TestClient(app).get("/_unclassified", headers=AUTH)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "PERMISSION_DENIED"
        assert "ROUTE_OPERATIONS" in response.json()["error"]["message"]

    def test_inline_guarded_route_without_a_table_entry_is_allowed(self, tmp_path: Path) -> None:
        """The per-route override still works -- that is what keeps /auth/* alive."""
        from fastapi import Depends

        from keboola_agent_cli.server.dependencies import require_permission

        app = _app(tmp_path)

        @app.get("/_inline", dependencies=[Depends(require_permission("config.list"))])
        def _inline() -> dict[str, str]:
            return {"status": "ok"}

        client = TestClient(app)
        assert client.get("/_inline", headers=AUTH).status_code == 200

    def test_inline_guarded_route_is_still_subject_to_its_own_policy(self, tmp_path: Path) -> None:
        from fastapi import Depends

        from keboola_agent_cli.server.dependencies import require_permission

        _persist_policy(tmp_path, PermissionPolicy(mode="allow", deny=["config.list"]))
        app = _app(tmp_path)

        @app.get("/_inline", dependencies=[Depends(require_permission("config.list"))])
        def _inline() -> dict[str, str]:  # pragma: no cover - never reached
            return {"status": "ok"}

        assert TestClient(app).get("/_inline", headers=AUTH).status_code == 403


class TestPermissionsShowEndpoint:
    def test_reports_no_policy_when_clean(self, tmp_path: Path) -> None:
        body = _client(tmp_path).get("/permissions/show", headers=AUTH).json()
        assert body == {"active": False, "policy": None}

    def test_reports_the_persisted_policy(self, tmp_path: Path) -> None:
        _persist_policy(tmp_path, PermissionPolicy(mode="deny", allow=["cli:read"], deny=[]))
        body = _client(tmp_path).get("/permissions/show", headers=AUTH).json()
        assert body["active"] is True
        assert body["policy"] == {"mode": "deny", "allow": ["cli:read"], "deny": []}

    def test_reports_the_effective_policy_including_session_flags(self, tmp_path: Path) -> None:
        """The merged view is the point: a REST caller cannot see the daemon's flags."""
        body = (
            _client(tmp_path, deny_destructive=True).get("/permissions/show", headers=AUTH).json()
        )
        assert body["active"] is True
        assert body["policy"]["deny"] == ["cli:destructive"]

    def test_surfaces_inert_patterns(self, tmp_path: Path) -> None:
        _persist_policy(tmp_path, PermissionPolicy(mode="allow", deny=["tool:write"]))
        body = _client(tmp_path).get("/permissions/show", headers=AUTH).json()
        assert body["inert_patterns"] == ["tool:write"]
        assert body["inert_since_version"] == "0.85.0"

    def test_is_reachable_under_a_total_deny(self, tmp_path: Path) -> None:
        """Discovery must survive the policy it describes, or it is useless."""
        _persist_policy(tmp_path, PermissionPolicy(mode="deny", allow=[]))
        response = _client(tmp_path).get("/permissions/show", headers=AUTH)
        assert response.status_code == 200
        assert response.json()["active"] is True

    def test_still_requires_the_bearer_token(self, tmp_path: Path) -> None:
        assert _client(tmp_path).get("/permissions/show").status_code == 401


class TestServeOnlyOperationsAreRegistered:
    """New serve-only keys must be real registry entries AND declared serve-only."""

    @pytest.mark.parametrize("operation", ["ai.chat", "workspace.sql-improve"])
    def test_operation_is_registered_and_declared_serve_only(self, operation: str) -> None:
        assert operation in OPERATION_REGISTRY
        assert operation in SERVE_ONLY_OPERATIONS


class TestVerbAndRiskClassAgree:
    """Cheap structural checks that catch the mapping mistakes worth catching.

    Neither is a law of nature -- HTTP verbs and risk classes are different
    vocabularies -- so both carry an explicit allowlist. The point is that a
    disagreement has to be *chosen*: a new DELETE quietly classified ``read``
    would be a firewall hole nothing else notices.
    """

    # POST because the request needs a request BODY, not because it mutates.
    _NON_MUTATING_POSTS: ClassVar[set[str]] = {
        "POST /data-apps/validate-repo",
        "POST /documentation/query",
        "POST /flows/validate",
        "POST /kai/ask",
        "POST /lineage/build",
        "POST /lineage/show",
        "POST /semantic-layer/diff",
        "POST /workspaces/{project}/{workspace_id}/password",
    }
    # `branch merge` only ever produces a URL (the merge happens in the web
    # UI), so mirroring its `write` class on a GET is CLI parity, not a slip.
    _NON_READ_GETS: ClassVar[set[str]] = {"GET /branches/{project}/merge-url"}

    def test_every_delete_route_is_destructive_or_admin(self) -> None:
        offenders = sorted(
            f"{method} {path} -> {op} ({OPERATION_REGISTRY[op]})"
            for (method, path), op in ROUTE_OPERATIONS.items()
            if method == "DELETE" and OPERATION_REGISTRY[op] not in ("destructive", "admin")
        )
        assert offenders == [], (
            "A DELETE route classified below `destructive` slips through "
            f"--deny-destructive: {offenders}"
        )

    def test_mutating_verbs_are_not_classified_read(self) -> None:
        offenders = sorted(
            f"{method} {path}"
            for (method, path), op in ROUTE_OPERATIONS.items()
            if method in ("POST", "PUT", "PATCH") and OPERATION_REGISTRY[op] == "read"
        )
        assert set(offenders) <= self._NON_MUTATING_POSTS, (
            "New mutating route classified `read` -- it would survive "
            f"--deny-writes: {sorted(set(offenders) - self._NON_MUTATING_POSTS)}"
        )

    def test_get_routes_are_reads(self) -> None:
        offenders = sorted(
            f"{method} {path}"
            for (method, path), op in ROUTE_OPERATIONS.items()
            if method == "GET" and OPERATION_REGISTRY[op] != "read"
        )
        assert set(offenders) <= self._NON_READ_GETS, (
            "A GET classified above `read` is blocked by --deny-writes; make sure "
            f"that is intended: {sorted(set(offenders) - self._NON_READ_GETS)}"
        )
