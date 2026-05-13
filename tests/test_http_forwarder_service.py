"""Tests for HttpForwarderService -- the business layer for ``kbagent http``.

The CLI tests in ``test_http_client_cmd.py`` exercise the same code path
end-to-end via Typer ``CliRunner`` + ``pytest-httpx``; this file targets
the service contract directly so future callers (e.g. an in-process
retry path) get type-stable assertions without spinning up Typer.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import pytest

from keboola_agent_cli.constants import ENV_KBAGENT_SERVE_TOKEN, ENV_KBAGENT_SERVE_URL
from keboola_agent_cli.errors import ErrorCode
from keboola_agent_cli.services.http_forwarder_service import (
    ForwardedResponse,
    ForwarderError,
    HttpForwarderService,
)


@pytest.fixture
def env_set(monkeypatch):
    """Pin the two required env vars so methods don't bail at endpoint resolve."""
    monkeypatch.setenv(ENV_KBAGENT_SERVE_URL, "http://127.0.0.1:8001")
    monkeypatch.setenv(ENV_KBAGENT_SERVE_TOKEN, "svc-bearer-test")


class TestResolveEndpoint:
    def test_returns_url_token_pair(self, env_set) -> None:
        url, token = HttpForwarderService.resolve_endpoint()
        assert url == "http://127.0.0.1:8001"
        assert token == "svc-bearer-test"

    def test_strips_trailing_slash(self, monkeypatch) -> None:
        monkeypatch.setenv(ENV_KBAGENT_SERVE_URL, "http://127.0.0.1:8001/")
        monkeypatch.setenv(ENV_KBAGENT_SERVE_TOKEN, "x")
        url, _ = HttpForwarderService.resolve_endpoint()
        assert url == "http://127.0.0.1:8001"  # no trailing /

    def test_missing_url_raises_exit_2(self, monkeypatch) -> None:
        monkeypatch.delenv(ENV_KBAGENT_SERVE_URL, raising=False)
        monkeypatch.setenv(ENV_KBAGENT_SERVE_TOKEN, "x")
        with pytest.raises(ForwarderError) as exc_info:
            HttpForwarderService.resolve_endpoint()
        assert exc_info.value.exit_code == 2
        assert exc_info.value.error_code == ErrorCode.CONFIG_ERROR

    def test_missing_token_raises_exit_2(self, monkeypatch) -> None:
        monkeypatch.setenv(ENV_KBAGENT_SERVE_URL, "http://x")
        monkeypatch.delenv(ENV_KBAGENT_SERVE_TOKEN, raising=False)
        with pytest.raises(ForwarderError):
            HttpForwarderService.resolve_endpoint()


class TestResolveBody:
    def test_none_returns_none(self) -> None:
        assert HttpForwarderService.resolve_body(None) is None
        assert HttpForwarderService.resolve_body("") is None

    def test_inline_json(self) -> None:
        assert HttpForwarderService.resolve_body('{"a": 1}') == {"a": 1}

    def test_at_file_reads_disk(self, tmp_path: Path) -> None:
        path = tmp_path / "body.json"
        path.write_text('{"from_file": true}', encoding="utf-8")
        assert HttpForwarderService.resolve_body(f"@{path}") == {"from_file": True}

    def test_dash_reads_stdin(self, monkeypatch) -> None:
        # Replace sys.stdin with a StringIO so .read() returns our payload
        # without blocking on a real TTY.
        monkeypatch.setattr("sys.stdin", io.StringIO('{"from_stdin": 1}'))
        assert HttpForwarderService.resolve_body("-") == {"from_stdin": 1}

    def test_invalid_json_raises_exit_2(self) -> None:
        with pytest.raises(ForwarderError) as exc_info:
            HttpForwarderService.resolve_body("{not json")
        assert exc_info.value.exit_code == 2
        assert "valid JSON" in exc_info.value.message


class TestRequest:
    def test_get_returns_forwarded_response_with_json(self, env_set, httpx_mock) -> None:
        httpx_mock.add_response(
            url="http://127.0.0.1:8001/projects",
            json={"projects": []},
            status_code=200,
        )
        svc = HttpForwarderService()
        result = svc.request("GET", "/projects", body=None, timeout=5.0)
        assert isinstance(result, ForwardedResponse)
        assert result.is_json is True
        assert result.decoded == {"projects": []}

    def test_get_normalizes_missing_leading_slash(self, env_set, httpx_mock) -> None:
        # Caller passes "projects", service should call "/projects".
        httpx_mock.add_response(
            url="http://127.0.0.1:8001/projects",
            json={"ok": True},
            status_code=200,
        )
        result = HttpForwarderService().request("GET", "projects", body=None, timeout=5.0)
        assert result.decoded == {"ok": True}

    def test_post_sends_json_body_and_content_type(self, env_set, httpx_mock) -> None:
        httpx_mock.add_response(
            url="http://127.0.0.1:8001/agents",
            json={"id": "abc"},
            status_code=201,
        )
        HttpForwarderService().request(
            "POST",
            "/agents",
            body='{"name": "x"}',
            timeout=5.0,
        )
        req = httpx_mock.get_requests()[0]
        assert req.headers["content-type"] == "application/json"
        assert json.loads(req.read()) == {"name": "x"}

    def test_bearer_header_present(self, env_set, httpx_mock) -> None:
        httpx_mock.add_response(
            url="http://127.0.0.1:8001/x",
            json={},
            status_code=200,
        )
        HttpForwarderService().request("GET", "/x", body=None, timeout=5.0)
        assert httpx_mock.get_requests()[0].headers["authorization"] == "Bearer svc-bearer-test"

    def test_4xx_raises_forwarder_error_exit_1(self, env_set, httpx_mock) -> None:
        httpx_mock.add_response(
            url="http://127.0.0.1:8001/forbidden",
            json={"status": "error", "error": {"message": "no"}},
            status_code=403,
        )
        with pytest.raises(ForwarderError) as exc_info:
            HttpForwarderService().request("GET", "/forbidden", body=None, timeout=5.0)
        assert exc_info.value.exit_code == 1
        assert exc_info.value.error_code == ErrorCode.API_ERROR
        assert "403" in exc_info.value.message

    def test_transport_error_raises_exit_4(self, env_set, httpx_mock) -> None:
        httpx_mock.add_exception(httpx.ConnectError("conn refused"))
        with pytest.raises(ForwarderError) as exc_info:
            HttpForwarderService().request("GET", "/x", body=None, timeout=5.0)
        assert exc_info.value.exit_code == 4
        assert exc_info.value.error_code == ErrorCode.CONNECTION_ERROR

    def test_non_json_response_returns_text_with_is_json_false(self, env_set, httpx_mock) -> None:
        httpx_mock.add_response(
            url="http://127.0.0.1:8001/text",
            content=b"plain body",
            headers={"content-type": "text/plain"},
            status_code=200,
        )
        result = HttpForwarderService().request("GET", "/text", body=None, timeout=5.0)
        assert result.is_json is False
        assert result.decoded == "plain body"

    def test_json_content_type_but_invalid_json_falls_back_to_text(
        self, env_set, httpx_mock
    ) -> None:
        # Server lies about content-type; we should not crash.
        httpx_mock.add_response(
            url="http://127.0.0.1:8001/lies",
            content=b"not really json",
            headers={"content-type": "application/json"},
            status_code=200,
        )
        result = HttpForwarderService().request("GET", "/lies", body=None, timeout=5.0)
        assert result.is_json is False
        assert result.decoded == "not really json"
