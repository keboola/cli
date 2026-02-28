"""Tests for BaseHttpClient - retry logic, error sanitization, shared HTTP behavior."""

import httpx
import pytest

from keboola_agent_cli.constants import MAX_API_ERROR_LENGTH, MAX_RETRIES
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.http_base import BaseHttpClient

STACK_URL = "https://connection.keboola.com"
TOKEN = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"


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
        http_base_module.time.sleep = lambda x: None
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
        http_base_module.time.sleep = lambda x: None
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
        http_base_module.time.sleep = lambda x: None
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
        http_base_module.time.sleep = lambda x: None
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
        http_base_module.time.sleep = lambda x: None
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
                "GET", "/test-path",
                client=alt_client,
                base_url="https://queue.keboola.com",
            )
            assert response.status_code == 200
            assert response.json() == {"queue": True}
        finally:
            alt_client.close()
            base_client.close()


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
        assert "901-...pt0k" in exc_info.value.message
        client.close()


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
