"""Persistent MCP HTTP server daemon manager.

Manages a single keboola-mcp-server process running in streamable-http mode.
The server runs as a daemon — it survives between CLI invocations.

State is persisted to ~/.cache/kbagent/:
  mcp-server.pid        — PID of the daemon process
  mcp-server.port       — TCP port the server listens on
  mcp-server.last-used  — Unix timestamp of last tool call (for idle timeout)

The daemon auto-terminates after 30 minutes of inactivity. The last-used
timestamp is updated on every ensure_running() call and checked on the next
invocation.

Per-request project credentials are passed via HTTP headers:
- X-Storage-Token: storage API token
- X-Storage-API-URL: stack URL
- X-Branch-ID: optional branch ID

One server serves ALL projects — just different headers per request.
"""

import logging
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from ..constants import (
    MCP_SERVER_HEALTH_TIMEOUT,
    MCP_SERVER_STARTUP_TIMEOUT,
)
from .mcp_service import detect_mcp_server_command

logger = logging.getLogger(__name__)

_CACHE_DIR = Path("~/.cache/kbagent").expanduser()
_PID_FILE = _CACHE_DIR / "mcp-server.pid"
_PORT_FILE = _CACHE_DIR / "mcp-server.port"
_LAST_USED_FILE = _CACHE_DIR / "mcp-server.last-used"

# Daemon auto-terminates after this many seconds of inactivity.
_IDLE_TIMEOUT: float = 30 * 60  # 30 minutes


def _find_free_port() -> int:
    """Find a free TCP port by binding to port 0 and reading the assigned port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _process_alive(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _tcp_reachable(port: int) -> bool:
    """Check if a TCP port is accepting connections."""
    try:
        with socket.create_connection(
            ("127.0.0.1", port),
            timeout=MCP_SERVER_HEALTH_TIMEOUT,
        ):
            return True
    except (OSError, ConnectionRefusedError):
        return False


def _read_daemon_state() -> tuple[int, int] | None:
    """Read PID and port from state files.

    Returns:
        (pid, port) tuple, or None if state files are missing/invalid.
    """
    try:
        pid = int(_PID_FILE.read_text().strip())
        port = int(_PORT_FILE.read_text().strip())
        return pid, port
    except (FileNotFoundError, ValueError):
        return None


def _write_daemon_state(pid: int, port: int) -> None:
    """Write PID and port to state files."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(pid))
    _PORT_FILE.write_text(str(port))


