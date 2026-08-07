"""Tests for `AuthClient` (Layer 3, programmatic auth / browser login).

Covers the RFC 8628 device-poll matrix, PKCE code exchange body, refresh's
`invalid_grant` -> `SESSION_EXPIRED` mapping, its bounded attempt budget and the
rotation-deadlock replay that is the one exception to it, the 404 ->
feature-flag mapping shared by every auth endpoint, the per-request bearer
header on introspect, and revoke's public (no-Authorization, JSON-body)
contract including its never-raises uncertain-result path.
"""

from __future__ import annotations

import json
import logging
import math
import sys
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from keboola_agent_cli.auth import auth_client as auth_client_module
from keboola_agent_cli.auth.auth_client import AuthClient
from keboola_agent_cli.auth.models import (
    CliTokenResponse,
    DeviceAuthorization,
    DevicePollStatus,
    IntrospectResponse,
    PatCreateResult,
    RevokeResult,
    SudoChallengeResult,
    SudoResult,
)
from keboola_agent_cli.commands._helpers import map_error_to_exit_code
from keboola_agent_cli.constants import (
    AUTH_LOCK_TIMEOUT,
    AUTH_REFRESH_CONTENTION_DEFAULT_DELAY,
    AUTH_REFRESH_CONTENTION_MAX_DELAY,
    AUTH_REFRESH_CONTENTION_RETRIES,
    AUTH_REFRESH_CONTENTION_STRING_CODE,
    AUTH_REFRESH_MAX_WALL_CLOCK,
    AUTH_REFRESH_TIMEOUT,
)
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError

STACK_URL = "https://connection.keboola.com"


def _make_client() -> AuthClient:
    return AuthClient(STACK_URL)


# ----------------------------------------------------------------------------
# authorize_url
# ----------------------------------------------------------------------------


class TestAuthorizeUrl:
    def test_builds_expected_query(self) -> None:
        """No HTTP request is sent; the query string carries every required
        PKCE parameter, correctly URL-encoded."""
        client = _make_client()
        try:
            url = client.authorize_url(
                redirect_uri="http://127.0.0.1:54321/callback",
                code_challenge="abc123~-._XYZ",
                state="state-with-special-chars/+=",
            )
        finally:
            client.close()

        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.netloc == "connection.keboola.com"
        assert parsed.path == "/admin/auth/pkce/authorize"

        params = parse_qs(parsed.query)
        assert params["responseType"] == ["code"]
        assert params["clientId"] == ["keboola-cli"]
        assert params["redirectUri"] == ["http://127.0.0.1:54321/callback"]
        assert params["codeChallenge"] == ["abc123~-._XYZ"]
        assert params["codeChallengeMethod"] == ["S256"]
        assert params["state"] == ["state-with-special-chars/+="]


# ----------------------------------------------------------------------------
# PKCE code exchange
# ----------------------------------------------------------------------------


class TestExchangePkceCode:
    def test_sends_exact_body_and_parses_response(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/pkce/token",
            method="POST",
            json={
                "accessToken": "kbc_at_abc",
                "refreshToken": "kbc_rt_def",
                "tokenType": "Bearer",
                "expiresIn": 3600,
                "sessionId": "sess-1",
                "user": {"id": 42, "email": "user@example.com", "name": "User"},
            },
        )

        client = _make_client()
        try:
            result = client.exchange_pkce_code(
                code="kbc_ac_code",
                state="the-state",
                redirect_uri="http://127.0.0.1:12345/callback",
                code_verifier="verifier-value",
            )
        finally:
            client.close()

        assert isinstance(result, CliTokenResponse)
        assert result.access_token == "kbc_at_abc"
        assert result.refresh_token == "kbc_rt_def"
        assert result.session_id == "sess-1"
        assert result.user is not None
        assert result.user.id == "42"

        request = httpx_mock.get_requests()[0]
        assert request.method == "POST"
        body = request.read().decode()
        import json as _json

        payload = _json.loads(body)
        assert payload == {
            "clientId": "keboola-cli",
            "code": "kbc_ac_code",
            "state": "the-state",
            "redirectUri": "http://127.0.0.1:12345/callback",
            "codeVerifier": "verifier-value",
        }


# ----------------------------------------------------------------------------
# Device authorization start
# ----------------------------------------------------------------------------


class TestStartDeviceAuthorization:
    def test_returns_device_authorization(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/device",
            method="POST",
            json={
                "deviceCode": "dev-code-1",
                "userCode": "ABCD-1234",
                "verificationUri": "https://connection.keboola.com/device",
                "verificationUriComplete": "https://connection.keboola.com/device?user_code=ABCD-1234",
                "expiresIn": 900,
                "interval": 5,
            },
        )

        client = _make_client()
        try:
            result = client.start_device_authorization()
        finally:
            client.close()

        assert isinstance(result, DeviceAuthorization)
        assert result.device_code == "dev-code-1"
        assert result.user_code == "ABCD-1234"
        assert result.interval == 5

        request = httpx_mock.get_requests()[0]
        import json as _json

        assert _json.loads(request.read().decode()) == {
            "clientId": "keboola-cli",
            "scope": {"credentialType": "session"},
        }


# ----------------------------------------------------------------------------
# Device-token polling matrix
# ----------------------------------------------------------------------------


