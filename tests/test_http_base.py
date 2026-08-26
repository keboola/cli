"""Tests for BaseHttpClient - retry logic, error sanitization, shared HTTP behavior."""

import platform
from typing import SupportsIndex
from unittest.mock import patch

import httpx
import pytest

from keboola_agent_cli.constants import (
    APP_NAME,
    MAX_API_ERROR_LENGTH,
    MAX_EXCEPTION_ID_LENGTH,
    MAX_RETRIES,
)
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.http_base import BaseHttpClient, build_user_agent


def _noop_sleep(seconds: SupportsIndex | float, /) -> None:
    """No-op replacement for time.sleep used in retry tests."""


STACK_URL = "https://connection.keboola.com"
TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"


class TestBaseHttpClientRetry:
    """Verify retry logic works via the base class."""

    def test_retry_on_503_then_success(self, httpx_mock) -> None:
        """BaseHttpClient retries on 503 and succeeds on subsequent attempt."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            status_code=503,
            text="Service Unavailable",
        )
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            json={"status": "ok"},
            status_code=200,
        )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = _noop_sleep  # ty: ignore[invalid-assignment]
        try:
            response = client._do_request("GET", "/test-path")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
            assert len(httpx_mock.get_requests()) == 2
        finally:
            http_base_module.time.sleep = original_sleep
            client.close()

    def test_retry_exhausted_raises_error(self, httpx_mock) -> None:
        """BaseHttpClient raises KeboolaApiError after exhausting retries on persistent 500."""
        for _ in range(MAX_RETRIES):
            httpx_mock.add_response(
                url=f"{STACK_URL}/test-path",
                status_code=500,
                text="Internal Server Error",
            )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = _noop_sleep  # ty: ignore[invalid-assignment]
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("GET", "/test-path")
            assert exc_info.value.retryable is True
            assert exc_info.value.status_code == 500
            assert len(httpx_mock.get_requests()) == MAX_RETRIES
        finally:
            http_base_module.time.sleep = original_sleep
            client.close()

    def test_retry_on_429_rate_limit(self, httpx_mock) -> None:
        """BaseHttpClient retries on 429 and succeeds."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            status_code=429,
            text="Rate limit exceeded",
        )
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            json={"result": "ok"},
            status_code=200,
        )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = _noop_sleep  # ty: ignore[invalid-assignment]
        try:
            response = client._do_request("GET", "/test-path")
            assert response.status_code == 200
            assert len(httpx_mock.get_requests()) == 2
        finally:
            http_base_module.time.sleep = original_sleep
            client.close()

    def test_no_retry_on_400(self, httpx_mock) -> None:
        """BaseHttpClient does NOT retry on 400."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            status_code=400,
            json={"error": "Bad request"},
        )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            client._do_request("GET", "/test-path")

        assert exc_info.value.status_code == 400
        assert exc_info.value.retryable is False
        assert len(httpx_mock.get_requests()) == 1
        client.close()

    def test_timeout_retries_then_raises(self, httpx_mock) -> None:
        """BaseHttpClient retries on timeout and raises TIMEOUT error after exhaustion."""
        for _ in range(MAX_RETRIES):
            httpx_mock.add_exception(
                httpx.ReadTimeout("Read timed out"),
                url=f"{STACK_URL}/test-path",
            )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = _noop_sleep  # ty: ignore[invalid-assignment]
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("GET", "/test-path")
            assert exc_info.value.error_code == "TIMEOUT"
            assert exc_info.value.retryable is True
        finally:
            http_base_module.time.sleep = original_sleep
            client.close()

    def test_connect_error_retries_then_raises(self, httpx_mock) -> None:
        """BaseHttpClient retries on connection error and raises after exhaustion."""
        for _ in range(MAX_RETRIES):
            httpx_mock.add_exception(
                httpx.ConnectError("Connection refused"),
                url=f"{STACK_URL}/test-path",
            )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = _noop_sleep  # ty: ignore[invalid-assignment]
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("GET", "/test-path")
            assert exc_info.value.error_code == "CONNECTION_ERROR"
            assert exc_info.value.retryable is True
        finally:
            http_base_module.time.sleep = original_sleep
            client.close()

    def test_alternate_client_parameter(self, httpx_mock) -> None:
        """_do_request accepts alternate client and base_url for queue-like usage."""
        httpx_mock.add_response(
            url="https://queue.keboola.com/test-path",
            json={"queue": True},
            status_code=200,
        )

        base_client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        alt_client = httpx.Client(
            base_url="https://queue.keboola.com",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        try:
            response = base_client._do_request(
                "GET",
                "/test-path",
                client=alt_client,
                base_url="https://queue.keboola.com",
            )
            assert response.status_code == 200
            assert response.json() == {"queue": True}
        finally:
            alt_client.close()
            base_client.close()


class TestNonIdempotentRetryPolicy:
    """A 5xx/transport failure on a non-idempotent method must NOT be retried.

    ``POST`` creates server-side state. Keboola's own token mint persists the
    token row *before* the step that can fail, so a blind retry of a failed
    ``POST /v2/storage/tokens`` can silently leave two live credentials behind
    (issue #599). Only the RFC 9110 idempotent methods are safe to repeat.
    """

    def _client(self) -> BaseHttpClient:
        return BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    def test_post_not_retried_on_500(self, httpx_mock) -> None:
        """A POST answered with 500 fails on the first attempt -- no second mint."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v2/storage/tokens",
            status_code=500,
            json={"error": "Application error."},
        )

        client = self._client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("POST", "/v2/storage/tokens")
            assert exc_info.value.status_code == 500
            assert len(httpx_mock.get_requests()) == 1
        finally:
            client.close()

    def test_post_not_retried_on_502(self, httpx_mock) -> None:
        """The gate is the method, not the specific 5xx code."""
        httpx_mock.add_response(url=f"{STACK_URL}/test-path", status_code=502, text="Bad Gateway")

        client = self._client()
        try:
            with pytest.raises(KeboolaApiError):
                client._do_request("POST", "/test-path")
            assert len(httpx_mock.get_requests()) == 1
        finally:
            client.close()

    def test_patch_not_retried_on_500(self, httpx_mock) -> None:
        """PATCH is not idempotent either (RFC 9110), so it gets the same gate."""
        httpx_mock.add_response(url=f"{STACK_URL}/test-path", status_code=500, text="boom")

        client = self._client()
        try:
            with pytest.raises(KeboolaApiError):
                client._do_request("PATCH", "/test-path")
            assert len(httpx_mock.get_requests()) == 1
        finally:
            client.close()

    def test_post_still_retried_on_429(self, httpx_mock) -> None:
        """429 means the server refused to process it -- repeating is safe."""
        httpx_mock.add_response(url=f"{STACK_URL}/test-path", status_code=429, text="slow down")
        httpx_mock.add_response(url=f"{STACK_URL}/test-path", status_code=200, json={"ok": True})

        client = self._client()
        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = _noop_sleep  # ty: ignore[invalid-assignment]
        try:
            response = client._do_request("POST", "/test-path")
            assert response.status_code == 200
            assert len(httpx_mock.get_requests()) == 2
        finally:
            http_base_module.time.sleep = original_sleep
            client.close()

    def test_put_retried_on_500(self, httpx_mock) -> None:
        """PUT is idempotent, so it keeps the retry safety net."""
        httpx_mock.add_response(url=f"{STACK_URL}/test-path", status_code=500, text="boom")
        httpx_mock.add_response(url=f"{STACK_URL}/test-path", status_code=200, json={"ok": True})

        client = self._client()
        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = _noop_sleep  # ty: ignore[invalid-assignment]
        try:
            response = client._do_request("PUT", "/test-path")
            assert response.status_code == 200
            assert len(httpx_mock.get_requests()) == 2
        finally:
            http_base_module.time.sleep = original_sleep
            client.close()

    def test_delete_retried_on_503(self, httpx_mock) -> None:
        """DELETE is idempotent -- repeating a delete converges on the same state."""
        httpx_mock.add_response(url=f"{STACK_URL}/test-path", status_code=503, text="unavailable")
        httpx_mock.add_response(url=f"{STACK_URL}/test-path", status_code=204)

        client = self._client()
        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = _noop_sleep  # ty: ignore[invalid-assignment]
        try:
            response = client._do_request("DELETE", "/test-path")
            assert response.status_code == 204
            assert len(httpx_mock.get_requests()) == 2
        finally:
            http_base_module.time.sleep = original_sleep
            client.close()

    def test_post_not_retried_on_timeout(self, httpx_mock) -> None:
        """A timed-out POST may already have taken effect -- never repeat it."""
        httpx_mock.add_exception(httpx.ReadTimeout("Read timed out"), url=f"{STACK_URL}/test-path")

        client = self._client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("POST", "/test-path")
            assert exc_info.value.error_code == "TIMEOUT"
            assert len(httpx_mock.get_requests()) == 1
        finally:
            client.close()

    def test_post_retried_on_connect_error(self, httpx_mock) -> None:
        """A refused connection never reached the server, so a POST may repeat."""
        httpx_mock.add_exception(httpx.ConnectError("Connection refused"), url=f"{STACK_URL}/x")
        httpx_mock.add_response(url=f"{STACK_URL}/x", status_code=200, json={"ok": True})

        client = self._client()
        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = _noop_sleep  # ty: ignore[invalid-assignment]
        try:
            response = client._do_request("POST", "/x")
            assert response.status_code == 200
            assert len(httpx_mock.get_requests()) == 2
        finally:
            http_base_module.time.sleep = original_sleep
            client.close()


