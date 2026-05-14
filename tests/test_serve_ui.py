"""Tests for ``kbagent serve --ui`` single-process UI mode.

Covers the three pieces glued in by ``server._install_ui``:

1. ``GET /`` and ``GET /index.html`` serve the SPA shell *unauthenticated*
   and set a ``kbagent_session`` HttpOnly cookie. The browser then attaches
   it to every same-origin request automatically. The token never enters
   the JS heap, the URL, or uvicorn's access log.
2. ``/api/<path>`` is path-rewritten to ``/<path>`` so the SPA's existing
   ``/api/projects`` calls reach the bare ``projects`` router.
3. The auth middleware accepts the ``kbagent_session`` cookie as a Bearer
   fallback so ``EventSource`` (which cannot send custom headers) still
   authenticates -- via cookie, not via URL query param.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from keboola_agent_cli.server import create_app
from keboola_agent_cli.server.auth import PUBLIC_PATHS  # noqa: F401  (sanity import)


@pytest.fixture
def ui_dist(tmp_path: Path) -> Path:
    """Minimal SPA dist with index.html + an asset, mimicking `npm run build`."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        "<!doctype html>\n<html><head><title>kbagent</title></head>"
        '<body><script src="/assets/main.js"></script></body></html>',
        encoding="utf-8",
    )
    assets = dist / "assets"
    assets.mkdir()
    (assets / "main.js").write_text("console.log('hi')", encoding="utf-8")
    return dist


def _make_client(
    tmp_path: Path, ui_dist: Path | None = None, token: str = "test-bearer-xyz"
) -> TestClient:
    app = create_app(
        config_dir=str(tmp_path / "kbagent-config"),
        auth_token=token,
        ui_dist=str(ui_dist) if ui_dist else None,
    )
    # raise_server_exceptions=False so internal 500s come back as responses
    # instead of bubbling out and masking the assertion message.
    return TestClient(app, raise_server_exceptions=False)


class TestUiBootstrap:
    def test_index_served_unauthenticated_with_session_cookie(
        self, tmp_path: Path, ui_dist: Path
    ) -> None:
        client = _make_client(tmp_path, ui_dist=ui_dist, token="my-secret-123")
        # No Authorization header -> still 200, because index.html is the
        # bootstrap surface. Auth materializes via the Set-Cookie response.
        resp = client.get("/")
        assert resp.status_code == 200
        body = resp.text
        # Token NOT injected into the page body anymore -- it lives only in
        # the HttpOnly cookie. This is the key security improvement: the
        # token cannot be exfiltrated via XSS (JS can't read HttpOnly).
        assert "my-secret-123" not in body
        assert "__KBAGENT_TOKEN" not in body
        # Cookie shape: HttpOnly + SameSite=Strict + Path=/ (Secure flag
        # intentionally absent so http://127.0.0.1 dev installs work).
        set_cookie = resp.headers.get("set-cookie", "")
        assert "kbagent_session=my-secret-123" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=strict" in set_cookie.replace("Strict", "strict").replace(
            "STRICT", "strict"
        )
        assert "Path=/" in set_cookie
        # Same behavior on /index.html.
        resp2 = client.get("/index.html")
        assert resp2.status_code == 200
        assert "kbagent_session=my-secret-123" in resp2.headers.get("set-cookie", "")

    def test_assets_served_unauthenticated(self, tmp_path: Path, ui_dist: Path) -> None:
        client = _make_client(tmp_path, ui_dist=ui_dist)
        resp = client.get("/assets/main.js")
        assert resp.status_code == 200
        assert "console.log" in resp.text


