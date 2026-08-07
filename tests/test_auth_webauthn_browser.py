"""Tests for auth/webauthn_browser.py: the WebAuthn sudo-ceremony loopback server.

Mirrors test_auth_pkce.py's approach: a real loopback HTTP server driven by
real HTTP requests, not a mock of http.server.
"""

from __future__ import annotations

import contextlib
import json
import threading
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from keboola_agent_cli.auth.webauthn_browser import (
    WebAuthnCallback,
    WebAuthnCallbackServer,
    WebAuthnCeremonyDenied,
    WebAuthnCeremonyTimeout,
    WebAuthnStateMismatch,
    build_ceremony_url,
    generate_webauthn_state,
)


def _get(base_url: str, params: dict[str, str] | None = None) -> None:
    query = urlencode(params) if params else ""
    url = f"{base_url}?{query}" if query else base_url
    with contextlib.suppress(urllib.error.URLError, OSError):
        urllib.request.urlopen(url, timeout=5)


def _get_after(delay: float, base_url: str, params: dict[str, str] | None = None) -> None:
    timer = threading.Timer(delay, _get, args=(base_url, params))
    timer.daemon = True
    timer.start()


class TestGenerateWebAuthnState:
    def test_produces_distinct_nonces(self) -> None:
        first = generate_webauthn_state()
        second = generate_webauthn_state()
        assert first
        assert first != second


class TestWebAuthnCallbackServerSuccess:
    def test_real_loopback_request_resolves_wait(self) -> None:
        with WebAuthnCallbackServer(expected_state="expected-state") as server:
            assert server.redirect_uri.startswith("http://127.0.0.1:")
            assert server.redirect_uri.endswith("/callback")

            _get_after(
                0.05,
                server.redirect_uri,
                {"assertion": "fake-assertion-json", "state": "expected-state"},
            )

            result = server.wait(timeout=5.0)

        assert result == WebAuthnCallback(assertion="fake-assertion-json", state="expected-state")

    def test_favicon_style_request_does_not_resolve_wait(self) -> None:
        with WebAuthnCallbackServer(expected_state="expected-state") as server:
            _get_after(0.02, f"{server.redirect_uri.rsplit('/callback', 1)[0]}/favicon.ico")
            _get_after(
                0.08,
                server.redirect_uri,
                {"assertion": "real-assertion", "state": "expected-state"},
            )

            result = server.wait(timeout=5.0)

        assert result == WebAuthnCallback(assertion="real-assertion", state="expected-state")


class TestWebAuthnCallbackServerStateMismatch:
    def test_wrong_state_raises_and_never_resolves_a_result(self) -> None:
        with WebAuthnCallbackServer(expected_state="expected-state") as server:
            _get_after(0.02, server.redirect_uri, {"assertion": "a", "state": "wrong-state"})
            with pytest.raises(WebAuthnStateMismatch):
                server.wait(timeout=5.0)


class TestWebAuthnCallbackServerDenied:
    def test_error_param_raises_ceremony_denied(self) -> None:
        with WebAuthnCallbackServer(expected_state="expected-state") as server:
            _get_after(
                0.02,
                server.redirect_uri,
                {
                    "error": "cancelled",
                    "error_description": "user dismissed",
                    "state": "expected-state",
                },
            )
            with pytest.raises(WebAuthnCeremonyDenied) as excinfo:
                server.wait(timeout=5.0)
            assert excinfo.value.error == "cancelled"


class TestWebAuthnCallbackServerTimeout:
    def test_no_callback_raises_timeout(self) -> None:
        with (
            WebAuthnCallbackServer(expected_state="expected-state") as server,
            pytest.raises(WebAuthnCeremonyTimeout),
        ):
            server.wait(timeout=0.2)


class TestBuildCeremonyUrl:
    def test_query_carries_all_fields(self) -> None:
        url = build_ceremony_url(
            stack_url="https://connection.keboola.com",
            ceremony_path="/admin/auth/sudo/webauthn",
            options={"challenge": "abc", "rpId": "keboola.com"},
            challenge_token="kbc_mfa_xyz",
            redirect_uri="http://127.0.0.1:12345/callback",
            state="state123",
        )
        parsed = urlsplit(url)
        assert parsed.scheme == "https"
        assert parsed.netloc == "connection.keboola.com"
        assert parsed.path == "/admin/auth/sudo/webauthn"

        params = parse_qs(parsed.query)
        assert params["challengeToken"] == ["kbc_mfa_xyz"]
        assert params["redirectUri"] == ["http://127.0.0.1:12345/callback"]
        assert params["state"] == ["state123"]
        assert json.loads(params["options"][0]) == {"challenge": "abc", "rpId": "keboola.com"}
