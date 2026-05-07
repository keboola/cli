"""Version service - detect local versions and check for updates.

Provides version information for kbagent and the keboola-mcp-server
dependency. Both are auto-updated on kbagent startup (see auto_update.py)
and explicitly via ``kbagent update``. The MCP server version is detected
from the locally installed binary or Python distribution; the latest
version is resolved from PyPI.
"""

import logging
import re
import shutil
import subprocess
from typing import Any

import httpx
from packaging.version import InvalidVersion, Version

from .. import __version__
from ..constants import (
    KBAGENT_GITHUB_REPO,
    KBAGENT_INSTALL_SOURCE,
    MCP_PROBE_TIMEOUT,
    MCP_PYPI_URL,
    MCP_UPGRADE_TIMEOUT,
    VERSION_CHECK_TIMEOUT,
)

logger = logging.getLogger(__name__)

# keboola-mcp-server constants
MCP_PACKAGE_NAME = "keboola-mcp-server"
MCP_BINARY_NAME = "keboola_mcp_server"


def _is_uvx_available() -> bool:
    """Check if uvx is available on PATH."""
    return shutil.which("uvx") is not None


def _get_local_mcp_version(timeout: float = MCP_PROBE_TIMEOUT) -> str | None:
    """Detect the locally installed keboola-mcp-server version.

    Resolution order:

    1. ``keboola_mcp_server --version`` subprocess (works for both a direct
       binary install and ``uv tool install`` -- they both publish a
       ``keboola_mcp_server`` script on PATH).
    2. ``importlib.metadata.version("keboola-mcp-server")`` (works when the
       package is pip-installed in the active Python environment).
    3. None when neither works (typically: uvx cache; we cannot cleanly
       inspect uvx-cached-only installs without forcing a download).

    Args:
        timeout: Subprocess timeout in seconds for the binary --version probe.

    Returns:
        Version string like ``"1.59.1"``, or None when undetectable.
    """
    binary = shutil.which(MCP_BINARY_NAME)
    if binary:
        try:
            result = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode == 0:
                # Combined stdout + stderr -- some tools print version to stderr.
                output = (result.stdout or "") + (result.stderr or "")
                m = re.search(r"(\d+\.\d+\.\d+)", output)
                if m:
                    return m.group(1)
        except (subprocess.TimeoutExpired, OSError):
            pass

    # Fallback: try importlib.metadata in the current Python environment.
    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _pkg_version

        try:
            return _pkg_version(MCP_PACKAGE_NAME)
        except PackageNotFoundError:
            return None
    except ImportError:
        return None


def _uv_tool_list_has_mcp(stdout: str) -> bool:
    """Detect whether ``uv tool list`` output contains ``keboola-mcp-server``.

    Robust against three classes of false-positive that a naive substring
    match (``MCP_PACKAGE_NAME in stdout``) would suffer:

    * **Similarly-named packages** -- e.g. a hypothetical
      ``keboola-mcp-server-foo`` would substring-match but is NOT the same
      tool; we want exact equality on the first whitespace-separated token.
    * **Indented binary listings** -- ``uv tool list`` shows each tool's
      published scripts on indented continuation lines (``    - <script>``),
      which can include ``keboola_mcp_server`` (the binary name) for tools
      that are NOT this package.
    * **Hint / warning text in stderr-merged buffers** -- callers should
      only ever feed `result.stdout` here, but per-line parsing also
      tolerates accidental stderr injection.

    Args:
        stdout: Captured stdout of ``uv tool list``.

    Returns:
        True iff a non-indented, non-blank line's first token is exactly
        ``keboola-mcp-server``.
    """
    for line in stdout.splitlines():
        # Skip indented continuation lines (binary listings under a tool).
        if line.startswith((" ", "\t")):
            continue
        stripped = line.strip()
        if not stripped:
            continue
        # First whitespace-separated token == package name (exact match).
        if stripped.split(maxsplit=1)[0] == MCP_PACKAGE_NAME:
            return True
    return False