class TestApiAlias:
    def test_api_prefix_routes_to_bare_endpoint(self, tmp_path: Path, ui_dist: Path) -> None:
        client = _make_client(tmp_path, ui_dist=ui_dist, token="t")
        # /health/ping is in PUBLIC_PATHS; both /health/ping and
        # /api/health/ping should hit it.
        bare = client.get("/health/ping")
        aliased = client.get("/api/health/ping")
        assert bare.status_code == 200
        assert aliased.status_code == 200
        assert bare.json() == aliased.json()

    def test_api_alias_disabled_without_ui(self, tmp_path: Path) -> None:
        # Without --ui, /api/* should NOT be rewritten -- it 404s like any
        # unknown path. Critical contract: API-only deployments (BFF
        # upstream) keep /api/* free for the BFF to own.
        client = _make_client(tmp_path, ui_dist=None)
        resp = client.get(
            "/api/health/ping",
            headers={"authorization": "Bearer t"},
        )
        # Either 404 (no route) or 401 (auth before 404). Both prove the
        # rewrite did NOT happen -- a successful 200 would be a regression.
        assert resp.status_code in {404, 401}


class TestCookieAuth:
    """Cookie path: ``GET /`` sets the cookie, subsequent requests use it.

    The auth middleware accepts ``kbagent_session=<token>`` as a Bearer
    fallback iff no Authorization header was sent. This is what makes
    ``EventSource`` work without smuggling the token through a query
    param (which would land in uvicorn's access log).
    """

    def test_cookie_authenticates_after_bootstrap(self, tmp_path: Path, ui_dist: Path) -> None:
        client = _make_client(tmp_path, ui_dist=ui_dist, token="ck-token")
        # Without auth -> 401.
        assert client.get("/agents").status_code == 401
        # Bootstrap GET / sets the cookie on the TestClient session jar.
        bootstrap = client.get("/")
        assert bootstrap.status_code == 200
        assert "kbagent_session=ck-token" in bootstrap.headers.get("set-cookie", "")
        # Subsequent request reuses the cookie automatically.
        resp = client.get("/agents")
        assert resp.status_code == 200

    def test_invalid_cookie_rejected(self, tmp_path: Path, ui_dist: Path) -> None:
        client = _make_client(tmp_path, ui_dist=ui_dist, token="real-token")
        client.cookies.set("kbagent_session", "wrong-token")
        assert client.get("/agents").status_code == 401

    def test_query_param_no_longer_accepted(self, tmp_path: Path, ui_dist: Path) -> None:
        # The legacy ``?_kbagent_token=...`` fallback was removed in favor
        # of the cookie path -- it was the only reason the bearer token
        # ever appeared in URLs / access logs. Verify a request that would
        # have authenticated under the old design is now rejected.
        client = _make_client(tmp_path, ui_dist=ui_dist, token="t")
        resp = client.get("/agents?_kbagent_token=t")
        assert resp.status_code == 401

    def test_header_takes_precedence_when_both_present(self, tmp_path: Path, ui_dist: Path) -> None:
        # When the header IS present, the cookie fallback is not even
        # consulted -- so a wrong cookie value next to a right header still
        # passes. Asserts the branches don't interfere.
        client = _make_client(tmp_path, ui_dist=ui_dist, token="t")
        client.cookies.set("kbagent_session", "wrong-cookie")
        resp = client.get(
            "/agents",
            headers={"authorization": "Bearer t"},
        )
        assert resp.status_code == 200


class TestUiOptional:
    def test_no_ui_path_no_ui_routes(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path, ui_dist=None, token="t")
        # / has no handler when --ui is not enabled; it returns the
        # framework's 404 envelope (FastAPI default).
        resp = client.get("/", headers={"authorization": "Bearer t"})
        assert resp.status_code == 404

    def test_missing_dist_skips_mount_silently(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Pointing --ui at a path that doesn't have index.html should log a
        # warning and skip the mount, not crash. (CLI --ui flag separately
        # asserts the path is real and exits non-zero before reaching
        # create_app; this test covers programmatic callers.)
        bogus = tmp_path / "no-build-here"
        bogus.mkdir()
        with caplog.at_level("WARNING"):
            app = create_app(
                config_dir=str(tmp_path / "cfg"),
                auth_token="t",
                ui_dist=str(bogus),
            )
        client = TestClient(app, raise_server_exceptions=False)
        # Mount was skipped -> / 404s as in API-only mode.
        resp = client.get("/", headers={"authorization": "Bearer t"})
        assert resp.status_code == 404
        assert any("missing index.html" in rec.message for rec in caplog.records)
