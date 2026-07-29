"""Tests for the self-reinstall runner (issue #528).

The original corruption is not reproducible here -- it needs a real `uv tool
install` losing a race against a real Windows file lock on a live environment.
So most contracts are pinned structurally: the waiter script's text, the
scheduling bookkeeping, and the one behaviour observable everywhere, that a slow
installer is never killed.

``TestWaiterScriptOnWindows`` goes further and *executes* the generated
PowerShell on the windows-latest CI runner, which is the only place it runs at
all. That is not decoration -- it is what caught the helper writing its log as
UTF-16LE.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from keboola_agent_cli.constants import (
    DEFERRED_UPDATE_ABANDONED_MARKER,
    DEFERRED_UPDATE_EXIT_FILENAME,
    DEFERRED_UPDATE_MARKER_FILENAME,
    DEFERRED_UPDATE_STALE_SECONDS,
    ENV_DEFER_UPDATE,
)
from keboola_agent_cli.update_runner import (
    DeferredUpdateRequest,
    DeferredUpdateStatus,
    InstallStatus,
    build_helper_command,
    build_waiter_script,
    collect_finished_deferred_update,
    is_update_pending,
    quote_for_powershell,
    request_deferred_update,
    resolve_powershell,
    run_install,
    should_defer,
)

REQUEST = DeferredUpdateRequest(
    from_version="0.76.3",
    target_version="0.76.4",
    install_command=(
        r"C:\tools\uv.exe",
        "tool",
        "install",
        "--force",
        "--reinstall",
        "keboola-cli[server] @ https://example.invalid/keboola_cli-0.76.4-py3-none-any.whl",
    ),
    recovery_command="uv tool install --force --reinstall ...",
)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every bookkeeping file at a scratch directory."""
    monkeypatch.setattr("keboola_agent_cli.update_runner.state_dir", lambda: tmp_path)


class TestShouldDefer:
    """Which platforms must not install into their own live environment."""

    def test_defaults_to_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_DEFER_UPDATE, raising=False)
        monkeypatch.setattr("keboola_agent_cli.update_runner.os.name", "nt")
        assert should_defer() is True

    def test_defaults_off_on_posix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_DEFER_UPDATE, raising=False)
        monkeypatch.setattr("keboola_agent_cli.update_runner.os.name", "posix")
        assert should_defer() is False

    @pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
    def test_env_forces_on(self, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_DEFER_UPDATE, value)
        monkeypatch.setattr("keboola_agent_cli.update_runner.os.name", "posix")
        assert should_defer() is True

    @pytest.mark.parametrize("value", ["0", "false", "NO", "off"])
    def test_env_forces_off(self, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_DEFER_UPDATE, value)
        monkeypatch.setattr("keboola_agent_cli.update_runner.os.name", "nt")
        assert should_defer() is False


