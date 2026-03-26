"""Tests for AiServiceClient - URL derivation, component detail, suggestions."""

from urllib.parse import quote

import pytest

from keboola_agent_cli.ai_client import AiServiceClient
from keboola_agent_cli.errors import KeboolaApiError

STACK_URL = "https://connection.keboola.com"
AI_BASE_URL = "https://ai.keboola.com"
TOKEN = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"

SAMPLE_COMPONENT_DETAIL = {
    "componentId": "keboola.ex-http",
    "componentName": "HTTP",
    "componentType": "extractor",
    "componentCategories": ["API"],
    "componentFlags": [],
    "description": "Download CSV files from HTTP/HTTPS URLs",
    "longDescription": "",
    "documentationUrl": "https://help.keboola.com/components/extractors/storage/http/",
    "documentation": "Some markdown docs",
    "configurationSchema": {
        "type": "object",
        "required": ["baseUrl"],
        "properties": {"baseUrl": {"type": "string"}},
    },
    "configurationRowSchema": {},
    "rootConfigurationExamples": [{"parameters": {"baseUrl": "https://example.com"}}],
    "rowConfigurationExamples": [],
}

SAMPLE_SUGGEST_RESPONSE = {
    "components": [
        {"componentId": "keboola.ex-http", "score": 0.95, "source": "ai"},
        {"componentId": "keboola.ex-aws-s3", "score": 0.7, "source": "ai"},
    ]
}


class TestDeriveAiUrl:
    """Verify AI Service URL derivation via shared _derive_service_url."""

    def test_derive_ai_url_us_stack(self) -> None:
        """US stack: connection.keboola.com -> ai.keboola.com."""
        result = AiServiceClient._derive_service_url("https://connection.keboola.com", "ai")
        assert result == "https://ai.keboola.com"

    def test_derive_ai_url_eu_stack(self) -> None:
        """EU stack: connection.eu-central-1.keboola.com -> ai.eu-central-1.keboola.com."""
        result = AiServiceClient._derive_service_url(
            "https://connection.eu-central-1.keboola.com", "ai"
        )
        assert result == "https://ai.eu-central-1.keboola.com"

    def test_derive_ai_url_azure_stack(self) -> None:
        """Azure stack: connection.north-europe.azure.keboola.com -> ai.north-europe.azure.keboola.com."""
        result = AiServiceClient._derive_service_url(
            "https://connection.north-europe.azure.keboola.com", "ai"
        )
        assert result == "https://ai.north-europe.azure.keboola.com"

    def test_derive_ai_url_trailing_slash(self) -> None:
        """Trailing slash in input URL is preserved in derivation."""
        result = AiServiceClient._derive_service_url("https://connection.keboola.com/", "ai")
        assert result == "https://ai.keboola.com/"


class TestGetComponentDetail:
    """Verify get_component_detail() fetches and returns component documentation."""

    def test_get_component_detail_success(self, httpx_mock) -> None:
        """get_component_detail() returns parsed JSON on 200 response."""
        httpx_mock.add_response(
            url=f"{AI_BASE_URL}/docs/components/keboola.ex-http",
            json=SAMPLE_COMPONENT_DETAIL,
            status_code=200,
        )

        client = AiServiceClient(stack_url=STACK_URL, token=TOKEN)
        try:
            result = client.get_component_detail("keboola.ex-http")

            assert result["componentId"] == "keboola.ex-http"
            assert result["componentName"] == "HTTP"
            assert result["componentType"] == "extractor"
            assert result["componentCategories"] == ["API"]
            assert result["description"] == "Download CSV files from HTTP/HTTPS URLs"
            assert result["configurationSchema"]["required"] == ["baseUrl"]
            assert len(httpx_mock.get_requests()) == 1
        finally:
            client.close()

    def test_get_component_detail_not_found(self, httpx_mock) -> None:
        """get_component_detail() raises KeboolaApiError on 404 response."""
        httpx_mock.add_response(
            url=f"{AI_BASE_URL}/docs/components/nonexistent.component",
            json={"error": "Component not found"},
            status_code=404,
        )

        client = AiServiceClient(stack_url=STACK_URL, token=TOKEN)
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client.get_component_detail("nonexistent.component")

            assert exc_info.value.status_code == 404
            assert exc_info.value.error_code == "NOT_FOUND"
            assert exc_info.value.retryable is False
        finally:
            client.close()


class TestSuggestComponents:
    """Verify suggest_components() sends prompt and returns ranked results."""

    def test_suggest_components_success(self, httpx_mock) -> None:
        """suggest_components() returns list of component suggestions."""
        httpx_mock.add_response(
            url=f"{AI_BASE_URL}/suggest/component",
            json=SAMPLE_SUGGEST_RESPONSE,
            status_code=200,
        )

        client = AiServiceClient(stack_url=STACK_URL, token=TOKEN)
        try:
            result = client.suggest_components("download files from HTTP")

            assert len(result) == 2
            assert result[0]["componentId"] == "keboola.ex-http"
            assert result[0]["score"] == 0.95
            assert result[1]["componentId"] == "keboola.ex-aws-s3"

            # Verify the request payload
            request = httpx_mock.get_requests()[0]
            assert request.method == "POST"
            import json

            body = json.loads(request.content)
            assert body == {"prompt": "download files from HTTP"}
        finally:
            client.close()

    def test_suggest_components_empty(self, httpx_mock) -> None:
        """suggest_components() returns empty list when no components match."""
        httpx_mock.add_response(
            url=f"{AI_BASE_URL}/suggest/component",
            json={"components": []},
            status_code=200,
        )

        client = AiServiceClient(stack_url=STACK_URL, token=TOKEN)
        try:
            result = client.suggest_components("something very obscure")

            assert result == []
            assert len(httpx_mock.get_requests()) == 1
        finally:
            client.close()


class TestUrlEncoding:
    """Verify component IDs with special characters are URL-encoded."""

    def test_url_encoding(self, httpx_mock) -> None:
        """Component ID with slash is URL-encoded so it does not create extra path segments."""
        component_id = "keboola/ex-http"
        encoded_id = quote(component_id, safe="")
        # encoded_id == "keboola%2Fex-http"
        assert encoded_id == "keboola%2Fex-http"

        httpx_mock.add_response(
            url=f"{AI_BASE_URL}/docs/components/{encoded_id}",
            json=SAMPLE_COMPONENT_DETAIL,
            status_code=200,
        )

        client = AiServiceClient(stack_url=STACK_URL, token=TOKEN)
        try:
            result = client.get_component_detail(component_id)

            assert result["componentId"] == "keboola.ex-http"
            # Verify the request was made with percent-encoded slash
            request = httpx_mock.get_requests()[0]
            raw_url = str(request.url)
            assert "%2F" in raw_url or "%2f" in raw_url
        finally:
            client.close()
