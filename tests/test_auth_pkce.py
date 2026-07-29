"""Tests for auth/pkce.py: PKCE challenge generation and the loopback callback server.

Uses a real loopback HTTP server driven by real HTTP requests (urllib) rather
than mocking `http.server` -- the whole point of `PkceCallbackServer` is the
wire-level behaviour (query parsing, constant-time state check, timing), which
a mock would not exercise.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import urllib.error
import urllib.request
from urllib.parse import urlencode

import pytest

from keboola_agent_cli.auth import pkce
from keboola_agent_cli.auth.pkce import (
    LoopbackCallback,
    PkceAuthorizationError,
    PkceCallbackServer,
    PkceCallbackTimeout,
    PkceChallenge,
    PkceSetupError,
    PkceStateMismatch,
    generate_pkce_challenge,
)


def _get(base_url: str, params: dict[str, str] | None = None) -> None:
    """Send a real GET to the callback server.

    Swallows both the 404 the handler legitimately returns for an ignored
    request (e.g. the favicon-style case) and a connection error, which
    happens when this call was scheduled via `_get_after` and the server was
    already closed (deliberately-too-late-callback tests) by the time it fires.
    """
    query = urlencode(params) if params else ""
    url = f"{base_url}?{query}" if query else base_url
    with contextlib.suppress(urllib.error.URLError, OSError):
        urllib.request.urlopen(url, timeout=5)


def _get_after(delay: float, base_url: str, params: dict[str, str] | None = None) -> None:
    """Schedule `_get` on a background thread after `delay` seconds."""
    timer = threading.Timer(delay, _get, args=(base_url, params))
    timer.daemon = True
    timer.start()


class TestGeneratePkceChallenge:
    """generate_pkce_challenge produces a fresh, correctly-shaped triple."""

    def test_returns_frozen_dataclass_with_distinct_fields(self) -> None:
        challenge = generate_pkce_challenge()
        assert isinstance(challenge, PkceChallenge)
        assert challenge.code_verifier
        assert challenge.code_challenge
        assert challenge.state
        assert challenge.code_verifier != challenge.state
        assert challenge.code_verifier != challenge.code_challenge

    def test_two_calls_produce_different_values(self) -> None:
        first = generate_pkce_challenge()
        second = generate_pkce_challenge()
        assert first.code_verifier != second.code_verifier
        assert first.state != second.state

    def test_rfc7636_s256_test_vector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The RFC 7636 appendix B test vector: a fixed verifier must produce the
        documented S256 challenge."""
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        expected_challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

        monkeypatch.setattr(pkce.secrets, "token_urlsafe", lambda _n: verifier)

        challenge = generate_pkce_challenge()

        assert challenge.code_verifier == verifier
        assert challenge.code_challenge == expected_challenge


class TestPkceCallbackServerSuccess:
    """A correct callback (matching state, code present) resolves wait()."""

    def test_real_loopback_request_resolves_wait(self) -> None:
        with PkceCallbackServer(expected_state="expected-state") as server:
            assert server.redirect_uri.startswith("http://127.0.0.1:")
            assert server.redirect_uri.endswith("/callback")

            _get_after(
                0.05, server.redirect_uri, {"code": "auth-code-123", "state": "expected-state"}
            )

            result = server.wait(timeout=5.0)

        assert result == LoopbackCallback(code="auth-code-123", state="expected-state")

    def test_favicon_style_request_does_not_resolve_wait(self) -> None:
        """A request with neither `code` nor `error` (e.g. a stray /favicon.ico)
        must be ignored, not treated as a callback."""
        with PkceCallbackServer(expected_state="expected-state") as server:
            _get_after(0.02, f"{server.redirect_uri.rsplit('/callback', 1)[0]}/favicon.ico")
            _get_after(0.08, server.redirect_uri, {"code": "real-code", "state": "expected-state"})

            result = server.wait(timeout=5.0)

        assert result == LoopbackCallback(code="real-code", state="expected-state")


class TestPkceCallbackServerStateMismatch:
    """A state mismatch is terminal: PkceStateMismatch, never a fallback trigger,
    and the code must never reach an exchange call."""

    def test_state_mismatch_raises_and_never_reaches_exchange(self) -> None:
        exchange_calls: list[tuple[str, str]] = []

        def fake_exchange(code: str, state: str) -> None:
            exchange_calls.append((code, state))

        with PkceCallbackServer(expected_state="expected-state") as server:
            _get_after(0.05, server.redirect_uri, {"code": "auth-code", "state": "wrong-state"})

            with pytest.raises(PkceStateMismatch):
                callback = server.wait(timeout=5.0)
                fake_exchange(callback.code, callback.state)  # pragma: no cover - unreachable

        assert exchange_calls == []

    def test_state_mismatch_is_not_a_pkce_setup_error(self) -> None:
        """Terminal errors must not subclass PkceSetupError, or a caller doing
        `except PkceSetupError: fall back to device flow` would wrongly retry."""
        assert not issubclass(PkceStateMismatch, PkceSetupError)

    def test_missing_state_on_callback_is_treated_as_mismatch(self) -> None:
        with PkceCallbackServer(expected_state="expected-state") as server:
            _get_after(0.05, server.redirect_uri, {"code": "auth-code"})

            with pytest.raises(PkceStateMismatch):
                server.wait(timeout=5.0)


