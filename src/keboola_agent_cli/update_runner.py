"""How the kbagent self-reinstall is actually executed (issue #528).

Both self-update entry points -- the startup hook in :mod:`.auto_update` and
the explicit ``kbagent update`` command -- ultimately run one install command.
*Where* that command runs is a correctness question, not a detail:

``uv tool install`` recreates a tool environment by **removing** it and then
creating a fresh venv at the same path (``uv-tool/src/lib.rs``:
``create_environment`` -> *"Remove any existing environment"* -> ``create_venv``).
The removal is neither atomic nor rollback-able. On POSIX that is harmless --
unlinking a file another process holds open leaves that process's inode intact,
so a running kbagent survives having its own venv replaced under it. On Windows
it is fatal: uv's trampoline (``kbagent.exe``) loads the tool venv's interpreter
DLL **in-process**, so those files are locked; the removal deletes everything it
can, hits a locked file, and aborts -- leaving a gutted venv. Upstream tracks the
same failure as astral-sh/uv#11930.

This module therefore provides two things:

* :func:`run_install` -- a subprocess runner that **never kills the installer**.
  ``subprocess.run(timeout=...)`` terminates the child on timeout, which on
  Windows is ``TerminateProcess`` -- a hard kill of uv mid-write produces exactly
  the same gutted venv as a lock failure. When the deadline passes we stop
  *waiting*; we do not stop *uv*.
* :func:`request_deferred_update` -- schedules the install to run from a detached
  helper **after** every kbagent process has exited, so nothing holds the target
  environment open. The result is reported by the next kbagent startup.

The helper is a PowerShell one-liner: Windows PowerShell 5.1 is an in-box OS
component on every supported Windows 10/11 edition, and ``ExecutionPolicy``
governs script *files*, not ``-Command`` strings, so no policy can block it.
Its script text is built by pure functions so the exact contract is unit-testable
on POSIX CI.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import platformdirs

from .constants import (
    DEFERRED_UPDATE_ABANDONED_MARKER,
    DEFERRED_UPDATE_EXIT_FILENAME,
    DEFERRED_UPDATE_LOG_FILENAME,
    DEFERRED_UPDATE_MARKER_FILENAME,
    DEFERRED_UPDATE_MAX_WAIT_SECONDS,
    DEFERRED_UPDATE_POLL_SECONDS,
    DEFERRED_UPDATE_PROCESS_NAME,
    DEFERRED_UPDATE_STALE_SECONDS,
    ENV_DEFER_UPDATE,
)

logger = logging.getLogger(__name__)

# Windows-only process-creation flags. Absent on POSIX, where the detached
# helper path is never taken; `getattr` keeps the module importable there.
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

# Fallback location of the in-box Windows PowerShell, used when PATH lookup
# fails (a stripped PATH in a service context, a broken shell profile).
_WINDOWS_POWERSHELL_RELATIVE = r"System32\WindowsPowerShell\v1.0\powershell.exe"


class InstallStatus(Enum):
    """Terminal state of one install-command execution.

    ``STILL_RUNNING`` is deliberately not a failure: the installer outran our
    patience, not our expectations. We stopped waiting and left it working.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STILL_RUNNING = "still_running"


@dataclass(frozen=True)
class InstallRun:
    """Outcome of running one install command, plus whatever it printed."""

    status: InstallStatus
    exit_code: int | None
    output: str
    log_path: Path


@dataclass(frozen=True)
class DeferredUpdateRequest:
    """Everything the detached helper needs; nothing it can re-derive."""

    from_version: str
    target_version: str
    install_command: tuple[str, ...]
    recovery_command: str | None