class TestServerErrorGuidance:
    """A 5xx must tell the operator what to do next (issue #599)."""

    def _client(self) -> BaseHttpClient:
        return BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    def test_unretried_500_names_the_partial_effect_risk(self, httpx_mock) -> None:
        """The POST hint must warn the operation may already have taken effect."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v2/storage/tokens",
            status_code=500,
            json={"error": "Application error."},
        )

        client = self._client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("POST", "/v2/storage/tokens")
            message = exc_info.value.message
            assert "POST" in message
            assert "not retried" in message
            assert "may already have taken effect" in message
        finally:
            client.close()

    def test_exhausted_500_points_at_an_upstream_incident(self, httpx_mock) -> None:
        """A 500 that survives every retry is an incident, not a caller mistake."""
        for _ in range(MAX_RETRIES):
            httpx_mock.add_response(
                url=f"{STACK_URL}/test-path",
                status_code=500,
                json={"error": "Application error."},
            )

        client = self._client()
        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = _noop_sleep  # ty: ignore[invalid-assignment]
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("GET", "/test-path")
            message = exc_info.value.message
            assert f"{MAX_RETRIES} attempts" in message
            assert "upstream Keboola incident" in message
            assert "support" in message
        finally:
            http_base_module.time.sleep = original_sleep
            client.close()

    def test_exception_id_surfaced_for_support(self, httpx_mock) -> None:
        """Keboola's exceptionId is the only handle support can trace -- keep it."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            status_code=500,
            json={
                "error": "Application error.",
                "exceptionId": "kbc-eu-central-1-connection-abc123",
                "message": "Please contact our support",
            },
        )

        client = self._client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("POST", "/test-path")
            assert "kbc-eu-central-1-connection-abc123" in exc_info.value.message
        finally:
            client.close()

    def test_rate_limited_then_500_on_a_post_warns_about_partial_effect(self, httpx_mock) -> None:
        """A POST can reach attempt 2 via a 429 -- the 5xx there is still its first.

        The hint must be the partial-effect warning, not "upstream incident":
        telling an operator to escalate instead of checking what already
        landed is exactly how the duplicate this PR prevents gets created.
        """
        httpx_mock.add_response(url=f"{STACK_URL}/test-path", status_code=429, text="slow down")
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path", status_code=500, json={"error": "Application error."}
        )

        client = self._client()
        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = _noop_sleep  # ty: ignore[invalid-assignment]
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("POST", "/test-path")
            message = exc_info.value.message
            assert "may already have taken effect" in message
            assert "upstream Keboola incident" not in message
            assert exc_info.value.retryable is False
        finally:
            http_base_module.time.sleep = original_sleep
            client.close()

    def test_exhausted_hint_counts_server_errors_not_total_attempts(self, httpx_mock) -> None:
        """A 429 burned an attempt but was not a 5xx -- do not count it as one."""
        httpx_mock.add_response(url=f"{STACK_URL}/test-path", status_code=429, text="slow down")
        for _ in range(MAX_RETRIES - 1):
            httpx_mock.add_response(
                url=f"{STACK_URL}/test-path", status_code=500, json={"error": "boom"}
            )

        client = self._client()
        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = _noop_sleep  # ty: ignore[invalid-assignment]
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("GET", "/test-path")
            message = exc_info.value.message
            assert "upstream Keboola incident" in message
            assert f"{MAX_RETRIES - 1} attempts" in message
            assert f"{MAX_RETRIES} attempts" not in message
        finally:
            http_base_module.time.sleep = original_sleep
            client.close()

    def test_exception_id_is_length_capped(self, httpx_mock) -> None:
        """An untrusted id must not reach the terminal unbounded."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            status_code=500,
            json={"error": "Application error.", "exceptionId": "a" * 5000},
        )

        client = self._client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("POST", "/test-path")
            assert "a" * MAX_EXCEPTION_ID_LENGTH in exc_info.value.message
            assert "a" * (MAX_EXCEPTION_ID_LENGTH + 1) not in exc_info.value.message
        finally:
            client.close()

    def test_exception_id_markup_and_newlines_stripped(self, httpx_mock) -> None:
        """Human-mode errors render through Rich with markup ON (output.py).

        A server-supplied id carrying brackets would be interpreted as markup,
        and newlines would let it forge extra log lines (CWE-117). Neither may
        survive into the message.
        """
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            status_code=500,
            json={
                "error": "Application error.",
                "exceptionId": "kbc-1[bold red]spoof[/bold red]\nError: fake\x07",
            },
        )

        client = self._client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("POST", "/test-path")
            message = exc_info.value.message
            assert "[bold red]" not in message
            assert "\n" not in message
            assert "\x07" not in message
            # the legitimate characters survive so support still gets a handle
            assert "kbc-1" in message
        finally:
            client.close()

    def test_non_string_exception_id_ignored(self, httpx_mock) -> None:
        """A numeric/object `exceptionId` is not an id -- drop it silently."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            status_code=500,
            json={"error": "Application error.", "exceptionId": {"nested": 1}},
        )

        client = self._client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("POST", "/test-path")
            assert "exceptionId" not in exc_info.value.message
        finally:
            client.close()

    def test_no_hint_appended_to_client_errors(self, httpx_mock) -> None:
        """A 4xx is the caller's problem -- the incident hint would be misleading."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            status_code=400,
            json={"error": "Bad request"},
        )

        client = self._client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("POST", "/test-path")
            message = exc_info.value.message
            assert "not retried" not in message
            assert "upstream Keboola incident" not in message
        finally:
            client.close()


class TestBaseHttpClientErrorSanitization:
    """Verify message truncation and error mapping in the base class."""

    def test_long_error_message_truncated(self, httpx_mock) -> None:
        """API error messages longer than MAX_API_ERROR_LENGTH are truncated."""
        long_message = "A" * 1000
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            json={"error": long_message},
            status_code=400,
        )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            client._do_request("GET", "/test-path")

        # Full 1000-char message should NOT appear
        assert long_message not in exc_info.value.message
        # Truncated message (500 chars + "...") should be present
        assert "A" * MAX_API_ERROR_LENGTH + "..." in exc_info.value.message
        client.close()

    def test_short_error_message_not_truncated(self, httpx_mock) -> None:
        """Short API error messages are kept intact."""
        short_message = "Bad request"
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            json={"error": short_message},
            status_code=400,
        )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            client._do_request("GET", "/test-path")

        assert short_message in exc_info.value.message
        client.close()

    def test_exactly_max_length_not_truncated(self, httpx_mock) -> None:
        """Error message of exactly MAX_API_ERROR_LENGTH is not truncated."""
        exact_message = "B" * MAX_API_ERROR_LENGTH
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            json={"error": exact_message},
            status_code=400,
        )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            client._do_request("GET", "/test-path")

        # Exactly MAX_API_ERROR_LENGTH chars should not be truncated
        assert exact_message in exc_info.value.message
        client.close()

    def test_rich_markup_contained_by_truncation(self, httpx_mock) -> None:
        """Rich markup brackets in error messages are contained by truncation."""
        malicious_msg = "[bold red]" + "X" * 600 + "[/bold red]"
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            json={"error": malicious_msg},
            status_code=400,
        )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            client._do_request("GET", "/test-path")

        # Full malicious markup should not appear
        assert malicious_msg not in exc_info.value.message
        # Should be truncated
        assert "..." in exc_info.value.message
        client.close()

    def test_non_json_error_body_handled(self, httpx_mock) -> None:
        """Non-JSON error response body is handled gracefully."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            text="<html>500 Internal Error</html>",
            status_code=400,
        )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            client._do_request("GET", "/test-path")

        assert "500" in exc_info.value.message
        client.close()

    def test_int_error_field_falls_back_to_description(self, httpx_mock) -> None:
        """Metastore returns `{"error": 422, "description": "..."}` — the int
        in `error` must not shadow the real message in `description`."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            json={"error": 422, "description": "field type not allowed"},
            status_code=422,
        )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            client._do_request("GET", "/test-path")

        # The real message must be surfaced; the int `422` must not.
        assert "field type not allowed" in exc_info.value.message
        # And we must not be left with a bare "API error 422: 422"
        # (the regression this test pins).
        assert "API error 422: 422" not in exc_info.value.message
        client.close()

    def test_description_field_used_when_no_message(self, httpx_mock) -> None:
        """FastAPI returns `{"description": "..."}` by default — surface it."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            json={"description": "validation failed"},
            status_code=400,
        )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            client._do_request("GET", "/test-path")

        assert "validation failed" in exc_info.value.message
        client.close()

    def test_errors_list_shape_serialised(self, httpx_mock) -> None:
        """Metastore 422 ships `{"errors": [{...}]}` (list of dicts) — must
        be json-serialised so the message contains the diagnostic content,
        not the Python list repr."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            json={
                "errors": [
                    {"loc": ["body", "name"], "msg": "field required"},
                ],
            },
            status_code=422,
        )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            client._do_request("GET", "/test-path")

        # Both the field path and the human message must be present.
        assert "field required" in exc_info.value.message
        assert "name" in exc_info.value.message
        client.close()

    def test_detail_list_shape_serialised(self, httpx_mock) -> None:
        """FastAPI 422 uses `{"detail": [{...}]}` (the canonical pydantic
        validation shape). Same serialisation rule as `errors`."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            json={
                "detail": [
                    {"loc": ["query", "limit"], "msg": "value is not a valid integer"},
                ],
            },
            status_code=422,
        )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            client._do_request("GET", "/test-path")

        assert "value is not a valid integer" in exc_info.value.message
        assert "limit" in exc_info.value.message
        client.close()

    def test_401_maps_to_invalid_token(self, httpx_mock) -> None:
        """401 status code maps to INVALID_TOKEN error code."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            json={"error": "Invalid token"},
            status_code=401,
        )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            client._do_request("GET", "/test-path")

        assert exc_info.value.error_code == "INVALID_TOKEN"
        assert exc_info.value.status_code == 401
        assert exc_info.value.retryable is False
        client.close()

    def test_403_maps_to_access_denied(self, httpx_mock) -> None:
        """403 status code maps to ACCESS_DENIED error code."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            json={"error": "Forbidden"},
            status_code=403,
        )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            client._do_request("GET", "/test-path")

        assert exc_info.value.error_code == "ACCESS_DENIED"
        assert exc_info.value.status_code == 403
        assert exc_info.value.retryable is False
        client.close()

    def test_404_maps_to_not_found(self, httpx_mock) -> None:
        """404 status code maps to NOT_FOUND error code."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            json={"error": "Not found"},
            status_code=404,
        )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            client._do_request("GET", "/test-path")

        assert exc_info.value.error_code == "NOT_FOUND"
        assert exc_info.value.status_code == 404
        assert exc_info.value.retryable is False
        client.close()

    def test_token_masked_in_error_messages(self, httpx_mock) -> None:
        """Full token never appears in error messages."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            json={"error": "Some error"},
            status_code=401,
        )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            client._do_request("GET", "/test-path")

        # Full token must NOT appear in the error message
        assert TOKEN not in exc_info.value.message
        # Masked form should appear
        assert "901-...XXXX" in exc_info.value.message
        client.close()


