"""Browser-based WebAuthn sudo step-up: loopback callback half.

Mirrors `pkce.py`'s pattern deliberately: a WebAuthn ceremony
(`navigator.credentials.get()`) can only run on a page whose origin matches
(or is a registrable suffix of) the credential's relying-party id -- a page
kbagent hosts itself on `127.0.0.1` cannot complete it, the same way this
CLI cannot complete an OAuth authorization step itself. The ceremony page
has to be served by the stack, exactly like `/admin/auth/pkce/authorize` is
for login; this module is only the CLI-side half that opens the browser at
that page and waits on a loopback listener for the result.

PLACEHOLDER CONTRACT -- confirm before pointing this at a live stack:
`AUTH_SUDO_WEBAUTHN_CEREMONY_PATH` (constants.py) and the query parameter
names below (`options`, `challengeToken`, `redirect_uri`, `state` out;
`assertion`, `state` back) are this module's best guess at a page that
mirrors the existing PKCE authorize/callback contract -- run its own
`navigator.credentials.get()` using the options it's handed, then redirect
the browser to `redirect_uri` with the resulting assertion. Everything else
here (the loopback listener, the state check, opening the browser, wiring
the result into `POST /v1/auth/sudo`) is real and does not change once the
actual page/contract is confirmed -- only the one path constant and the
query parameter names would need adjusting.
"""

from __future__ import annotations

import http.server
import json
import secrets
import socket
import threading
from dataclasses import dataclass
from http import HTTPStatus
from urllib.parse import parse_qs, urlencode, urlsplit

from ..constants import AUTH_CALLBACK_TIMEOUT, AUTH_PKCE_STATE_BYTES

_LOOPBACK_CANDIDATES: tuple[tuple[str, int], ...] = (
    ("127.0.0.1", socket.AF_INET),
    ("::1", socket.AF_INET6),
)

_SUCCESS_BODY = (
    "<html><head><title>Keboola CLI sudo step-up</title></head><body>"
    "<p>Step-up complete. You can close this tab and return to your terminal.</p>"
    "</body></html>"
)
_FAILURE_BODY = (
    "<html><head><title>Keboola CLI sudo step-up</title></head><body>"
    "<p>Step-up failed. You can close this tab and return to your terminal.</p>"
    "</body></html>"
)


def generate_webauthn_state() -> str:
    """A fresh CSRF-style nonce for one ceremony, sized like the PKCE `state`."""
    return secrets.token_urlsafe(AUTH_PKCE_STATE_BYTES)


@dataclass(frozen=True)
class WebAuthnCallback:
    """The WebAuthn assertion and state received on the loopback callback.

    ``assertion`` is the raw string the ceremony page redirected back with --
    passed through verbatim to `AuthClient.sudo_webauthn`'s `webauthnAssertion`
    body field, never parsed or re-encoded here.
    """

    assertion: str
    state: str


class WebAuthnCeremonySetupError(Exception):
    """Pre-ceremony failure (loopback bind, no usable browser). No credential spent yet."""


class WebAuthnCeremonyTimeout(WebAuthnCeremonySetupError):
    """No callback arrived within the wait timeout."""


class WebAuthnStateMismatch(Exception):
    """The callback's ``state`` did not match the one this CLI generated.

    Terminal, same reasoning as `PkceStateMismatch`: the redirect did not
    originate from the ceremony this process started.
    """


class WebAuthnCeremonyDenied(Exception):
    """The ceremony page redirected back with ``error=`` (cancelled, no authenticator, ...)."""

    def __init__(self, error: str, description: str = "") -> None:
        message = f"WebAuthn step-up failed: {error}"
        if description:
            message = f"{message} ({description})"
        super().__init__(message)
        self.error = error
        self.description = description


def _first_query_value(params: dict[str, list[str]], key: str) -> str | None:
    values = params.get(key)
    return values[0] if values else None


