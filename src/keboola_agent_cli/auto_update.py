"""Auto-update module for kbagent CLI.

Checks for updates on startup, downloads the new version, and re-execs
the same command in the updated version (similar to Claude Code).

All output goes to sys.stderr.write() since OutputFormatter is not yet
initialized when this runs. The entire flow is wrapped in a blanket
try/except so it NEVER crashes the CLI.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from importlib.metadata import distribution
from pathlib import Path

import platformdirs

from . import __version__
from .changelog import ENV_UPDATED_FROM, format_whats_new
from .constants import (
    AUTO_UPDATE_CHECK_INTERVAL,
    ENV_AUTO_UPDATE,
    ENV_SKIP_UPDATE,
    KBAGENT_INSTALL_SOURCE,
    VERSION_CACHE_FILENAME,
    VERSION_CHECK_TIMEOUT,
)
from .services.version_service import (
    _detect_mcp_install_method,
    _fetch_kbagent_latest_version,
    _fetch_mcp_latest_version,
    _get_local_mcp_version,
    _is_up_to_date,
    _perform_mcp_update,
)

logger = logging.getLogger(__name__)


def _get_cache_path() -> Path:
    """Return path to the version cache file.

    Uses the global config directory (~/.config/keboola-agent-cli/).
    """
    config_dir = Path(platformdirs.user_config_dir("keboola-agent-cli"))
    return config_dir / VERSION_CACHE_FILENAME


def _read_cache() -> dict | None:
    """Read the version cache file.

    Returns:
        Parsed dict with ``last_check`` (required) and any of
        ``latest_version`` / ``mcp_latest_version`` / ``mcp_install_method``,
        or None if the file is missing, unreadable, or corrupt. Older
        cache formats lacking the MCP fields are still accepted -- the
        missing fields trigger a fresh fetch in the same run.
    """
    cache_path = _get_cache_path()
    try:
        if not cache_path.is_file():
            return None
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "last_check" in data and "latest_version" in data:
            return data
        return None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _write_cache(
    latest_version: str,
    mcp_latest_version: str | None = None,
    mcp_install_method: str | None = None,
) -> None:
    """Write the version cache file.

    Args:
        latest_version: kbagent latest version (required).
        mcp_latest_version: keboola-mcp-server latest version from PyPI.
        mcp_install_method: Detected MCP install method (drives upgrade cmd).
    """
    cache_path = _get_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict = {
            "last_check": time.time(),
            "latest_version": latest_version,
        }
        if mcp_latest_version is not None:
            payload["mcp_latest_version"] = mcp_latest_version
        if mcp_install_method is not None:
            payload["mcp_install_method"] = mcp_install_method
        cache_path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass  # Non-critical; next run will re-fetch


def _is_cache_fresh(cache: dict, ttl: int) -> bool:
    """Check whether the cache is still within its TTL.

    Args:
        cache: Parsed cache dict with 'last_check' timestamp.
        ttl: Maximum age in seconds.

    Returns:
        True if the cache is fresh, False if stale.
    """
    try:
        return (time.time() - float(cache["last_check"])) < ttl
    except (KeyError, TypeError, ValueError):
        return False


def _is_dev_install() -> bool:
    """Detect development (editable) installs.

    Returns True if:
    - __version__ is '0.0.0-dev' (PackageNotFoundError fallback), or
    - The package was installed in editable mode (PEP 660 direct_url.json).
    """
    if __version__ == "0.0.0-dev":
        return True

    try:
        dist = distribution("keboola-agent-cli")
        direct_url = dist.read_text("direct_url.json")
        if direct_url:
            data = json.loads(direct_url)
            # Editable installs have dir_info.editable = true
            if data.get("dir_info", {}).get("editable", False):
                return True
    except Exception:
        pass

    return False


def _should_skip() -> bool:
    """Determine whether the auto-update check should be skipped.

    Skip conditions:
    - KBAGENT_SKIP_UPDATE=1 (set by re-exec to prevent loops)
    - KBAGENT_AUTO_UPDATE in {false, 0, no} (user opt-out)
    - Development/editable install
    - Current command is 'update' or 'version' (handled separately)
    """
    # Re-exec guard
    if os.environ.get(ENV_SKIP_UPDATE) == "1":
        return True

    # User opt-out
    auto_update_val = os.environ.get(ENV_AUTO_UPDATE, "").lower().strip()
    if auto_update_val in ("false", "0", "no"):
        return True

    # Dev install
    if _is_dev_install():
        return True

    # Skip for update/version commands (they handle versioning themselves)
    argv = sys.argv
    if len(argv) >= 2:
        cmd = argv[1].lower()
        if cmd in ("update", "version"):
            return True

    return False


def _perform_update(latest_version: str) -> bool:
    """Download and install the latest version.

    Tries uv first, falls back to pip.

    Args:
        latest_version: The version being updated to (for logging).

    Returns:
        True if the update succeeded, False otherwise.
    """
    uv_path = shutil.which("uv")
    if uv_path:
        cmd = [uv_path, "tool", "install", "--upgrade", KBAGENT_INSTALL_SOURCE]
    else:
        pip_path = shutil.which("pip")
        if pip_path is None:
            return False
        cmd = [pip_path, "install", "--upgrade", KBAGENT_INSTALL_SOURCE]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        return False


def _re_exec() -> None:
    """Replace the current process with the updated kbagent binary.

    Sets KBAGENT_SKIP_UPDATE=1 to prevent infinite re-exec loops.
    Falls back to `python -m keboola_agent_cli` if the kbagent binary
    is not found on PATH.
    """
    env = os.environ.copy()
    env[ENV_SKIP_UPDATE] = "1"

    kbagent_path = shutil.which("kbagent")
    if kbagent_path:
        os.execvpe("kbagent", sys.argv, env)
    else:
        # Fallback: run as python module
        new_argv = [sys.executable, "-m", "keboola_agent_cli", *sys.argv[1:]]
        os.execvpe(sys.executable, new_argv, env)


def show_post_update_changelog() -> None:
    """Print 'What's new' after a successful auto-update re-exec.

    Checks for ``KBAGENT_UPDATED_FROM`` env var (set before re-exec).
    If present, prints the changelog for the current version and clears
    the env var so it only fires once.
    """
    try:
        old_version = os.environ.pop(ENV_UPDATED_FROM, "")
        if not old_version:
            return
        msg = format_whats_new(old_version, __version__)
        if msg:
            sys.stderr.write(msg)
    except Exception:
        pass  # Never crash


def _maybe_update_mcp(cache: dict | None, fetched_now: bool) -> str | None:
    """Check for and apply a keboola-mcp-server upgrade.

    Args:
        cache: Existing cache dict (may be stale) or None.
        fetched_now: True if this run has already done a fresh latest-version
            fetch for kbagent. Used to avoid double network round-trips:
            when stale, we issue both fetches in the same pass and persist
            both to the cache.

    Returns:
        ``mcp_latest_version`` to persist to the cache (None if skipped or
        fetch failed). Caller composes the cache write.
    """
    # Use cached MCP latest if fresh; otherwise fetch.
    cached_latest: str | None = None
    if cache is not None:
        candidate = cache.get("mcp_latest_version")
        if isinstance(candidate, str) and candidate:
            cached_latest = candidate

    if not fetched_now and cached_latest:
        mcp_latest: str | None = cached_latest
    else:
        mcp_latest = _fetch_mcp_latest_version(timeout=VERSION_CHECK_TIMEOUT)

    if mcp_latest is None:
        return cached_latest  # nothing to do; preserve any prior cache

    local_version = _get_local_mcp_version()
    up_to_date = _is_up_to_date(local_version, mcp_latest)
    if up_to_date is True:
        return mcp_latest

    method = _detect_mcp_install_method()
    if method == "none":
        # Nothing installed locally; do not auto-install on startup.
        return mcp_latest

    sys.stderr.write(
        f"Updating keboola-mcp-server v{local_version or 'unknown'} -> v{mcp_latest}"
        f" (via {method})...\n"
    )
    success, info = _perform_mcp_update(method=method, timeout=180.0)
    if success:
        post_version = _get_local_mcp_version() or mcp_latest
        sys.stderr.write(f"Updated keboola-mcp-server to v{post_version}.\n")
    else:
        sys.stderr.write(
            f"keboola-mcp-server upgrade skipped: {info}; continuing with current version.\n"
        )

    return mcp_latest


def maybe_auto_update() -> None:
    """Main entry point for the auto-update flow.

    Called from ``cli.py`` at the very top of ``main()``. Orchestrates two
    sequential stages:

    1. **kbagent self-update** -- if the installed version is behind the
       latest GitHub release, download the upgrade and ``execvpe`` the new
       binary with the same argv. The new process re-enters this function
       and the kbagent stage short-circuits as up-to-date.
    2. **keboola-mcp-server update** -- if the locally installed MCP server
       is behind PyPI, run the upgrade command matching the install
       method (``uv tool upgrade`` / ``pip install -U`` / ``uvx --refresh``).
       No re-exec is needed: the MCP server is spawned by ``tool call``
       commands and the next spawn picks up the new version.

    Cache discipline: a single cache file at
    ``~/.config/keboola-agent-cli/version_cache.json`` stores both
    ``latest_version`` (kbagent) and ``mcp_latest_version`` so we make at
    most two PyPI/GitHub round-trips per ``AUTO_UPDATE_CHECK_INTERVAL``.

    This function NEVER raises. All exceptions are caught and logged at
    debug level so the CLI always proceeds normally.
    """
    try:
        if _should_skip():
            return

        cache = _read_cache()
        cache_is_fresh = bool(cache and _is_cache_fresh(cache, AUTO_UPDATE_CHECK_INTERVAL))

        # Stage 1: kbagent self-update.
        if cache_is_fresh:
            latest_version: str | None = cache.get("latest_version")  # type: ignore[union-attr]
        else:
            latest_version = _fetch_kbagent_latest_version(timeout=VERSION_CHECK_TIMEOUT)

        if latest_version is not None:
            up_to_date = _is_up_to_date(__version__, latest_version)
            if up_to_date is False:
                sys.stderr.write(f"Updating kbagent v{__version__} -> v{latest_version}...\n")
                if _perform_update(latest_version):
                    sys.stderr.write(f"Updated to v{latest_version}. Re-launching...\n")
                    # Persist cache before re-exec so the new process does
                    # not refetch immediately.
                    _write_cache(
                        latest_version,
                        mcp_latest_version=cache.get("mcp_latest_version") if cache else None,
                        mcp_install_method=cache.get("mcp_install_method") if cache else None,
                    )
                    os.environ[ENV_UPDATED_FROM] = __version__
                    _re_exec()
                    return  # Defensive: _re_exec replaces the process.
                sys.stderr.write("Auto-update failed; continuing with current version.\n")

        # Stage 2: keboola-mcp-server update.
        mcp_latest = _maybe_update_mcp(cache, fetched_now=not cache_is_fresh)
        mcp_install_method = _detect_mcp_install_method()

        # Persist combined cache (kbagent + MCP) when we did any fresh fetch.
        if not cache_is_fresh and latest_version is not None:
            _write_cache(
                latest_version,
                mcp_latest_version=mcp_latest,
                mcp_install_method=mcp_install_method,
            )
    except Exception:
        # Blanket catch: auto-update must NEVER crash the CLI.
        logger.debug("Auto-update check failed", exc_info=True)