def _detect_mcp_install_method() -> str:
    """Detect how keboola-mcp-server is installed locally.

    The detection drives which upgrade command is appropriate:

    - ``uv_tool``  -- installed via ``uv tool install``; upgrade with
      ``uv tool upgrade keboola-mcp-server``.
    - ``pip_env``  -- pip-installed in the active Python environment;
      upgrade with ``pip install --upgrade keboola-mcp-server``.
    - ``uvx``      -- only available via uvx cache (no persistent install);
      upgrade with ``uvx --refresh ...`` to invalidate the cache.
    - ``none``     -- not detectable; cannot upgrade automatically.

    Returns:
        One of ``"uv_tool"``, ``"pip_env"``, ``"uvx"``, ``"none"``.
    """
    binary = shutil.which(MCP_BINARY_NAME)
    if binary:
        # Binary exists. Check if it is registered with `uv tool`.
        uv_path = shutil.which("uv")
        if uv_path:
            try:
                result = subprocess.run(
                    [uv_path, "tool", "list"],
                    capture_output=True,
                    text=True,
                    timeout=MCP_PROBE_TIMEOUT,
                )
                if result.returncode == 0 and _uv_tool_list_has_mcp(result.stdout):
                    return "uv_tool"
            except (subprocess.TimeoutExpired, OSError):
                pass
        # Binary exists but not under uv tool -- treat as pip env install.
        return "pip_env"

    # No binary on PATH; try importlib.metadata.
    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import distribution as _dist

        try:
            _dist(MCP_PACKAGE_NAME)
            return "pip_env"
        except PackageNotFoundError:
            pass
    except ImportError:
        pass

    # Fallback: uvx cache only.
    if shutil.which("uvx"):
        return "uvx"

    return "none"


