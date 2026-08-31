"""Tests for KaiService — Keboola AI Assistant business logic.

Tests the sync wrapper methods (ping, ask, chat_message, get_history)
with mocked KaiClient and feature-flag detection.
"""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helpers import setup_single_project, setup_two_projects
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import TokenVerifyResponse
from keboola_agent_cli.services.kai_service import KaiService


def _make_kai_service(tmp_config_dir: Path, features: list[str] | None = None):
    """Create a KaiService with a mock client that returns given features."""
    store = setup_single_project(tmp_config_dir)
    mock_client = MagicMock()
    mock_client.verify_token.return_value = TokenVerifyResponse(
        token_id="t-123",
        token_description="test token",
        project_id=258,
        project_name="Production",
        owner_name="Production",
        features=features or [],
    )
    mock_client.close.return_value = None

    service = KaiService(
        config_store=store,
        client_factory=lambda url, token: mock_client,
    )
    return service, mock_client


class TestKaiServicePing:
    """Tests for KaiService.ping()."""

    def test_ping_success(self, tmp_config_dir: Path) -> None:
        """ping returns server health info when Kai is enabled."""
        service, _ = _make_kai_service(tmp_config_dir, features=["agent-chat"])

        mock_ping_resp = MagicMock()
        mock_ping_resp.timestamp = datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC)

        mock_info_resp = MagicMock()
        mock_info_resp.app_name = "kai-api"
        mock_info_resp.app_version = "1.2.3"
        mock_info_resp.server_version = "2.0.0"
        mock_info_resp.connected_mcp = {"status": "connected"}

        mock_kai_client = AsyncMock()
        mock_kai_client.ping.return_value = mock_ping_resp
        mock_kai_client.info.return_value = mock_info_resp
        mock_kai_client.__aenter__ = AsyncMock(return_value=mock_kai_client)
        mock_kai_client.__aexit__ = AsyncMock(return_value=False)

        with patch.object(service, "_create_kai_client", return_value=mock_kai_client):
            result = service.ping("prod")

        assert result["project_alias"] == "prod"
        assert result["timestamp"] == "2025-01-15T10:30:00+00:00"
        assert result["app_name"] == "kai-api"
        assert result["app_version"] == "1.2.3"
        assert result["server_version"] == "2.0.0"
        assert result["mcp_status"] == "connected"

    def test_ping_kai_not_enabled(self, tmp_config_dir: Path) -> None:
        """ping raises KeboolaApiError when agent-chat feature flag is missing."""
        service, _ = _make_kai_service(tmp_config_dir, features=[])

        with pytest.raises(KeboolaApiError) as exc_info:
            service.ping("prod")

        assert exc_info.value.error_code == "KAI_NOT_ENABLED"
        assert "Kai is not enabled" in exc_info.value.message

    def test_ping_mcp_status_unknown(self, tmp_config_dir: Path) -> None:
        """ping returns 'unknown' when connected_mcp is not a dict."""
        service, _ = _make_kai_service(tmp_config_dir, features=["agent-chat"])

        mock_ping_resp = MagicMock()
        mock_ping_resp.timestamp = datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC)

        mock_info_resp = MagicMock()
        mock_info_resp.app_name = "kai-api"
        mock_info_resp.app_version = "1.2.3"
        mock_info_resp.server_version = "2.0.0"
        mock_info_resp.connected_mcp = "not-a-dict"

        mock_kai_client = AsyncMock()
        mock_kai_client.ping.return_value = mock_ping_resp
        mock_kai_client.info.return_value = mock_info_resp
        mock_kai_client.__aenter__ = AsyncMock(return_value=mock_kai_client)
        mock_kai_client.__aexit__ = AsyncMock(return_value=False)

        with patch.object(service, "_create_kai_client", return_value=mock_kai_client):
            result = service.ping("prod")

        assert result["mcp_status"] == "unknown"


