"""Tests for the ``kbagent http`` CLI subcommand (thin HTTP client).

Two surfaces under test:

- Env-var enforcement: without KBAGENT_SERVE_URL + KBAGENT_SERVE_TOKEN, the
  command must refuse to run (exit 2) -- the command has no meaningful
  target outside a ``kbagent serve`` subprocess context.
- Happy-path forwarding: GET/POST round-trips via pytest-httpx, including
  Bearer header injection, --body parsing (inline / @file / -), and 4xx
  error mapping.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.constants import (
    ENV_KBAGENT_SERVE_TOKEN,
    ENV_KBAGENT_SERVE_URL,
)

runner = CliRunner()

SERVE_URL = "http://127.0.0.1:8001"
SERVE_TOKEN = "test-bearer-fixture"


class TestEnvEnforcement:
    def test_missing_env_vars_exits_2(self, monkeypatch) -> None:
        monkeypatch.delenv(ENV_KBAGENT_SERVE_URL, raising=False)
        monkeypatch.delenv(ENV_KBAGENT_SERVE_TOKEN, raising=False)
        result = runner.invoke(app, ["http", "get", "/projects"])
        assert result.exit_code == 2
        assert "KBAGENT_SERVE_URL" in result.output
        assert "KBAGENT_SERVE_TOKEN" in result.output


class TestHttpGet:
    def test_get_forwards_bearer_and_returns_json(self, monkeypatch, httpx_mock) -> None:
        monkeypatch.setenv(ENV_KBAGENT_SERVE_URL, SERVE_URL)
        monkeypatch.setenv(ENV_KBAGENT_SERVE_TOKEN, SERVE_TOKEN)
        httpx_mock.add_response(
            url=f"{SERVE_URL}/projects",
            json={"projects": [{"alias": "padak"}]},
            status_code=200,
        )
        result = runner.invoke(app, ["http", "get", "/projects"])
        assert result.exit_code == 0
        assert "padak" in result.output
        # Bearer header must reach the server.
        req = httpx_mock.get_requests()[0]
        assert req.headers["authorization"] == f"Bearer {SERVE_TOKEN}"

    def test_get_normalizes_path_without_leading_slash(self, monkeypatch, httpx_mock) -> None:
        monkeypatch.setenv(ENV_KBAGENT_SERVE_URL, SERVE_URL)
        monkeypatch.setenv(ENV_KBAGENT_SERVE_TOKEN, SERVE_TOKEN)
        httpx_mock.add_response(url=f"{SERVE_URL}/health/ping", json={"status": "ok"})
        result = runner.invoke(app, ["http", "get", "health/ping"])
        assert result.exit_code == 0

    def test_get_4xx_maps_to_exit_1(self, monkeypatch, httpx_mock) -> None:
        monkeypatch.setenv(ENV_KBAGENT_SERVE_URL, SERVE_URL)
        monkeypatch.setenv(ENV_KBAGENT_SERVE_TOKEN, SERVE_TOKEN)
        httpx_mock.add_response(
            url=f"{SERVE_URL}/projects",
            json={"status": "error", "error": {"code": "UNAUTHORIZED", "message": "nope"}},
            status_code=401,
        )
        result = runner.invoke(app, ["http", "get", "/projects"])
        assert result.exit_code == 1
        assert "401" in result.output


class TestHttpPostBody:
    def test_inline_json_body(self, monkeypatch, httpx_mock) -> None:
        monkeypatch.setenv(ENV_KBAGENT_SERVE_URL, SERVE_URL)
        monkeypatch.setenv(ENV_KBAGENT_SERVE_TOKEN, SERVE_TOKEN)
        httpx_mock.add_response(
            url=f"{SERVE_URL}/agents/test", json={"run_id": "abc"}, status_code=200
        )
        body = json.dumps({"name": "x", "cron": "0 * * * *"})
        result = runner.invoke(app, ["http", "post", "/agents/test", "--body", body])
        assert result.exit_code == 0
        sent = json.loads(httpx_mock.get_requests()[0].read())
        assert sent == {"name": "x", "cron": "0 * * * *"}

    def test_file_body_with_at_prefix(self, monkeypatch, httpx_mock, tmp_path: Path) -> None:
        monkeypatch.setenv(ENV_KBAGENT_SERVE_URL, SERVE_URL)
        monkeypatch.setenv(ENV_KBAGENT_SERVE_TOKEN, SERVE_TOKEN)
        body_file = tmp_path / "body.json"
        body_file.write_text(json.dumps({"foo": "bar"}))
        httpx_mock.add_response(url=f"{SERVE_URL}/x", json={"ok": True}, status_code=200)
        result = runner.invoke(app, ["http", "post", "/x", "--body", f"@{body_file}"])
        assert result.exit_code == 0
        sent = json.loads(httpx_mock.get_requests()[0].read())
        assert sent == {"foo": "bar"}

    def test_invalid_json_body_exits_2(self, monkeypatch) -> None:
        monkeypatch.setenv(ENV_KBAGENT_SERVE_URL, SERVE_URL)
        monkeypatch.setenv(ENV_KBAGENT_SERVE_TOKEN, SERVE_TOKEN)
        result = runner.invoke(app, ["http", "post", "/x", "--body", "not json{"])
        assert result.exit_code == 2
        assert "valid JSON" in result.output
