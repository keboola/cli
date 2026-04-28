"""Tests for KeboolaClient - verify_token, retries, timeouts, error handling."""

import contextlib
import json
from unittest.mock import patch
from urllib.parse import parse_qs, quote

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

VERIFY_TOKEN_RESPONSE_WITH_FEATURES = {
    "id": "12345",
    "description": "tok",
    "owner": {
        "id": 1234,
        "name": "Test Project",
        "features": ["storage-branches", "queuev2", "agent-chat"],
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


class TestProjectFeatures:
    """Tests for get_project_features() / has_feature() lazy cache."""

    def test_get_project_features_caches_after_first_call(self, httpx_mock) -> None:
        """Second has_feature() call must not trigger a second verify_token HTTP."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            json=VERIFY_TOKEN_RESPONSE_WITH_FEATURES,
            status_code=200,
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )
        try:
            features = client.get_project_features()
            assert "storage-branches" in features
            # Second call hits the cache; if it issued HTTP, pytest_httpx
            # would fail "unexpected request" because we registered exactly
            # one response.
            assert client.has_feature("storage-branches") is True
            assert client.has_feature("agent-chat") is True
            assert client.has_feature("nonexistent-feature") is False
        finally:
            client.close()

    def test_verify_token_populates_feature_cache(self, httpx_mock) -> None:
        """An explicit verify_token() warms the cache for subsequent has_feature()."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            json=VERIFY_TOKEN_RESPONSE_WITH_FEATURES,
            status_code=200,
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )
        try:
            client.verify_token()
            # No further HTTP registered -- cache must answer.
            assert client.has_feature("queuev2") is True
        finally:
            client.close()

    def test_has_feature_false_when_owner_has_no_features(self, httpx_mock) -> None:
        """Empty / missing features list returns False for any flag."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tokens/verify",
            json={"id": "1", "description": "", "owner": {"id": 7, "name": "P"}},
            status_code=200,
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )
        try:
            assert client.has_feature("storage-branches") is False
            assert client.get_project_features() == frozenset()
        finally:
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


class TestGetConfigState:
    """Tests for get_config_state()."""

    def test_get_config_state_returns_state_dict(self, httpx_mock) -> None:
        """get_config_state() returns the state field from the config detail."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components/keboola.ex-db-snowflake/configs/42",
            json={
                "id": "42",
                "name": "cfg",
                "configuration": {},
                "state": {"cursor": "2026-04-23T10:00:00Z", "count": 42},
            },
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            state = client.get_config_state("keboola.ex-db-snowflake", "42")
            assert state == {"cursor": "2026-04-23T10:00:00Z", "count": 42}

    def test_get_config_state_returns_empty_dict_when_state_missing(self, httpx_mock) -> None:
        """When the API response has no state field, return {} (not None)."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components/keboola.ex-db-snowflake/configs/42",
            json={"id": "42", "name": "cfg", "configuration": {}},
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            state = client.get_config_state("keboola.ex-db-snowflake", "42")
            assert state == {}

    def test_get_config_state_with_branch_id(self, httpx_mock) -> None:
        """get_config_state(branch_id) uses branch-scoped URL."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/123/components/keboola.ex-db-snowflake/configs/42",
            json={"id": "42", "state": {"key": "value"}},
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            state = client.get_config_state("keboola.ex-db-snowflake", "42", branch_id=123)
            assert state == {"key": "value"}


class TestListComponentsWithConfigs:
    """Tests for list_components_with_configs() including the include_state flag."""

    def test_list_components_with_configs_default(self, httpx_mock) -> None:
        """Default call sends include=configuration,rows."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components?include=configuration%2Crows",
            json=[],
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.list_components_with_configs()
            assert result == []

    def test_list_components_with_configs_include_state(self, httpx_mock) -> None:
        """include_state=True adds ``state`` to the include query param."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components?include=configuration%2Crows%2Cstate",
            json=[],
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.list_components_with_configs(include_state=True)
            assert result == []

    def test_list_components_with_configs_include_state_with_branch(self, httpx_mock) -> None:
        """include_state+branch_id combines correctly in the URL."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/123/components?include=configuration%2Crows%2Cstate",
            json=[],
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.list_components_with_configs(branch_id=123, include_state=True)
            assert result == []


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

    def test_list_buckets_with_branch_id(self, httpx_mock) -> None:
        """list_buckets(branch_id=123) uses branch-prefixed URL."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/123/buckets",
            json=SAMPLE_BUCKETS,
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.list_buckets(branch_id=123)
            assert len(result) == 2
            assert result[0]["id"] == "in.c-data"

    def test_list_buckets_with_branch_id_and_include(self, httpx_mock) -> None:
        """list_buckets(branch_id=123, include=) combines branch prefix with params."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/123/buckets?include=linkedBuckets",
            json=SAMPLE_BUCKETS_WITH_SHARING,
            status_code=200,
        )

        with KeboolaClient(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ) as client:
            result = client.list_buckets(include="linkedBuckets", branch_id=123)
            assert len(result) == 1


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


class TestConfigRowMethods:
    """Tests for create_config_row / update_config_row / delete_config_row.

    These methods are wired into ``sync push`` for row-level deployment (FIIA's
    primary use case: deploying ``keboola.variables`` values rows). They were
    previously unreachable from the service layer -- these tests lock the
    HTTP contract: URL, method, form-encoding, ``configuration`` JSON-stringified.
    """

    TOKEN = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"

    @staticmethod
    def _parse_form_body(request: httpx.Request) -> dict[str, str]:
        """Parse form-encoded request body to a flat str->str dict."""
        parsed = parse_qs(request.content.decode("utf-8"), keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}

    def test_create_config_row_no_branch(self, httpx_mock) -> None:
        """POST to /v2/storage/components/{c}/configs/{id}/rows with configuration JSON-stringified."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components/keboola.variables/configs/vars-1/rows",
            method="POST",
            json={"id": "row-new", "name": "Main", "configuration": {"values": []}},
            status_code=201,
        )

        with KeboolaClient(stack_url="https://connection.keboola.com", token=self.TOKEN) as client:
            result = client.create_config_row(
                component_id="keboola.variables",
                config_id="vars-1",
                name="Main",
                configuration={"values": [{"name": "year_start", "value": "2016"}]},
                description="default values",
            )

        assert result["id"] == "row-new"
        body = self._parse_form_body(httpx_mock.get_requests()[0])
        assert body["name"] == "Main"
        assert body["description"] == "default values"
        # configuration must be JSON-stringified, not nested form-fields
        assert json.loads(body["configuration"]) == {
            "values": [{"name": "year_start", "value": "2016"}]
        }

    def test_create_config_row_with_branch(self, httpx_mock) -> None:
        """branch_id routes the POST to /v2/storage/branch/{id}/components/.../rows."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/200/components/keboola.variables/configs/vars-1/rows",
            method="POST",
            json={"id": "row-new"},
            status_code=201,
        )

        with KeboolaClient(stack_url="https://connection.keboola.com", token=self.TOKEN) as client:
            client.create_config_row(
                component_id="keboola.variables",
                config_id="vars-1",
                name="dev",
                configuration={"values": []},
                branch_id=200,
            )

    def test_create_config_row_api_error_propagates(self, httpx_mock) -> None:
        """HTTP 400 from the row-create endpoint surfaces as KeboolaApiError."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components/keboola.variables/configs/vars-1/rows",
            method="POST",
            json={"error": "Validation", "code": "validation", "message": "bad row"},
            status_code=400,
        )

        with KeboolaClient(stack_url="https://connection.keboola.com", token=self.TOKEN) as client:
            with pytest.raises(KeboolaApiError) as excinfo:
                client.create_config_row(
                    component_id="keboola.variables",
                    config_id="vars-1",
                    name="bad",
                    configuration={"values": "not-a-list"},
                )
            assert excinfo.value.status_code == 400

    def test_update_config_row_full_payload(self, httpx_mock) -> None:
        """PUT with all fields: name, description, configuration, changeDescription."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components/keboola.variables/configs/vars-1/rows/row-1",
            method="PUT",
            json={"id": "row-1", "name": "Updated"},
            status_code=200,
        )

        with KeboolaClient(stack_url="https://connection.keboola.com", token=self.TOKEN) as client:
            client.update_config_row(
                component_id="keboola.variables",
                config_id="vars-1",
                row_id="row-1",
                name="Updated",
                configuration={"values": [{"name": "year", "value": "2025"}]},
                description="new",
                change_description="Deployed via kbagent sync push",
            )

        body = self._parse_form_body(httpx_mock.get_requests()[0])
        assert body["name"] == "Updated"
        assert body["description"] == "new"
        assert body["changeDescription"] == "Deployed via kbagent sync push"
        assert json.loads(body["configuration"]) == {"values": [{"name": "year", "value": "2025"}]}

    def test_update_config_row_partial_omits_unset_fields(self, httpx_mock) -> None:
        """None-valued fields are NOT included in the form body (partial update)."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components/keboola.variables/configs/vars-1/rows/row-1",
            method="PUT",
            json={"id": "row-1"},
            status_code=200,
        )

        with KeboolaClient(stack_url="https://connection.keboola.com", token=self.TOKEN) as client:
            client.update_config_row(
                component_id="keboola.variables",
                config_id="vars-1",
                row_id="row-1",
                configuration={"values": []},
                # name, description, change_description intentionally omitted
            )

        body = self._parse_form_body(httpx_mock.get_requests()[0])
        assert "configuration" in body
        assert "name" not in body
        assert "description" not in body
        assert "changeDescription" not in body

    def test_update_config_row_with_branch(self, httpx_mock) -> None:
        """branch_id routes the PUT to the dev-branch endpoint."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/200/components/keboola.variables/configs/vars-1/rows/row-1",
            method="PUT",
            json={"id": "row-1"},
            status_code=200,
        )

        with KeboolaClient(stack_url="https://connection.keboola.com", token=self.TOKEN) as client:
            client.update_config_row(
                component_id="keboola.variables",
                config_id="vars-1",
                row_id="row-1",
                name="dev-version",
                branch_id=200,
            )

    def test_delete_config_row_no_branch(self, httpx_mock) -> None:
        """DELETE to /v2/storage/components/.../rows/{row_id} returns None."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components/keboola.variables/configs/vars-1/rows/row-1",
            method="DELETE",
            status_code=204,
        )

        with KeboolaClient(stack_url="https://connection.keboola.com", token=self.TOKEN) as client:
            result = client.delete_config_row(
                component_id="keboola.variables",
                config_id="vars-1",
                row_id="row-1",
            )
        assert result is None

    def test_delete_config_row_with_branch(self, httpx_mock) -> None:
        """branch_id routes the DELETE to the dev-branch endpoint."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/200/components/keboola.variables/configs/vars-1/rows/row-1",
            method="DELETE",
            status_code=204,
        )

        with KeboolaClient(stack_url="https://connection.keboola.com", token=self.TOKEN) as client:
            client.delete_config_row(
                component_id="keboola.variables",
                config_id="vars-1",
                row_id="row-1",
                branch_id=200,
            )

    def test_delete_config_row_not_found_raises(self, httpx_mock) -> None:
        """HTTP 404 on DELETE surfaces as KeboolaApiError with status 404."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/components/keboola.variables/configs/vars-1/rows/missing",
            method="DELETE",
            json={"error": "Not found", "code": "notFound"},
            status_code=404,
        )

        with KeboolaClient(stack_url="https://connection.keboola.com", token=self.TOKEN) as client:
            with pytest.raises(KeboolaApiError) as excinfo:
                client.delete_config_row(
                    component_id="keboola.variables",
                    config_id="vars-1",
                    row_id="missing",
                )
            assert excinfo.value.status_code == 404


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


