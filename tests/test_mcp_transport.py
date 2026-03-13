"""Tests for McpServerManager - persistent MCP HTTP daemon manager."""

import os
import socket
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from keboola_agent_cli.services.mcp_transport import (
    McpServerManager,
    _find_free_port,
    _is_idle_expired,
    _process_alive,
    _tcp_reachable,
    _touch_last_used,
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
        assert len(ports) >= 2


class TestProcessAlive:
    """Tests for _process_alive()."""

    def test_current_process_is_alive(self) -> None:
        """Current process PID should be alive."""
        assert _process_alive(os.getpid()) is True

    def test_nonexistent_pid_is_not_alive(self) -> None:
        """PID 0 or very large PID should not be alive."""
        assert _process_alive(99999999) is False


class TestTcpReachable:
    """Tests for _tcp_reachable()."""

    def test_closed_port_not_reachable(self) -> None:
        """A port with no listener should return False."""
        port = _find_free_port()
        assert _tcp_reachable(port) is False

    def test_open_port_reachable(self) -> None:
        """A port with a listener should return True."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            assert _tcp_reachable(port) is True
        finally:
            srv.close()


class TestMcpServerManager:
    """Tests for McpServerManager daemon lifecycle."""

    def test_initial_state_no_pid_files(self, tmp_path: Path) -> None:
        """When no PID/port files exist, manager reports not running."""
        with (
            patch("keboola_agent_cli.services.mcp_transport._PID_FILE", tmp_path / "mcp.pid"),
            patch("keboola_agent_cli.services.mcp_transport._PORT_FILE", tmp_path / "mcp.port"),
        ):
            manager = McpServerManager()
            assert manager.is_running is False
            assert manager.port is None
            assert manager.base_url is None

    def test_get_status_not_running(self, tmp_path: Path) -> None:
        """Status when no daemon is running."""
        with (
            patch("keboola_agent_cli.services.mcp_transport._PID_FILE", tmp_path / "mcp.pid"),
            patch("keboola_agent_cli.services.mcp_transport._PORT_FILE", tmp_path / "mcp.port"),
            patch("keboola_agent_cli.services.mcp_transport._LAST_USED_FILE", tmp_path / "last-used"),
        ):
            manager = McpServerManager()
            status = manager.get_status()
            assert status["running"] is False
            assert status["port"] is None
            assert status["base_url"] is None
            assert status["pid"] is None
            assert status["idle_seconds"] is None

    def test_get_status_running_with_idle(self, tmp_path: Path) -> None:
        """Status includes idle_seconds when daemon is running."""
        pid_file = tmp_path / "mcp.pid"
        port_file = tmp_path / "mcp.port"
        last_used_file = tmp_path / "last-used"
        pid_file.write_text("99999")
        port_file.write_text("12345")
        last_used_file.write_text(str(time.time() - 120))  # 2 minutes ago
        with (
            patch("keboola_agent_cli.services.mcp_transport._PID_FILE", pid_file),
            patch("keboola_agent_cli.services.mcp_transport._PORT_FILE", port_file),
            patch("keboola_agent_cli.services.mcp_transport._LAST_USED_FILE", last_used_file),
            patch("keboola_agent_cli.services.mcp_transport._process_alive", return_value=True),
            patch("keboola_agent_cli.services.mcp_transport._tcp_reachable", return_value=True),
        ):
            manager = McpServerManager()
            status = manager.get_status()
            assert status["running"] is True
            assert status["pid"] == 99999
            assert status["port"] == 12345
            assert status["idle_seconds"] is not None
            assert 115 <= status["idle_seconds"] <= 125

    @patch("keboola_agent_cli.services.mcp_transport.detect_mcp_server_command")
    def test_start_no_command_raises(self, mock_detect: MagicMock, tmp_path: Path) -> None:
        """If no MCP server command is found, RuntimeError is raised."""
        mock_detect.return_value = None
        with (
            patch("keboola_agent_cli.services.mcp_transport._PID_FILE", tmp_path / "mcp.pid"),
            patch("keboola_agent_cli.services.mcp_transport._PORT_FILE", tmp_path / "mcp.port"),
            patch("keboola_agent_cli.services.mcp_transport._CACHE_DIR", tmp_path),
        ):
            manager = McpServerManager()
            with pytest.raises(RuntimeError, match="Cannot find keboola-mcp-server"):
                manager.ensure_running()

    @patch("keboola_agent_cli.services.mcp_transport.detect_mcp_server_command")
    def test_start_server_fails_to_respond(self, mock_detect: MagicMock, tmp_path: Path) -> None:
        """If server process starts but never becomes healthy, RuntimeError is raised."""
        mock_detect.return_value = ["echo", "test"]
        with (
            patch("keboola_agent_cli.services.mcp_transport._PID_FILE", tmp_path / "mcp.pid"),
            patch("keboola_agent_cli.services.mcp_transport._PORT_FILE", tmp_path / "mcp.port"),
            patch("keboola_agent_cli.services.mcp_transport._CACHE_DIR", tmp_path),
        ):
            manager = McpServerManager()
            with (
                patch.object(manager, "_wait_for_ready", return_value=False),
                patch("subprocess.Popen") as mock_popen,
            ):
                mock_process = MagicMock()
                mock_process.poll.return_value = None
                mock_process.pid = 12345
                mock_popen.return_value = mock_process

                with pytest.raises(RuntimeError, match="MCP daemon failed to start"):
                    manager.ensure_running()

    @patch("keboola_agent_cli.services.mcp_transport.detect_mcp_server_command")
    def test_start_and_stop(self, mock_detect: MagicMock, tmp_path: Path) -> None:
        """Starting the daemon writes state; stopping clears it."""
        mock_detect.return_value = ["echo", "test"]
        pid_file = tmp_path / "mcp.pid"
        port_file = tmp_path / "mcp.port"
        with (
            patch("keboola_agent_cli.services.mcp_transport._PID_FILE", pid_file),
            patch("keboola_agent_cli.services.mcp_transport._PORT_FILE", port_file),
            patch("keboola_agent_cli.services.mcp_transport._CACHE_DIR", tmp_path),
        ):
            manager = McpServerManager()
            with (
                patch.object(manager, "_wait_for_ready", return_value=True),
                patch("subprocess.Popen") as mock_popen,
                patch("keboola_agent_cli.services.mcp_transport._process_alive", return_value=True),
                patch("keboola_agent_cli.services.mcp_transport._tcp_reachable", return_value=True),
            ):
                mock_process = MagicMock()
                mock_process.pid = 12345
                mock_popen.return_value = mock_process

                url = manager.ensure_running()
                assert url.startswith("http://127.0.0.1:")
                assert pid_file.read_text() == "12345"

            # Now stop — patch process alive to True so it tries to kill
            with patch("keboola_agent_cli.services.mcp_transport._process_alive", return_value=False):
                manager.stop()
                assert not pid_file.exists()
                assert not port_file.exists()

    @patch("keboola_agent_cli.services.mcp_transport.detect_mcp_server_command")
    def test_ensure_running_reuses_existing_daemon(
        self, mock_detect: MagicMock, tmp_path: Path
    ) -> None:
        """If daemon is already running (valid PID+port), reuse without spawning."""
        mock_detect.return_value = ["echo", "test"]
        pid_file = tmp_path / "mcp.pid"
        port_file = tmp_path / "mcp.port"
        pid_file.write_text("99999")
        port_file.write_text("12345")
        with (
            patch("keboola_agent_cli.services.mcp_transport._PID_FILE", pid_file),
            patch("keboola_agent_cli.services.mcp_transport._PORT_FILE", port_file),
            patch("keboola_agent_cli.services.mcp_transport._process_alive", return_value=True),
            patch("keboola_agent_cli.services.mcp_transport._tcp_reachable", return_value=True),
            patch("subprocess.Popen") as mock_popen,
        ):
            manager = McpServerManager()
            url = manager.ensure_running()
            assert url == "http://127.0.0.1:12345"
            # Popen should NOT be called — reusing existing daemon
            mock_popen.assert_not_called()

    def test_stop_when_not_running(self, tmp_path: Path) -> None:
        """Stopping when no daemon is running is a no-op."""
        with (
            patch("keboola_agent_cli.services.mcp_transport._PID_FILE", tmp_path / "mcp.pid"),
            patch("keboola_agent_cli.services.mcp_transport._PORT_FILE", tmp_path / "mcp.port"),
        ):
            manager = McpServerManager()
            manager.stop()  # Should not raise


class TestIdleTimeout:
    """Tests for idle timeout logic."""

    def test_no_last_used_file_not_expired(self, tmp_path: Path) -> None:
        """When no last-used file exists, daemon is not considered expired."""
        with patch("keboola_agent_cli.services.mcp_transport._LAST_USED_FILE", tmp_path / "last-used"):
            assert _is_idle_expired() is False

    def test_recent_last_used_not_expired(self, tmp_path: Path) -> None:
        """When last-used is recent, daemon is not expired."""
        f = tmp_path / "last-used"
        f.write_text(str(time.time() - 60))  # 1 minute ago
        with patch("keboola_agent_cli.services.mcp_transport._LAST_USED_FILE", f):
            assert _is_idle_expired() is False

    def test_old_last_used_expired(self, tmp_path: Path) -> None:
        """When last-used is over 30 min ago, daemon is expired."""
        f = tmp_path / "last-used"
        f.write_text(str(time.time() - 31 * 60))  # 31 minutes ago
        with patch("keboola_agent_cli.services.mcp_transport._LAST_USED_FILE", f):
            assert _is_idle_expired() is True

    def test_touch_last_used_writes_timestamp(self, tmp_path: Path) -> None:
        """touch_last_used() writes a recent timestamp."""
        f = tmp_path / "last-used"
        with (
            patch("keboola_agent_cli.services.mcp_transport._LAST_USED_FILE", f),
            patch("keboola_agent_cli.services.mcp_transport._CACHE_DIR", tmp_path),
        ):
            before = time.time()
            _touch_last_used()
            after = time.time()
            written = float(f.read_text())
            assert before <= written <= after

    def test_ensure_running_stops_idle_daemon(self, tmp_path: Path) -> None:
        """ensure_running() stops and restarts daemon when idle timeout expired."""
        pid_file = tmp_path / "mcp.pid"
        port_file = tmp_path / "mcp.port"
        last_used_file = tmp_path / "last-used"
        pid_file.write_text("99999")
        port_file.write_text("12345")
        last_used_file.write_text(str(time.time() - 31 * 60))  # expired

        with (
            patch("keboola_agent_cli.services.mcp_transport._PID_FILE", pid_file),
            patch("keboola_agent_cli.services.mcp_transport._PORT_FILE", port_file),
            patch("keboola_agent_cli.services.mcp_transport._LAST_USED_FILE", last_used_file),
            patch("keboola_agent_cli.services.mcp_transport._CACHE_DIR", tmp_path),
            patch("keboola_agent_cli.services.mcp_transport._process_alive", return_value=True),
            patch("keboola_agent_cli.services.mcp_transport._tcp_reachable", return_value=True),
            patch("keboola_agent_cli.services.mcp_transport.detect_mcp_server_command", return_value=["echo"]),
            patch("subprocess.Popen") as mock_popen,
        ):
            mock_process = MagicMock()
            mock_process.pid = 11111
            mock_popen.return_value = mock_process
            manager = McpServerManager()
            with patch.object(manager, "_wait_for_ready", return_value=True):
                url = manager.ensure_running()
            # New daemon started (not old port 12345)
            assert "11111" in pid_file.read_text() or mock_popen.called


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

    def test_get_transport_mode_auto_http_when_binary_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When env var unset and binary available, auto-selects 'http'."""
        monkeypatch.delenv("KBAGENT_MCP_TRANSPORT", raising=False)
        import shutil
        import importlib
        import keboola_agent_cli.services.mcp_service as svc_module

        original_which = shutil.which

        def fake_which(name: str) -> str | None:
            if name == "keboola_mcp_server":
                return "/usr/local/bin/keboola_mcp_server"
            return original_which(name)

        with patch("keboola_agent_cli.services.mcp_service.shutil.which", side_effect=fake_which):
            importlib.reload(svc_module)
            assert svc_module._get_transport_mode() == "http"

    def test_get_transport_mode_stdio_when_no_binary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When env var unset and no binary, defaults to 'stdio'."""
        monkeypatch.delenv("KBAGENT_MCP_TRANSPORT", raising=False)
        import keboola_agent_cli.services.mcp_service as svc_module

        with patch("keboola_agent_cli.services.mcp_service.shutil.which", return_value=None):
            assert svc_module._get_transport_mode() == "stdio"

    def test_get_transport_mode_explicit_stdio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """KBAGENT_MCP_TRANSPORT=stdio always returns 'stdio'."""
        monkeypatch.setenv("KBAGENT_MCP_TRANSPORT", "stdio")
        from keboola_agent_cli.services.mcp_service import _get_transport_mode
        assert _get_transport_mode() == "stdio"

    def test_get_transport_mode_explicit_http(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """KBAGENT_MCP_TRANSPORT=http always returns 'http'."""
        monkeypatch.setenv("KBAGENT_MCP_TRANSPORT", "http")
        from keboola_agent_cli.services.mcp_service import _get_transport_mode
        assert _get_transport_mode() == "http"

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


class TestMcpServiceTransportSelection:
    """Tests for McpService._get_server_url() transport selection."""

    def test_stdio_mode_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In stdio mode, _get_server_url() returns None."""
        monkeypatch.setenv("KBAGENT_MCP_TRANSPORT", "stdio")
        from keboola_agent_cli.config_store import ConfigStore
        from keboola_agent_cli.services.mcp_service import McpService

        store = ConfigStore(config_dir=tmp_path / "config")
        svc = McpService(config_store=store)
        assert svc._get_server_url() is None

    def test_http_mode_with_failed_server_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In HTTP mode, if daemon fails to start, returns None (fallback to stdio)."""
        monkeypatch.setenv("KBAGENT_MCP_TRANSPORT", "http")
        from keboola_agent_cli.config_store import ConfigStore
        from keboola_agent_cli.services.mcp_service import McpService

        store = ConfigStore(config_dir=tmp_path / "config")
        svc = McpService(config_store=store)

        mock_manager = MagicMock()
        mock_manager.ensure_running.side_effect = RuntimeError("No server")

        with patch(
            "keboola_agent_cli.services.mcp_transport.get_server_manager",
            return_value=mock_manager,
        ):
            assert svc._get_server_url() is None
