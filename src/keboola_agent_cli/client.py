"""Keboola API client with retry, timeouts, and token masking.

This is the only module that communicates with the Keboola Storage API.
All HTTP details, endpoint URLs, and error mapping are encapsulated here.
"""

import time
from typing import Any

import httpx

from . import __version__
from .errors import KeboolaApiError, mask_token
from .models import TokenVerifyResponse

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
BACKOFF_BASE = 1.0  # seconds; delays: 1s, 2s, 4s


class KeboolaClient:
    """HTTP client for the Keboola Storage API.

    Provides methods to interact with Keboola endpoints with built-in
    retry logic (exponential backoff for 429/5xx), timeouts, and
    automatic token masking in error messages.
    """

    def __init__(self, stack_url: str, token: str) -> None:
        self._stack_url = stack_url.rstrip("/")
        self._token = token
        self._masked_token = mask_token(token)
        self._client = httpx.Client(
            base_url=self._stack_url,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
            headers={
                "X-StorageApi-Token": token,
                "User-Agent": f"keboola-agent-cli/{__version__}",
            },
        )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> "KeboolaClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Execute an HTTP request with retry and exponential backoff.

        Retries on status codes 429, 500, 502, 503, 504 up to MAX_RETRIES times
        with exponential backoff (1s, 2s, 4s).

        Raises:
            KeboolaApiError: On HTTP errors (with masked token) or after retries exhausted.
        """
        last_response: httpx.Response | None = None

        for attempt in range(MAX_RETRIES):
            try:
                response = self._client.request(method, path, **kwargs)

                if response.status_code < 400:
                    return response

                if response.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES - 1:
                    delay = BACKOFF_BASE * (2**attempt)
                    time.sleep(delay)
                    last_response = response
                    continue

                self._raise_api_error(response)

            except httpx.TimeoutException as exc:
                if attempt < MAX_RETRIES - 1:
                    delay = BACKOFF_BASE * (2**attempt)
                    time.sleep(delay)
                    continue
                raise KeboolaApiError(
                    message=f"Request timed out connecting to {self._stack_url} (token: {self._masked_token})",
                    status_code=0,
                    error_code="TIMEOUT",
                    retryable=True,
                ) from exc

            except httpx.ConnectError as exc:
                if attempt < MAX_RETRIES - 1:
                    delay = BACKOFF_BASE * (2**attempt)
                    time.sleep(delay)
                    continue
                raise KeboolaApiError(
                    message=f"Cannot connect to {self._stack_url} (token: {self._masked_token})",
                    status_code=0,
                    error_code="CONNECTION_ERROR",
                    retryable=True,
                ) from exc

        if last_response is not None:
            self._raise_api_error(last_response)

        raise KeboolaApiError(
            message=f"Request failed after {MAX_RETRIES} retries to {self._stack_url} (token: {self._masked_token})",
            status_code=0,
            error_code="RETRY_EXHAUSTED",
            retryable=True,
        )

    def _raise_api_error(self, response: httpx.Response) -> None:
        """Convert an HTTP error response into a KeboolaApiError."""
        status = response.status_code

        try:
            body = response.json()
            api_message = body.get("error", body.get("message", response.text))
        except Exception:
            api_message = response.text

        if status == 401:
            raise KeboolaApiError(
                message=f"Invalid or expired token (token: {self._masked_token}): {api_message}",
                status_code=status,
                error_code="INVALID_TOKEN",
                retryable=False,
            )

        if status == 403:
            raise KeboolaApiError(
                message=f"Access denied (token: {self._masked_token}): {api_message}",
                status_code=status,
                error_code="ACCESS_DENIED",
                retryable=False,
            )

        if status == 404:
            raise KeboolaApiError(
                message=f"Resource not found: {api_message}",
                status_code=status,
                error_code="NOT_FOUND",
                retryable=False,
            )

        retryable = status in RETRYABLE_STATUS_CODES
        raise KeboolaApiError(
            message=f"API error {status} from {self._stack_url} (token: {self._masked_token}): {api_message}",
            status_code=status,
            error_code="API_ERROR",
            retryable=retryable,
        )

    def verify_token(self) -> TokenVerifyResponse:
        """Verify the storage API token and retrieve project information.

        Returns:
            TokenVerifyResponse with project name, ID, and token description.

        Raises:
            KeboolaApiError: If token is invalid (401) or other API error.
        """
        response = self._request("GET", "/v2/storage/tokens/verify")
        data = response.json()

        return TokenVerifyResponse(
            token_id=str(data.get("id", "")),
            token_description=data.get("description", ""),
            project_id=data.get("owner", {}).get("id", 0),
            project_name=data.get("owner", {}).get("name", ""),
            owner_name=data.get("owner", {}).get("name", ""),
        )

    def list_components(self, component_type: str | None = None) -> list[dict[str, Any]]:
        """List components with their configurations.

        Args:
            component_type: Optional filter (extractor, writer, transformation, application).

        Returns:
            List of component dicts from the API.
        """
        params: dict[str, str] = {"include": "configuration"}
        if component_type:
            params["componentType"] = component_type

        response = self._request("GET", "/v2/storage/components", params=params)
        return response.json()

    def get_config_detail(self, component_id: str, config_id: str) -> dict[str, Any]:
        """Get detailed information about a specific configuration.

        Args:
            component_id: The component ID (e.g. keboola.ex-db-snowflake).
            config_id: The configuration ID.

        Returns:
            Configuration detail dict from the API.
        """
        response = self._request(
            "GET",
            f"/v2/storage/components/{component_id}/configs/{config_id}",
        )
        return response.json()