class TestPollDeviceToken:
    def test_pending(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/device/token",
            method="POST",
            json={"error": "authorization_pending", "params": {"error": "authorization_pending"}},
            status_code=400,
        )
        client = _make_client()
        try:
            result = client.poll_device_token("dev-code-1")
        finally:
            client.close()

        assert result.status == DevicePollStatus.PENDING
        assert result.tokens is None

        request = httpx_mock.get_requests()[0]
        import json as _json

        assert _json.loads(request.read().decode()) == {
            "clientId": "keboola-cli",
            "deviceCode": "dev-code-1",
        }

    def test_slow_down_carries_new_interval(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/device/token",
            method="POST",
            json={
                "error": "slow_down",
                "params": {"error": "slow_down", "interval": 10},
            },
            status_code=400,
        )
        client = _make_client()
        try:
            result = client.poll_device_token("dev-code-1")
        finally:
            client.close()

        assert result.status == DevicePollStatus.SLOW_DOWN
        assert result.interval == 10

    def test_slow_down_without_interval_in_body(self, httpx_mock) -> None:
        """No `params.interval` in the body -> interval is None; the caller
        (auth/device.py) applies its own increment/cap."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/device/token",
            method="POST",
            json={"error": "slow_down"},
            status_code=400,
        )
        client = _make_client()
        try:
            result = client.poll_device_token("dev-code-1")
        finally:
            client.close()

        assert result.status == DevicePollStatus.SLOW_DOWN
        assert result.interval is None

    def test_success(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/device/token",
            method="POST",
            json={
                "accessToken": "kbc_at_final",
                "refreshToken": "kbc_rt_final",
                "expiresIn": 3600,
                "sessionId": "sess-2",
            },
            status_code=200,
        )
        client = _make_client()
        try:
            result = client.poll_device_token("dev-code-1")
        finally:
            client.close()

        assert result.status == DevicePollStatus.OK
        assert result.tokens is not None
        assert result.tokens.access_token == "kbc_at_final"

    def test_denied(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/device/token",
            method="POST",
            json={"error": "access_denied", "params": {"error": "access_denied"}},
            status_code=400,
        )
        client = _make_client()
        try:
            result = client.poll_device_token("dev-code-1")
        finally:
            client.close()

        assert result.status == DevicePollStatus.DENIED

    def test_expired(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/device/token",
            method="POST",
            json={"error": "expired_token", "params": {"error": "expired_token"}},
            status_code=400,
        )
        client = _make_client()
        try:
            result = client.poll_device_token("dev-code-1")
        finally:
            client.close()

        assert result.status == DevicePollStatus.EXPIRED

    def test_unknown_error_token_maps_to_error(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/device/token",
            method="POST",
            json={"error": "invalid_grant", "params": {"error": "invalid_grant"}},
            status_code=400,
        )
        client = _make_client()
        try:
            result = client.poll_device_token("dev-code-1")
        finally:
            client.close()

        assert result.status == DevicePollStatus.ERROR

    def test_http_429_maps_to_slow_down(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/device/token",
            method="POST",
            json={"error": "rate_limit_exceeded"},
            status_code=429,
        )
        client = _make_client()
        try:
            result = client.poll_device_token("dev-code-1")
        finally:
            client.close()

        assert result.status == DevicePollStatus.SLOW_DOWN

    def test_malformed_body_never_crashes(self, httpx_mock) -> None:
        """A response that is not valid JSON at all must come back as a typed
        ERROR result, never propagate a parsing exception."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/device/token",
            method="POST",
            content=b"not json at all",
            status_code=400,
            headers={"Content-Type": "text/plain"},
        )
        client = _make_client()
        try:
            result = client.poll_device_token("dev-code-1")
        finally:
            client.close()

        assert result.status == DevicePollStatus.ERROR
        assert result.message

    def test_malformed_success_body_never_crashes(self, httpx_mock) -> None:
        """A 2xx response whose body cannot be parsed into a CliTokenResponse
        (missing required fields) must not crash the poll loop."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/device/token",
            method="POST",
            json={"unexpected": "shape"},
            status_code=200,
        )
        client = _make_client()
        try:
            result = client.poll_device_token("dev-code-1")
        finally:
            client.close()

        assert result.status == DevicePollStatus.ERROR
        assert result.message

    def test_404_raises_not_supported(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/device/token",
            method="POST",
            status_code=404,
            json={"error": "Not Found"},
        )
        client = _make_client()
        try:
            with pytest.raises(KeboolaApiError) as excinfo:
                client.poll_device_token("dev-code-1")
        finally:
            client.close()

        assert excinfo.value.error_code == ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK


# ----------------------------------------------------------------------------
# refresh
# ----------------------------------------------------------------------------


class TestRefresh:
    def test_success_sends_expected_body(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
            json={
                "accessToken": "kbc_at_new",
                "refreshToken": "kbc_rt_new",
                "expiresIn": 3600,
                "sessionId": "sess-3",
            },
        )
        client = _make_client()
        try:
            result = client.refresh("kbc_rt_old")
        finally:
            client.close()

        assert result.access_token == "kbc_at_new"
        request = httpx_mock.get_requests()[0]
        import json as _json

        assert _json.loads(request.read().decode()) == {"refreshToken": "kbc_rt_old"}

    def test_invalid_grant_maps_to_session_expired(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
            json={"error": "invalid_grant", "message": "invalid_grant"},
            status_code=400,
        )
        client = _make_client()
        try:
            with pytest.raises(KeboolaApiError) as excinfo:
                client.refresh("kbc_rt_replayed")
        finally:
            client.close()

        assert excinfo.value.error_code == ErrorCode.SESSION_EXPIRED
        # No token value leaked into the exception message.
        assert "kbc_rt_replayed" not in excinfo.value.message

    def test_invalid_grant_on_401_also_maps_to_session_expired(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
            json={"error": "invalid_grant"},
            status_code=401,
        )
        client = _make_client()
        try:
            with pytest.raises(KeboolaApiError) as excinfo:
                client.refresh("kbc_rt_replayed")
        finally:
            client.close()

        assert excinfo.value.error_code == ErrorCode.SESSION_EXPIRED

    def test_production_401_prose_maps_to_session_expired(self, httpx_mock) -> None:
        """The shape production actually returns -- 401 with NO `invalid_grant`.

        Regression test for a real failure on `connection.keboola.com`: a
        revoked/replayed refresh token comes back as 401 with the body
        message "Invalid refresh token." and no OAuth error token anywhere.
        The original substring-only check missed it, so the error fell
        through to the generic `INVALID_TOKEN` mapping. The consequences
        were severe and compounding: the user got an opaque "Invalid or
        expired token" with no remedy, `_perform_refresh` did not purge the
        dead session (it purges only on `SESSION_EXPIRED`), so every later
        command failed identically forever, and `auth status` -- which
        re-raises anything that is neither `SESSION_EXPIRED` nor a network
        code -- crashed instead of reporting "expired".
        """
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
            json={"error": "Unauthorized", "message": "Invalid refresh token."},
            status_code=401,
        )
        client = _make_client()
        try:
            with pytest.raises(KeboolaApiError) as excinfo:
                client.refresh("kbc_rt_revoked")
        finally:
            client.close()

        assert excinfo.value.error_code == ErrorCode.SESSION_EXPIRED
        assert "kbagent auth login" in excinfo.value.message
        assert "kbc_rt_revoked" not in excinfo.value.message

    def test_bare_401_with_no_message_maps_to_session_expired(self, httpx_mock) -> None:
        """A 401 needs no marker at all -- the body is the only credential."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
            text="",
            status_code=401,
        )
        client = _make_client()
        try:
            with pytest.raises(KeboolaApiError) as excinfo:
                client.refresh("kbc_rt_dead")
        finally:
            client.close()

        assert excinfo.value.error_code == ErrorCode.SESSION_EXPIRED

    def test_400_prose_without_oauth_token_maps_to_session_expired(self, httpx_mock) -> None:
        """A 400 carrying grant prose (but no `invalid_grant`) still counts."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
            json={"message": "Expired refresh token"},
            status_code=400,
        )
        client = _make_client()
        try:
            with pytest.raises(KeboolaApiError) as excinfo:
                client.refresh("kbc_rt_old")
        finally:
            client.close()

        assert excinfo.value.error_code == ErrorCode.SESSION_EXPIRED

    @pytest.mark.parametrize(
        "message",
        [
            "The refresh token has expired.",
            "Refresh token expired.",
            "Refresh token revoked.",
            "Refresh token family revoked.",
            "This refresh token is no longer valid.",
            "Unknown refreshToken.",
        ],
    )
    def test_400_verdict_on_the_token_maps_to_session_expired(
        self, httpx_mock, message: str
    ) -> None:
        """Word order and verb form vary by server; the verdict is what decides.

        Fixed phrases would miss all of these, leaving a dead session unpurged so
        every later command repeats one opaque error forever.
        """
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
            json={"message": message},
            status_code=400,
        )
        client = _make_client()
        try:
            with pytest.raises(KeboolaApiError) as excinfo:
                client.refresh("kbc_rt_old")
        finally:
            client.close()

        assert excinfo.value.error_code == ErrorCode.SESSION_EXPIRED

    @pytest.mark.parametrize(
        "message",
        [
            "The refresh token field contains an invalid character.",
            "Missing parameter: refresh token is unknown to this request schema.",
            "refreshToken field is invalid: value must not exceed 512 characters.",
            "Malformed refreshToken parameter.",
        ],
    )
    def test_400_complaining_about_the_request_shape_is_not_remapped(
        self, httpx_mock, message: str
    ) -> None:
        """ "invalid" and "unknown" describe a malformed field, not only a dead token.

        Each of these names the refresh token and carries a verdict word, so a
        subject-plus-verdict rule alone classifies them as a rejected grant and
        `SessionTokenProvider` deletes the session -- turning a bug on our own side
        (a truncated or mis-encoded value) into a forced browser re-login.
        """
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
            json={"message": message},
            status_code=400,
        )
        client = _make_client()
        try:
            with pytest.raises(KeboolaApiError) as excinfo:
                client.refresh("kbc_rt_valid")
        finally:
            client.close()

        assert excinfo.value.error_code != ErrorCode.SESSION_EXPIRED

    def test_400_naming_the_field_without_a_verdict_is_not_remapped(self, httpx_mock) -> None:
        """A validation error about the field is our own bug, not a dead token.

        `SessionTokenProvider` purges `auth.json` on `SESSION_EXPIRED`, so
        classifying a malformed-request 400 that way would force an avoidable
        re-login on a refresh token the server never rejected.
        """
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
            json={"message": "The refresh token must be a string."},
            status_code=400,
        )
        client = _make_client()
        try:
            with pytest.raises(KeboolaApiError) as excinfo:
                client.refresh("kbc_rt_valid")
        finally:
            client.close()

        assert excinfo.value.error_code != ErrorCode.SESSION_EXPIRED

    def test_other_400_is_not_remapped(self, httpx_mock) -> None:
        """A 400 unrelated to invalid_grant keeps the generic error mapping."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
            json={"error": "some_other_problem"},
            status_code=400,
        )
        client = _make_client()
        try:
            with pytest.raises(KeboolaApiError) as excinfo:
                client.refresh("kbc_rt_old")
        finally:
            client.close()

        assert excinfo.value.error_code != ErrorCode.SESSION_EXPIRED


