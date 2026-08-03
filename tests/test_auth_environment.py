"""Tests for auth/environment.py: browser/remote heuristics.

Every signal `detect_browser_environment` reads is monkeypatched directly
(env vars via `monkeypatch.setenv`/`delenv`, PATH lookups via `shutil.which`,
the `wslview --version` probe via `subprocess.run`, platform via
`sys.platform`) so the heuristics matrix runs without any real SSH session,
container, or WSL install.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any

import pytest

from keboola_agent_cli.auth import environment
from keboola_agent_cli.auth.environment import (
    BrowserEnvironment,
    detect_browser_environment,
    open_browser,
)

_FORCING_ENV_VARS = ("SSH_CONNECTION", "SSH_TTY", "container", "WSL_INTEROP")


def _baseline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    platform: str = "linux",
    dockerenv_exists: bool = False,
    which: Callable[[str], str | None] | None = None,
    run_returncode: int = 0,
    webbrowser_available: bool = True,
) -> None:
    """Reset every heuristic signal to a known "usable desktop" baseline, then
    let the caller override individual pieces for the scenario under test."""
    for name in _FORCING_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(environment.sys, "platform", platform)
    monkeypatch.setattr(environment.os.path, "exists", lambda _path: dockerenv_exists)

    which_fn = which if which is not None else (lambda _name: None)
    monkeypatch.setattr(environment.shutil, "which", which_fn)

    def fake_run(
        args: list[str], *, capture_output: bool, timeout: float, check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=args, returncode=run_returncode)

    monkeypatch.setattr(environment.subprocess, "run", fake_run)

    if webbrowser_available:
        monkeypatch.setattr(environment.webbrowser, "get", lambda: object())
    else:

        def _raise() -> Any:
            raise environment.webbrowser.Error("no browser controller")

        monkeypatch.setattr(environment.webbrowser, "get", _raise)


class TestForcedRemoteHeuristics:
    """Each of these signals alone must force the device flow, with its own reason."""

    def test_ssh_connection_forces_device_flow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _baseline(
            monkeypatch, which=lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None
        )
        monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 22 10.0.0.2 22")

        result = detect_browser_environment()

        assert result.loopback_browser_usable is False
        assert "SSH" in result.reason
        assert result.opener == ""

    def test_ssh_tty_forces_device_flow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _baseline(
            monkeypatch, which=lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None
        )
        monkeypatch.setenv("SSH_TTY", "/dev/pts/0")

        result = detect_browser_environment()

        assert result.loopback_browser_usable is False
        assert "SSH" in result.reason

    def test_dockerenv_marker_forces_device_flow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _baseline(
            monkeypatch,
            dockerenv_exists=True,
            which=lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None,
        )

        result = detect_browser_environment()

        assert result.loopback_browser_usable is False
        assert "container" in result.reason.lower()

    def test_container_env_var_forces_device_flow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _baseline(
            monkeypatch, which=lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None
        )
        monkeypatch.setenv("container", "podman")

        result = detect_browser_environment()

        assert result.loopback_browser_usable is False
        assert "container" in result.reason.lower()

    def test_wsl_without_wslview_on_path_forces_device_flow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _baseline(monkeypatch, which=lambda _name: None)
        monkeypatch.setenv("WSL_INTEROP", "/run/WSL/1_interop")

        result = detect_browser_environment()

        assert result.loopback_browser_usable is False
        assert "WSL" in result.reason

    def test_wsl_with_broken_wslview_forces_device_flow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """wslview on PATH but `wslview --version` exits non-zero must still count
        as no working opener -- presence on PATH alone is not enough."""
        _baseline(
            monkeypatch,
            which=lambda name: "/usr/bin/wslview" if name == "wslview" else None,
            run_returncode=1,
        )
        monkeypatch.setenv("WSL_INTEROP", "/run/WSL/1_interop")

        result = detect_browser_environment()

        assert result.loopback_browser_usable is False
        assert "WSL" in result.reason

    def test_wsl_with_working_wslview_is_usable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _baseline(
            monkeypatch,
            which=lambda name: "/usr/bin/wslview" if name == "wslview" else None,
            run_returncode=0,
        )
        monkeypatch.setenv("WSL_INTEROP", "/run/WSL/1_interop")

        result = detect_browser_environment()

        assert result == BrowserEnvironment(
            loopback_browser_usable=True, reason="", opener="wslview"
        )

    def test_wslview_probe_uses_a_short_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The version probe must not be allowed to hang the login flow."""
        captured: dict[str, Any] = {}

        def fake_run(
            args: list[str], *, capture_output: bool, timeout: float, check: bool
        ) -> subprocess.CompletedProcess[bytes]:
            captured["timeout"] = timeout
            return subprocess.CompletedProcess(args=args, returncode=0)

        _baseline(monkeypatch, which=lambda name: "/usr/bin/wslview" if name == "wslview" else None)
        monkeypatch.setattr(environment.subprocess, "run", fake_run)
        monkeypatch.setenv("WSL_INTEROP", "/run/WSL/1_interop")

        detect_browser_environment()

        assert 0 < captured["timeout"] <= 5

    def test_wslview_probe_raising_is_treated_as_not_working(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raising_run(
            args: list[str], *, capture_output: bool, timeout: float, check: bool
        ) -> subprocess.CompletedProcess[bytes]:
            raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)

        _baseline(monkeypatch, which=lambda name: "/usr/bin/wslview" if name == "wslview" else None)
        monkeypatch.setattr(environment.subprocess, "run", raising_run)
        monkeypatch.setenv("WSL_INTEROP", "/run/WSL/1_interop")

        result = detect_browser_environment()

        assert result.loopback_browser_usable is False