class TestConversationIdHeader:
    """Test X-Conversation-ID header propagation from env var."""

    def test_conversation_id_header_set_when_env_present(self, httpx_mock, monkeypatch) -> None:
        """X-Conversation-ID header is sent when KBAGENT_CONVERSATION_ID is set."""
        monkeypatch.setenv("KBAGENT_CONVERSATION_ID", "conv-abc-123")

        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            json={"ok": True},
            status_code=200,
        )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        client._do_request("GET", "/test-path")
        client.close()

        request = httpx_mock.get_request()
        assert request.headers["X-Conversation-ID"] == "conv-abc-123"

    def test_conversation_id_header_absent_when_env_not_set(self, httpx_mock, monkeypatch) -> None:
        """X-Conversation-ID header is NOT sent when env var is unset."""
        monkeypatch.delenv("KBAGENT_CONVERSATION_ID", raising=False)

        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            json={"ok": True},
            status_code=200,
        )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        client._do_request("GET", "/test-path")
        client.close()

        request = httpx_mock.get_request()
        assert "X-Conversation-ID" not in request.headers

    def test_conversation_id_header_absent_when_env_empty(self, httpx_mock, monkeypatch) -> None:
        """X-Conversation-ID header is NOT sent when env var is empty string."""
        monkeypatch.setenv("KBAGENT_CONVERSATION_ID", "")

        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            json={"ok": True},
            status_code=200,
        )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        client._do_request("GET", "/test-path")
        client.close()

        request = httpx_mock.get_request()
        assert "X-Conversation-ID" not in request.headers


