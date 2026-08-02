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
import time
from importlib.metadata import distribution
from pathlib import Path

import platformdirs

from . import __version__
from .changelog import ENV_UPDATED_FROM, format_whats_new
from .constants import (
    AUTO_UPDATE_CHECK_INTERVAL,
    ENV_AUTO_UPDATE,
    ENV_SKIP_UPDATE,
    MCP_UPGRADE_TIMEOUT,
    VERSION_CACHE_FILENAME,
    VERSION_CHECK_TIMEOUT,
)
from .frozen_dist import FrozenDistribution, detect_frozen_distribution
from .services.version_service import (
    MCP_PACKAGE_NAME,
    MCP_UV_PRERELEASE_FLAG,
    KbagentUpdatePlan,
    McpUpdatePlan,
    _detect_mcp_install_method,
    _fetch_kbagent_latest_version,
    _fetch_mcp_latest_version,
    _get_local_mcp_version,
    _is_up_to_date,
    _perform_mcp_update,
    build_kbagent_upgrade_command,
    get_update_timeout,
    prepare_kbagent_update_plan,
    prepare_mcp_update_plan,
    resolve_kbagent_wheel_url,
)
from .update_runner import (
    DeferredUpdateReport,
    DeferredUpdateRequest,
    DeferredUpdateStatus,
    InstallStatus,
    collect_finished_deferred_update,
    request_deferred_update,
    run_install,
    should_defer,
)

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
# Python interpreter, so the kbagent-self-upgrade -> re-exec -> MCP-stage
# chain from PR #257 is preserved.
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
        Parsed dict with ``last_check`` (required) and any of
        ``latest_version`` / ``mcp_latest_version`` / ``mcp_install_method``,
        or None if the file is missing, unreadable, or corrupt. Older
        cache formats lacking the MCP fields are still accepted -- the
        missing fields trigger a fresh fetch in the same run.
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


def _write_cache(
    latest_version: str | None,
    mcp_latest_version: str | None = None,
    mcp_install_method: str | None = None,
) -> None:
    """Write the version cache file.

    Args:
        latest_version: kbagent latest version. Falls back to the running
            interpreter's ``__version__`` when None -- caller is in a
            re-exec'd process where Stage 1 was skipped and we still want
            to persist the MCP-side fields without losing the kbagent key.
        mcp_latest_version: keboola-mcp-server latest version from PyPI.
        mcp_install_method: Detected MCP install method (drives upgrade cmd).
    """
    if latest_version is None:
        # Re-exec path: persist the running version so cache_is_fresh
        # logic on the NEXT run still has a kbagent-side anchor.
        latest_version = __version__
    cache_path = _get_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict = {
            "last_check": time.time(),
            "latest_version": latest_version,
        }
        if mcp_latest_version is not None:
            payload["mcp_latest_version"] = mcp_latest_version
        if mcp_install_method is not None:
            payload["mcp_install_method"] = mcp_install_method
        cache_path.write_text(json.dumps(payload), encoding="utf-8")
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
    """
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
        pass

    return False


def _should_skip_kbagent_stage() -> bool:
    """Whether the kbagent self-upgrade stage should be skipped.

    Re-exec guard (``KBAGENT_SKIP_UPDATE=1``) skips ONLY the kbagent stage.
    The MCP stage in the re-exec'd process is intentionally allowed to
    proceed -- otherwise a freshly-upgraded kbagent on a stale MCP would
    require a second invocation to refresh MCP. See
    :func:`_should_skip_all` for the wider conditions that gate both stages.
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


def _should_skip_all() -> bool:
    """Whether the entire auto-update flow should be skipped.

    Skip conditions (apply to BOTH kbagent and MCP stages):

    - ``KBAGENT_AUTO_UPDATE`` in ``{false, 0, no}`` (user opt-out).
    - Development / editable install (we never auto-upgrade a dev tree).
    - Current command is ``update`` / ``version`` (those commands handle
      versioning themselves and would loop if auto-update fired here too).

    Notably **does NOT include** the re-exec guard
    ``KBAGENT_SKIP_UPDATE=1`` -- that is per-stage and only skips the
    kbagent stage. See :func:`_should_skip_kbagent_stage`.
    """
    # User opt-out
    auto_update_val = os.environ.get(ENV_AUTO_UPDATE, "").lower().strip()
    if auto_update_val in ("false", "0", "no"):
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


