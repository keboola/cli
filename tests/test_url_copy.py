"""Unit tests for the 'press c to copy' helper (commands/_url_copy.py)."""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from keboola_agent_cli.commands import _url_copy
from keboola_agent_cli.commands._url_copy import CopyableUrlWait, detect_clipboard


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=True, width=80)


class _Recorder:
    def __init__(self) -> None:
        self.copied: list[str] = []

    def __call__(self, text: str) -> bool:
        self.copied.append(text)
        return True


def test_detect_clipboard_none_when_no_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_url_copy.shutil, "which", lambda _cmd: None)
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


def test_disabled_without_backend() -> None:
    wait = CopyableUrlWait(_console(), copier=None, interactive=True)
    assert wait.enabled is False


def test_disabled_when_not_interactive() -> None:
    wait = CopyableUrlWait(_console(), copier=_Recorder(), interactive=False)
    assert wait.enabled is False


def test_on_prompt_prints_hint_when_enabled() -> None:
    console = _console()
    wait = CopyableUrlWait(console, copier=_Recorder(), interactive=True)
    wait.on_prompt("https://example.com/x")
    assert "Press c to copy the link" in console.file.getvalue()


def test_on_prompt_silent_when_disabled() -> None:
    console = _console()
    wait = CopyableUrlWait(console, copier=None, interactive=True)
    wait.on_prompt("https://example.com/x")
    assert console.file.getvalue() == ""


def test_wait_copies_on_c_keypress(monkeypatch: pytest.MonkeyPatch) -> None:
    console = _console()
    rec = _Recorder()
    wait = CopyableUrlWait(console, copier=rec, interactive=True)
    wait.on_prompt("https://example.com/x")
    keys = iter(["c", None])
    monkeypatch.setattr(wait, "_read_key", lambda _timeout: next(keys, None))
    wait.wait(0.01)
    assert rec.copied == ["https://example.com/x"]
    assert "Copied to clipboard" in console.file.getvalue()


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