class TestUserAgent:
    """Test the centralized User-Agent that signs every Keboola API call."""

    def test_build_user_agent_components(self) -> None:
        """UA carries product/version plus OS, arch, and Python interpreter."""
        ua = build_user_agent()
        assert ua.startswith(f"{APP_NAME}/")
        # Comment section with host metadata: "(<os> <release>; <arch>; <impl> <ver>)"
        assert platform.system() in ua
        assert platform.machine() in ua
        assert platform.python_implementation() in ua
        assert platform.python_version() in ua

    def test_build_user_agent_no_hostname_pii(self) -> None:
        """Hostname (platform.node()) is PII and must never leak into the UA."""
        node = platform.node()
        if node:
            assert node not in build_user_agent()

    def test_base_client_sends_enriched_user_agent(self, httpx_mock) -> None:
        """BaseHttpClient sets the enriched UA centrally, even if caller omits it."""
        httpx_mock.add_response(url=f"{STACK_URL}/test-path", json={"ok": True}, status_code=200)

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"X-StorageApi-Token": TOKEN},
        )
        client._do_request("GET", "/test-path")
        client.close()

        request = httpx_mock.get_request()
        assert request.headers["User-Agent"] == build_user_agent()
        assert platform.system() in request.headers["User-Agent"]

    def test_base_client_overrides_caller_user_agent(self, httpx_mock) -> None:
        """A caller-supplied UA is replaced by the canonical one (one signature for the fleet)."""
        httpx_mock.add_response(url=f"{STACK_URL}/test-path", json={"ok": True}, status_code=200)

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"X-StorageApi-Token": TOKEN, "User-Agent": "stale/0.0.1"},
        )
        client._do_request("GET", "/test-path")
        client.close()

        request = httpx_mock.get_request()
        assert request.headers["User-Agent"] == build_user_agent()
        assert "stale/0.0.1" not in request.headers["User-Agent"]


