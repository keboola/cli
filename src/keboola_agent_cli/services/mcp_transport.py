"""Persistent MCP HTTP server manager.

Manages a single keboola-mcp-server process running in streamable-http mode.
The server stays running between tool calls, eliminating subprocess spawn overhead.

Per-request project credentials are passed via HTTP headers:
- X-Storage-Token: storage API token
- X-Storage-API-URL: stack URL
- X-Branch-ID: optional branch ID

One server serves ALL projects - just different headers per request.
"""

import atexit
import contextlib
import logging
import os
import socket
import subprocess
import time
from typing import Any

from ..constants import (
    MCP_SERVER_HEALTH_TIMEOUT,
    MCP_SERVER_STARTUP_TIMEOUT,
)
from .mcp_service import detect_mcp_server_command

logger = logging.getLogger(__name__)

# Env vars the MCP subprocess needs (PATH for binary discovery, HOME for
# uv/pip caches, locale vars for Unicode handling, etc.). KBC_* tokens
# are intentionally NOT inherited — they would expose master/manage tokens
# to the MCP subprocess (issue #269 sec-02 / sec-08). Per-project
# Storage tokens are passed via per-request HTTP headers, not env.
_MCP_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "TERM",
    "SHELL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PYTHONPATH",
    "PYTHONHOME",
    "VIRTUAL_ENV",
    # uv-specific cache locations so "uv tool run" picks up the right venv
    "UV_CACHE_DIR",
    "UV_PYTHON",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_CONFIG_HOME",
)


def _build_minimal_env() -> dict[str, str]:
    """Build an env dict for MCP subprocess containing only allow-listed keys.

    Strips ``KBC_MASTER_TOKEN*``, ``KBC_MANAGE_API_TOKEN``, ``KBC_TOKEN``,
    and any other ``KBC_*`` variable so the MCP server does not inherit
    secrets that belong to other auth scopes (issue #269 sec-02 / sec-08).
    """
    return {key: os.environ[key] for key in _MCP_ENV_ALLOWLIST if key in os.environ}


def _find_free_port() -> int:
    """Find a free TCP port by binding to port 0 and reading the assigned port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class McpServerManager:
    """Manages a persistent keboola-mcp-server process with HTTP transport.

    Singleton-like usage: one instance per CLI process. The server is started
    lazily on first use and cleaned up on process exit.
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._port: int | None = None
        self._base_url: str | None = None
        self._registered_atexit: bool = False

    @property
    def port(self) -> int | None:
        """Return the port the server is listening on, or None if not running."""
        return self._port

    @property
    def base_url(self) -> str | None:
        """Return the base URL of the running server, or None if not running."""
        return self._base_url

    @property
    def is_running(self) -> bool:
        """Check if the server process is alive."""
        if self._process is None:
            return False
        return self._process.poll() is None

    def ensure_running(self) -> str:
        """Start the server if not running and return the base URL.

        Returns:
            Base URL string like "http://127.0.0.1:PORT".

        Raises:
            RuntimeError: If the server cannot be started or fails health check.
        """
        if self.is_running and self._base_url is not None:
            # Quick health check - if server crashed, restart
            if self._health_check():
                return self._base_url
            logger.warning("MCP server health check failed, restarting")
            self.stop()

        return self._start()

    def _start(self) -> str:
        """Start the MCP server process.

        Returns:
            Base URL of the running server.

        Raises:
            RuntimeError: If the server command is not found or server fails to start.
        """
        command_parts = detect_mcp_server_command()
        if command_parts is None:
            raise RuntimeError(
                "Cannot find keboola-mcp-server. "
                "Install it with: pip install keboola-mcp-server (or: uvx keboola_mcp_server)"
            )

        port = _find_free_port()
        cmd = [
            *command_parts,
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]

        logger.info("Starting persistent MCP server: %s", " ".join(cmd))

        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_build_minimal_env(),
        )
        self._port = port
        self._base_url = f"http://127.0.0.1:{port}"

        # Register cleanup on exit (only once)
        if not self._registered_atexit:
            atexit.register(self.stop)
            self._registered_atexit = True

        # Wait for server to be ready
        if not self._wait_for_ready():
            # Collect stderr for diagnostics
            stderr_output = ""
            if self._process.stderr:
                with contextlib.suppress(Exception):
                    stderr_output = self._process.stderr.read1(4096).decode(errors="replace")  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]  # read1 is on BufferedIOBase but stderr is typed IO[bytes]
            self.stop()
            raise RuntimeError(
                f"MCP server failed to start within {MCP_SERVER_STARTUP_TIMEOUT}s. "
                f"Command: {' '.join(cmd)}"
                + (f"\nStderr: {stderr_output}" if stderr_output else "")
            )

        logger.info("MCP server ready at %s", self._base_url)
        return self._base_url

    def _wait_for_ready(self) -> bool:
        """Poll the server until it responds to health check or timeout."""
        deadline = time.monotonic() + MCP_SERVER_STARTUP_TIMEOUT
        interval = 0.2

        while time.monotonic() < deadline:
            # Check if process died
            if self._process is not None and self._process.poll() is not None:
                return False

            if self._health_check():
                return True

            time.sleep(interval)
            # Exponential backoff up to 1s
            interval = min(interval * 1.5, 1.0)

        return False

    def _health_check(self) -> bool:
        """Check if the server is responding via a TCP connection test.

        We use a simple TCP connect instead of HTTP request to avoid
        needing httpx as a dependency at this layer.
        """
        if self._port is None:
            return False
        try:
            with socket.create_connection(
                ("127.0.0.1", self._port),
                timeout=MCP_SERVER_HEALTH_TIMEOUT,
            ):
                return True
        except (OSError, ConnectionRefusedError):
            return False

    def stop(self) -> None:
        """Stop the server process if running."""
        if self._process is not None:
            logger.info("Stopping persistent MCP server (pid=%s)", self._process.pid)
            try:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=2)
            except Exception as exc:
                logger.warning("Error stopping MCP server: %s", exc)
            finally:
                self._process = None
                self._port = None
                self._base_url = None

    def get_status(self) -> dict[str, Any]:
        """Return status info for the doctor command."""
        return {
            "running": self.is_running,
            "port": self._port,
            "base_url": self._base_url,
            "pid": self._process.pid if self._process else None,
        }


# Module-level singleton
_server_manager: McpServerManager | None = None


def get_server_manager() -> McpServerManager:
    """Get or create the module-level McpServerManager singleton."""
    global _server_manager
    if _server_manager is None:
        _server_manager = McpServerManager()
    return _server_manager
