"""Layer-3 HTTP client for the Keboola programmatic-auth endpoints.

Communicates directly with the stack's Connection host (no `stream.`/`ai.`
style host derivation -- the auth endpoints live on the stack URL itself).
Unlike every other client in this codebase, this one carries **no** durable
credential: PKCE/device exchange and refresh are unauthenticated by design
(the authorization code / device code / refresh token themselves are the
credential being presented), `introspect` stamps a bearer header per request
rather than on the client, and `revoke` is a public endpoint that takes the
token to revoke in its request body.

Inherits shared retry/backoff (429/5xx) and error-mapping infrastructure from
:class:`BaseHttpClient`, with one deliberate exception: `poll_device_token`
bypasses that infrastructure entirely (see its docstring).
"""

from __future__ import annotations

import json
from typing import Any, NoReturn
from urllib.parse import urlencode

import httpx

from ..constants import (
    AUTH_CLIENT_ID,
    AUTH_DEVICE_PATH,
    AUTH_DEVICE_TOKEN_PATH,
    AUTH_PKCE_AUTHORIZE_PATH,
    AUTH_PKCE_TOKEN_PATH,
    AUTH_SESSIONS_PATH,
    AUTH_TOKEN_INTROSPECT_PATH,
    AUTH_TOKEN_REFRESH_PATH,
    AUTH_TOKEN_REVOKE_PATH,
    MAX_API_ERROR_LENGTH,
)
from ..errors import ErrorCode, KeboolaApiError
from ..http_base import BaseHttpClient
from .models import (
    CliTokenResponse,
    DeviceAuthorization,
    DevicePollResult,
    DevicePollStatus,
    IntrospectResponse,
    RevokeResult,
)

# RFC 8628 error tokens (params.error / top-level error) the device-token
# endpoint answers with on a non-2xx response.
_DEVICE_ERROR_PENDING = "authorization_pending"
_DEVICE_ERROR_SLOW_DOWN = "slow_down"
_DEVICE_ERROR_DENIED = "access_denied"
_DEVICE_ERROR_EXPIRED = "expired_token"

# Substrings that identify a rejected grant in a 400 response body. Only
# consulted for 400 -- see `_is_rejected_grant` for why 401 needs no marker.
# Matched case-insensitively against the mapped error message.
_GRANT_REJECTION_MARKERS = (
    "invalid_grant",
    "invalid refresh token",
    "expired refresh token",
    "refresh token",
)


def _is_rejected_grant(exc: KeboolaApiError) -> bool:
    """True when a refresh failure means "this refresh token is unusable".

    Deliberately does NOT require the OAuth `invalid_grant` token to appear
    in the response. Production `connection.keboola.com` answers a replayed
    or revoked refresh token with **401 and the prose "Invalid refresh
    token."** -- no `invalid_grant` anywhere -- so the original
    substring-only check silently missed the single most common real-world
    case and let it fall through to the generic `INVALID_TOKEN` mapping.

    A 401 is treated as a rejected grant unconditionally: the refresh
    endpoint's *only* credential is the refresh token in the body, so there
    is nothing else a 401 could be about, and re-presenting the same token
    can never start succeeding.

    A 400 is ambiguous -- it can also mean we sent a malformed body, i.e.
    our own bug -- and purging on that would destroy a still-valid refresh
    token and force an avoidable re-login. So 400 requires a marker in the
    message before it is classified as a rejected grant.
    """
    if exc.status_code == 401:
        return True
    if exc.status_code == 400:
        message = (exc.message or "").lower()
        return any(marker in message for marker in _GRANT_REJECTION_MARKERS)
    return False


