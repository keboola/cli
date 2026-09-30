"""'press c to copy' option for a value printed by a command that then waits.

Two users: the device-login flow (``commands/auth.py``) copies the
verification URL, and ``data-app password`` copies the password (which it never
prints). The design is deliberately narrow: it folds a single-key read into a
wait -- for device login, the wait between device-token polls (the ``sleep``
seam of ``auth/device.run_device_flow``) -- so there is no background thread
and no in-place redraw. When the terminal or the clipboard cannot support it,
the option disables itself and the command's output is byte-for-byte unchanged.

The clipboard backend is a native command detected by probe, so its presence
is known before anything is printed -- that is what lets the hint stay hidden
when a copy is not actually possible.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from rich.console import Console

_POSIX = sys.platform != "win32"

# A clipboard command that hangs (e.g. xclip waiting on an unreachable X
# display) must not hang the command that called it.
_CLIPBOARD_TIMEOUT_SECONDS = 5.0

# WSL registers these binfmt entries whenever Windows interop is on, also in a
# shell where ``WSL_INTEROP`` is unset and the Windows PATH is missing -- the
# case in which ``clip.exe`` is not on PATH but still runs by its full path.
# Newer WSL releases with systemd register it as ``WSLInterop-late``.
_WSL_INTEROP_MARKERS = (
    Path("/proc/sys/fs/binfmt_misc/WSLInterop"),
    Path("/proc/sys/fs/binfmt_misc/WSLInterop-late"),
)
_WSL_CLIP_EXE = Path("/mnt/c/Windows/System32/clip.exe")

# How long to wait for the next byte after ESC. The bytes of an escape
# sequence (an arrow key sends ESC [ A) arrive together, so a short gap tells
# a lone Esc apart from the start of a sequence.
_ESCAPE_SEQUENCE_GAP_SECONDS = 0.05
# What ``_read_key`` returns for an escape sequence: a key no caller acts on.
_IGNORED_KEY = "\x00"

if _POSIX:
    import select
    import termios
    import tty
else:  # pragma: no cover - Windows-only
    import msvcrt


def _make_copier(argv: list[str]) -> Callable[[str], bool]:
    """Build a copy function that pipes text into ``argv`` on stdin.

    The text goes only to stdin, never into argv, so it cannot appear in a
    process listing. A failure or a timeout returns False.
    """

    def _copy(text: str) -> bool:
        try:
            subprocess.run(
                argv,
                input=text.encode("utf-8"),
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_CLIPBOARD_TIMEOUT_SECONDS,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    return _copy


def detect_clipboard() -> Callable[[str], bool] | None:
    """Return a function that copies text to the system clipboard, or ``None``.

    Detection probes for a native clipboard command with ``shutil.which`` so
    the result is deterministic and known up front -- the caller reads ``None``
    as "do not offer copy". Nothing is run here. Candidates, in order, per
    platform (the copy tries the next one when a tool fails, e.g. ``xclip``
    with no X display):

    - macOS:   ``pbcopy``
    - Windows: ``clip``
    - Linux / other (incl. WSL): ``wl-copy`` (Wayland), ``xclip`` / ``xsel``
      (X11), then ``clip.exe`` (the WSL bridge to the Windows clipboard when no
      X or Wayland tool is installed). On WSL, when none of them is on PATH,
      ``/mnt/c/Windows/System32/clip.exe`` by its full path.
    """
    if sys.platform == "darwin":
        candidates = [["pbcopy"]]
    elif sys.platform == "win32":
        candidates = [["clip"]]
    else:
        candidates = [
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "-b", "-i"],
            ["clip.exe"],
        ]
    available = [argv for argv in candidates if shutil.which(argv[0])]
    wsl_clip = _wsl_clip_exe()
    if wsl_clip is not None:
        available.append([str(wsl_clip)])
    if not available:
        return None
    copiers = [_make_copier(argv) for argv in available]

    def _copy_with_first_working(text: str) -> bool:
        return any(copier(text) for copier in copiers)

    return _copy_with_first_working


def _wsl_clip_exe() -> Path | None:
    """``clip.exe`` by its full path when this is WSL with Windows interop on."""
    if not sys.platform.startswith("linux"):
        return None
    on_wsl = bool(os.environ.get("WSL_INTEROP")) or any(
        marker.exists() for marker in _WSL_INTEROP_MARKERS
    )
    if on_wsl and _WSL_CLIP_EXE.is_file():
        return _WSL_CLIP_EXE
    return None


def stdio_is_interactive() -> bool:
    """True only when stdin and stdout are a terminal and this process is in its foreground.

    A background job (``kbagent ... &``) still has a terminal, but changing
    the terminal mode from the background makes the kernel stop the process
    (SIGTTOU), so it counts as not interactive.
    """
    return (
        hasattr(sys.stdin, "isatty")
        and sys.stdin.isatty()
        and hasattr(sys.stdout, "isatty")
        and sys.stdout.isatty()
        and _in_terminal_foreground()
    )


def _in_terminal_foreground() -> bool:
    """False for a background process group; True where the check does not apply."""
    if not _POSIX:
        return True
    try:
        return os.getpgrp() == os.tcgetpgrp(sys.stdin.fileno())
    except (OSError, ValueError, AttributeError):
        return False


class CopyableUrlWait:
    """A 'press c to copy' option folded into a wait.

    Lifecycle:

    - ``on_prompt(value)``: remember the value, put the terminal in cbreak
      mode so a single key needs no Enter, then print the hint (cbreak first,
      so a key pressed as soon as the hint shows is not flushed). cbreak (not
      raw) keeps signal keys live, so Ctrl+C still interrupts. The value
      itself is never printed.
    - ``wait(interval)``: sleep up to ``interval`` seconds, but watch stdin
      meanwhile; on ``c`` copy the value and print a confirmation line once.
      Device login passes this as the ``sleep`` seam of ``run_device_flow``, so
      the poll cadence is unchanged -- each poll still waits its full interval.
      A key in ``finish_keys`` ends the wait early and sets ``finished``;
      so does end of input when ``finish_keys`` is set. An escape sequence
      (arrow keys) is read whole and ignored; a lone Esc is a key.
    - ``restore()``: undo the cbreak mode. Call it from a ``finally``;
      :meth:`prompt_and_wait` does all three steps that way.

    ``hint`` is the line printed by ``on_prompt`` (default: the device-login
    link hint). When ``enabled`` is False every method degrades to a plain
    sleep and prints nothing, so a non-interactive or clipboard-less run
    behaves as before.
    """

    def __init__(
        self,
        console: Console,
        *,
        key: str = "c",
        copier: Callable[[str], bool] | None = None,
        interactive: bool | None = None,
        hint: str | None = None,
        finish_keys: frozenset[str] = frozenset(),
    ) -> None:
        self._console = console
        self._key = key
        self._copier = copier if copier is not None else detect_clipboard()
        is_interactive = stdio_is_interactive() if interactive is None else interactive
        self.enabled = self._copier is not None and is_interactive
        self._hint = hint if hint is not None else f"Press {key} to copy the link"
        self._finish_keys = finish_keys
        self._value: str | None = None
        self._copied = False
        self._finished = False
        self._saved_termios: list | None = None

    @property
    def copied(self) -> bool:
        """True once the value was copied to the clipboard."""
        return self._copied

    @property
    def finished(self) -> bool:
        """True once a key in ``finish_keys`` ended the wait."""
        return self._finished

    def on_prompt(self, value: str | None) -> None:
        """Arm the option for ``value`` (no-op when disabled or ``value`` is empty)."""
        if not self.enabled or not value:
            return
        self._value = value
        self._enter_cbreak()
        # cyan matches the copied URL's colour at the device-login call site,
        # so the hint and the link it copies read as the same thing.
        self._console.print(f"[cyan]{self._hint}[/cyan]")

    def prompt_and_wait(self, value: str, timeout: float) -> None:
        """``on_prompt`` + ``wait``; the terminal mode is restored even on Ctrl+C."""
        try:
            self.on_prompt(value)
            self.wait(timeout)
        finally:
            self.restore()

    def wait(self, interval: float) -> None:
        """Wait ``interval`` seconds, copying the value if ``c`` is pressed."""
        if not self.enabled or self._value is None:
            time.sleep(interval)
            return
        deadline = time.monotonic() + interval
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            key = self._read_key(remaining)
            if key is None:
                return
            if key == "":  # end of input: nothing more can arrive
                if self._finish_keys:
                    self._finished = True
                    return
                time.sleep(max(0.0, deadline - time.monotonic()))
                return
            if key.lower() == self._key and not self._copied:
                self._do_copy()
            elif key.lower() in self._finish_keys:
                self._finished = True
                return
            # any other key: keep waiting out the remaining interval

    def restore(self) -> None:
        """Restore the terminal mode saved by ``on_prompt``."""
        if not _POSIX or self._saved_termios is None:
            return
        with contextlib.suppress(termios.error, OSError, ValueError):
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved_termios)
        self._saved_termios = None

    # -- internals -----------------------------------------------------------

    def _enter_cbreak(self) -> None:
        if not _POSIX:
            return
        try:
            fd = sys.stdin.fileno()
            self._saved_termios = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        except (termios.error, OSError, ValueError):
            self._saved_termios = None

    def _read_key(self, timeout: float) -> str | None:
        """Read one key within ``timeout``. ``None`` = timed out, ``''`` = EOF.

        POSIX reads the file descriptor with ``os.read``, not ``sys.stdin``:
        a buffered read would pull a burst such as ``c`` + Enter into Python's
        buffer, where ``select`` no longer sees the Enter.
        """
        if _POSIX:
            try:
                fd = sys.stdin.fileno()
                ready, _, _ = select.select([fd], [], [], timeout)
            except (OSError, ValueError):
                time.sleep(timeout)
                return None
            if not ready:
                return None
            try:
                data = os.read(fd, 1)
            except OSError:  # the terminal went away (EIO after a hangup)
                return ""
            if data == b"\x1b" and _read_escape_tail(fd):
                return _IGNORED_KEY
            return data.decode("utf-8", "replace")
        end = time.monotonic() + timeout  # pragma: no cover - Windows-only
        while time.monotonic() < end:
            if msvcrt.kbhit():
                return msvcrt.getwch()
            time.sleep(min(0.03, max(0.0, end - time.monotonic())))
        return None

    def _do_copy(self) -> None:
        assert self._value is not None
        if self._copier is not None and self._copier(self._value):
            self._copied = True
            self._console.print("[green]✓ Copied to clipboard[/green]")


def _read_byte_within(fd: int, timeout: float) -> bytes | None:
    """One byte from ``fd`` if it arrives within ``timeout``, else None."""
    try:
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return None
        return os.read(fd, 1) or None
    except OSError:
        return None


def _read_escape_tail(fd: int) -> bool:
    """After ESC, read the rest of an escape sequence; False for a lone Esc.

    CSI (ESC [ ...) ends with a byte in 0x40-0x7E (arrow keys: ESC [ A);
    SS3 (ESC O x) is one more byte; anything else (Alt+key) is one byte.
    """
    first = _read_byte_within(fd, _ESCAPE_SEQUENCE_GAP_SECONDS)
    if first is None:
        return False
    if first == b"[":
        while True:
            byte = _read_byte_within(fd, _ESCAPE_SEQUENCE_GAP_SECONDS)
            if byte is None or 0x40 <= byte[0] <= 0x7E:
                break
    elif first == b"O":
        _read_byte_within(fd, _ESCAPE_SEQUENCE_GAP_SECONDS)
    return True
