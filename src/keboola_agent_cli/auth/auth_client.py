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
:class:`BaseHttpClient`, with two deliberate exceptions that keep the mapping
but skip the retry loop: `poll_device_token` (a polling 400 is a protocol
state, not a failure) and `refresh` (a blind retry would re-present the refresh
token, and it runs under a wall-clock ceiling the retry loop would outlast).
`refresh` makes one narrow exception of its own for a rotation deadlock, which
the server reports in a form that proves nothing rotated. See their docstrings.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any, NoReturn
from urllib.parse import urlencode

import httpx

from ..constants import (
    AUTH_CLIENT_ID,
    AUTH_DEVICE_PATH,
    AUTH_DEVICE_TOKEN_PATH,
    AUTH_PKCE_AUTHORIZE_PATH,
    AUTH_PKCE_TOKEN_PATH,
    AUTH_REFRESH_CONTENTION_DEFAULT_DELAY,
    AUTH_REFRESH_CONTENTION_MAX_DELAY,
    AUTH_REFRESH_CONTENTION_RETRIES,
    AUTH_REFRESH_CONTENTION_STRING_CODE,
    AUTH_REFRESH_TIMEOUT,
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
#
# The OAuth token is decisive on its own.
_GRANT_REJECTION_MARKERS = ("invalid_grant",)

# Otherwise a 400 must name the credential AND pass a verdict on it. Requiring
# both halves rather than fixed phrases is what keeps two failure modes apart
# without guessing the server's exact wording: "The refresh token has expired."
# and "Refresh token revoked." are rejections whatever the word order, while
# "The refresh token must be a string." names the field and passes no verdict --
# that is a malformed request, i.e. our own bug, and purging a still-valid
# credential over it would force an avoidable re-login.
_GRANT_SUBJECT_MARKERS = ("refresh token", "refreshtoken")
_GRANT_VERDICT_MARKERS = (
    "invalid",
    "expired",
    "revoked",
    "not valid",
    "no longer valid",
    "unknown",
    "rejected",
)

# ...and must not read as a complaint about the SHAPE of the request. "invalid"
# and "unknown" describe a malformed field as naturally as a dead credential
# ("the refresh token field contains an invalid character"), and a substring test
# cannot tell which noun the verdict attaches to. Any of these vetoes the
# classification, because the two mistakes are not symmetric: refusing to purge
# leaves an error the user can clear with `auth login`, while purging wrongly
# destroys a working credential over a bug on our own side.
_REQUEST_SHAPE_MARKERS = (
    "field",
    "parameter",
    "schema",
    "must be",
    "must not",
    "character",
    "exceed",
    "length",
    "required",
    "missing",
    "malformed",
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
    token and force an avoidable re-login. So a 400 counts only when it
    carries `invalid_grant`, or names the refresh token AND passes a verdict
    on it AND does not read as a complaint about the request's shape (see the
    three marker tuples above for why all three halves are required).
    """
    if exc.status_code == 401:
        return True
    if exc.status_code == 400:
        message = (exc.message or "").lower()
        if any(marker in message for marker in _GRANT_REJECTION_MARKERS):
            return True
        names_the_token = any(marker in message for marker in _GRANT_SUBJECT_MARKERS)
        passes_a_verdict = any(marker in message for marker in _GRANT_VERDICT_MARKERS)
        about_the_request = any(marker in message for marker in _REQUEST_SHAPE_MARKERS)
        return names_the_token and passes_a_verdict and not about_the_request
    return False


def _error_string_code(response: httpx.Response) -> str | None:
    """Read the server's machine-readable string code out of an error body.

    Three shapes are accepted because the key depends on which serializer sits
    in front of the endpoint: top-level ``code``, top-level ``stringCode``, or
    nested ``exception.code``. A body that is not JSON, or not an object, has no
    code -- callers must treat `None` as "unclassifiable", never as a match.

    `RecursionError` joins `ValueError` because the two ways a body can defeat
    the decoder are not in the same exception hierarchy: malformed input raises
    `json.JSONDecodeError` (a `ValueError`), while input nested past the
    interpreter's recursion limit raises `RecursionError` (a `RuntimeError`).
    Letting the second escape would propagate out of the refresh call as neither
    `_AbandonedRefresh` nor `KeboolaApiError`, so the lease holder could not
    classify it and would leave its claim standing until the TTL expired.
    """
    try:
        body = response.json()
    except (ValueError, RecursionError):
        return None
    if not isinstance(body, dict):
        return None

    for key in ("code", "stringCode"):
        value = body.get(key)
        if isinstance(value, str):
            return value
    nested = body.get("exception")
    if isinstance(nested, dict) and isinstance(nested.get("code"), str):
        return str(nested["code"])
    return None


def _contention_retry_delay(response: httpx.Response) -> float | None:
    """Seconds to wait before replaying this refresh, or `None` if it must not be.

    Only a rotation deadlock qualifies: a 503 whose body carries
    `AUTH_REFRESH_CONTENTION_STRING_CODE`. That pair is the server's own proof
    that the rotation transaction rolled back, which is what makes replaying the
    same refresh token safe here and nowhere else. The status alone is not
    enough -- a 503 from a proxy or from load shedding proves nothing about
    whether the rotation committed.

    `Retry-After` is honoured when present and parseable, clamped to
    `AUTH_REFRESH_CONTENTION_MAX_DELAY` because the wait happens inside the
    caller's wall-clock ceiling; a longer wait would only get the attempt
    abandoned mid-sleep. The returned delay is always a finite, non-negative
    number of seconds, so the caller can hand it straight to `time.sleep`.
    """
    if response.status_code != 503:
        return None
    if _error_string_code(response) != AUTH_REFRESH_CONTENTION_STRING_CODE:
        return None

    header = response.headers.get("Retry-After")
    try:
        requested = float(header) if header else AUTH_REFRESH_CONTENTION_DEFAULT_DELAY
    except ValueError:
        # An HTTP-date `Retry-After` is legal but the server sends seconds; treat
        # anything unparseable as "no guidance" rather than as a reason to skip
        # a retry the response has already proven safe.
        requested = AUTH_REFRESH_CONTENTION_DEFAULT_DELAY
    if not math.isfinite(requested):
        # `float()` accepts "nan" and "inf". Clamping cannot repair a NaN --
        # every comparison against it is False, so both `min` and `max` pass it
        # through -- and `time.sleep(nan)` raises, turning a recoverable
        # deadlock into an uncaught error the lease holder cannot classify.
        requested = AUTH_REFRESH_CONTENTION_DEFAULT_DELAY
    return min(max(requested, 0.0), AUTH_REFRESH_CONTENTION_MAX_DELAY)


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

        The second deliberate exception to the shared retry infrastructure
        (`poll_device_token` is the other): this bypasses `_do_request` and
        issues ONE request per failure class, under `AUTH_REFRESH_TIMEOUT`
        rather than the client's default timeout. A refresh lease in
        ``auth.json`` keeps concurrent kbagent processes off this token while
        the call runs, and a waiter gives up after
        `AUTH_REFRESH_WAIT_TIMEOUT`; three retries at the default 30 s read
        timeout plus backoff would outlast that, so a merely slow auth service
        would have every other process report contention that is not really
        there.

        The single exception is a rotation deadlock, which the server reports
        as a 503 carrying `AUTH_REFRESH_CONTENTION_STRING_CODE` and a
        `Retry-After`. That response is proof the rotation rolled back whole,
        so the submitted token is still the session's current one and
        re-presenting it is an ordinary refresh rather than a replay -- the
        reasoning that forbids a blanket retry is exactly what permits this
        one. `AUTH_REFRESH_CONTENTION_RETRIES` bounds it and the lease is held
        across it, so no other process can present the token in between.

        These per-phase timeouts do not by themselves bound the call: httpx
        applies `read` / `write` per I/O operation, so a trickled response
        never trips them. The total is capped by the caller --
        `SessionTokenProvider._refresh_within_budget` enforces
        `AUTH_REFRESH_MAX_WALL_CLOCK`, which is where the lock hold is owned.

        Dropping the retry costs little: on failure the old refresh token is
        still on disk and, for as long as the server's idempotent grace window
        is open, still usable, so the next command refreshes again -- while a
        retry would re-present that same token, which is the very replay the
        grace window exists to forgive. The window is measured from the
        server's own rotation and its length is a stack-side setting, so the
        recovery holds only while nothing stalls the next attempt.

        A rejected grant (the refresh token was replayed after the grace
        window, or its whole family was revoked) is mapped to
        `ErrorCode.SESSION_EXPIRED` before the generic error path, so callers
        can distinguish "log in again" from a transient API failure. That
        distinction is load-bearing in two places, which is why it must not
        depend on the server's prose: `SessionTokenProvider._perform_refresh`
        purges the dead session from `auth.json` only for `SESSION_EXPIRED`,
        and `AuthService.status` reports "expired" only for `SESSION_EXPIRED`
        (re-raising anything else). Get the classification wrong and a dead
        session lingers forever, every command fails with an opaque 401, and
        even `auth status` -- the one command meant to diagnose it -- crashes
        instead of answering.

        A transport failure is reported as `ErrorCode.TIMEOUT` /
        `ErrorCode.CONNECTION_ERROR` (both exit 4, network) so a slow or
        unreachable auth service never masquerades as a session problem.
        """
        for attempt in range(AUTH_REFRESH_CONTENTION_RETRIES + 1):
            response = self._post_refresh(refresh_token)
            if response.status_code < 400:
                return CliTokenResponse.model_validate(response.json())

            delay = _contention_retry_delay(response)
            if delay is None or attempt == AUTH_REFRESH_CONTENTION_RETRIES:
                self._raise_refresh_error(response)
            time.sleep(delay)

        raise AssertionError("unreachable: the final attempt always returns or raises")

    def _post_refresh(self, refresh_token: str) -> httpx.Response:
        """Issue one refresh request, mapping transport failures to network codes."""
        try:
            return self._client.request(
                "POST",
                AUTH_TOKEN_REFRESH_PATH,
                json={"refreshToken": refresh_token},
                timeout=AUTH_REFRESH_TIMEOUT,
            )
        except httpx.TimeoutException as exc:
            raise KeboolaApiError(
                message=(
                    f"Refreshing your Keboola login at {self._base_url} timed out. "
                    "Run the command again."
                ),
                status_code=0,
                error_code=ErrorCode.TIMEOUT,
                retryable=True,
            ) from exc
        except httpx.TransportError as exc:
            raise KeboolaApiError(
                message=(
                    f"Cannot reach {self._base_url} to refresh your Keboola login "
                    f"({type(exc).__name__})."
                ),
                status_code=0,
                error_code=ErrorCode.CONNECTION_ERROR,
                retryable=True,
            ) from exc

    def _raise_refresh_error(self, response: httpx.Response) -> NoReturn:
        """Map a failed refresh response, escalating a rejected grant."""
        try:
            self._map_auth_error(response)
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
        raise AssertionError("unreachable: _map_auth_error always raises")

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
