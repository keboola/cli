"""Tests for KeboolaClient - verify_token, retries, timeouts, error handling."""

from unittest.mock import patch
from urllib.parse import quote

import httpx
import pytest

from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.constants import MAX_RETRIES
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
        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = lambda x: None
        try:
            result = client.verify_token()
            assert result.project_name == "Test Project"
        finally:
            http_base_module.time.sleep = original_sleep
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

        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = lambda x: None
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client.verify_token()
            assert exc_info.value.retryable is True
        finally:
            http_base_module.time.sleep = original_sleep
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

        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = lambda x: None
        try:
            result = client.verify_token()
            assert result.project_name == "Test Project"
        finally:
            http_base_module.time.sleep = original_sleep
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

        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = lambda x: None
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client.verify_token()
            assert exc_info.value.error_code == "TIMEOUT"
            assert exc_info.value.retryable is True
        finally:
            http_base_module.time.sleep = original_sleep
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

        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = lambda x: None
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client.verify_token()
            assert exc_info.value.error_code == "CONNECTION_ERROR"
            assert exc_info.value.retryable is True
        finally:
            http_base_module.time.sleep = original_sleep
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

        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = lambda x: None
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client.verify_token()
            assert full_token not in exc_info.value.message
            assert "901-...pt0k" in exc_info.value.message
        finally:
            http_base_module.time.sleep = original_sleep
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

    def test_list_components_with_branch_id(self, httpx_mock) -> None:
        """list_components(branch_id) uses branch prefix in URL."""
        components = [
            {"id": "keboola.ex-db-snowflake", "type": "extractor", "name": "Snowflake"},
        ]
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/123/components?include=configuration",
            json=components,
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.list_components(branch_id=123)
            assert len(result) == 1
            assert result[0]["id"] == "keboola.ex-db-snowflake"


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

    def test_get_config_detail_with_branch_id(self, httpx_mock) -> None:
        """get_config_detail(branch_id) uses branch prefix in URL."""
        config_data = {"id": "42", "name": "My Config", "configuration": {}}
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/123/components/keboola.ex-db-snowflake/configs/42",
            json=config_data,
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.get_config_detail("keboola.ex-db-snowflake", "42", branch_id=123)
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

        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = lambda x: None
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
            http_base_module.time.sleep = original_sleep

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

        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = lambda x: None
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
            http_base_module.time.sleep = original_sleep

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
            assert result.project_id is None


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