class TestPkceCallbackServerAuthorizationError:
    """An `error=` redirect is terminal, no fallback."""

    def test_error_redirect_raises_authorization_error(self) -> None:
        with PkceCallbackServer(expected_state="expected-state") as server:
            _get_after(
                0.05,
                server.redirect_uri,
                {
                    "error": "access_denied",
                    "error_description": "The user declined the request.",
                    "state": "expected-state",
                },
            )

            with pytest.raises(PkceAuthorizationError) as exc_info:
                server.wait(timeout=5.0)

        assert exc_info.value.error == "access_denied"
        assert exc_info.value.description == "The user declined the request."

    def test_authorization_error_is_not_a_pkce_setup_error(self) -> None:
        assert not issubclass(PkceAuthorizationError, PkceSetupError)

    def test_error_redirect_with_wrong_state_is_a_state_mismatch_not_authorization_error(
        self,
    ) -> None:
        """State is checked before anything else -- even before inspecting `error=`."""
        with PkceCallbackServer(expected_state="expected-state") as server:
            _get_after(
                0.05,
                server.redirect_uri,
                {"error": "access_denied", "state": "wrong-state"},
            )

            with pytest.raises(PkceStateMismatch):
                server.wait(timeout=5.0)


class TestPkceCallbackServerTimeout:
    """wait() raises the fallback-eligible PkceCallbackTimeout when nothing arrives."""

    def test_timeout_raises_pkce_callback_timeout(self) -> None:
        with (
            PkceCallbackServer(expected_state="expected-state") as server,
            pytest.raises(PkceCallbackTimeout),
        ):
            server.wait(timeout=0.1)

    def test_pkce_callback_timeout_is_fallback_eligible(self) -> None:
        assert issubclass(PkceCallbackTimeout, PkceSetupError)

    def test_callback_arriving_just_before_timeout_succeeds(self) -> None:
        with PkceCallbackServer(expected_state="expected-state") as server:
            _get_after(0.05, server.redirect_uri, {"code": "on-time", "state": "expected-state"})
            result = server.wait(timeout=0.3)

        assert result.code == "on-time"

    def test_callback_arriving_just_after_timeout_is_not_observed(self) -> None:
        """A callback scheduled to land after the (short, injected) timeout must
        not be picked up -- wait() raises PkceCallbackTimeout on schedule."""
        with PkceCallbackServer(expected_state="expected-state") as server:
            _get_after(0.4, server.redirect_uri, {"code": "too-late", "state": "expected-state"})

            with pytest.raises(PkceCallbackTimeout):
                server.wait(timeout=0.1)


class TestPkceCallbackServerLifecycle:
    """Context manager teardown and idempotent close()."""

    def test_close_is_idempotent(self) -> None:
        server = PkceCallbackServer(expected_state="s")
        server.close()
        server.close()  # must not raise

    def test_context_manager_closes_on_exception(self) -> None:
        with pytest.raises(RuntimeError), PkceCallbackServer(expected_state="s") as server:
            port = server._httpd.server_address[1]
            raise RuntimeError("boom")

        # The port should be free again after teardown.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
        finally:
            probe.close()

    def test_failing_thread_start_does_not_leak_the_bound_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`__init__` binds before it starts the serving thread, and `__exit__`
        only runs once `__init__` returns -- so a `RuntimeError: can't start new
        thread` (thread exhaustion) would otherwise leave a listener bound with
        no owner left to close it. The port must be free again afterwards."""
        bound: list[pkce._CallbackHTTPServer] = []
        real_bind = PkceCallbackServer._bind

        def capturing_bind(expected_state: str) -> pkce._CallbackHTTPServer:
            httpd = real_bind(expected_state)
            bound.append(httpd)
            return httpd

        def refuse_to_start(_self: threading.Thread) -> None:
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(PkceCallbackServer, "_bind", staticmethod(capturing_bind))
        monkeypatch.setattr(threading.Thread, "start", refuse_to_start)

        with pytest.raises(RuntimeError, match="can't start new thread"):
            PkceCallbackServer(expected_state="s")

        assert len(bound) == 1
        port = bound[0].server_address[1]
        monkeypatch.undo()  # restore Thread.start before touching sockets
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
        finally:
            probe.close()

    def test_bind_failure_raises_pkce_setup_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            pkce,
            "_LOOPBACK_CANDIDATES",
            (("240.0.0.1", socket.AF_INET), ("240.0.0.2", socket.AF_INET6)),
        )
        with pytest.raises(PkceSetupError):
            PkceCallbackServer(expected_state="s")

    def test_bind_falls_back_to_ipv6_when_ipv4_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            pkce,
            "_LOOPBACK_CANDIDATES",
            (("240.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)),
        )
        try:
            server = PkceCallbackServer(expected_state="s")
        except PkceSetupError:
            pytest.skip("IPv6 loopback is unavailable in this environment")
        try:
            assert server.redirect_uri.startswith("http://[::1]:")
        finally:
            server.close()
