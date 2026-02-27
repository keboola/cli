"""Keboola Manage API client with retry, timeouts, and token masking.

This module communicates with the Keboola Manage API for organization-level
operations like listing projects and creating Storage API tokens.
Uses a different auth header (X-KBC-ManageApiToken) than the Storage API client.
"""

import time
from typing import Any

import httpx

from . import __version__
from .errors import KeboolaApiError, mask_token

# Reuse same retry constants as the Storage API client
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
BACKOFF_BASE = 1.0  # seconds; delays: 1s, 2s, 4s


class ManageClient:
    """HTTP client for the Keboola Manage API.

    Provides methods to list organization projects and create Storage API
    tokens, with built-in retry logic (exponential backoff for 429/5xx),
    timeouts, and automatic token masking in error messages.
    """

    def __init__(self, stack_url: str, manage_token: str) -> None:
        self._stack_url = stack_url.rstrip("/")
        self._manage_token = manage_token
        self._masked_token = mask_token(manage_token)
        self._client = httpx.Client(
            base_url=self._stack_url,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
            headers={
                "X-KBC-ManageApiToken": manage_token,
                "User-Agent": f"keboola-agent-cli/{__version__}",
            },
        )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> "ManageClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _do_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
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

        # Truncate to prevent Rich markup injection and excessive output
        max_api_error_length = 500
        if isinstance(api_message, str) and len(api_message) > max_api_error_length:
            api_message = api_message[:max_api_error_length] + "..."

        if status == 401:
            raise KeboolaApiError(
                message=f"Invalid or expired manage token (token: {self._masked_token}): {api_message}",
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

    def list_organization_projects(self, org_id: int) -> list[dict[str, Any]]:
        """List all projects in an organization.

        Args:
            org_id: The organization ID.

        Returns:
            List of project dicts with at least 'id' and 'name' fields.

        Raises:
            KeboolaApiError: On API errors.
        """
        response = self._do_request("GET", f"/manage/organizations/{org_id}/projects")
        return response.json()

    def create_project_token(
        self,
        project_id: int,
        description: str,
        can_manage_buckets: bool = True,
        can_read_all_file_uploads: bool = True,
    ) -> dict[str, Any]:
        """Create a new Storage API token for a project.

        Args:
            project_id: The project ID.
            description: Token description.
            can_manage_buckets: Whether the token can manage buckets.
            can_read_all_file_uploads: Whether the token can read all file uploads.

        Returns:
            Token dict including the 'token' field (shown only once).

        Raises:
            KeboolaApiError: On API errors.
        """
        payload = {
            "description": description,
            "canManageBuckets": can_manage_buckets,
            "canReadAllFileUploads": can_read_all_file_uploads,
        }
        response = self._do_request(
            "POST", f"/manage/projects/{project_id}/tokens", json=payload
        )
        return response.json()
