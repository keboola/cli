"""Keboola Developer Portal HTTP client (apps-api.keboola.com).

Auth model:
- Login (email + password) returns a bearer token. On a personal account, the
  first login returns an MFA session; we prompt the user via /dev/tty and
  re-login with {email, session, code} to obtain the bearer.
- The bearer lives ONLY on this client instance (in self._bearer). It is
  never written to disk, never logged, and discarded when the client closes.
- Each kbagent invocation logs in fresh; there is no token cache.

The client is intentionally dumb: dry-run, diff, and confirm logic belong to
the service and command layers.
"""

from __future__ import annotations

import logging

import httpx

from .errors import ErrorCode, KeboolaApiError
from .http_base import BaseHttpClient
from .models import DeveloperPortalIdentity

logger = logging.getLogger(__name__)


def _tty_prompt(label: str, *, secret: bool = False) -> str | None:
    """Prompt via the controlling terminal so a redirected stdin can't break it.

    Returns None when no /dev/tty is available (non-interactive shell, no
    controlling terminal). Caller must treat None as "cannot prompt".
    """
    try:
        with open("/dev/tty", "w") as out:
            if secret:
                import getpass

                return getpass.getpass(label, stream=out)
            out.write(label)
            out.flush()
            with open("/dev/tty") as tin:
                return tin.readline().rstrip("\n")
    except OSError:
        return None


class DeveloperPortalClient(BaseHttpClient):
    """HTTP client for the Keboola Developer Portal."""

    def __init__(self, identity: DeveloperPortalIdentity) -> None:
        # We don't have a bearer yet — pass empty token. Login populates it.
        super().__init__(
            base_url=identity.portal_url,
            token="",
            headers={"Accept": "application/json"},
        )
        self._identity = identity
        self._bearer: str | None = None

    def _ensure_authenticated(self) -> None:
        """Log in if not already authenticated. Idempotent on the instance."""
        if self._bearer is not None:
            return
        self._bearer = self._login(self._identity.username, self._identity.password)
        self._client.headers["Authorization"] = self._bearer

    def _login(self, username: str, password: str) -> str:
        try:
            resp = self._client.post(
                "/auth/login",
                json={"email": username, "password": password},
            )
        except httpx.HTTPError as exc:
            raise KeboolaApiError(
                message=f"Developer Portal login transport error: {exc}",
                error_code=ErrorCode.CONNECTION_ERROR,
            ) from exc
        if resp.status_code != 200:
            raise KeboolaApiError(
                message=(
                    f"Developer Portal login failed (HTTP {resp.status_code}). "
                    "Check the identity credentials."
                ),
                error_code=ErrorCode.DP_LOGIN_FAILED,
            )
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("token"):
            return payload["token"]
        # MFA path — implemented in Task 7.
        if isinstance(payload, dict) and payload.get("session"):
            return self._login_with_mfa(username, payload["session"])
        raise KeboolaApiError(
            message="Developer Portal login response missing token and session",
            error_code=ErrorCode.DP_LOGIN_FAILED,
        )

    def _login_with_mfa(self, username: str, session: str) -> str:
        code = _tty_prompt("MFA code: ")
        if not code:
            raise KeboolaApiError(
                message=(
                    "Developer Portal identity requires an MFA code, but no "
                    "interactive terminal is available. Run from a real "
                    "terminal, or switch to a service.{vendor}.{id} "
                    "account (no MFA)."
                ),
                error_code=ErrorCode.DP_MFA_REQUIRED,
            )
        try:
            resp = self._client.post(
                "/auth/login",
                json={"email": username, "session": session, "code": code.strip()},
            )
        except httpx.HTTPError as exc:
            raise KeboolaApiError(
                message=f"Developer Portal MFA login transport error: {exc}",
                error_code=ErrorCode.CONNECTION_ERROR,
            ) from exc
        if resp.status_code != 200:
            raise KeboolaApiError(
                message=f"Developer Portal MFA login failed (HTTP {resp.status_code})",
                error_code=ErrorCode.DP_LOGIN_FAILED,
            )
        payload = resp.json()
        if not isinstance(payload, dict) or not payload.get("token"):
            raise KeboolaApiError(
                message="Developer Portal MFA login response missing token",
                error_code=ErrorCode.DP_LOGIN_FAILED,
            )
        return payload["token"]
