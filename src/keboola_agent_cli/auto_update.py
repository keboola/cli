"""Auto-update module for kbagent CLI.

Checks for updates on startup, downloads the new version, and re-execs
the same command in the updated version (similar to Claude Code).

All output goes to sys.stderr.write() since OutputFormatter is not yet
initialized when this runs. The entire flow is wrapped in a blanket
try/except so it NEVER crashes the CLI.
"""

import enum
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from importlib.metadata import distribution
from pathlib import Path
from typing import TYPE_CHECKING

import platformdirs

from . import __version__
from .changelog import ENV_UPDATED_FROM, format_whats_new
from .constants import (
    AUTO_UPDATE_CHECK_INTERVAL,
    DEPRECATION_NOTICE_FINAL_WINDOW_SECONDS,
    DEPRECATION_NOTICE_INTERVAL_SECONDS,
    ENV_AUTO_UPDATE,
    ENV_SKIP_UPDATE,
    VERSION_CACHE_FILENAME,
    VERSION_CHECK_TIMEOUT,
)
from .frozen_dist import FrozenDistribution, detect_frozen_distribution, is_frozen_build
from .services.version_service import (
    GatedRelease,
    KbagentUpdatePlan,
    NextMajorRelease,
    _is_up_to_date,
    _major_of,
    build_hardlink_retry_command,
    build_kbagent_upgrade_command,
    fetch_gated_release,
    get_update_timeout,
    next_major_cache_keys,
    next_major_from_cache,
    prepare_kbagent_update_plan,
    resolve_kbagent_wheel_url,
    summarize_install_failure,
)
from .update_notice import DeprecationNotice, NoticeMode, build_notice, print_once, register
from .update_runner import (
    DeferredUpdateReport,
    DeferredUpdateRequest,
    DeferredUpdateStatus,
    InstallStatus,
    collect_finished_deferred_update,
    pending_update_target,
    request_deferred_update,
    run_install,
    should_defer,
)

if TYPE_CHECKING:
    from .output import OutputFormatter

logger = logging.getLogger(__name__)


class UpdateOutcome(enum.Enum):
    """Outcome of a single kbagent self-update attempt (issue #353).

    Distinguishes a build/install TIMEOUT (the git+ source build outran the
    timeout -- not a real failure; the next run picks it up) from a genuine
    FAILED install, so the startup hook stops printing a misleading
    "Auto-update failed" banner when the install is merely slow.
    """

    SUCCESS = "success"
    TIMEOUT = "timeout"
    FAILED = "failed"


@dataclass(frozen=True)
class UpdateResult:
    """An :class:`UpdateOutcome` plus what the installer printed when it failed (issue #771)."""

    outcome: UpdateOutcome
    output: str = ""


# Process-level sentinel for the auto-update flow.
#
# Bug D fix from issue #263: ``kbagent repl`` re-enters the entire CLI
# (and therefore ``main()`` -> ``maybe_auto_update()``) on every prompt
# iteration. Pre-fix, the auto-update banner re-fired once per command
# typed at the prompt -- one fetch, one (potentially failing) upgrade
# attempt, one stderr write. The sentinel short-circuits subsequent
# in-process invocations after the first.
#
# Re-exec'd processes (kbagent self-upgrade -> ``execvpe`` to new binary)
# start with a fresh sentinel because the module is reloaded into a new
# Python interpreter; the re-exec'd run then short-circuits as up to date.
_AUTO_UPDATE_RAN: bool = False


def _get_cache_path() -> Path:
    """Return path to the version cache file.

    Uses the global config directory (~/.config/keboola-agent-cli/).
    """
    config_dir = Path(platformdirs.user_config_dir("keboola-agent-cli"))
    return config_dir / VERSION_CACHE_FILENAME