class TestResolveAppName:
    """Dynamic distribution-name resolution (PyPI rename bridge, #424).

    One codebase must run under BOTH `keboola-cli` (current) and the legacy
    `keboola-agent-cli` distribution (the migration-bridge wheel), so
    `version()` / User-Agent never break depending on how the user installed.
    """

    def _patch_version(self, installed: set[str]):
        from importlib.metadata import PackageNotFoundError

        def fake_version(name: str) -> str:
            if name in installed:
                return "0.63.1"
            raise PackageNotFoundError(name)

        return patch("keboola_agent_cli.constants.version", side_effect=fake_version)

    def test_prefers_current_name(self) -> None:
        from keboola_agent_cli.constants import _resolve_app_name

        with self._patch_version({"keboola-cli", "keboola-agent-cli"}):
            assert _resolve_app_name() == "keboola-cli"

    def test_falls_back_to_legacy_name(self) -> None:
        from keboola_agent_cli.constants import _resolve_app_name

        # Installed via the bridge wheel -> only the legacy distribution exists.
        with self._patch_version({"keboola-agent-cli"}):
            assert _resolve_app_name() == "keboola-agent-cli"

    def test_defaults_to_current_when_neither_installed(self) -> None:
        from keboola_agent_cli.constants import APP_NAME_CANDIDATES, _resolve_app_name

        # Editable/source checkout without built metadata -> stable default.
        with self._patch_version(set()):
            assert _resolve_app_name() == APP_NAME_CANDIDATES[0] == "keboola-cli"


