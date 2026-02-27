"""Keboola API client with retry, timeouts, and token masking.

This is the only module that communicates with the Keboola Storage API
and the Keboola Queue API. All HTTP details, endpoint URLs, and error
mapping are encapsulated here.
"""

import time
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

import httpx

from . import __version__
from .errors import KeboolaApiError, mask_token
from .models import TokenVerifyResponse

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
BACKOFF_BASE = 1.0  # seconds; delays: 1s, 2s, 4s


class KeboolaClient:
    """HTTP client for the Keboola Storage API and Queue API.

    Provides methods to interact with Keboola endpoints with built-in
    retry logic (exponential backoff for 429/5xx), timeouts, and
    automatic token masking in error messages.
    """

    def __init__(self, stack_url: str, token: str) -> None:
        self._stack_url = stack_url.rstrip("/")
        self._token = token
        self._masked_token = mask_token(token)
        self._headers = {
            "X-StorageApi-Token": token,
            "User-Agent": f"keboola-agent-cli/{__version__}",
        }
        self._client = httpx.Client(
            base_url=self._stack_url,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
            headers=self._headers,
        )
        self._queue_client: httpx.Client | None = None

    @property
    def _queue_base_url(self) -> str:
        """Derive Queue API base URL from the Storage API URL.

        Replaces 'connection.' with 'queue.' in the hostname.
        E.g. https://connection.keboola.com -> https://queue.keboola.com
        """
        parsed = urlparse(self._stack_url)
        hostname = parsed.hostname or ""
        queue_host = hostname.replace("connection.", "queue.", 1)
        return urlunparse(parsed._replace(netloc=queue_host))

    def close(self) -> None:
        """Close the underlying HTTP clients."""
        self._client.close()
        if self._queue_client is not None:
            self._queue_client.close()

    def __enter__(self) -> "KeboolaClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _do_request(
        self, client: httpx.Client, base_url: str, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        """Execute an HTTP request with retry and exponential backoff.

        Shared retry logic for both Storage and Queue API clients.

        Retries on status codes 429, 500, 502, 503, 504 up to MAX_RETRIES times
        with exponential backoff (1s, 2s, 4s).

        Raises:
            KeboolaApiError: On HTTP errors (with masked token) or after retries exhausted.
        """
        last_response: httpx.Response | None = None

        for attempt in range(MAX_RETRIES):
            try:
                response = client.request(method, path, **kwargs)

                if response.status_code < 400:
                    return response

                if response.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES - 1:
                    delay = BACKOFF_BASE * (2**attempt)
                    time.sleep(delay)
                    last_response = response
                    continue

                self._raise_api_error(response, base_url)

            except httpx.TimeoutException as exc:
                if attempt < MAX_RETRIES - 1:
                    delay = BACKOFF_BASE * (2**attempt)
                    time.sleep(delay)
                    continue
                raise KeboolaApiError(
                    message=f"Request timed out connecting to {base_url} (token: {self._masked_token})",
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
                    message=f"Cannot connect to {base_url} (token: {self._masked_token})",
                    status_code=0,
                    error_code="CONNECTION_ERROR",
                    retryable=True,
                ) from exc

        if last_response is not None:
            self._raise_api_error(last_response, base_url)

        raise KeboolaApiError(
            message=f"Request failed after {MAX_RETRIES} retries to {base_url} (token: {self._masked_token})",
            status_code=0,
            error_code="RETRY_EXHAUSTED",
            retryable=True,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Execute a Storage API request with retry."""
        return self._do_request(self._client, self._stack_url, method, path, **kwargs)

    def _queue_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Execute a Queue API request with retry. Lazily creates the queue client."""
        if self._queue_client is None:
            self._queue_client = httpx.Client(
                base_url=self._queue_base_url,
                timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
                headers=self._headers,
            )
        return self._do_request(
            self._queue_client, self._queue_base_url, method, path, **kwargs
        )

    def _raise_api_error(self, response: httpx.Response, base_url: str | None = None) -> None:
        """Convert an HTTP error response into a KeboolaApiError."""
        status = response.status_code
        url_label = base_url or self._stack_url

        try:
            body = response.json()
            api_message = body.get("error", body.get("message", response.text))
        except Exception:
            api_message = response.text

        # Truncate to prevent Rich markup injection and excessive output
        max_api_error_length = 500
        if isinstance(api_message, str) and len(api_message) > max_api_error_length:
            api_message = api_message[:max_api_error_length] + "..."

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
            message=f"API error {status} from {url_label} (token: {self._masked_token}): {api_message}",
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
        safe_component_id = quote(component_id, safe="")
        safe_config_id = quote(config_id, safe="")
        response = self._request(
            "GET",
            f"/v2/storage/components/{safe_component_id}/configs/{safe_config_id}",
        )
        return response.json()

    def list_buckets(self, include: str | None = None) -> list[dict[str, Any]]:
        """List storage buckets with optional extended information.

        Args:
            include: Optional include parameter (e.g. "linkedBuckets" for sharing info).

        Returns:
            List of bucket dicts from the API.
        """
        params: dict[str, str] = {}
        if include:
            params["include"] = include
        response = self._request("GET", "/v2/storage/buckets", params=params)
        return response.json()

    def list_jobs(
        self,
        component_id: str | None = None,
        config_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List jobs from the Queue API.

        Args:
            component_id: Optional filter by component ID.
            config_id: Optional filter by config ID (requires component_id).
            status: Optional filter by job status.
            limit: Max number of jobs to return (1-500, default 50).
            offset: Offset for pagination.

        Returns:
            List of job dicts from the Queue API.
        """
        params: dict[str, str | int] = {"limit": limit, "offset": offset}
        if component_id:
            params["component"] = component_id
        if config_id:
            params["config"] = config_id
        if status:
            params["status"] = status

        response = self._queue_request("GET", "/search/jobs", params=params)
        return response.json()

    def get_job_detail(self, job_id: str) -> dict[str, Any]:
        """Get detailed information about a specific job from the Queue API.

        Args:
            job_id: The job ID.

        Returns:
            Job detail dict from the Queue API.
        """
        safe_job_id = quote(job_id, safe="")
        response = self._queue_request("GET", f"/jobs/{safe_job_id}")
        return response.json()