class TestRunInstall:
    """The installer subprocess is waited on, never terminated."""

    def test_success(self) -> None:
        run = run_install((sys.executable, "-c", "print('ok')"), timeout=30)
        assert run.status is InstallStatus.SUCCEEDED
        assert run.exit_code == 0
        assert "ok" in run.output

    def test_failure_reports_exit_code_and_output(self) -> None:
        run = run_install(
            (sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"),
            timeout=30,
        )
        assert run.status is InstallStatus.FAILED
        assert run.exit_code == 3
        assert "boom" in run.output

    def test_missing_executable_is_a_failure_not_a_crash(self, tmp_path: Path) -> None:
        run = run_install((str(tmp_path / "definitely-not-here"),), timeout=30)
        assert run.status is InstallStatus.FAILED
        assert run.exit_code is None

    def test_slow_installer_is_left_running(self, tmp_path: Path) -> None:
        """The deadline bounds our waiting, not the installer's work.

        ``subprocess.run(timeout=...)`` would kill the child here. For a package
        installer that is the corruption itself: uv terminated mid-transaction
        leaves the same half-removed environment a Windows lock does (#528).
        """
        sentinel = tmp_path / "finished.txt"
        program = (
            "import time, pathlib, sys;"
            "print('early-output', flush=True);"
            "time.sleep(1.5);"
            f"pathlib.Path({str(sentinel)!r}).write_text('done')"
        )

        run = run_install((sys.executable, "-c", program), timeout=0.5)

        assert run.status is InstallStatus.STILL_RUNNING
        assert run.exit_code is None
        assert not sentinel.exists(), "precondition: the child cannot have finished yet"
        # `run.output` was read while the child still holds the log open and the
        # parent has already closed its own handle. On Windows that combination
        # depends on file-sharing semantics, so this assertion is what proves it
        # -- the Windows CI job runs this suite.
        assert "early-output" in run.output

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not sentinel.exists():
            time.sleep(0.1)
        assert sentinel.exists(), "the installer was killed instead of being left to finish"


class TestQuoteForPowershell:
    """Single-quoted PowerShell literals: no expansion, quotes doubled."""

    def test_windows_path_passes_through_untouched(self) -> None:
        assert quote_for_powershell(r"C:\tools\uv.exe") == r"'C:\tools\uv.exe'"

    def test_embedded_quote_is_doubled(self) -> None:
        assert quote_for_powershell("it's") == "'it''s'"

    def test_dollar_is_not_expanded_because_it_is_single_quoted(self) -> None:
        assert quote_for_powershell("$env:PATH") == "'$env:PATH'"


class TestBuildWaiterScript:
    """The helper's contract: wait for every kbagent, then install once."""

    @pytest.fixture
    def script(self, tmp_path: Path) -> str:
        return build_waiter_script(
            REQUEST,
            pid=4321,
            exit_file=tmp_path / DEFERRED_UPDATE_EXIT_FILENAME,
            install_log=tmp_path / "install.log",
            max_wait_seconds=900,
            poll_seconds=2,
            process_name="kbagent",
        )

    def test_waits_for_the_scheduling_process_by_pid(self, script: str) -> None:
        """Covers a scheduler not named `kbagent` (`python -m keboola_agent_cli`)."""
        assert "Wait-Process -Id 4321 -Timeout 900" in script

    def test_then_waits_for_every_other_kbagent(self, script: str) -> None:
        """The PID wait cannot see a second shell holding the same venv open."""
        assert "Get-Process -Name 'kbagent'" in script
        assert "Start-Sleep -Seconds 2" in script

    def test_gives_up_without_installing_when_kbagent_never_exits(self, script: str) -> None:
        """Doing nothing is always safe; a partial install never is."""
        abandon = f"Set-Content -LiteralPath $exitFile -Value '{DEFERRED_UPDATE_ABANDONED_MARKER}'"
        assert abandon in script
        # The abandon branch must precede the installer invocation.
        assert script.index(abandon) < script.index("$LASTEXITCODE")

    def test_installs_the_exact_prepared_command(self, script: str) -> None:
        expected = " ".join(quote_for_powershell(part) for part in REQUEST.install_command)
        assert f"$output = (& {expected} 2>&1 | Out-String -Width 4096)" in script

    def test_writes_the_log_as_utf8_not_a_redirection(self, script: str) -> None:
        """PowerShell 5.1 redirections are UTF-16LE; the log must be UTF-8.

        Every other writer and reader of that file assumes UTF-8, so a `*>>`
        redirection made it unreadable (caught by the Windows CI job).
        """
        assert "*>>" not in script
        assert "New-Object System.Text.UTF8Encoding $false" in script

    def test_records_the_installer_exit_code(self, script: str) -> None:
        # Captured immediately after the installer, before anything else runs.
        assert "$code = $LASTEXITCODE" in script
        assert "Set-Content -LiteralPath $exitFile -Value ([string]$code)" in script

    def test_a_powershell_level_error_is_recorded_as_a_failure(self, script: str) -> None:
        assert "Set-Content -LiteralPath $exitFile -Value 'failed'" in script


class TestBuildHelperCommand:
    """The helper must not read a profile, prompt, or show a window."""

    def test_flags(self) -> None:
        argv = build_helper_command("powershell.exe", "Write-Output 1")
        assert argv[0] == "powershell.exe"
        assert "-NoProfile" in argv
        assert "-NonInteractive" in argv
        assert argv[-2:] == ["-Command", "Write-Output 1"]


class TestRequestDeferredUpdate:
    """Scheduling writes evidence first, then spawns a detached helper."""

    @pytest.fixture(autouse=True)
    def _powershell_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "keboola_agent_cli.update_runner.resolve_powershell", lambda: "powershell.exe"
        )

    def test_spawns_detached_and_records_the_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spawned: list[dict] = []

        def fake_popen(argv: list[str], **kwargs: object) -> object:
            spawned.append({"argv": argv, "kwargs": kwargs})
            return object()

        monkeypatch.setattr("keboola_agent_cli.update_runner.subprocess.Popen", fake_popen)

        assert request_deferred_update(REQUEST) is True

        assert len(spawned) == 1
        # No inherited console, no inherited descriptors: the helper has to
        # outlive this process, and a pipe whose reader is gone would block it.
        assert spawned[0]["kwargs"]["close_fds"] is True
        assert spawned[0]["kwargs"]["stdin"] is subprocess.DEVNULL
        assert spawned[0]["kwargs"]["stdout"] is subprocess.DEVNULL

        marker = json.loads((tmp_path / DEFERRED_UPDATE_MARKER_FILENAME).read_text())
        assert marker["from_version"] == "0.76.3"
        assert marker["target_version"] == "0.76.4"
        assert marker["recovery_command"] == REQUEST.recovery_command

    def test_second_request_does_not_spawn_a_second_helper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Single-flight: two helpers would race into the corruption we prevent."""
        spawns = 0

        def fake_popen(argv: list[str], **kwargs: object) -> object:
            nonlocal spawns
            spawns += 1
            return object()

        monkeypatch.setattr("keboola_agent_cli.update_runner.subprocess.Popen", fake_popen)

        assert request_deferred_update(REQUEST) is True
        assert request_deferred_update(REQUEST) is True
        assert spawns == 1

    def test_without_powershell_nothing_is_scheduled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The caller must be told, not silently given an unsafe inline install."""
        monkeypatch.setattr("keboola_agent_cli.update_runner.resolve_powershell", lambda: None)

        assert request_deferred_update(REQUEST) is False
        assert not (tmp_path / DEFERRED_UPDATE_MARKER_FILENAME).exists()

    def test_spawn_failure_clears_the_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def failing_popen(argv: list[str], **kwargs: object) -> object:
            raise OSError("no such file")

        monkeypatch.setattr("keboola_agent_cli.update_runner.subprocess.Popen", failing_popen)

        assert request_deferred_update(REQUEST) is False
        assert not (tmp_path / DEFERRED_UPDATE_MARKER_FILENAME).exists()