class TestWslviewProbeCost:
    """`wslview --version` costs up to `_WSLVIEW_PROBE_TIMEOUT_SECONDS` per spawn.

    Three consumers need the answer (the WSL heuristic, the opener name, and the
    no-opener check), so probing per consumer would put several seconds on the
    login hot path for every WSL user. The result is computed once and threaded
    through instead of memoized, so nothing survives between calls -- a cache
    would also make this suite order-dependent.
    """

    def _counting_baseline(self, monkeypatch: pytest.MonkeyPatch, *, returncode: int) -> list[str]:
        spawns: list[str] = []

        def counting_run(
            args: list[str], *, capture_output: bool, timeout: float, check: bool
        ) -> subprocess.CompletedProcess[bytes]:
            spawns.append(" ".join(args))
            return subprocess.CompletedProcess(args=args, returncode=returncode)

        _baseline(monkeypatch, which=lambda name: "/usr/bin/wslview" if name == "wslview" else None)
        monkeypatch.setattr(environment.subprocess, "run", counting_run)
        return spawns

    def test_working_wslview_is_probed_once_per_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spawns = self._counting_baseline(monkeypatch, returncode=0)
        monkeypatch.setenv("WSL_INTEROP", "/run/WSL/1_interop")

        result = detect_browser_environment()

        assert result.opener == "wslview"
        assert spawns == ["wslview --version"]

    def test_broken_wslview_is_probed_once_per_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The early-return path must not re-probe on its way out either."""
        spawns = self._counting_baseline(monkeypatch, returncode=1)
        monkeypatch.setenv("WSL_INTEROP", "/run/WSL/1_interop")

        result = detect_browser_environment()

        assert result.loopback_browser_usable is False
        assert spawns == ["wslview --version"]

    def test_not_probed_at_all_once_ssh_already_forced_the_device_flow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SSH and container are decided before the probe, so those machines pay nothing."""
        spawns = self._counting_baseline(monkeypatch, returncode=0)
        monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 22 10.0.0.2 22")

        detect_browser_environment()

        assert spawns == []

    def test_result_is_not_memoized_between_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A newly-installed (or newly-broken) wslview is picked up by the next login."""
        spawns = self._counting_baseline(monkeypatch, returncode=0)

        detect_browser_environment()
        detect_browser_environment()

        assert spawns == ["wslview --version", "wslview --version"]


class TestNoOpenerHeuristic:
    def test_no_opener_and_no_webbrowser_controller_forces_device_flow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _baseline(monkeypatch, which=lambda _name: None, webbrowser_available=False)

        result = detect_browser_environment()

        assert result.loopback_browser_usable is False
        assert result.opener == ""
        assert result.reason

    def test_no_known_opener_but_webbrowser_controller_available_is_usable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No xdg-open/wslview/open/start on PATH, but `webbrowser.get()` still
        resolves a controller (e.g. via $BROWSER) -- still usable."""
        _baseline(monkeypatch, which=lambda _name: None, webbrowser_available=True)

        result = detect_browser_environment()

        assert result.loopback_browser_usable is True
        assert result.opener == ""
        assert result.reason == ""


class TestPlatformOpenerDetection:
    def test_linux_xdg_open_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _baseline(
            monkeypatch,
            platform="linux",
            which=lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None,
        )

        result = detect_browser_environment()

        assert result == BrowserEnvironment(
            loopback_browser_usable=True, reason="", opener="xdg-open"
        )

    def test_macos_open_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _baseline(
            monkeypatch,
            platform="darwin",
            which=lambda name: "/usr/bin/open" if name == "open" else None,
        )

        result = detect_browser_environment()

        assert result == BrowserEnvironment(loopback_browser_usable=True, reason="", opener="open")

    def test_windows_start_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _baseline(
            monkeypatch,
            platform="win32",
            which=lambda name: "C:\\Windows\\start.exe" if name == "start" else None,
        )

        result = detect_browser_environment()

        assert result == BrowserEnvironment(loopback_browser_usable=True, reason="", opener="start")

    def test_windows_falls_back_to_explorer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _baseline(
            monkeypatch,
            platform="win32",
            which=lambda name: "C:\\Windows\\explorer.exe" if name == "explorer" else None,
        )

        result = detect_browser_environment()

        assert result.opener == "explorer"


class TestOpenBrowser:
    def test_returns_false_when_no_handler_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise() -> Any:
            raise environment.webbrowser.Error("no browser controller")

        monkeypatch.setattr(environment.webbrowser, "get", _raise)

        assert open_browser("https://connection.keboola.com/admin/auth/pkce/authorize?x=1") is False

    def test_returns_true_and_dispatches_open_on_a_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened: list[str] = []

        monkeypatch.setattr(environment.webbrowser, "get", lambda: object())
        monkeypatch.setattr(environment.webbrowser, "open", opened.append)

        assert open_browser("https://connection.keboola.com/admin/auth/pkce/authorize?x=1") is True

        # The open is dispatched on a daemon thread; give it a moment to run.
        for thread in list(environment.threading.enumerate()):
            if thread is not environment.threading.current_thread():
                thread.join(timeout=1.0)

        assert opened == ["https://connection.keboola.com/admin/auth/pkce/authorize?x=1"]

    def test_never_raises_even_when_webbrowser_open_itself_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_url: str) -> bool:
            raise RuntimeError("boom")

        monkeypatch.setattr(environment.webbrowser, "get", lambda: object())
        monkeypatch.setattr(environment.webbrowser, "open", _boom)

        # Must not raise, even though the underlying opener call fails on its
        # background thread.
        assert open_browser("https://connection.keboola.com/admin/auth/pkce/authorize?x=1") is True

        for thread in list(environment.threading.enumerate()):
            if thread is not environment.threading.current_thread():
                thread.join(timeout=1.0)
