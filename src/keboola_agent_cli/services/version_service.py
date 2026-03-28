"""Version service - detect local versions and check for updates.

Provides version information for kbagent and its dependency:
- keboola-mcp-server - always runs latest via 'uvx keboola_mcp_server@latest',
  version resolved from PyPI
"""

import logging
import re
import shutil
from typing import Any

import httpx
from packaging.version import InvalidVersion, Version

from .. import __version__
from ..constants import (
    KBAGENT_GITHUB_REPO,
    KBAGENT_INSTALL_SOURCE,
    MCP_PYPI_URL,
    VERSION_CHECK_TIMEOUT,
)

logger = logging.getLogger(__name__)


def _is_uvx_available() -> bool:
    """Check if uvx is available on PATH."""
    return shutil.which("uvx") is not None


def _fetch_kbagent_latest_version(timeout: float = VERSION_CHECK_TIMEOUT) -> str | None:
    """Fetch latest kbagent version from GitHub releases.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Version string like '0.16.0', or None on failure.
    """
    try:
        response = httpx.get(
            f"https://api.github.com/repos/{KBAGENT_GITHUB_REPO}/releases/latest",
            timeout=timeout,
            follow_redirects=True,
            headers={"Accept": "application/vnd.github.v3+json"},
        )
        response.raise_for_status()
        tag = response.json().get("tag_name", "")
        # Strip leading 'v' from tag (e.g. 'v0.16.0' -> '0.16.0')
        version = tag.lstrip("v")
        if re.match(r"\d+\.\d+\.\d+", version):
            return version
        return None
    except (httpx.HTTPError, KeyError, ValueError):
        logger.debug("Failed to fetch latest kbagent version", exc_info=True)
        return None


def _fetch_mcp_latest_version(timeout: float = VERSION_CHECK_TIMEOUT) -> str | None:
    """Fetch latest keboola-mcp-server version from PyPI.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Version string like '1.46.0', or None on failure.
    """
    try:
        response = httpx.get(
            MCP_PYPI_URL,
            timeout=timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        data = response.json()
        version = data.get("info", {}).get("version", "")
        if re.match(r"\d+\.\d+\.\d+", version):
            return version
        return None
    except (httpx.HTTPError, KeyError, ValueError):
        logger.debug("Failed to fetch latest MCP server version", exc_info=True)
        return None


def _is_up_to_date(local: str | None, latest: str | None) -> bool | None:
    """Compare local and latest versions.

    Args:
        local: Locally installed version string.
        latest: Latest available version string.

    Returns:
        True if up to date, False if update available, None if comparison not possible.
    """
    if local is None or latest is None:
        return None
    try:
        return Version(local) >= Version(latest)
    except InvalidVersion:
        return None


class VersionService:
    """Business logic for version detection and update checks.

    Detects local versions of kbagent and checks for available
    keboola-mcp-server updates.
    """

    def get_versions(self) -> dict[str, Any]:
        """Get version information for kbagent and its dependency.

        keboola-mcp-server: always runs latest via 'uvx ... @latest',
        so we only need to check PyPI for the current latest version
        and whether uvx is available.

        Returns:
            Structured dict with kbagent version and dependency info.
        """
        uvx_available = _is_uvx_available()
        mcp_latest = _fetch_mcp_latest_version()
        kbagent_latest = _fetch_kbagent_latest_version()
        kbagent_up_to_date = _is_up_to_date(__version__, kbagent_latest)

        mcp_entry: dict[str, Any] = {
            "name": "keboola-mcp-server",
            "description": "Keboola MCP Server (via uvx @latest)",
            "uvx_available": uvx_available,
            "latest_version": mcp_latest,
            "auto_updates": True,
        }

        return {
            "kbagent": {
                "version": __version__,
                "latest_version": kbagent_latest,
                "up_to_date": kbagent_up_to_date,
                "upgrade_command": f"uv tool install --upgrade {KBAGENT_INSTALL_SOURCE}",
            },
            "dependencies": [
                mcp_entry,
            ],
        }

    def self_update(self) -> dict[str, Any]:
        """Update kbagent to the latest version via uv tool install.

        Returns:
            Dict with update result (old version, new version, output).
        """
        import subprocess

        old_version = __version__
        kbagent_latest = _fetch_kbagent_latest_version()
        up_to_date = _is_up_to_date(old_version, kbagent_latest)

        if up_to_date is True:
            return {
                "updated": False,
                "current_version": old_version,
                "latest_version": kbagent_latest,
                "message": f"kbagent v{old_version} is already up to date.",
            }

        # Try uv tool install --upgrade first, fall back to pip
        uv_path = shutil.which("uv")
        if uv_path:
            cmd = [uv_path, "tool", "install", "--upgrade", KBAGENT_INSTALL_SOURCE]
        else:
            pip_path = shutil.which("pip")
            if pip_path is None:
                return {
                    "updated": False,
                    "current_version": old_version,
                    "latest_version": kbagent_latest,
                    "message": "Neither 'uv' nor 'pip' found on PATH. "
                    f"Install manually: uv tool install --upgrade {KBAGENT_INSTALL_SOURCE}",
                }
            cmd = [pip_path, "install", "--upgrade", KBAGENT_INSTALL_SOURCE]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                return {
                    "updated": True,
                    "current_version": old_version,
                    "latest_version": kbagent_latest,
                    "message": f"Updated kbagent from v{old_version} to v{kbagent_latest}. "
                    "Restart your shell to use the new version.",
                    "output": result.stdout.strip(),
                }
            return {
                "updated": False,
                "current_version": old_version,
                "latest_version": kbagent_latest,
                "message": f"Update failed: {result.stderr.strip()}",
                "output": result.stderr.strip(),
            }
        except subprocess.TimeoutExpired:
            return {
                "updated": False,
                "current_version": old_version,
                "latest_version": kbagent_latest,
                "message": "Update timed out after 120 seconds.",
            }
