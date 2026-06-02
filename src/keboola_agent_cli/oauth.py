"""OAuth 2.0 Authorization Code + PKCE flow against the Keboola Connection OAuth server.

Implements browser-based project login (`kbagent project login`):

- PKCE pair generation (RFC 7636, S256) -- public client, NO client_secret.
- Authorization URL building for ``https://connection.<stack>/oauth/authorize``.
- Loopback callback server (RFC 8252) bound to 127.0.0.1 that receives the
  authorization code redirect.
- Code -> token exchange and refresh-token rotation against ``/oauth/token``.
- Minting a short-lived Storage API token from the OAuth access token via
  ``POST /v2/storage/tokens`` with ``Authorization: Bearer`` -- the same
  pattern the Keboola MCP server uses in production. The minted token is what
  every downstream kbagent client uses (Queue API and AI Service do not accept
  Bearer tokens yet), so the rest of the CLI is untouched by OAuth.

This module is LAYER 3 (HTTP + protocol) like ``http_base.py``: no Typer, no
Rich, no service imports. The login orchestration lives in
``services/oauth_login_service.py``; the silent-refresh chokepoint
(`ensure_fresh_oauth_token`) is called from ``BaseService.resolve_projects()``.

Security notes:
- The OAuth refresh token is persisted in config.json under the existing
  0600 + atomic-write + flock protections (same risk class as Storage tokens).
- The OAuth access token is NEVER persisted -- it is used transiently to mint
  a Storage token and discarded. With the refresh token we can always obtain
  a fresh access token.
- The callback server binds 127.0.0.1 only, validates the CSRF ``state``
  parameter, and accepts exactly one code.
"""

import base64
import contextlib
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import TracebackType
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .constants import (
    DEFAULT_OAUTH_CLIENT_ID,
    ENV_OAUTH_CLIENT_ID,
    OAUTH_CALLBACK_PATH,
    OAUTH_CALLBACK_PORTS,
    OAUTH_LOGIN_AUTHORIZE_PATH,
    OAUTH_LOGIN_TIMEOUT_SECONDS,
    OAUTH_LOGIN_TOKEN_PATH,
    OAUTH_REFRESH_MARGIN_SECONDS,
    OAUTH_SAPI_TOKEN_DESCRIPTION,
    OAUTH_SAPI_TOKEN_LIFETIME_SECONDS,
)
from .errors import ErrorCode, KeboolaApiError, mask_token

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = httpx.Timeout(30.0)


def resolve_oauth_client_id() -> str:
    """Resolve the OAuth client_id for the CLI.

    The client must be registered in the stack's Connection OAuth server
    (``league:oauth2-server:create-client ... --public``). Until the
    registration name is finalized across stacks, the id can be overridden
    via the ``KBAGENT_OAUTH_CLIENT_ID`` env var (also what the fake-server
    tests use).
    """
    return os.environ.get(ENV_OAUTH_CLIENT_ID, "").strip() or DEFAULT_OAUTH_CLIENT_ID


# ── PKCE (RFC 7636) ─────────────────────────────────────────────────


@dataclass(frozen=True)
class PkcePair:
    """A PKCE code_verifier and its S256 code_challenge."""

    verifier: str
    challenge: str