class _CallbackHTTPServer(http.server.HTTPServer):
    def __init__(
        self, server_address: tuple[str, int], address_family: int, expected_state: str
    ) -> None:
        self.address_family = address_family
        super().__init__(server_address, _CallbackHandler)
        self.expected_state = expected_state
        self.result: WebAuthnCallback | None = None
        self.error: Exception | None = None
        self.event = threading.Event()


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Handles exactly one meaningful `GET /callback` request per server."""

    def log_message(self, format: str, *args: object) -> None:
        """Silence the stdlib access log -- the URL carries the assertion."""
        return

    def do_GET(self) -> None:
        server = self.server
        if not isinstance(server, _CallbackHTTPServer):  # pragma: no cover - defensive
            self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
            self.end_headers()
            return

        params = parse_qs(urlsplit(self.path).query)
        assertion = _first_query_value(params, "assertion")
        error = _first_query_value(params, "error")

        if assertion is None and error is None:
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()
            return

        state = _first_query_value(params, "state")

        if not _constant_time_eq(state or "", server.expected_state):
            server.error = WebAuthnStateMismatch(
                "The redirect's state parameter did not match the one this CLI "
                "sent; refusing to treat this callback as a valid step-up."
            )
            self._respond(ok=False)
            server.event.set()
            return

        if error is not None:
            description = _first_query_value(params, "error_description") or ""
            server.error = WebAuthnCeremonyDenied(error, description)
            self._respond(ok=False)
            server.event.set()
            return

        server.result = WebAuthnCallback(assertion=assertion or "", state=state or "")
        self._respond(ok=True)
        server.event.set()

    def _respond(self, *, ok: bool) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write((_SUCCESS_BODY if ok else _FAILURE_BODY).encode("utf-8"))


def _constant_time_eq(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a, b)


class WebAuthnCallbackServer:
    """Loopback HTTP listener that receives the WebAuthn-ceremony redirect.

    Binds an ephemeral port on `127.0.0.1`, falling back to `[::1]`. Use as
    a context manager. See the module docstring for the placeholder ceremony
    page / query contract this listener expects to be redirected back with.
    """

    def __init__(self, expected_state: str) -> None:
        self._closed = True
        self._expected_state = expected_state
        self._httpd = self._bind(expected_state)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
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
        raise WebAuthnCeremonySetupError(
            f"Could not bind a loopback callback listener on 127.0.0.1 or [::1]: {last_error}"
        ) from last_error

    def __enter__(self) -> WebAuthnCallbackServer:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @property
    def redirect_uri(self) -> str:
        _host, port = self._httpd.server_address[:2]
        if self._httpd.address_family == socket.AF_INET6:
            return f"http://[::1]:{port}/callback"
        return f"http://127.0.0.1:{port}/callback"

    def wait(self, timeout: float = AUTH_CALLBACK_TIMEOUT) -> WebAuthnCallback:
        """Block until a valid callback resolves the step-up, or ``timeout`` elapses."""
        if not self._httpd.event.wait(timeout):
            raise WebAuthnCeremonyTimeout(
                f"Timed out after {timeout:.0f}s waiting for the browser to "
                "complete the WebAuthn step-up and redirect back to the CLI."
            )
        if self._httpd.error is not None:
            raise self._httpd.error
        if self._httpd.result is None:  # pragma: no cover - defensive
            raise WebAuthnCeremonySetupError(
                "Callback server signalled completion without a result."
            )
        return self._httpd.result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._httpd.shutdown()
        self._httpd.server_close()


def build_ceremony_url(
    *,
    stack_url: str,
    ceremony_path: str,
    options: dict,
    challenge_token: str,
    redirect_uri: str,
    state: str,
) -> str:
    """Build the browser-facing WebAuthn ceremony URL.

    ``options`` (the `PublicKeyCredentialRequestOptions` from
    `POST /v1/auth/sudo/challenge`) travels as a JSON query parameter so the
    ceremony page does not need a second authenticated call to fetch it --
    it only has the one-time `challenge_token`, not this session's bearer.
    """
    query = urlencode(
        {
            "challengeToken": challenge_token,
            "options": json.dumps(options, separators=(",", ":")),
            "redirectUri": redirect_uri,
            "state": state,
        }
    )
    return f"{stack_url.rstrip('/')}{ceremony_path}?{query}"
