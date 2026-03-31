"""Tests for McpServerManager - persistent MCP HTTP server manager."""

import socket
from unittest.mock import MagicMock, patch

import pytest

from keboola_agent_cli.services.mcp_transport import (
    McpServerManager,
    _find_free_port,
    get_server_manager,
)


class TestFindFreePort:
    """Tests for _find_free_port()."""

    def test_returns_valid_port(self) -> None:
        """Should return a port number in valid range."""
        port = _find_free_port()
        assert isinstance(port, int)
        assert 1024 <= port <= 65535

    def test_returns_different_ports(self) -> None:
        """Consecutive calls should return different ports (usually)."""
        ports = {_find_free_port() for _ in range(5)}
        # At least 2 different ports out of 5 calls
        assert len(ports) >= 2


class TestMcpServerManager:
    """Tests for McpServerManager lifecycle."""

    def test_initial_state(self) -> None:
        """New manager starts with no server running."""
        manager = McpServerManager()
        assert manager.is_running is False
        assert manager.port is None
        assert manager.base_url is None

    def test_get_status_not_running(self) -> None:
        """Status when server is not running."""
        manager = McpServerManager()
        status = manager.get_status()
        assert status["running"] is False
        assert status["port"] is None
        assert status["base_url"] is None
        assert status["pid"] is None

    @patch("keboola_agent_cli.services.mcp_transport.detect_mcp_server_command")
    def test_start_no_command_raises(self, mock_detect: MagicMock) -> None:
        """If no MCP server command is found, RuntimeError is raised."""
        mock_detect.return_value = None
        manager = McpServerManager()

        with pytest.raises(RuntimeError, match="Cannot find keboola-mcp-server"):
            manager.ensure_running()

    @patch("keboola_agent_cli.services.mcp_transport.detect_mcp_server_command")
    def test_start_server_fails_to_respond(self, mock_detect: MagicMock) -> None:
        """If server process starts but never becomes healthy, RuntimeError is raised."""
        mock_detect.return_value = ["echo", "test"]

        manager = McpServerManager()

        # Mock _wait_for_ready to always return False (timeout)
        with (
            patch.object(manager, "_wait_for_ready", return_value=False),
            patch("subprocess.Popen") as mock_popen,
        ):
            mock_process = MagicMock()
            mock_process.poll.return_value = None
            mock_process.stderr = MagicMock()
            mock_process.stderr.read1.return_value = b"some error"
            mock_process.pid = 12345
            mock_popen.return_value = mock_process

            with pytest.raises(RuntimeError, match="MCP server failed to start"):
                manager.ensure_running()

    @patch("keboola_agent_cli.services.mcp_transport.detect_mcp_server_command")
    def test_start_and_stop(self, mock_detect: MagicMock) -> None:
        """Starting and stopping the server cleans up state."""
        mock_detect.return_value = ["echo", "test"]

        manager = McpServerManager()

        with (
            patch.object(manager, "_wait_for_ready", return_value=True),
            patch("subprocess.Popen") as mock_popen,
        ):
            mock_process = MagicMock()
            mock_process.poll.return_value = None
            mock_process.pid = 12345
            mock_popen.return_value = mock_process

            url = manager.ensure_running()
            assert url.startswith("http://127.0.0.1:")
            assert manager.is_running is True
            assert manager.port is not None

            manager.stop()
            mock_process.terminate.assert_called_once()
            assert manager.is_running is False
            assert manager.port is None
            assert manager.base_url is None

    @patch("keboola_agent_cli.services.mcp_transport.detect_mcp_server_command")
    def test_ensure_running_reuses_existing(self, mock_detect: MagicMock) -> None:
        """If server is already running and healthy, reuse it."""
        mock_detect.return_value = ["echo", "test"]

        manager = McpServerManager()

        with (
            patch.object(manager, "_wait_for_ready", return_value=True),
            patch("subprocess.Popen") as mock_popen,
        ):
            mock_process = MagicMock()
            mock_process.poll.return_value = None
            mock_process.pid = 12345
            mock_popen.return_value = mock_process

            url1 = manager.ensure_running()

            with patch.object(manager, "_health_check", return_value=True):
                url2 = manager.ensure_running()

            assert url1 == url2
            # Popen should only be called once (reuse)
            assert mock_popen.call_count == 1

        manager.stop()

    def test_health_check_no_port(self) -> None:
        """Health check with no port returns False."""
        manager = McpServerManager()
        assert manager._health_check() is False

    def test_health_check_connection_refused(self) -> None:
        """Health check to closed port returns False."""
        manager = McpServerManager()
        manager._port = _find_free_port()
        assert manager._health_check() is False

    def test_health_check_real_server(self) -> None:
        """Health check to a real listening socket returns True."""
        manager = McpServerManager()

        # Start a temporary TCP server
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        try:
            manager._port = port
            assert manager._health_check() is True
        finally:
            srv.close()

    def test_stop_when_not_running(self) -> None:
        """Stopping when no server is running is a no-op."""
        manager = McpServerManager()
        manager.stop()  # Should not raise


