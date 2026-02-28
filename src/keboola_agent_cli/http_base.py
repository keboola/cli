"""Base HTTP client with shared retry, timeout, and error handling logic.

Both KeboolaClient (Storage API) and ManageClient (Manage API) inherit
from BaseHttpClient to avoid duplicating the retry loop, error mapping,
and message sanitization code.
"""

import logging
import time
from typing import Any

import httpx

from .constants import (
    BACKOFF_BASE,
    MAX_API_ERROR_LENGTH,
    MAX_RETRIES,
    MAX_RETRY_AFTER_SECONDS,
    RETRYABLE_STATUS_CODES,
)
from .errors import KeboolaApiError, mask_token

logger = logging.getLogger(__name__)


class BaseHttpClient:
    """Shared HTTP client with retry, timeout, and error handling.

    Provides:
    - _do_request(method, path, **kwargs): HTTP request with retry + backoff
    - _raise_api_error(response, base_url=None): error mapping with truncation
    - Context manager support (close, __enter__, __exit__)

    Subclasses must call super().__init__() with base_url, token, headers,
    and optional timeout.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        headers: dict[str, str],
        timeout: httpx.Timeout | None = None,
    ) -> None:
        from .constants import DEFAULT_TIMEOUT

        self._base_url = base_url.rstrip("/")
        self._token = token
        self._masked_token = mask_token(token)
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=timeout or DEFAULT_TIMEOUT,
            headers=headers,
        )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> "BaseHttpClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _do_request(
        self,
        method: str,
        path: str,
        *,
        client: httpx.Client | None = None,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Execute an HTTP request with retry and exponential backoff.

        Retries on status codes 429, 500, 502, 503, 504 up to MAX_RETRIES times
        with exponential backoff (1s, 2s, 4s).

        Args:
            method: HTTP method (GET, POST, etc.).
            path: URL path relative to base_url.
            client: Optional httpx.Client to use (defaults to self._client).
                Useful for subclasses that maintain multiple clients (e.g. queue client).
            base_url: Optional base URL for error messages (defaults to self._base_url).
            **kwargs: Additional arguments passed to httpx.Client.request().

        Returns:
            The HTTP response on success.

        Raises:
            KeboolaApiError: On HTTP errors (with masked token) or after retries exhausted.
        """
        http_client = client or self._client
        url_label = base_url or self._base_url
        last_response: httpx.Response | None = None

        for attempt in range(MAX_RETRIES):
            try:
                response = http_client.request(method, path, **kwargs)

                if response.status_code < 400:
                    return response

                if response.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES - 1:
                    if response.status_code == 429:
                        retry_after = response.headers.get("Retry-After")
                        if retry_after:
                            try:
                                delay = min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
                            except ValueError:
                                delay = BACKOFF_BASE * (2**attempt)
                        else:
                            delay = BACKOFF_BASE * (2**attempt)
                    else:
                        delay = BACKOFF_BASE * (2**attempt)
                    logger.debug(
                        "Retry attempt %d/%d for %s %s (status %d), delay %.1fs",
                        attempt + 1, MAX_RETRIES, method, path, response.status_code, delay,
                    )
                    time.sleep(delay)
                    last_response = response
                    continue

                self._raise_api_error(response, url_label)

            except httpx.TimeoutException as exc:
                if attempt < MAX_RETRIES - 1:
                    delay = BACKOFF_BASE * (2**attempt)
                    logger.debug(
                        "Retry attempt %d/%d for %s %s (timeout), delay %.1fs",
                        attempt + 1, MAX_RETRIES, method, path, delay,
                    )
                    time.sleep(delay)
                    continue
                raise KeboolaApiError(
                    message=f"Request timed out connecting to {url_label} (token: {self._masked_token})",
                    status_code=0,
                    error_code="TIMEOUT",
                    retryable=True,
                ) from exc

            except httpx.ConnectError as exc:
                if attempt < MAX_RETRIES - 1:
                    delay = BACKOFF_BASE * (2**attempt)
                    logger.debug(
                        "Retry attempt %d/%d for %s %s (connection error), delay %.1fs",
                        attempt + 1, MAX_RETRIES, method, path, delay,
                    )
                    time.sleep(delay)
                    continue
                raise KeboolaApiError(
                    message=f"Cannot connect to {url_label} (token: {self._masked_token})",
                    status_code=0,
                    error_code="CONNECTION_ERROR",
                    retryable=True,
                ) from exc

        if last_response is not None:
            self._raise_api_error(last_response, url_label)

        raise KeboolaApiError(
            message=f"Request failed after {MAX_RETRIES} retries to {url_label} (token: {self._masked_token})",
            status_code=0,
            error_code="RETRY_EXHAUSTED",
            retryable=True,
        )

    def _raise_api_error(self, response: httpx.Response, base_url: str | None = None) -> None:
        """Convert an HTTP error response into a KeboolaApiError.

        Parses the response body for error messages, truncates long messages
        to MAX_API_ERROR_LENGTH characters, and maps status codes to
        appropriate error codes.

        Args:
            response: The HTTP error response.
            base_url: Optional URL label for error messages.

        Raises:
            KeboolaApiError: Always raised with appropriate error code and message.
        """
        status = response.status_code
        url_label = base_url or self._base_url

        try:
            body = response.json()
            api_message = body.get("error", body.get("message", response.text))
        except Exception:
            api_message = response.text

        # Truncate to prevent Rich markup injection and excessive output
        if isinstance(api_message, str) and len(api_message) > MAX_API_ERROR_LENGTH:
            api_message = api_message[:MAX_API_ERROR_LENGTH] + "..."

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