class TestCollectFinishedDeferredUpdate:
    """A scheduled update is reported exactly once, and never optimistically."""

    @staticmethod
    def _write_marker(tmp_path: Path, *, age_seconds: float = 0.0) -> None:
        (tmp_path / DEFERRED_UPDATE_MARKER_FILENAME).write_text(
            json.dumps(
                {
                    "requested_at": time.time() - age_seconds,
                    "pid": 1,
                    "from_version": "0.76.3",
                    "target_version": "0.76.4",
                    "recovery_command": "uv tool install --force --reinstall x",
                }
            )
        )

    def test_nothing_scheduled(self) -> None:
        assert collect_finished_deferred_update() is None

    def test_still_waiting_reports_nothing_yet(self, tmp_path: Path) -> None:
        self._write_marker(tmp_path)
        assert collect_finished_deferred_update() is None
        assert (tmp_path / DEFERRED_UPDATE_MARKER_FILENAME).exists()
        assert is_update_pending() is True

    def test_success(self, tmp_path: Path) -> None:
        self._write_marker(tmp_path)
        (tmp_path / DEFERRED_UPDATE_EXIT_FILENAME).write_text("0")

        report = collect_finished_deferred_update()

        assert report is not None
        assert report.status is DeferredUpdateStatus.SUCCEEDED
        assert report.from_version == "0.76.3"
        assert report.target_version == "0.76.4"
        # Reported once: the bookkeeping is gone afterwards.
        assert not (tmp_path / DEFERRED_UPDATE_MARKER_FILENAME).exists()
        assert not (tmp_path / DEFERRED_UPDATE_EXIT_FILENAME).exists()
        assert collect_finished_deferred_update() is None

    def test_non_zero_exit_is_a_failure_carrying_recovery(self, tmp_path: Path) -> None:
        self._write_marker(tmp_path)
        (tmp_path / DEFERRED_UPDATE_EXIT_FILENAME).write_text("2")

        report = collect_finished_deferred_update()

        assert report is not None
        assert report.status is DeferredUpdateStatus.FAILED
        assert report.exit_code == 2
        assert report.recovery_command == "uv tool install --force --reinstall x"

    def test_abandoned_is_not_a_failure(self, tmp_path: Path) -> None:
        """Nothing was installed, so the environment is untouched."""
        self._write_marker(tmp_path)
        (tmp_path / DEFERRED_UPDATE_EXIT_FILENAME).write_text(DEFERRED_UPDATE_ABANDONED_MARKER)

        report = collect_finished_deferred_update()

        assert report is not None
        assert report.status is DeferredUpdateStatus.ABANDONED

    def test_unparseable_result_is_treated_as_a_failure(self, tmp_path: Path) -> None:
        """An unreadable outcome is exactly when the recovery command matters."""
        self._write_marker(tmp_path)
        (tmp_path / DEFERRED_UPDATE_EXIT_FILENAME).write_text("something went sideways")

        report = collect_finished_deferred_update()

        assert report is not None
        assert report.status is DeferredUpdateStatus.FAILED

    def test_helper_that_never_reported_is_reported_as_lost(self, tmp_path: Path) -> None:
        """Machine rebooted mid-wait: report it once rather than wait forever."""
        self._write_marker(tmp_path, age_seconds=DEFERRED_UPDATE_STALE_SECONDS + 60)

        report = collect_finished_deferred_update()

        assert report is not None
        assert report.status is DeferredUpdateStatus.LOST
        assert not (tmp_path / DEFERRED_UPDATE_MARKER_FILENAME).exists()

    def test_a_stale_marker_does_not_block_a_new_schedule(self, tmp_path: Path) -> None:
        self._write_marker(tmp_path, age_seconds=DEFERRED_UPDATE_STALE_SECONDS + 60)
        assert is_update_pending() is False