# ----------------------------------------------------------------------------
# refresh: the lock-hold budget (single attempt + short timeout)
# ----------------------------------------------------------------------------


class TestRefreshLockHoldBudget:
    """`refresh` runs inside `auth.json.lock`, so its wall clock is bounded.

    Every other holder of that lock waits only `AUTH_LOCK_TIMEOUT` before
    reporting it as stuck (a `ConfigError`, exit 5, blaming a "stuck" process
    that is merely slow). The retry loop `refresh` would otherwise inherit
    allows 3 x 30 s reads plus 1 s + 2 s backoff -- three times the acquire
    timeout. These tests pin the two properties that keep the hold short.
    """

    def test_budget_leaves_clear_headroom_under_the_lock_acquire_timeout(self) -> None:
        """Relationship between the two constants, so a later bump cannot close the gap.

        This asserts arithmetic only. That the ceiling is actually *enforced* --
        the httpx per-phase timeouts cannot do it -- is asserted where the lock
        is held, in `tests/test_token_provider.py::TestRefreshWallClockCeiling`.
        """
        assert AUTH_REFRESH_MAX_WALL_CLOCK * 2 <= AUTH_LOCK_TIMEOUT

    def test_request_carries_the_constrained_timeout(self, httpx_mock) -> None:
        """Per-request, so `introspect` and the login flows keep DEFAULT_TIMEOUT."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
            json={"accessToken": "kbc_at_new", "refreshToken": "kbc_rt_new", "expiresIn": 3600},
        )
        client = _make_client()
        try:
            client.refresh("kbc_rt_old")
        finally:
            client.close()

        assert httpx_mock.get_requests()[0].extensions["timeout"] == AUTH_REFRESH_TIMEOUT.as_dict()

    def test_retryable_status_is_not_retried(self, httpx_mock) -> None:
        """A 503 fails immediately instead of burning 3 attempts + 3 s of backoff."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
            status_code=503,
            json={"error": "Service Unavailable"},
        )
        client = _make_client()
        started = time.monotonic()
        try:
            with pytest.raises(KeboolaApiError):
                client.refresh("kbc_rt_old")
        finally:
            client.close()
        elapsed = time.monotonic() - started

        assert len(httpx_mock.get_requests()) == 1
        # The shared retry loop would sleep 1 s then 2 s between attempts.
        assert elapsed < 1.0

    def test_timeout_is_reported_as_a_network_error(self, httpx_mock) -> None:
        """Truthful classification: a slow auth service is a network problem (exit 4),
        not a session problem (exit 3) and not a stuck lock (exit 5)."""
        httpx_mock.add_exception(
            httpx.ReadTimeout("read timed out"),
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
        )
        client = _make_client()
        try:
            with pytest.raises(KeboolaApiError) as excinfo:
                client.refresh("kbc_rt_slow")
        finally:
            client.close()

        assert excinfo.value.error_code == ErrorCode.TIMEOUT
        assert map_error_to_exit_code(excinfo.value) == 4
        assert len(httpx_mock.get_requests()) == 1
        assert "kbc_rt_slow" not in excinfo.value.message

    def test_connect_failure_is_reported_as_a_network_error(self, httpx_mock) -> None:
        httpx_mock.add_exception(
            httpx.ConnectError("no route to host"),
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
        )
        client = _make_client()
        try:
            with pytest.raises(KeboolaApiError) as excinfo:
                client.refresh("kbc_rt_old")
        finally:
            client.close()

        assert excinfo.value.error_code == ErrorCode.CONNECTION_ERROR
        assert map_error_to_exit_code(excinfo.value) == 4


