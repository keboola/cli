"""Version service - detect local versions and check for updates.

Provides version information for kbagent and the keboola-mcp-server
dependency. Both are auto-updated on kbagent startup (see auto_update.py)
and explicitly via ``kbagent update``. The MCP server version is detected
from the locally installed binary or Python distribution; the latest
version is resolved from PyPI.
"""

import importlib.util
import logging
import re
import shutil
import subprocess
from typing import Any

import httpx
from packaging.version import InvalidVersion, Version

from .. import __version__
from ..constants import (
    KBAGENT_GITHUB_REPO,
    KBAGENT_INSTALL_SOURCE,
    MCP_PROBE_TIMEOUT,
    MCP_PYPI_URL,
    MCP_UPGRADE_TIMEOUT,
    VERSION_CHECK_TIMEOUT,
)

logger = logging.getLogger(__name__)

# keboola-mcp-server constants
MCP_PACKAGE_NAME = "keboola-mcp-server"
MCP_BINARY_NAME = "keboola_mcp_server"


def _is_uvx_available() -> bool:
    """Check if uvx is available on PATH."""
    return shutil.which("uvx") is not None


def has_server_extras() -> bool:
    """Detect whether the current install was created with ``[server]`` extras.

    The detection probe is ``importlib.util.find_spec('fastapi')``: FastAPI
    is pulled in *only* by the optional ``[server]`` extra (declared in
    ``pyproject.toml``'s ``[project.optional-dependencies]`` table), so its
    presence is a reliable proxy for "user originally installed with
    ``--with 'keboola-agent-cli[server]'``".

    Used by every kbagent self-upgrade path (``kbagent update`` and the
    startup auto-update hook) to decide whether to pair ``uv tool install``
    with ``--with 'keboola-agent-cli[server]'`` -- without that flag, the
    fresh re-resolution silently drops the FastAPI + uvicorn extras and
    breaks ``kbagent serve --ui`` for users who originally installed with
    ``[server]``. (Bug fixed in v0.40.2 for the explicit ``kbagent update``
    path; extended to the startup auto-update hook in v0.41.1.)
    """
    return importlib.util.find_spec("fastapi") is not None


def build_kbagent_upgrade_command(
    *, prerelease: bool = False, target_version: str | None = None
) -> list[str] | None:
    """Build the argv command to upgrade kbagent in-place.

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
        target_version: When set together with ``prerelease=True``, append
            ``@v<target_version>`` to the git+ source URL so uv installs
            the exact commit pointed to by the tag. Critical when betas
            live on a feature branch instead of main -- without this, uv
            resolves the default branch and silently installs the stale
            main HEAD even though the version fetcher advertised the
            beta tag. Ignored for stable upgrades (the auto-update path
            always tracks main, which IS the latest stable).

    Returns:
        Command list ready for :func:`subprocess.run`, or ``None`` if
        neither ``uv`` nor ``pip`` is on ``PATH`` (in which case the
        caller surfaces a manual-install hint).
    """
    # Tag-pin the install source ONLY for beta opt-in (Variant B fix).
    # Stable upgrades let uv resolve main HEAD as before -- main IS
    # the stable channel, so an extra HTTP round-trip to fetch the
    # tag name would be pure overhead.
    install_source = KBAGENT_INSTALL_SOURCE
    if prerelease and target_version:
        install_source = f"{KBAGENT_INSTALL_SOURCE}@v{target_version}"
    has_server = has_server_extras()
    uv_path = shutil.which("uv")
    if uv_path:
        if has_server:
            # We pair ``--with`` with ``--force`` (rather than
            # ``--upgrade``) because ``uv tool install`` rejects
            # ``--upgrade`` together with ``--with`` when the additional
            # spec resolves to a different version than the existing
            # tool environment -- ``--force`` is uv's documented way to
            # reapply both in one shot.
            cmd = [
                uv_path,
                "tool",
                "install",
                "--force",
                "--with",
                "keboola-agent-cli[server]",
                install_source,
            ]
        else:
            cmd = [uv_path, "tool", "install", "--upgrade", install_source]
        if prerelease:
            # Insert before the source spec so uv parses it as a global
            # resolver flag (not a positional arg).
            cmd.insert(-1, "--prerelease=allow")
        return cmd
    pip_path = shutil.which("pip")
    if pip_path is None:
        return None
    # pip extras syntax: the [server] suffix attaches to the project
    # name in the PEP 508 spec; for git+ URLs we wrap with the project
    # name on the left of the URL.
    install_spec = f"keboola-agent-cli[server] @ {install_source}" if has_server else install_source
    cmd = [pip_path, "install", "--upgrade", install_spec]
    if prerelease:
        cmd.insert(2, "--pre")
    return cmd


