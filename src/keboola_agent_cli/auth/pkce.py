"""PKCE authorization-code login: challenge generation + loopback callback.

Implements the browser-based half of `kbagent auth login` (design doc
section 4.5 step 3): generate a fresh verifier/challenge/state triple, hand
the user a browser URL, and receive the authorization-code redirect on a
loopback HTTP listener bound to this process.

The exception hierarchy below encodes a security property, not just error
taxonomy: `PkceSetupError` (and its `PkceCallbackTimeout` subclass) are the
ONLY failures that may trigger an automatic fallback to the device flow --
nothing has been exchanged yet, so restarting the whole login through a
different channel is safe. `PkceStateMismatch` and `PkceAuthorizationError`
are terminal: the authorization step itself produced a definitive answer
(a forged/replayed callback, or an explicit `error=` from the server), and
silently retrying via another flow would mask that instead of surfacing it.
`services/auth_service.py` (package F) depends on this distinction to decide
whether `auth login` may fall back to `auth/device.py`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import secrets
import socket
import threading
from dataclasses import dataclass
from http import HTTPStatus
from urllib.parse import parse_qs, urlsplit

from ..constants import AUTH_CALLBACK_TIMEOUT, AUTH_PKCE_STATE_BYTES, AUTH_PKCE_VERIFIER_BYTES

# (host, socket address family) pairs tried in order when binding the
# loopback listener: IPv4 loopback first, IPv6 loopback as a fallback for
# hosts where IPv4 loopback is unavailable (design doc section 4.5 step 3).
_LOOPBACK_CANDIDATES: tuple[tuple[str, int], ...] = (
    ("127.0.0.1", socket.AF_INET),
    ("::1", socket.AF_INET6),
)

_SUCCESS_BODY = (
    "<html><head><title>Keboola CLI login</title></head><body>"
    "<p>Login complete. You can close this tab and return to your terminal.</p>"
    "</body></html>"
)
_FAILURE_BODY = (
    "<html><head><title>Keboola CLI login</title></head><body>"
    "<p>Login failed. You can close this tab and return to your terminal.</p>"
    "</body></html>"
)


@dataclass(frozen=True)
class PkceChallenge:
    """One PKCE verifier/challenge/state triple, generated fresh per login attempt.

    ``code_verifier`` never leaves this process except in the token-exchange
    request body; only ``code_challenge`` and ``state`` are sent to the
    browser-facing authorize URL.
    """

    code_verifier: str
    code_challenge: str
    state: str


def generate_pkce_challenge() -> PkceChallenge:
    """Generate a verifier/challenge/state triple sized per constants.py.

    Sizes are read from `AUTH_PKCE_VERIFIER_BYTES` / `AUTH_PKCE_STATE_BYTES`
    rather than hardcoded here, keeping the CSPRNG strength (RFC 7636's
    43-128 char verifier, >=256-bit entropy; >=128-bit state) a single source
    of truth with the design doc's rationale.
    """
    code_verifier = secrets.token_urlsafe(AUTH_PKCE_VERIFIER_BYTES)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(AUTH_PKCE_STATE_BYTES)
    return PkceChallenge(code_verifier=code_verifier, code_challenge=code_challenge, state=state)


@dataclass(frozen=True)
class LoopbackCallback:
    """The authorization code and state received on the loopback callback."""

    code: str
    state: str


class PkceSetupError(Exception):
    """Pre-exchange PKCE failure -- the caller MAY fall back to the device flow.

    Covers loopback bind failure and no usable browser (raised by the caller
    around `open_browser`/`detect_browser_environment`, not by this module),
    plus `PkceCallbackTimeout` below. Nothing has been exchanged when this is
    raised, so a device-flow retry is always safe.
    """


class PkceCallbackTimeout(PkceSetupError):
    """No callback arrived within the wait timeout.

    Fallback-eligible for the same reason as `PkceSetupError`: the browser
    may simply be unreachable (headless/remote session) or the user
    abandoned the tab, and nothing has been exchanged yet.
    """


class PkceStateMismatch(Exception):
    """The callback's ``state`` did not match the one this CLI generated.

    Terminal -- deliberately NOT a `PkceSetupError` subclass. A mismatch
    means the redirect did not originate from the authorize request this
    process sent (forged or replayed callback); the caller must never fall
    back to the device flow and must never exchange the accompanying code.
    """


class PkceAuthorizationError(Exception):
    """The authorization server redirected back with ``error=``. Terminal, no fallback.

    Distinct from `PkceSetupError` for the same reason as `PkceStateMismatch`:
    the authorization step completed (with a denial or error), so nothing a
    device-flow retry would do differently -- it would just ask the user to
    deny/error again.
    """

    def __init__(self, error: str, description: str = "") -> None:
        message = f"Authorization failed: {error}"
        if description:
            message = f"{message} ({description})"
        super().__init__(message)
        self.error = error
        self.description = description


def _first_query_value(params: dict[str, list[str]], key: str) -> str | None:
    """Return the first value for ``key`` in a `urllib.parse.parse_qs` result, or None."""
    values = params.get(key)
    return values[0] if values else None


class _CallbackHTTPServer(http.server.HTTPServer):
    """HTTPServer carrying the per-login state the request handler needs.

    Stored on the server instance (rather than as handler class attributes)
    so two `PkceCallbackServer` instances in the same process -- e.g. two
    test cases, or two concurrent logins -- never share state through a
    class attribute.
    """

    def __init__(
        self, server_address: tuple[str, int], address_family: int, expected_state: str
    ) -> None:
        self.address_family = address_family
        super().__init__(server_address, _CallbackHandler)
        self.expected_state = expected_state
        self.result: LoopbackCallback | None = None
        self.error: Exception | None = None
        self.event = threading.Event()


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Handles exactly one meaningful `GET /callback` request per server.

    Requests without a ``code`` or ``error`` query parameter (e.g. a stray
    `/favicon.ico`) are answered with a plain 404 and never resolve the wait.
    """

    def log_message(self, format: str, *args: object) -> None:
        """Silence the stdlib access log.

        The callback URL contains the authorization code as a query
        parameter; the default `BaseHTTPRequestHandler` logging would print
        the full request line (and thus the code) to stderr.
        """
        return

    def do_GET(self) -> None:
        server = self.server
        if not isinstance(server, _CallbackHTTPServer):  # pragma: no cover - defensive
            self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
            self.end_headers()
            return

        params = parse_qs(urlsplit(self.path).query)
        code = _first_query_value(params, "code")
        error = _first_query_value(params, "error")

        if code is None and error is None:
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()
            return

        state = _first_query_value(params, "state")

        # State is checked before anything else, and before the caller can
        # ever exchange the code -- see the module docstring.
        if not hmac.compare_digest(state or "", server.expected_state):
            server.error = PkceStateMismatch(
                "The redirect's state parameter did not match the one this CLI "
                "sent; refusing to treat this callback as a valid login."
            )
            self._respond(ok=False)
            server.event.set()
            return

        if error is not None:
            description = _first_query_value(params, "error_description") or ""
            server.error = PkceAuthorizationError(error, description)
            self._respond(ok=False)
            server.event.set()
            return

        server.result = LoopbackCallback(code=code or "", state=state or "")
        self._respond(ok=True)
        server.event.set()

    def _respond(self, *, ok: bool) -> None:
        """Serve the minimal success/failure page. Never echoes code/state/token."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write((_SUCCESS_BODY if ok else _FAILURE_BODY).encode("utf-8"))


class PkceCallbackServer:
    """Loopback HTTP listener that receives the authorization-code redirect.

    Binds an ephemeral port on `127.0.0.1`, falling back to `[::1]` when the
    IPv4 loopback is unavailable. Never binds a non-loopback address. Use as
    a context manager so the listener is always torn down -- on success, on
    timeout, and on a terminal error. Precedent: `commands/lineage.py`'s
    stdlib `http.server` usage.
    """

    def __init__(self, expected_state: str) -> None:
        # Starts True so a construction that raises leaves nothing half-open to
        # close: `close()` would otherwise call `shutdown()` on a server whose
        # `serve_forever` never ran, which blocks forever waiting for it to stop.
        self._closed = True
        self._expected_state = expected_state
        self._httpd = self._bind(expected_state)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        # The socket is bound by now, but `__exit__` only runs once `__init__`
        # has returned -- so a failing `start()` (`RuntimeError: can't start new
        # thread` under thread exhaustion) would leak the bound listener with no
        # owner left to close it.
        try:
            self._thread.start()
        except BaseException:
            self._httpd.server_close()
            raise
        self._closed = False

    @staticmethod
    def _bind(expected_state: str) -> _CallbackHTTPServer:
        last_error: OSError | None = None
        for host, family in _LOOPBACK_CANDIDATES:
            try:
                return _CallbackHTTPServer((host, 0), family, expected_state)
            except OSError as exc:
                last_error = exc
                continue
        raise PkceSetupError(
            f"Could not bind a loopback callback listener on 127.0.0.1 or [::1]: {last_error}"
        ) from last_error

    def __enter__(self) -> PkceCallbackServer:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @property
    def redirect_uri(self) -> str:
        """The `redirect_uri` to pass to `authorize_url`, matching the bound listener."""
        _host, port = self._httpd.server_address[:2]
        if self._httpd.address_family == socket.AF_INET6:
            return f"http://[::1]:{port}/callback"
        return f"http://127.0.0.1:{port}/callback"

    def wait(self, timeout: float = AUTH_CALLBACK_TIMEOUT) -> LoopbackCallback:
        """Block until a valid callback resolves the login, or ``timeout`` elapses.

        Raises `PkceCallbackTimeout` on expiry, `PkceStateMismatch` or
        `PkceAuthorizationError` for a terminal callback outcome, and returns
        the `LoopbackCallback` on success.
        """
        if not self._httpd.event.wait(timeout):
            raise PkceCallbackTimeout(
                f"Timed out after {timeout:.0f}s waiting for the browser to "
                "complete the login and redirect back to the CLI."
            )
        if self._httpd.error is not None:
            raise self._httpd.error
        if self._httpd.result is None:  # pragma: no cover - defensive
            raise PkceSetupError("Callback server signalled completion without a result.")
        return self._httpd.result

    def close(self) -> None:
        """Shut down the listener. Idempotent -- safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        self._httpd.shutdown()
        self._httpd.server_close()


__all__ = [
    "LoopbackCallback",
    "PkceAuthorizationError",
    "PkceCallbackServer",
    "PkceCallbackTimeout",
    "PkceChallenge",
    "PkceSetupError",
    "PkceStateMismatch",
    "generate_pkce_challenge",
]
