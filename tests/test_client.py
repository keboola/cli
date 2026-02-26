"""Tests for KeboolaClient - verify_token, retries, timeouts, error handling."""

import httpx
import pytest

from keboola_agent_cli.client import MAX_RETRIES, KeboolaClient
from keboola_agent_cli.errors import KeboolaApiError

VERIFY_TOKEN_RESPONSE = {
    "id": "12345",
    "description": "My test token",
    "owner": {
        "id": 1234,
        "name": "Test Project",
    },
}


class TestVerifyToken:
    """Tests for verify_token() success and failure paths."""

    def test_verify_token_success(self, httpx_mock) -> None:
        """verify_token() returns TokenVerifyResponse with project info on success."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            json=VERIFY_TOKEN_RESPONSE,
            status_code=200,
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )
        result = client.verify_token()

        assert result.project_name == "Test Project"
        assert result.project_id == 1234
        assert result.token_description == "My test token"
        assert result.token_id == "12345"
        client.close()

    def test_verify_token_401_error(self, httpx_mock) -> None:
        """verify_token() raises KeboolaApiError with INVALID_TOKEN on 401."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            json={"error": "Invalid access token"},
            status_code=401,
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            client.verify_token()

        assert exc_info.value.error_code == "INVALID_TOKEN"
        assert exc_info.value.status_code == 401
        assert exc_info.value.retryable is False
        client.close()

    def test_verify_token_403_error(self, httpx_mock) -> None:
        """verify_token() raises KeboolaApiError with ACCESS_DENIED on 403."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            json={"error": "Access denied"},
            status_code=403,
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            client.verify_token()

        assert exc_info.value.error_code == "ACCESS_DENIED"
        assert exc_info.value.status_code == 403
        client.close()


class TestRetryBehavior:
    """Tests for retry on 5xx and 429 status codes."""

    def test_retry_on_503_then_success(self, httpx_mock) -> None:
        """Client retries on 503 and succeeds on subsequent attempt."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            status_code=503,
            text="Service Unavailable",
        )
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            json=VERIFY_TOKEN_RESPONSE,
            status_code=200,
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )

        # Monkeypatch time.sleep to avoid actual delays in tests
        import keboola_agent_cli.client as client_module

        original_sleep = client_module.time.sleep
        client_module.time.sleep = lambda x: None
        try:
            result = client.verify_token()
            assert result.project_name == "Test Project"
        finally:
            client_module.time.sleep = original_sleep
            client.close()

    def test_retry_exhausted_raises_error(self, httpx_mock) -> None:
        """Client raises KeboolaApiError after exhausting retries on persistent 503."""
        for _ in range(MAX_RETRIES):
            httpx_mock.add_response(
                url="https://connection.keboola.com/v2/storage/tokens/verify",
                status_code=503,
                text="Service Unavailable",
            )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )

        import keboola_agent_cli.client as client_module

        original_sleep = client_module.time.sleep
        client_module.time.sleep = lambda x: None
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client.verify_token()
            assert exc_info.value.retryable is True
        finally:
            client_module.time.sleep = original_sleep
            client.close()

    def test_retry_on_429(self, httpx_mock) -> None:
        """Client retries on 429 (rate limit) and succeeds on next attempt."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            status_code=429,
            text="Rate limit exceeded",
        )
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            json=VERIFY_TOKEN_RESPONSE,
            status_code=200,
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )

        import keboola_agent_cli.client as client_module

        original_sleep = client_module.time.sleep
        client_module.time.sleep = lambda x: None
        try:
            result = client.verify_token()
            assert result.project_name == "Test Project"
        finally:
            client_module.time.sleep = original_sleep
            client.close()

    def test_no_retry_on_400(self, httpx_mock) -> None:
        """Client does NOT retry on 400 (client error)."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            status_code=400,
            json={"error": "Bad request"},
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            client.verify_token()

        assert exc_info.value.status_code == 400
        assert exc_info.value.retryable is False
        client.close()