def _touch_last_used() -> None:
    """Update the last-used timestamp to now."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _LAST_USED_FILE.write_text(str(time.time()))


def _is_idle_expired() -> bool:
    """Return True if the daemon has been idle longer than _IDLE_TIMEOUT."""
    try:
        last_used = float(_LAST_USED_FILE.read_text().strip())
        return (time.time() - last_used) > _IDLE_TIMEOUT
    except (FileNotFoundError, ValueError):
        # No timestamp → assume it's been running a while, let it live
        return False


def _clear_daemon_state() -> None:
    """Remove state files."""
    for f in (_PID_FILE, _PORT_FILE, _LAST_USED_FILE):
        try:
            f.unlink()
        except FileNotFoundError:
            pass


class McpServerManager:
    """Manages a persistent keboola-mcp-server daemon with HTTP transport.

    The server runs as a detached daemon process that survives between CLI
    invocations. State (PID + port) is stored in ~/.cache/kbagent/.

    On first use the daemon is started (~4s cold start). Subsequent CLI
    invocations reuse the running daemon (~0.03s connection overhead).
    """

    @property
    def is_running(self) -> bool:
        """Check if the daemon is alive and reachable."""
        state = _read_daemon_state()
        if state is None:
            return False
        pid, port = state
        return _process_alive(pid) and _tcp_reachable(port)

    @property
    def port(self) -> int | None:
        """Return the port the daemon is listening on, or None if not running."""
        state = _read_daemon_state()
        if state is None:
            return None
        pid, port = state
        if _process_alive(pid) and _tcp_reachable(port):
            return port
        return None

    @property
    def base_url(self) -> str | None:
        """Return the base URL of the running daemon, or None if not running."""
        p = self.port
        return f"http://127.0.0.1:{p}" if p is not None else None

    def ensure_running(self) -> str:
        """Return daemon URL, starting the daemon if necessary.

        Updates the last-used timestamp on every call so the idle timeout
        is reset. Stops and restarts the daemon if it has been idle too long.

        Returns:
            Base URL string like "http://127.0.0.1:PORT".

        Raises:
            RuntimeError: If the daemon cannot be started.
        """
        state = _read_daemon_state()
        if state is not None:
            pid, port = state
            if _process_alive(pid) and _tcp_reachable(port):
                # Check idle timeout before reusing
                if _is_idle_expired():
                    logger.info(
                        "MCP daemon idle timeout reached (pid=%d), restarting", pid
                    )
                    self.stop()
                else:
                    logger.debug("Reusing existing MCP daemon pid=%d port=%d", pid, port)
                    _touch_last_used()
                    return f"http://127.0.0.1:{port}"
            else:
                # Stale state — clear it
                logger.info("Stale MCP daemon state (pid=%d), starting new daemon", pid)
                _clear_daemon_state()

        url = self._start_daemon()
        _touch_last_used()
        return url

    def _start_daemon(self) -> str:
        """Start a new daemon process and persist its state.

        Returns:
            Base URL of the running daemon.

        Raises:
            RuntimeError: If the daemon command is not found or fails to start.
        """
        command_parts = detect_mcp_server_command()
        if command_parts is None:
            raise RuntimeError(
                "Cannot find keboola-mcp-server. "
                "Install it with: pip install keboola-mcp-server "
                "(or: uvx keboola_mcp_server)"
            )

        port = _find_free_port()
        cmd = [
            *command_parts,
            "--transport", "streamable-http",
            "--host", "127.0.0.1",
            "--port", str(port),
        ]

        logger.info("Starting MCP daemon: %s", " ".join(cmd))

        # start_new_session=True detaches from CLI process group — daemon survives CLI exit
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        _write_daemon_state(proc.pid, port)

        if not self._wait_for_ready(proc, port):
            _clear_daemon_state()
            try:
                proc.terminate()
            except Exception:
                pass
            raise RuntimeError(
                f"MCP daemon failed to start within {MCP_SERVER_STARTUP_TIMEOUT}s. "
                f"Command: {' '.join(cmd)}"
            )

        logger.info("MCP daemon ready at http://127.0.0.1:%d (pid=%d)", port, proc.pid)
        return f"http://127.0.0.1:{port}"

    def _wait_for_ready(self, proc: subprocess.Popen, port: int) -> bool:  # type: ignore[type-arg]
        """Poll until the daemon accepts TCP connections or timeout."""
        deadline = time.monotonic() + MCP_SERVER_STARTUP_TIMEOUT
        interval = 0.2

        while time.monotonic() < deadline:
            # Check if process died early
            if proc.poll() is not None:
                return False
            if _tcp_reachable(port):
                return True
            time.sleep(interval)
            interval = min(interval * 1.5, 1.0)

        return False

    def stop(self) -> None:
        """Stop the daemon if running."""
        state = _read_daemon_state()
        if state is None:
            return
        pid, port = state
        if _process_alive(pid):
            logger.info("Stopping MCP daemon (pid=%d)", pid)
            try:
                os.kill(pid, signal.SIGTERM)
                # Wait up to 5s for graceful shutdown
                for _ in range(50):
                    if not _process_alive(pid):
                        break
                    time.sleep(0.1)
                else:
                    os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        _clear_daemon_state()

    def get_status(self) -> dict[str, Any]:
        """Return status info for the doctor command."""
        state = _read_daemon_state()
        if state is None:
            return {"running": False, "port": None, "base_url": None, "pid": None, "idle_seconds": None}
        pid, port = state
        alive = _process_alive(pid)
        reachable = _tcp_reachable(port) if alive else False
        idle_seconds: float | None = None
        try:
            last_used = float(_LAST_USED_FILE.read_text().strip())
            idle_seconds = round(time.time() - last_used)
        except (FileNotFoundError, ValueError):
            pass
        return {
            "running": alive and reachable,
            "port": port if reachable else None,
            "base_url": f"http://127.0.0.1:{port}" if reachable else None,
            "pid": pid if alive else None,
            "idle_seconds": idle_seconds,
        }


# Module-level singleton
_server_manager: McpServerManager | None = None


def get_server_manager() -> McpServerManager:
    """Get or create the module-level McpServerManager singleton."""
    global _server_manager
    if _server_manager is None:
        _server_manager = McpServerManager()
    return _server_manager
