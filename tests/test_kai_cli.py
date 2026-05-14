"""Tests for Kai CLI commands via CliRunner.

Tests the `kbagent kai` subcommands: ping, ask, chat, history.
Each command is tested in both JSON and human output modes, plus error cases.
"""

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from helpers import setup_single_project
from keboola_agent_cli.cli import app
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.services.kai_service import KaiService

runner = CliRunner()


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes for CI where Rich adds color codes."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


class TestKaiPingCli:
    """Tests for `kbagent kai ping` command."""

    def test_kai_ping_json_output(self, tmp_config_dir: Path) -> None:
        """kai ping --json returns structured JSON with server info."""
        setup_single_project(tmp_config_dir)

        mock_service = MagicMock(spec=KaiService)
        mock_service.resolve_alias.return_value = "prod"
        mock_service.ping.return_value = {
            "project_alias": "prod",
            "timestamp": "2025-01-15T10:30:00+00:00",
            "app_name": "kai-api",
            "app_version": "1.2.3",
            "server_version": "2.0.0",
            "mcp_status": "connected",
        }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.kai.get_service",
                lambda ctx, name: mock_service,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "--config-dir",
                    str(tmp_config_dir),
                    "kai",
                    "ping",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["project_alias"] == "prod"
        assert output["data"]["app_name"] == "kai-api"
        assert output["data"]["mcp_status"] == "connected"

    def test_kai_ping_human_output(self, tmp_config_dir: Path) -> None:
        """kai ping in human mode shows readable server info."""
        setup_single_project(tmp_config_dir)

        mock_service = MagicMock(spec=KaiService)
        mock_service.resolve_alias.return_value = "prod"
        mock_service.ping.return_value = {
            "project_alias": "prod",
            "timestamp": "2025-01-15T10:30:00+00:00",
            "app_name": "kai-api",
            "app_version": "1.2.3",
            "server_version": "2.0.0",
            "mcp_status": "connected",
        }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.kai.get_service",
                lambda ctx, name: mock_service,
            )

            result = runner.invoke(
                app,
                [
                    "--config-dir",
                    str(tmp_config_dir),
                    "kai",
                    "ping",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "Kai is alive" in result.output
        assert "kai-api" in result.output
        assert "connected" in result.output

    def test_kai_ping_api_error(self, tmp_config_dir: Path) -> None:
        """kai ping with API error returns structured error and exit code 1."""
        setup_single_project(tmp_config_dir)

        mock_service = MagicMock(spec=KaiService)
        mock_service.resolve_alias.return_value = "prod"
        mock_service.ping.side_effect = KeboolaApiError(
            message="Kai ping failed: Connection refused",
            status_code=0,
            error_code="KAI_ERROR",
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.kai.get_service",
                lambda ctx, name: mock_service,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "--config-dir",
                    str(tmp_config_dir),
                    "kai",
                    "ping",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 1
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "KAI_ERROR" in output["error"]["code"]

    def test_kai_ping_not_enabled(self, tmp_config_dir: Path) -> None:
        """kai ping when Kai is not enabled returns KAI_NOT_ENABLED error."""
        setup_single_project(tmp_config_dir)

        mock_service = MagicMock(spec=KaiService)
        mock_service.resolve_alias.return_value = "prod"
        mock_service.ping.side_effect = KeboolaApiError(
            message="Kai is not enabled for project 'prod'.",
            status_code=0,
            error_code="KAI_NOT_ENABLED",
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.kai.get_service",
                lambda ctx, name: mock_service,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "--config-dir",
                    str(tmp_config_dir),
                    "kai",
                    "ping",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 1
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "KAI_NOT_ENABLED" in output["error"]["code"]

    def test_kai_ping_help(self) -> None:
        """kai ping --help shows usage information."""
        result = runner.invoke(app, ["kai", "ping", "--help"])

        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "Check Kai server health" in output
        assert "--project" in output


class TestKaiAskCli:
    """Tests for `kbagent kai ask` command."""

    def test_kai_ask_json_output(self, tmp_config_dir: Path) -> None:
        """kai ask --json returns structured JSON with response text."""
        setup_single_project(tmp_config_dir)

        mock_service = MagicMock(spec=KaiService)
        mock_service.resolve_alias.return_value = "prod"
        mock_service.ask.return_value = {
            "project_alias": "prod",
            "chat_id": "chat-xyz-789",
            "response": "You have 5 transformations configured.",
        }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.kai.get_service",
                lambda ctx, name: mock_service,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "--config-dir",
                    str(tmp_config_dir),
                    "kai",
                    "ask",
                    "--message",
                    "How many transformations?",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["chat_id"] == "chat-xyz-789"
        assert output["data"]["response"] == "You have 5 transformations configured."

    def test_kai_ask_human_output(self, tmp_config_dir: Path) -> None:
        """kai ask in human mode shows just the response text."""
        setup_single_project(tmp_config_dir)

        mock_service = MagicMock(spec=KaiService)
        mock_service.resolve_alias.return_value = "prod"
        mock_service.ask.return_value = {
            "project_alias": "prod",
            "chat_id": "chat-xyz-789",
            "response": "You have 5 transformations configured.",
        }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.kai.get_service",
                lambda ctx, name: mock_service,
            )

            result = runner.invoke(
                app,
                [
                    "--config-dir",
                    str(tmp_config_dir),
                    "kai",
                    "ask",
                    "--message",
                    "How many transformations?",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "You have 5 transformations configured." in result.output

    def test_kai_ask_api_error(self, tmp_config_dir: Path) -> None:
        """kai ask with API error returns structured error."""
        setup_single_project(tmp_config_dir)

        mock_service = MagicMock(spec=KaiService)
        mock_service.resolve_alias.return_value = "prod"
        mock_service.ask.side_effect = KeboolaApiError(
            message="Kai ask failed: timeout",
            status_code=0,
            error_code="KAI_ERROR",
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.kai.get_service",
                lambda ctx, name: mock_service,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "--config-dir",
                    str(tmp_config_dir),
                    "kai",
                    "ask",
                    "--message",
                    "test",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 1
        output = json.loads(result.output)
        assert output["status"] == "error"

    def test_kai_ask_help(self) -> None:
        """kai ask --help shows usage information."""
        result = runner.invoke(app, ["kai", "ask", "--help"])

        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "Ask Kai a one-shot question" in output
        assert "--message" in output
        assert "--project" in output


class TestKaiChatCli:
    """Tests for `kbagent kai chat` command."""

    def test_kai_chat_json_output(self, tmp_config_dir: Path) -> None:
        """kai chat --json returns structured JSON with chat_id and response."""
        setup_single_project(tmp_config_dir)

        mock_service = MagicMock(spec=KaiService)
        mock_service.resolve_alias.return_value = "prod"
        mock_service.chat_message.return_value = {
            "project_alias": "prod",
            "chat_id": "chat-session-001",
            "response": "I can help with that.",
        }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.kai.get_service",
                lambda ctx, name: mock_service,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "--config-dir",
                    str(tmp_config_dir),
                    "kai",
                    "chat",
                    "--message",
                    "Help me debug",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["chat_id"] == "chat-session-001"
        assert output["data"]["response"] == "I can help with that."

    def test_kai_chat_with_chat_id(self, tmp_config_dir: Path) -> None:
        """kai chat --chat-id passes the ID to the service for continuation."""
        setup_single_project(tmp_config_dir)

        mock_service = MagicMock(spec=KaiService)
        mock_service.resolve_alias.return_value = "prod"
        mock_service.chat_message.return_value = {
            "project_alias": "prod",
            "chat_id": "existing-chat-42",
            "response": "Continuing our conversation.",
        }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.kai.get_service",
                lambda ctx, name: mock_service,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "--config-dir",
                    str(tmp_config_dir),
                    "kai",
                    "chat",
                    "--message",
                    "What about now?",
                    "--chat-id",
                    "existing-chat-42",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        # Verify chat_id was passed through to the service
        mock_service.chat_message.assert_called_once_with(
            "prod", "What about now?", chat_id="existing-chat-42"
        )

    def test_kai_chat_human_output(self, tmp_config_dir: Path) -> None:
        """kai chat in human mode shows response text and chat ID."""
        setup_single_project(tmp_config_dir)

        mock_service = MagicMock(spec=KaiService)
        mock_service.resolve_alias.return_value = "prod"
        mock_service.chat_message.return_value = {
            "project_alias": "prod",
            "chat_id": "chat-session-001",
            "response": "Here is the answer.",
        }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.kai.get_service",
                lambda ctx, name: mock_service,
            )

            result = runner.invoke(
                app,
                [
                    "--config-dir",
                    str(tmp_config_dir),
                    "kai",
                    "chat",
                    "--message",
                    "question",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "Here is the answer." in result.output
        assert "chat-session-001" in result.output

    def test_kai_chat_help(self) -> None:
        """kai chat --help shows usage information."""
        result = runner.invoke(app, ["kai", "chat", "--help"])

        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "Send a message to Kai" in output
        assert "--message" in output
        assert "--chat-id" in output


class TestKaiHistoryCli:
    """Tests for `kbagent kai history` command."""

    def test_kai_history_json_output(self, tmp_config_dir: Path) -> None:
        """kai history --json returns structured JSON with chat list."""
        setup_single_project(tmp_config_dir)

        mock_service = MagicMock(spec=KaiService)
        mock_service.resolve_alias.return_value = "prod"
        mock_service.get_history.return_value = {
            "project_alias": "prod",
            "chats": [
                {
                    "id": "chat-aaa-111",
                    "title": "Data pipeline question",
                    "created_at": "2025-01-10T08:00:00+00:00",
                    "visibility": "private",
                },
                {
                    "id": "chat-bbb-222",
                    "title": "(untitled)",
                    "created_at": None,
                    "visibility": "public",
                },
            ],
            "has_more": False,
        }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.kai.get_service",
                lambda ctx, name: mock_service,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "--config-dir",
                    str(tmp_config_dir),
                    "kai",
                    "history",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert len(output["data"]["chats"]) == 2
        assert output["data"]["chats"][0]["title"] == "Data pipeline question"
        assert output["data"]["has_more"] is False

    def test_kai_history_with_limit(self, tmp_config_dir: Path) -> None:
        """kai history --limit passes the limit to the service."""
        setup_single_project(tmp_config_dir)

        mock_service = MagicMock(spec=KaiService)
        mock_service.resolve_alias.return_value = "prod"
        mock_service.get_history.return_value = {
            "project_alias": "prod",
            "chats": [],
            "has_more": False,
        }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.kai.get_service",
                lambda ctx, name: mock_service,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "--config-dir",
                    str(tmp_config_dir),
                    "kai",
                    "history",
                    "--limit",
                    "25",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 0
        mock_service.get_history.assert_called_once_with("prod", limit=25)

    def test_kai_history_human_output(self, tmp_config_dir: Path) -> None:
        """kai history in human mode shows a table of chats."""
        setup_single_project(tmp_config_dir)

        mock_service = MagicMock(spec=KaiService)
        mock_service.resolve_alias.return_value = "prod"
        mock_service.get_history.return_value = {
            "project_alias": "prod",
            "chats": [
                {
                    "id": "chat-aaa-111-full-uuid",
                    "title": "Pipeline debugging",
                    "created_at": "2025-01-10T08:00:00+00:00",
                    "visibility": "private",
                },
            ],
            "has_more": True,
        }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.kai.get_service",
                lambda ctx, name: mock_service,
            )

            result = runner.invoke(
                app,
                [
                    "--config-dir",
                    str(tmp_config_dir),
                    "kai",
                    "history",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "Pipeline debugging" in result.output
        assert "More chats available" in result.output

    def test_kai_history_empty_human(self, tmp_config_dir: Path) -> None:
        """kai history in human mode shows 'No chat history' when empty."""
        setup_single_project(tmp_config_dir)

        mock_service = MagicMock(spec=KaiService)
        mock_service.resolve_alias.return_value = "prod"
        mock_service.get_history.return_value = {
            "project_alias": "prod",
            "chats": [],
            "has_more": False,
        }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.kai.get_service",
                lambda ctx, name: mock_service,
            )

            result = runner.invoke(
                app,
                [
                    "--config-dir",
                    str(tmp_config_dir),
                    "kai",
                    "history",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "No chat history" in result.output

    def test_kai_history_help(self) -> None:
        """kai history --help shows usage information."""
        result = runner.invoke(app, ["kai", "history", "--help"])

        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "List recent Kai chat sessions" in output
        assert "--project" in output
        assert "--limit" in output


class TestKaiPreflightCli:
    """Tests for `kbagent kai preflight` command."""

    def test_kai_preflight_ok_json(self, tmp_config_dir: Path) -> None:
        """kai preflight --json returns ok=True payload when Kai is enabled."""
        setup_single_project(tmp_config_dir)

        mock_service = MagicMock(spec=KaiService)
        mock_service.resolve_alias.return_value = "prod"
        mock_service.preflight.return_value = {
            "project_alias": "prod",
            "ok": True,
            "is_master_token": True,
            "has_agent_chat_feature": True,
            "token_description": "owner-token",
            "project_id": 258,
            "project_name": "Production",
            "error": None,
        }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.kai.get_service",
                lambda ctx, name: mock_service,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "--config-dir",
                    str(tmp_config_dir),
                    "kai",
                    "preflight",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["ok"] is True
        assert output["data"]["is_master_token"] is True
        assert output["data"]["has_agent_chat_feature"] is True
        assert output["data"]["project_id"] == 258

    def test_kai_preflight_not_ready_human(self, tmp_config_dir: Path) -> None:
        """kai preflight in human mode shows 'Kai is NOT ready' + reason on failure."""
        setup_single_project(tmp_config_dir)

        mock_service = MagicMock(spec=KaiService)
        mock_service.resolve_alias.return_value = "prod"
        mock_service.preflight.return_value = {
            "project_alias": "prod",
            "ok": False,
            "is_master_token": False,
            "has_agent_chat_feature": True,
            "token_description": "custom",
            "project_id": 258,
            "project_name": "Production",
            "error": "the configured token is not the project's master token",
        }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.kai.get_service",
                lambda ctx, name: mock_service,
            )

            result = runner.invoke(
                app,
                [
                    "--config-dir",
                    str(tmp_config_dir),
                    "kai",
                    "preflight",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = _strip_ansi(result.output)
        assert "Kai is NOT ready" in output
        assert "master token" in output  # the reason

    def test_kai_preflight_help(self) -> None:
        """kai preflight --help shows usage information."""
        result = runner.invoke(app, ["kai", "preflight", "--help"])

        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "configured token can use Kai" in output
        assert "--project" in output


class TestKaiChatDetailCli:
    """Tests for `kbagent kai chat-detail` command."""

    def test_kai_chat_detail_json(self, tmp_config_dir: Path) -> None:
        """kai chat-detail --json returns the full transcript as structured JSON."""
        setup_single_project(tmp_config_dir)

        mock_service = MagicMock(spec=KaiService)
        mock_service.resolve_alias.return_value = "prod"
        mock_service.get_chat_detail.return_value = {
            "project_alias": "prod",
            "chat_id": "chat-abc",
            "title": "Test chat",
            "created_at": "2025-02-01T10:00:00+00:00",
            "messages": [
                {
                    "id": "msg-1",
                    "role": "user",
                    "content": "Question?",
                    "created_at": "2025-02-01T10:00:00+00:00",
                },
                {
                    "id": "msg-2",
                    "role": "assistant",
                    "content": "Answer.",
                    "created_at": "2025-02-01T10:00:05+00:00",
                },
            ],
        }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.kai.get_service",
                lambda ctx, name: mock_service,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "--config-dir",
                    str(tmp_config_dir),
                    "kai",
                    "chat-detail",
                    "--chat-id",
                    "chat-abc",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["chat_id"] == "chat-abc"
        assert len(output["data"]["messages"]) == 2
        assert output["data"]["messages"][0]["role"] == "user"
        mock_service.get_chat_detail.assert_called_once_with("prod", "chat-abc")

    def test_kai_chat_detail_human(self, tmp_config_dir: Path) -> None:
        """kai chat-detail in human mode shows title + messages by role."""
        setup_single_project(tmp_config_dir)

        mock_service = MagicMock(spec=KaiService)
        mock_service.resolve_alias.return_value = "prod"
        mock_service.get_chat_detail.return_value = {
            "project_alias": "prod",
            "chat_id": "chat-abc",
            "title": "Pipeline debug",
            "created_at": "2025-02-01T10:00:00+00:00",
            "messages": [
                {"role": "user", "content": "What is broken?", "created_at": None},
                {"role": "assistant", "content": "Run XYZ.", "created_at": None},
            ],
        }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.kai.get_service",
                lambda ctx, name: mock_service,
            )

            result = runner.invoke(
                app,
                [
                    "--config-dir",
                    str(tmp_config_dir),
                    "kai",
                    "chat-detail",
                    "--chat-id",
                    "chat-abc",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = _strip_ansi(result.output)
        assert "Pipeline debug" in output
        assert "What is broken?" in output
        assert "Run XYZ." in output

    def test_kai_chat_detail_empty_messages(self, tmp_config_dir: Path) -> None:
        """kai chat-detail in human mode shows '(no messages)' when chat is empty."""
        setup_single_project(tmp_config_dir)

        mock_service = MagicMock(spec=KaiService)
        mock_service.resolve_alias.return_value = "prod"
        mock_service.get_chat_detail.return_value = {
            "project_alias": "prod",
            "chat_id": "chat-empty",
            "title": None,
            "created_at": None,
            "messages": [],
        }

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.kai.get_service",
                lambda ctx, name: mock_service,
            )

            result = runner.invoke(
                app,
                [
                    "--config-dir",
                    str(tmp_config_dir),
                    "kai",
                    "chat-detail",
                    "--chat-id",
                    "chat-empty",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = _strip_ansi(result.output)
        assert "(no messages)" in output

    def test_kai_chat_detail_help(self) -> None:
        """kai chat-detail --help shows usage information."""
        result = runner.invoke(app, ["kai", "chat-detail", "--help"])

        assert result.exit_code == 0
        output = _strip_ansi(result.output)
        assert "full message history" in output
        assert "--chat-id" in output


class TestKaiConfigError:
    """Tests for ConfigError handling across kai commands."""

    def test_kai_ping_config_error(self, tmp_config_dir: Path) -> None:
        """kai ping with ConfigError returns exit code 5."""
        setup_single_project(tmp_config_dir)

        from keboola_agent_cli.errors import ConfigError

        mock_service = MagicMock(spec=KaiService)
        mock_service.resolve_alias.side_effect = ConfigError("Project 'unknown' not found.")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.kai.get_service",
                lambda ctx, name: mock_service,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "--config-dir",
                    str(tmp_config_dir),
                    "kai",
                    "ping",
                    "--project",
                    "unknown",
                ],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "CONFIG_ERROR" in output["error"]["code"]
