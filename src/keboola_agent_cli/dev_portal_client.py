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
import urllib.error
import urllib.request
from typing import Any

import httpx

from .constants import DP_MFA_CHALLENGE_TYPE, MAX_API_ERROR_LENGTH
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

    @property
    def bearer(self) -> str | None:
        """The active bearer token, or None if not yet authenticated.

        In-memory only; never written to disk. Exposed so the service can
        reuse one login across a prepare/apply pair (see seed_bearer) instead
        of re-authenticating — which, on a personal MFA account, would prompt
        for a second MFA code on a single write.
        """
        return self._bearer

    def seed_bearer(self, bearer: str) -> None:
        """Reuse a bearer obtained by an earlier client for the same identity.

        Lets the service carry one authenticated session across the
        prepare -> (random-code confirm) -> apply flow without a second login.
        """
        self._bearer = bearer
        self._client.headers["Authorization"] = bearer

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
        """Confirm an MFA-gated login.

        Per the Keboola Developer Portal apiary spec, the same POST /auth/login
        endpoint accepts {email, session, code, challenge}. The `challenge`
        field is documented as optional with default SOFTWARE_TOKEN_MFA, but
        in practice the server rejects calls that omit it (404 with the
        misleading "must be one of" enum message attached to the admin schema).
        Send it explicitly. Single attempt only -- /auth/login consumes the
        session, so any retry on the same session always 404s with "Invalid
        code or auth state for the user" regardless of the new challenge type.
        """
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
        body = {
            "email": username,
            "session": session,
            "code": code.strip(),
            "challenge": DP_MFA_CHALLENGE_TYPE,
        }
        try:
            resp = self._client.post("/auth/login", json=body)
        except httpx.HTTPError as exc:
            raise KeboolaApiError(
                message=f"Developer Portal MFA login transport error: {exc}",
                error_code=ErrorCode.CONNECTION_ERROR,
            ) from exc
        if resp.status_code == 200:
            payload = resp.json()
            if isinstance(payload, dict) and payload.get("token"):
                return payload["token"]
            raise KeboolaApiError(
                message=(
                    "Developer Portal MFA login returned HTTP 200 but no "
                    f"'token' field in response: {payload!r}"
                ),
                error_code=ErrorCode.DP_LOGIN_FAILED,
            )
        try:
            body_text = resp.text[:MAX_API_ERROR_LENGTH]
        except (UnicodeDecodeError, AttributeError):
            body_text = "<unreadable>"
        raise KeboolaApiError(
            message=(
                f"Developer Portal MFA login failed (HTTP {resp.status_code}): "
                f"{body_text}. If your TOTP code rotates every 30s, this is "
                "often a stale code -- retry promptly. If the server says "
                "'Invalid code or auth state' on a fresh session, the code "
                "itself was wrong."
            ),
            error_code=ErrorCode.DP_LOGIN_FAILED,
        )

    # ----- Reads -----

    def list_apps(self, vendor: str) -> list[dict[str, Any]]:
        self._ensure_authenticated()
        resp = self._do_request("GET", f"/vendors/{vendor}/apps?limit=1000")
        if resp.status_code != 200:
            self._raise_dp_error(resp, action="list apps", vendor=vendor)
        payload = resp.json()
        if isinstance(payload, dict) and "apps" in payload:
            return list(payload["apps"])
        if isinstance(payload, list):
            return payload
        return []

    def get_app(self, vendor: str, app_id: str) -> dict[str, Any]:
        self._ensure_authenticated()
        try:
            resp = self._do_request("GET", f"/vendors/{vendor}/apps/{app_id}")
        except KeboolaApiError as exc:
            if exc.error_code == ErrorCode.NOT_FOUND:
                raise KeboolaApiError(
                    message=f"Developer Portal app '{app_id}' not found in vendor '{vendor}'",
                    error_code=ErrorCode.DP_APP_NOT_FOUND,
                ) from exc
            raise
        return resp.json()

    # ----- Writes -----

    def create_app(self, vendor: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_authenticated()
        resp = self._do_request("POST", f"/vendors/{vendor}/apps", json=payload)
        if resp.status_code not in (200, 201):
            self._raise_dp_error(resp, action="create app", vendor=vendor)
        return resp.json()

    def patch_app(self, vendor: str, app_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """PATCH an app. Routes by identity role:
        - admin -> PATCH /admin/apps/{app_id} (permissive schema, accepts the
          9 fields forbidden() on the vendor schema: complexity, categories,
          forwardToken, forwardTokenDetails, injectEnvironment, processTimeout,
          requiredMemory, features, category).
        - vendor -> PATCH /vendors/{vendor}/apps/{app_id} (default, restricted
          schema). The `vendor` arg is still required for the path.
        """
        self._ensure_authenticated()
        if self._identity.role_hint == "admin":
            path = f"/admin/apps/{app_id}"
        else:
            path = f"/vendors/{vendor}/apps/{app_id}"
        resp = self._do_request("PATCH", path, json=payload)
        if resp.status_code not in (200, 204):
            self._raise_dp_error(resp, action="patch app", vendor=vendor, app_id=app_id)
        return resp.json() if resp.content else {}

    def publish_app(self, vendor: str, app_id: str) -> dict[str, Any]:
        self._ensure_authenticated()
        resp = self._do_request("POST", f"/vendors/{vendor}/apps/{app_id}/publish")
        if resp.status_code not in (200, 202):
            self._raise_dp_error(resp, action="publish app", vendor=vendor, app_id=app_id)
        return resp.json() if resp.content else {"status": "submitted"}

    def deprecate_app(self, vendor: str, app_id: str) -> dict[str, Any]:
        self._ensure_authenticated()
        resp = self._do_request("POST", f"/vendors/{vendor}/apps/{app_id}/deprecate")
        if resp.status_code not in (200, 202):
            self._raise_dp_error(resp, action="deprecate app", vendor=vendor, app_id=app_id)
        return resp.json() if resp.content else {"status": "deprecated"}

    def upload_icon(self, vendor: str, app_id: str, png_bytes: bytes) -> None:
        """Two-hop icon upload: ask the portal for a presigned S3 URL, then PUT bytes there.

        The S3 PUT does NOT use this client's httpx instance (no retry, no auth,
        no User-Agent injection). We use urllib directly so the wire shape stays
        exactly what S3 expects.
        """
        self._ensure_authenticated()
        try:
            resp = self._do_request("POST", f"/vendors/{vendor}/apps/{app_id}/icon")
        except KeboolaApiError as exc:
            raise KeboolaApiError(
                message=(f"Developer Portal failed to mint icon-upload URL: {exc.message}"),
                error_code=ErrorCode.DP_ICON_UPLOAD_FAILED,
            ) from exc
        if resp.status_code != 200:
            raise KeboolaApiError(
                message=(
                    f"Developer Portal failed to mint icon-upload URL (HTTP {resp.status_code})"
                ),
                error_code=ErrorCode.DP_ICON_UPLOAD_FAILED,
            )
        payload = resp.json()
        link = payload.get("link") if isinstance(payload, dict) else None
        if not link:
            raise KeboolaApiError(
                message="Developer Portal icon-upload response missing 'link'",
                error_code=ErrorCode.DP_ICON_UPLOAD_FAILED,
            )
        req = urllib.request.Request(
            link,
            data=png_bytes,
            headers={"Content-Type": "image/png"},
            method="PUT",
        )
        try:
            with urllib.request.urlopen(req) as s3_resp:
                if getattr(s3_resp, "status", 200) >= 300:
                    raise KeboolaApiError(
                        message=f"Icon S3 PUT failed (HTTP {s3_resp.status})",
                        error_code=ErrorCode.DP_ICON_UPLOAD_FAILED,
                    )
        except urllib.error.HTTPError as exc:
            raise KeboolaApiError(
                message=f"Icon S3 PUT failed (HTTP {exc.code}): {exc.reason}",
                error_code=ErrorCode.DP_ICON_UPLOAD_FAILED,
            ) from exc

    # ----- Error mapping -----

    def _raise_dp_error(
        self,
        resp: httpx.Response,
        *,
        action: str,
        vendor: str | None = None,
        app_id: str | None = None,
    ) -> None:
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        ctx = f"{action}"
        if vendor:
            ctx += f" (vendor={vendor})"
        if app_id:
            ctx += f" (app={app_id})"
        raise KeboolaApiError(
            message=f"Developer Portal {ctx} failed (HTTP {resp.status_code}): {body}",
            error_code=ErrorCode.API_ERROR,
        )
