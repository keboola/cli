"""Transfer progress on stderr for the storage upload/download commands.

A multi-hour 200 GB upload is often run by an AI agent or in CI, where there
is no terminal to draw a bar on. ``--progress`` therefore reports in two
shapes, always on stderr so stdout (the ``--json`` envelope) stays untouched:

- stderr is a terminal: a Rich bar with percent, transferred/total, speed,
  elapsed time and ETA.
- otherwise: one plain line every ``PROGRESS_LOG_INTERVAL_SECONDS`` plus a
  final line, e.g.
  ``upload big10g.csv: 42.0% 4.20/10.00 GiB, 67.30 MiB/s, elapsed 0:01:03, ETA 0:01:27``

Without the flag the behaviour predates it: a transient bar in human mode on
a terminal, nothing otherwise.

Callbacks receive ``(bytes_done, total_bytes)``; ``total_bytes`` may be None
(a sliced download whose manifest carries no sizes), in which case percent and
ETA are left out. A ``(0, total)`` call before any byte has moved restarts the
clock: a download first waits for its export job, and that wait is neither
transfer time nor a reason to underestimate the speed. The callbacks come from the transfer's coordinator thread;
:class:`ProgressLog` locks anyway, so a future caller on several threads
stays correct.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Annotated

import typer

from ..constants import PROGRESS_LOG_INTERVAL_SECONDS, PROGRESS_RATE_WINDOW_SECONDS
from ..output import OutputFormatter

ProgressCallback = Callable[[int, int | None], None]

ProgressOption = Annotated[
    bool,
    typer.Option(
        "--progress",
        help=(
            "Always report transfer progress on stderr (percent, speed, elapsed, ETA), "
            "also with --json and without a terminal: a bar on a terminal, otherwise "
            f"one line every {PROGRESS_LOG_INTERVAL_SECONDS:g}s. stdout is untouched."
        ),
    ),
]

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def _unit_for(n: float) -> tuple[str, float]:
    """The largest binary unit ``n`` reaches, and its divisor."""
    divisor = 1.0
    for unit in _UNITS[:-1]:
        if n < divisor * 1024:
            return unit, divisor
        divisor *= 1024
    return _UNITS[-1], divisor


def format_bytes(n: float) -> str:
    """``n`` bytes in a binary unit, e.g. ``4.20 GiB``."""
    unit, divisor = _unit_for(n)
    return f"{n:.0f} B" if unit == "B" else f"{n / divisor:.2f} {unit}"


def _format_duration(seconds: float) -> str:
    return str(timedelta(seconds=int(seconds)))


class ProgressLog:
    """Plain-text progress lines for a non-terminal stderr.

    ``update`` is the transfer callback; it emits a line at most once per
    ``interval`` seconds. ``finish`` emits the closing line. ``clock`` is
    injectable so tests drive time explicitly.
    """

    def __init__(
        self,
        label: str,
        total_bytes: int | None,
        write: Callable[[str], None],
        *,
        clock: Callable[[], float] = time.monotonic,
        interval: float = PROGRESS_LOG_INTERVAL_SECONDS,
        window: float = PROGRESS_RATE_WINDOW_SECONDS,
    ) -> None:
        self._label = label
        self._total = total_bytes if total_bytes and total_bytes > 0 else None
        self._write = write
        self._clock = clock
        self._interval = interval
        self._window = window
        self._lock = threading.Lock()
        self._start = clock()
        self._last_emit = self._start
        self._done = 0
        # (time, bytes_done) samples covering the trailing rate window; the
        # oldest one is kept just outside it as the anchor.
        self._samples: deque[tuple[float, int]] = deque([(self._start, 0)])

    def update(self, done: int, total: int | None) -> None:
        """Record ``done`` bytes; emit a line when the interval has passed."""
        with self._lock:
            now = self._clock()
            if total is not None and total > 0:
                self._total = total
            if done == 0 and self._done == 0:
                self._start = self._last_emit = now
                self._samples = deque([(now, 0)])
                return
            self._done = done
            self._samples.append((now, done))
            while len(self._samples) > 1 and self._samples[1][0] <= now - self._window:
                self._samples.popleft()
            if now - self._last_emit >= self._interval:
                self._last_emit = now
                self._write(self._line(now, final=False))

    def finish(self, *, failed: bool = False) -> None:
        """Emit the closing line (overall average speed, no ETA)."""
        with self._lock:
            self._write(self._line(self._clock(), final=True, failed=failed))

    def window_rate(self) -> float | None:
        """Bytes per second over the trailing window; None before a sample."""
        (t0, d0), (t1, d1) = self._samples[0], self._samples[-1]
        if t1 <= t0:
            return None
        return (d1 - d0) / (t1 - t0)

    def _line(self, now: float, *, final: bool, failed: bool = False) -> str:
        elapsed = now - self._start
        total = self._total
        parts: list[str] = []
        if total is not None:
            unit, divisor = _unit_for(total)
            percent = min(100.0, self._done * 100 / total)
            amount = f"{self._done / divisor:.2f}/{total / divisor:.2f} {unit}"
            if unit == "B":
                amount = f"{self._done}/{total} B"
            head = f"{percent:.1f}% {amount}"
        else:
            head = format_bytes(self._done)
        if final:
            rate = self._done / elapsed if elapsed > 0 else None
            speed = f"avg {format_bytes(rate)}/s" if rate is not None else "avg ?"
        else:
            rate = self.window_rate()
            speed = f"{format_bytes(rate)}/s" if rate is not None else "? /s"
        parts.extend([head, speed, f"elapsed {_format_duration(elapsed)}"])
        if final:
            parts.append("failed" if failed else "done")
        elif total is not None:
            eta = (
                _format_duration(max(0, total - self._done) / rate)
                if rate is not None and rate > 0
                else "?"
            )
            parts.append(f"ETA {eta}")
        return f"{self._label}: " + ", ".join(parts)


@contextmanager
def transfer_progress(
    formatter: OutputFormatter, *, label: str, total_bytes: int | None, enabled: bool
) -> Iterator[ProgressCallback | None]:
    """Yield an ``on_progress(done, total)`` callback, or None for no progress.

    ``enabled`` is the ``--progress`` flag. Off: a transient bar only in human
    mode on a terminal (the pre-flag default). On: always, on stderr -- a bar
    on a terminal, plain :class:`ProgressLog` lines otherwise.
    """
    console = formatter.err_console
    if console.is_terminal and (enabled or not formatter.json_mode):
        with _rich_bar(formatter, label, total_bytes, transient=not enabled) as callback:
            yield callback
        return
    if not enabled:
        yield None
        return

    def _write(line: str) -> None:
        console.print(line, markup=False, highlight=False, soft_wrap=True)

    log = ProgressLog(label, total_bytes, _write)
    try:
        yield log.update
    except BaseException:
        log.finish(failed=True)
        raise
    log.finish()


@contextmanager
def _rich_bar(
    formatter: OutputFormatter, label: str, total_bytes: int | None, *, transient: bool
) -> Iterator[ProgressCallback]:
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )

    with Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        DownloadColumn(binary_units=True),
        TransferSpeedColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=formatter.err_console,
        transient=transient,
        speed_estimate_period=PROGRESS_RATE_WINDOW_SECONDS,
    ) as progress:
        task = progress.add_task(label, total=total_bytes or None)

        def _on_progress(done: int, total: int | None) -> None:
            if done == 0:  # transfer starts now: restart elapsed/speed (see module doc)
                progress.reset(task, total=total or None)
            elif total:
                progress.update(task, completed=done, total=total)
            else:
                progress.update(task, completed=done)

        yield _on_progress
