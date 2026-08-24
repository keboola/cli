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


class TestUiShellCaching:
    """The SPA shell must never be trusted to a browser cache without revalidation.

    Live-observed during the PR #665 UI audit: after a ``kbagent serve --ui``
    restart (new bearer token), a browser reload served the *cached*
    ``index.html`` without contacting the server. No request means no fresh
    ``Set-Cookie``, so the stale session cookie 401'd every ``/api/*`` call
    and the SPA silently rendered empty lists. ``Cache-Control: no-cache``
    forces the browser to revalidate the shell on every load -- and because
    the bootstrap route always answers a full 200 (it implements no
    conditional-request handling), every revalidation re-sets the cookie.
    """

    def test_shell_responses_always_revalidate(self, tmp_path: Path, ui_dist: Path) -> None:
        client = _make_client(tmp_path, ui_dist=ui_dist)
        for path in ("/", "/index.html"):
            resp = client.get(path)
            assert resp.status_code == 200, path
            assert resp.headers.get("cache-control") == "no-cache", (
                f"GET {path} must answer 'Cache-Control: no-cache' so a browser "
                "revalidates the shell (and picks up a fresh session cookie) "
                "after a server restart"
            )

    def test_assets_keep_default_caching(self, tmp_path: Path, ui_dist: Path) -> None:
        # Build assets are content-hashed by Vite (a new build means new
        # URLs), so the no-cache stamp is scoped to the shell only.
        client = _make_client(tmp_path, ui_dist=ui_dist)
        resp = client.get("/assets/main.js")
        assert resp.status_code == 200
        assert resp.headers.get("cache-control") != "no-cache"


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

    def test_dev_portal_read_requires_auth_in_ui_mode(self, tmp_path: Path, ui_dist: Path) -> None:
        # Regression for the auth-bypass gap: a GET to an API route that is
        # NOT in `_is_ui_public`'s allow-list would be mistaken for an SPA
        # route and served without bearer validation. `/dev-portal/*` must
        # be treated as API (401 without auth), not as a public SPA path.
        client = _make_client(tmp_path, ui_dist=ui_dist, token="t")
        resp = client.get("/dev-portal/apps?vendor=keboola")
        assert resp.status_code == 401, (
            f"GET /dev-portal/apps must require auth, got {resp.status_code}: {resp.text}"
        )

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


class TestUiAuthRouteAwareBypass:
    """GHSA-ffpq-prmh-3gx2: in --ui mode, real API routes must require auth even
    when they were absent from the old hand-maintained prefix allow-list. The
    predicate is now route-aware, so /doctor, /version, /changelog (health
    router, all previously missing from the list and thus served unauthenticated)
    are protected, while genuine client-side SPA routes still fall through to the
    public index.html shell."""

    @pytest.mark.parametrize("path", ["/doctor", "/version", "/changelog"])
    def test_health_router_extras_require_auth_in_ui_mode(
        self, tmp_path: Path, ui_dist: Path, path: str
    ) -> None:
        client = _make_client(tmp_path, ui_dist=ui_dist, token="t")
        resp = client.get(path)
        assert resp.status_code == 401, (
            f"GET {path} must require auth in --ui mode, got {resp.status_code}: {resp.text}"
        )

    def test_non_api_path_stays_public_not_auth_walled(self, tmp_path: Path, ui_dist: Path) -> None:
        # A path that is NOT a registered API route must remain PUBLIC (auth
        # skipped) so the SPA surface is reachable -- it must not be mistaken
        # for a protected endpoint and 401'd. (StaticFiles 404s an unknown path;
        # the security-relevant property is that it is NOT a 401, i.e. the
        # route-aware predicate did not over-protect a non-endpoint path.)
        client = _make_client(tmp_path, ui_dist=ui_dist, token="t")
        resp = client.get("/some-client-side-view")
        assert resp.status_code != 401, (
            f"non-API path must stay public (not auth-walled), got {resp.status_code}"
        )


