"""Microsoft Entra ID token endpoint, used only to prove a new client secret works.

``kbagent oauth rotate-azure-secret`` asks Entra for a client-credentials
token with the secret it just created. A 200 proves Entra accepts the secret;
the token itself is discarded.

Requests go out once per call with no retry: a rejected secret is the normal
answer for a few seconds after it was created, and the caller
(``services/_oauth_tools.check_client_secret``) owns the wait-and-retry loop.
The secret is only ever in the form body; errors carry the status code and
Entra's ``error`` code, never the request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

import httpx

from .http_base import BaseHttpClient

ENTRA_LOGIN_URL = "https://login.microsoftonline.com"
GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"


@dataclass
class EntraTokenAttempt:
    """Outcome of one token request: HTTP status (0 = no answer) and error code."""

    status_code: int
    error: str


class EntraClient(BaseHttpClient):
    """Client-credentials token requests against login.microsoftonline.com."""

    def __init__(self) -> None:
        super().__init__(base_url=ENTRA_LOGIN_URL, token="", headers={"Accept": "application/json"})

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def client_credentials(self, tenant_id: str, app_id: str, secret: str) -> EntraTokenAttempt:
        """Request one token; never raises for an HTTP or network failure."""
        form = {
            "grant_type": "client_credentials",
            "client_id": app_id,
            "client_secret": secret,
            "scope": GRAPH_DEFAULT_SCOPE,
        }
        try:
            response = self._client.post(f"/{tenant_id}/oauth2/v2.0/token", data=form)
        except httpx.HTTPError as exc:
            return EntraTokenAttempt(status_code=0, error=f"network error: {type(exc).__name__}")
        if response.status_code == 200:
            return EntraTokenAttempt(status_code=200, error="")
        try:
            body = response.json()
        except ValueError:
            body = {}
        error = body.get("error", "") if isinstance(body, dict) else ""
        return EntraTokenAttempt(status_code=response.status_code, error=str(error))
