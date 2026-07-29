# Design: defer the Windows self-update out of the running environment

**Issue:** [#528](https://github.com/keboola/cli/issues/528)
**Supersedes:** `2026-07-23-issue-528-safe-self-update-design.md` (shipped in v0.76.2)
**Status:** Implemented
**Target:** `kbagent update` and the startup auto-update hook

## Why the v0.76.2 fix was not enough

v0.76.2 moved every network call, probe, and command construction ahead of any
mutation, updated the independent MCP environment first, and made the kbagent
reinstall a terminal `uv tool install --force --reinstall` from an exact
release artifact. That was a correct fix for a real ordering bug -- the missing
`certifi/cacert.pem` in incident 2 -- and it did not fix the reported
corruption. The reporter hit it again on v0.76.2 -> v0.76.3.

The reason is that ordering was never the mechanism.

`uv tool install` recreates a tool environment **in place**. From
`crates/uv-tool/src/lib.rs`, `create_environment` removes any existing
environment and then calls `create_venv` at the same path. There is no
temporary build directory, no atomic swap, and no rollback.

On POSIX this is safe by accident of the filesystem semantics: unlinking a file
another process holds open leaves that process's inode intact, so a running
kbagent survives having its own venv deleted and rebuilt underneath it. That is
why nobody on macOS or Linux ever saw this.

On Windows it cannot work. uv's `kbagent.exe` trampoline loads the tool venv's
interpreter in-process, so those files are locked for as long as kbagent runs.
The removal deletes every file it can, reaches a locked one, and aborts. What
is left is not a mixture of old and new distributions -- it is a **partially
deleted** venv, which is exactly what the reported symptoms describe: `rich`
still present but `rich/_windows.py` gone, `typer` still present but
`typer/rich_utils.py` gone. Upstream tracks the same class of failure as
astral-sh/uv#11930 (`uv tool upgrade` of an in-use tool leaves the environment
inconsistent and the receipt lying about it).

Running the installer from inside the environment it replaces is therefore
unsafe on Windows *by construction*, with any combination of uv flags.

### The second, independent corruption vector

Both update paths ran the installer through `subprocess.run(..., timeout=...)`,
which **kills the child** when the deadline expires -- on Windows a
`TerminateProcess`. Killing uv part-way through recreating a venv produces the
same half-deleted environment a file lock does, from our own code, on every
platform. The default deadline was 300s; a cold resolution of the `[server]`
extra on a Windows machine with real-time AV scanning is not reliably under
that.

## Goals

- Never run the installer from a process whose own environment is the target.
- Never terminate an installer mid-transaction.
- Never fall back to the unsafe path when the safe one is unavailable.
- Keep the POSIX behaviour that demonstrably works today.
- Report a deferred outcome exactly once, with recovery guidance on failure.
- Be verifiable in CI on macOS/Linux, where the failure cannot be reproduced.

## Non-goals

- Making `uv tool install` atomic. That is uv's to own.
- Updating while a long-lived kbagent (`serve`, `repl`) refuses to exit. The
  helper declines and retries later; declining is always safe.
- A persistent background update daemon.
- Reworking the MCP environment, which is separate and never the one we run from.

## Design

### 1. `update_runner.run_install` -- bound the wait, not the installer

A single execution helper for both entry points. Output goes to a log file
rather than a pipe (a pipe whose reader is gone would block a child we intend
to outlive), and on timeout it returns `STILL_RUNNING` **without killing**. The
caller reports that the install continues in the background and deliberately
offers no recovery command: a second installer aimed at an environment a live
uv is rewriting is the corruption, not the cure.

### 2. `update_runner.request_deferred_update` -- install after we are gone

On Windows (`should_defer()`, overridable with `KBAGENT_DEFER_UPDATE`) the
prepared install command is handed to a detached helper:

1. Write a marker file first, so a helper that dies immediately still leaves
   evidence a later run can report instead of a silent no-op.
2. Spawn `powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden
   -Command <script>` with `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`,
   `close_fds=True`, and all stdio at `DEVNULL`.
3. The script waits twice -- `Wait-Process -Id <pid>` for the scheduling
   process (which may not be named `kbagent`, e.g. `python -m
   keboola_agent_cli`), then a `Get-Process -Name kbagent` loop for every other
   kbagent holding the environment open -- and only then runs the installer,
   recording `$LASTEXITCODE`.
