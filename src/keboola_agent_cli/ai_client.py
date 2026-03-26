"""Keboola AI Service API client with retry, timeouts, and token masking.

This module communicates with the Keboola AI Service API for component
documentation, search, and suggestion features. Derives the AI Service
URL from the Storage API stack URL by replacing 'connection.' with 'ai.'
in the hostname.

Inherits shared retry/error logic from BaseHttpClient.
"""

import logging
from typing import Any
from urllib.parse import quote

from . import __version__
from .constants import AI_SERVICE_TIMEOUT
from .http_base import BaseHttpClient

logger = logging.getLogger(__name__)


class AiServiceClient(BaseHttpClient):
    """HTTP client for Keboola AI Service API (component schemas, search, docs).

    Provides methods to fetch component documentation and suggest components
    based on natural language queries, with built-in retry logic (exponential
    backoff for 429/5xx), timeouts, and automatic token masking in error messages.

    Inherits _do_request() and _raise_api_error() from BaseHttpClient.
    """

    def __init__(self, stack_url: str, token: str) -> None:
        self._stack_url = stack_url.rstrip("/")
        ai_base_url = self._derive_service_url(self._stack_url, "ai")
        headers = {
            "X-StorageApi-Token": token,
            "User-Agent": f"keboola-agent-cli/{__version__}",
        }
        super().__init__(
            base_url=ai_base_url,
            token=token,
            headers=headers,
            timeout=AI_SERVICE_TIMEOUT,
        )

    def __enter__(self) -> "AiServiceClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def get_component_detail(self, component_id: str) -> dict[str, Any]:
        """Fetch detailed documentation for a specific component.

        Args:
            component_id: The component identifier (e.g. 'keboola.ex-aws-s3').
                Will be URL-encoded for the request path.

        Returns:
            Dict with component documentation including schema, description,
            and configuration details.

        Raises:
            KeboolaApiError: On API errors (404 if component not found, etc.).
        """
        encoded_id = quote(component_id, safe="")
        response = self._do_request("GET", f"/docs/components/{encoded_id}")
        return response.json()

    def suggest_components(self, query: str) -> list[dict[str, Any]]:
        """Suggest components matching a natural language query.

        Args:
            query: Natural language description of the desired functionality
                (e.g. 'extract data from AWS S3').

        Returns:
            List of component dicts with relevance-ranked suggestions.

        Raises:
            KeboolaApiError: On API errors.
        """
        payload = {"prompt": query}
        response = self._do_request("POST", "/suggest/component", json=payload)
        return response.json().get("components", [])