class TestTimeoutHandling:
    """Tests for timeout handling."""

    def test_timeout_raises_api_error(self, httpx_mock) -> None:
        """Timeout exceptions are wrapped in KeboolaApiError with TIMEOUT code."""
        httpx_mock.add_exception(
            httpx.ReadTimeout("Read timed out"),
            url="https://connection.keboola.com/v2/storage/tokens/verify",
        )
        httpx_mock.add_exception(
            httpx.ReadTimeout("Read timed out"),
            url="https://connection.keboola.com/v2/storage/tokens/verify",
        )
        httpx_mock.add_exception(
            httpx.ReadTimeout("Read timed out"),
            url="https://connection.keboola.com/v2/storage/tokens/verify",
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )

        import keboola_agent_cli.client as client_module

        original_sleep = client_module.time.sleep
        client_module.time.sleep = lambda x: None
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client.verify_token()
            assert exc_info.value.error_code == "TIMEOUT"
            assert exc_info.value.retryable is True
        finally:
            client_module.time.sleep = original_sleep
            client.close()

    def test_connect_error_raises_api_error(self, httpx_mock) -> None:
        """Connection errors are wrapped in KeboolaApiError with CONNECTION_ERROR code."""
        httpx_mock.add_exception(
            httpx.ConnectError("Connection refused"),
            url="https://connection.keboola.com/v2/storage/tokens/verify",
        )
        httpx_mock.add_exception(
            httpx.ConnectError("Connection refused"),
            url="https://connection.keboola.com/v2/storage/tokens/verify",
        )
        httpx_mock.add_exception(
            httpx.ConnectError("Connection refused"),
            url="https://connection.keboola.com/v2/storage/tokens/verify",
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )

        import keboola_agent_cli.client as client_module

        original_sleep = client_module.time.sleep
        client_module.time.sleep = lambda x: None
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client.verify_token()
            assert exc_info.value.error_code == "CONNECTION_ERROR"
            assert exc_info.value.retryable is True
        finally:
            client_module.time.sleep = original_sleep
            client.close()


class TestTokenMaskingInErrors:
    """Tests that token is never fully exposed in error messages."""

    def test_401_error_masks_token(self, httpx_mock) -> None:
        """Token is masked in 401 error messages."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            json={"error": "Invalid token"},
            status_code=401,
        )

        full_token = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"
        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=full_token,
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            client.verify_token()

        # Full token must NOT appear in the error message
        assert full_token not in exc_info.value.message
        # Masked form should appear
        assert "901-...pt0k" in exc_info.value.message
        client.close()

    def test_timeout_error_masks_token(self, httpx_mock) -> None:
        """Token is masked in timeout error messages."""
        for _ in range(MAX_RETRIES):
            httpx_mock.add_exception(
                httpx.ReadTimeout("Read timed out"),
                url="https://connection.keboola.com/v2/storage/tokens/verify",
            )

        full_token = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"
        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=full_token,
        )

        import keboola_agent_cli.client as client_module

        original_sleep = client_module.time.sleep
        client_module.time.sleep = lambda x: None
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client.verify_token()
            assert full_token not in exc_info.value.message
            assert "901-...pt0k" in exc_info.value.message
        finally:
            client_module.time.sleep = original_sleep
            client.close()


class TestClientHeaders:
    """Tests that the client sends correct headers."""

    def test_user_agent_header(self, httpx_mock) -> None:
        """Client sends User-Agent header with version."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            json=VERIFY_TOKEN_RESPONSE,
            status_code=200,
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )
        client.verify_token()

        request = httpx_mock.get_request()
        assert "keboola-agent-cli/" in request.headers["user-agent"]
        client.close()

    def test_storage_api_token_header(self, httpx_mock) -> None:
        """Client sends X-StorageApi-Token header."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            json=VERIFY_TOKEN_RESPONSE,
            status_code=200,
        )

        token = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"
        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=token,
        )
        client.verify_token()

        request = httpx_mock.get_request()
        assert request.headers["x-storageapi-token"] == token
        client.close()


class TestContextManager:
    """Tests for context manager support."""

    def test_context_manager(self, httpx_mock) -> None:
        """Client can be used as a context manager."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            json=VERIFY_TOKEN_RESPONSE,
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.verify_token()
            assert result.project_name == "Test Project"


class TestListComponents:
    """Tests for list_components()."""

    def test_list_components_success(self, httpx_mock) -> None:
        """list_components() returns component list from API."""
        components = [
            {"id": "keboola.ex-db-snowflake", "type": "extractor", "name": "Snowflake"},
        ]
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components?include=configuration",
            json=components,
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.list_components()
            assert len(result) == 1
            assert result[0]["id"] == "keboola.ex-db-snowflake"

    def test_list_components_with_type_filter(self, httpx_mock) -> None:
        """list_components(component_type) sends componentType query param."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components?include=configuration&componentType=extractor",
            json=[],
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.list_components(component_type="extractor")
            assert result == []


class TestGetConfigDetail:
    """Tests for get_config_detail()."""

    def test_get_config_detail_success(self, httpx_mock) -> None:
        """get_config_detail() returns config detail from API."""
        config_data = {"id": "42", "name": "My Config", "configuration": {}}
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components/keboola.ex-db-snowflake/configs/42",
            json=config_data,
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.get_config_detail("keboola.ex-db-snowflake", "42")
            assert result["id"] == "42"
            assert result["name"] == "My Config"


class TestMalformedJsonResponse:
    """Tests for handling malformed JSON responses from the API."""

    def test_malformed_json_in_error_response(self, httpx_mock) -> None:
        """Client handles non-JSON error response body gracefully."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            text="<html>502 Bad Gateway</html>",
            status_code=502,
        )
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            text="<html>502 Bad Gateway</html>",
            status_code=502,
        )
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            text="<html>502 Bad Gateway</html>",
            status_code=502,
        )

        import keboola_agent_cli.client as client_module

        original_sleep = client_module.time.sleep
        client_module.time.sleep = lambda x: None
        try:
            with KeboolaClient(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ) as client:
                with pytest.raises(KeboolaApiError) as exc_info:
                    client.verify_token()
                assert exc_info.value.retryable is True
                # Error message should contain the raw text body
                assert "502" in exc_info.value.message
        finally:
            client_module.time.sleep = original_sleep

    def test_malformed_json_in_success_response(self, httpx_mock) -> None:
        """Client raises error when success response has non-parseable JSON."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            text="not json at all",
            status_code=200,
            headers={"content-type": "text/plain"},
        )

        with (
            KeboolaClient(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ) as client,
            pytest.raises((ValueError, KeyError)),
        ):
            # verify_token calls response.json() which will fail
            client.verify_token()

    def test_empty_json_error_body(self, httpx_mock) -> None:
        """Client handles empty JSON object in error response."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            json={},
            status_code=401,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            with pytest.raises(KeboolaApiError) as exc_info:
                client.verify_token()
            assert exc_info.value.error_code == "INVALID_TOKEN"
            assert exc_info.value.status_code == 401