class TestQueueBaseUrl:
    """Tests for Queue API URL derivation from Storage API URL."""

    def test_queue_url_from_aws_stack(self) -> None:
        """Queue URL replaces 'connection.' with 'queue.' for AWS stack."""
        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )
        assert client._queue_base_url == "https://queue.keboola.com"
        client.close()

    def test_queue_url_from_azure_stack(self) -> None:
        """Queue URL replaces 'connection.' for Azure stack."""
        client = KeboolaClient(
            stack_url="https://connection.north-europe.azure.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )
        assert client._queue_base_url == "https://queue.north-europe.azure.keboola.com"
        client.close()

    def test_queue_url_from_gcp_stack(self) -> None:
        """Queue URL replaces 'connection.' for GCP stack."""
        client = KeboolaClient(
            stack_url="https://connection.europe-west3.gcp.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )
        assert client._queue_base_url == "https://queue.europe-west3.gcp.keboola.com"
        client.close()

    def test_queue_url_with_trailing_slash(self) -> None:
        """Queue URL derivation works when stack URL has trailing slash."""
        client = KeboolaClient(
            stack_url="https://connection.keboola.com/",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )
        assert client._queue_base_url == "https://queue.keboola.com"
        client.close()


class TestListJobs:
    """Tests for list_jobs() - Queue API interaction."""

    def test_list_jobs_success(self, httpx_mock) -> None:
        """list_jobs() returns job list from Queue API."""
        jobs = [
            {
                "id": 1001,
                "status": "success",
                "component": "keboola.ex-db-snowflake",
                "configId": "123",
                "createdTime": "2026-02-26T10:00:00Z",
                "durationSeconds": 45,
            },
        ]
        httpx_mock.add_response(
            url="https://queue.keboola.com/search/jobs?limit=50&offset=0",
            json=jobs,
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.list_jobs()
            assert len(result) == 1
            assert result[0]["id"] == 1001
            assert result[0]["status"] == "success"

    def test_list_jobs_with_filters(self, httpx_mock) -> None:
        """list_jobs() passes component, config, and status filters as query params."""
        httpx_mock.add_response(
            url="https://queue.keboola.com/search/jobs?limit=10&offset=0&component=keboola.ex-db-snowflake&config=42&status=error",
            json=[],
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.list_jobs(
                component_id="keboola.ex-db-snowflake",
                config_id="42",
                status="error",
                limit=10,
            )
            assert result == []

    def test_list_jobs_401_error(self, httpx_mock) -> None:
        """list_jobs() raises KeboolaApiError with INVALID_TOKEN on 401 from Queue API."""
        httpx_mock.add_response(
            url="https://queue.keboola.com/search/jobs?limit=50&offset=0",
            json={"error": "Invalid token"},
            status_code=401,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            with pytest.raises(KeboolaApiError) as exc_info:
                client.list_jobs()
            assert exc_info.value.error_code == "INVALID_TOKEN"

    def test_list_jobs_retry_on_503(self, httpx_mock) -> None:
        """list_jobs() retries on 503 from Queue API and succeeds."""
        httpx_mock.add_response(
            url="https://queue.keboola.com/search/jobs?limit=50&offset=0",
            status_code=503,
            text="Service Unavailable",
        )
        httpx_mock.add_response(
            url="https://queue.keboola.com/search/jobs?limit=50&offset=0",
            json=[{"id": 1, "status": "success"}],
            status_code=200,
        )

        import keboola_agent_cli.http_base as http_base_module

        original_sleep = http_base_module.time.sleep
        http_base_module.time.sleep = lambda x: None
        try:
            with KeboolaClient(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ) as client:
                result = client.list_jobs()
                assert len(result) == 1
        finally:
            http_base_module.time.sleep = original_sleep

    def test_list_jobs_empty_result(self, httpx_mock) -> None:
        """list_jobs() returns empty list when no jobs match."""
        httpx_mock.add_response(
            url="https://queue.keboola.com/search/jobs?limit=50&offset=0",
            json=[],
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.list_jobs()
            assert result == []


class TestCloseWithQueueClient:
    """Tests that close() properly closes both Storage and Queue clients."""

    def test_close_without_queue_client(self) -> None:
        """close() works when queue client was never created."""
        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )
        assert client._queue_client is None
        client.close()  # Should not raise

    def test_close_with_queue_client(self, httpx_mock) -> None:
        """close() closes both storage and queue clients."""
        httpx_mock.add_response(
            url="https://queue.keboola.com/search/jobs?limit=50&offset=0",
            json=[],
            status_code=200,
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )
        # Trigger queue client creation
        client.list_jobs()
        assert client._queue_client is not None
        client.close()  # Should not raise


class TestGetJobDetail:
    """Tests for get_job_detail() - Queue API interaction."""

    def test_get_job_detail_success(self, httpx_mock) -> None:
        """get_job_detail() returns job detail from Queue API."""
        job_data = {
            "id": "1001",
            "status": "success",
            "component": "keboola.ex-db-snowflake",
            "config": "123",
            "createdTime": "2026-02-26T10:00:00Z",
            "durationSeconds": 45,
            "result": {"message": "All good"},
        }
        httpx_mock.add_response(
            url="https://queue.keboola.com/jobs/1001",
            json=job_data,
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.get_job_detail("1001")
            assert result["id"] == "1001"
            assert result["status"] == "success"
            assert result["result"]["message"] == "All good"

    def test_get_job_detail_not_found(self, httpx_mock) -> None:
        """get_job_detail() raises NOT_FOUND for nonexistent job."""
        httpx_mock.add_response(
            url="https://queue.keboola.com/jobs/999999",
            json={"error": "Job not found"},
            status_code=404,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            with pytest.raises(KeboolaApiError) as exc_info:
                client.get_job_detail("999999")
            assert exc_info.value.error_code == "NOT_FOUND"
            assert exc_info.value.status_code == 404


SAMPLE_DEV_BRANCHES = [
    {
        "id": 123,
        "name": "main",
        "isDefault": True,
        "created": "2025-01-01T00:00:00Z",
        "description": "",
    },
    {
        "id": 456,
        "name": "feature-x",
        "isDefault": False,
        "created": "2025-06-15T10:30:00Z",
        "description": "Feature",
    },
]


class TestListDevBranches:
    """Tests for list_dev_branches() - Storage API branch listing."""

    def test_list_dev_branches_success(self, httpx_mock) -> None:
        """list_dev_branches() returns branch list from Storage API."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/dev-branches",
            json=SAMPLE_DEV_BRANCHES,
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.list_dev_branches()
            assert len(result) == 2
            assert result[0]["id"] == 123
            assert result[0]["name"] == "main"
            assert result[0]["isDefault"] is True
            assert result[1]["id"] == 456
            assert result[1]["name"] == "feature-x"

    def test_list_dev_branches_empty(self, httpx_mock) -> None:
        """list_dev_branches() returns empty list when no branches exist."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/dev-branches",
            json=[],
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.list_dev_branches()
            assert result == []

    def test_list_dev_branches_401_error(self, httpx_mock) -> None:
        """list_dev_branches() raises KeboolaApiError on 401."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/dev-branches",
            json={"error": "Invalid access token"},
            status_code=401,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            with pytest.raises(KeboolaApiError) as exc_info:
                client.list_dev_branches()
            assert exc_info.value.error_code == "INVALID_TOKEN"


SAMPLE_BUCKETS = [
    {"id": "in.c-data", "name": "Data", "stage": "in"},
    {"id": "out.c-results", "name": "Results", "stage": "out"},
]

SAMPLE_BUCKETS_WITH_SHARING = [
    {
        "id": "in.c-shared",
        "name": "Shared",
        "stage": "in",
        "sharing": "organization-project",
        "linkedBy": [{"id": "in.c-linked", "project": {"id": 7012, "name": "Target"}}],
    },
]


class TestListBuckets:
    """Tests for list_buckets() - Storage API bucket listing."""

    def test_list_buckets_success(self, httpx_mock) -> None:
        """list_buckets() returns bucket list from Storage API without include param."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/buckets",
            json=SAMPLE_BUCKETS,
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.list_buckets()
            assert len(result) == 2
            assert result[0]["id"] == "in.c-data"

    def test_list_buckets_with_include(self, httpx_mock) -> None:
        """list_buckets(include=) passes include query param and returns sharing info."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/buckets?include=linkedBuckets",
            json=SAMPLE_BUCKETS_WITH_SHARING,
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.list_buckets(include="linkedBuckets")
            assert len(result) == 1
            assert result[0]["sharing"] == "organization-project"
            assert result[0]["linkedBy"][0]["project"]["id"] == 7012

    def test_list_buckets_empty(self, httpx_mock) -> None:
        """list_buckets() returns empty list when no buckets exist."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/buckets",
            json=[],
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.list_buckets()
            assert result == []


class TestApiErrorMessageTruncation:
    """Tests for S4: API error message truncation to 500 characters."""

    def test_api_error_message_truncation(self, httpx_mock) -> None:
        """Long server response is truncated to 500 characters in error message."""
        long_message = "A" * 1000
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            json={"error": long_message},
            status_code=400,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            with pytest.raises(KeboolaApiError) as exc_info:
                client.verify_token()

            # The full 1000 char message should NOT appear
            assert long_message not in exc_info.value.message
            # The truncated message (500 chars + "...") should be present
            assert "A" * 500 + "..." in exc_info.value.message

    def test_short_error_message_not_truncated(self, httpx_mock) -> None:
        """Short server response is not truncated."""
        short_message = "Bad request"
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            json={"error": short_message},
            status_code=400,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            with pytest.raises(KeboolaApiError) as exc_info:
                client.verify_token()

            assert short_message in exc_info.value.message
            # Should not have the truncation indicator
            assert "..." not in exc_info.value.message or short_message in exc_info.value.message

    def test_exactly_500_chars_not_truncated(self, httpx_mock) -> None:
        """Error message of exactly 500 characters is not truncated."""
        exact_message = "B" * 500
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            json={"error": exact_message},
            status_code=400,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            with pytest.raises(KeboolaApiError) as exc_info:
                client.verify_token()

            # Exactly 500 chars should not be truncated
            assert exact_message in exc_info.value.message

    def test_rich_markup_in_error_truncated(self, httpx_mock) -> None:
        """Rich markup brackets in error messages are contained by truncation."""
        malicious_msg = "[bold red]" + "X" * 600 + "[/bold red]"
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            json={"error": malicious_msg},
            status_code=400,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            with pytest.raises(KeboolaApiError) as exc_info:
                client.verify_token()

            # Full malicious markup should not appear
            assert malicious_msg not in exc_info.value.message
            # Should be truncated
            assert "..." in exc_info.value.message


class TestUrlPathEncoding:
    """Tests for S5: URL-encode path parameters to prevent path traversal."""

    def test_url_path_encoding_component_id(self, httpx_mock) -> None:
        """Special characters in component_id are URL-encoded."""
        encoded_component = quote("keboola.ex-db/../admin", safe="")
        encoded_config = quote("42", safe="")

        httpx_mock.add_response(
            url=f"https://connection.keboola.com/v2/storage/components/{encoded_component}/configs/{encoded_config}",
            json={"id": "42", "name": "Config"},
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.get_config_detail("keboola.ex-db/../admin", "42")
            assert result["id"] == "42"

    def test_url_path_encoding_config_id(self, httpx_mock) -> None:
        """Special characters in config_id are URL-encoded."""
        encoded_component = quote("keboola.ex-db-snowflake", safe="")
        encoded_config = quote("42/../secret", safe="")

        httpx_mock.add_response(
            url=f"https://connection.keboola.com/v2/storage/components/{encoded_component}/configs/{encoded_config}",
            json={"id": "42", "name": "Config"},
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.get_config_detail("keboola.ex-db-snowflake", "42/../secret")
            assert result["id"] == "42"

    def test_url_path_encoding_job_id(self, httpx_mock) -> None:
        """Special characters in job_id are URL-encoded."""
        encoded_job = quote("1001/../admin", safe="")

        httpx_mock.add_response(
            url=f"https://queue.keboola.com/jobs/{encoded_job}",
            json={"id": "1001", "status": "success"},
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.get_job_detail("1001/../admin")
            assert result["id"] == "1001"

    def test_normal_ids_not_affected(self, httpx_mock) -> None:
        """Normal IDs without special chars work correctly with encoding."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components/keboola.ex-db-snowflake/configs/42",
            json={"id": "42", "name": "Config"},
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.get_config_detail("keboola.ex-db-snowflake", "42")
            assert result["id"] == "42"


class TestRetryAfterHeader:
    """Tests for Retry-After header on 429 responses."""

    def test_retry_after_header_respected(self, httpx_mock) -> None:
        """429 response with Retry-After header uses specified delay."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            status_code=429,
            text="Rate limit exceeded",
            headers={"Retry-After": "5"},
        )
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            json=VERIFY_TOKEN_RESPONSE,
            status_code=200,
        )

        import keboola_agent_cli.http_base as http_base_module

        sleep_calls: list[float] = []
        original_sleep = http_base_module.time.sleep

        def capture_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        http_base_module.time.sleep = capture_sleep
        try:
            with KeboolaClient(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ) as client:
                result = client.verify_token()
                assert result.project_name == "Test Project"
                # Should have used 5.0 from Retry-After header, not default backoff (1.0)
                assert len(sleep_calls) == 1
                assert sleep_calls[0] == 5.0
        finally:
            http_base_module.time.sleep = original_sleep

    def test_retry_after_capped_at_60(self, httpx_mock) -> None:
        """Retry-After values > 60s are capped at 60s."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            status_code=429,
            text="Rate limit exceeded",
            headers={"Retry-After": "120"},
        )
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            json=VERIFY_TOKEN_RESPONSE,
            status_code=200,
        )

        import keboola_agent_cli.http_base as http_base_module

        sleep_calls: list[float] = []
        original_sleep = http_base_module.time.sleep

        def capture_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        http_base_module.time.sleep = capture_sleep
        try:
            with KeboolaClient(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ) as client:
                result = client.verify_token()
                assert result.project_name == "Test Project"
                # Should be capped at 60.0
                assert len(sleep_calls) == 1
                assert sleep_calls[0] == 60.0
        finally:
            http_base_module.time.sleep = original_sleep

    def test_retry_after_invalid_falls_back_to_backoff(self, httpx_mock) -> None:
        """Invalid Retry-After value falls back to exponential backoff."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            status_code=429,
            text="Rate limit exceeded",
            headers={"Retry-After": "not-a-number"},
        )
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            json=VERIFY_TOKEN_RESPONSE,
            status_code=200,
        )

        import keboola_agent_cli.http_base as http_base_module

        sleep_calls: list[float] = []
        original_sleep = http_base_module.time.sleep

        def capture_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        http_base_module.time.sleep = capture_sleep
        try:
            with KeboolaClient(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ) as client:
                result = client.verify_token()
                assert result.project_name == "Test Project"
                # Should fall back to backoff: BACKOFF_BASE * 2^0 = 1.0
                assert len(sleep_calls) == 1
                assert sleep_calls[0] == 1.0
        finally:
            http_base_module.time.sleep = original_sleep

    def test_429_without_retry_after_uses_backoff(self, httpx_mock) -> None:
        """429 response without Retry-After header uses default exponential backoff."""
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

        import keboola_agent_cli.http_base as http_base_module

        sleep_calls: list[float] = []
        original_sleep = http_base_module.time.sleep

        def capture_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        http_base_module.time.sleep = capture_sleep
        try:
            with KeboolaClient(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ) as client:
                result = client.verify_token()
                assert result.project_name == "Test Project"
                # Should use default backoff: BACKOFF_BASE * 2^0 = 1.0
                assert len(sleep_calls) == 1
                assert sleep_calls[0] == 1.0
        finally:
            http_base_module.time.sleep = original_sleep


class TestQueueUrlWarning:
    """Tests for queue URL derivation warning when hostname doesn't change."""

    def test_non_standard_url_warns(self) -> None:
        """Non-standard URL without 'connection.' in hostname logs warning."""

        with patch("keboola_agent_cli.http_base.logger") as mock_logger:
            client = KeboolaClient(
                stack_url="https://custom.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            )
            # Access _queue_base_url to trigger derivation
            _ = client._queue_base_url
            mock_logger.warning.assert_called_once()
            assert "did not change hostname" in mock_logger.warning.call_args[0][0]
            client.close()

    def test_standard_url_no_warning(self) -> None:
        """Standard URL with 'connection.' in hostname does not log warning."""
        with patch("keboola_agent_cli.http_base.logger") as mock_logger:
            client = KeboolaClient(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            )
            _ = client._queue_base_url
            mock_logger.warning.assert_not_called()
            client.close()


class TestCreateDevBranch:
    """Tests for create_dev_branch() - async Storage API branch creation."""

    def test_create_dev_branch_success(self, httpx_mock) -> None:
        """create_dev_branch() polls job and returns branch data from results."""
        # POST returns an async job
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/dev-branches",
            json={
                "id": 999999,
                "status": "success",
                "operationName": "devBranchCreate",
                "results": {"id": 789, "name": "my-feature", "description": "", "isDefault": False},
            },
            status_code=201,
            method="POST",
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.create_dev_branch("my-feature")
            assert result["id"] == 789
            assert result["name"] == "my-feature"

    def test_create_dev_branch_with_description(self, httpx_mock) -> None:
        """create_dev_branch() sends description in the request body."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/dev-branches",
            json={
                "id": 999998,
                "status": "success",
                "operationName": "devBranchCreate",
                "results": {"id": 790, "name": "my-feature", "description": "A feature branch"},
            },
            status_code=201,
            method="POST",
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.create_dev_branch("my-feature", description="A feature branch")
            assert result["id"] == 790
            assert result["description"] == "A feature branch"

            # Verify the POST request body contained the description
            request = httpx_mock.get_requests()[0]
            import json

            body = json.loads(request.content)
            assert body["name"] == "my-feature"
            assert body["description"] == "A feature branch"

    def test_create_dev_branch_polls_waiting_job(self, httpx_mock) -> None:
        """create_dev_branch() polls a waiting job until success."""
        # POST returns a waiting job
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/dev-branches",
            json={"id": 111, "status": "waiting"},
            status_code=201,
            method="POST",
        )
        # First poll: still processing
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/jobs/111",
            json={"id": 111, "status": "processing"},
            method="GET",
        )
        # Second poll: success
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/jobs/111",
            json={
                "id": 111,
                "status": "success",
                "results": {"id": 555, "name": "polled-branch"},
            },
            method="GET",
        )

        from unittest.mock import patch

        with (
            patch("keboola_agent_cli.client.time.sleep"),
            KeboolaClient(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ) as client,
        ):
            result = client.create_dev_branch("polled-branch")
            assert result["id"] == 555


class TestDeleteDevBranch:
    """Tests for delete_dev_branch() - async Storage API branch deletion."""

    def test_delete_dev_branch_success(self, httpx_mock) -> None:
        """delete_dev_branch() polls job until success."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/dev-branches/789",
            json={"id": 222, "status": "success", "operationName": "devBranchDelete"},
            method="DELETE",
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            # Should not raise any exception
            client.delete_dev_branch(789)


class TestCreateSandboxConfigBranch:
    """Tests for create_sandbox_config() branch_id routing."""

    def test_create_sandbox_config_no_branch(self, httpx_mock) -> None:
        """Without branch_id, uses /v2/storage/components/... endpoint."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components/keboola.sandboxes/configs",
            method="POST",
            json={"id": "cfg-1", "name": "test"},
            status_code=201,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.create_sandbox_config(name="test")
            assert result["id"] == "cfg-1"

    def test_create_sandbox_config_with_branch(self, httpx_mock) -> None:
        """With branch_id, uses /v2/storage/branch/{id}/components/... endpoint."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/200/components/keboola.sandboxes/configs",
            method="POST",
            json={"id": "cfg-2", "name": "branch-ws"},
            status_code=201,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.create_sandbox_config(name="branch-ws", branch_id=200)
            assert result["id"] == "cfg-2"


class TestDeleteConfigBranch:
    """Tests for delete_config() branch_id routing."""

    def test_delete_config_no_branch(self, httpx_mock) -> None:
        """Without branch_id, uses /v2/storage/components/... endpoint."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components/keboola.sandboxes/configs/cfg-1",
            method="DELETE",
            status_code=204,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            client.delete_config("keboola.sandboxes", "cfg-1")

    def test_delete_config_with_branch(self, httpx_mock) -> None:
        """With branch_id, uses /v2/storage/branch/{id}/components/... endpoint."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/200/components/keboola.sandboxes/configs/cfg-1",
            method="DELETE",
            status_code=204,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            client.delete_config("keboola.sandboxes", "cfg-1", branch_id=200)


class TestLoadWorkspaceTablesPreserve:
    """Tests for load_workspace_tables() preserve parameter."""

    def test_load_workspace_tables_preserve_false(self, httpx_mock) -> None:
        """load_workspace_tables sends preserve=False in the request body by default."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/workspaces/42/load",
            method="POST",
            json={"id": 900, "status": "success"},
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.load_workspace_tables(
                workspace_id=42,
                tables=[{"source": "in.c-main.orders", "destination": "orders"}],
            )
            assert result["status"] == "success"

            import json

            request = httpx_mock.get_requests()[0]
            body = json.loads(request.content)
            assert body["preserve"] is False
            assert len(body["input"]) == 1
            assert body["input"][0]["source"] == "in.c-main.orders"

    def test_load_workspace_tables_preserve_true(self, httpx_mock) -> None:
        """load_workspace_tables sends preserve=True when requested."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/workspaces/42/load",
            method="POST",
            json={"id": 901, "status": "success"},
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.load_workspace_tables(
                workspace_id=42,
                tables=[{"source": "in.c-main.orders", "destination": "orders"}],
                preserve=True,
            )
            assert result["status"] == "success"

            import json

            request = httpx_mock.get_requests()[0]
            body = json.loads(request.content)
            assert body["preserve"] is True

    def test_load_workspace_tables_preserve_with_branch(self, httpx_mock) -> None:
        """load_workspace_tables sends preserve in body when branch_id is set."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/200/workspaces/42/load",
            method="POST",
            json={"id": 902, "status": "success"},
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.load_workspace_tables(
                workspace_id=42,
                tables=[{"source": "in.c-main.orders", "destination": "orders"}],
                branch_id=200,
                preserve=True,
            )
            assert result["status"] == "success"

            import json

            request = httpx_mock.get_requests()[0]
            body = json.loads(request.content)
            assert body["preserve"] is True


"""Client tests for async table upload: prepare_file_upload, _upload_to_cloud,
import_table_async, upload_table -- appended to test_client.py via script."""

_TOKEN = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"
_BASE = "https://connection.keboola.com"


class TestPrepareFileUpload:
    """Tests for KeboolaClient.prepare_file_upload()."""

    def test_sends_only_name_and_size(self, httpx_mock) -> None:
        """prepare_file_upload sends name and sizeBytes but NOT isPermanent/isPublic."""
        from urllib.parse import parse_qs

        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/files/prepare",
            method="POST",
            json={"id": 999, "url": "https://s3.example.com/", "uploadParams": {}},
            status_code=200,
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            result = client.prepare_file_upload(name="data.csv", size_bytes=1234)

        assert result["id"] == 999
        request = httpx_mock.get_requests()[0]
        body = parse_qs(request.content.decode())
        assert body["name"] == ["data.csv"]
        assert body["sizeBytes"] == ["1234"]
        assert "isPermanent" not in body
        assert "isPublic" not in body

    def test_api_error_propagates(self, httpx_mock) -> None:
        """prepare_file_upload raises KeboolaApiError on API failure."""
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/files/prepare",
            method="POST",
            json={"error": "Unauthorized"},
            status_code=401,
        )
        with (
            KeboolaClient(stack_url=_BASE, token=_TOKEN) as client,
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client.prepare_file_upload(name="data.csv", size_bytes=100)
        assert exc_info.value.status_code == 401


class TestUploadToCloud:
    """Tests for KeboolaClient._upload_to_cloud()."""

    def test_success_204(self, httpx_mock, tmp_path) -> None:
        """_upload_to_cloud succeeds when cloud storage returns 204 (S3)."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_bytes(b"id,name\n1,Alice\n")
        httpx_mock.add_response(
            url="https://s3.amazonaws.com/kbc-test/",
            method="POST",
            status_code=204,
        )
        upload_info = {
            "url": "https://s3.amazonaws.com/kbc-test/",
            "uploadParams": {"key": "exp/data.csv", "AWSAccessKeyId": "AKID"},
        }
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            client._upload_to_cloud(upload_info, str(csv_file))

        assert any(r.url.host == "s3.amazonaws.com" for r in httpx_mock.get_requests())

    def test_success_gcp_bearer_token(self, httpx_mock, tmp_path) -> None:
        """_upload_to_cloud uses GCS JSON API PUT with bearer token when gcsUploadParams present."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_bytes(b"id\n1\n")
        gcs_url = "https://storage.googleapis.com/kbc-bucket/exp-15/2000/files/data.csv"
        httpx_mock.add_response(url=gcs_url, method="PUT", status_code=200)
        upload_info = {
            "url": "https://storage.googleapis.com/kbc-bucket/data.csv?response-content-disposition=attachment",
            "uploadParams": None,
            "gcsUploadParams": {
                "bucket": "kbc-bucket",
                "key": "exp-15/2000/files/data.csv",
                "access_token": "ya29.test-token",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        }
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            client._upload_to_cloud(upload_info, str(csv_file))

        put_req = next(r for r in httpx_mock.get_requests() if r.method == "PUT")
        assert "Bearer ya29.test-token" in put_req.headers.get("authorization", "")

    def test_success_200_gcs_signed_url(self, httpx_mock, tmp_path) -> None:
        """_upload_to_cloud uses PUT when uploadParams is empty (GCS/ABS signed URL)."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_bytes(b"id\n1\n")
        gcs_url = "https://storage.googleapis.com/kbc-bucket/path/file.csv?X-Goog-Signature=abc"
        httpx_mock.add_response(
            url=gcs_url,
            method="PUT",
            status_code=200,
        )
        upload_info = {"url": gcs_url, "uploadParams": {}}
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            client._upload_to_cloud(upload_info, str(csv_file))

        assert any(r.method == "PUT" for r in httpx_mock.get_requests())

    def test_success_200_gcs_post(self, httpx_mock, tmp_path) -> None:
        """_upload_to_cloud uses POST when uploadParams is non-empty (S3-style GCS POST)."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_bytes(b"id\n1\n")
        httpx_mock.add_response(
            url="https://storage.googleapis.com/kbc-test/",
            method="POST",
            status_code=200,
        )
        upload_info = {
            "url": "https://storage.googleapis.com/kbc-test/",
            "uploadParams": {"GoogleAccessId": "sa@project.iam.gserviceaccount.com"},
        }
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            client._upload_to_cloud(upload_info, str(csv_file))

    def test_non_success_raises(self, httpx_mock, tmp_path) -> None:
        """_upload_to_cloud raises KeboolaApiError when cloud storage PUT returns error."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_bytes(b"id\n1\n")
        gcs_url = "https://storage.googleapis.com/kbc-bucket/path/file.csv?X-Goog-Signature=abc"
        httpx_mock.add_response(
            url=gcs_url,
            method="PUT",
            status_code=403,
        )
        upload_info = {"url": gcs_url, "uploadParams": {}}
        with (
            KeboolaClient(stack_url=_BASE, token=_TOKEN) as client,
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client._upload_to_cloud(upload_info, str(csv_file))
        assert exc_info.value.error_code == "UPLOAD_FAILED"
        assert exc_info.value.status_code == 403


class TestImportTableAsync:
    """Tests for KeboolaClient.import_table_async()."""

    def test_polls_until_success(self, httpx_mock) -> None:
        """import_table_async POSTs to import-async and polls until job succeeds."""
        from urllib.parse import parse_qs

        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/tables/in.c-b.users/import-async",
            method="POST",
            json={"id": 42, "status": "waiting"},
            status_code=201,
        )
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/jobs/42",
            method="GET",
            json={"id": 42, "status": "success", "results": {"importedRowsCount": 5}},
            status_code=200,
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            job = client.import_table_async(table_id="in.c-b.users", file_id=999, incremental=True)

        assert job["status"] == "success"
        post_req = next(r for r in httpx_mock.get_requests() if r.method == "POST")
        body = parse_qs(post_req.content.decode())
        assert body["dataFileId"] == ["999"]
        assert body["incremental"] == ["1"]

    def test_job_failure_raises(self, httpx_mock) -> None:
        """import_table_async raises KeboolaApiError when import job fails."""
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/tables/in.c-b.users/import-async",
            method="POST",
            json={"id": 77, "status": "waiting"},
            status_code=201,
        )
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/jobs/77",
            method="GET",
            json={"id": 77, "status": "error", "error": {"message": "Import failed"}},
            status_code=200,
        )
        with (
            KeboolaClient(stack_url=_BASE, token=_TOKEN) as client,
            pytest.raises(KeboolaApiError, match="Import failed"),
        ):
            client.import_table_async(table_id="in.c-b.users", file_id=999)


class TestUploadTableClient:
    """Tests for KeboolaClient.upload_table() -- full 3-step async flow."""

    def test_full_flow(self, httpx_mock, tmp_path) -> None:
        """upload_table orchestrates prepare -> cloud upload -> import-async -> poll."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_bytes(b"id,name\n1,Alice\n2,Bob\n")

        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/files/prepare",
            method="POST",
            json={
                "id": 100,
                "url": "https://s3.amazonaws.com/kbc-test/",
                "uploadParams": {"key": "exp/data.csv"},
            },
            status_code=200,
        )
        httpx_mock.add_response(
            url="https://s3.amazonaws.com/kbc-test/",
            method="POST",
            status_code=204,
        )
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/tables/in.c-b.users/import-async",
            method="POST",
            json={"id": 55, "status": "waiting"},
            status_code=201,
        )
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/jobs/55",
            method="GET",
            json={
                "id": 55,
                "status": "success",
                "results": {"importedRowsCount": 2, "warnings": []},
            },
            status_code=200,
        )

        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            result = client.upload_table(table_id="in.c-b.users", file_path=str(csv_file))

        assert result.get("importedRowsCount") == 2

    def test_full_flow_gcp(self, httpx_mock, tmp_path) -> None:
        """upload_table orchestrates prepare -> GCP bearer PUT -> import-async -> poll."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_bytes(b"id,name\n1,Alice\n")
        gcs_upload_url = "https://storage.googleapis.com/kbc-bucket/exp-15/2000/files/data.csv"

        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/files/prepare",
            method="POST",
            json={
                "id": 200,
                "url": "https://storage.googleapis.com/kbc-bucket/data.csv?response-content-disposition=attachment",
                "uploadParams": None,
                "gcsUploadParams": {
                    "bucket": "kbc-bucket",
                    "key": "exp-15/2000/files/data.csv",
                    "access_token": "ya29.test-token",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            },
            status_code=200,
        )
        httpx_mock.add_response(
            url=gcs_upload_url,
            method="PUT",
            status_code=200,
        )
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/tables/in.c-b.users/import-async",
            method="POST",
            json={"id": 66, "status": "waiting"},
            status_code=201,
        )
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/jobs/66",
            method="GET",
            json={
                "id": 66,
                "status": "success",
                "results": {"importedRowsCount": 1, "warnings": []},
            },
            status_code=200,
        )

        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            result = client.upload_table(table_id="in.c-b.users", file_path=str(csv_file))

        assert result.get("importedRowsCount") == 1

    def test_cloud_upload_failure_raises(self, httpx_mock, tmp_path) -> None:
        """upload_table raises KeboolaApiError if cloud upload (signed URL PUT) returns error."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_bytes(b"id\n1\n")
        signed_url = "https://storage.googleapis.com/kbc/file.csv?X-Goog-Signature=abc"

        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/files/prepare",
            method="POST",
            json={"id": 101, "url": signed_url, "uploadParams": {}},
            status_code=200,
        )
        httpx_mock.add_response(
            url=signed_url,
            method="PUT",
            status_code=403,
        )

        with (
            KeboolaClient(stack_url=_BASE, token=_TOKEN) as client,
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client.upload_table(table_id="in.c-b.users", file_path=str(csv_file))
        assert exc_info.value.error_code == "UPLOAD_FAILED"