# ----------------------------------------------------------------------------
# refresh: the rotation-deadlock retry
# ----------------------------------------------------------------------------


def _contention_response(
    *, code_key: str = "code", retry_after: str | None = "1"
) -> dict[str, Any]:
    """A 503 shaped like the server's rotation-deadlock answer."""
    body: dict[str, Any] = {"message": "Refresh contention, retry the request."}
    if code_key == "exception.code":
        body["exception"] = {"code": AUTH_REFRESH_CONTENTION_STRING_CODE}
    else:
        body[code_key] = AUTH_REFRESH_CONTENTION_STRING_CODE
    response: dict[str, Any] = {
        "url": f"{STACK_URL}/v1/auth/token/refresh",
        "method": "POST",
        "status_code": 503,
        "json": body,
    }
    if retry_after is not None:
        response["headers"] = {"Retry-After": retry_after}
    return response


class TestRefreshContentionRetry:
    """A rotation deadlock is the one refresh failure that may be replayed.

    The server rolls the whole rotation back, so the submitted token is still
    the session's current one -- the reasoning that forbids a blanket retry is
    what permits this one. These tests pin both edges: the deadlock is retried,
    and nothing else is.
    """

    def test_a_contention_503_is_retried_and_can_succeed(self, httpx_mock, monkeypatch) -> None:
        """The whole point: a transient deadlock resolves without user action."""
        monkeypatch.setattr(auth_client_module.time, "sleep", lambda _seconds: None)
        httpx_mock.add_response(**_contention_response())
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
            json={"accessToken": "kbc_at_new", "refreshToken": "kbc_rt_new", "expiresIn": 3600},
        )
        client = _make_client()
        try:
            result = client.refresh("kbc_rt_old")
        finally:
            client.close()

        assert result.access_token == "kbc_at_new"
        assert len(httpx_mock.get_requests()) == 2

    def test_the_replay_presents_the_same_token(self, httpx_mock, monkeypatch) -> None:
        """Safe only because nothing rotated -- a fresh token would 401 instead."""
        monkeypatch.setattr(auth_client_module.time, "sleep", lambda _seconds: None)
        httpx_mock.add_response(**_contention_response())
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
            json={"accessToken": "kbc_at_new", "refreshToken": "kbc_rt_new", "expiresIn": 3600},
        )
        client = _make_client()
        try:
            client.refresh("kbc_rt_old")
        finally:
            client.close()

        bodies = [json.loads(request.content) for request in httpx_mock.get_requests()]
        assert [body["refreshToken"] for body in bodies] == ["kbc_rt_old", "kbc_rt_old"]

    @pytest.mark.parametrize("code_key", ["code", "stringCode", "exception.code"])
    def test_every_body_shape_the_serializers_emit_is_recognised(
        self, httpx_mock, monkeypatch, code_key: str
    ) -> None:
        """The key depends on the serializer in front of the endpoint.

        Reading only one of the three would silently turn the retry off on
        whichever stacks use the other shapes.
        """
        monkeypatch.setattr(auth_client_module.time, "sleep", lambda _seconds: None)
        httpx_mock.add_response(**_contention_response(code_key=code_key))
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
            json={"accessToken": "kbc_at_new", "refreshToken": "kbc_rt_new", "expiresIn": 3600},
        )
        client = _make_client()
        try:
            client.refresh("kbc_rt_old")
        finally:
            client.close()

        assert len(httpx_mock.get_requests()) == 2

    def test_a_503_without_the_contention_code_is_not_retried(self, httpx_mock) -> None:
        """A proxy 503 or load shedding proves nothing about whether the rotation committed.

        Replaying then risks presenting a token the server has already rotated
        out, which past its grace window is a proven replay -- family revocation.
        """
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
            status_code=503,
            json={"code": "storage.maintenance", "message": "Service Unavailable"},
        )
        client = _make_client()
        try:
            with pytest.raises(KeboolaApiError):
                client.refresh("kbc_rt_old")
        finally:
            client.close()

        assert len(httpx_mock.get_requests()) == 1

    def test_the_contention_code_on_another_status_is_not_retried(self, httpx_mock) -> None:
        """The proof is the pair, not the code alone."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
            status_code=500,
            json={"code": AUTH_REFRESH_CONTENTION_STRING_CODE},
        )
        client = _make_client()
        try:
            with pytest.raises(KeboolaApiError):
                client.refresh("kbc_rt_old")
        finally:
            client.close()

        assert len(httpx_mock.get_requests()) == 1

    def test_a_persistent_deadlock_gives_up_and_never_purges_the_session(
        self, httpx_mock, monkeypatch
    ) -> None:
        """Contention is not a credential verdict.

        Classifying it as one would purge a live session from auth.json and force
        a browser re-login over a transient database lock.
        """
        monkeypatch.setattr(auth_client_module.time, "sleep", lambda _seconds: None)
        for _ in range(AUTH_REFRESH_CONTENTION_RETRIES + 1):
            httpx_mock.add_response(**_contention_response())
        client = _make_client()
        try:
            with pytest.raises(KeboolaApiError) as excinfo:
                client.refresh("kbc_rt_old")
        finally:
            client.close()

        assert len(httpx_mock.get_requests()) == AUTH_REFRESH_CONTENTION_RETRIES + 1
        assert excinfo.value.error_code != ErrorCode.SESSION_EXPIRED
        assert excinfo.value.retryable is True

    @pytest.mark.parametrize("code_key", ["code", "stringCode", "exception.code"])
    def test_giving_up_says_what_to_do_whatever_shape_the_body_took(
        self, httpx_mock, monkeypatch, code_key: str
    ) -> None:
        """The user-facing message must not depend on the stack's serializer.

        The generic extractor prefers an `exception` key over `message`, so on the
        nested shape the mapped message is a JSON fragment rather than prose --
        leaving the user with a 503 and nothing to act on. The server's own detail
        stays reachable as the cause.
        """
        monkeypatch.setattr(auth_client_module.time, "sleep", lambda _seconds: None)
        for _ in range(AUTH_REFRESH_CONTENTION_RETRIES + 1):
            httpx_mock.add_response(**_contention_response(code_key=code_key))
        client = _make_client()
        try:
            with pytest.raises(KeboolaApiError) as excinfo:
                client.refresh("kbc_rt_old")
        finally:
            client.close()

        assert "Run the command again." in excinfo.value.message
        assert "{" not in excinfo.value.message
        assert AUTH_REFRESH_CONTENTION_STRING_CODE not in excinfo.value.message
        assert isinstance(excinfo.value.__cause__, KeboolaApiError)

    def test_the_retry_decision_is_traceable_in_the_logs(
        self, httpx_mock, monkeypatch, caplog
    ) -> None:
        """The shared retry loop logs every decision; this one opts out of that loop.

        Without an equivalent trace, a stack seeing repeated rotation deadlocks
        leaves nothing in kbagent's own logs to distinguish "retried" from "not
        contention" from "gave up".
        """
        monkeypatch.setattr(auth_client_module.time, "sleep", lambda _seconds: None)
        for _ in range(AUTH_REFRESH_CONTENTION_RETRIES + 1):
            httpx_mock.add_response(**_contention_response())
        client = _make_client()
        try:
            with (
                caplog.at_level(logging.DEBUG, logger=auth_client_module.logger.name),
                pytest.raises(KeboolaApiError),
            ):
                client.refresh("kbc_rt_old")
        finally:
            client.close()

        messages = [record.getMessage() for record in caplog.records]
        assert any("replaying refresh" in message for message in messages)
        assert any("giving up" in message for message in messages)
        assert not any("kbc_rt_old" in message for message in messages)

    def test_retry_after_is_honoured_but_clamped(self, httpx_mock, monkeypatch) -> None:
        """The wait runs inside the caller's wall-clock ceiling.

        An unclamped header would have the attempt abandoned mid-sleep, turning
        a recoverable deadlock into an abandoned refresh.
        """
        slept: list[float] = []
        monkeypatch.setattr(auth_client_module.time, "sleep", slept.append)
        httpx_mock.add_response(**_contention_response(retry_after="600"))
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
            json={"accessToken": "kbc_at_new", "refreshToken": "kbc_rt_new", "expiresIn": 3600},
        )
        client = _make_client()
        try:
            client.refresh("kbc_rt_old")
        finally:
            client.close()

        assert slept == [AUTH_REFRESH_CONTENTION_MAX_DELAY]

    @pytest.mark.parametrize("retry_after", [None, "Wed, 21 Oct 2026 07:28:00 GMT", ""])
    def test_missing_or_unparseable_retry_after_still_retries(
        self, httpx_mock, monkeypatch, retry_after: str | None
    ) -> None:
        """An HTTP-date header is legal; absent guidance is not a veto.

        Skipping the retry here would discard a recovery the response has already
        proven safe, over a header we only use to pick a delay.
        """
        slept: list[float] = []
        monkeypatch.setattr(auth_client_module.time, "sleep", slept.append)
        httpx_mock.add_response(**_contention_response(retry_after=retry_after))
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
            json={"accessToken": "kbc_at_new", "refreshToken": "kbc_rt_new", "expiresIn": 3600},
        )
        client = _make_client()
        try:
            client.refresh("kbc_rt_old")
        finally:
            client.close()

        assert slept == [AUTH_REFRESH_CONTENTION_DEFAULT_DELAY]
        assert len(httpx_mock.get_requests()) == 2

    @pytest.mark.parametrize("retry_after", ["nan", "inf", "-inf", "1e400", "-5"])
    def test_a_non_finite_retry_after_never_reaches_sleep(
        self, httpx_mock, monkeypatch, retry_after: str
    ) -> None:
        """`float()` accepts "nan" and "inf", and clamping cannot repair a NaN.

        Every comparison against NaN is False, so `min`/`max` pass it straight
        through and `time.sleep(nan)` raises -- an uncaught error out of the
        refresh call, which the lease holder cannot classify. The delay handed to
        `sleep` must always be a finite, non-negative number.
        """
        slept: list[float] = []
        monkeypatch.setattr(auth_client_module.time, "sleep", slept.append)
        httpx_mock.add_response(**_contention_response(retry_after=retry_after))
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
            json={"accessToken": "kbc_at_new", "refreshToken": "kbc_rt_new", "expiresIn": 3600},
        )
        client = _make_client()
        try:
            client.refresh("kbc_rt_old")
        finally:
            client.close()

        assert len(slept) == 1
        assert math.isfinite(slept[0])
        assert 0.0 <= slept[0] <= AUTH_REFRESH_CONTENTION_MAX_DELAY
        # Real sleep, not the fake: a NaN would raise here rather than assert.
        time.sleep(slept[0] * 0)

    def test_a_json_body_nested_past_the_recursion_limit_is_not_retried(self, httpx_mock) -> None:
        """Malformed and too-deep bodies defeat the decoder via different exceptions.

        `json.JSONDecodeError` is a `ValueError`; exceeding the recursion limit
        raises `RecursionError`, a `RuntimeError`. Letting the second escape
        propagates out of the refresh call as neither `_AbandonedRefresh` nor
        `KeboolaApiError`, so the lease holder cannot classify it.
        """
        depth = sys.getrecursionlimit() * 20
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
            status_code=503,
            headers={"Content-Type": "application/json"},
            content=b"[" * depth + b"]" * depth,
        )
        client = _make_client()
        try:
            with pytest.raises(KeboolaApiError):
                client.refresh("kbc_rt_old")
        finally:
            client.close()

        assert len(httpx_mock.get_requests()) == 1

    def test_a_non_json_503_body_is_not_retried(self, httpx_mock) -> None:
        """An unclassifiable body is not a match -- HTML from a proxy must not retry."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/refresh",
            method="POST",
            status_code=503,
            text="<html><body>503 Service Unavailable</body></html>",
        )
        client = _make_client()
        try:
            with pytest.raises(KeboolaApiError):
                client.refresh("kbc_rt_old")
        finally:
            client.close()

        assert len(httpx_mock.get_requests()) == 1


