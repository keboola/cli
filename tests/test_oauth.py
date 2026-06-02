"""Tests for the OAuth project login flow (oauth.py + OAuthLoginService).

Layers covered:
- PKCE pair generation and authorize-URL building (pure unit tests).
- Loopback callback server (real HTTP against 127.0.0.1).
- Token exchange / refresh / Storage-token minting (pytest-httpx mocks).
- Silent refresh chokepoint ``ensure_fresh_oauth_token`` incl. rotation
  persistence and graceful degradation on failure.
- Full protocol round-trip against a fake Connection OAuth server that
  enforces PKCE (real HTTP, no mocks) -- the closest local approximation of
  the real stack until the public OAuth client is registered.
"""

import base64
import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import OAuthCredentials, ProjectConfig, TokenVerifyResponse
from keboola_agent_cli.oauth import (
    OAuthCallbackServer,
    build_authorize_url,
    ensure_fresh_oauth_token,
    exchange_code,
    generate_pkce_pair,
    generate_state,
    mint_storage_token,
    refresh_oauth_tokens,
)
from keboola_agent_cli.services.base import BaseService
from keboola_agent_cli.services.oauth_login_service import OAuthLoginService

STACK_URL = "https://connection.test.keboola.com"
TEST_SAPI_TOKEN = "901-55555-fakeMintedTokenXXXXXXXXXXXXX"


