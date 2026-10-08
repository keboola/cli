"""Unit tests for the 'press c to copy' helper (commands/_url_copy.py)."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from rich.console import Console

from keboola_agent_cli.commands import _url_copy
from keboola_agent_cli.commands._url_copy import CopyableUrlWait, detect_clipboard


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=True, width=80)


def _output(console: Console) -> str:
    """Everything a ``_console()`` was asked to print.

    Rich declares ``Console.file`` as ``IO[str]``, which has no ``getvalue``,
    so reading the buffer straight off the console is a type error (`ty`). The
    cast narrows it back to the ``StringIO`` ``_console`` put there -- the same
    idiom ``tests/test_output.py`` uses at its 23 read sites. Wrapped in a
    helper here only because this file reads the buffer from several tests.
    """
    return cast(io.StringIO, console.file).getvalue()


class _Recorder:
    def __init__(self) -> None:
        self.copied: list[str] = []

    def __call__(self, text: str) -> bool:
        self.copied.append(text)
        return True


def test_detect_clipboard_none_when_no_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_url_copy.shutil, "which", lambda _cmd: None)
    # Without this the WSL clip.exe fallback finds the real one on a WSL machine.
    monkeypatch.setattr(_url_copy, "_wsl_clip_exe", lambda: None)
    assert detect_clipboard() is None


def test_detect_clipboard_darwin_uses_pbcopy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_url_copy.sys, "platform", "darwin")
    seen: list[str] = []

    def _which(cmd: str) -> str | None:
        seen.append(cmd)
        return f"/usr/bin/{cmd}"

    monkeypatch.setattr(_url_copy.shutil, "which", _which)
    assert detect_clipboard() is not None
    assert seen == ["pbcopy"]


def test_detect_clipboard_linux_prefers_wl_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_url_copy.sys, "platform", "linux")
    monkeypatch.setattr(
        _url_copy.shutil, "which", lambda cmd: "/usr/bin/wl-copy" if cmd == "wl-copy" else None
    )
    assert detect_clipboard() is not None


def test_disabled_without_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    # `copier=None` means "detect one", NOT "there is none" (_url_copy.py:123),
    # so the no-backend case has to be staged by making detection fail. Without
    # this the test passes only on a machine with no clipboard command -- i.e.
    # a bare CI container -- and fails for every developer on macOS (`pbcopy`
    # is always there), Windows, WSL, or a Linux desktop.
    monkeypatch.setattr(_url_copy, "detect_clipboard", lambda: None)
    wait = CopyableUrlWait(_console(), copier=None, interactive=True)
    assert wait.enabled is False


def test_disabled_when_not_interactive() -> None:
    wait = CopyableUrlWait(_console(), copier=_Recorder(), interactive=False)
    assert wait.enabled is False


def test_on_prompt_prints_hint_when_enabled() -> None:
    console = _console()
    wait = CopyableUrlWait(console, copier=_Recorder(), interactive=True)
    wait.on_prompt("https://example.com/x")
    assert "Press c to copy the link" in _output(console)


def test_on_prompt_hint_is_cyan() -> None:
    """The style of the `auth login --device-code` hint, also used by `data-app password`."""
    console = Console(
        file=io.StringIO(), force_terminal=True, color_system="standard", no_color=False, width=80
    )
    wait = CopyableUrlWait(console, copier=_Recorder(), interactive=True)
    wait.on_prompt("https://example.com/x")
    assert "\x1b[36mPress c to copy the link\x1b[0m" in _output(console)


def test_on_prompt_silent_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same staging as test_disabled_without_backend: no detectable backend.
    monkeypatch.setattr(_url_copy, "detect_clipboard", lambda: None)
    console = _console()
    wait = CopyableUrlWait(console, copier=None, interactive=True)
    wait.on_prompt("https://example.com/x")
    assert _output(console) == ""


def test_wait_copies_on_c_keypress(monkeypatch: pytest.MonkeyPatch) -> None:
    console = _console()
    rec = _Recorder()
    wait = CopyableUrlWait(console, copier=rec, interactive=True)
    wait.on_prompt("https://example.com/x")
    keys = iter(["c", None])
    monkeypatch.setattr(wait, "_read_key", lambda _timeout: next(keys, None))
    wait.wait(0.01)
    assert rec.copied == ["https://example.com/x"]
    assert "Copied to clipboard" in _output(console)


def test_wait_copies_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    console = _console()
    rec = _Recorder()
    wait = CopyableUrlWait(console, copier=rec, interactive=True)
    wait.on_prompt("https://example.com/x")
    keys = iter(["c", "c", None])
    monkeypatch.setattr(wait, "_read_key", lambda _timeout: next(keys, None))
    wait.wait(0.01)
    assert rec.copied == ["https://example.com/x"]


def test_wait_sleeps_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(_url_copy.time, "sleep", lambda seconds: slept.append(seconds))
    wait = CopyableUrlWait(_console(), copier=None, interactive=True)
    wait.wait(0.5)
    assert slept == [0.5]


class _RunRecorder:
    """Stand-in for ``subprocess.run``: records each call, optionally raises."""

    def __init__(self, raises: BaseException | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self._raises = raises

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append((argv, kwargs))
        if self._raises is not None:
            raise self._raises
        return subprocess.CompletedProcess(argv, 0)


def _linux_without_path_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_url_copy.sys, "platform", "linux")
    monkeypatch.setattr(_url_copy.shutil, "which", lambda _cmd: None)
    monkeypatch.delenv("WSL_INTEROP", raising=False)


def _fake_wsl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, marker: bool) -> Path:
    """Point the WSL probes at files under ``tmp_path``; return the fake clip.exe."""
    marker_path = tmp_path / "WSLInterop"
    if marker:
        marker_path.write_text("enabled\n")
    clip = tmp_path / "clip.exe"
    clip.write_text("")
    monkeypatch.setattr(_url_copy, "_WSL_INTEROP_MARKERS", (marker_path,))
    monkeypatch.setattr(_url_copy, "_WSL_CLIP_EXE", clip)
    return clip


def test_copier_sends_text_on_stdin_never_in_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_url_copy.sys, "platform", "darwin")
    monkeypatch.setattr(_url_copy.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    run = _RunRecorder()
    monkeypatch.setattr(_url_copy.subprocess, "run", run)

    copier = detect_clipboard()
    assert copier is not None
    assert copier("s3cret-value") is True

    argv, kwargs = run.calls[0]
    assert argv == ["pbcopy"]
    assert kwargs["input"] == b"s3cret-value"
    assert kwargs["timeout"] == _url_copy._CLIPBOARD_TIMEOUT_SECONDS
    assert all("s3cret-value" not in arg for arg in argv)


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired(["xclip"], _url_copy._CLIPBOARD_TIMEOUT_SECONDS),
        subprocess.CalledProcessError(1, ["xclip"]),
        FileNotFoundError("xclip"),
    ],
)
def test_copier_reports_a_hung_or_failed_tool_as_false(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    monkeypatch.setattr(_url_copy.sys, "platform", "linux")
    monkeypatch.setattr(
        _url_copy.shutil, "which", lambda cmd: "/usr/bin/xclip" if cmd == "xclip" else None
    )
    monkeypatch.setattr(_url_copy.subprocess, "run", _RunRecorder(raises=error))
    copier = detect_clipboard()
    assert copier is not None
    assert copier("x") is False


@pytest.mark.parametrize("via", ["marker", "env"])
def test_wsl_falls_back_to_clip_exe_by_full_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, via: str
) -> None:
    """A WSL shell without the Windows PATH still reaches clip.exe by its full path."""
    _linux_without_path_tools(monkeypatch)
    clip = _fake_wsl(monkeypatch, tmp_path, marker=via == "marker")
    if via == "env":
        monkeypatch.setenv("WSL_INTEROP", "/run/WSL/1_interop")
    run = _RunRecorder()
    monkeypatch.setattr(_url_copy.subprocess, "run", run)

    copier = detect_clipboard()
    assert copier is not None
    assert copier("pw") is True
    argv, kwargs = run.calls[0]
    assert argv == [str(clip)]
    assert kwargs["input"] == b"pw"


def test_no_clip_exe_fallback_outside_wsl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _linux_without_path_tools(monkeypatch)
    _fake_wsl(monkeypatch, tmp_path, marker=False)
    assert detect_clipboard() is None


def test_no_clip_exe_fallback_when_clip_exe_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _linux_without_path_tools(monkeypatch)
    _fake_wsl(monkeypatch, tmp_path, marker=True)
    monkeypatch.setattr(_url_copy, "_WSL_CLIP_EXE", tmp_path / "missing" / "clip.exe")
    assert detect_clipboard() is None


def test_path_tool_wins_over_the_wsl_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(_url_copy.sys, "platform", "linux")
    monkeypatch.setattr(
        _url_copy.shutil, "which", lambda cmd: "/usr/bin/wl-copy" if cmd == "wl-copy" else None
    )
    _fake_wsl(monkeypatch, tmp_path, marker=True)
    run = _RunRecorder()
    monkeypatch.setattr(_url_copy.subprocess, "run", run)
    copier = detect_clipboard()
    assert copier is not None
    copier("x")
    assert run.calls[0][0] == ["wl-copy"]


def test_custom_hint_and_value_never_print_the_value() -> None:
    console = _console()
    rec = _Recorder()
    wait = CopyableUrlWait(console, copier=rec, interactive=True, hint="Press c to copy it")
    wait.on_prompt("the-secret")
    assert "Press c to copy it" in _output(console)
    assert "the-secret" not in _output(console)


def test_finish_key_ends_the_wait_without_copying(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    wait = CopyableUrlWait(
        _console(), copier=rec, interactive=True, finish_keys=frozenset({"\n", "q"})
    )
    wait.on_prompt("the-secret")
    keys = iter(["x", "Q", "c"])
    monkeypatch.setattr(wait, "_read_key", lambda _timeout: next(keys, None))
    wait.wait(60.0)
    assert wait.finished is True
    assert wait.copied is False
    assert rec.copied == []


def test_copy_then_finish_key(monkeypatch: pytest.MonkeyPatch) -> None:
    console = _console()
    rec = _Recorder()
    wait = CopyableUrlWait(console, copier=rec, interactive=True, finish_keys=frozenset({"\n"}))
    wait.on_prompt("the-secret")
    keys = iter(["c", "c", "\n"])
    monkeypatch.setattr(wait, "_read_key", lambda _timeout: next(keys, None))
    wait.wait(60.0)
    assert rec.copied == ["the-secret"]
    assert wait.copied is True
    assert wait.finished is True
    assert "the-secret" not in _output(console)


def test_a_failing_tool_falls_through_to_the_next_and_to_wsl_clip_exe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """xclip without an X display fails at copy time; the next tool is tried."""
    monkeypatch.setattr(_url_copy.sys, "platform", "linux")
    monkeypatch.setattr(
        _url_copy.shutil,
        "which",
        lambda cmd: f"/usr/bin/{cmd}" if cmd in ("xclip", "xsel") else None,
    )
    clip = _fake_wsl(monkeypatch, tmp_path, marker=True)
    calls: list[str] = []

    def _run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append(argv[0])
        if argv[0] != str(clip):
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(_url_copy.subprocess, "run", _run)
    copier = detect_clipboard()
    assert copier is not None
    assert copier("pw") is True
    assert calls == ["xclip", "xsel", str(clip)]


def test_prompt_and_wait_restores_the_terminal_when_wait_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wait = CopyableUrlWait(_console(), copier=_Recorder(), interactive=True)
    restored: list[bool] = []

    def _interrupt(_interval: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(wait, "wait", _interrupt)
    monkeypatch.setattr(wait, "restore", lambda: restored.append(True))
    with pytest.raises(KeyboardInterrupt):
        wait.prompt_and_wait("the-secret", 60.0)
    assert restored == [True]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file descriptors")
class TestKeyReadingFromAPipe:
    """``_read_key`` on a real file descriptor (a pipe stands in for stdin)."""

    @pytest.fixture
    def pipe(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "r")
        monkeypatch.setattr(_url_copy.sys, "stdin", reader)
        yield write_fd
        reader.close()

    def _wait(self, finish_keys: frozenset[str] = frozenset({"\n", "\x1b", "q"})) -> Any:
        wait = CopyableUrlWait(
            _console(), copier=_Recorder(), interactive=True, finish_keys=finish_keys
        )
        wait.on_prompt("the-secret")
        return wait

    def test_eof_finishes_when_finish_keys_are_set(self, pipe: int) -> None:
        wait = self._wait()
        os.close(pipe)
        wait.wait(5.0)
        assert wait.finished is True

    def test_eof_keeps_the_old_wait_without_finish_keys(
        self, pipe: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slept: list[float] = []
        monkeypatch.setattr(_url_copy.time, "sleep", lambda seconds: slept.append(seconds))
        wait = self._wait(frozenset())
        os.close(pipe)
        wait.wait(5.0)
        assert wait.finished is False
        assert slept and slept[0] > 0  # device login: sleep out the poll interval

    def test_escape_sequence_is_ignored_and_the_burst_is_read(self, pipe: int) -> None:
        wait = self._wait()
        os.write(pipe, b"\x1b[Ac\n")
        wait.wait(5.0)
        assert wait.copied is True
        assert wait.finished is True

    def test_lone_esc_finishes(self, pipe: int) -> None:
        wait = self._wait()
        os.write(pipe, b"\x1b")
        wait.wait(5.0)
        assert wait.finished is True
        assert wait.copied is False


def test_background_process_group_is_not_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Changing the terminal mode from the background would stop the process (SIGTTOU)."""
    tty_stream = type("Tty", (), {"isatty": lambda self: True, "fileno": lambda self: 0})()
    monkeypatch.setattr(_url_copy.sys, "stdin", tty_stream)
    monkeypatch.setattr(_url_copy.sys, "stdout", tty_stream)
    monkeypatch.setattr(_url_copy, "_POSIX", True)
    monkeypatch.setattr(_url_copy.os, "getpgrp", lambda: 100, raising=False)
    monkeypatch.setattr(_url_copy.os, "tcgetpgrp", lambda _fd: 200, raising=False)
    assert _url_copy.stdio_is_interactive() is False
    monkeypatch.setattr(_url_copy.os, "tcgetpgrp", lambda _fd: 100, raising=False)
    assert _url_copy.stdio_is_interactive() is True