# ----------------------------------------------------------------------------
# introspect
# ----------------------------------------------------------------------------


class TestIntrospect:
    def test_sends_bearer_header(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/introspect",
            method="GET",
            json={
                "active": True,
                "sessionId": "sess-4",
                "user": {"id": "1", "email": "a@b.com", "name": "A"},
                "projects": [{"id": 10105, "name": "Demo", "role": "admin"}],
            },
        )
        client = _make_client()
        try:
            result = client.introspect("kbc_at_live")
        finally:
            client.close()

        assert isinstance(result, IntrospectResponse)
        assert result.active is True
        assert result.projects[0].id == 10105

        request = httpx_mock.get_requests()[0]
        assert request.headers["Authorization"] == "Bearer kbc_at_live"


# ----------------------------------------------------------------------------
# revoke
# ----------------------------------------------------------------------------


class TestRevoke:
    def test_sends_body_and_no_authorization_header(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/revoke",
            method="POST",
            status_code=200,
            json={"revoked": True},
        )
        client = _make_client()
        try:
            result = client.revoke("kbc_rt_old")
        finally:
            client.close()

        assert isinstance(result, RevokeResult)
        assert result.confirmed is True

        request = httpx_mock.get_requests()[0]
        import json as _json

        assert _json.loads(request.read().decode()) == {
            "token": "kbc_rt_old",
            "tokenTypeHint": "refreshToken",
        }
        assert "Authorization" not in request.headers

    def test_custom_token_type_hint(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/revoke",
            method="POST",
            status_code=200,
            json={"revoked": True},
        )
        client = _make_client()
        try:
            client.revoke("kbc_at_old", token_type_hint="accessToken")
        finally:
            client.close()

        import json as _json

        request = httpx_mock.get_requests()[0]
        assert _json.loads(request.read().decode()) == {
            "token": "kbc_at_old",
            "tokenTypeHint": "accessToken",
        }

    def test_server_error_never_raises(self, httpx_mock) -> None:
        """A 500 from the revoke endpoint must not raise -- logout has to
        continue with local cleanup and report the uncertainty instead."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/revoke",
            method="POST",
            status_code=500,
            json={"error": "internal error"},
        )
        client = _make_client()
        try:
            result = client.revoke("kbc_rt_old")
        finally:
            client.close()

        assert result.confirmed is False
        assert result.message

    def test_404_never_raises_either(self, httpx_mock) -> None:
        """Even the feature-flag 404 case must come back as a RevokeResult,
        not an exception -- revoke() has its own bypass path, not the shared
        `_do_request`-based 404 mapping."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/token/revoke",
            method="POST",
            status_code=404,
            json={"error": "Not Found"},
        )
        client = _make_client()
        try:
            result = client.revoke("kbc_rt_old")
        finally:
            client.close()

        assert result.confirmed is False