def _b64url_sha256(value: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(value.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


# ── PKCE + authorize URL ────────────────────────────────────────────


class TestPkce:
    def test_verifier_charset_and_length(self) -> None:
        pair = generate_pkce_pair()
        assert 43 <= len(pair.verifier) <= 128
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
        assert set(pair.verifier) <= allowed

    def test_challenge_is_s256_of_verifier(self) -> None:
        pair = generate_pkce_pair()
        assert pair.challenge == _b64url_sha256(pair.verifier)
        assert "=" not in pair.challenge  # no base64 padding

    def test_pairs_are_unique(self) -> None:
        assert generate_pkce_pair().verifier != generate_pkce_pair().verifier
        assert generate_state() != generate_state()

    def test_authorize_url_params(self) -> None:
        url = build_authorize_url(
            STACK_URL + "/",  # trailing slash must not double up
            client_id="kbagent-cli",
            redirect_uri="http://127.0.0.1:8765/callback",
            state="state-123",
            code_challenge="challenge-456",
        )
        parsed = urllib.parse.urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.path == "/oauth/authorize"
        params = dict(urllib.parse.parse_qsl(parsed.query))
        assert params == {
            "client_id": "kbagent-cli",
            "response_type": "code",
            "redirect_uri": "http://127.0.0.1:8765/callback",
            "state": "state-123",
            "code_challenge": "challenge-456",
            "code_challenge_method": "S256",
        }
        # Public client: never a secret; server-default scope: never a scope.
        assert "client_secret" not in params
        assert "scope" not in params


# ── Loopback callback server ────────────────────────────────────────


def _browser_get(url: str) -> int:
    """Simulate the browser redirect with stdlib urllib (NOT httpx, so
    pytest-httpx interception never swallows it)."""
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status


class TestCallbackServer:
    def test_receives_code(self) -> None:
        with OAuthCallbackServer(ports=(0,)) as server:  # port 0 = ephemeral
            status = _browser_get(f"{server.redirect_uri}?code=abc123&state=st1")
            assert status == 200
            assert server.wait_for_code("st1", timeout=5) == "abc123"

    def test_state_mismatch_raises(self) -> None:
        with OAuthCallbackServer(ports=(0,)) as server:
            _browser_get(f"{server.redirect_uri}?code=abc123&state=WRONG")
            with pytest.raises(KeboolaApiError) as exc_info:
                server.wait_for_code("st1", timeout=5)
            assert exc_info.value.error_code == ErrorCode.OAUTH_ERROR
            assert "state mismatch" in exc_info.value.message.lower()

    def test_error_redirect_raises(self) -> None:
        with OAuthCallbackServer(ports=(0,)) as server:
            _browser_get(
                f"{server.redirect_uri}?error=access_denied&error_description=User+denied+consent"
            )
            with pytest.raises(KeboolaApiError) as exc_info:
                server.wait_for_code("st1", timeout=5)
            assert "User denied consent" in exc_info.value.message

    def test_timeout_raises(self) -> None:
        with OAuthCallbackServer(ports=(0,)) as server:
            with pytest.raises(KeboolaApiError) as exc_info:
                server.wait_for_code("st1", timeout=0.1)
            assert "Timed out" in exc_info.value.message

    def test_unknown_path_is_404_and_does_not_consume_the_wait(self) -> None:
        with OAuthCallbackServer(ports=(0,)) as server:
            base = server.redirect_uri.rsplit("/callback", 1)[0]
            with pytest.raises(urllib.error.HTTPError) as http_err:
                _browser_get(f"{base}/favicon.ico")
            assert http_err.value.code == 404
            _browser_get(f"{server.redirect_uri}?code=late&state=st1")
            assert server.wait_for_code("st1", timeout=5) == "late"

    def test_port_fallback_when_first_port_taken(self) -> None:
        with OAuthCallbackServer(ports=(0,)) as first:
            taken_port = first.server_address[1]
            with OAuthCallbackServer(ports=(taken_port, 0)) as second:
                assert second.server_address[1] != taken_port

    def test_no_free_port_raises(self) -> None:
        with OAuthCallbackServer(ports=(0,)) as first:
            taken_port = first.server_address[1]
            with pytest.raises(KeboolaApiError) as exc_info:
                OAuthCallbackServer(ports=(taken_port,))
            assert "callback port" in exc_info.value.message


# ── Token endpoint (mocked HTTP) ────────────────────────────────────


def _token_response(access: str = "at-1", refresh: str = "rt-1") -> dict:
    return {
        "token_type": "Bearer",
        "expires_in": 3600,
        "access_token": access,
        "refresh_token": refresh,
    }


class TestTokenEndpoint:
    def test_exchange_code_success_posts_pkce_form(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/oauth/token",
            method="POST",
            json=_token_response(),
        )
        tokens = exchange_code(
            STACK_URL,
            client_id="kbagent-cli",
            code="auth-code-1",
            code_verifier="verifier-1",
            redirect_uri="http://127.0.0.1:8765/callback",
        )
        assert tokens.access_token == "at-1"
        assert tokens.refresh_token == "rt-1"
        assert tokens.expires_in == 3600

        form = dict(urllib.parse.parse_qsl(httpx_mock.get_requests()[0].read().decode()))
        assert form["grant_type"] == "authorization_code"
        assert form["code"] == "auth-code-1"
        assert form["code_verifier"] == "verifier-1"
        assert form["redirect_uri"] == "http://127.0.0.1:8765/callback"
        assert "client_secret" not in form  # public client, PKCE only

    def test_exchange_error_payload_raises_oauth_error(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/oauth/token",
            method="POST",
            status_code=400,
            json={"error": "invalid_grant", "error_description": "Code expired"},
        )
        with pytest.raises(KeboolaApiError) as exc_info:
            exchange_code(
                STACK_URL,
                client_id="kbagent-cli",
                code="stale",
                code_verifier="v",
                redirect_uri="http://127.0.0.1:8765/callback",
            )
        assert exc_info.value.error_code == ErrorCode.OAUTH_ERROR
        assert "Code expired" in exc_info.value.message

    def test_missing_refresh_token_raises(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/oauth/token",
            method="POST",
            json={"access_token": "at", "expires_in": 3600},
        )
        with pytest.raises(KeboolaApiError) as exc_info:
            refresh_oauth_tokens(STACK_URL, client_id="c", refresh_token="rt")
        assert "missing" in exc_info.value.message

    def test_refresh_posts_refresh_grant(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/oauth/token",
            method="POST",
            json=_token_response(access="at-2", refresh="rt-2"),
        )
        tokens = refresh_oauth_tokens(STACK_URL, client_id="kbagent-cli", refresh_token="rt-1")
        assert tokens.refresh_token == "rt-2"
        form = dict(urllib.parse.parse_qsl(httpx_mock.get_requests()[0].read().decode()))
        assert form == {
            "client_id": "kbagent-cli",
            "grant_type": "refresh_token",
            "refresh_token": "rt-1",
        }


class TestMintStorageToken:
    def test_success_sends_bearer_and_capability_flags(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v2/storage/tokens",
            method="POST",
            json={"token": TEST_SAPI_TOKEN},
        )
        token = mint_storage_token(STACK_URL, access_token="at-1")
        assert token == TEST_SAPI_TOKEN

        request = httpx_mock.get_requests()[0]
        assert request.headers["Authorization"] == "Bearer at-1"
        payload = json.loads(request.read())
        assert payload["canManageBuckets"] is True
        assert payload["canManageTokens"] is True
        assert payload["expiresIn"] == 7200

    def test_rejection_raises_oauth_error(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v2/storage/tokens",
            method="POST",
            status_code=401,
            json={"error": "Invalid access token"},
        )
        with pytest.raises(KeboolaApiError) as exc_info:
            mint_storage_token(STACK_URL, access_token="bad")
        assert exc_info.value.error_code == ErrorCode.OAUTH_ERROR


# ── Config round-trip ───────────────────────────────────────────────


class TestOAuthCredentialsPersistence:
    def test_round_trip_through_config_store(self, config_store: ConfigStore) -> None:
        project = ProjectConfig(
            stack_url=STACK_URL,
            token=TEST_SAPI_TOKEN,
            project_name="OAuth Proj",
            project_id=901,
            oauth=OAuthCredentials(
                client_id="kbagent-cli",
                refresh_token="rt-persisted",
                token_expires_at=1234567890.0,
            ),
        )
        config_store.add_project("oauth-proj", project)
        loaded = config_store.get_project("oauth-proj")
        assert loaded is not None
        assert loaded.oauth is not None
        assert loaded.oauth.refresh_token == "rt-persisted"
        assert loaded.oauth.token_expires_at == 1234567890.0

    def test_classic_project_has_no_oauth_block(self, config_store: ConfigStore) -> None:
        config_store.add_project(
            "classic", ProjectConfig(stack_url=STACK_URL, token=TEST_SAPI_TOKEN)
        )
        loaded = config_store.get_project("classic")
        assert loaded is not None
        assert loaded.oauth is None


# ── Silent refresh chokepoint ───────────────────────────────────────


def _add_oauth_project(
    config_store: ConfigStore,
    alias: str = "oauth-proj",
    expires_in: float = -10.0,  # default: already expired
) -> ProjectConfig:
    project = ProjectConfig(
        stack_url=STACK_URL,
        token="901-1-staleMintedToken",
        project_name="OAuth Proj",
        project_id=901,
        oauth=OAuthCredentials(
            client_id="kbagent-cli",
            refresh_token="rt-old",
            token_expires_at=time.time() + expires_in,
        ),
    )
    config_store.add_project(alias, project)
    return project


class TestEnsureFreshOAuthToken:
    def test_classic_project_passthrough_no_network(self, config_store: ConfigStore) -> None:
        project = ProjectConfig(stack_url=STACK_URL, token=TEST_SAPI_TOKEN)
        assert ensure_fresh_oauth_token(config_store, "p", project) is project

    def test_fresh_token_passthrough_no_network(self, config_store: ConfigStore) -> None:
        project = _add_oauth_project(config_store, expires_in=3600.0)
        result = ensure_fresh_oauth_token(config_store, "oauth-proj", project)
        assert result.token == project.token
        assert result.oauth is not None
        assert result.oauth.refresh_token == "rt-old"

    def test_expired_token_refreshes_and_persists_rotation(
        self, config_store: ConfigStore, httpx_mock
    ) -> None:
        _add_oauth_project(config_store)
        httpx_mock.add_response(
            url=f"{STACK_URL}/oauth/token",
            method="POST",
            json=_token_response(access="at-new", refresh="rt-new"),
        )
        httpx_mock.add_response(
            url=f"{STACK_URL}/v2/storage/tokens",
            method="POST",
            json={"token": TEST_SAPI_TOKEN},
        )

        stale = config_store.get_project("oauth-proj")
        assert stale is not None
        fresh = ensure_fresh_oauth_token(config_store, "oauth-proj", stale)

        assert fresh.token == TEST_SAPI_TOKEN
        assert fresh.oauth is not None
        assert fresh.oauth.refresh_token == "rt-new"  # rotation persisted
        assert fresh.oauth.token_expires_at is not None
        assert fresh.oauth.token_expires_at > time.time()

        # The rotated credentials must be ON DISK, not only in memory.
        persisted = config_store.get_project("oauth-proj")
        assert persisted is not None
        assert persisted.token == TEST_SAPI_TOKEN
        assert persisted.oauth is not None
        assert persisted.oauth.refresh_token == "rt-new"

    def test_none_expiry_treated_as_expired(self, config_store: ConfigStore, httpx_mock) -> None:
        project = ProjectConfig(
            stack_url=STACK_URL,
            token="901-1-stale",
            project_id=901,
            oauth=OAuthCredentials(client_id="c", refresh_token="rt-old"),
        )
        config_store.add_project("oauth-proj", project)
        httpx_mock.add_response(
            url=f"{STACK_URL}/oauth/token", method="POST", json=_token_response()
        )
        httpx_mock.add_response(
            url=f"{STACK_URL}/v2/storage/tokens",
            method="POST",
            json={"token": TEST_SAPI_TOKEN},
        )
        fresh = ensure_fresh_oauth_token(config_store, "oauth-proj", project)
        assert fresh.token == TEST_SAPI_TOKEN

    def test_refresh_failure_returns_stale_project(
        self, config_store: ConfigStore, httpx_mock
    ) -> None:
        _add_oauth_project(config_store)
        httpx_mock.add_response(
            url=f"{STACK_URL}/oauth/token",
            method="POST",
            status_code=400,
            json={"error": "invalid_grant", "error_description": "Refresh token revoked"},
        )
        stale = config_store.get_project("oauth-proj")
        assert stale is not None
        result = ensure_fresh_oauth_token(config_store, "oauth-proj", stale)
        # Graceful degradation: stale project returned, no exception -- the
        # downstream 401 is reported per-project in multi-project fan-outs.
        assert result.token == stale.token
        assert result.oauth is not None
        assert result.oauth.refresh_token == "rt-old"

    def test_resolve_projects_triggers_refresh(self, config_store: ConfigStore, httpx_mock) -> None:
        """BaseService.resolve_projects() is the chokepoint -- an expired
        OAuth project resolved through ANY service comes back fresh."""
        _add_oauth_project(config_store)
        config_store.add_project(
            "classic", ProjectConfig(stack_url=STACK_URL, token=TEST_SAPI_TOKEN)
        )
        httpx_mock.add_response(
            url=f"{STACK_URL}/oauth/token",
            method="POST",
            json=_token_response(access="at-new", refresh="rt-new"),
        )
        httpx_mock.add_response(
            url=f"{STACK_URL}/v2/storage/tokens",
            method="POST",
            json={"token": TEST_SAPI_TOKEN},
        )

        service = BaseService(config_store=config_store)
        resolved = service.resolve_projects()
        assert resolved["oauth-proj"].token == TEST_SAPI_TOKEN
        assert resolved["classic"].token == TEST_SAPI_TOKEN  # untouched


# ── Login service orchestration (mocked protocol) ───────────────────


def _verify_response(project_id: int = 901, name: str = "OAuth Proj") -> TokenVerifyResponse:
    return TokenVerifyResponse(
        token_id="t-1",
        token_description="desc",
        project_id=project_id,
        project_name=name,
        owner_name="Owner",
    )


def _make_login_service(config_store: ConfigStore) -> tuple[OAuthLoginService, MagicMock]:
    client = MagicMock()
    client.verify_token.return_value = _verify_response()

    def client_factory(stack_url: str, token: str):
        return client

    return OAuthLoginService(config_store=config_store, client_factory=client_factory), client


@pytest.fixture
def patched_protocol(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch the oauth protocol functions the service imported by name."""
    calls: dict = {}
    base = "keboola_agent_cli.services.oauth_login_service"

    class FakeServer:
        redirect_uri = "http://127.0.0.1:8765/callback"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def wait_for_code(self, state: str, timeout: float) -> str:
            calls["waited_state"] = state
            return "auth-code-1"

    def fake_exchange(stack_url, *, client_id, code, code_verifier, redirect_uri):
        calls["exchange"] = {
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        }
        from keboola_agent_cli.oauth import OAuthTokens

        return OAuthTokens(access_token="at-1", refresh_token="rt-1", expires_in=3600)

    def fake_mint(stack_url, *, access_token, **kwargs):
        calls["minted_with"] = access_token
        return TEST_SAPI_TOKEN

    monkeypatch.setattr(base + ".OAuthCallbackServer", lambda ports: FakeServer())
    monkeypatch.setattr(base + ".exchange_code", fake_exchange)
    monkeypatch.setattr(base + ".mint_storage_token", fake_mint)
    monkeypatch.setattr(base + ".webbrowser", MagicMock())
    return calls


class TestOAuthLoginService:
    def test_login_persists_project_with_oauth_credentials(
        self, config_store: ConfigStore, patched_protocol: dict
    ) -> None:
        service, client = _make_login_service(config_store)
        shown_urls: list[str] = []

        result = service.login(
            "connection.test.keboola.com",  # bare host accepted like project add
            on_authorize_url=shown_urls.append,
        )

        assert result["alias"] == "oauth-proj"  # slugified project name
        assert result["project_id"] == 901
        assert result["re_authenticated"] is False
        assert result["auth_type"] == "oauth"
        assert TEST_SAPI_TOKEN not in json.dumps(result)  # masked in output

        assert shown_urls and shown_urls[0].startswith(f"{STACK_URL}/oauth/authorize?")
        assert patched_protocol["minted_with"] == "at-1"
        client.verify_token.assert_called_once()

        saved = config_store.get_project("oauth-proj")
        assert saved is not None
        assert saved.token == TEST_SAPI_TOKEN
        assert saved.oauth is not None
        assert saved.oauth.refresh_token == "rt-1"

    def test_relogin_updates_existing_entry_in_place(
        self, config_store: ConfigStore, patched_protocol: dict
    ) -> None:
        _add_oauth_project(config_store, alias="myproj")
        config_store.set_project_branch("myproj", 4242)  # must survive re-login
        service, _client = _make_login_service(config_store)

        result = service.login(STACK_URL)

        assert result["alias"] == "myproj"
        assert result["re_authenticated"] is True
        saved = config_store.get_project("myproj")
        assert saved is not None
        assert saved.oauth is not None
        assert saved.oauth.refresh_token == "rt-1"
        assert saved.active_branch_id == 4242
        # No duplicate created under the slugified name.
        assert config_store.get_project("oauth-proj") is None

    def test_alias_collision_with_different_project_raises(
        self, config_store: ConfigStore, patched_protocol: dict
    ) -> None:
        config_store.add_project(
            "oauth-proj",
            ProjectConfig(stack_url=STACK_URL, token="901-2-other", project_id=999),
        )
        service, _client = _make_login_service(config_store)
        from keboola_agent_cli.errors import ConfigError

        with pytest.raises(ConfigError, match="different project"):
            service.login(STACK_URL)

    def test_explicit_alias_wins(self, config_store: ConfigStore, patched_protocol: dict) -> None:
        service, _client = _make_login_service(config_store)
        result = service.login(STACK_URL, alias="prod")
        assert result["alias"] == "prod"
        assert config_store.get_project("prod") is not None


# ── Full protocol round-trip against a fake Connection server ───────


class _FakeConnectionHandler(BaseHTTPRequestHandler):
    """Minimal Connection stand-in: /oauth/authorize, /oauth/token,
    /v2/storage/tokens, /v2/storage/tokens/verify. Enforces PKCE S256."""

    server: "_FakeConnectionServer"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/oauth/authorize":
            params = dict(urllib.parse.parse_qsl(parsed.query))
            self.server.challenge = params["code_challenge"]
            redirect = f"{params['redirect_uri']}?code=fake-auth-code&state={params['state']}"
            self.send_response(302)
            self.send_header("Location", redirect)
            self.end_headers()
        elif parsed.path == "/v2/storage/tokens/verify":
            self._json(
                200,
                {
                    "id": "t-1",
                    "description": "minted",
                    "owner": {"id": 901, "name": "Fake Project"},
                },
            )
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode()
        if self.path == "/oauth/token":
            form = dict(urllib.parse.parse_qsl(body))
            if form.get("grant_type") == "refresh_token":
                # League-style rotation: only the CURRENT refresh token works,
                # and a NEW one is issued (the old one is revoked).
                if form.get("refresh_token") != self.server.current_refresh_token:
                    self._json(400, {"error": "invalid_grant"})
                    return
                self.server.refresh_count += 1
                self.server.current_refresh_token = (
                    f"fake-refresh-token-r{self.server.refresh_count}"
                )
                self._json(
                    200,
                    {
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "access_token": "fake-access-token",
                        "refresh_token": self.server.current_refresh_token,
                    },
                )
                return
            # Real PKCE enforcement: S256(verifier) must equal the challenge
            # captured during /oauth/authorize.
            if form.get("code") != "fake-auth-code" or (
                _b64url_sha256(form.get("code_verifier", "")) != self.server.challenge
            ):
                self._json(400, {"error": "invalid_grant"})
                return
            self._json(
                200,
                {
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "access_token": "fake-access-token",
                    "refresh_token": self.server.current_refresh_token,
                },
            )
        elif self.path == "/v2/storage/tokens":
            if self.headers.get("Authorization") != "Bearer fake-access-token":
                self._json(401, {"error": "bad bearer"})
                return
            self._json(200, {"token": TEST_SAPI_TOKEN})
        else:
            self._json(404, {"error": "not found"})

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        pass


class _FakeConnectionServer(HTTPServer):
    challenge: str = ""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _FakeConnectionHandler)
        self.current_refresh_token = "fake-refresh-token"
        self.refresh_count = 0
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def close(self) -> None:
        self.shutdown()
        self.server_close()
        self._thread.join(timeout=5)


class TestFullProtocolRoundTrip:
    """Real-HTTP end-to-end: authorize redirect -> loopback callback ->
    PKCE-verified code exchange -> Bearer-minted Storage token.

    No pytest-httpx fixture here on purpose -- every hop is a real request.
    """

    def test_oauth_dance_against_fake_server(self) -> None:
        fake = _FakeConnectionServer()
        try:
            pkce = generate_pkce_pair()
            state = generate_state()
            with OAuthCallbackServer(ports=(0,)) as callback:
                authorize_url = build_authorize_url(
                    fake.url,
                    client_id="kbagent-cli",
                    redirect_uri=callback.redirect_uri,
                    state=state,
                    code_challenge=pkce.challenge,
                )
                # "Browser": follows the 302 from /oauth/authorize to the
                # loopback callback automatically.
                assert _browser_get(authorize_url) == 200
                code = callback.wait_for_code(state, timeout=10)
                redirect_uri = callback.redirect_uri

            tokens = exchange_code(
                fake.url,
                client_id="kbagent-cli",
                code=code,
                code_verifier=pkce.verifier,
                redirect_uri=redirect_uri,
            )
            assert tokens.refresh_token == "fake-refresh-token"

            sapi_token = mint_storage_token(fake.url, access_token=tokens.access_token)
            assert sapi_token == TEST_SAPI_TOKEN
        finally:
            fake.close()

    def test_refresh_rotation_against_fake_server(self) -> None:
        """Refresh works only with the CURRENT refresh token (League rotates
        and revokes), so a stale token must be rejected."""
        fake = _FakeConnectionServer()
        try:
            first = refresh_oauth_tokens(
                fake.url, client_id="kbagent-cli", refresh_token="fake-refresh-token"
            )
            assert first.refresh_token == "fake-refresh-token-r1"
            second = refresh_oauth_tokens(
                fake.url, client_id="kbagent-cli", refresh_token=first.refresh_token
            )
            assert second.refresh_token == "fake-refresh-token-r2"
            # The original (revoked) token no longer works.
            with pytest.raises(KeboolaApiError) as exc_info:
                refresh_oauth_tokens(
                    fake.url, client_id="kbagent-cli", refresh_token="fake-refresh-token"
                )
            assert exc_info.value.error_code == ErrorCode.OAUTH_ERROR
        finally:
            fake.close()

    def test_wrong_verifier_is_rejected_by_pkce(self) -> None:
        fake = _FakeConnectionServer()
        try:
            pkce = generate_pkce_pair()
            state = generate_state()
            with OAuthCallbackServer(ports=(0,)) as callback:
                authorize_url = build_authorize_url(
                    fake.url,
                    client_id="kbagent-cli",
                    redirect_uri=callback.redirect_uri,
                    state=state,
                    code_challenge=pkce.challenge,
                )
                _browser_get(authorize_url)
                code = callback.wait_for_code(state, timeout=10)
                redirect_uri = callback.redirect_uri

            with pytest.raises(KeboolaApiError) as exc_info:
                exchange_code(
                    fake.url,
                    client_id="kbagent-cli",
                    code=code,
                    code_verifier="not-the-right-verifier-at-all-padpadpadpadpad",
                    redirect_uri=redirect_uri,
                )
            assert exc_info.value.error_code == ErrorCode.OAUTH_ERROR
        finally:
            fake.close()


# ── CLI layer ───────────────────────────────────────────────────────


class TestProjectLoginCli:
    def _invoke(self, tmp_config_dir: Path, args: list[str], login_result=None, login_error=None):
        from typer.testing import CliRunner

        from keboola_agent_cli.cli import app

        runner = CliRunner()
        with patch.object(OAuthLoginService, "login") as mock_login:
            if login_error is not None:
                mock_login.side_effect = login_error
            else:
                mock_login.return_value = login_result or {
                    "alias": "oauth-proj",
                    "project_name": "OAuth Proj",
                    "project_id": 901,
                    "stack_url": STACK_URL,
                    "token": "901-55...XXXX",
                    "token_expires_at": time.time() + 7200,
                    "re_authenticated": False,
                    "auth_type": "oauth",
                }
            result = runner.invoke(
                app,
                ["--json", "--config-dir", str(tmp_config_dir), "project", "login", *args],
            )
        return result, mock_login

    def test_login_success_json(self, tmp_config_dir: Path) -> None:
        result, mock_login = self._invoke(tmp_config_dir, ["--url", STACK_URL])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        assert payload["data"]["alias"] == "oauth-proj"
        kwargs = mock_login.call_args.kwargs
        assert kwargs["open_browser"] is True
        assert kwargs["port"] is None

    def test_no_browser_and_port_flags_forwarded(self, tmp_config_dir: Path) -> None:
        result, mock_login = self._invoke(
            tmp_config_dir,
            ["--url", STACK_URL, "--no-browser", "--port", "9000", "--project", "prod"],
        )
        assert result.exit_code == 0, result.output
        kwargs = mock_login.call_args.kwargs
        assert kwargs["open_browser"] is False
        assert kwargs["port"] == 9000
        assert kwargs["alias"] == "prod"

    def test_oauth_error_maps_to_exit_3(self, tmp_config_dir: Path) -> None:
        error = KeboolaApiError(
            "OAuth login failed in the browser: access_denied",
            error_code=ErrorCode.OAUTH_ERROR,
        )
        result, _mock = self._invoke(tmp_config_dir, ["--url", STACK_URL], login_error=error)
        assert result.exit_code == 3
        payload = json.loads(result.output)
        assert payload["error"]["code"] == "OAUTH_ERROR"
