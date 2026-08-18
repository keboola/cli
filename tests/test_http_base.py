"""Tests for BaseHttpClient - retry logic, error sanitization, shared HTTP behavior."""

import platform
from typing import SupportsIndex
from unittest.mock import patch

import httpx
import pytest

from keboola_agent_cli.constants import (
    APP_NAME,
    MAX_API_ERROR_LENGTH,
    MAX_RETRIES,
    NON_IDEMPOTENT_NOT_RETRIED_HINT,
    UPSTREAM_ERROR_HINT,
)
from keboola_agent_cli.errors import KeboolaApiError
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
    """A credential-minting call must not be replayed on an ambiguous failure.

    Replaying ``POST /v2/storage/tokens`` after a 5xx (or a read timeout) can
    mint a SECOND live token the caller never sees a value for and therefore
    can never revoke -- issue #599.
    """

    @staticmethod
    def _client() -> BaseHttpClient:
        return BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    def test_non_idempotent_500_is_not_retried(self, httpx_mock) -> None:
        """A single 500 ends the call: no replay, no second token."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v2/storage/tokens",
            status_code=500,
            json={"error": "Application error."},
        )

        client = self._client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("POST", "/v2/storage/tokens", idempotent=False)
            assert len(httpx_mock.get_requests()) == 1
            assert exc_info.value.status_code == 500
            # Automated callers read `retryable`; replaying a mint is not safe.
            assert exc_info.value.retryable is False
            assert NON_IDEMPOTENT_NOT_RETRIED_HINT in exc_info.value.message
        finally:
            client.close()

    def test_idempotent_500_still_retries(self, httpx_mock) -> None:
        """The default path is unchanged: a read still gets its 3 attempts."""
        for _ in range(MAX_RETRIES):
            httpx_mock.add_response(url=f"{STACK_URL}/test-path", status_code=500)

        client = self._client()
        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = _noop_sleep  # ty: ignore[invalid-assignment]
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("GET", "/test-path")
            assert len(httpx_mock.get_requests()) == MAX_RETRIES
            assert exc_info.value.retryable is True
            assert NON_IDEMPOTENT_NOT_RETRIED_HINT not in exc_info.value.message
        finally:
            http_base_module.time.sleep = original_sleep
            client.close()

    def test_non_idempotent_429_still_retries(self, httpx_mock) -> None:
        """429 is rejected before the handler runs, so replay cannot duplicate."""
        httpx_mock.add_response(url=f"{STACK_URL}/v2/storage/tokens", status_code=429)
        httpx_mock.add_response(
            url=f"{STACK_URL}/v2/storage/tokens", status_code=201, json={"id": "1"}
        )

        client = self._client()
        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = _noop_sleep  # ty: ignore[invalid-assignment]
        try:
            response = client._do_request("POST", "/v2/storage/tokens", idempotent=False)
            assert response.status_code == 201
            assert len(httpx_mock.get_requests()) == 2
        finally:
            http_base_module.time.sleep = original_sleep
            client.close()

    def test_non_idempotent_read_timeout_is_not_retried(self, httpx_mock) -> None:
        """A read timeout may have been applied server-side -- do not replay it."""
        httpx_mock.add_exception(
            httpx.ReadTimeout("Read timed out"), url=f"{STACK_URL}/v2/storage/tokens"
        )

        client = self._client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("POST", "/v2/storage/tokens", idempotent=False)
            assert len(httpx_mock.get_requests()) == 1
            assert exc_info.value.error_code == "TIMEOUT"
            assert exc_info.value.retryable is False
            assert NON_IDEMPOTENT_NOT_RETRIED_HINT in exc_info.value.message
        finally:
            client.close()

    def test_non_idempotent_connect_timeout_still_retries(self, httpx_mock) -> None:
        """A connect timeout never handed the request over -- replay is safe."""
        httpx_mock.add_exception(
            httpx.ConnectTimeout("Connect timed out"), url=f"{STACK_URL}/v2/storage/tokens"
        )
        httpx_mock.add_response(
            url=f"{STACK_URL}/v2/storage/tokens", status_code=201, json={"id": "1"}
        )

        client = self._client()
        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = _noop_sleep  # ty: ignore[invalid-assignment]
        try:
            response = client._do_request("POST", "/v2/storage/tokens", idempotent=False)
            assert response.status_code == 201
            assert len(httpx_mock.get_requests()) == 2
        finally:
            http_base_module.time.sleep = original_sleep
            client.close()

    def test_non_idempotent_connect_error_still_retries(self, httpx_mock) -> None:
        """A refused connection never reached the handler -- replay is safe."""
        httpx_mock.add_exception(
            httpx.ConnectError("Connection refused"), url=f"{STACK_URL}/v2/storage/tokens"
        )
        httpx_mock.add_response(
            url=f"{STACK_URL}/v2/storage/tokens", status_code=201, json={"id": "1"}
        )

        client = self._client()
        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = _noop_sleep  # ty: ignore[invalid-assignment]
        try:
            response = client._do_request("POST", "/v2/storage/tokens", idempotent=False)
            assert response.status_code == 201
            assert len(httpx_mock.get_requests()) == 2
        finally:
            http_base_module.time.sleep = original_sleep
            client.close()

    def test_non_idempotent_4xx_is_unchanged(self, httpx_mock) -> None:
        """A 403 was never retried and still maps to ACCESS_DENIED, no 5xx hint."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v2/storage/tokens",
            status_code=403,
            json={"error": "You don't have access to the resource."},
        )

        client = self._client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("POST", "/v2/storage/tokens", idempotent=False)
            assert exc_info.value.error_code == "ACCESS_DENIED"
            assert UPSTREAM_ERROR_HINT not in exc_info.value.message
        finally:
            client.close()


class TestUpstreamErrorGuidance:
    """A 5xx should tell the caller it is upstream, and carry the support id."""

    def test_5xx_message_carries_upstream_hint(self, httpx_mock) -> None:
        for _ in range(MAX_RETRIES):
            httpx_mock.add_response(
                url=f"{STACK_URL}/test-path",
                status_code=500,
                json={"error": "Application error."},
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
            assert "Application error." in exc_info.value.message
            assert UPSTREAM_ERROR_HINT in exc_info.value.message
        finally:
            http_base_module.time.sleep = original_sleep
            client.close()

    def test_exception_id_surfaced_when_present(self, httpx_mock) -> None:
        """Keboola's support-traceable id is the first thing support asks for."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            status_code=503,
            json={"error": "Application error.", "exceptionId": "storage-api-abc123"},
        )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("POST", "/test-path", idempotent=False)
            assert "exceptionId: storage-api-abc123" in exc_info.value.message
        finally:
            client.close()

    def test_4xx_has_no_upstream_hint(self, httpx_mock) -> None:
        """A rejected request is the caller's to fix -- do not blame upstream."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/test-path",
            status_code=400,
            json={"error": "Invalid bucket id"},
        )

        client = BaseHttpClient(
            base_url=STACK_URL,
            token=TOKEN,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client._do_request("GET", "/test-path")
            assert UPSTREAM_ERROR_HINT not in exc_info.value.message
            assert exc_info.value.retryable is False
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