def _perform_mcp_update(
    method: str | None = None,
    timeout: float = MCP_UPGRADE_TIMEOUT,
) -> tuple[bool, str]:
    """Run the appropriate upgrade command for keboola-mcp-server.

    Args:
        method: Optional install method; if None, detect via
            :func:`_detect_mcp_install_method`.
        timeout: Subprocess timeout in seconds.

    Returns:
        Tuple of ``(success, output_or_reason)``.
    """
    if method is None:
        method = _detect_mcp_install_method()

    cmd: list[str] | None = None
    if method == "uv_tool":
        uv_path = shutil.which("uv")
        if uv_path is None:
            return False, "uv not found on PATH"
        cmd = [uv_path, "tool", "upgrade", MCP_PACKAGE_NAME]
    elif method == "pip_env":
        pip_path = shutil.which("pip")
        if pip_path is None:
            return False, "pip not found on PATH"
        cmd = [pip_path, "install", "--upgrade", MCP_PACKAGE_NAME]
    elif method == "uvx":
        uvx_path = shutil.which("uvx")
        if uvx_path is None:
            return False, "uvx not found on PATH"
        # `uvx --refresh` re-downloads even cached packages.
        cmd = [
            uvx_path,
            "--refresh",
            "--from",
            MCP_PACKAGE_NAME,
            MCP_BINARY_NAME,
            "--version",
        ]
    elif method == "none":
        return False, "keboola-mcp-server is not installed"
    else:
        return False, f"unknown install method: {method!r}"

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return True, (result.stdout or "").strip()
        return False, (result.stderr or "").strip() or "upgrade subprocess failed"
    except subprocess.TimeoutExpired:
        return False, f"upgrade timed out after {timeout}s"
    except OSError as exc:
        return False, f"subprocess error: {exc}"


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

        Both ``kbagent`` and ``keboola-mcp-server`` are auto-updated on
        startup (see :mod:`keboola_agent_cli.auto_update`) and explicitly
        via ``kbagent update``. This method reports:

        - the local installed version (best-effort detection for MCP),
        - the latest available version (GitHub Releases for kbagent,
          PyPI for MCP),
        - the up-to-date status,
        - the install method for MCP (drives which upgrade command runs),
        - the upgrade command shown to the user.

        Returns:
            Structured dict with kbagent + MCP version info.
        """
        kbagent_latest = _fetch_kbagent_latest_version()
        kbagent_up_to_date = _is_up_to_date(__version__, kbagent_latest)

        mcp_local = _get_local_mcp_version()
        mcp_latest = _fetch_mcp_latest_version()
        mcp_up_to_date = _is_up_to_date(mcp_local, mcp_latest)
        mcp_method = _detect_mcp_install_method()

        # Map install method to the upgrade command shown to users.
        mcp_upgrade_cmd_by_method = {
            "uv_tool": f"uv tool upgrade {MCP_PACKAGE_NAME}",
            "pip_env": f"pip install --upgrade {MCP_PACKAGE_NAME}",
            "uvx": f"uvx --refresh --from {MCP_PACKAGE_NAME} {MCP_BINARY_NAME} --version",
            "none": f"uv tool install {MCP_PACKAGE_NAME}",
        }

        mcp_entry: dict[str, Any] = {
            "name": MCP_PACKAGE_NAME,
            "description": "Keboola MCP Server",
            "uvx_available": _is_uvx_available(),
            "version": mcp_local,
            "latest_version": mcp_latest,
            "up_to_date": mcp_up_to_date,
            "install_method": mcp_method,
            "auto_updates": True,
            "upgrade_command": mcp_upgrade_cmd_by_method.get(
                mcp_method, mcp_upgrade_cmd_by_method["none"]
            ),
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
        """Update kbagent + keboola-mcp-server to the latest versions.

        Two-stage flow (both stages always run -- kbagent up-to-date does
        not skip the MCP stage, and vice versa):

        1. **kbagent** -- uses ``uv tool install --upgrade`` (preferred)
           or pip fallback. If the installed kbagent is already at the
           latest version, the stage reports ``updated=False`` without
           running a subprocess.
        2. **keboola-mcp-server** -- detects install method via
           :func:`_detect_mcp_install_method`, runs the matching upgrade
           command (``uv tool upgrade`` / ``pip install -U`` /
           ``uvx --refresh``), and reports the before / after versions.
           If the local MCP version cannot be detected (e.g. uvx-cache-only
           install on first run) the stage still attempts the upgrade --
           a refreshed cache is the desired outcome there.

        Returns:
            Dict with both stages' results::

                {
                    "kbagent": {"updated": bool, "current_version": str,
                                "latest_version": str|None, "message": str,
                                "output": str|None},
                    "mcp": {"updated": bool|None, "current_version": str|None,
                            "latest_version": str|None, "install_method": str,
                            "message": str, "output": str|None},
                    "updated": bool,    # True iff at least one stage upgraded
                    "message": str,     # Human-readable single-line summary
                }
        """
        kbagent_result = self._update_kbagent()
        mcp_result = self._update_mcp()

        any_updated = bool(kbagent_result.get("updated") or mcp_result.get("updated"))
        summary = self._compose_update_summary(kbagent_result, mcp_result)

        return {
            "kbagent": kbagent_result,
            "mcp": mcp_result,
            "updated": any_updated,
            "message": summary,
        }

    @staticmethod
    def _compose_update_summary(kbagent_result: dict[str, Any], mcp_result: dict[str, Any]) -> str:
        """Build a one-line summary of the two-stage update result."""
        parts: list[str] = []
        if kbagent_result.get("updated"):
            parts.append(
                f"kbagent v{kbagent_result.get('current_version')}"
                f" -> v{kbagent_result.get('latest_version')}"
            )
        elif kbagent_result.get("current_version") and kbagent_result.get("latest_version"):
            parts.append(f"kbagent v{kbagent_result.get('current_version')} (already up to date)")

        if mcp_result.get("updated"):
            current = mcp_result.get("current_version") or "unknown"
            latest = mcp_result.get("latest_version") or "?"
            parts.append(f"keboola-mcp-server v{current} -> v{latest}")
        elif mcp_result.get("updated") is False and mcp_result.get("current_version"):
            parts.append(
                f"keboola-mcp-server v{mcp_result.get('current_version')} (already up to date)"
            )
        elif mcp_result.get("updated") is False:
            parts.append(f"keboola-mcp-server: {mcp_result.get('message', 'skipped')}")

        if not parts:
            return "Nothing to update."
        return " | ".join(parts)

    @staticmethod
    def _update_kbagent() -> dict[str, Any]:
        """Run the kbagent self-upgrade subprocess (or short-circuit)."""
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

        # Try uv tool install --upgrade first, fall back to pip.
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

    @staticmethod
    def _update_mcp() -> dict[str, Any]:
        """Run the keboola-mcp-server upgrade subprocess (or short-circuit)."""
        method = _detect_mcp_install_method()
        local_version = _get_local_mcp_version()
        latest_version = _fetch_mcp_latest_version()
        up_to_date = _is_up_to_date(local_version, latest_version)

        # Short-circuit: detected and up-to-date.
        if up_to_date is True:
            return {
                "updated": False,
                "current_version": local_version,
                "latest_version": latest_version,
                "install_method": method,
                "message": f"keboola-mcp-server v{local_version} is already up to date.",
            }

        # Short-circuit: nothing to upgrade against.
        if method == "none":
            return {
                "updated": False,
                "current_version": local_version,
                "latest_version": latest_version,
                "install_method": method,
                "message": (
                    "keboola-mcp-server is not installed. "
                    f"Install with: uv tool install {MCP_PACKAGE_NAME}"
                ),
            }

        # Run the upgrade.
        success, output = _perform_mcp_update(method=method, timeout=MCP_UPGRADE_TIMEOUT)
        post_version = _get_local_mcp_version() if success else local_version

        return {
            "updated": bool(success),
            "current_version": local_version,
            "latest_version": latest_version,
            "post_upgrade_version": post_version,
            "install_method": method,
            "message": (
                f"Upgraded keboola-mcp-server "
                f"({local_version or 'unknown'} -> {post_version or latest_version or '?'}) "
                f"via {method}."
                if success
                else f"keboola-mcp-server upgrade failed: {output}"
            ),
            "output": output,
        }