# ----------------------------------------------------------------------------
# delete_session -- kill a specific server session by id (orphan retry, B-1/B-2)
# ----------------------------------------------------------------------------


class TestDeleteSession:
    def test_sends_delete_with_bearer_header(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/sessions/sess-orphan-1",
            method="DELETE",
            status_code=204,
        )
        client = _make_client()
        try:
            result = client.delete_session("sess-orphan-1", "kbc_at_live")
        finally:
            client.close()

        assert isinstance(result, RevokeResult)
        assert result.confirmed is True

        request = httpx_mock.get_requests()[0]
        assert request.method == "DELETE"
        assert request.headers["Authorization"] == "Bearer kbc_at_live"

    def test_404_is_treated_as_already_gone(self, httpx_mock) -> None:
        """A 404 on a SPECIFIC session id most likely means it is already
        gone (revoked, expired, or never existed) -- the desired end state
        was already reached, so this must be `confirmed=True`, not a
        feature-flag error like the other auth endpoints' 404 mapping."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/sessions/sess-orphan-1",
            method="DELETE",
            status_code=404,
            json={"error": "Not Found"},
        )
        client = _make_client()
        try:
            result = client.delete_session("sess-orphan-1", "kbc_at_live")
        finally:
            client.close()

        assert result.confirmed is True

    def test_server_error_never_raises(self, httpx_mock) -> None:
        """A 500 must not raise -- logout has to continue with local cleanup
        and report the uncertainty instead."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/sessions/sess-orphan-1",
            method="DELETE",
            status_code=500,
            json={"error": "internal error"},
        )
        client = _make_client()
        try:
            result = client.delete_session("sess-orphan-1", "kbc_at_live")
        finally:
            client.close()

        assert result.confirmed is False
        assert result.message

    def test_network_error_never_raises(self, httpx_mock) -> None:
        import httpx

        httpx_mock.add_exception(httpx.ConnectError("boom"))
        client = _make_client()
        try:
            result = client.delete_session("sess-orphan-1", "kbc_at_live")
        finally:
            client.close()

        assert result.confirmed is False
        assert "boom" in result.message