class TestEmptyResponse:
    """Tests for handling empty responses."""

    def test_empty_body_error_response(self, httpx_mock) -> None:
        """Client handles completely empty error response body."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            text="",
            status_code=500,
        )
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            text="",
            status_code=500,
        )
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            text="",
            status_code=500,
        )

        import keboola_agent_cli.client as client_module

        original_sleep = client_module.time.sleep
        client_module.time.sleep = lambda x: None
        try:
            with KeboolaClient(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ) as client:
                with pytest.raises(KeboolaApiError) as exc_info:
                    client.verify_token()
                assert exc_info.value.retryable is True
                assert exc_info.value.status_code == 500
        finally:
            client_module.time.sleep = original_sleep

    def test_empty_components_list(self, httpx_mock) -> None:
        """list_components returns empty list when API returns empty array."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components?include=configuration",
            json=[],
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.list_components()
            assert result == []

    def test_verify_token_minimal_response(self, httpx_mock) -> None:
        """verify_token handles response with minimal/missing fields."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            json={"id": "1"},
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.verify_token()
            assert result.token_id == "1"
            assert result.project_name == ""
            assert result.project_id == 0


class TestLargeResponse:
    """Tests for handling large API responses."""

    def test_large_components_list(self, httpx_mock) -> None:
        """Client handles response with many components."""
        # Generate 200 components with 10 configs each
        components = []
        for i in range(200):
            configs = []
            for j in range(10):
                configs.append(
                    {
                        "id": str(i * 10 + j),
                        "name": f"Config {j} of Component {i}",
                        "description": f"Description for config {j}",
                    }
                )
            components.append(
                {
                    "id": f"keboola.component-{i}",
                    "name": f"Component {i}",
                    "type": "extractor",
                    "configurations": configs,
                }
            )

        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components?include=configuration",
            json=components,
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.list_components()
            assert len(result) == 200
            assert len(result[0]["configurations"]) == 10
            assert result[199]["id"] == "keboola.component-199"

    def test_large_config_detail(self, httpx_mock) -> None:
        """Client handles config detail with large configuration payload."""
        # Simulate a large configuration with nested parameters
        large_config = {
            "id": "42",
            "name": "Large Config",
            "description": "A config with large parameters",
            "componentId": "keboola.ex-db-snowflake",
            "configuration": {
                "parameters": {f"param_{i}": f"value_{i}" for i in range(500)},
                "storage": {
                    "input": {
                        "tables": [
                            {"source": f"in.c-data.table_{i}", "destination": f"table_{i}.csv"}
                            for i in range(100)
                        ]
                    }
                },
            },
            "rows": [{"id": str(i), "name": f"Row {i}"} for i in range(50)],
        }

        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components/keboola.ex-db-snowflake/configs/42",
            json=large_config,
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.get_config_detail("keboola.ex-db-snowflake", "42")
            assert result["id"] == "42"
            assert len(result["configuration"]["parameters"]) == 500
            assert len(result["rows"]) == 50


class TestStackUrlNormalization:
    """Tests for stack URL handling edge cases."""

    def test_trailing_slash_removed(self, httpx_mock) -> None:
        """Client strips trailing slash from stack URL."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            json=VERIFY_TOKEN_RESPONSE,
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com/",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.verify_token()
            assert result.project_name == "Test Project"

    def test_404_returns_not_found_error(self, httpx_mock) -> None:
        """Client returns NOT_FOUND error code for 404 responses."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components/nonexistent/configs/999",
            json={"error": "Configuration not found"},
            status_code=404,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            with pytest.raises(KeboolaApiError) as exc_info:
                client.get_config_detail("nonexistent", "999")
            assert exc_info.value.error_code == "NOT_FOUND"
            assert exc_info.value.status_code == 404
            assert exc_info.value.retryable is False