_ABS_SAS = (
    "BlobEndpoint=https://kbcftp.blob.core.windows.net;"
    "SharedAccessSignature=sv=2017-11-09&sr=c&sp=rwl&sig=abc123"
)
_ABS_UPLOAD_PARAMS = {
    "blobName": "data.csv",
    "accountName": "kbcftp",
    "container": "exp-15-files-123",
    "absCredentials": {"SASConnectionString": _ABS_SAS},
}


class TestUploadToCloudAzure:
    """Tests for _upload_to_cloud Azure Blob Storage path."""

    def test_abs_put_uses_write_sas(self, httpx_mock, tmp_path) -> None:
        """Azure upload constructs URL from absUploadParams (not read-only url)."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_bytes(b"id\n1\n")
        expected_url = (
            "https://kbcftp.blob.core.windows.net/exp-15-files-123/data.csv"
            "?sv=2017-11-09&sr=c&sp=rwl&sig=abc123"
        )
        httpx_mock.add_response(url=expected_url, method="PUT", status_code=201)
        upload_info = {
            "url": "https://kbcftp.blob.core.windows.net/read-only?sp=rl",
            "absUploadParams": _ABS_UPLOAD_PARAMS,
        }
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            client._upload_to_cloud(upload_info, str(csv_file))

        put_req = next(r for r in httpx_mock.get_requests() if r.method == "PUT")
        assert "sp=rwl" in str(put_req.url)
        assert "read-only" not in str(put_req.url)

    def test_abs_put_includes_blob_type_header(self, httpx_mock, tmp_path) -> None:
        """Azure PUT must include x-ms-blob-type: BlockBlob header."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_bytes(b"id\n1\n")
        httpx_mock.add_response(method="PUT", status_code=201)
        upload_info = {
            "url": "https://kbcftp.blob.core.windows.net/read-only?sp=rl",
            "absUploadParams": _ABS_UPLOAD_PARAMS,
        }
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            client._upload_to_cloud(upload_info, str(csv_file))

        put_req = next(r for r in httpx_mock.get_requests() if r.method == "PUT")
        assert put_req.headers.get("x-ms-blob-type") == "BlockBlob"

    def test_abs_put_accepts_201(self, httpx_mock, tmp_path) -> None:
        """Azure returns 201 Created on success — must not raise."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_bytes(b"id\n1\n")
        httpx_mock.add_response(method="PUT", status_code=201)
        upload_info = {
            "url": "https://kbcftp.blob.core.windows.net/read-only?sp=rl",
            "absUploadParams": _ABS_UPLOAD_PARAMS,
        }
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            client._upload_to_cloud(upload_info, str(csv_file))  # should not raise


class TestBuildAbsUploadUrl:
    """Tests for _build_abs_upload_url helper."""

    def test_parses_connection_string(self) -> None:
        from keboola_agent_cli.client import _build_abs_upload_url

        params = {
            "blobName": "test.csv",
            "container": "exp-15-files-123",
            "absCredentials": {
                "SASConnectionString": (
                    "BlobEndpoint=https://account.blob.core.windows.net;"
                    "SharedAccessSignature=sv=2017-11-09&sr=c&sp=rwl&sig=abc%2Bxyz"
                ),
            },
        }
        url = _build_abs_upload_url(params)
        assert url == (
            "https://account.blob.core.windows.net/exp-15-files-123/test.csv"
            "?sv=2017-11-09&sr=c&sp=rwl&sig=abc%2Bxyz"
        )


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


class TestCreateJob:
    """Tests for create_job() - Queue API job creation with branch support."""

    def test_create_job_without_branch(self, httpx_mock) -> None:
        """create_job() without branch_id does not include branchId in body."""
        job_response = {"id": 555, "status": "waiting", "component": "keboola.ex-http"}
        httpx_mock.add_response(
            url="https://queue.keboola.com/jobs",
            method="POST",
            json=job_response,
            status_code=201,
        )

        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            result = client.create_job(component_id="keboola.ex-http", config_id="42")

        assert result["id"] == 555
        request = httpx_mock.get_request()
        import json

        body = json.loads(request.content)
        assert body["component"] == "keboola.ex-http"
        assert body["config"] == "42"
        assert "branchId" not in body

    def test_create_job_with_branch(self, httpx_mock) -> None:
        """create_job() with branch_id includes branchId in POST body."""
        job_response = {"id": 556, "status": "waiting", "branchId": "789"}
        httpx_mock.add_response(
            url="https://queue.keboola.com/jobs",
            method="POST",
            json=job_response,
            status_code=201,
        )

        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            result = client.create_job(
                component_id="keboola.snowflake-transformation",
                config_id="100",
                branch_id=789,
            )

        assert result["id"] == 556
        assert result["branchId"] == "789"
        request = httpx_mock.get_request()
        import json

        body = json.loads(request.content)
        assert body["branchId"] == "789"
        assert body["component"] == "keboola.snowflake-transformation"
        assert body["config"] == "100"

    def test_create_job_with_branch_and_row_ids(self, httpx_mock) -> None:
        """create_job() with branch_id and config_row_ids includes both in body."""
        job_response = {"id": 557, "status": "waiting"}
        httpx_mock.add_response(
            url="https://queue.keboola.com/jobs",
            method="POST",
            json=job_response,
            status_code=201,
        )

        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            result = client.create_job(
                component_id="keboola.ex-http",
                config_id="42",
                branch_id=123,
                config_row_ids=["row1", "row2"],
            )

        assert result["id"] == 557
        request = httpx_mock.get_request()
        import json

        body = json.loads(request.content)
        assert body["branchId"] == "123"
        assert body["configRowIds"] == ["row1", "row2"]

    def test_create_job_with_variable_values_id(self, httpx_mock) -> None:
        """create_job() with variable_values_id forwards it as ``variableValuesId``."""
        httpx_mock.add_response(
            url="https://queue.keboola.com/jobs",
            method="POST",
            json={"id": 600, "status": "waiting"},
            status_code=201,
        )

        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            client.create_job(
                component_id="keboola.snowflake-transformation",
                config_id="100",
                variable_values_id="row-vars-001",
            )

        import json

        body = json.loads(httpx_mock.get_request().content)
        assert body["variableValuesId"] == "row-vars-001"

    def test_create_job_without_variable_values_id_omits_field(self, httpx_mock) -> None:
        """Default call path does not include ``variableValuesId`` in the body.

        Locks the "only send when set" contract so configs without linked
        variables do not trip validation on the Queue API side.
        """
        httpx_mock.add_response(
            url="https://queue.keboola.com/jobs",
            method="POST",
            json={"id": 601, "status": "waiting"},
            status_code=201,
        )

        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            client.create_job(component_id="keboola.ex-http", config_id="42")

        import json

        body = json.loads(httpx_mock.get_request().content)
        assert "variableValuesId" not in body


class TestKillJob:
    """Tests for kill_job() - Queue API POST /jobs/{id}/kill."""

    def test_kill_job_success(self, httpx_mock) -> None:
        """kill_job() returns full job dict with desiredStatus=terminating on 200."""
        job_response = {
            "id": "1234",
            "status": "processing",
            "desiredStatus": "terminating",
            "isFinished": False,
        }
        httpx_mock.add_response(
            url="https://queue.keboola.com/jobs/1234/kill",
            method="POST",
            json=job_response,
            status_code=200,
        )

        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            result = client.kill_job("1234")

        assert result["id"] == "1234"
        assert result["desiredStatus"] == "terminating"

    def test_kill_job_400_not_killable(self, httpx_mock) -> None:
        """kill_job() raises KeboolaApiError(400) on terminal-state jobs."""
        httpx_mock.add_response(
            url="https://queue.keboola.com/jobs/9999/kill",
            method="POST",
            json={
                "error": 'Job id "9999" is not in one of killable states (created,waiting,processing).',
                "code": 400,
            },
            status_code=400,
        )

        with (
            KeboolaClient(stack_url=_BASE, token=_TOKEN) as client,
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client.kill_job("9999")

        assert exc_info.value.status_code == 400
        assert "killable states" in exc_info.value.message.lower()

    def test_kill_job_500_404_mismatch(self, httpx_mock) -> None:
        """kill_job() surfaces Queue API's 500/404 inconsistency for bogus/success jobs.

        Queue API returns HTTP 500 with body code=404 for already-finished or
        missing jobs. Client passes the error through; service layer distinguishes
        the two cases via a follow-up GET.
        """
        # HTTP 500 triggers retry path (MAX_RETRIES total attempts); stub each.
        for _ in range(MAX_RETRIES):
            httpx_mock.add_response(
                url="https://queue.keboola.com/jobs/1/kill",
                method="POST",
                json={"error": "Internal Server Error occurred.", "code": 404},
                status_code=500,
            )

        with (
            KeboolaClient(stack_url=_BASE, token=_TOKEN) as client,
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client.kill_job("1")

        assert exc_info.value.status_code == 500

    def test_kill_job_url_encodes_job_id(self, httpx_mock) -> None:
        """kill_job() URL-encodes the job ID to prevent path injection."""
        httpx_mock.add_response(
            url="https://queue.keboola.com/jobs/ab%2Fcd/kill",
            method="POST",
            json={"id": "ab/cd", "status": "processing", "desiredStatus": "terminating"},
            status_code=200,
        )

        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            client.kill_job("ab/cd")


class TestIterPollIntervals:
    """Tests for _iter_poll_intervals -- curve math and strategy dispatch."""

    def test_exponential_curve_matches_constant(self) -> None:
        """First 30 yields == 2.0, next 48 == 5.0, then infinite 15.0."""
        from itertools import islice

        from keboola_agent_cli.client import _iter_poll_intervals

        seq = list(islice(_iter_poll_intervals("exponential"), 30 + 48 + 10))
        assert seq[:30] == [2.0] * 30
        assert seq[30:78] == [5.0] * 48
        assert seq[78:] == [15.0] * 10

    def test_fixed_yields_storage_interval_forever(self) -> None:
        """'fixed' yields STORAGE_JOB_POLL_INTERVAL indefinitely."""
        from itertools import islice

        from keboola_agent_cli.client import _iter_poll_intervals
        from keboola_agent_cli.constants import STORAGE_JOB_POLL_INTERVAL

        seq = list(islice(_iter_poll_intervals("fixed"), 5))
        assert seq == [STORAGE_JOB_POLL_INTERVAL] * 5


class TestWaitForQueueJob:
    """Tests for wait_for_queue_job -- strategy dispatch, deadline, failure."""

    def _mk_client(self):
        return KeboolaClient(stack_url=_BASE, token=_TOKEN)

    def test_wait_success_on_first_poll(self, httpx_mock, monkeypatch) -> None:
        """Finished job returns on the first poll; no sleep needed."""
        httpx_mock.add_response(
            url="https://queue.keboola.com/jobs/job-1",
            method="GET",
            json={"id": "job-1", "status": "success", "isFinished": True},
        )
        sleeps: list[float] = []
        monkeypatch.setattr("keboola_agent_cli.client.time.sleep", lambda s: sleeps.append(s))

        with self._mk_client() as client:
            job = client.wait_for_queue_job("job-1", max_wait=60.0)

        assert job["status"] == "success"
        assert sleeps == []

    def test_wait_honors_exponential_intervals(self, httpx_mock, monkeypatch) -> None:
        """Two unfinished polls then finished -- sleep args match the curve head."""
        for _ in range(2):
            httpx_mock.add_response(
                url="https://queue.keboola.com/jobs/job-2",
                method="GET",
                json={"id": "job-2", "status": "processing", "isFinished": False},
            )
        httpx_mock.add_response(
            url="https://queue.keboola.com/jobs/job-2",
            method="GET",
            json={"id": "job-2", "status": "success", "isFinished": True},
        )
        sleeps: list[float] = []
        monkeypatch.setattr("keboola_agent_cli.client.time.sleep", lambda s: sleeps.append(s))

        with self._mk_client() as client:
            client.wait_for_queue_job("job-2", max_wait=600.0, poll_strategy="exponential")

        # Two polls -> two sleeps; both at the 2s phase of the curve.
        assert sleeps == [2.0, 2.0]

    def test_wait_honors_fixed_strategy(self, httpx_mock, monkeypatch) -> None:
        """poll_strategy='fixed' sleeps STORAGE_JOB_POLL_INTERVAL between polls."""
        from keboola_agent_cli.constants import STORAGE_JOB_POLL_INTERVAL

        httpx_mock.add_response(
            url="https://queue.keboola.com/jobs/job-3",
            method="GET",
            json={"id": "job-3", "status": "processing", "isFinished": False},
        )
        httpx_mock.add_response(
            url="https://queue.keboola.com/jobs/job-3",
            method="GET",
            json={"id": "job-3", "status": "success", "isFinished": True},
        )
        sleeps: list[float] = []
        monkeypatch.setattr("keboola_agent_cli.client.time.sleep", lambda s: sleeps.append(s))

        with self._mk_client() as client:
            client.wait_for_queue_job("job-3", max_wait=600.0, poll_strategy="fixed")

        assert sleeps == [STORAGE_JOB_POLL_INTERVAL]

    def test_wait_rejects_unknown_strategy(self) -> None:
        """Invalid strategy raises ValueError before any network call."""
        with (
            self._mk_client() as client,
            pytest.raises(ValueError, match="Invalid poll_strategy"),
        ):
            client.wait_for_queue_job("job-4", poll_strategy="linear")

    def test_wait_raises_on_status_error(self, httpx_mock, monkeypatch) -> None:
        """status='error' produces QUEUE_JOB_FAILED with the job's error message."""
        httpx_mock.add_response(
            url="https://queue.keboola.com/jobs/bad-1",
            method="GET",
            json={
                "id": "bad-1",
                "status": "error",
                "isFinished": True,
                "result": {"message": "SQL compilation failed"},
            },
        )
        monkeypatch.setattr("keboola_agent_cli.client.time.sleep", lambda s: None)

        with self._mk_client() as client, pytest.raises(KeboolaApiError) as exc_info:
            client.wait_for_queue_job("bad-1", max_wait=60.0)

        assert exc_info.value.error_code == "QUEUE_JOB_FAILED"
        assert "SQL compilation failed" in exc_info.value.message

    def test_wait_raises_timeout_on_deadline(self, httpx_mock, monkeypatch) -> None:
        """Deadline exceeded raises QUEUE_JOB_TIMEOUT."""
        httpx_mock.add_response(
            url="https://queue.keboola.com/jobs/slow-1",
            method="GET",
            json={"id": "slow-1", "status": "processing", "isFinished": False},
        )

        # Use a stepped clock that stays at the final value if production
        # calls monotonic() more times than we expected. This makes the
        # test robust to refactors that add observability calls without
        # changing the behaviour under test.
        values = iter([0.0, 6.0])
        last = [0.0]

        def fake_monotonic() -> float:
            with contextlib.suppress(StopIteration):
                last[0] = next(values)
            return last[0]

        monkeypatch.setattr("keboola_agent_cli.client.time.monotonic", fake_monotonic)
        monkeypatch.setattr("keboola_agent_cli.client.time.sleep", lambda s: None)

        with self._mk_client() as client, pytest.raises(KeboolaApiError) as exc_info:
            client.wait_for_queue_job("slow-1", max_wait=5.0)

        assert exc_info.value.error_code == "QUEUE_JOB_TIMEOUT"
        assert exc_info.value.status_code == 504

    def test_wait_caps_sleep_to_remaining_deadline(self, httpx_mock, monkeypatch) -> None:
        """Last sleep before deadline is trimmed so we don't overshoot by one interval."""
        httpx_mock.add_response(
            url="https://queue.keboola.com/jobs/deadline-1",
            method="GET",
            json={"id": "deadline-1", "status": "processing", "isFinished": False},
        )
        httpx_mock.add_response(
            url="https://queue.keboola.com/jobs/deadline-1",
            method="GET",
            json={"id": "deadline-1", "status": "processing", "isFinished": False},
        )

        # First monotonic() sets deadline at 100.0; the second (after the
        # first get) returns 99.0 so remaining=1.0 < interval=2.0; the third
        # (after sleep) returns 101.0 so loop exits. Clamp on exhaustion
        # so a refactor adding observability calls doesn't crash the test.
        times = iter([0.0, 99.0, 101.0])
        last = [0.0]

        def fake_monotonic() -> float:
            with contextlib.suppress(StopIteration):
                last[0] = next(times)
            return last[0]

        monkeypatch.setattr("keboola_agent_cli.client.time.monotonic", fake_monotonic)
        sleeps: list[float] = []
        monkeypatch.setattr("keboola_agent_cli.client.time.sleep", lambda s: sleeps.append(s))

        with self._mk_client() as client, pytest.raises(KeboolaApiError):
            client.wait_for_queue_job("deadline-1", max_wait=100.0)

        assert sleeps == [1.0]  # trimmed from 2.0 to 1.0


