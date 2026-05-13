"""Tests for ``kbagent serve --ui`` single-process UI mode.

Covers the three pieces glued in by ``server._install_ui``:

1. ``GET /`` and ``GET /index.html`` serve the SPA shell with the bearer
   token injected as ``window.__KBAGENT_TOKEN`` -- and they do so
   *unauthenticated* (otherwise the browser couldn't bootstrap).
2. ``/api/<path>`` is path-rewritten to ``/<path>`` so the SPA's existing
   ``/api/projects`` calls reach the bare ``projects`` router.
3. The auth middleware accepts ``?_kbagent_token=...`` as a fallback so
   ``EventSource`` (which can't send headers) still authenticates.
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
    def test_index_served_unauthenticated_with_token_injected(
        self, tmp_path: Path, ui_dist: Path
    ) -> None:
        client = _make_client(tmp_path, ui_dist=ui_dist, token="my-secret-123")
        # No Authorization header -> still 200, because index.html is the
        # bootstrap surface that LATER carries the token.
        resp = client.get("/")
        assert resp.status_code == 200
        body = resp.text
        # Token injected into a <script> tag before </head>.
        assert 'window.__KBAGENT_TOKEN = "my-secret-123"' in body
        assert "kbagent-token-inject" in body
        assert "</head>" in body  # not corrupted
        # And /index.html is the same.
        resp2 = client.get("/index.html")
        assert resp2.status_code == 200
        assert 'window.__KBAGENT_TOKEN = "my-secret-123"' in resp2.text

    def test_assets_served_unauthenticated(self, tmp_path: Path, ui_dist: Path) -> None:
        client = _make_client(tmp_path, ui_dist=ui_dist)
        resp = client.get("/assets/main.js")
        assert resp.status_code == 200
        assert "console.log" in resp.text

    def test_token_html_special_chars_escaped(self, tmp_path: Path, ui_dist: Path) -> None:
        # Tokens are urlsafe base64, so '<' / '>' don't appear in practice,
        # but the escape function should defend against an injected one
        # anyway. ``"`` would break the literal; verify it doesn't get through.
        client = _make_client(tmp_path, ui_dist=ui_dist, token='ab"cd<ef>')
        body = client.get("/").text
        assert 'ab"cd<ef>' not in body  # raw form is gone
        assert 'ab\\"cd\\u003cef\\u003e' in body  # escaped form is present


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


class TestEventSourceQueryFallback:
    def test_query_param_token_authenticates(self, tmp_path: Path, ui_dist: Path) -> None:
        client = _make_client(tmp_path, ui_dist=ui_dist, token="qt-token")
        # /docs is public, so use a non-public route to exercise auth.
        # /agents requires auth and works without external state if the
        # store is empty; pick GET /agents which lists tasks (returns []).
        # Without auth -> 401.
        resp_no_auth = client.get("/agents")
        assert resp_no_auth.status_code == 401
        # With ?_kbagent_token=... -> 200, even without Authorization header.
        resp_qs = client.get("/agents?_kbagent_token=qt-token")
        assert resp_qs.status_code == 200

    def test_query_param_invalid_still_rejected(self, tmp_path: Path, ui_dist: Path) -> None:
        client = _make_client(tmp_path, ui_dist=ui_dist, token="qt-token")
        resp = client.get("/agents?_kbagent_token=wrong")
        assert resp.status_code == 401

    def test_header_takes_precedence_when_both_present(self, tmp_path: Path, ui_dist: Path) -> None:
        # When the header IS present, the query param fallback is not even
        # consulted -- so a wrong query value next to a right header still
        # passes (the inverse would also pass; we just want to prove the
        # branches don't interfere).
        client = _make_client(tmp_path, ui_dist=ui_dist, token="t")
        resp = client.get(
            "/agents?_kbagent_token=wrong-but-ignored",
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