class TestKaiServiceAsk:
    """Tests for KaiService.ask()."""

    def test_ask_success(self, tmp_config_dir: Path) -> None:
        """ask returns chat_id and response text."""
        service, _ = _make_kai_service(tmp_config_dir, features=["agent-chat"])

        mock_kai_client = AsyncMock()
        mock_kai_client.chat.return_value = ("chat-abc-123", "The answer is 42.")
        mock_kai_client.__aenter__ = AsyncMock(return_value=mock_kai_client)
        mock_kai_client.__aexit__ = AsyncMock(return_value=False)

        with patch.object(service, "_create_kai_client", return_value=mock_kai_client):
            result = service.ask("prod", "What is the answer?")

        assert result["project_alias"] == "prod"
        assert result["chat_id"] == "chat-abc-123"
        assert result["response"] == "The answer is 42."

    def test_ask_api_error(self, tmp_config_dir: Path) -> None:
        """ask wraps KaiError into KeboolaApiError."""
        from kai_client import KaiError

        service, _ = _make_kai_service(tmp_config_dir, features=["agent-chat"])

        mock_kai_client = AsyncMock()
        mock_kai_client.chat.side_effect = KaiError(message="Service unavailable")
        mock_kai_client.__aenter__ = AsyncMock(return_value=mock_kai_client)
        mock_kai_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.object(service, "_create_kai_client", return_value=mock_kai_client),
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            service.ask("prod", "test question")

        assert exc_info.value.error_code == "KAI_ERROR"
        assert "Kai ask failed" in exc_info.value.message
        assert "Service unavailable" in exc_info.value.message

    def test_ask_kai_not_enabled(self, tmp_config_dir: Path) -> None:
        """ask raises KAI_NOT_ENABLED when feature flag is missing."""
        service, _ = _make_kai_service(tmp_config_dir, features=[])

        with pytest.raises(KeboolaApiError) as exc_info:
            service.ask("prod", "some question")

        assert exc_info.value.error_code == "KAI_NOT_ENABLED"


class TestKaiServiceChat:
    """Tests for KaiService.chat_message()."""

    def test_chat_new_session(self, tmp_config_dir: Path) -> None:
        """chat_message without chat_id creates a new session."""
        service, _ = _make_kai_service(tmp_config_dir, features=["agent-chat"])

        # Create a mock event with type="text" and text attribute
        mock_event = MagicMock()
        mock_event.type = "text"
        mock_event.text = "Hello from Kai!"

        mock_kai_client = AsyncMock()
        # new_chat_id() is called without await, so use MagicMock for it
        mock_kai_client.new_chat_id = MagicMock(return_value="new-chat-id-456")

        # send_message returns an async iterator
        async def mock_send_message(cid, msg):
            yield mock_event

        mock_kai_client.send_message = mock_send_message
        mock_kai_client.__aenter__ = AsyncMock(return_value=mock_kai_client)
        mock_kai_client.__aexit__ = AsyncMock(return_value=False)

        with patch.object(service, "_create_kai_client", return_value=mock_kai_client):
            result = service.chat_message("prod", "Hello!")

        assert result["project_alias"] == "prod"
        assert result["chat_id"] == "new-chat-id-456"
        assert result["response"] == "Hello from Kai!"

    def test_chat_continue(self, tmp_config_dir: Path) -> None:
        """chat_message with chat_id continues an existing session."""
        service, _ = _make_kai_service(tmp_config_dir, features=["agent-chat"])

        mock_event1 = MagicMock()
        mock_event1.type = "text"
        mock_event1.text = "Part one. "

        mock_event2 = MagicMock()
        mock_event2.type = "text"
        mock_event2.text = "Part two."

        # Non-text event should be skipped
        mock_event_other = MagicMock()
        mock_event_other.type = "tool_call"

        mock_kai_client = AsyncMock()

        async def mock_send_message(cid, msg):
            yield mock_event1
            yield mock_event_other
            yield mock_event2

        mock_kai_client.send_message = mock_send_message
        mock_kai_client.__aenter__ = AsyncMock(return_value=mock_kai_client)
        mock_kai_client.__aexit__ = AsyncMock(return_value=False)

        with patch.object(service, "_create_kai_client", return_value=mock_kai_client):
            result = service.chat_message("prod", "Continue please", chat_id="existing-chat-789")

        assert result["chat_id"] == "existing-chat-789"
        assert result["response"] == "Part one. Part two."

    def test_chat_kai_error(self, tmp_config_dir: Path) -> None:
        """chat_message wraps KaiError into KeboolaApiError."""
        from kai_client import KaiError

        service, _ = _make_kai_service(tmp_config_dir, features=["agent-chat"])

        mock_kai_client = AsyncMock()
        mock_kai_client.new_chat_id.return_value = "chat-err"

        async def mock_send_message(cid, msg):
            raise KaiError(message="Chat session expired")
            yield  # needed to make this an async generator

        mock_kai_client.send_message = mock_send_message
        mock_kai_client.__aenter__ = AsyncMock(return_value=mock_kai_client)
        mock_kai_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.object(service, "_create_kai_client", return_value=mock_kai_client),
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            service.chat_message("prod", "test")

        assert exc_info.value.error_code == "KAI_ERROR"
        assert "Kai chat failed" in exc_info.value.message


