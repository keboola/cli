"""Base HTTP client with shared retry, timeout, and error handling logic.

Both KeboolaClient (Storage API) and ManageClient (Manage API) inherit
from BaseHttpClient to avoid duplicating the retry loop, error mapping,
and message sanitization code.
"""

import json
import logging
import os
import platform
import re
import time
from typing import Any, Self
from urllib.parse import urlparse, urlunparse

import httpx

from .constants import (
    APP_NAME,
    BACKOFF_BASE,
    ENV_CONVERSATION_ID,
    MAX_API_ERROR_LENGTH,
    MAX_EXCEPTION_ID_LENGTH,
    MAX_RETRIES,
    MAX_RETRY_AFTER_SECONDS,
    RETRY_SAFE_METHODS,
    RETRYABLE_STATUS_CODES,
    TOKEN_VALIDITY_ERROR_MARKERS,
    UNINFORMATIVE_ERROR_MESSAGES,
)
from .errors import ErrorCode, KeboolaApiError, mask_token

logger = logging.getLogger(__name__)

# Everything a real Keboola `exceptionId` is made of. Anything else in that
# server-supplied field -- Rich markup brackets, newlines that would forge an
# extra log line (CWE-117), control characters -- is dropped before the id is
# interpolated into an error message.
_EXCEPTION_ID_DISALLOWED = re.compile(r"[^A-Za-z0-9._:-]+")


def build_user_agent() -> str:
    """Build the User-Agent that signs every Keboola API call.

    Format (RFC 7231 product + comment):

        keboola-cli/<version> (<os> <release>; <arch>; <impl> <pyver>)
        e.g. keboola-cli/0.45.0 (Darwin 25.3.0; arm64; CPython 3.12.7)

    Keboola's edge logs this verbatim (DataDog access logs), so the fleet can
    be segmented by version and OS/arch. Only neutral host metadata is sent --
    never ``platform.node()`` (the hostname is PII). Identity ("which project /
    user") is resolved server-side from the token, never derived client-side.
    """
    from . import __version__

    return (
        f"{APP_NAME}/{__version__} "
        f"({platform.system()} {platform.release()}; "
        f"{platform.machine()}; "
        f"{platform.python_implementation()} {platform.python_version()})"
    )