class TestFetchJobEvents:
    """Tests for fetch_job_events -- runId-based Storage Events endpoint."""

    def test_events_list_payload(self, httpx_mock) -> None:
        payload = [
            {"uuid": "u1", "type": "info", "message": "starting", "runId": "j-1"},
            {"uuid": "u2", "type": "error", "message": "boom", "runId": "j-1"},
        ]
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/events?runId=j-1",
            method="GET",
            json=payload,
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            events = client.fetch_job_events("j-1")
        assert events == payload

    def test_events_limit_query_param(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/events?runId=j-2&limit=50",
            method="GET",
            json=[],
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            client.fetch_job_events("j-2", limit=50)

    def test_events_dict_wrapped_payload(self, httpx_mock) -> None:
        """Tolerant of a future dict shape {events: [...]}"""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/events?runId=j-3",
            method="GET",
            json={"events": [{"uuid": "u1"}], "total": 1},
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            events = client.fetch_job_events("j-3")
        assert events == [{"uuid": "u1"}]

    def test_events_unexpected_payload_returns_empty(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/events?runId=j-4",
            method="GET",
            json={"unexpected": "shape"},
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            events = client.fetch_job_events("j-4")
        assert events == []


_BRANCH_METADATA_SAMPLE = [
    {
        "id": 1001,
        "key": "KBC.projectDescription",
        "value": "# My project",
        "provider": "user",
        "timestamp": "2026-04-16T10:00:00+0200",
    },
    {
        "id": 1002,
        "key": "something.else",
        "value": "abc",
        "provider": "user",
        "timestamp": "2026-04-16T10:01:00+0200",
    },
]


class TestBranchMetadata:
    """Tests for branch metadata CRUD against /v2/storage/branch/{id}/metadata."""

    def test_list_branch_metadata_default(self, httpx_mock) -> None:
        """list_branch_metadata() hits /branch/default/metadata and returns entries."""
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/branch/default/metadata",
            method="GET",
            json=_BRANCH_METADATA_SAMPLE,
            status_code=200,
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            result = client.list_branch_metadata()
        assert result == _BRANCH_METADATA_SAMPLE

    def test_list_branch_metadata_numeric(self, httpx_mock) -> None:
        """list_branch_metadata(branch_id=123) hits /branch/123/metadata."""
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/branch/123/metadata",
            method="GET",
            json=[],
            status_code=200,
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            result = client.list_branch_metadata(branch_id=123)
        assert result == []

    def test_set_branch_metadata_encodes_php_indices(self, httpx_mock) -> None:
        """set_branch_metadata() URL-encodes PHP-style indices in the form body."""
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/branch/default/metadata",
            method="POST",
            json=_BRANCH_METADATA_SAMPLE[:1],
            status_code=201,
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            result = client.set_branch_metadata(
                entries=[("KBC.projectDescription", "# My project")]
            )

        assert result == _BRANCH_METADATA_SAMPLE[:1]
        request = httpx_mock.get_request()
        body = request.content.decode()
        # httpx URL-encodes "[" and "]" as %5B / %5D; accept either form.
        normalized = body.replace("%5B", "[").replace("%5D", "]")
        assert "metadata[0][key]=KBC.projectDescription" in normalized
        assert "metadata[0][value]=" in normalized
        assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")

    def test_set_branch_metadata_bulk(self, httpx_mock) -> None:
        """set_branch_metadata() encodes multiple entries with increasing indices."""
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/branch/default/metadata",
            method="POST",
            json=_BRANCH_METADATA_SAMPLE,
            status_code=201,
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            client.set_branch_metadata(entries=[("a", "1"), ("b", "2"), ("c", "3")])
        body = httpx_mock.get_request().content.decode()
        normalized = body.replace("%5B", "[").replace("%5D", "]")
        assert "metadata[0][key]=a" in normalized
        assert "metadata[1][key]=b" in normalized
        assert "metadata[2][key]=c" in normalized

    def test_delete_branch_metadata(self, httpx_mock) -> None:
        """delete_branch_metadata() issues DELETE on the right path."""
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/branch/default/metadata/1001",
            method="DELETE",
            status_code=204,
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            client.delete_branch_metadata(metadata_id=1001)
        assert httpx_mock.get_request().method == "DELETE"

    def test_get_branch_metadata_value_found(self, httpx_mock) -> None:
        """get_branch_metadata_value() returns the matching entry's value."""
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/branch/default/metadata",
            method="GET",
            json=_BRANCH_METADATA_SAMPLE,
            status_code=200,
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            value = client.get_branch_metadata_value(key="KBC.projectDescription")
        assert value == "# My project"

    def test_get_branch_metadata_value_missing(self, httpx_mock) -> None:
        """get_branch_metadata_value() returns sentinel when the key is absent."""
        from keboola_agent_cli.constants import METADATA_NOT_FOUND

        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/branch/default/metadata",
            method="GET",
            json=[],
            status_code=200,
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            value = client.get_branch_metadata_value(key="KBC.projectDescription")
        assert value is METADATA_NOT_FOUND


# ---------------------------------------------------------------------------
# Storage object metadata (bucket + table)
# ---------------------------------------------------------------------------

_STORAGE_META_RESPONSE = [
    {
        "id": "9001",
        "key": "KBC.description",
        "value": "A test description",
        "provider": "user",
        "timestamp": "2026-04-22T10:00:00+0200",
    },
]


class TestListBucketMetadata:
    """Tests for list_bucket_metadata() - GET /v2/storage/buckets/{id}/metadata."""

    def test_list_bucket_metadata_no_branch(self, httpx_mock) -> None:
        from urllib.parse import quote as url_quote

        safe_id = url_quote("in.c-bucket", safe="")
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/buckets/{safe_id}/metadata",
            method="GET",
            json=_STORAGE_META_RESPONSE,
            status_code=200,
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            result = client.list_bucket_metadata(bucket_id="in.c-bucket")
        assert result == _STORAGE_META_RESPONSE

    def test_list_bucket_metadata_with_branch(self, httpx_mock) -> None:
        from urllib.parse import quote as url_quote

        safe_id = url_quote("in.c-bucket", safe="")
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/branch/42/buckets/{safe_id}/metadata",
            method="GET",
            json=_STORAGE_META_RESPONSE,
            status_code=200,
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            result = client.list_bucket_metadata(bucket_id="in.c-bucket", branch_id=42)
        assert result == _STORAGE_META_RESPONSE


class TestSetBucketMetadata:
    """Tests for set_bucket_metadata() - POST /v2/storage/buckets/{id}/metadata."""

    def test_set_bucket_metadata_php_form_body(self, httpx_mock) -> None:
        """Encodes provider + PHP-array indices in the form body."""
        from urllib.parse import quote as url_quote

        safe_id = url_quote("in.c-my-bucket", safe="")
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/buckets/{safe_id}/metadata",
            method="POST",
            json=_STORAGE_META_RESPONSE,
            status_code=201,
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            result = client.set_bucket_metadata(
                bucket_id="in.c-my-bucket",
                entries=[("KBC.description", "A test description")],
            )

        assert result == _STORAGE_META_RESPONSE
        req = httpx_mock.get_request()
        body = req.content.decode().replace("%5B", "[").replace("%5D", "]")
        assert "provider=user" in body
        assert "metadata[0][key]=KBC.description" in body
        assert (
            "metadata[0][value]=A+test+description" in body
            or "A%20test%20description" in body
            or "A test description" in body
        )
        assert req.headers["content-type"].startswith("application/x-www-form-urlencoded")

    def test_set_bucket_metadata_with_branch(self, httpx_mock) -> None:
        """Uses branch prefix when branch_id is provided."""
        from urllib.parse import quote as url_quote

        safe_id = url_quote("in.c-my-bucket", safe="")
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/branch/42/buckets/{safe_id}/metadata",
            method="POST",
            json=_STORAGE_META_RESPONSE,
            status_code=201,
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            result = client.set_bucket_metadata(
                bucket_id="in.c-my-bucket",
                entries=[("KBC.description", "Branch desc")],
                branch_id=42,
            )
        assert result == _STORAGE_META_RESPONSE

    def test_set_bucket_metadata_multiple_entries(self, httpx_mock) -> None:
        """Multiple entries get sequential PHP indices."""
        from urllib.parse import quote as url_quote

        safe_id = url_quote("in.c-bucket", safe="")
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/buckets/{safe_id}/metadata",
            method="POST",
            json=_STORAGE_META_RESPONSE,
            status_code=201,
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            client.set_bucket_metadata(
                bucket_id="in.c-bucket",
                entries=[("k1", "v1"), ("k2", "v2")],
            )
        body = httpx_mock.get_request().content.decode().replace("%5B", "[").replace("%5D", "]")
        assert "metadata[0][key]=k1" in body
        assert "metadata[1][key]=k2" in body

    def test_set_bucket_metadata_system_provider(self, httpx_mock) -> None:
        """provider='system' is required for KBC.* keys (issue #224 fix path).

        The Storage API rejects user-provider writes on the reserved KBC.*
        namespace (e.g. ``KBC.createdBy.branch.id``); auto-materialize must
        be able to opt into system provider explicitly.
        """
        from urllib.parse import quote as url_quote

        safe_id = url_quote("in.c-bucket", safe="")
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/buckets/{safe_id}/metadata",
            method="POST",
            json=_STORAGE_META_RESPONSE,
            status_code=201,
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            client.set_bucket_metadata(
                bucket_id="in.c-bucket",
                entries=[("KBC.createdBy.branch.id", "1295438")],
                provider="system",
            )
        body = httpx_mock.get_request().content.decode().replace("%5B", "[").replace("%5D", "]")
        assert "provider=system" in body
        assert "metadata[0][key]=KBC.createdBy.branch.id" in body
        assert "metadata[0][value]=1295438" in body


class TestSetTableMetadata:
    """Tests for set_table_metadata() - POST /v2/storage/tables/{id}/metadata."""

    def test_set_table_metadata_php_form_body(self, httpx_mock) -> None:
        """Encodes provider + PHP-array indices for a table metadata POST."""
        from urllib.parse import quote as url_quote

        safe_id = url_quote("in.c-b.tbl", safe="")
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/tables/{safe_id}/metadata",
            method="POST",
            json=_STORAGE_META_RESPONSE,
            status_code=201,
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            result = client.set_table_metadata(
                table_id="in.c-b.tbl",
                entries=[("KBC.description", "A test description")],
            )
        assert result == _STORAGE_META_RESPONSE
        req = httpx_mock.get_request()
        body = req.content.decode().replace("%5B", "[").replace("%5D", "]")
        assert "provider=user" in body
        assert "metadata[0][key]=KBC.description" in body

    def test_set_table_metadata_with_branch(self, httpx_mock) -> None:
        """Uses branch prefix when branch_id is provided."""
        from urllib.parse import quote as url_quote

        safe_id = url_quote("in.c-b.tbl", safe="")
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/branch/7/tables/{safe_id}/metadata",
            method="POST",
            json=_STORAGE_META_RESPONSE,
            status_code=201,
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            result = client.set_table_metadata(
                table_id="in.c-b.tbl",
                entries=[("KBC.description", "Branch desc")],
                branch_id=7,
            )
        assert result == _STORAGE_META_RESPONSE

    def test_set_table_metadata_column_convention(self, httpx_mock) -> None:
        """Column descriptions use KBC.column.{name}.description key convention."""
        from urllib.parse import quote as url_quote

        safe_id = url_quote("in.c-b.tbl", safe="")
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/tables/{safe_id}/metadata",
            method="POST",
            json=_STORAGE_META_RESPONSE,
            status_code=201,
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            client.set_table_metadata(
                table_id="in.c-b.tbl",
                entries=[("KBC.column.city.description", "City name")],
            )
        body = httpx_mock.get_request().content.decode().replace("%5B", "[").replace("%5D", "]")
        assert "KBC.column.city.description" in body