def _perform_update(
    latest_version: str, *, command: tuple[str, ...] | None = None
) -> UpdateOutcome:
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
        :class:`UpdateOutcome`: ``SUCCESS`` on a clean install, ``TIMEOUT`` when
        the install outran :func:`get_update_timeout` -- it is still running and
        will finish on its own, so the next launch picks it up -- and ``FAILED``
        otherwise.
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
        return UpdateOutcome.FAILED

    # Deliberately does NOT kill the installer when the deadline passes -- see
    # ``update_runner.run_install``. A terminated uv leaves the same
    # half-removed venv a Windows file lock does (issue #528).
    run = run_install(tuple(cmd), timeout=get_update_timeout())
    if run.status is InstallStatus.SUCCEEDED:
        return UpdateOutcome.SUCCESS
    if run.status is InstallStatus.STILL_RUNNING:
        return UpdateOutcome.TIMEOUT
    return UpdateOutcome.FAILED


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
        pass  # Never crash


def _maybe_update_mcp(cache: dict | None, fetched_now: bool) -> str | None:
    """Check for and apply a keboola-mcp-server upgrade.

    Args:
        cache: Existing cache dict (may be stale) or None.
        fetched_now: True if this run has already done a fresh latest-version
            fetch for kbagent. Used to avoid double network round-trips:
            when stale, we issue both fetches in the same pass and persist
            both to the cache.

    Returns:
        ``mcp_latest_version`` to persist to the cache (None if skipped or
        fetch failed). Caller composes the cache write.
    """
    # Use cached MCP latest if fresh; otherwise fetch.
    cached_latest: str | None = None
    if cache is not None:
        candidate = cache.get("mcp_latest_version")
        if isinstance(candidate, str) and candidate:
            cached_latest = candidate

    if not fetched_now and cached_latest:
        mcp_latest: str | None = cached_latest
    else:
        mcp_latest = _fetch_mcp_latest_version(timeout=VERSION_CHECK_TIMEOUT)

    if mcp_latest is None:
        return cached_latest  # nothing to do; preserve any prior cache

    local_version = _get_local_mcp_version()
    if local_version is None:
        # Bug C fix from issue #263: when local-version detection fails,
        # do NOT fall through to the upgrade attempt. The previous behaviour
        # printed an "Updating ... vunknown -> v1.59.1" banner and ran the
        # upgrade subprocess every TTL window because `up_to_date` was None
        # (not True), which bypassed the short-circuit below. The fix opts
        # out of the upgrade for this TTL window and lets the next fresh-
        # cache pass retry detection. The cache write below records the
        # latest version regardless so the cache TTL still ticks.
        return mcp_latest

    up_to_date = _is_up_to_date(local_version, mcp_latest)
    if up_to_date is True:
        return mcp_latest

    method = _detect_mcp_install_method()
    if method == "none":
        # Nothing installed locally; do not auto-install on startup.
        return mcp_latest

    sys.stderr.write(
        f"Updating keboola-mcp-server v{local_version or 'unknown'} -> v{mcp_latest}"
        f" (via {method})...\n"
    )
    pre_version = local_version
    success, info = _perform_mcp_update(method=method, timeout=MCP_UPGRADE_TIMEOUT)
    if success:
        # Bug E fix from issue #263: subprocess returncode == 0 is NOT
        # enough to claim the upgrade actually happened. `uv tool upgrade`
        # exits 0 even when its resolver backtracks to the previously
        # installed version (a real-world reproducer: keboola-mcp-server
        # v1.59.1 declares fastmcp==3.2.0 strict equality, the existing
        # venv has fastmcp==2.13.0.2, uv resolves back to v1.32.0 and
        # exits clean). Compare pre and post versions and tell the truth.
        post_version = _get_local_mcp_version()
        if post_version and pre_version and post_version != pre_version:
            sys.stderr.write(f"Updated keboola-mcp-server to v{post_version}.\n")
        elif post_version is None:
            # Probe failed post-upgrade; cannot verify -- assume latest.
            sys.stderr.write(
                f"Updated keboola-mcp-server (probe failed; latest on PyPI: v{mcp_latest}).\n"
            )
        else:
            # Subprocess exit 0 but local version unchanged. Cause: uv's
            # resolver backtracked to an older release that still satisfies
            # every constraint (a pre-release-only transitive pin, a strict
            # equality the venv cannot meet, or a Python floor). The upgrade
            # commands already pass --prerelease=allow (issue #324); if the
            # version still will not move, force a clean reinstall WITH the
            # same flag -- a plain reinstall hits the identical wall.
            sys.stderr.write(
                f"keboola-mcp-server upgrade exit 0 but local version still v{pre_version} "
                f"(latest: v{mcp_latest}). The resolver backtracked to an older release -- "
                f"run `uv tool install --reinstall {MCP_UV_PRERELEASE_FLAG} {MCP_PACKAGE_NAME}` "
                f"to force the latest.\n"
            )
    else:
        sys.stderr.write(
            f"keboola-mcp-server upgrade skipped: {info}; continuing with current version.\n"
        )

    return mcp_latest