class TestKaiServicePreflight:
    """Tests for KaiService.preflight() — non-raising token readiness check."""

    def test_preflight_ok(self, tmp_config_dir: Path) -> None:
        """preflight returns ok=True when token is master + has agent-chat feature."""
        service, mock_client = _make_kai_service(tmp_config_dir)
        mock_client.get_project_info.return_value = {
            "isMasterToken": True,
            "description": "owner-token",
            "owner": {
                "id": 258,
                "name": "Production",
                "features": ["agent-chat", "some-other-flag"],
            },
        }

        result = service.preflight("prod")

        assert result["project_alias"] == "prod"
        assert result["ok"] is True
        assert result["is_master_token"] is True
        assert result["has_agent_chat_feature"] is True
        assert result["token_description"] == "owner-token"
        assert result["project_id"] == 258
        assert result["project_name"] == "Production"
        assert result["error"] is None
        mock_client.close.assert_called_once()

    def test_preflight_not_master_token(self, tmp_config_dir: Path) -> None:
        """preflight returns ok=False when token is a custom (non-master) token."""
        service, mock_client = _make_kai_service(tmp_config_dir)
        mock_client.get_project_info.return_value = {
            "isMasterToken": False,
            "description": "custom-token",
            "owner": {
                "id": 258,
                "name": "Production",
                "features": ["agent-chat"],
            },
        }

        result = service.preflight("prod")

        assert result["ok"] is False
        assert result["is_master_token"] is False
        assert result["has_agent_chat_feature"] is True
        assert "not the project's master" in result["error"]

    def test_preflight_missing_feature(self, tmp_config_dir: Path) -> None:
        """preflight returns ok=False when the agent-chat feature flag is missing."""
        service, mock_client = _make_kai_service(tmp_config_dir)
        mock_client.get_project_info.return_value = {
            "isMasterToken": True,
            "description": "owner-token",
            "owner": {
                "id": 258,
                "name": "Production",
                "features": [],
            },
        }

        result = service.preflight("prod")

        assert result["ok"] is False
        assert result["is_master_token"] is True
        assert result["has_agent_chat_feature"] is False
        assert "AI Agent Chat" in result["error"]

    def test_preflight_api_error_is_caught(self, tmp_config_dir: Path) -> None:
        """preflight returns gracefully with error field set when verify fails."""
        service, mock_client = _make_kai_service(tmp_config_dir)
        mock_client.get_project_info.side_effect = KeboolaApiError(
            message="Invalid token", status_code=401, error_code="AUTH_ERROR"
        )

        result = service.preflight("prod")

        # preflight NEVER raises — UI relies on this for friendly warnings
        assert result["ok"] is False
        assert result["error"] == "Invalid token"
        assert result["is_master_token"] is False
        mock_client.close.assert_called_once()


