"""Keboola OAuth broker HTTP client (``oauth.<stack>``, the ``/manage`` API).

The broker stores one OAuth "consumer" per component: app key, encrypted app
secret, auth and token URLs. Its ``/manage`` routes accept a Manage API
application token with the ``oauth:manage`` scope in ``X-KBC-ManageApiToken``
(keboola/oauth-service ``#[ApplicationTokenAuth(['oauth:manage'])]``).

``GET`` returns ``appSecret`` and ``appSecretDocker`` as ciphertext, never the
plain secret. ``PATCH /manage/{componentId}`` is a partial update; sending
``appSecret`` re-encrypts both fields.

The client is intentionally dumb: token checks, redaction, plan and confirm
live in ``services/oauth_service.py`` and ``commands/oauth.py``.
"""

from __future__ import annotations

from typing import Any, Self
from urllib.parse import quote

from .http_base import BaseHttpClient


class OAuthBrokerClient(BaseHttpClient):
    """HTTP client for the consumer API of one stack's OAuth broker."""

    def __init__(self, oauth_url: str, manage_token: str) -> None:
        super().__init__(
            base_url=oauth_url,
            token=manage_token,
            headers={"X-KBC-ManageApiToken": manage_token, "Accept": "application/json"},
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def list_consumers(self) -> list[dict[str, Any]]:
        """``GET /manage`` -- every consumer registered on the stack."""
        return self._do_request("GET", "/manage").json()

    def get_consumer(self, component_id: str) -> dict[str, Any]:
        """``GET /manage/{componentId}``."""
        return self._do_request("GET", f"/manage/{quote(component_id, safe='')}").json()

    def patch_consumer(self, component_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """``PATCH /manage/{componentId}`` with only the fields that change."""
        response = self._do_request(
            "PATCH", f"/manage/{quote(component_id, safe='')}", json=body, retry_safe=True
        )
        return response.json()