def generate_pkce_pair() -> PkcePair:
    """Generate a PKCE verifier/challenge pair (S256 method).

    The verifier is 43-128 chars of [A-Za-z0-9-._~] per RFC 7636;
    ``secrets.token_urlsafe(64)`` yields ~86 chars from that alphabet.
    The challenge is BASE64URL(SHA256(verifier)) without padding.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return PkcePair(verifier=verifier, challenge=challenge)


def generate_state() -> str:
    """Generate a CSRF state nonce for the authorization request."""
    return secrets.token_urlsafe(32)


# ── Authorization URL ───────────────────────────────────────────────


def build_authorize_url(
    stack_url: str,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    """Build the ``/oauth/authorize`` URL the browser is sent to.

    No ``scope`` is sent -- the Keboola OAuth server applies its own default
    scope (mirrors the MCP server's authorize request). The user logs in and
    selects the project on the Connection consent screen; the issued tokens
    are tied to that selection.
    """
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{stack_url.rstrip('/')}{OAUTH_LOGIN_AUTHORIZE_PATH}?{urlencode(params)}"


# ── Token endpoint (exchange + refresh) ─────────────────────────────


@dataclass(frozen=True)
class OAuthTokens:
    """Response from the OAuth token endpoint."""

    access_token: str
    refresh_token: str
    expires_in: int


def _post_token_request(stack_url: str, data: dict[str, str], operation: str) -> OAuthTokens:
    """POST to ``/oauth/token`` and parse the token response.

    Shared by code exchange and refresh. Raises ``KeboolaApiError`` with
    ``ErrorCode.OAUTH_ERROR`` on any failure; never echoes token material
    into error messages.
    """
    url = f"{stack_url.rstrip('/')}{OAUTH_LOGIN_TOKEN_PATH}"
    try:
        response = httpx.post(
            url,
            data=data,
            headers={"Accept": "application/json"},
            timeout=_HTTP_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.TimeoutException as exc:
        raise KeboolaApiError(
            f"OAuth {operation} timed out against {url}",
            error_code=ErrorCode.TIMEOUT,
            retryable=True,
        ) from exc
    except httpx.HTTPError as exc:
        raise KeboolaApiError(
            f"OAuth {operation} failed: cannot reach {url}: {exc}",
            error_code=ErrorCode.CONNECTION_ERROR,
            retryable=True,
        ) from exc

    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        payload = {}

    if response.status_code != 200 or "error" in payload:
        # League returns RFC 6749 error JSON ({"error": ..., "error_description"/"hint": ...}).
        detail = (
            payload.get("error_description")
            or payload.get("hint")
            or payload.get("message")
            or payload.get("error")
            or f"HTTP {response.status_code}"
        )
        raise KeboolaApiError(
            f"OAuth {operation} rejected by {url}: {detail}",
            error_code=ErrorCode.OAUTH_ERROR,
            status_code=response.status_code,
        )

    access_token = payload.get("access_token") or ""
    refresh_token = payload.get("refresh_token") or ""
    if not access_token or not refresh_token:
        raise KeboolaApiError(
            f"OAuth {operation} response from {url} is missing access_token/refresh_token fields.",
            error_code=ErrorCode.OAUTH_ERROR,
        )
    return OAuthTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=int(payload.get("expires_in") or 3600),
    )


def exchange_code(
    stack_url: str,
    *,
    client_id: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> OAuthTokens:
    """Exchange an authorization code for access + refresh tokens.

    Public client: ``code_verifier`` proves possession (PKCE), no secret.
    The Keboola OAuth server requires ``redirect_uri`` in the exchange.
    """
    return _post_token_request(
        stack_url,
        {
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        },
        operation="code exchange",
    )


def refresh_oauth_tokens(stack_url: str, *, client_id: str, refresh_token: str) -> OAuthTokens:
    """Rotate the refresh token for a fresh access + refresh token pair.

    The League OAuth server issues a NEW refresh token on every refresh and
    revokes the old one -- callers MUST persist the returned refresh token
    immediately or the session is lost.
    """
    return _post_token_request(
        stack_url,
        {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        operation="token refresh",
    )


# ── Storage token minting ───────────────────────────────────────────


def mint_storage_token(
    stack_url: str,
    *,
    access_token: str,
    expires_in: int = OAUTH_SAPI_TOKEN_LIFETIME_SECONDS,
    description: str = OAUTH_SAPI_TOKEN_DESCRIPTION,
) -> str:
    """Create a short-lived Storage API token using the OAuth access token.

    ``POST /v2/storage/tokens`` accepts ``Authorization: Bearer
    <oauth-access-token>`` (verified against the production MCP server
    implementation). The minted token carries the same capability flags
    kbagent's `project refresh` uses via the Manage API, so every existing
    command keeps working unchanged.
    """
    url = f"{stack_url.rstrip('/')}/v2/storage/tokens"
    try:
        response = httpx.post(
            url,
            json={
                "description": description,
                "expiresIn": expires_in,
                "canManageBuckets": True,
                "canReadAllFileUploads": True,
                "canReadAllProjectEvents": True,
                "canManageDevBranches": True,
                "canManageTokens": True,
            },
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.TimeoutException as exc:
        raise KeboolaApiError(
            f"Storage token creation timed out against {url}",
            error_code=ErrorCode.TIMEOUT,
            retryable=True,
        ) from exc
    except httpx.HTTPError as exc:
        raise KeboolaApiError(
            f"Storage token creation failed: cannot reach {url}: {exc}",
            error_code=ErrorCode.CONNECTION_ERROR,
            retryable=True,
        ) from exc

    if response.status_code != 200:
        raise KeboolaApiError(
            f"Storage token creation rejected ({response.status_code}) by {url}. "
            "The OAuth access token may lack project access.",
            error_code=ErrorCode.OAUTH_ERROR,
            status_code=response.status_code,
        )
    token = response.json().get("token") or ""
    if not token:
        raise KeboolaApiError(
            f"Storage token response from {url} is missing the 'token' field.",
            error_code=ErrorCode.OAUTH_ERROR,
        )
    return token


# ── Loopback callback server (RFC 8252) ─────────────────────────────


@dataclass(frozen=True)
class CallbackResult:
    """Outcome of the browser redirect to the loopback callback."""

    code: str = ""
    state: str = ""
    error: str = ""


_SUCCESS_PAGE = (
    "<html><head><title>kbagent login</title></head><body "
    'style="font-family: sans-serif; text-align: center; margin-top: 4em;">'
    "<h2>&#10003; Login complete</h2>"
    "<p>You can close this tab and return to your terminal.</p>"
    "</body></html>"
)
_ERROR_PAGE = (
    "<html><head><title>kbagent login</title></head><body "
    'style="font-family: sans-serif; text-align: center; margin-top: 4em;">'
    "<h2>&#10007; Login failed</h2>"
    "<p>{reason}</p><p>Return to your terminal for details.</p>"
    "</body></html>"
)


class _CallbackHandler(BaseHTTPRequestHandler):
    """Captures the single OAuth redirect; everything else is a 404."""

    server: "OAuthCallbackServer"  # narrowed for type-checkers

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != OAUTH_CALLBACK_PATH:
            self._respond(404, _ERROR_PAGE.format(reason="Unknown callback path."))
            return

        params = parse_qs(parsed.query)
        error = (params.get("error") or [""])[0]
        code = (params.get("code") or [""])[0]
        state = (params.get("state") or [""])[0]

        if error:
            description = (params.get("error_description") or [error])[0]
            self.server.deliver(CallbackResult(error=description))
            self._respond(200, _ERROR_PAGE.format(reason=description))
            return
        if not code or not state:
            self.server.deliver(
                CallbackResult(error="Callback is missing 'code' or 'state' parameter.")
            )
            self._respond(400, _ERROR_PAGE.format(reason="Missing code/state parameter."))
            return

        self.server.deliver(CallbackResult(code=code, state=state))
        self._respond(200, _SUCCESS_PAGE)

    def _respond(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        # Route http.server's stderr chatter to the debug logger; the query
        # string contains the authorization code, so never log it verbatim.
        logger.debug("OAuth callback request: %s", self.path.split("?")[0])


class OAuthCallbackServer(HTTPServer):
    """Single-use loopback HTTP server that waits for the OAuth redirect.

    Binds 127.0.0.1 on the first free port from ``ports`` (the registered
    client must whitelist ``http://127.0.0.1:<port>/callback`` for each).
    Use as a context manager; call :meth:`wait_for_code` after opening the
    browser.
    """

    def __init__(self, ports: tuple[int, ...] = OAUTH_CALLBACK_PORTS) -> None:
        self._result: CallbackResult | None = None
        self._received = threading.Event()
        bound_error: OSError | None = None
        for port in ports:
            try:
                super().__init__(("127.0.0.1", port), _CallbackHandler)
                bound_error = None
                break
            except OSError as exc:
                bound_error = exc
        if bound_error is not None:
            raise KeboolaApiError(
                f"No free OAuth callback port among {ports}. "
                "Close the process occupying them or pass --port.",
                error_code=ErrorCode.OAUTH_ERROR,
            )
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    @property
    def redirect_uri(self) -> str:
        """The redirect URI this server listens on."""
        return f"http://127.0.0.1:{self.server_address[1]}{OAUTH_CALLBACK_PATH}"

    def deliver(self, result: CallbackResult) -> None:
        """Record the first callback result and wake the waiter (later ones ignored)."""
        if self._result is None:
            self._result = result
        self._received.set()

    def wait_for_code(
        self,
        expected_state: str,
        timeout: float = OAUTH_LOGIN_TIMEOUT_SECONDS,
    ) -> str:
        """Block until the browser redirect arrives; return the authorization code.

        Raises:
            KeboolaApiError: On timeout, an OAuth error redirect (e.g. the
                user denied consent), or a CSRF state mismatch.
        """
        if not self._received.wait(timeout):
            raise KeboolaApiError(
                f"Timed out after {int(timeout)}s waiting for the browser login. "
                "Re-run `kbagent project login` and complete the login in the browser.",
                error_code=ErrorCode.OAUTH_ERROR,
            )
        result = self._result or CallbackResult(error="No callback received.")
        if result.error:
            raise KeboolaApiError(
                f"OAuth login failed in the browser: {result.error}",
                error_code=ErrorCode.OAUTH_ERROR,
            )
        if not secrets.compare_digest(result.state, expected_state):
            raise KeboolaApiError(
                "OAuth state mismatch on the callback (possible CSRF or a "
                "stale browser tab). Re-run `kbagent project login`.",
                error_code=ErrorCode.OAUTH_ERROR,
            )
        return result.code

    def close(self) -> None:
        """Stop the server thread and release the port."""
        self.shutdown()
        self.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "OAuthCallbackServer":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()


# ── Silent refresh chokepoint ───────────────────────────────────────

# fcntl is POSIX-only (mirrors config_store.py); on Windows we skip locking.
try:
    import fcntl

    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False


@contextlib.contextmanager
def _refresh_lock(lock_path: str):
    """Inter-process lock serializing refresh-token rotation.

    The League OAuth server REVOKES the old refresh token on rotation, so two
    kbagent processes refreshing concurrently would race: the loser persists
    a revoked token and the session dies. An exclusive flock on a sidecar
    file in the config dir serializes them; the winner persists the rotated
    token and the loser re-reads config and sees the fresh one.
    """
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if _HAS_FCNTL:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if _HAS_FCNTL:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def ensure_fresh_oauth_token(config_store, alias: str, project):
    """Return ``project`` with a valid Storage token, refreshing if needed.

    No-op (and zero network calls) unless the project was added via
    ``kbagent project login`` AND its minted Storage token is expired or
    inside the refresh margin. Called from
    ``BaseService.resolve_projects()`` so every service, the MCP subprocess
    env, and the `kbagent serve` routers all receive a fresh token without
    any per-call-site changes.

    On refresh failure the stale project is returned unchanged with a
    warning logged -- a single stale OAuth project must not torpedo a
    multi-project fan-out; the downstream 401 is reported per-project via
    the existing error-accumulation machinery.

    Args:
        config_store: ConfigStore for persisting the rotated credentials.
        project: ProjectConfig (typed loosely to avoid an import cycle with
            models via services.base).

    Returns:
        The fresh ProjectConfig (the same instance when no refresh was needed).
    """
    from .models import OAuthCredentials  # local import: avoid models<->oauth cycle risk

    oauth = getattr(project, "oauth", None)
    # isinstance (not a None check) so mock ProjectConfig objects used across
    # the test suite never trip the refresh path.
    if not isinstance(oauth, OAuthCredentials):
        return project
    expires_at = oauth.token_expires_at
    if expires_at is not None and expires_at - time.time() > OAUTH_REFRESH_MARGIN_SECONDS:
        return project

    lock_path = str(config_store.config_dir / ".oauth-refresh.lock")
    with _refresh_lock(lock_path):
        # Another process may have rotated the credentials while we waited
        # on the lock -- re-read and bail out if the token is fresh now.
        latest = config_store.get_project(alias)
        if latest is not None and latest.oauth is not None:
            latest_expiry = latest.oauth.token_expires_at
            if (
                latest_expiry is not None
                and latest_expiry - time.time() > OAUTH_REFRESH_MARGIN_SECONDS
            ):
                return latest
            oauth = latest.oauth
            project = latest

        logger.debug("Refreshing OAuth session for project '%s'", alias)
        try:
            tokens = refresh_oauth_tokens(
                project.stack_url,
                client_id=oauth.client_id,
                refresh_token=oauth.refresh_token,
            )
            sapi_token = mint_storage_token(project.stack_url, access_token=tokens.access_token)
        except KeboolaApiError as exc:
            logger.warning(
                "OAuth refresh failed for project '%s' (%s): %s -- run "
                "`kbagent project login --url %s` to re-authenticate.",
                alias,
                mask_token(oauth.refresh_token),
                exc.message,
                project.stack_url,
            )
            return project

        new_creds = OAuthCredentials(
            client_id=oauth.client_id,
            refresh_token=tokens.refresh_token,
            token_expires_at=time.time() + OAUTH_SAPI_TOKEN_LIFETIME_SECONDS,
        )
        config_store.edit_project(alias, token=sapi_token, oauth=new_creds)
        updated = config_store.get_project(alias)
        return updated if updated is not None else project