class TestKaiServiceGetChatDetail:
    """Tests for KaiService.get_chat_detail() — full chat transcript."""

    def test_get_chat_detail_success(self, tmp_config_dir: Path) -> None:
        """get_chat_detail returns a flat message list and chat metadata."""
        service, _ = _make_kai_service(tmp_config_dir, features=["agent-chat"])

        mock_msg_user = MagicMock()
        mock_msg_user.id = "msg-1"
        mock_msg_user.role = "user"
        mock_msg_user.parts = [{"type": "text", "text": "What is the answer?"}]
        mock_msg_user.created_at = datetime(2025, 2, 1, 10, 0, 0, tzinfo=UTC)

        mock_msg_assistant = MagicMock()
        mock_msg_assistant.id = "msg-2"
        mock_msg_assistant.role = "assistant"
        # Multi-part text + a tool_call part that must be skipped
        mock_msg_assistant.parts = [
            {"type": "text", "text": "The answer "},
            {"type": "tool_call", "name": "search"},
            {"type": "text", "text": "is 42."},
        ]
        mock_msg_assistant.created_at = datetime(2025, 2, 1, 10, 0, 5, tzinfo=UTC)

        mock_chat_detail = MagicMock()
        mock_chat_detail.id = "chat-abc"
        mock_chat_detail.title = "Test chat"
        mock_chat_detail.created_at = datetime(2025, 2, 1, 10, 0, 0, tzinfo=UTC)
        mock_chat_detail.messages = [mock_msg_user, mock_msg_assistant]

        mock_kai_client = AsyncMock()
        mock_kai_client.get_chat.return_value = mock_chat_detail
        mock_kai_client.__aenter__ = AsyncMock(return_value=mock_kai_client)
        mock_kai_client.__aexit__ = AsyncMock(return_value=False)

        with patch.object(service, "_create_kai_client", return_value=mock_kai_client):
            result = service.get_chat_detail("prod", "chat-abc")

        assert result["project_alias"] == "prod"
        assert result["chat_id"] == "chat-abc"
        assert result["title"] == "Test chat"
        assert result["created_at"] == "2025-02-01T10:00:00+00:00"
        assert len(result["messages"]) == 2
        assert result["messages"][0]["role"] == "user"
        assert result["messages"][0]["content"] == "What is the answer?"
        # tool_call part skipped, text parts joined into single content
        assert result["messages"][1]["role"] == "assistant"
        assert result["messages"][1]["content"] == "The answer is 42."

    def test_get_chat_detail_kai_not_enabled(self, tmp_config_dir: Path) -> None:
        """get_chat_detail raises KAI_NOT_ENABLED when feature flag is missing."""
        service, _ = _make_kai_service(tmp_config_dir, features=[])

        with pytest.raises(KeboolaApiError) as exc_info:
            service.get_chat_detail("prod", "chat-abc")

        assert exc_info.value.error_code == "KAI_NOT_ENABLED"

    def test_get_chat_detail_kai_error(self, tmp_config_dir: Path) -> None:
        """get_chat_detail wraps KaiError into KeboolaApiError(KAI_ERROR)."""
        from kai_client import KaiError

        service, _ = _make_kai_service(tmp_config_dir, features=["agent-chat"])

        mock_kai_client = AsyncMock()
        mock_kai_client.get_chat.side_effect = KaiError(message="Chat not found")
        mock_kai_client.__aenter__ = AsyncMock(return_value=mock_kai_client)
        mock_kai_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.object(service, "_create_kai_client", return_value=mock_kai_client),
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            service.get_chat_detail("prod", "missing")

        assert exc_info.value.error_code == "KAI_ERROR"
        assert "Chat not found" in exc_info.value.message

    def test_get_chat_detail_skips_empty_user_message(self, tmp_config_dir: Path) -> None:
        """Legacy user messages with no text parts are skipped, not surfaced as empty."""
        service, _ = _make_kai_service(tmp_config_dir, features=["agent-chat"])

        empty_user = MagicMock()
        empty_user.role = "user"
        # No text parts — only a tool_call shape (legacy chat where text lived elsewhere)
        empty_user.parts = [{"type": "tool_call", "name": "search"}]

        normal_user = MagicMock()
        normal_user.id = "msg-2"
        normal_user.role = "user"
        normal_user.parts = [{"type": "text", "text": "hi"}]
        normal_user.created_at = None

        mock_chat = MagicMock()
        mock_chat.id = "c"
        mock_chat.title = None
        mock_chat.created_at = None
        mock_chat.messages = [empty_user, normal_user]

        mock_kai_client = AsyncMock()
        mock_kai_client.get_chat.return_value = mock_chat
        mock_kai_client.__aenter__ = AsyncMock(return_value=mock_kai_client)
        mock_kai_client.__aexit__ = AsyncMock(return_value=False)

        with patch.object(service, "_create_kai_client", return_value=mock_kai_client):
            result = service.get_chat_detail("prod", "c")

        # Empty-content user message dropped; normal one survives
        assert len(result["messages"]) == 1
        assert result["messages"][0]["content"] == "hi"


