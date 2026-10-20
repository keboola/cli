"""Tests for the ``kbagent http`` CLI subcommand (thin HTTP client).

Three areas under test:

- Env-var enforcement: without KBAGENT_SERVE_URL + KBAGENT_SERVE_TOKEN, the
  command must refuse to run (exit 2) -- the command has no meaningful
  target outside a ``kbagent serve`` subprocess context.
- Permission check: a denied ``http.<verb>`` exits 6 before any request.
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
    EXIT_PERMISSION_DENIED,
)

runner = CliRunner()

SERVE_URL = "http://127.0.0.1:8001"
SERVE_TOKEN = "test-bearer-fixture"


class TestEnvEnforcement:
    def test_missing_env_vars_exits_5(self, monkeypatch) -> None:
        """CONFIG_ERROR exits 5 like everywhere else. It exited 2 before 1.0.0."""
        monkeypatch.delenv(ENV_KBAGENT_SERVE_URL, raising=False)
        monkeypatch.delenv(ENV_KBAGENT_SERVE_TOKEN, raising=False)
        result = runner.invoke(app, ["http", "get", "/projects"])
        assert result.exit_code == 5
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


class TestPermissionCheck:
    """`http` checks `http.<verb>` against the caller's policy before any request."""

    @staticmethod
    def _invoke(monkeypatch, tmp_path: Path, *args: str):
        monkeypatch.setenv(ENV_KBAGENT_SERVE_URL, SERVE_URL)
        monkeypatch.setenv(ENV_KBAGENT_SERVE_TOKEN, SERVE_TOKEN)
        return runner.invoke(app, ["--config-dir", str(tmp_path), "--json", *args])

    def test_deny_destructive_blocks_delete(self, monkeypatch, tmp_path: Path, httpx_mock) -> None:
        result = self._invoke(monkeypatch, tmp_path, "--deny-destructive", "http", "delete", "/x")
        assert result.exit_code == EXIT_PERMISSION_DENIED
        assert "http.delete" in result.output
        assert httpx_mock.get_requests() == []

    def test_deny_writes_blocks_post(self, monkeypatch, tmp_path: Path, httpx_mock) -> None:
        result = self._invoke(
            monkeypatch, tmp_path, "--deny-writes", "http", "post", "/x", "--body", "{}"
        )
        assert result.exit_code == EXIT_PERMISSION_DENIED
        assert httpx_mock.get_requests() == []

    def test_deny_writes_allows_get(self, monkeypatch, tmp_path: Path, httpx_mock) -> None:
        httpx_mock.add_response(url=f"{SERVE_URL}/projects", json={"projects": []})
        result = self._invoke(monkeypatch, tmp_path, "--deny-writes", "http", "get", "/projects")
        assert result.exit_code == 0, result.output