def _read_cache() -> dict | None:
    """Read the version cache file.

    Returns:
        Parsed dict with ``last_check`` and ``latest_version`` (both
        required), or None if the file is missing, unreadable, or corrupt.
        Deliberately tolerant of extra keys: a cache file written by an
        older kbagent carries fields this version no longer knows about,
        and must still load rather than be rejected as corrupt (which would
        force a needless fetch on the first run after an upgrade).
    """
    cache_path = _get_cache_path()
    try:
        if not cache_path.is_file():
            return None
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "last_check" in data and "latest_version" in data:
            return data
        return None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _write_cache(latest_version: str | None, *, next_major_keys: dict | None = None) -> None:
    """Write the version cache file.

    Args:
        latest_version: kbagent latest version. Falls back to the running
            interpreter's ``__version__`` when None -- the caller is in a
            re-exec'd process where the update stage was skipped and we
            still want the TTL to tick. Never a version of a higher major:
            clients older than the major gate read this file and would
            install it (#791 section 4.3).
        next_major_keys: The next-major keys (``next_major_version``,
            ``next_major_release_id``, ``next_major_published_at``,
            ``checked_at``) from :func:`next_major_cache_keys`.
    """
    if latest_version is None:
        # Re-exec path: persist the running version so cache_is_fresh
        # logic on the NEXT run still has a kbagent-side anchor.
        latest_version = __version__
    payload: dict = {
        "last_check": time.time(),
        "latest_version": latest_version,
        **(next_major_keys or {}),
    }
    previous = _read_cache()
    if previous and "notice_shown_at" in previous:
        payload["notice_shown_at"] = previous["notice_shown_at"]
    _write_cache_payload(payload)


def _mark_notice_shown(now: float) -> None:
    """Record when the human deprecation notice was printed (once per 24 hours)."""
    cache = _read_cache()
    if cache is None:
        return
    _write_cache_payload({**cache, "notice_shown_at": now})