class TestBaseHttpClientContextManager:
    """Test context manager protocol on BaseHttpClient."""

    def test_context_manager(self, httpx_mock) -> None:
        """BaseHttpClient works as a context manager."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            json={"ok": True},
            status_code=200,
        )

        with BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client:
            response = client._do_request("GET", "/test-path")
            assert response.status_code == 200


class TestUnauthorizedErrorMapping:
    """A 401 is not automatically a bad credential (issue #711)."""

    def _client(self) -> BaseHttpClient:
        return BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    def test_metastore_project_scope_401_is_not_blamed_on_the_token(self, httpx_mock) -> None:
        """The reported case: Metastore 401s for a token Storage accepts.

        Calling that "Invalid or expired token" sent the reporter checking
        expiry and master-token status for a server-side fault.
        """
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            status_code=401,
            json={
                "error": 401,
                "code": "401",
                "exception": "Failed to create project scope",
                "exceptionId": "metastore-fbfeCiBSXXBLk7D",
                "status": "error",
            },
        )

        client = self._client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("GET", "/test-path")
            exc = exc_info.value
            assert exc.error_code == ErrorCode.AUTH_REJECTED
            assert exc.status_code == 401
            assert exc.retryable is False
            assert "Failed to create project scope" in exc.message
            # The wrong diagnosis must be gone, not merely de-emphasised.
            assert "Invalid or expired token" not in exc.message
        finally:
            client.close()

    def test_non_token_401_keeps_the_exception_id(self, httpx_mock) -> None:
        """The handle support traces by must survive onto a 401.

        It used to be appended only after the 401 branch had already raised,
        so the reporter had to fall back to raw curl to obtain it.
        """
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            status_code=401,
            json={
                "exception": "Failed to create project scope",
                "exceptionId": "metastore-fbfeCiBSXXBLk7D",
            },
        )

        client = self._client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("GET", "/test-path")
            assert "metastore-fbfeCiBSXXBLk7D" in exc_info.value.message
        finally:
            client.close()

    def test_genuine_invalid_token_401_still_maps_to_invalid_token(self, httpx_mock) -> None:
        """The long-standing mapping is unchanged when the API does blame the token."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            status_code=401,
            json={"error": "Invalid access token", "code": "401"},
        )

        client = self._client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("GET", "/test-path")
            exc = exc_info.value
            assert exc.error_code == ErrorCode.INVALID_TOKEN
            assert "Invalid or expired token" in exc.message
        finally:
            client.close()

    def test_expired_token_401_still_maps_to_invalid_token(self, httpx_mock) -> None:
        """Expiry phrasing that never mentions the word "token" is still a token fault."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            status_code=401,
            json={"error": "Your session has expired, please sign in again"},
        )

        client = self._client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("GET", "/test-path")
            assert exc_info.value.error_code == ErrorCode.INVALID_TOKEN
        finally:
            client.close()

    def test_401_masks_the_token_under_both_mappings(self, httpx_mock) -> None:
        """Neither branch may echo the credential in full."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            status_code=401,
            json={"exception": "Failed to create project scope"},
        )

        client = self._client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("GET", "/test-path")
            assert TOKEN not in exc_info.value.message
        finally:
            client.close()

    def test_403_keeps_the_exception_id(self, httpx_mock) -> None:
        """Same early-return gap as the 401 branch."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            status_code=403,
            json={"error": "You don't have access", "exceptionId": "kbc-connection-deadbeef"},
        )

        client = self._client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("GET", "/test-path")
            exc = exc_info.value
            assert exc.error_code == ErrorCode.ACCESS_DENIED
            assert "kbc-connection-deadbeef" in exc.message
        finally:
            client.close()

    def test_404_keeps_the_exception_id(self, httpx_mock) -> None:
        """Same early-return gap as the 401 branch."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            status_code=404,
            json={"error": "Bucket not found", "exceptionId": "kbc-connection-cafe1234"},
        )

        client = self._client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("GET", "/test-path")
            exc = exc_info.value
            assert exc.error_code == ErrorCode.NOT_FOUND
            assert "kbc-connection-cafe1234" in exc.message
        finally:
            client.close()

    def test_401_without_exception_id_adds_no_empty_suffix(self, httpx_mock) -> None:
        """A stack that sends no exceptionId must not gain a dangling marker."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            status_code=401,
            json={"error": "Invalid access token"},
        )

        client = self._client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("GET", "/test-path")
            assert "exceptionId" not in exc_info.value.message
        finally:
            client.close()

    def test_401_with_no_message_at_all_stays_invalid_token(self, httpx_mock) -> None:
        """Silence is not the server blaming something else.

        A 401 with an empty body is the textbook rejected-credential
        response; diverting it to AUTH_REJECTED would over-read the silence.
        """
        httpx_mock.add_response(url=f"{STACK_URL}/test-path", status_code=401, json={})

        client = self._client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("GET", "/test-path")
            assert exc_info.value.error_code == ErrorCode.INVALID_TOKEN
        finally:
            client.close()