class TestKaiServiceHistory:
    """Tests for KaiService.get_history()."""

    def test_history_success(self, tmp_config_dir: Path) -> None:
        """get_history returns a list of chat summaries."""
        service, _ = _make_kai_service(tmp_config_dir, features=["agent-chat"])

        chat1 = MagicMock()
        chat1.id = "chat-aaa"
        chat1.title = "First chat"
        chat1.created_at = datetime(2025, 1, 10, 8, 0, 0, tzinfo=UTC)
        chat1.visibility = "private"

        chat2 = MagicMock()
        chat2.id = "chat-bbb"
        chat2.title = None  # untitled
        chat2.created_at = None
        chat2.visibility = "public"

        mock_history = MagicMock()
        mock_history.chats = [chat1, chat2]
        mock_history.has_more = True

        mock_kai_client = AsyncMock()
        mock_kai_client.get_history.return_value = mock_history
        mock_kai_client.__aenter__ = AsyncMock(return_value=mock_kai_client)
        mock_kai_client.__aexit__ = AsyncMock(return_value=False)

        with patch.object(service, "_create_kai_client", return_value=mock_kai_client):
            result = service.get_history("prod", limit=5)

        assert result["project_alias"] == "prod"
        assert result["has_more"] is True
        assert len(result["chats"]) == 2

        assert result["chats"][0]["id"] == "chat-aaa"
        assert result["chats"][0]["title"] == "First chat"
        assert result["chats"][0]["created_at"] == "2025-01-10T08:00:00+00:00"
        assert result["chats"][0]["visibility"] == "private"

        assert result["chats"][1]["id"] == "chat-bbb"
        assert result["chats"][1]["title"] == "(untitled)"
        assert result["chats"][1]["created_at"] is None
        assert result["chats"][1]["visibility"] == "public"

    def test_history_empty(self, tmp_config_dir: Path) -> None:
        """get_history returns empty list when no chats exist."""
        service, _ = _make_kai_service(tmp_config_dir, features=["agent-chat"])

        mock_history = MagicMock()
        mock_history.chats = []
        mock_history.has_more = False

        mock_kai_client = AsyncMock()
        mock_kai_client.get_history.return_value = mock_history
        mock_kai_client.__aenter__ = AsyncMock(return_value=mock_kai_client)
        mock_kai_client.__aexit__ = AsyncMock(return_value=False)

        with patch.object(service, "_create_kai_client", return_value=mock_kai_client):
            result = service.get_history("prod")

        assert result["chats"] == []
        assert result["has_more"] is False

    def test_history_kai_not_enabled(self, tmp_config_dir: Path) -> None:
        """get_history raises KAI_NOT_ENABLED when feature flag is missing."""
        service, _ = _make_kai_service(tmp_config_dir, features=[])

        with pytest.raises(KeboolaApiError) as exc_info:
            service.get_history("prod")

        assert exc_info.value.error_code == "KAI_NOT_ENABLED"


class TestKaiServiceResolveAlias:
    """Tests for KaiService.resolve_alias()."""

    def test_resolve_explicit_alias(self, tmp_config_dir: Path) -> None:
        """resolve_alias with explicit alias validates and returns it."""
        service, _ = _make_kai_service(tmp_config_dir)
        assert service.resolve_alias("prod") == "prod"

    def test_resolve_default_alias(self, tmp_config_dir: Path) -> None:
        """resolve_alias with None returns the sole configured project."""
        service, _ = _make_kai_service(tmp_config_dir)
        assert service.resolve_alias(None) == "prod"

    def test_resolve_default_honors_pin(self, tmp_config_dir: Path, monkeypatch) -> None:
        """resolve_alias(None) returns the pin, not the first project.

        Regression test for issue #684: with two projects and the pin moved
        to the second one, `kai ping/preflight/ask/chat` without --project
        acted on the first registered project.
        """
        monkeypatch.delenv("KBAGENT_PROJECT", raising=False)
        store = setup_two_projects(tmp_config_dir)
        # add_project() pinned "prod" (first added); repoint like `project use dev`.
        cfg = store.load()
        cfg.default_project = "dev"
        store.save(cfg)

        service = KaiService(config_store=store, client_factory=lambda url, token: MagicMock())
        assert service.resolve_alias(None) == "dev"

    def test_resolve_unknown_alias(self, tmp_config_dir: Path) -> None:
        """resolve_alias raises ConfigError for unknown alias."""
        service, _ = _make_kai_service(tmp_config_dir)
        with pytest.raises(ConfigError):
            service.resolve_alias("nonexistent")