class TestGetServerManager:
    """Tests for the module-level singleton."""

    def test_returns_same_instance(self) -> None:
        """get_server_manager() returns the same instance."""
        m1 = get_server_manager()
        m2 = get_server_manager()
        assert m1 is m2

    def test_returns_mcpservermanager_instance(self) -> None:
        """get_server_manager() returns an McpServerManager."""
        m = get_server_manager()
        assert isinstance(m, McpServerManager)


class TestHttpTransportFunctions:
    """Tests for HTTP transport helper functions in mcp_service."""

    def test_get_transport_mode_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default transport mode is 'stdio'."""
        monkeypatch.delenv("KBAGENT_MCP_TRANSPORT", raising=False)
        from keboola_agent_cli.services.mcp_service import _get_transport_mode

        assert _get_transport_mode() == "stdio"

    def test_get_transport_mode_stdio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """KBAGENT_MCP_TRANSPORT=stdio returns 'stdio'."""
        monkeypatch.setenv("KBAGENT_MCP_TRANSPORT", "stdio")
        from keboola_agent_cli.services.mcp_service import _get_transport_mode

        assert _get_transport_mode() == "stdio"

    def test_build_http_headers(self) -> None:
        """Headers include token and stack URL."""
        from keboola_agent_cli.models import ProjectConfig
        from keboola_agent_cli.services.mcp_service import _build_http_headers

        project = ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="test-token",
        )
        headers = _build_http_headers(project)
        assert headers["X-Storage-Token"] == "test-token"
        assert headers["X-Storage-API-URL"] == "https://connection.keboola.com"
        assert "X-Branch-ID" not in headers

    def test_build_http_headers_with_branch(self) -> None:
        """Headers include branch ID when provided."""
        from keboola_agent_cli.models import ProjectConfig
        from keboola_agent_cli.services.mcp_service import _build_http_headers

        project = ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="test-token",
        )
        headers = _build_http_headers(project, branch_id="123")
        assert headers["X-Branch-ID"] == "123"

    def test_build_http_headers_with_conversation_id(self, monkeypatch) -> None:
        """Headers include X-Conversation-ID when env var is set."""
        from keboola_agent_cli.models import ProjectConfig
        from keboola_agent_cli.services.mcp_service import _build_http_headers

        monkeypatch.setenv("KBAGENT_CONVERSATION_ID", "conv-xyz-789")
        project = ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="test-token",
        )
        headers = _build_http_headers(project)
        assert headers["X-Conversation-ID"] == "conv-xyz-789"

    def test_build_http_headers_no_conversation_id_when_unset(self, monkeypatch) -> None:
        """Headers omit X-Conversation-ID when env var is not set."""
        from keboola_agent_cli.models import ProjectConfig
        from keboola_agent_cli.services.mcp_service import _build_http_headers

        monkeypatch.delenv("KBAGENT_CONVERSATION_ID", raising=False)
        project = ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="test-token",
        )
        headers = _build_http_headers(project)
        assert "X-Conversation-ID" not in headers


class TestMcpServiceTransportSelection:
    """Tests for McpService._get_server_url() transport selection."""

    def test_stdio_mode_returns_none(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        """In stdio mode, _get_server_url() returns None."""
        monkeypatch.setenv("KBAGENT_MCP_TRANSPORT", "stdio")
        from keboola_agent_cli.config_store import ConfigStore
        from keboola_agent_cli.services.mcp_service import McpService

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)
        svc = McpService(config_store=store)

        assert svc._get_server_url() is None

    def test_http_mode_with_failed_server_returns_none(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In HTTP mode, if server fails to start, returns None (fallback to stdio)."""
        monkeypatch.setenv("KBAGENT_MCP_TRANSPORT", "http")
        from keboola_agent_cli.config_store import ConfigStore
        from keboola_agent_cli.services.mcp_service import McpService

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)
        svc = McpService(config_store=store)

        mock_manager = MagicMock()
        mock_manager.ensure_running.side_effect = RuntimeError("No server")

        with patch(
            "keboola_agent_cli.services.mcp_transport.get_server_manager",
            return_value=mock_manager,
        ):
            assert svc._get_server_url() is None
