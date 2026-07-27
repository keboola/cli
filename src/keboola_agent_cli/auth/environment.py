"""Browser/remote heuristics: decide whether a same-machine loopback login is usable.

`kbagent auth login` prefers the PKCE authorization-code flow (it needs no
copy-paste), but that only works when this process can (a) open a browser
that (b) can actually reach the loopback listener it just bound -- true on a
desktop, false over SSH, inside a container, or in most WSL setups without a
Windows-side browser opener installed. `detect_browser_environment` centralises
that decision so `services/auth_service.py` (package F) can fall back to the
device flow with a human-readable reason instead of watching PKCE time out.

Every signal is read through an indirection (`os.environ`, `shutil.which`,
`subprocess.run`, `sys.platform`) so tests can monkeypatch each one instead of
faking real SSH sessions, containers, or WSL installs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from dataclasses import dataclass

# Timeout for the `wslview --version` probe: this only needs to prove the
# binary responds, not do real work, so a short timeout keeps a hung/broken
# wslview from stalling the login flow.
_WSLVIEW_PROBE_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class BrowserEnvironment:
    """Whether this machine can complete a same-machine loopback browser login."""

    loopback_browser_usable: bool
    reason: str  # "" when usable; else a short human explanation for the fallback notice
    opener: str  # detected opener command ("xdg-open" / "wslview" / "open" / "start"), "" if none


def _env_flag_set(name: str) -> bool:
    """True when environment variable ``name`` is set to a non-empty value."""
    return bool(os.environ.get(name))


def _is_ssh_session() -> bool:
    return _env_flag_set("SSH_CONNECTION") or _env_flag_set("SSH_TTY")


def _is_containerized() -> bool:
    """True inside a container: Docker's `/.dockerenv` marker, or `$container` (systemd-nspawn,
    Podman, and other OCI runtimes that set it) is present."""
    if os.path.exists("/.dockerenv"):
        return True
    return _env_flag_set("container")


def _wslview_is_working() -> bool:
    """True when `wslview` is on PATH and `wslview --version` exits 0.

    Presence on PATH alone is not enough: a stale/broken wslview shim (common
    when the Windows-side WSL utilities are out of date) would otherwise be
    reported as a usable opener and PKCE would time out waiting for a browser
    that never opens.
    """
    if shutil.which("wslview") is None:
        return False
    try:
        completed = subprocess.run(
            ["wslview", "--version"],
            capture_output=True,
            timeout=_WSLVIEW_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _is_wsl_without_working_opener() -> bool:
    return _env_flag_set("WSL_INTEROP") and not _wslview_is_working()


def _detect_opener() -> str:
    """Best-effort name of the command `open_browser` would end up using.

    Purely informational (surfaced to the user in the fallback notice /
    `--verbose`); the actual open still goes through `webbrowser.open`, which
    has its own, more complete resolution logic.
    """
    if shutil.which("wslview") is not None and _wslview_is_working():
        return "wslview"
    if sys.platform == "darwin":
        return "open" if shutil.which("open") is not None else ""
    if sys.platform.startswith("win"):
        for candidate in ("start", "explorer"):
            if shutil.which(candidate) is not None:
                return candidate
        return ""
    # Linux and other POSIX platforms.
    return "xdg-open" if shutil.which("xdg-open") is not None else ""


def _has_no_opener() -> bool:
    """True when neither a known opener command nor `webbrowser.get()` is available."""
    if _detect_opener():
        return False
    try:
        webbrowser.get()
    except webbrowser.Error:
        return True
    return False


def detect_browser_environment() -> BrowserEnvironment:
    """Decide whether this process can complete a same-machine loopback login.

    Each heuristic below forces the device flow and carries its own `reason`
    text, shown to the user as the fallback explanation (auth contract
    section 9 / design doc section 4.5 step 2):

    - `SSH_CONNECTION` or `SSH_TTY` set (remote shell session).
    - `/.dockerenv` exists, or `$container` is set (containerized).
    - `WSL_INTEROP` is set without a working `wslview` (WSL without a
      reachable Windows-side browser opener).
    - No opener among `xdg-open`/`wslview` (Linux), `open` (macOS),
      `start`/`explorer` (Windows), and `webbrowser.get()` also raises.
    """
    if _is_ssh_session():
        return BrowserEnvironment(
            loopback_browser_usable=False,
            reason="Detected a remote SSH session (SSH_CONNECTION/SSH_TTY is set).",
            opener="",
        )

    if _is_containerized():
        return BrowserEnvironment(
            loopback_browser_usable=False,
            reason="Detected a containerized environment (/.dockerenv or $container is set).",
            opener="",
        )

    if _is_wsl_without_working_opener():
        return BrowserEnvironment(
            loopback_browser_usable=False,
            reason=(
                "Detected WSL (WSL_INTEROP is set) without a working wslview "
                "to open a Windows-side browser."
            ),
            opener="",
        )

    opener = _detect_opener()
    if _has_no_opener():
        return BrowserEnvironment(
            loopback_browser_usable=False,
            reason="No browser opener command was found on this machine.",
            opener="",
        )

    return BrowserEnvironment(loopback_browser_usable=True, reason="", opener=opener)


def open_browser(url: str) -> bool:
    """Best-effort `webbrowser.open` on a daemon thread.

    Never raises and never logs ``url`` -- it carries the PKCE code
    challenge and state, which must not land in any log or terminal echo.
    Returns False when no handler is available (detected synchronously,
    before spawning the thread) and True once the open has been dispatched;
    a True return does not guarantee a browser window actually appeared, only
    that a handler accepted the request.
    """
    try:
        webbrowser.get()
    except webbrowser.Error:
        return False

    threading.Thread(target=_open_silently, args=(url,), daemon=True).start()
    return True


def _open_silently(url: str) -> None:
    """Thread target for `open_browser`: swallow any exception.

    An uncaught exception in a thread is printed via `threading.excepthook`,
    traceback and all -- including this function's arguments, i.e. the URL.
    Catching everything here (not just `webbrowser.Error`) keeps that URL out
    of stderr regardless of what `webbrowser.open` raises internally.
    """
    try:
        webbrowser.open(url)
    except Exception:
        return


__all__ = ["BrowserEnvironment", "detect_browser_environment", "open_browser"]