class DeferredUpdateStatus(Enum):
    """What became of a previously scheduled deferred update."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"
    LOST = "lost"


@dataclass(frozen=True)
class DeferredUpdateReport:
    """A finished deferred update, ready to be reported to the user once."""

    status: DeferredUpdateStatus
    from_version: str
    target_version: str
    exit_code: int | None
    recovery_command: str | None
    log_path: Path


def state_dir() -> Path:
    """Directory holding deferred-update bookkeeping files.

    The global config directory, matching the version cache in
    :mod:`.auto_update` -- deferred state must outlive the process that
    scheduled it and must be found by an unrelated later invocation.
    """
    return Path(platformdirs.user_config_dir("keboola-agent-cli"))


def _marker_path() -> Path:
    return state_dir() / DEFERRED_UPDATE_MARKER_FILENAME


def _exit_path() -> Path:
    return state_dir() / DEFERRED_UPDATE_EXIT_FILENAME


def log_path() -> Path:
    """Path the installer's combined output is appended to."""
    return state_dir() / DEFERRED_UPDATE_LOG_FILENAME


def should_defer() -> bool:
    """Whether the install must run out-of-process rather than inline.

    Windows by default, because there the in-place venv removal performed by
    ``uv tool install`` cannot survive the running process holding it open.
    ``KBAGENT_DEFER_UPDATE`` forces the decision either way -- ``1`` to exercise
    the deferred path on POSIX (how CI covers it end to end), ``0`` as an escape
    hatch for a Windows user who would rather take the in-place risk.
    """
    raw = os.environ.get(ENV_DEFER_UPDATE, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return os.name == "nt"


def run_install(command: tuple[str, ...], *, timeout: float) -> InstallRun:
    """Run an install command, waiting at most ``timeout`` -- and never killing it.

    ``subprocess.run(..., timeout=...)`` kills the child when the deadline
    passes. For a package installer that is the worst possible response: uv is
    then terminated part-way through recreating a venv, which leaves the same
    half-deleted environment a Windows file lock would (issue #528). Here the
    deadline only bounds *our* waiting; the installer keeps running and finishes
    the transaction it started.

    Output goes to :func:`log_path` rather than a pipe, for the same reason --
    ``PIPE`` would deadlock once the OS buffer filled and we stopped reading,
    and a child we intend to outlive must not be writing into a pipe whose
    reader is gone.

    Args:
        command: Full argv of the installer.
        timeout: Seconds to wait before giving up on the wait (not on the child).

    Returns:
        :class:`InstallRun` describing the outcome and the captured output.
    """
    destination = log_path()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.debug("Could not create the update log directory", exc_info=True)

    try:
        with destination.open("a", encoding="utf-8", errors="replace") as sink:
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return InstallRun(
                    status=InstallStatus.STILL_RUNNING,
                    exit_code=None,
                    output=_tail(destination),
                    log_path=destination,
                )
    except OSError as exc:
        return InstallRun(
            status=InstallStatus.FAILED,
            exit_code=None,
            output=str(exc),
            log_path=destination,
        )

    return InstallRun(
        status=InstallStatus.SUCCEEDED if exit_code == 0 else InstallStatus.FAILED,
        exit_code=exit_code,
        output=_tail(destination),
        log_path=destination,
    )


def _tail(path: Path, *, max_chars: int = 4000) -> str:
    """Return the end of the install log, or an empty string if unreadable."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:].strip()


def quote_for_powershell(value: str) -> str:
    """Wrap ``value`` in a PowerShell single-quoted literal.

    Single quotes are the only PowerShell string form with no escape sequences
    at all -- no ``$`` expansion, no backtick escapes -- so a Windows path full
    of backslashes passes through untouched. The one character needing care is
    the quote itself, which is escaped by doubling.
    """
    return "'" + value.replace("'", "''") + "'"


def build_waiter_script(
    request: DeferredUpdateRequest,
    *,
    pid: int,
    exit_file: Path,
    install_log: Path,
    max_wait_seconds: int = DEFERRED_UPDATE_MAX_WAIT_SECONDS,
    poll_seconds: int = DEFERRED_UPDATE_POLL_SECONDS,
    process_name: str = DEFERRED_UPDATE_PROCESS_NAME,
    abandoned_marker: str = DEFERRED_UPDATE_ABANDONED_MARKER,
) -> str:
    """Build the PowerShell program the detached helper runs.

    It waits twice, deliberately. ``Wait-Process -Id`` covers the process that
    scheduled the update even when it is not named ``kbagent`` (``python -m
    keboola_agent_cli``); the ``Get-Process -Name`` loop then covers every
    *other* kbagent still holding the environment open, which is the case the
    PID wait cannot see. Only when both are clear is the installer allowed to
    touch the venv.

    If kbagent is still running when the window closes -- a ``kbagent serve``
    left open for the day -- the helper installs **nothing** and records that
    it gave up. Doing nothing is always safe here; the update simply retries on
    a later run.

    The installer's output is appended through ``UTF8Encoding($false)`` rather
    than a ``*>>`` redirection. Windows PowerShell 5.1 writes redirections as
    **UTF-16LE with a BOM**, which made the shared install log unreadable as the
    UTF-8 every other writer and reader of that file assumes -- caught by the
    Windows CI job, which is the only place the helper actually executes.

    Returns:
        A self-contained script suitable for ``powershell.exe -Command``.
    """
    quoted_argv = " ".join(quote_for_powershell(part) for part in request.install_command)
    exit_literal = quote_for_powershell(str(exit_file))
    log_literal = quote_for_powershell(str(install_log))
    name_literal = quote_for_powershell(process_name)
    abandoned_literal = quote_for_powershell(abandoned_marker)

    return "\n".join(
        [
            f"$exitFile = {exit_literal}",
            f"$logFile = {log_literal}",
            "try {",
            f"  Wait-Process -Id {pid} -Timeout {max_wait_seconds} -ErrorAction SilentlyContinue",
            f"  $deadline = (Get-Date).AddSeconds({max_wait_seconds})",
            f"  while ((Get-Process -Name {name_literal} -ErrorAction SilentlyContinue)"
            " -and ((Get-Date) -lt $deadline)) {",
            f"    Start-Sleep -Seconds {poll_seconds}",
            "  }",
            f"  if (Get-Process -Name {name_literal} -ErrorAction SilentlyContinue) {{",
            f"    Set-Content -LiteralPath $exitFile -Value {abandoned_literal}",
            "    exit 0",
            "  }",
            # -Width keeps Out-String from wrapping uv's output at the default
            # console width and mangling long requirement lines in the log.
            f"  $output = (& {quoted_argv} 2>&1 | Out-String -Width 4096)",
            # Captured before anything else runs, so nothing can clobber it.
            "  $code = $LASTEXITCODE",
            "  [System.IO.File]::AppendAllText("
            "$logFile, $output, (New-Object System.Text.UTF8Encoding $false))",
            "  Set-Content -LiteralPath $exitFile -Value ([string]$code)",
            "} catch {",
            "  [System.IO.File]::AppendAllText("
            "$logFile, ($_ | Out-String -Width 4096), (New-Object System.Text.UTF8Encoding $false))",
            "  Set-Content -LiteralPath $exitFile -Value 'failed'",
            "}",
        ]
    )


def resolve_powershell() -> str | None:
    """Locate an interpreter for the detached helper.

    Prefers in-box Windows PowerShell, falling back to PowerShell 7 (``pwsh``)
    and finally to the absolute ``System32`` path for the case where PATH has
    been stripped. ``None`` means we must not schedule anything -- the caller
    then tells the user the exact command instead of risking an in-place
    install.
    """
    for candidate in ("powershell", "pwsh"):
        found = shutil.which(candidate)
        if found:
            return found
    # `os.environ` upper-cases its keys on Windows, so the canonical spelling
    # of `%SystemRoot%` here is the all-caps one.
    system_root = os.environ.get("SYSTEMROOT")
    if system_root:
        fallback = Path(system_root) / _WINDOWS_POWERSHELL_RELATIVE
        if fallback.is_file():
            return str(fallback)
    return None


def build_helper_command(powershell: str, script: str) -> list[str]:
    """Build the argv that launches the waiter script detached from any console."""
    return [
        powershell,
        "-NoProfile",
        "-NonInteractive",
        "-WindowStyle",
        "Hidden",
        "-Command",
        script,
    ]


def is_update_pending() -> bool:
    """Whether a scheduled deferred update is still waiting to run.

    Single-flight guard: every kbagent startup evaluates the same update, so
    without this a handful of shells opened in a row would each spawn their own
    helper and they would race each other into the very corruption this module
    exists to prevent.
    """
    marker = _read_marker()
    if marker is None:
        return False
    if _exit_path().is_file():
        return False
    return not _marker_is_stale(marker)


def _marker_is_stale(marker: dict) -> bool:
    try:
        return (time.time() - float(marker["requested_at"])) > DEFERRED_UPDATE_STALE_SECONDS
    except (KeyError, TypeError, ValueError):
        return True


def _read_marker() -> dict | None:
    path = _marker_path()
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def request_deferred_update(request: DeferredUpdateRequest) -> bool:
    """Schedule ``request`` to be installed once no kbagent process is left.

    Writes the marker *before* spawning, so a helper that dies immediately still
    leaves evidence a later run can report rather than a silent no-op.

    Returns:
        True when a helper was spawned (or one is already pending). False when
        scheduling is impossible -- no PowerShell, unwritable state directory --
        which the caller must surface as "run this command yourself", never as a
        reason to fall back to an in-place install.
    """
    if is_update_pending():
        return True

    powershell = resolve_powershell()
    if powershell is None:
        logger.debug("No PowerShell interpreter found; cannot defer the update")
        return False

    exit_file = _exit_path()
    install_log = log_path()
    script = build_waiter_script(
        request,
        pid=os.getpid(),
        exit_file=exit_file,
        install_log=install_log,
    )

    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        exit_file.unlink(missing_ok=True)
        _marker_path().write_text(
            json.dumps(
                {
                    "requested_at": time.time(),
                    "pid": os.getpid(),
                    "from_version": request.from_version,
                    "target_version": request.target_version,
                    "recovery_command": request.recovery_command,
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        logger.debug("Could not persist the deferred-update marker", exc_info=True)
        return False

    try:
        subprocess.Popen(
            build_helper_command(powershell, script),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP,
        )
    except OSError:
        logger.debug("Could not spawn the deferred-update helper", exc_info=True)
        _marker_path().unlink(missing_ok=True)
        return False

    return True


def collect_finished_deferred_update() -> DeferredUpdateReport | None:
    """Consume the result of a previously scheduled deferred update.

    Called once per startup. Clears the bookkeeping files so a result is
    reported exactly once, whatever it was.

    Returns:
        A report when a scheduled update has reached a terminal state, or None
        when nothing was scheduled or the helper is still waiting.
    """
    marker = _read_marker()
    if marker is None:
        return None

    exit_file = _exit_path()
    raw_exit: str | None = None
    if exit_file.is_file():
        try:
            raw_exit = exit_file.read_text(encoding="utf-8").strip()
        except OSError:
            raw_exit = None
    elif not _marker_is_stale(marker):
        return None  # helper is still waiting for kbagent to exit

    _clear_deferred_state()

    status, exit_code = _classify_exit(raw_exit, marker_is_stale=raw_exit is None)
    return DeferredUpdateReport(
        status=status,
        from_version=str(marker.get("from_version") or "unknown"),
        target_version=str(marker.get("target_version") or "unknown"),
        exit_code=exit_code,
        recovery_command=marker.get("recovery_command"),
        log_path=log_path(),
    )


def _classify_exit(
    raw_exit: str | None, *, marker_is_stale: bool
) -> tuple[DeferredUpdateStatus, int | None]:
    """Map the helper's exit file to a status.

    Anything that is neither a clean exit code nor the explicit "gave up"
    marker counts as a failure -- an unparseable result is exactly the case
    where the user needs the recovery command, so it must not be optimistic.
    """
    if raw_exit is None:
        return (DeferredUpdateStatus.LOST if marker_is_stale else DeferredUpdateStatus.FAILED), None
    if raw_exit == DEFERRED_UPDATE_ABANDONED_MARKER:
        return DeferredUpdateStatus.ABANDONED, None
    try:
        exit_code = int(raw_exit)
    except ValueError:
        return DeferredUpdateStatus.FAILED, None
    if exit_code == 0:
        return DeferredUpdateStatus.SUCCEEDED, 0
    return DeferredUpdateStatus.FAILED, exit_code


def _clear_deferred_state() -> None:
    """Remove the marker and exit files; the log is kept for diagnosis."""
    for path in (_marker_path(), _exit_path()):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not clear deferred-update state at %s", path, exc_info=True)