class TestCorsCredentialsGuard:
    """GHSA-5mh2-6xgr-rf89: allow_credentials=True must never pair with a
    wildcard or malformed CORS origin (Starlette would reflect any Origin and
    return Access-Control-Allow-Credentials: true). create_app rejects such a
    config at startup so the unsafe combination can never ship."""

    @pytest.mark.parametrize(
        "bad",
        [
            ["*"],
            ["http://localhost:5173", "*"],
            ["evil.com"],
            ["http://evil.com/path"],
            ["ws://x"],
        ],
    )
    def test_unsafe_cors_origins_rejected(self, tmp_path: Path, bad: list[str]) -> None:
        from keboola_agent_cli.errors import ConfigError

        with pytest.raises(ConfigError):
            create_app(config_dir=str(tmp_path / "cfg"), auth_token="t", cors_origins=bad)

    @pytest.mark.parametrize(
        "good",
        [
            None,
            ["http://localhost:5173"],
            ["https://app.example.com", "http://127.0.0.1:8000"],
        ],
    )
    def test_safe_cors_origins_accepted(self, tmp_path: Path, good: list[str] | None) -> None:
        app = create_app(config_dir=str(tmp_path / "cfg"), auth_token="t", cors_origins=good)
        assert app is not None

    def test_is_valid_cors_origin_predicate(self) -> None:
        from keboola_agent_cli.server.app import _is_valid_cors_origin

        assert _is_valid_cors_origin("http://localhost:5173")
        assert _is_valid_cors_origin("https://app.example.com")
        assert _is_valid_cors_origin("http://127.0.0.1:8000")
        assert not _is_valid_cors_origin("*")
        assert not _is_valid_cors_origin("example.com")  # no scheme
        assert not _is_valid_cors_origin("http://x/path")  # carries a path
        assert not _is_valid_cors_origin("ws://x")  # wrong scheme
        assert not _is_valid_cors_origin("http://user:pass@evil.com")  # userinfo

    def test_serve_cli_rejects_wildcard_cors_origin(self, monkeypatch) -> None:
        # NB: the CLI layer must surface an unsafe --cors-origin as a clean
        # usage error (exit 2 from typer.BadParameter), not a traceback, and
        # must not start the server -- validation runs before create_app /
        # uvicorn.run, so the wildcard case fails fast without binding a port.
        monkeypatch.setenv("KBAGENT_AUTO_UPDATE", "false")
        from typer.testing import CliRunner

        from keboola_agent_cli.cli import app as cli_app

        result = CliRunner().invoke(cli_app, ["serve", "--cors-origin", "*"])
        assert result.exit_code == 2, result.output


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


class TestUiConfigBanner:
    """``GET /ui-config`` -- the SPA's non-secret bootstrap switches.

    Delivered as an endpoint, NOT injected into ``index.html``: the injection
    point this would have extended (``window.__KBAGENT_TOKEN``) was removed in
    favour of the session cookie, and an injected copy would miss the
    StaticFiles ``html=True`` fallback that serves the shell for deep links --
    letting a suppressed popup reappear. ``test_banner_flag_not_injected_into_html``
    pins the "still nothing in the HTML" half of that decision.
    """

    def test_banner_defaults_to_enabled(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path, token="t")
        resp = client.get("/ui-config", headers={"authorization": "Bearer t"})
        assert resp.status_code == 200
        assert resp.json() == {"banner": True}

    def test_banner_disabled_when_flag_set(self, tmp_path: Path) -> None:
        app = create_app(
            config_dir=str(tmp_path / "cfg"),
            auth_token="t",
            ui_banner=False,
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/ui-config", headers={"authorization": "Bearer t"})
        assert resp.status_code == 200
        assert resp.json() == {"banner": False}

    def test_banner_flag_available_without_ui_mount(self, tmp_path: Path) -> None:
        # The SPA also runs against a bare `kbagent serve` via the Vite dev
        # server / Node BFF, where no dist is mounted -- the switch must still
        # be readable there.
        app = create_app(config_dir=str(tmp_path / "cfg"), auth_token="t", ui_banner=False)
        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/ui-config", headers={"authorization": "Bearer t"}).json() == {
            "banner": False
        }

    def test_banner_flag_not_injected_into_html(self, tmp_path: Path, ui_dist: Path) -> None:
        # The shell stays a static artifact: no config script, no secrets.
        app = create_app(
            config_dir=str(tmp_path / "cfg"),
            auth_token="t",
            ui_dist=str(ui_dist),
            ui_banner=False,
        )
        client = TestClient(app, raise_server_exceptions=False)
        body = client.get("/").text
        assert "__KBAGENT_UI__" not in body
        assert "banner" not in body

    def test_ui_config_requires_auth(self, tmp_path: Path, ui_dist: Path) -> None:
        # Not part of the public bootstrap surface -- the SPA reads it with the
        # session cookie, exactly like /version.
        client = _make_client(tmp_path, ui_dist=ui_dist, token="t")
        assert client.get("/api/ui-config").status_code == 401


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