@pytest.mark.skipif(os.name != "nt", reason="exercises the real PowerShell helper")
class TestWaiterScriptOnWindows:
    """Run the generated helper for real on the Windows CI runner.

    Everything above pins the script's *text*. This runs it, which is the only
    way to know the PowerShell actually parses and behaves -- the script is
    authored on machines that cannot execute it.
    """

    @staticmethod
    def _already_exited_pid() -> int:
        """A PID that is guaranteed gone, so `Wait-Process` takes its error path."""
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait()
        return process.pid

    def _run_helper(self, script: str) -> None:
        powershell = resolve_powershell()
        assert powershell is not None, "Windows must always provide an in-box PowerShell"
        subprocess.run(build_helper_command(powershell, script), check=True, timeout=180)

    def test_installer_exit_code_is_recorded(self, tmp_path: Path) -> None:
        exit_file = tmp_path / DEFERRED_UPDATE_EXIT_FILENAME
        install_log = tmp_path / "install.log"
        request = DeferredUpdateRequest(
            from_version="1.0.0",
            target_version="2.0.0",
            install_command=(sys.executable, "-c", "print('installed')"),
            recovery_command=None,
        )

        self._run_helper(
            build_waiter_script(
                request,
                pid=self._already_exited_pid(),
                exit_file=exit_file,
                install_log=install_log,
                max_wait_seconds=60,
                poll_seconds=1,
                # Nothing is named `kbagent` on the runner, so the wait loop
                # falls straight through to the install.
                process_name="kbagent",
            )
        )

        assert exit_file.read_text(encoding="utf-8").strip() == "0"
        # The log must be plain UTF-8. PowerShell 5.1's `*>>` wrote UTF-16LE
        # with a BOM, which `_tail` -- and the user -- read as mojibake.
        assert not install_log.read_bytes().startswith(b"\xff\xfe")
        assert "installed" in install_log.read_text(encoding="utf-8")

    def test_failing_installer_is_reported_not_swallowed(self, tmp_path: Path) -> None:
        exit_file = tmp_path / DEFERRED_UPDATE_EXIT_FILENAME
        request = DeferredUpdateRequest(
            from_version="1.0.0",
            target_version="2.0.0",
            install_command=(sys.executable, "-c", "import sys; sys.exit(3)"),
            recovery_command=None,
        )

        self._run_helper(
            build_waiter_script(
                request,
                pid=self._already_exited_pid(),
                exit_file=exit_file,
                install_log=tmp_path / "install.log",
                max_wait_seconds=60,
                poll_seconds=1,
                process_name="kbagent",
            )
        )

        assert exit_file.read_text(encoding="utf-8").strip() == "3"

    def test_gives_up_without_installing_while_a_process_is_still_running(
        self, tmp_path: Path
    ) -> None:
        """The branch that protects the environment: something is still holding it.

        Watching this very interpreter's own name guarantees the loop keeps
        finding a live process, which is the situation a `kbagent serve` creates.
        """
        exit_file = tmp_path / DEFERRED_UPDATE_EXIT_FILENAME
        sentinel = tmp_path / "must-not-exist.txt"
        request = DeferredUpdateRequest(
            from_version="1.0.0",
            target_version="2.0.0",
            install_command=(
                sys.executable,
                "-c",
                f"import pathlib; pathlib.Path({str(sentinel)!r}).write_text('ran')",
            ),
            recovery_command=None,
        )

        self._run_helper(
            build_waiter_script(
                request,
                pid=self._already_exited_pid(),
                exit_file=exit_file,
                install_log=tmp_path / "install.log",
                max_wait_seconds=3,
                poll_seconds=1,
                process_name=Path(sys.executable).stem,
            )
        )

        assert exit_file.read_text(encoding="utf-8").strip() == DEFERRED_UPDATE_ABANDONED_MARKER
        assert not sentinel.exists(), "the installer must not touch a live environment"