def _write_cache_payload(payload: dict) -> None:
    """Replace the cache file with ``payload`` in one atomic step."""
    cache_path = _get_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace (issue #271 sec-12): write a temp file next to the
        # cache, then rename it over the cache in one step. A reader never sees
        # a partial file, and a failed write keeps the previous cache. Each
        # writer has its own temp file, so parallel kbagent starts need no
        # lock: the last rename wins, and each value is a complete check result.
        fd, tmp_name = tempfile.mkstemp(
            dir=cache_path.parent, prefix=f".{cache_path.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                tmp_file.write(json.dumps(payload))
            os.replace(tmp_path, cache_path)
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise
    except OSError:
        pass  # Non-critical; next run will re-fetch


def _is_cache_fresh(cache: dict, ttl: int) -> bool:
    """Check whether the cache is still within its TTL.

    Args:
        cache: Parsed cache dict with 'last_check' timestamp.
        ttl: Maximum age in seconds.

    Returns:
        True if the cache is fresh, False if stale.
    """
    try:
        return (time.time() - float(cache["last_check"])) < ttl
    except (KeyError, TypeError, ValueError):
        return False


def _is_dev_install() -> bool:
    """Detect development (editable) installs.

    Returns True if:
    - __version__ is '0.0.0-dev' (PackageNotFoundError fallback), or
    - The package was installed in editable mode (PEP 660 direct_url.json).

    A frozen (PyInstaller) build is NEVER a dev tree, whatever its bundled
    metadata claims -- and the claim is routinely wrong. The release workflow
    freezes from a ``uv run`` sync, which installs the project editable, and
    ``--collect-all keboola_agent_cli`` copies the whole ``.dist-info`` into the
    binary *including* ``direct_url.json`` with ``"editable": true``. Every
    shipped binary therefore looked like a developer checkout here, which
    silently disabled the entire startup hook -- including the frozen-build
    notification that is supposed to replace the self-update for exactly those
    users (see :mod:`keboola_agent_cli.frozen_dist`). The check must come first:
    the bundled marker describes the BUILD MACHINE, not the machine running it.
    """
    if is_frozen_build():
        return False

    if __version__ == "0.0.0-dev":
        return True

    try:
        dist = distribution("keboola-cli")
        direct_url = dist.read_text("direct_url.json")
        if direct_url:
            data = json.loads(direct_url)
            # Editable installs have dir_info.editable = true
            if data.get("dir_info", {}).get("editable", False):
                return True
    except Exception:
        logger.debug("editable-install detection failed", exc_info=True)

    return False


def _should_skip_kbagent_stage() -> bool:
    """Whether the kbagent self-upgrade stage should be skipped.

    Set by the re-exec guard (``KBAGENT_SKIP_UPDATE=1``) so the process a
    self-upgrade re-exec'd into does not try to upgrade itself again. See
    :func:`_should_skip_all` for the wider conditions that gate the whole
    flow, including the cache refresh.
    """
    return os.environ.get(ENV_SKIP_UPDATE) == "1"


def _top_level_subcommand_is_versioning(args: list[str]) -> bool:
    """True iff the top-level subcommand is ``update`` / ``version``.

    Walks past global flags to the first POSITIONAL token -- the top-level Typer
    subcommand -- so it correctly catches the subcommand sitting after global
    flags (``kbagent --json update``) WITHOUT matching a nested ``update`` like
    ``kbagent config update`` / ``flow update`` / ``agent update`` (whose first
    positional is ``config`` / ``flow`` / ``agent``). ``--config-dir`` is the one
    global option that consumes the following token as its value, so its value is
    skipped too; every other global option is a boolean flag.
    """
    value_flags = {"--config-dir"}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in value_flags:
            i += 2  # skip the flag AND its value (e.g. `--config-dir /path`)
            continue
        if arg.startswith("-"):
            i += 1  # boolean flag, or `--flag=value` form; skip just this token
            continue
        return arg.lower() in ("update", "version")
    return False


def _auto_update_disabled() -> bool:
    """``KBAGENT_AUTO_UPDATE`` is one of ``false``, ``0``, ``no``."""
    return os.environ.get(ENV_AUTO_UPDATE, "").lower().strip() in ("false", "0", "no")


def _should_skip_all() -> bool:
    """Whether the entire auto-update flow should be skipped.

    Skip conditions:

    - ``KBAGENT_AUTO_UPDATE`` in ``{false, 0, no}`` (user opt-out).
    - Development / editable install (we never auto-upgrade a dev tree).
    - Current command is ``update`` / ``version`` (those commands handle
      versioning themselves and would loop if auto-update fired here too).

    Notably **does NOT include** the re-exec guard
    ``KBAGENT_SKIP_UPDATE=1`` -- that gates only the self-upgrade itself,
    not the cache refresh. See :func:`_should_skip_kbagent_stage`.
    """
    # User opt-out
    if _auto_update_disabled():
        return True

    # Dev install
    if _is_dev_install():
        return True

    # Skip for `update` / `version` -- they handle versioning themselves and
    # would otherwise double-fire and disagree with the startup banner (Bug 3,
    # issue #353). The subcommand can sit AFTER global flags (`kbagent --json
    # update`), so we resolve the first positional token rather than checking
    # argv[1] -- WITHOUT matching nested `update` subcommands like
    # `kbagent config update`.
    return _top_level_subcommand_is_versioning(sys.argv[1:])


def _should_skip() -> bool:
    """Backwards-compatible alias for the old gate-everything check.

    Pre-v0.30.1 callers (and our own tests) treated this as a single skip
    decision for the whole flow. Today it is the OR of the kbagent-stage
    re-exec guard and the wider dev/opt-out gate -- the call sites in
    :func:`maybe_auto_update` now consult the two helpers separately.
    """
    return _should_skip_kbagent_stage() or _should_skip_all()


def _perform_update(latest_version: str, *, command: tuple[str, ...] | None = None) -> UpdateResult:
    """Download and install the latest version.

    Delegates to :func:`build_kbagent_upgrade_command` so this path stays
    byte-for-byte consistent with the explicit ``kbagent update`` command --
    in particular, the optional ``[server]`` extras are preserved when the
    install probe (``fastapi`` importable) says they were originally there.
    Prior to v0.41.1 this path ran a bare ``uv tool install --upgrade`` and
    silently dropped the extras, breaking ``kbagent serve --ui`` for users
    who got auto-updated on startup.

    Args:
        latest_version: The version being updated to (for logging).

    Returns:
        :class:`UpdateResult` whose ``outcome`` is ``SUCCESS`` on a clean install,
        ``TIMEOUT`` when the install outran :func:`get_update_timeout` -- it is
        still running and will finish on its own, so the next launch picks it up --
        and ``FAILED`` otherwise, with the installer's output in ``output``.
    """
    # ``command`` is supplied by the startup planner. Keep the fallback only
    # for direct legacy callers; it must never be used after another stage has
    # mutated the kbagent environment.
    if command is None:
        wheel_url = resolve_kbagent_wheel_url(latest_version)
        cmd = build_kbagent_upgrade_command(target_version=latest_version, wheel_url=wheel_url)
    else:
        cmd = list(command)
    if cmd is None:
        return UpdateResult(UpdateOutcome.FAILED)

    # Deliberately does NOT kill the installer when the deadline passes -- see
    # ``update_runner.run_install``. A terminated uv leaves the same
    # half-removed venv a Windows file lock does (issue #528).
    run = run_install(tuple(cmd), timeout=get_update_timeout())
    if run.status is InstallStatus.SUCCEEDED:
        return UpdateResult(UpdateOutcome.SUCCESS)
    if run.status is InstallStatus.STILL_RUNNING:
        return UpdateResult(UpdateOutcome.TIMEOUT)
    return UpdateResult(UpdateOutcome.FAILED, run.output)


def _format_update_failure(output: str, recovery_command: str | None) -> str:
    """Render the startup banner for a failed install (issue #771).

    Names the line the installer ended with, so the user can tell a network
    error from a permanent one.
    """
    lines = ["Auto-update failed; continuing with current version."]
    cause = summarize_install_failure(output)
    if cause:
        lines.append(f"Cause: {cause}")
    if recovery_command:
        lines.append(f"Recover with: {recovery_command}")
    return "\n".join(lines) + "\n"


def _re_exec() -> None:
    """Replace the current process with the updated kbagent binary.

    Sets KBAGENT_SKIP_UPDATE=1 to prevent infinite re-exec loops.
    Falls back to `python -m keboola_agent_cli` if the kbagent binary
    is not found on PATH.
    """
    env = os.environ.copy()
    env[ENV_SKIP_UPDATE] = "1"

    kbagent_path = shutil.which("kbagent")
    if kbagent_path:
        os.execvpe("kbagent", sys.argv, env)
    else:
        # Fallback: run as python module
        new_argv = [sys.executable, "-m", "keboola_agent_cli", *sys.argv[1:]]
        os.execvpe(sys.executable, new_argv, env)


def show_post_update_changelog() -> None:
    """Print 'What's new' after a successful auto-update re-exec.

    Checks for ``KBAGENT_UPDATED_FROM`` env var (set before re-exec).
    If present, prints the changelog for the current version and clears
    the env var so it only fires once.
    """
    try:
        old_version = os.environ.pop(ENV_UPDATED_FROM, "")
        if not old_version:
            return
        msg = format_whats_new(old_version, __version__)
        if msg:
            sys.stderr.write(msg)
    except Exception:
        logger.debug("what's-new banner rendering failed", exc_info=True)


def report_finished_deferred_update() -> None:
    """Print the outcome of a deferred update scheduled by an earlier run.

    The deferred path (Windows) installs from a detached helper *after* this
    process is gone, so the process that scheduled the update can never report
    it. Whoever starts next does -- exactly once -- and on success also shows
    the changelog the in-place path shows after its re-exec.
    """
    try:
        report = collect_finished_deferred_update()
    except Exception:
        logger.debug("Could not collect the deferred-update result", exc_info=True)
        return
    if report is None:
        return
    sys.stderr.write(_format_deferred_report(report))


def _format_deferred_report(report: DeferredUpdateReport) -> str:
    """Render a finished deferred update as one stderr block.

    An abandoned update is *not* a failure and must not read like one: nothing
    was installed because another kbagent kept running, so the environment is
    untouched and the next launch simply tries again.
    """
    if report.status is DeferredUpdateStatus.SUCCEEDED:
        message = f"Updated kbagent to v{report.target_version}.\n"
        whats_new = format_whats_new(report.from_version, __version__) or ""
        return message + whats_new
    if report.status is DeferredUpdateStatus.ABANDONED:
        return (
            f"Background update to v{report.target_version} was skipped: another kbagent "
            "process kept running. It will be retried.\n"
        )
    recovery = f" Recover with: {report.recovery_command}" if report.recovery_command else ""
    exit_note = f" (exit code {report.exit_code})" if report.exit_code is not None else ""
    return (
        f"Background update to v{report.target_version} did not complete{exit_note}. "
        f"Log: {report.log_path}.{recovery}\n"
    )


def _schedule_deferred_update(plan: KbagentUpdatePlan) -> None:
    """Hand the terminal reinstall to a detached helper instead of running it here.

    On Windows an in-place ``uv tool install`` deletes the venv this process is
    executing from and cannot finish, leaving it gutted (issue #528). The helper
    waits until no kbagent is left and only then installs, so this process just
    keeps going on the current version -- the update lands for the next launch.

    When no helper can be spawned we say so and print the exact command. Falling
    back to an in-place install here would reintroduce the corruption.
    """
    target = plan.latest_version or __version__
    if plan.command is None:
        return
    pending = pending_update_target()
    if pending is not None and pending != target:
        sys.stderr.write(
            f"An update to v{pending} is already scheduled; v{target} is not scheduled "
            "until it has run.\n"
        )
        return
    request = DeferredUpdateRequest(
        from_version=__version__,
        target_version=target,
        install_command=plan.command,
        recovery_command=plan.recovery_command,
        hardlink_retry_command=build_hardlink_retry_command(plan.command),
    )
    if request_deferred_update(request):
        sys.stderr.write(
            f"Updating kbagent v{__version__} -> v{target} in the background; "
            "it applies once every kbagent process has exited.\n"
        )
        return
    sys.stderr.write(
        f"kbagent v{target} is available but cannot be installed safely while kbagent is "
        f"running. Install it with: {plan.recovery_command}\n"
    )


def _notify_frozen_update_available(
    distribution: FrozenDistribution, latest_version: str | None
) -> None:
    """Report a new release to a native-binary user instead of self-updating.

    Replaces the self-update for frozen (PyInstaller) builds. Neither the inline
    reinstall nor the deferred Windows helper can upgrade a Chocolatey /
    Homebrew / apt / dnf install -- both would create an unrelated second copy
    that shadows the real binary on PATH (see
    :mod:`keboola_agent_cli.frozen_dist`). So we only tell the user, naming the
    command their own channel actually accepts.

    Silent when already current or when the latest version is unknown (offline,
    or the re-exec guard suppressed the fetch): a version banner with nothing
    actionable behind it is noise.
    """
    if _is_up_to_date(__version__, latest_version) is not False:
        return
    sys.stderr.write(
        f"kbagent v{__version__} -> v{latest_version} available. Self-update is "
        f"disabled for the standalone binary ({distribution.channel.value}); "
        f"{distribution.upgrade_hint}\n"
    )


def _prepare_auto_kbagent_plan(latest_version: str | None) -> KbagentUpdatePlan:
    """Adapt the shared plan to the startup comparison seam used by tests."""
    prepared = prepare_kbagent_update_plan(latest_version)
    up_to_date = _is_up_to_date(__version__, latest_version)
    return KbagentUpdatePlan(
        current_version=prepared.current_version,
        latest_version=prepared.latest_version,
        up_to_date=up_to_date,
        command=prepared.command if up_to_date is False else None,
        recovery_command=prepared.recovery_command,
        crosses_major=up_to_date is False and _major_of(latest_version) > _major_of(__version__),
    )


def _notice_mode() -> tuple[NoticeMode, str | None]:
    """The text form of the deprecation notice, and the channel command of a native binary."""
    frozen = detect_frozen_distribution()
    if frozen is not None:
        return NoticeMode.FROZEN, frozen.upgrade_command or frozen.upgrade_hint
    if _auto_update_disabled():
        return NoticeMode.AUTO_UPDATE_OFF, None
    return NoticeMode.AUTO_UPDATE, None


def register_deprecation_notice(
    next_major: NextMajorRelease | None, *, now: float | None = None
) -> DeprecationNotice | None:
    """Build the deprecation notice for ``next_major`` and keep it for the output layer.

    Returns None (and keeps nothing) when no release of the next major is known.
    """
    if next_major is None or _major_of(next_major.version) != _major_of(__version__) + 1:
        return None
    mode, channel_command = _notice_mode()
    notice = build_notice(
        current_version=__version__,
        next_version=next_major.version,
        published_ts=next_major.published_ts(),
        migration_ts=next_major.migration_ts(),
        now=now if now is not None else time.time(),
        mode=mode,
        channel_command=channel_command,
    )
    register(notice)
    return notice


def show_deprecation_notice(formatter: "OutputFormatter") -> None:
    """Show the notice in ``version``, ``update``, ``doctor`` and ``context``: on every run.

    Uses the notice this run already built (startup check, or the fresh lookup
    of ``version`` / ``update``), else the next-major keys of the cache, so it
    needs no network. JSON mode only keeps it for ``warnings[]`` (the envelope
    builders read it); human mode prints it through ``formatter.warning`` on
    stderr (spec HO1, UP3), unless the startup check printed it already.
    """
    try:
        from rich.markup import escape

        from .update_notice import current, take_unprinted_text

        if current() is None:
            next_major, _ = next_major_from_cache(_read_cache(), _major_of(__version__) + 1)
            register_deprecation_notice(next_major)
        if formatter.json_mode:
            return
        text = take_unprinted_text()
        if text is not None:
            formatter.warning(escape(text))
    except Exception:
        logger.debug("Could not show the deprecation notice", exc_info=True)


def _publish_startup_notice(
    next_major: NextMajorRelease | None, cache: dict | None, *, json_mode: bool
) -> None:
    """The deprecation notice of the startup check.

    ``--json``: a ``warnings[]`` entry on every run. Human mode: one stderr line
    once per :data:`DEPRECATION_NOTICE_INTERVAL_SECONDS`, and on every run from
    :data:`DEPRECATION_NOTICE_FINAL_WINDOW_SECONDS` before M.
    """
    notice = register_deprecation_notice(next_major)
    if notice is None or json_mode or next_major is None:
        return
    now = time.time()
    migration = next_major.migration_ts()
    final_window = (
        migration is not None and now >= migration - DEPRECATION_NOTICE_FINAL_WINDOW_SECONDS
    )
    shown_at = cache.get("notice_shown_at") if cache else None
    due = (
        final_window
        or not isinstance(shown_at, int | float)
        or now - float(shown_at) >= DEPRECATION_NOTICE_INTERVAL_SECONDS
    )
    if due and print_once():
        _mark_notice_shown(now)


def _cached_gate(cache: dict | None, latest_version: str | None) -> GatedRelease:
    """A :class:`GatedRelease` from the cache alone; it never crosses a major."""
    next_major, checked_at = next_major_from_cache(cache, _major_of(__version__) + 1)
    return GatedRelease(latest_version, latest_version, next_major, checked_at)


def maybe_auto_update(*, json_mode: bool = False) -> None:
    """Main entry point for the auto-update flow.

    Called from ``cli.py`` at the very top of ``main()``. If the installed
    version is behind the latest GitHub release, the upgrade is installed and
    the same argv is ``execvpe``'d into the new binary. The new process
    re-enters this function and short-circuits as up-to-date, because the
    re-exec sets ``KBAGENT_SKIP_UPDATE=1``.

    **Major versions are gated** (#791 section 4.3): the lookup is
    :func:`fetch_gated_release`, which stays on the running major until the
    migration date of the next one. A crossing is never installed inline and
    never re-execs: it goes to the deferred helper on every OS, so this
    command finishes on the old major and the next run starts on the new one.
    While a next major is known, the deprecation notice is shown
    (``json_mode``: in ``warnings[]`` instead of on stderr).

    **Frozen (PyInstaller) builds get a notification instead of an install.**
    A native binary from Chocolatey / WinGet / Homebrew / apt / dnf is not a
    uv-managed tool environment, so neither the inline reinstall nor the
    deferred Windows helper applies -- both would install an unrelated second
    copy instead of upgrading the running one (full rationale in
    :mod:`keboola_agent_cli.frozen_dist`).

    Cache discipline: a single cache file at
    ``~/.config/keboola-agent-cli/version_cache.json`` stores
    ``latest_version`` so we make at most one GitHub round-trip per
    ``AUTO_UPDATE_CHECK_INTERVAL``.

    This function NEVER raises. All exceptions are caught and logged at
    debug level so the CLI always proceeds normally.
    """
    global _AUTO_UPDATE_RAN
    try:
        # Bug D fix from issue #263: per-process sentinel. ``kbagent repl``
        # re-enters main() on every prompt; without this gate the auto-
        # update flow fired (and printed banners) once per command typed.
        # Set BEFORE any work so a crash mid-flow still gates subsequent
        # in-process re-entries.
        if _AUTO_UPDATE_RAN:
            return
        _AUTO_UPDATE_RAN = True

        # A deferred update scheduled by an earlier run installs after that run
        # exits, so its outcome has to be reported by someone else. Do it before
        # the skip gates: the user is owed the result -- especially a failure and
        # its recovery command -- even on a run that will not update anything.
        report_finished_deferred_update()

        # Wide gates (dev install / opt-out / update|version commands)
        # skip the update entirely -- there is nothing reasonable to do.
        if _should_skip_all():
            return

        cache = _read_cache()
        cache_is_fresh = bool(cache and _is_cache_fresh(cache, AUTO_UPDATE_CHECK_INTERVAL))
        cached_kbagent = cache.get("latest_version") if cache else None
        # A kbagent older than the major gate on the same machine may have
        # written a version of a higher major here. Never install it.
        if isinstance(cached_kbagent, str) and _major_of(cached_kbagent) > _major_of(__version__):
            cached_kbagent = None
        skip_kbagent_stage = _should_skip_kbagent_stage()
        # A frozen (PyInstaller) binary is upgraded by the package manager that
        # placed it, never by us. Detected BEFORE planning so the wheel-URL HEAD
        # probe inside prepare_kbagent_update_plan is skipped as well -- that
        # would be a wasted network round-trip on every single startup, for a
        # command this process is never allowed to run.
        frozen_dist = detect_frozen_distribution()
        use_cached_kbagent = cache_is_fresh and isinstance(cached_kbagent, str)
        if use_cached_kbagent:
            gated = _cached_gate(cache, cached_kbagent)
        elif skip_kbagent_stage:
            gated = _cached_gate(cache, None)
        else:
            gated = fetch_gated_release(timeout=VERSION_CHECK_TIMEOUT, cache=cache)
        latest_version = gated.latest_version

        # Planning is complete before any subprocess can mutate the tool
        # environment. In particular, all HTTP requests, import probes, PATH
        # inspection, and command construction are above this line.
        kbagent_plan = (
            _prepare_auto_kbagent_plan(latest_version)
            if not skip_kbagent_stage and frozen_dist is None
            else KbagentUpdatePlan(__version__, latest_version, True, None, None)
        )

        # Persist the prepared cache before any self-mutation. Only the
        # running major's version goes there (see `_write_cache`).
        if not use_cached_kbagent:
            _write_cache(
                latest_version=(
                    gated.same_major_version
                    if gated.same_major_version is not None
                    else cached_kbagent
                    if isinstance(cached_kbagent, str)
                    else None
                ),
                next_major_keys=next_major_cache_keys(gated),
            )

        _publish_startup_notice(gated.next_major, cache, json_mode=json_mode)

        # Frozen builds: the install becomes a notification. Deliberately placed
        # AFTER the cache write -- letting the TTL tick is what throttles the
        # banner below. Placed BEFORE the `should_defer()` branch further down,
        # so the deferred Windows helper is never scheduled for a binary it
        # cannot install over either.
        if frozen_dist is not None:
            # Only on a run that actually refreshed the cache, i.e. at most once
            # per AUTO_UPDATE_CHECK_INTERVAL. Unlike the normal path this banner
            # cannot resolve itself by re-exec'ing, so without throttling it
            # would print on every kbagent invocation until the user upgrades --
            # pure noise in any script that shells out to kbagent in a loop.
            if not use_cached_kbagent:
                _notify_frozen_update_available(frozen_dist, latest_version)
            return

        if kbagent_plan.up_to_date is not False:
            return
        if kbagent_plan.command is None:
            # Only offer a recovery line when there is one; `Recover with: None`
            # is worse than saying nothing. A failed version lookup never gets
            # here -- `up_to_date` is None then, and the guard above returns
            # first, deliberately keeping a transient network blip silent
            # instead of nagging on every single command.
            recovery = kbagent_plan.recovery_command
            sys.stderr.write(
                "Auto-update could not prepare a reinstall command."
                + (f" Recover with: {recovery}\n" if recovery else "\n")
            )
            return

        # Where the reinstall runs is the whole fix for issue #528: an in-place
        # `uv tool install` deletes the venv this process runs from, which
        # Windows cannot survive. Defer there; keep the proven inline install +
        # re-exec on POSIX, where replacing an open file is safe. A major
        # crossing is deferred on every OS and never re-execs (#791 UP2).
        if should_defer() or kbagent_plan.crosses_major:
            _schedule_deferred_update(kbagent_plan)
            return

        sys.stderr.write(f"Updating kbagent v{__version__} -> v{kbagent_plan.latest_version}...\n")
        result = _perform_update(
            kbagent_plan.latest_version or __version__, command=kbagent_plan.command
        )
        outcome = result.outcome
        if outcome is UpdateOutcome.SUCCESS:
            sys.stderr.write(f"Updated to v{kbagent_plan.latest_version}. Re-launching...\n")
            os.environ[ENV_UPDATED_FROM] = __version__
            _re_exec()
            return
        if outcome is UpdateOutcome.TIMEOUT:
            # The installer was NOT killed -- it is still working. Saying
            # "recover with ..." here would invite the user to start a second
            # installer against the same environment.
            sys.stderr.write(
                f"Update still running after {int(get_update_timeout())}s; it continues in the "
                "background and applies on the next launch.\n"
            )
        else:
            sys.stderr.write(_format_update_failure(result.output, kbagent_plan.recovery_command))
    except Exception:
        # Blanket catch: auto-update must NEVER crash the CLI.
        logger.debug("Auto-update check failed", exc_info=True)