4. If kbagent is still running when the window closes, it installs **nothing**
   and records that it gave up.

Windows PowerShell 5.1 is an in-box OS component on every supported Windows
10/11 edition and cannot be uninstalled, and `ExecutionPolicy` governs script
*files*, not `-Command` strings, so no policy can block the helper. If no
interpreter is found at all, scheduling fails and the caller prints the exact
install command -- it never falls back to the in-place install.

A single-flight guard (`is_update_pending`) stops several shells opened in a
row from each spawning a helper and racing each other into the corruption.

### 3. Report on the next launch

The process that schedules an update exits before it runs, so it can never
report it. `report_finished_deferred_update()` runs at the top of
`maybe_auto_update`, **before** the skip gates -- the user is owed the outcome
even on a run that will not update anything (a dev install, an opt-out,
`kbagent version`). Success also prints the changelog the in-place path prints
after its re-exec. The bookkeeping files are cleared, so a result is reported
exactly once.

Outcomes are deliberately distinguished:

| Outcome | Meaning | Recovery offered |
|---|---|---|
| `SUCCEEDED` | installed | no |
| `ABANDONED` | kbagent never exited; nothing installed | no -- nothing to recover |
| `FAILED` | installer exited non-zero, or an unreadable result | yes |
| `LOST` | helper never reported and the marker went stale | yes |

An unparseable result counts as a failure: that is precisely when the user
needs the recovery command, so it must not be optimistic.

### 4. POSIX is untouched

`should_defer()` is False there, so the inline install plus `os.execvpe`
re-exec stays exactly as it is. `execve` genuinely replaces the process image,
and unlinking open files is safe -- the majority path keeps its instant
upgrade with no new failure mode.

## Implementation map

- `src/keboola_agent_cli/update_runner.py` (new) -- `run_install`, the deferred
  scheduler, the waiter-script builders, and the marker/report lifecycle.
  A new module rather than growth in `version_service.py`, which is already
  past the 1000-LOC soft ceiling.
- `src/keboola_agent_cli/constants.py` -- deferred-update filenames, wait and
  poll windows, staleness horizon, `KBAGENT_DEFER_UPDATE`.
- `src/keboola_agent_cli/auto_update.py` -- report a finished deferred update;
  schedule instead of installing when deferring; truthful timeout banner.
- `src/keboola_agent_cli/services/version_service.py` -- `_update_kbagent`
  schedules on Windows, reports `deferred`, and never inline-installs there;
  `_compose_update_summary` renders `(scheduled)` rather than `FAILED`.
- `src/keboola_agent_cli/commands/version.py` -- surface the actionable stage
  message in human mode.

## Validation

Unit-testable on POSIX CI, and covered:

- `run_install` leaves a slow child alive (a real subprocess writing a sentinel
  after the wait expires) -- the one behaviour that *is* directly observable
  everywhere.
- The waiter script's exact contract: PID wait, process-name loop, give-up
  branch ordering, the quoted install argv, exit-code recording.
- Scheduling: marker contents, detached spawn flags, single-flight, no-helper
  refusal, spawn-failure cleanup.
- Report lifecycle for every outcome, reported once.
- `kbagent update` on the deferred platform never reaches `run_install`.
- POSIX still installs inline and re-execs.

Not reproducible in CI, and stated as such: the Windows file lock itself.
Before release, on Windows 11 with `uv tool install "keboola-cli[server] @
<previous release wheel>"`: run `kbagent update`, confirm it reports
`(scheduled)` and that the next launch reports success; repeat through the
startup hook; and confirm that with `kbagent serve` left running the update is
reported as skipped with the environment intact.

## Risks and follow-up

- The helper is a detached PowerShell process. Enterprise EDR may flag or block
  that; the failure mode is benign (scheduling fails, the user is told the
  command) but it should be watched for in the field.
- `Get-Process -Name kbagent` does not see a kbagent running as
  `python -m keboola_agent_cli`. The PID wait covers the scheduling process;
  another such process running concurrently is not covered.
- The standalone PyInstaller distribution (Chocolatey / `cli-dist`) has no
  `sys.frozen` guard anywhere in `src/`, so a frozen binary would still plan a
  `uv tool install`. Out of scope here; filed separately.