class TestInstallOutputBelongsToItsOwnRun:
    """`output` must be this run's transcript, not the shared log's tail.

    The log is appended to by every update -- the MCP stage writes to it moments
    before the kbagent stage in the very same `kbagent update`, and the Windows
    helper appends to it too. Reporting the tail of the whole file attributed
    someone else's output to this command.
    """

    def test_second_run_output_excludes_the_first(self) -> None:
        first = run_install((sys.executable, "-c", "print('FIRST-RUN-MARKER')"), timeout=30)
        second = run_install((sys.executable, "-c", "print('SECOND-RUN-MARKER')"), timeout=30)

        assert "FIRST-RUN-MARKER" in first.output
        assert "SECOND-RUN-MARKER" in second.output
        assert "FIRST-RUN-MARKER" not in second.output

    def test_log_keeps_every_run_even_though_output_does_not(self, tmp_path: Path) -> None:
        """Trimming the *report* must not trim the *log* -- it is the diagnosis."""
        run_install((sys.executable, "-c", "print('FIRST-RUN-MARKER')"), timeout=30)
        run_install((sys.executable, "-c", "print('SECOND-RUN-MARKER')"), timeout=30)

        log = (tmp_path / "pending_update.log").read_text(encoding="utf-8")
        assert "FIRST-RUN-MARKER" in log
        assert "SECOND-RUN-MARKER" in log

    def test_oversized_log_is_rolled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every update appends here, so the file cannot grow without bound."""
        monkeypatch.setattr("keboola_agent_cli.update_runner.DEFERRED_UPDATE_LOG_MAX_BYTES", 64)
        log = tmp_path / "pending_update.log"
        log.write_text("x" * 500, encoding="utf-8")

        run = run_install((sys.executable, "-c", "print('AFTER-ROLL')"), timeout=30)

        assert "x" * 100 not in log.read_text(encoding="utf-8")
        assert "AFTER-ROLL" in run.output