def _apply_prepared_mcp_update(plan: McpUpdatePlan) -> None:
    """Apply a previously prepared MCP update while kbagent is still intact."""
    if plan.latest_version is None or plan.up_to_date is True or plan.install_method == "none":
        return
    if plan.current_version is None or plan.command is None:
        return

    sys.stderr.write(
        f"Updating keboola-mcp-server v{plan.current_version} -> v{plan.latest_version}"
        f" (via {plan.install_method})...\n"
    )
    success, info = _perform_mcp_update(
        method=plan.install_method,
        timeout=MCP_UPGRADE_TIMEOUT,
        command=plan.command,
    )
    if not success:
        sys.stderr.write(
            f"keboola-mcp-server upgrade skipped: {info}; continuing with current version.\n"
        )
        return

    # A post-MCP probe is permitted: the kbagent environment has not yet
    # changed. It is deliberately the last discovery operation in this flow.
    post_version = _get_local_mcp_version()
    if post_version and post_version != plan.current_version:
        sys.stderr.write(f"Updated keboola-mcp-server to v{post_version}.\n")
    elif post_version is None:
        sys.stderr.write(
            f"Updated keboola-mcp-server (probe failed; latest on PyPI: v{plan.latest_version}).\n"
        )
    else:
        sys.stderr.write(
            f"keboola-mcp-server upgrade exit 0 but local version still v{plan.current_version} "
            f"(latest: v{plan.latest_version}). Run `uv tool install --reinstall "
            f"{MCP_UV_PRERELEASE_FLAG} {MCP_PACKAGE_NAME}` to force the latest.\n"
        )


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
    request = DeferredUpdateRequest(
        from_version=__version__,
        target_version=target,
        install_command=plan.command,
        recovery_command=plan.recovery_command,
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

    Replaces Stage 1 for frozen (PyInstaller) builds. Neither the inline
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
    )