class BaseHttpClient:
    """Shared HTTP client with retry, timeout, and error handling.

    Provides:
    - _do_request(method, path, **kwargs): HTTP request with retry + backoff
    - _raise_api_error(response, base_url=None): error mapping with truncation
    - Context manager support (close, __enter__, __exit__)

    Subclasses must call super().__init__() with base_url, token, headers,
    and optional timeout.

    ``http_auth`` is an additive, keyword-only ``httpx.Auth`` hook (e.g.
    ``auth.token_provider.BearerAuth``) passed straight through to the
    underlying ``httpx.Client``. It defaults to ``None``, which is
    byte-identical to the client's behaviour before this parameter existed:
    static-token callers are completely unaffected.
    """

    # Name of the surface this client speaks to, for subclasses that only
    # understand a static Storage token. ``None`` means the client supports
    # bearer sessions (KeboolaClient / ManageClient / AuthClient) and must not be
    # guarded. Set it and the sentinel can no longer reach the wire through a
    # constructor whose caller forgot to guard it.
    SESSION_AUTH_FEATURE: str | None = None

    def __init__(
        self,
        base_url: str,
        token: str,
        headers: dict[str, str],
        timeout: httpx.Timeout | None = None,
        *,
        http_auth: httpx.Auth | None = None,
    ) -> None:
        from .auth.sentinel import require_static_token
        from .constants import DEFAULT_TIMEOUT

        if http_auth is None and self.SESSION_AUTH_FEATURE is not None:
            require_static_token(token, feature=self.SESSION_AUTH_FEATURE)

        self._base_url = base_url.rstrip("/")
        self._token = token
        self._masked_token = mask_token(token)
        self._http_auth = http_auth
        # Sign every request centrally so all subclasses share one UA string
        # (and OS/version enrichment) instead of hardcoding it five times.
        headers["User-Agent"] = build_user_agent()
        conversation_id = os.environ.get(ENV_CONVERSATION_ID, "")
        if conversation_id:
            headers["X-Conversation-ID"] = conversation_id
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=timeout or DEFAULT_TIMEOUT,
            headers=headers,
            auth=http_auth,
        )

    @staticmethod
    def _derive_service_url(stack_url: str, service_prefix: str) -> str:
        """Derive a service base URL by replacing 'connection.' in the hostname.

        E.g. _derive_service_url("https://connection.keboola.com", "queue")
             -> "https://queue.keboola.com"
        """
        parsed = urlparse(stack_url)
        hostname = parsed.hostname or ""
        new_host = hostname.replace("connection.", f"{service_prefix}.", 1)
        if new_host == hostname:
            logger.warning(
                "%s URL derivation did not change hostname: %s",
                service_prefix,
                hostname,
            )
        return urlunparse(parsed._replace(netloc=new_host))

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> Self:
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
        retry_safe: bool | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Execute an HTTP request with retry and exponential backoff.

        Retries on status codes 429, 500, 502, 503, 504 up to MAX_RETRIES times
        with exponential backoff (1s, 2s, 4s).

        A 5xx (and a read/write timeout) is only repeated on an idempotent
        method -- see ``RETRY_SAFE_METHODS``. Repeating a failed POST/PATCH can
        duplicate server-side state the caller never gets to see, so those fail
        on the first attempt with a message saying so (issue #599). A 429 and a
        refused connection are repeated on every method: in both cases the
        server provably did not process the request.

        Args:
            method: HTTP method (GET, POST, etc.).
            path: URL path relative to base_url.
            client: Optional httpx.Client to use (defaults to self._client).
                Useful for subclasses that maintain multiple clients (e.g. queue client).
            base_url: Optional base URL for error messages (defaults to self._base_url).
            retry_safe: Override the method-based idempotency verdict. Pass
                ``False`` for a request whose method looks idempotent but whose
                SERVER-SIDE MEANING changes once it has succeeded -- the
                canonical case is ``DELETE`` on a component configuration,
                where a repeat lands on the now-trashed config and purges it
                permanently. ``None`` (default) keeps the method-based rule.
            **kwargs: Additional arguments passed to httpx.Client.request().

        Returns:
            The HTTP response on success.

        Raises:
            KeboolaApiError: On HTTP errors (with masked token) or after retries exhausted.
        """
        http_client = client or self._client
        url_label = base_url or self._base_url
        last_response: httpx.Response | None = None
        # An explicit override wins: RETRY_SAFE_METHODS reasons about the METHOD,
        # but idempotency is a property of the endpoint. See the `retry_safe` arg.
        retry_safe = method.upper() in RETRY_SAFE_METHODS if retry_safe is None else retry_safe
        # Counted separately from `attempt`: a 429 burns an attempt without
        # being a server error, so using the attempt index would report "the
        # same 5xx came back on N attempts" after seeing exactly one.
        server_error_attempts = 0

        for attempt in range(MAX_RETRIES):
            try:
                response = http_client.request(method, path, **kwargs)

                if response.status_code < 400:
                    return response

                if response.status_code >= 500:
                    server_error_attempts += 1
                # 429 is repeatable on any method; a 5xx only on an idempotent one.
                may_repeat = retry_safe or response.status_code == 429
                if (
                    response.status_code in RETRYABLE_STATUS_CODES
                    and may_repeat
                    and attempt < MAX_RETRIES - 1
                ):
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
                        attempt + 1,
                        MAX_RETRIES,
                        method,
                        path,
                        response.status_code,
                        delay,
                    )
                    time.sleep(delay)
                    last_response = response
                    continue

                hint = self._server_error_hint(
                    method, response.status_code, server_error_attempts, retry_safe=retry_safe
                )
                self._raise_api_error(
                    response,
                    url_label,
                    hint=hint,
                    retryable=may_repeat and response.status_code in RETRYABLE_STATUS_CODES,
                )

            except httpx.TimeoutException as exc:
                # A connect/pool timeout never delivered the request, so it is
                # repeatable on any method. A read/write timeout means the
                # request WAS sent and the outcome is unknown -- only repeat it
                # when the method is idempotent.
                timeout_repeatable = retry_safe or isinstance(
                    exc, httpx.ConnectTimeout | httpx.PoolTimeout
                )
                if timeout_repeatable and attempt < MAX_RETRIES - 1:
                    delay = BACKOFF_BASE * (2**attempt)
                    logger.debug(
                        "Retry attempt %d/%d for %s %s (timeout), delay %.1fs",
                        attempt + 1,
                        MAX_RETRIES,
                        method,
                        path,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                unsafe_note = "" if timeout_repeatable else f" {self._non_idempotent_note(method)}"
                raise KeboolaApiError(
                    message=(
                        f"Request timed out connecting to {url_label} "
                        f"(token: {self._masked_token}){unsafe_note}"
                    ),
                    status_code=0,
                    error_code=ErrorCode.TIMEOUT,
                    retryable=timeout_repeatable,
                ) from exc

            except httpx.ConnectError as exc:
                if attempt < MAX_RETRIES - 1:
                    delay = BACKOFF_BASE * (2**attempt)
                    logger.debug(
                        "Retry attempt %d/%d for %s %s (connection error), delay %.1fs",
                        attempt + 1,
                        MAX_RETRIES,
                        method,
                        path,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                raise KeboolaApiError(
                    message=f"Cannot connect to {url_label} (token: {self._masked_token})",
                    status_code=0,
                    error_code=ErrorCode.CONNECTION_ERROR,
                    retryable=True,
                ) from exc

        if last_response is not None:
            self._raise_api_error(
                last_response,
                url_label,
                hint=self._server_error_hint(
                    method, last_response.status_code, server_error_attempts
                ),
            )

        raise KeboolaApiError(
            message=f"Request failed after {MAX_RETRIES} retries to {url_label} (token: {self._masked_token})",
            status_code=0,
            error_code=ErrorCode.RETRY_EXHAUSTED,
            retryable=True,
        )

    @staticmethod
    def _safe_exception_id(raw: object) -> str:
        """Bound and de-fang the server-supplied ``exceptionId``.

        The id is untrusted input that ends up in a message rendered through
        Rich with markup enabled (``OutputFormatter.error``), so it gets the
        same "bounded before it reaches a terminal" treatment the API's own
        error text gets from MAX_API_ERROR_LENGTH. Disallowed characters are
        dropped rather than escaped: a real Keboola id contains none of them,
        so this is lossless in practice, and support still gets a handle out
        of a partially mangled value instead of nothing.
        """
        if not isinstance(raw, str):
            return ""
        return _EXCEPTION_ID_DISALLOWED.sub("", raw)[:MAX_EXCEPTION_ID_LENGTH]

    @staticmethod
    def _is_token_validity_401(api_message: str) -> bool:
        """Should this HTTP 401 keep the long-standing INVALID_TOKEN mapping?

        True in two cases:

        * The body describes a bad or expired credential, the way Keboola's
          own 401s do ("Invalid access token", "Access token expired").
        * The body says nothing at all. A 401 with no detail is the textbook
          rejected-credential response, and claiming "the API did not report
          the token as invalid" would be over-reading silence.

        False only when the server said something substantive that blames
        something else -- Metastore's `"Failed to create project scope"`, an
        internal project-scope resolution failure for a token the Storage API
        accepts on the very same stack (issue #711).

        Deliberately permissive in the True direction: an unrecognised message
        that DOES mention a token keeps the historical mapping, so the new
        code appears only where the server demonstrably blamed something else.
        """
        lowered = api_message.strip().lower()
        if lowered in UNINFORMATIVE_ERROR_MESSAGES:
            return True
        return any(marker in lowered for marker in TOKEN_VALIDITY_ERROR_MARKERS)

    @staticmethod
    def _non_idempotent_note(method: str) -> str:
        """One sentence naming the partial-effect risk of an unrepeated write."""
        return (
            f"{method.upper()} is not idempotent, so this request was not retried -- "
            "the operation may already have taken effect server-side; verify the resource "
            "state before trying again."
        )

    @classmethod
    def _server_error_hint(
        cls,
        method: str,
        status: int,
        server_error_attempts: int,
        *,
        retry_safe: bool | None = None,
    ) -> str | None:
        """Return the actionable next step for a 5xx, or None if there isn't one.

        Two situations need two different answers (issue #599). A 5xx on a
        non-idempotent write means the server may already have done the work,
        so the next step is to check before repeating. A 5xx that survived
        every retry is the opposite: an upstream incident nobody on this side
        can fix, and the next step is to escalate with the exceptionId. A
        generic "API error 500" said neither.

        The method gate is checked FIRST and `server_error_attempts` counts
        only 5xx responses, because a POST can reach a second attempt via a
        429. Ordering it the other way round told the operator to escalate on
        exactly the request where they most needed to go and check what had
        already landed.
        """
        if status < 500:
            return None
        effective_safe = method.upper() in RETRY_SAFE_METHODS if retry_safe is None else retry_safe
        if not effective_safe:
            return cls._non_idempotent_note(method)
        if server_error_attempts > 1:
            return (
                f"The same 5xx came back on all {server_error_attempts} attempts, which points "
                "at an upstream Keboola incident rather than a caller mistake -- check "
                "status.keboola.com and contact Keboola support, quoting the exceptionId above."
            )
        return None

    def _raise_api_error(
        self,
        response: httpx.Response,
        base_url: str | None = None,
        *,
        hint: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        """Convert an HTTP error response into a KeboolaApiError.

        Parses the response body for error messages, truncates long messages
        to MAX_API_ERROR_LENGTH characters, and maps status codes to
        appropriate error codes.

        Args:
            response: The HTTP error response.
            base_url: Optional URL label for error messages.
            hint: Optional actionable next step appended to a 5xx message
                (see :meth:`_server_error_hint`).
            retryable: Overrides the status-derived ``retryable`` flag. A 500
                on a POST is in RETRYABLE_STATUS_CODES but must NOT be
                advertised as retryable -- kbagent deliberately did not repeat
                it, and neither should the caller without checking first.

        Raises:
            KeboolaApiError: Always raised with appropriate error code and message.
        """
        status = response.status_code
        url_label = base_url or self._base_url

        exception_id = ""
        details: dict = {}
        try:
            body = response.json()
            # Keboola answers a 5xx with a generic `error` ("Application
            # error.") plus an `exceptionId` -- the ONLY handle Keboola support
            # can trace the incident by. Dropping it, as this parser used to,
            # left the operator with nothing to escalate (issue #599).
            if isinstance(body, dict):
                exception_id = self._safe_exception_id(body.get("exceptionId"))
                # Keboola user errors also carry a machine-readable string
                # `code` (e.g. `storage.mergeRequests.notReadyToMerge`).
                # Surface it in details so a service can branch on it -- the
                # message alone holds only the human `error` text (DMD-1899;
                # the merge 409's two shapes differ exactly by this field).
                api_error_code = body.get("code")
                if isinstance(api_error_code, str) and api_error_code:
                    details["api_error_code"] = api_error_code
                # A Package HttpException additionally serializes its context
                # as `params` (ExceptionConverter) -- e.g. the merge-conflict
                # 409 carries the conflicting configurations in
                # `params.errors`. Surface it so a caller does not have to
                # re-fetch data the error already delivered.
                api_error_params = body.get("params")
                if isinstance(api_error_params, dict) and api_error_params:
                    details["api_error_params"] = api_error_params
            # Real Keboola APIs answer with one of these keys in priority
            # order. Two caveats:
            #   1. Keboola Metastore puts the HTTP status code into `error`
            #      as an int (e.g. {"error": 422}) -- using `or` would
            #      shadow the actual error message in `errors`/`exception`.
            #      So we only accept `error` if it's a non-empty string.
            #   2. `errors`/`detail` are lists of dicts (FastAPI / metastore
            #      422 shape); we json.dumps them so the f-string render
            #      below doesn't print `[{...}]` repr.
            err_field = body.get("error")
            api_message = (
                err_field
                if isinstance(err_field, str) and err_field
                else (
                    body.get("exception")
                    or body.get("message")
                    or body.get("description")
                    or body.get("detail")
                    or body.get("errors")
                    or json.dumps(body)
                )
            )
            if not isinstance(api_message, str):
                api_message = json.dumps(api_message)
        except Exception:
            api_message = response.text

        # Truncate to prevent Rich markup injection and excessive output
        if isinstance(api_message, str) and len(api_message) > MAX_API_ERROR_LENGTH:
            api_message = api_message[:MAX_API_ERROR_LENGTH] + "..."

        # The exceptionId belongs on EVERY error, not just the 5xx family.
        # These three branches used to raise before it was appended, so the
        # one handle Keboola support traces an incident by was dropped on
        # exactly the responses -- a 401 from Metastore -- where the fault was
        # server-side and the operator most needed to escalate (issue #711;
        # the 5xx half of this was fixed in #599). Bounded by
        # `_safe_exception_id`, same as below.
        id_suffix = f" [exceptionId: {exception_id}]" if exception_id else ""

        if status == 401:
            # A 401 is not automatically a credential problem. When the
            # server's own text does not describe one, say what it actually
            # said instead of asserting the token is bad -- see
            # TOKEN_VALIDITY_ERROR_MARKERS.
            if self._is_token_validity_401(api_message):
                raise KeboolaApiError(
                    message=(
                        f"Invalid or expired token (token: {self._masked_token}): "
                        f"{api_message}{id_suffix}"
                    ),
                    status_code=status,
                    error_code=ErrorCode.INVALID_TOKEN,
                    retryable=False,
                )
            raise KeboolaApiError(
                message=(
                    f"Authentication rejected by {url_label} with HTTP 401, but the API did "
                    f"not report the token as invalid or expired (token: "
                    f"{self._masked_token}): {api_message}{id_suffix}. Rotating the token is "
                    "unlikely to help -- verify it against another endpoint on the same stack "
                    "(`kbagent project status`), then escalate to Keboola support quoting the "
                    "exceptionId above."
                ),
                status_code=status,
                error_code=ErrorCode.AUTH_REJECTED,
                retryable=False,
                details=details,
            )

        if status == 403:
            raise KeboolaApiError(
                message=(f"Access denied (token: {self._masked_token}): {api_message}{id_suffix}"),
                status_code=status,
                error_code=ErrorCode.ACCESS_DENIED,
                retryable=False,
                details=details,
            )

        if status == 404:
            raise KeboolaApiError(
                message=f"Resource not found: {api_message}{id_suffix}",
                status_code=status,
                error_code=ErrorCode.NOT_FOUND,
                retryable=False,
                details=details,
            )

        # Appended AFTER the truncation above so they always survive into the
        # message the operator actually reads. That is safe only because each
        # is bounded on its own: the hint is a kbagent-authored constant, and
        # the id went through `_safe_exception_id`. Never append raw
        # server-supplied text here -- the truncation is what keeps the
        # console (Rich, markup enabled) from rendering it as markup.
        suffix = id_suffix
        if hint:
            suffix += f" {hint}"
        raise KeboolaApiError(
            message=(
                f"API error {status} from {url_label} "
                f"(token: {self._masked_token}): {api_message}{suffix}"
            ),
            status_code=status,
            error_code=ErrorCode.API_ERROR,
            retryable=status in RETRYABLE_STATUS_CODES if retryable is None else retryable,
            details=details,
        )
