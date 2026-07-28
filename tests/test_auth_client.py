"""Tests for `AuthClient` (Layer 3, programmatic auth / browser login).

Covers the RFC 8628 device-poll matrix, PKCE code exchange body, refresh's
`invalid_grant` -> `SESSION_EXPIRED` mapping, the 404 -> feature-flag mapping
shared by every auth endpoint, the per-request bearer header on introspect,
and revoke's public (no-Authorization, JSON-body) contract including its
never-raises uncertain-result path.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from keboola_agent_cli.auth.auth_client import AuthClient
from keboola_agent_cli.auth.models import (
    CliTokenResponse,
    DeviceAuthorization,
    DevicePollStatus,
    IntrospectResponse,
    RevokeResult,
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
# 404 -> AUTH_NOT_SUPPORTED_ON_STACK on every _do_request-based endpoint
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