class AuthClient(BaseHttpClient):
    """Layer-3 client for the Keboola programmatic-auth endpoints (no auth header).

    Every endpoint is fail-closed behind per-stack feature flags: when
    programmatic auth is off, the stack answers 404. A 404 on ANY auth
    endpoint therefore means "not enabled here", not "wrong URL" -- it is
    mapped to `AUTH_NOT_SUPPORTED_ON_STACK` with an actionable message rather
    than the generic `NOT_FOUND` (see `_map_auth_error`).
    """

    def __init__(self, stack_url: str, *, timeout: httpx.Timeout | None = None) -> None:
        """Construct a client bound to ``stack_url``.

        ``stack_url`` is expected to already be normalized (callers resolve
        the stack via `models.normalize_stack_url` before constructing this
        client, per the login algorithm) -- this module deliberately avoids
        importing the top-level `..models` to keep the auth package's
        import graph shallow (see the interface contract's import-cycle
        rules). ``token=""`` is passed to `BaseHttpClient`: none of these
        endpoints authenticate via a stored client-level credential.
        """
        self._stack_url = stack_url.rstrip("/")
        super().__init__(
            base_url=self._stack_url,
            token="",
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )

    def __enter__(self) -> AuthClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # PKCE
    # ------------------------------------------------------------------

    def authorize_url(self, *, redirect_uri: str, code_challenge: str, state: str) -> str:
        """Build the browser-facing PKCE authorize URL.

        No request is sent -- the caller opens this URL in the user's
        browser (or prints it) and waits for the loopback callback.
        """
        query = urlencode(
            {
                "responseType": "code",
                "clientId": AUTH_CLIENT_ID,
                "redirectUri": redirect_uri,
                "codeChallenge": code_challenge,
                "codeChallengeMethod": "S256",
                "state": state,
            }
        )
        return f"{self._base_url}{AUTH_PKCE_AUTHORIZE_PATH}?{query}"

    def exchange_pkce_code(
        self, *, code: str, state: str, redirect_uri: str, code_verifier: str
    ) -> CliTokenResponse:
        """Exchange an authorization code for a token pair (``POST .../pkce/token``)."""
        response = self._do_request(
            "POST",
            AUTH_PKCE_TOKEN_PATH,
            json={
                "clientId": AUTH_CLIENT_ID,
                "code": code,
                "state": state,
                "redirectUri": redirect_uri,
                "codeVerifier": code_verifier,
            },
        )
        return CliTokenResponse.model_validate(response.json())

    # ------------------------------------------------------------------
    # Device authorization (RFC 8628)
    # ------------------------------------------------------------------

    def start_device_authorization(self) -> DeviceAuthorization:
        """Start a device-authorization flow (``POST /v1/auth/device``)."""
        response = self._do_request(
            "POST",
            AUTH_DEVICE_PATH,
            json={"clientId": AUTH_CLIENT_ID, "scope": {"credentialType": "session"}},
        )
        return DeviceAuthorization.model_validate(response.json())

    def poll_device_token(self, device_code: str) -> DevicePollResult:
        """Issue one RFC 8628 device-token poll (``POST /v1/auth/device/token``).

        Bypasses `_do_request`/the shared retry loop entirely and calls
        `self._client.request` directly: a polling 400 is an expected
        protocol state (authorization_pending, slow_down, ...), not an API
        failure -- the shared retry loop would both sleep on it wrongly and
        raise instead of returning a typed result. Every outcome except a
        404 (feature disabled on this stack) comes back as a
        `DevicePollResult`, including a malformed/unparseable response body,
        so the polling loop in `auth/device.py` never has to catch an
        exception mid-loop.
        """
        response = self._client.request(
            "POST",
            AUTH_DEVICE_TOKEN_PATH,
            json={"clientId": AUTH_CLIENT_ID, "deviceCode": device_code},
        )

        if response.status_code == 404:
            self._map_auth_error(response)

        if response.status_code < 300:
            try:
                tokens = CliTokenResponse.model_validate(response.json())
            except Exception as exc:
                return DevicePollResult(
                    status=DevicePollStatus.ERROR,
                    message=self._truncate(f"Malformed device-token response: {exc}"),
                )
            return DevicePollResult(status=DevicePollStatus.OK, tokens=tokens)

        try:
            envelope = response.json()
        except Exception as exc:
            return DevicePollResult(
                status=DevicePollStatus.ERROR,
                message=self._truncate(f"Malformed device-token error response: {exc}"),
            )
        if not isinstance(envelope, dict):
            return DevicePollResult(
                status=DevicePollStatus.ERROR,
                message=self._truncate(f"Unexpected device-token error body: {envelope!r}"),
            )

        if response.status_code == 429:
            return DevicePollResult(
                status=DevicePollStatus.SLOW_DOWN,
                interval=self._extract_poll_interval(envelope),
            )

        error_token = self._extract_rfc8628_error(envelope)
        if error_token == _DEVICE_ERROR_PENDING:
            return DevicePollResult(status=DevicePollStatus.PENDING)
        if error_token == _DEVICE_ERROR_SLOW_DOWN:
            return DevicePollResult(
                status=DevicePollStatus.SLOW_DOWN,
                interval=self._extract_poll_interval(envelope),
            )
        if error_token == _DEVICE_ERROR_DENIED:
            return DevicePollResult(status=DevicePollStatus.DENIED, message=error_token)
        if error_token == _DEVICE_ERROR_EXPIRED:
            return DevicePollResult(status=DevicePollStatus.EXPIRED, message=error_token)

        # invalid_grant, incorrect_client_credentials, and anything unknown
        # (including a body with no recognisable error token at all) are all
        # terminal-but-unclassified: report ERROR rather than guess.
        return DevicePollResult(
            status=DevicePollStatus.ERROR,
            message=self._truncate(error_token or json.dumps(envelope)),
        )

    @staticmethod
    def _extract_rfc8628_error(envelope: dict[str, Any]) -> str:
        """Pull the RFC 8628 error token out of a device-token error envelope.

        Prefers the Keboola exception envelope's nested ``params.error``
        (the RFC 8628 token) and falls back to a top-level ``error`` string
        (also accepted per the interface contract).
        """
        params = envelope.get("params")
        if isinstance(params, dict):
            nested = params.get("error")
            if isinstance(nested, str) and nested:
                return nested
        top = envelope.get("error")
        return top if isinstance(top, str) else ""

    @staticmethod
    def _extract_poll_interval(envelope: dict[str, Any]) -> int | None:
        """Pull ``params.interval`` (the server's requested new poll interval)."""
        params = envelope.get("params")
        if isinstance(params, dict):
            interval = params.get("interval")
            if isinstance(interval, int):
                return interval
        return None

    # ------------------------------------------------------------------
    # Refresh / introspect / revoke
    # ------------------------------------------------------------------

    def refresh(self, refresh_token: str) -> CliTokenResponse:
        """Rotate a token pair (``POST /v1/auth/token/refresh``).

        Uses `_do_request` -- network/5xx retry is safe here because the
        server grants a 30 second idempotent grace window on the old
        refresh token. A rejected grant (the refresh token was replayed
        after the grace window, or its whole family was revoked) is mapped
        to `ErrorCode.SESSION_EXPIRED` before the generic error path, so
        callers can distinguish "log in again" from a transient API failure.
        That distinction is load-bearing in two places, which is why it must
        not depend on the server's prose: `SessionTokenProvider._perform_refresh`
        purges the dead session from `auth.json` only for `SESSION_EXPIRED`,
        and `AuthService.status` reports "expired" only for `SESSION_EXPIRED`
        (re-raising anything else). Get the classification wrong and a dead
        session lingers forever, every command fails with an opaque 401, and
        even `auth status` -- the one command meant to diagnose it -- crashes
        instead of answering.
        """
        try:
            response = self._do_request(
                "POST",
                AUTH_TOKEN_REFRESH_PATH,
                json={"refreshToken": refresh_token},
            )
        except KeboolaApiError as exc:
            if _is_rejected_grant(exc):
                raise KeboolaApiError(
                    message=(
                        "Your Keboola login expired or was revoked. Run "
                        "`kbagent auth login` to sign in again."
                    ),
                    status_code=exc.status_code,
                    error_code=ErrorCode.SESSION_EXPIRED,
                    retryable=False,
                ) from exc
            raise
        return CliTokenResponse.model_validate(response.json())

    def introspect(self, access_token: str) -> IntrospectResponse:
        """Fetch session metadata + accessible projects for a live access token.

        The bearer header is set **per request**, never stored on the
        client -- `AuthClient` has no notion of "the current session", it is
        handed whichever access token the caller wants to introspect.
        """
        response = self._do_request(
            "GET",
            AUTH_TOKEN_INTROSPECT_PATH,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return IntrospectResponse.model_validate(response.json())

    def revoke(self, token: str, *, token_type_hint: str = "refreshToken") -> RevokeResult:
        """Revoke a token (``POST /v1/auth/token/revoke``).

        This is a **public** endpoint: the token to revoke travels in the
        JSON body (``{"token": ..., "tokenTypeHint": ...}``), not an
        `Authorization` header -- a header-only call 400s. This method
        never raises: logout must proceed with local credential cleanup
        even when the remote revoke call fails or the network is down, so
        any failure comes back as `RevokeResult(confirmed=False, ...)`
        rather than an exception the caller would have to guard against.
        """
        try:
            response = self._client.request(
                "POST",
                AUTH_TOKEN_REVOKE_PATH,
                json={"token": token, "tokenTypeHint": token_type_hint},
            )
        except httpx.HTTPError as exc:
            return RevokeResult(
                confirmed=False,
                message=self._truncate(f"{type(exc).__name__}: {exc}"),
            )

        if response.status_code < 300:
            return RevokeResult(confirmed=True)
        return RevokeResult(
            confirmed=False,
            message=self._truncate(self._extract_error_message(response)),
        )

    def delete_session(self, session_id: str, access_token: str) -> RevokeResult:
        """Kill a specific server session by id (``DELETE /v1/auth/sessions/{id}``).

        This is the primitive for retrying a recorded *orphan* during logout
        (review B-1): an orphan only ever has a session id on hand, never
        that session's refresh token, so it cannot go through `revoke` (which
        needs an actual token value). Session management is authenticated
        (unlike the public `revoke` endpoint), so this stamps a per-request
        bearer header rather than sending the session id as a body/credential.

        Like `revoke`, this never raises: logout must proceed with local
        cleanup regardless of what the remote call does, so any failure
        comes back as ``RevokeResult(confirmed=False, ...)``. A 404 is
        treated as the desired end state already reached (the session is
        simply gone -- already revoked, expired, or never existed) and
        reported as ``confirmed=True``, not as a feature-flag error: unlike
        the other auth endpoints, a 404 here is scoped to one session id, not
        to whether programmatic auth is enabled on the stack at all.
        """
        try:
            response = self._client.request(
                "DELETE",
                f"{AUTH_SESSIONS_PATH}/{session_id}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            return RevokeResult(
                confirmed=False,
                message=self._truncate(f"{type(exc).__name__}: {exc}"),
            )

        if response.status_code < 300 or response.status_code == 404:
            return RevokeResult(confirmed=True)
        return RevokeResult(
            confirmed=False,
            message=self._truncate(self._extract_error_message(response)),
        )

    @staticmethod
    def _extract_error_message(response: httpx.Response) -> str:
        """Best-effort human message from a failed response body.

        Mirrors `BaseHttpClient._raise_api_error`'s body-parsing priority
        without raising -- `revoke()` must always return a `RevokeResult`,
        never propagate an exception.
        """
        try:
            body = response.json()
        except Exception:
            return response.text
        if not isinstance(body, dict):
            return json.dumps(body)
        err_field = body.get("error")
        message = (
            err_field
            if isinstance(err_field, str) and err_field
            else (
                body.get("exception")
                or body.get("message")
                or body.get("description")
                or body.get("detail")
                or body.get("errors")
                or json.dumps(body)
            )
        )
        return message if isinstance(message, str) else json.dumps(message)

    # ------------------------------------------------------------------
    # Shared error mapping
    # ------------------------------------------------------------------

    def _raise_api_error(self, response: httpx.Response, base_url: str | None = None) -> None:
        """Escalate a 404 before falling back to the shared error mapping.

        `BaseHttpClient._do_request` calls this method for every
        `_do_request`-based call in this client, so overriding it here
        (rather than adding a check in each method) covers all of them at
        once. `poll_device_token` bypasses `_do_request` entirely and calls
        `_map_auth_error` directly for the same 404 case.
        """
        self._map_auth_error(response)

    def _map_auth_error(self, response: httpx.Response) -> NoReturn:
        """Map a failed auth-endpoint response, escalating 404 to a dedicated code.

        A 404 here means programmatic auth (or the specific flow) is not
        enabled on this stack -- a fail-closed feature flag -- not "wrong
        URL". Surfacing the generic `NOT_FOUND` code would send the user
        chasing a routing bug that does not exist; this maps it to
        `AUTH_NOT_SUPPORTED_ON_STACK` with a message naming the static-token
        fallback instead. Every other status delegates to the shared
        `BaseHttpClient` mapping -- no retry loop is added around the 404
        case, since a disabled feature flag will not become enabled by
        retrying.
        """
        if response.status_code == 404:
            raise KeboolaApiError(
                message=(
                    f"Browser login is not enabled on this Keboola stack yet "
                    f"({self._base_url}). Use a static Storage token instead: "
                    "kbagent project add --project <alias> --url <stack> --token <token>."
                ),
                status_code=404,
                error_code=ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK,
                retryable=False,
            )
        super()._raise_api_error(response, self._base_url)
        # BaseHttpClient._raise_api_error always raises; the static type
        # checker cannot see that across the base-class call, so make the
        # divergence explicit rather than relax this method's NoReturn type.
        raise AssertionError("unreachable: BaseHttpClient._raise_api_error always raises")

    @staticmethod
    def _truncate(message: str) -> str:
        """Cap a message to `MAX_API_ERROR_LENGTH`, matching the base client's truncation."""
        if len(message) > MAX_API_ERROR_LENGTH:
            return message[:MAX_API_ERROR_LENGTH] + "..."
        return message


__all__ = ["AuthClient"]