def maybe_auto_update() -> None:
    """Main entry point for the auto-update flow.

    Called from ``cli.py`` at the very top of ``main()``. Orchestrates two
    sequential stages with **independent skip gating** (since v0.30.1):

    1. **kbagent self-update** -- if the installed version is behind the
       latest GitHub release, download the upgrade and ``execvpe`` the new
       binary with the same argv. The new process re-enters this function
       and the kbagent stage short-circuits as up-to-date.
    2. **keboola-mcp-server update** -- if the locally installed MCP server
       is behind PyPI, run the upgrade command matching the install
       method (``uv tool upgrade`` / ``pip install -U`` / ``uvx --refresh``).
       No re-exec is needed: the MCP server is spawned by ``tool call``
       commands and the next spawn picks up the new version.

    **Frozen (PyInstaller) builds replace Stage 1 with a notification.** A
    native binary from Chocolatey / WinGet / Homebrew / apt / dnf is not a
    uv-managed tool environment, so neither the inline reinstall nor the
    deferred Windows helper applies -- both would install an unrelated second
    copy instead of upgrading the running one (full rationale in
    :mod:`keboola_agent_cli.frozen_dist`). **Stage 2 still runs there**, and
    that is deliberate: ``keboola-mcp-server`` is a *separate* Python
    distribution that a frozen kbagent only ever spawns as a subprocess, so
    upgrading it neither touches nor depends on the frozen binary. A pure
    binary user with no Python at all is unaffected either way --
    :func:`_detect_mcp_install_method` returns ``"none"`` and the stage
    short-circuits without installing anything.

    Critical invariant: **the re-exec'd process (KBAGENT_SKIP_UPDATE=1)
    skips ONLY Stage 1**. Stage 2 always runs, so a kbagent self-upgrade
    on startup leaves the user with both kbagent AND MCP refreshed in
    a single boot, not two. This was the B-1 finding in the PR #257
    review -- gating the MCP stage on the same flag broke the
    "both stages always run" promise after a kbagent self-upgrade.

    Cache discipline: a single cache file at
    ``~/.config/keboola-agent-cli/version_cache.json`` stores both
    ``latest_version`` (kbagent) and ``mcp_latest_version`` so we make at
    most two PyPI/GitHub round-trips per ``AUTO_UPDATE_CHECK_INTERVAL``.

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
        # skip BOTH stages -- there is nothing reasonable to do.
        if _should_skip_all():
            return

        cache = _read_cache()
        cache_is_fresh = bool(cache and _is_cache_fresh(cache, AUTO_UPDATE_CHECK_INTERVAL))
        cached_kbagent = cache.get("latest_version") if cache else None
        cached_mcp = cache.get("mcp_latest_version") if cache else None
        skip_kbagent_stage = _should_skip_kbagent_stage()
        # A frozen (PyInstaller) binary is upgraded by the package manager that
        # placed it, never by us. Detected BEFORE planning so the wheel-URL HEAD
        # probe inside prepare_kbagent_update_plan is skipped as well -- that
        # would be a wasted network round-trip on every single startup, for a
        # command this process is never allowed to run.
        frozen_dist = detect_frozen_distribution()
        use_cached_kbagent = cache_is_fresh and isinstance(cached_kbagent, str)
        use_cached_mcp = cache_is_fresh and isinstance(cached_mcp, str)
        latest_version = (
            cached_kbagent
            if use_cached_kbagent
            else (
                None
                if skip_kbagent_stage
                else _fetch_kbagent_latest_version(timeout=VERSION_CHECK_TIMEOUT)
            )
        )
        mcp_latest = (
            cached_mcp
            if use_cached_mcp
            else _fetch_mcp_latest_version(timeout=VERSION_CHECK_TIMEOUT)
        )

        # Planning is complete before any subprocess can mutate either tool.
        # In particular, all HTTP requests, import probes, PATH inspection,
        # and command construction are above this line.
        mcp_plan = prepare_mcp_update_plan(mcp_latest)
        kbagent_plan = (
            _prepare_auto_kbagent_plan(latest_version)
            if not skip_kbagent_stage and frozen_dist is None
            else KbagentUpdatePlan(__version__, latest_version, True, None, None)
        )

        # MCP is an independent environment. Finish it before the terminal
        # self-reinstall and persist the prepared cache before self-mutation.
        _apply_prepared_mcp_update(mcp_plan)
        if not (use_cached_kbagent and use_cached_mcp):
            _write_cache(
                latest_version=(
                    latest_version
                    if latest_version is not None
                    else cached_kbagent
                    if isinstance(cached_kbagent, str)
                    else None
                ),
                mcp_latest_version=(
                    mcp_latest
                    if mcp_latest is not None
                    else cached_mcp
                    if isinstance(cached_mcp, str)
                    else None
                ),
                mcp_install_method=mcp_plan.install_method,
            )

        # Frozen builds: Stage 1 becomes a notification. Deliberately placed
        # AFTER the MCP stage and the cache write -- both stay fully active (see
        # this function's docstring for why MCP is still updated), and letting
        # the TTL tick is what throttles the banner below. Placed BEFORE the
        # `should_defer()` branch further down, so the deferred Windows helper
        # is never scheduled for a binary it cannot install over either.
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
            sys.stderr.write(
                "Auto-update could not prepare a reinstall command. "
                f"Recover with: {kbagent_plan.recovery_command}\n"
            )
            return

        # Where the reinstall runs is the whole fix for issue #528: an in-place
        # `uv tool install` deletes the venv this process runs from, which
        # Windows cannot survive. Defer there; keep the proven inline install +
        # re-exec on POSIX, where replacing an open file is safe.
        if should_defer():
            _schedule_deferred_update(kbagent_plan)
            return

        sys.stderr.write(f"Updating kbagent v{__version__} -> v{kbagent_plan.latest_version}...\n")
        outcome = _perform_update(
            kbagent_plan.latest_version or __version__, command=kbagent_plan.command
        )
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
            sys.stderr.write(
                "Auto-update failed; continuing with current version. Recover with: "
                f"{kbagent_plan.recovery_command}\n"
            )
    except Exception:
        # Blanket catch: auto-update must NEVER crash the CLI.
        logger.debug("Auto-update check failed", exc_info=True)