# ----------------------------------------------------------------------------
# 404 -> AUTH_NOT_SUPPORTED_ON_STACK on every endpoint that maps errors
# ----------------------------------------------------------------------------


class Test404OnEveryEndpoint:
    @pytest.mark.parametrize(
        "path",
        [
            "/v1/auth/pkce/token",
            "/v1/auth/device",
            "/v1/auth/token/refresh",
            "/v1/auth/token/introspect",
        ],
    )
    def test_404_maps_to_not_supported(self, httpx_mock, path: str) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}{path}",
            method="POST" if path != "/v1/auth/token/introspect" else "GET",
            status_code=404,
            json={"error": "Not Found"},
        )
        client = _make_client()
        try:
            with pytest.raises(KeboolaApiError) as excinfo:
                if path == "/v1/auth/pkce/token":
                    client.exchange_pkce_code(
                        code="c",
                        state="s",
                        redirect_uri="http://127.0.0.1:1/callback",
                        code_verifier="v",
                    )
                elif path == "/v1/auth/device":
                    client.start_device_authorization()
                elif path == "/v1/auth/token/refresh":
                    client.refresh("kbc_rt_old")
                else:
                    client.introspect("kbc_at_old")
        finally:
            client.close()

        assert excinfo.value.error_code == ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK
        assert STACK_URL in excinfo.value.message


class TestSudoTotp:
    def test_verified_stamps_bearer_and_body(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/sudo",
            method="POST",
            status_code=200,
            json={
                "sudoVerified": True,
                "sudoExpiresAt": "2026-01-01T00:05:00Z",
                "sudoTimeoutSeconds": 300,
            },
        )
        client = _make_client()
        try:
            result = client.sudo_totp("kbc_at_live", "123456")
        finally:
            client.close()

        assert isinstance(result, SudoResult)
        assert result.verified is True
        assert result.timeout_seconds == 300

        request = httpx_mock.get_requests()[0]
        assert request.headers["Authorization"] == "Bearer kbc_at_live"
        assert json.loads(request.read().decode()) == {"type": "totp", "totpCode": "123456"}

    def test_not_verified(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/sudo",
            method="POST",
            status_code=200,
            json={"sudoVerified": False, "sudoExpiresAt": "", "sudoTimeoutSeconds": 0},
        )
        client = _make_client()
        try:
            result = client.sudo_totp("kbc_at_live", "000000")
        finally:
            client.close()
        assert result.verified is False

    def test_404_maps_to_auth_not_supported(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/sudo", method="POST", status_code=404, json={}
        )
        client = _make_client()
        try:
            with pytest.raises(KeboolaApiError) as excinfo:
                client.sudo_totp("kbc_at_live", "123456")
        finally:
            client.close()
        assert excinfo.value.error_code == ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK


class TestSudoChallenge:
    def test_returns_challenge_token_and_options(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/sudo/challenge",
            method="POST",
            status_code=200,
            json={
                "challengeToken": "kbc_mfa_xyz",
                "options": {"challenge": "abc", "rpId": "keboola.com"},
                "expiresIn": 120,
            },
        )
        client = _make_client()
        try:
            result = client.sudo_challenge("kbc_at_live")
        finally:
            client.close()

        assert isinstance(result, SudoChallengeResult)
        assert result.challenge_token == "kbc_mfa_xyz"
        assert result.options == {"challenge": "abc", "rpId": "keboola.com"}
        assert result.expires_in == 120

        request = httpx_mock.get_requests()[0]
        assert request.headers["Authorization"] == "Bearer kbc_at_live"

    def test_404_maps_to_auth_not_supported(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/sudo/challenge", method="POST", status_code=404, json={}
        )
        client = _make_client()
        try:
            with pytest.raises(KeboolaApiError) as excinfo:
                client.sudo_challenge("kbc_at_live")
        finally:
            client.close()
        assert excinfo.value.error_code == ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK


class TestSudoWebauthn:
    def test_verified_sends_type_and_assertion(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/sudo",
            method="POST",
            status_code=200,
            json={
                "sudoVerified": True,
                "sudoExpiresAt": "2026-01-01T00:05:00Z",
                "sudoTimeoutSeconds": 300,
            },
        )
        client = _make_client()
        try:
            result = client.sudo_webauthn("kbc_at_live", "kbc_mfa_xyz", "fake-assertion-json")
        finally:
            client.close()

        assert result.verified is True

        request = httpx_mock.get_requests()[0]
        assert request.headers["Authorization"] == "Bearer kbc_at_live"
        assert json.loads(request.read().decode()) == {
            "type": "webauthn",
            "challengeToken": "kbc_mfa_xyz",
            "webauthnAssertion": "fake-assertion-json",
        }


class TestCreatePat:
    def test_minimal_request(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/pat",
            method="POST",
            status_code=201,
            json={
                "accessToken": "kbc_pat_abc123",
                "tokenType": "Bearer",
                "expiresIn": 7776000,
                "pat": {
                    "id": "pat-1",
                    "name": "ci-salesforce",
                    "scope": {"all": True},
                    "projects": [],
                    "readOnly": False,
                    "expiresAt": "2026-04-01T00:00:00Z",
                    "createdAt": "2026-01-01T00:00:00Z",
                },
            },
        )
        client = _make_client()
        try:
            result = client.create_pat("kbc_at_live", name="ci-salesforce")
        finally:
            client.close()

        assert isinstance(result, PatCreateResult)
        assert result.access_token == "kbc_pat_abc123"
        assert result.pat.id == "pat-1"
        assert result.pat.read_only is False

        request = httpx_mock.get_requests()[0]
        assert request.headers["Authorization"] == "Bearer kbc_at_live"
        assert json.loads(request.read().decode()) == {"name": "ci-salesforce"}

    def test_read_only_and_ttl_in_body(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/pat",
            method="POST",
            status_code=201,
            json={
                "accessToken": "kbc_pat_ro",
                "expiresIn": 86400,
                "pat": {
                    "id": "pat-2",
                    "name": "n",
                    "scope": {"all": True, "readOnly": True},
                    "projects": [],
                    "readOnly": True,
                    "expiresAt": "2026-01-02T00:00:00Z",
                    "createdAt": "2026-01-01T00:00:00Z",
                },
            },
        )
        client = _make_client()
        try:
            client.create_pat("kbc_at_live", name="n", read_only=True, expires_in=86400)
        finally:
            client.close()

        request = httpx_mock.get_requests()[0]
        assert json.loads(request.read().decode()) == {
            "name": "n",
            "expiresIn": 86400,
            "scope": {"all": True, "readOnly": True},
        }

    def test_project_ids_scope_narrows_to_allow_list(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/pat",
            method="POST",
            status_code=201,
            json={
                "accessToken": "kbc_pat_scoped",
                "expiresIn": 86400,
                "pat": {
                    "id": "pat-3",
                    "name": "n",
                    "scope": {"projects": ["9840"]},
                    "projects": [],
                    "readOnly": False,
                    "expiresAt": "2026-01-02T00:00:00Z",
                    "createdAt": "2026-01-01T00:00:00Z",
                },
            },
        )
        client = _make_client()
        try:
            client.create_pat("kbc_at_live", name="n", project_ids=["9840"])
        finally:
            client.close()

        request = httpx_mock.get_requests()[0]
        assert json.loads(request.read().decode()) == {
            "name": "n",
            "scope": {"projects": ["9840"]},
        }

    def test_sudo_not_active_raises(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/pat",
            method="POST",
            status_code=403,
            json={"error": "Sudo window not active."},
        )
        client = _make_client()
        try:
            with pytest.raises(KeboolaApiError):
                client.create_pat("kbc_at_live", name="n")
        finally:
            client.close()


class TestRevokePat:
    def test_confirmed(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/pat/pat-1", method="DELETE", status_code=204
        )
        client = _make_client()
        try:
            result = client.revoke_pat("kbc_at_live", "pat-1")
        finally:
            client.close()
        assert isinstance(result, RevokeResult)
        assert result.confirmed is True

        request = httpx_mock.get_requests()[0]
        assert request.headers["Authorization"] == "Bearer kbc_at_live"

    def test_already_revoked_404_is_confirmed(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/pat/pat-1", method="DELETE", status_code=404, json={}
        )
        client = _make_client()
        try:
            result = client.revoke_pat("kbc_at_live", "pat-1")
        finally:
            client.close()
        assert result.confirmed is True

    def test_server_error_never_raises(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STACK_URL}/v1/auth/pat/pat-1",
            method="DELETE",
            status_code=500,
            json={"error": "internal error"},
        )
        client = _make_client()
        try:
            result = client.revoke_pat("kbc_at_live", "pat-1")
        finally:
            client.close()
        assert result.confirmed is False
        assert result.message