class TestApiErrorCodeDetails:
    """The body's machine string `code` must survive into KeboolaApiError.details."""

    def _client(self) -> BaseHttpClient:
        return BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"X-StorageApi-Token": TOKEN},
        )

    def test_body_code_lands_in_details(self, httpx_mock) -> None:
        # The merge 409's "not ready" shape -- the message carries only the
        # human `error` text, so `code` in details is the ONLY machine handle
        # (MergeRequestService.merge() branches on it, DMD-1899).
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            status_code=409,
            json={
                "error": "Cannot merge, another merge request is processing.",
                "code": "storage.mergeRequests.notReadyToMerge",
            },
        )
        client = self._client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("GET", "/test-path")
            assert (
                exc_info.value.details["api_error_code"] == "storage.mergeRequests.notReadyToMerge"
            )
        finally:
            client.close()

    def test_no_code_means_no_details_key(self, httpx_mock) -> None:
        # The merge 409's conflict shape carries no string code -- details
        # must not invent one.
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            status_code=409,
            json={"error": "Configuration was changed in the default branch."},
        )
        client = self._client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("GET", "/test-path")
            assert "api_error_code" not in exc_info.value.details
        finally:
            client.close()

    def test_non_string_code_ignored(self, httpx_mock) -> None:
        # Keboola Metastore puts an int HTTP status into `error`; guard the
        # same way against a non-string `code`.
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            status_code=404,
            json={"error": "not found", "code": 404},
        )
        client = self._client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("GET", "/test-path")
            assert "api_error_code" not in exc_info.value.details
        finally:
            client.close()