def _get_local_mcp_version(timeout: float = MCP_PROBE_TIMEOUT) -> str | None:
    """Detect the locally installed keboola-mcp-server version.

    Resolution order:

    1. ``uv tool list`` (the canonical source for ``uv tool install``-managed
       binaries -- which is how kbagent's `doctor --fix` installs MCP). The
       output line ``keboola-mcp-server v1.59.1`` carries the exact version.
       This is the path the v0.30.2 fix relies on.
    2. ``importlib.metadata.version("keboola-mcp-server")`` (works when the
       package is pip-installed in the **same** Python environment as
       kbagent -- rare in production because kbagent itself is usually
       installed via ``uv tool install`` into a sibling venv).
    3. ``keboola_mcp_server --version`` subprocess. **Best-effort fallback**
       reserved for a future MCP server that adds a real ``--version`` CLI.
       Today the upstream binary does NOT honour ``--version`` and instead
       prints the usage block with returncode 0; the regex therefore finds
       no match and we move on cleanly.
    4. None when none of the above yields a version (typically: uvx cache;
       we cannot cleanly inspect uvx-cached-only installs without forcing
       a re-download).

    Args:
        timeout: Subprocess timeout in seconds for each subprocess probe.

    Returns:
        Version string like ``"1.59.1"``, or None when undetectable.
    """
    # 1. Preferred: read from `uv tool list` (works for `uv tool install`
    #    binaries, which is the kbagent doctor --fix install path).
    uv_path = shutil.which("uv")
    if uv_path:
        try:
            result = subprocess.run(
                [uv_path, "tool", "list"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode == 0:
                version = _uv_tool_list_get_mcp_version(result.stdout)
                if version:
                    return version
        except (subprocess.TimeoutExpired, OSError):
            pass

    # 2. Fallback: try importlib.metadata in the current Python environment.
    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _pkg_version

        try:
            return _pkg_version(MCP_PACKAGE_NAME)
        except PackageNotFoundError:
            pass
    except ImportError:
        pass

    # 3. Best-effort fallback: `keboola_mcp_server --version`. Currently a
    #    no-op against the upstream binary (no --version flag) but kept so
    #    a future release that adds the flag works without a kbagent
    #    re-roll.
    binary = shutil.which(MCP_BINARY_NAME)
    if binary:
        try:
            result = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode == 0:
                output = (result.stdout or "") + (result.stderr or "")
                # Heuristic: the upstream binary's --version-less help text
                # contains its own header with a path; reject any match
                # found inside a "usage:" line so we do not mistake a path
                # number (e.g. "/home/user/python3.12.9/...") for a version.
                cleaned = "\n".join(
                    line
                    for line in output.splitlines()
                    if not line.lstrip().lower().startswith("usage:")
                )
                m = re.search(r"\b(\d+\.\d+\.\d+)\b", cleaned)
                if m:
                    return m.group(1)
        except (subprocess.TimeoutExpired, OSError):
            pass

    return None


def _uv_tool_list_get_mcp_version(stdout: str) -> str | None:
    """Extract the keboola-mcp-server version from ``uv tool list`` output.

    Output format (one tool per block):

    .. code-block:: text

        keboola-mcp-server v1.59.1
        - keboola-mcp-server

    Robust against the same false-positive classes as
    :func:`_uv_tool_list_has_mcp` (similarly-named packages, indented
    binary lines, accidental stderr text) and rejects malformed lines
    where the second whitespace-separated token does not look like a
    semver-ish ``vX.Y.Z`` token.

    Args:
        stdout: Captured stdout of ``uv tool list``.

    Returns:
        Version string like ``"1.59.1"`` (the leading ``v`` stripped),
        or None if the package is not listed or the version token is
        missing / unparseable.
    """
    for line in stdout.splitlines():
        # Skip indented continuation lines (binary listings under a tool).
        if line.startswith((" ", "\t")):
            continue
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split()
        # First token must be exact package name; second must look like
        # ``vX.Y.Z`` (uv emits the leading "v"; tolerate the rare case
        # where it might not in some future version).
        if len(parts) < 2 or parts[0] != MCP_PACKAGE_NAME:
            continue
        version_token = parts[1].lstrip("v")
        if re.match(r"^\d+\.\d+\.\d+", version_token):
            return version_token
    return None


def _uv_tool_list_has_mcp(stdout: str) -> bool:
    """Detect whether ``uv tool list`` output contains ``keboola-mcp-server``.

    Robust against three classes of false-positive that a naive substring
    match (``MCP_PACKAGE_NAME in stdout``) would suffer:

    * **Similarly-named packages** -- e.g. a hypothetical
      ``keboola-mcp-server-foo`` would substring-match but is NOT the same
      tool; we want exact equality on the first whitespace-separated token.
    * **Indented binary listings** -- ``uv tool list`` shows each tool's
      published scripts on indented continuation lines (``    - <script>``),
      which can include ``keboola_mcp_server`` (the binary name) for tools
      that are NOT this package.
    * **Hint / warning text in stderr-merged buffers** -- callers should
      only ever feed `result.stdout` here, but per-line parsing also
      tolerates accidental stderr injection.

    Args:
        stdout: Captured stdout of ``uv tool list``.

    Returns:
        True iff a non-indented, non-blank line's first token is exactly
        ``keboola-mcp-server``.
    """
    for line in stdout.splitlines():
        # Skip indented continuation lines (binary listings under a tool).
        if line.startswith((" ", "\t")):
            continue
        stripped = line.strip()
        if not stripped:
            continue
        # First whitespace-separated token == package name (exact match).
        if stripped.split(maxsplit=1)[0] == MCP_PACKAGE_NAME:
            return True
    return False


def _detect_mcp_install_method() -> str:
    """Detect how keboola-mcp-server is installed locally.

    The detection drives which upgrade command is appropriate:

    - ``uv_tool``  -- installed via ``uv tool install``; upgrade with
      ``uv tool upgrade keboola-mcp-server``.
    - ``pip_env``  -- pip-installed in the active Python environment;
      upgrade with ``pip install --upgrade keboola-mcp-server``.
    - ``uvx``      -- only available via uvx cache (no persistent install);
      upgrade with ``uvx --refresh ...`` to invalidate the cache.
    - ``none``     -- not detectable; cannot upgrade automatically.

    Returns:
        One of ``"uv_tool"``, ``"pip_env"``, ``"uvx"``, ``"none"``.
    """
    binary = shutil.which(MCP_BINARY_NAME)
    if binary:
        # Binary exists. Check if it is registered with `uv tool`.
        uv_path = shutil.which("uv")
        if uv_path:
            try:
                result = subprocess.run(
                    [uv_path, "tool", "list"],
                    capture_output=True,
                    text=True,
                    timeout=MCP_PROBE_TIMEOUT,
                )
                if result.returncode == 0 and _uv_tool_list_has_mcp(result.stdout):
                    return "uv_tool"
            except (subprocess.TimeoutExpired, OSError):
                pass
        # Binary exists but not under uv tool -- treat as pip env install.
        return "pip_env"

    # No binary on PATH; try importlib.metadata.
    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import distribution as _dist

        try:
            _dist(MCP_PACKAGE_NAME)
            return "pip_env"
        except PackageNotFoundError:
            pass
    except ImportError:
        pass

    # Fallback: uvx cache only.
    if shutil.which("uvx"):
        return "uvx"

    return "none"


def _perform_mcp_update(
    method: str | None = None,
    timeout: float = MCP_UPGRADE_TIMEOUT,
) -> tuple[bool, str]:
    """Run the appropriate upgrade command for keboola-mcp-server.

    Args:
        method: Optional install method; if None, detect via
            :func:`_detect_mcp_install_method`.
        timeout: Subprocess timeout in seconds.

    Returns:
        Tuple of ``(success, output_or_reason)``.
    """
    if method is None:
        method = _detect_mcp_install_method()

    cmd: list[str] | None = None
    if method == "uv_tool":
        uv_path = shutil.which("uv")
        if uv_path is None:
            return False, "uv not found on PATH"
        cmd = [uv_path, "tool", "upgrade", MCP_PACKAGE_NAME]
    elif method == "pip_env":
        pip_path = shutil.which("pip")
        if pip_path is None:
            return False, "pip not found on PATH"
        cmd = [pip_path, "install", "--upgrade", MCP_PACKAGE_NAME]
    elif method == "uvx":
        # Promote uvx-cache install to a persistent `uv tool install`.
        # The previous strategy (`uvx --refresh ... <bin> --version`) was
        # broken: the upstream MCP binary does NOT honour --version (it
        # rejects the arg and exits non-zero), so the upgrade banner
        # always reported failure even when the cache refresh itself
        # worked. `uv tool install --upgrade` does the equivalent
        # refresh AND moves the binary to PATH so subsequent runs use the
        # faster `uv_tool` detection path. Bug B fix from issue #263.
        uv_path = shutil.which("uv")
        if uv_path is None:
            return False, "uv not found on PATH (needed to promote uvx cache to uv tool)"
        cmd = [uv_path, "tool", "install", "--upgrade", MCP_PACKAGE_NAME]
    elif method == "none":
        return False, "keboola-mcp-server is not installed"
    else:
        return False, f"unknown install method: {method!r}"

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return True, (result.stdout or "").strip()
        return False, (result.stderr or "").strip() or "upgrade subprocess failed"
    except subprocess.TimeoutExpired:
        return False, f"upgrade timed out after {timeout}s"
    except OSError as exc:
        return False, f"subprocess error: {exc}"


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
        # Strip leading 'v' from tag (e.g. 'v0.16.0' -> '0.16.0')
        version = tag.lstrip("v")
        if re.match(r"\d+\.\d+\.\d+", version):
            return version
        return None
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


def _fetch_mcp_latest_version(timeout: float = VERSION_CHECK_TIMEOUT) -> str | None:
    """Fetch latest keboola-mcp-server version from PyPI.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Version string like '1.46.0', or None on failure.
    """
    try:
        response = httpx.get(
            MCP_PYPI_URL,
            timeout=timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        data = response.json()
        version = data.get("info", {}).get("version", "")
        if re.match(r"\d+\.\d+\.\d+", version):
            return version
        return None
    except (httpx.HTTPError, KeyError, ValueError):
        logger.debug("Failed to fetch latest MCP server version", exc_info=True)
        return None


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


class VersionService:
    """Business logic for version detection and update checks.

    Detects local versions of kbagent and checks for available
    keboola-mcp-server updates.
    """

    def get_versions(self, *, include_prerelease: bool = False) -> dict[str, Any]:
        """Get version information for kbagent and its dependency.

        Both ``kbagent`` and ``keboola-mcp-server`` are auto-updated on
        startup (see :mod:`keboola_agent_cli.auto_update`) and explicitly
        via ``kbagent update``. This method reports:

        - the local installed version (best-effort detection for MCP),
        - the latest available version (GitHub Releases for kbagent,
          PyPI for MCP),
        - the up-to-date status,
        - the install method for MCP (drives which upgrade command runs),
        - the upgrade command shown to the user.

        Args:
            include_prerelease: When True, ``latest_version`` for kbagent
                reflects the newest pre-release (beta / rc) if one is more
                recent than the latest stable; surfaces what ``kbagent
                update --beta`` would install. MCP's PyPI lookup is not
                gated (MCP releases do not currently use pre-release tags).

        Returns:
            Structured dict with kbagent + MCP version info.
        """
        kbagent_latest = _fetch_kbagent_latest_version(include_prerelease=include_prerelease)
        kbagent_up_to_date = _is_up_to_date(__version__, kbagent_latest)

        mcp_local = _get_local_mcp_version()
        mcp_latest = _fetch_mcp_latest_version()
        mcp_up_to_date = _is_up_to_date(mcp_local, mcp_latest)
        mcp_method = _detect_mcp_install_method()

        # Persist the freshly-fetched versions to the auto-update cache so
        # the next ``kbagent <anything>`` startup hook sees them instead of
        # a stale TTL'd entry. Before v0.41.1 this method bypassed the
        # cache entirely, which meant ``kbagent version`` would show
        # ``v0.41.0 available`` while a follow-up ``kbagent serve --ui`` on
        # the same machine still auto-updated to whatever stale version
        # the cache held (e.g. 0.40.3). Lazy-imported to avoid a circular
        # import: ``auto_update`` imports from this module at module load.
        try:
            from ..auto_update import _write_cache

            _write_cache(
                latest_version=kbagent_latest,
                mcp_latest_version=mcp_latest,
                mcp_install_method=mcp_method,
            )
        except Exception:
            # Cache write is best-effort; never fail the version command
            # because the cache could not be persisted (read-only HOME,
            # disk full, permission error, etc.).
            logger.debug("Could not persist version cache", exc_info=True)

        # Map install method to the upgrade command shown to users. Must
        # match what `_perform_mcp_update` actually runs internally -- since
        # v0.30.3 the uvx path promotes to `uv tool install --upgrade`
        # (Bug B fix from issue #263), so the user-facing recommendation
        # must reflect that, not the broken `uvx --refresh ... --version`
        # chain the v0.30.1 logic used.
        mcp_upgrade_cmd_by_method = {
            "uv_tool": f"uv tool upgrade {MCP_PACKAGE_NAME}",
            "pip_env": f"pip install --upgrade {MCP_PACKAGE_NAME}",
            "uvx": f"uv tool install --upgrade {MCP_PACKAGE_NAME}",
            "none": f"uv tool install {MCP_PACKAGE_NAME}",
        }

        mcp_entry: dict[str, Any] = {
            "name": MCP_PACKAGE_NAME,
            "description": "Keboola MCP Server",
            "uvx_available": _is_uvx_available(),
            "version": mcp_local,
            "latest_version": mcp_latest,
            "up_to_date": mcp_up_to_date,
            "install_method": mcp_method,
            "auto_updates": True,
            "upgrade_command": mcp_upgrade_cmd_by_method.get(
                mcp_method, mcp_upgrade_cmd_by_method["none"]
            ),
        }

        # Reflect the actual install command the user should run, including
        # --prerelease=allow and @v<version> tag-pin when --beta is active.
        # Without this, programmatic JSON consumers reading upgrade_command
        # would copy a stable-channel install command even though
        # latest_version advertised a beta tag -- silently landing on the
        # wrong version.
        kbagent_target_version = kbagent_latest if include_prerelease else None
        kbagent_upgrade_cmd = build_kbagent_upgrade_command(
            prerelease=include_prerelease, target_version=kbagent_target_version
        )
        kbagent_upgrade_str = (
            " ".join(kbagent_upgrade_cmd)
            if kbagent_upgrade_cmd is not None
            else f"uv tool install --upgrade {KBAGENT_INSTALL_SOURCE}"
        )
        return {
            "kbagent": {
                "version": __version__,
                "latest_version": kbagent_latest,
                "up_to_date": kbagent_up_to_date,
                "upgrade_command": kbagent_upgrade_str,
            },
            "dependencies": [
                mcp_entry,
            ],
        }

    def self_update(self, *, include_prerelease: bool = False) -> dict[str, Any]:
        """Update kbagent + keboola-mcp-server to the latest versions.

        Two-stage flow (both stages always run -- kbagent up-to-date does
        not skip the MCP stage, and vice versa):

        1. **kbagent** -- uses ``uv tool install --upgrade`` (preferred)
           or pip fallback. If the installed kbagent is already at the
           latest version, the stage reports ``updated=False`` without
           running a subprocess.
        2. **keboola-mcp-server** -- detects install method via
           :func:`_detect_mcp_install_method`, runs the matching upgrade
           command (``uv tool upgrade`` / ``pip install -U`` /
           ``uvx --refresh``), and reports the before / after versions.
           If the local MCP version cannot be detected (e.g. uvx-cache-only
           install on first run) the stage still attempts the upgrade --
           a refreshed cache is the desired outcome there.

        Args:
            include_prerelease: When True (driven by ``kbagent update --beta``
                or ``KBAGENT_INCLUDE_PRERELEASE=1``), kbagent's version
                lookup considers pre-release (beta / rc) releases and the
                install command opts into resolver-level pre-release
                acceptance. The MCP stage is unaffected -- MCP releases
                do not use pre-release tags today.

        Returns:
            Dict with both stages' results::

                {
                    "kbagent": {"updated": bool, "current_version": str,
                                "latest_version": str|None, "message": str,
                                "output": str|None},
                    "mcp": {"updated": bool|None, "current_version": str|None,
                            "latest_version": str|None, "install_method": str,
                            "message": str, "output": str|None},
                    "updated": bool,    # True iff at least one stage upgraded
                    "message": str,     # Human-readable single-line summary
                }
        """
        kbagent_result = self._update_kbagent(include_prerelease=include_prerelease)
        mcp_result = self._update_mcp()

        any_updated = bool(kbagent_result.get("updated") or mcp_result.get("updated"))
        summary = self._compose_update_summary(kbagent_result, mcp_result)

        return {
            "kbagent": kbagent_result,
            "mcp": mcp_result,
            "updated": any_updated,
            "message": summary,
        }

    @staticmethod
    def _compose_update_summary(kbagent_result: dict[str, Any], mcp_result: dict[str, Any]) -> str:
        """Build a one-line summary of the two-stage update result."""
        parts: list[str] = []
        if kbagent_result.get("updated"):
            parts.append(
                f"kbagent v{kbagent_result.get('current_version')}"
                f" -> v{kbagent_result.get('latest_version')}"
            )
        elif kbagent_result.get("current_version") and kbagent_result.get("latest_version"):
            parts.append(f"kbagent v{kbagent_result.get('current_version')} (already up to date)")

        if mcp_result.get("updated"):
            current = mcp_result.get("current_version") or "unknown"
            latest = mcp_result.get("latest_version") or "?"
            parts.append(f"keboola-mcp-server v{current} -> v{latest}")
        elif mcp_result.get("updated") is False and mcp_result.get("current_version"):
            parts.append(
                f"keboola-mcp-server v{mcp_result.get('current_version')} (already up to date)"
            )
        elif mcp_result.get("updated") is False:
            parts.append(f"keboola-mcp-server: {mcp_result.get('message', 'skipped')}")

        if not parts:
            return "Nothing to update."
        return " | ".join(parts)

    @staticmethod
    def _update_kbagent(*, include_prerelease: bool = False) -> dict[str, Any]:
        """Run the kbagent self-upgrade subprocess (or short-circuit).

        Args:
            include_prerelease: When True (driven by ``kbagent update --beta``
                or ``KBAGENT_INCLUDE_PRERELEASE=1``), the version lookup
                considers beta / rc releases and the install command
                propagates ``--prerelease=allow`` so the resolver accepts
                PEP 440 pre-release tags like ``0.43.0b1``.
        """
        old_version = __version__
        kbagent_latest = _fetch_kbagent_latest_version(include_prerelease=include_prerelease)
        up_to_date = _is_up_to_date(old_version, kbagent_latest)

        if up_to_date is True:
            return {
                "updated": False,
                "current_version": old_version,
                "latest_version": kbagent_latest,
                "message": f"kbagent v{old_version} is already up to date.",
            }

        # Tag-pin the install URL ONLY for beta opt-in (Variant B fix).
        # Stable upgrades intentionally pass target_version=None so uv
        # resolves main HEAD as before -- main IS the stable channel.
        target_version = kbagent_latest if include_prerelease else None
        cmd = build_kbagent_upgrade_command(
            prerelease=include_prerelease, target_version=target_version
        )
        if cmd is None:
            with_flag = "--with 'keboola-agent-cli[server]' " if has_server_extras() else ""
            pre_flag = "--prerelease=allow " if include_prerelease else ""
            tag_suffix = f"@v{target_version}" if target_version else ""
            return {
                "updated": False,
                "current_version": old_version,
                "latest_version": kbagent_latest,
                "message": (
                    "Neither 'uv' nor 'pip' found on PATH. "
                    f"Install manually: uv tool install --upgrade {pre_flag}{with_flag}"
                    f"{KBAGENT_INSTALL_SOURCE}{tag_suffix}"
                ),
            }

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                return {
                    "updated": True,
                    "current_version": old_version,
                    "latest_version": kbagent_latest,
                    "message": f"Updated kbagent from v{old_version} to v{kbagent_latest}. "
                    "Restart your shell to use the new version.",
                    "output": result.stdout.strip(),
                }
            return {
                "updated": False,
                "current_version": old_version,
                "latest_version": kbagent_latest,
                "message": f"Update failed: {result.stderr.strip()}",
                "output": result.stderr.strip(),
            }
        except subprocess.TimeoutExpired:
            return {
                "updated": False,
                "current_version": old_version,
                "latest_version": kbagent_latest,
                "message": "Update timed out after 120 seconds.",
            }

    @staticmethod
    def _update_mcp() -> dict[str, Any]:
        """Run the keboola-mcp-server upgrade subprocess (or short-circuit)."""
        method = _detect_mcp_install_method()
        local_version = _get_local_mcp_version()
        latest_version = _fetch_mcp_latest_version()
        up_to_date = _is_up_to_date(local_version, latest_version)

        # Short-circuit: detected and up-to-date.
        if up_to_date is True:
            return {
                "updated": False,
                "current_version": local_version,
                "latest_version": latest_version,
                "install_method": method,
                "message": f"keboola-mcp-server v{local_version} is already up to date.",
            }

        # Short-circuit: nothing to upgrade against.
        if method == "none":
            return {
                "updated": False,
                "current_version": local_version,
                "latest_version": latest_version,
                "install_method": method,
                "message": (
                    "keboola-mcp-server is not installed. "
                    f"Install with: uv tool install {MCP_PACKAGE_NAME}"
                ),
            }

        # Run the upgrade.
        success, output = _perform_mcp_update(method=method, timeout=MCP_UPGRADE_TIMEOUT)
        post_version = _get_local_mcp_version() if success else local_version

        # Bug E fix from issue #263: subprocess returncode == 0 is NOT
        # enough to claim the upgrade happened. `uv tool upgrade` exits 0
        # even when its resolver backtracks to the previously installed
        # version (e.g. a transitive-dep constraint blocks the latest).
        # `updated` reflects the actual version delta, not just exit code.
        # The four success-branch cases:
        #   1. pre is None, post is set     -> fresh install; updated=True
        #   2. pre is set,  post is set, !=  -> normal upgrade; updated=True
        #   3. pre is set,  post is set, ==  -> Bug E no-op; updated=False
        #   4. pre / post unknown            -> probe failure; updated=False
        actually_updated = bool(
            success and post_version and (local_version is None or post_version != local_version)
        )

        if not success:
            message = f"keboola-mcp-server upgrade failed: {output}"
        elif actually_updated:
            message = (
                f"Upgraded keboola-mcp-server "
                f"({local_version or 'unknown'} -> {post_version}) via {method}."
            )
        elif post_version is None:
            message = (
                f"keboola-mcp-server upgrade ran via {method}; post-upgrade probe failed "
                f"(latest on PyPI: v{latest_version})."
            )
        else:
            # Subprocess exit 0 but local version unchanged.
            message = (
                f"keboola-mcp-server upgrade exit 0 but local version still "
                f"v{local_version} (latest: v{latest_version}). Possible Python or "
                f"dependency-version mismatch -- run `uv tool install --reinstall "
                f"{MCP_PACKAGE_NAME}` to diagnose."
            )

        return {
            "updated": actually_updated,
            "current_version": local_version,
            "latest_version": latest_version,
            "post_upgrade_version": post_version,
            "install_method": method,
            "message": message,
            "output": output,
        }