class TestConfigDetailSandboxAnnotation:
    """Issue #312: ``include_sandbox_annotation=true`` query param on
    ``GET /configs/{project}/{component_id}/{config_id}`` triggers the
    same enrichment as the CLI ``config detail`` command.

    Validates the HTTP-side closure of the #304 trap: web UI / scheduled
    agent / third-party HTTP callers can now see ``sandbox_annotation``
    without spelunking through ``parameters.id``.

    Default-off contract is also pinned: without the flag, the response
    shape is unchanged so existing programmatic consumers stay isolated
    from the new field.
    """

    def _patch_config_service(self, app, *, with_workspace: bool) -> dict:
        """Replace the real ConfigService.get_config_detail with a stub.

        Avoids hitting any real Keboola HTTP -- the test focuses on
        FastAPI's parameter binding and the router -> service plumbing.
        """
        captured: dict = {}

        def fake_get_config_detail(**kwargs):
            captured["kwargs"] = kwargs
            response = {
                "id": kwargs["config_id"],
                "componentId": kwargs["component_id"],
                "configuration": {"parameters": {"id": "1296392806"}},
                "rows": [],
                "project_alias": kwargs["alias"],
                "branch_id": kwargs.get("branch_id"),
            }
            # Emulate what the real service does when include_sandbox_annotation
            # is true and the component is keboola.sandboxes.
            if (
                kwargs.get("include_sandbox_annotation")
                and kwargs["component_id"] == "keboola.sandboxes"
            ):
                response["sandbox_annotation"] = {
                    "sandbox_service_id": "1296392806",
                    "storage_workspace_id": 2950518214 if with_workspace else None,
                    "note": "`parameters.id` ... sandbox-service internal ID ...",
                }
            return response

        app.state.registry.config.get_config_detail = fake_get_config_detail  # type: ignore[method-assign]
        return captured

    def test_flag_off_keeps_response_shape_stable(self, tmp_path: Path) -> None:
        """Default GET (no query param) MUST NOT include sandbox_annotation."""
        client = _make_client(tmp_path, token="t")
        captured = self._patch_config_service(client.app, with_workspace=True)

        resp = client.get(
            "/configs/prod/keboola.sandboxes/sb-cfg-1",
            headers={"authorization": "Bearer t"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "sandbox_annotation" not in body
        # Router forwarded include_sandbox_annotation=False (FastAPI default).
        assert captured["kwargs"]["include_sandbox_annotation"] is False

    def test_flag_on_returns_annotation(self, tmp_path: Path) -> None:
        """`?include_sandbox_annotation=true` propagates through the router."""
        client = _make_client(tmp_path, token="t")
        captured = self._patch_config_service(client.app, with_workspace=True)

        resp = client.get(
            "/configs/prod/keboola.sandboxes/sb-cfg-1",
            params={"include_sandbox_annotation": "true"},
            headers={"authorization": "Bearer t"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        ann = body.get("sandbox_annotation")
        assert ann is not None, "expected sandbox_annotation in HTTP response"
        assert ann["sandbox_service_id"] == "1296392806"
        assert ann["storage_workspace_id"] == 2950518214
        assert captured["kwargs"]["include_sandbox_annotation"] is True

    def test_flag_on_for_non_sandbox_component_is_no_op(self, tmp_path: Path) -> None:
        """The flag is keboola.sandboxes-specific; other components stay clean."""
        client = _make_client(tmp_path, token="t")
        self._patch_config_service(client.app, with_workspace=True)

        resp = client.get(
            "/configs/prod/keboola.ex-db-snowflake/cfg-101",
            params={"include_sandbox_annotation": "true"},
            headers={"authorization": "Bearer t"},
        )
        assert resp.status_code == 200, resp.text
        assert "sandbox_annotation" not in resp.json()
