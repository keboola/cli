"""'press c to copy' option for a URL printed by a command that then waits.

Only the device-login flow uses this today (``commands/auth.py``). The design
is deliberately narrow: it folds a single-key read into the wait the command
already does between device-token polls (the ``sleep`` seam of
``auth/device.run_device_flow``), so there is no background thread and no
in-place redraw. When the terminal or the clipboard cannot support it, the
option disables itself and the command's output is byte-for-byte unchanged.

The clipboard backend is a native command detected by probe, so its presence
is known before anything is printed -- that is what lets the hint stay hidden
when a copy is not actually possible.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import sys
import time
from collections.abc import Callable

from rich.console import Console

_POSIX = sys.platform != "win32"

if _POSIX:
    import select
    import termios
    import tty
else:  # pragma: no cover - Windows-only
    import msvcrt


def _make_copier(argv: list[str]) -> Callable[[str], bool]:
    """Build a copy function that pipes text into ``argv`` on stdin."""

    def _copy(text: str) -> bool:
        try:
            subprocess.run(
                argv,
                input=text.encode("utf-8"),
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    return _copy


def detect_clipboard() -> Callable[[str], bool] | None:
    """Return a function that copies text to the system clipboard, or ``None``.

    Detection probes for a native clipboard command with ``shutil.which`` so
    the result is deterministic and known up front -- the caller reads ``None``
    as "do not offer copy". First hit wins, per platform:

    - macOS:   ``pbcopy``
    - Windows: ``clip``
    - Linux / other (incl. WSL): ``wl-copy`` (Wayland), ``xclip`` / ``xsel``
      (X11), then ``clip.exe`` (the WSL bridge to the Windows clipboard when no
      X or Wayland tool is installed).
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
    for argv in candidates:
        if shutil.which(argv[0]):
            return _make_copier(argv)
    return None


def _stdio_is_interactive() -> bool:
    """True only when both stdin and stdout are a real terminal."""
    return (
        hasattr(sys.stdin, "isatty")
        and sys.stdin.isatty()
        and hasattr(sys.stdout, "isatty")
        and sys.stdout.isatty()
    )


class CopyableUrlWait:
    """A 'press c to copy' option folded into a poll wait.

    Lifecycle:

    - ``on_prompt(url)``: remember the URL, print the hint, and put the
      terminal in cbreak mode so a single key needs no Enter. cbreak (not raw)
      keeps signal keys live, so Ctrl+C still interrupts.
    - ``wait(interval)``: sleep up to ``interval`` seconds, but watch stdin
      meanwhile; on ``c`` copy the URL and print a confirmation line once.
      This is passed as the ``sleep`` seam of ``run_device_flow``, so the poll
      cadence is unchanged -- each poll still waits its full interval.
    - ``restore()``: undo the cbreak mode. Call it from a ``finally``.

    When ``enabled`` is False every method degrades to a plain sleep and prints
    nothing, so a non-interactive or clipboard-less run behaves as before.
    """

    def __init__(
        self,
        console: Console,
        *,
        key: str = "c",
        copier: Callable[[str], bool] | None = None,
        interactive: bool | None = None,
    ) -> None:
        self._console = console
        self._key = key
        self._copier = copier if copier is not None else detect_clipboard()
        is_interactive = _stdio_is_interactive() if interactive is None else interactive
        self.enabled = self._copier is not None and is_interactive
        self._url: str | None = None
        self._copied = False
        self._saved_termios: list | None = None

    def on_prompt(self, url: str | None) -> None:
        """Arm the option for ``url`` (no-op when disabled or ``url`` is empty)."""
        if not self.enabled or not url:
            return
        self._url = url
        # cyan matches the copied URL's colour at the call site, so the hint
        # and the link it copies read as the same thing.
        self._console.print(f"[cyan]Press {self._key} to copy the link[/cyan]")
        self._enter_cbreak()

    def wait(self, interval: float) -> None:
        """Wait ``interval`` seconds, copying the URL if ``c`` is pressed."""
        if not self.enabled or self._url is None:
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
            if key == "":  # EOF on a stdin that is not a real terminal
                time.sleep(max(0.0, deadline - time.monotonic()))
                return
            if key.lower() == self._key and not self._copied:
                self._do_copy()
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
        """Read one key within ``timeout``. ``None`` = timed out, ``''`` = EOF."""
        if _POSIX:
            try:
                ready, _, _ = select.select([sys.stdin], [], [], timeout)
            except (OSError, ValueError):
                time.sleep(timeout)
                return None
            if not ready:
                return None
            try:
                return sys.stdin.read(1)
            except (OSError, ValueError):
                return None
        end = time.monotonic() + timeout  # pragma: no cover - Windows-only
        while time.monotonic() < end:
            if msvcrt.kbhit():
                return msvcrt.getwch()
            time.sleep(min(0.03, max(0.0, end - time.monotonic())))
        return None

    def _do_copy(self) -> None:
        assert self._url is not None
        if self._copier is not None and self._copier(self._url):
            self._copied = True
            self._console.print("[green]✓ Copied to clipboard[/green]")
