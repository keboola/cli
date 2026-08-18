"""Frozen-build detection and native-distribution channel mapping.

kbagent ships through two fundamentally different channels:

1. **A Python distribution** (``uv tool install`` / ``pip install``) -- the
   self-update path rebuilds that tool environment in place.
2. **A self-contained PyInstaller binary** with no Python runtime, delivered by
   Chocolatey / WinGet / Homebrew / apt / dnf or unpacked by hand from a signed
   archive (see ``build/package/`` and ``.github/workflows/release-kbagent.yml``).

For (2) the uv/pip self-update is not merely useless, it is actively harmful:
``uv tool install --force --reinstall "keboola-cli[server] @ ..."`` creates a
**separate** uv tool environment that has nothing to do with the running binary.
The Chocolatey-managed ``kbagent.exe`` stays at the old version while a second
``kbagent`` appears in ``~/.local/bin`` -- which typically precedes the package
manager's directory on ``PATH``, so the user silently starts running a different
install than the one their package manager tracks. On a machine with no Python
at all the command simply fails and the startup banner cries wolf on every run.

This module is the single seam both update paths consult:

- :func:`is_frozen_build` -- am I running inside a PyInstaller bundle?
- :func:`detect_frozen_distribution` -- if so, which channel placed me and what
  is the correct upgrade command for it?

Why the version fallback in ``__init__.py`` does NOT already cover this: the
release workflow freezes with ``pyinstaller --collect-all keboola_agent_cli``,
and PyInstaller's ``collect_all()`` is a superset of ``--copy-metadata``. The
whole ``keboola_cli-<version>.dist-info`` directory is bundled, so
``importlib.metadata.version()`` resolves a real version inside the binary
(empirically verified: a frozen ``kbagent --version`` prints ``kbagent v0.77.0``,
not the ``0.0.0-dev`` fallback). ``_is_dev_install()`` therefore does not fire.

Worse, ``direct_url.json`` is bundled too, and it records the *build machine's*
install mode. Today CI freezes from a ``uv run`` sync, which installs the
project editable, so the bundled ``direct_url.json`` carries
``"editable": true`` and ``_is_dev_install()`` happens to return True -- the
startup hook is suppressed by accident, not by design. Freezing from a
non-editable install (``uv pip install .``, a wheel, ``UV_NO_EDITABLE=1``, or a
future uv default change) flips that flag and every shipped binary would start
running ``uv tool install`` on startup. Both states were reproduced locally.
Hence an explicit guard rather than reliance on that accident.
"""

import enum
import shutil
import sys
from dataclasses import dataclass

from .constants import (
    NATIVE_CHOCOLATEY_PATH_MARKERS,
    NATIVE_HOMEBREW_PATH_MARKERS,
    NATIVE_PACKAGE_NAME,
    NATIVE_RELEASES_URL,
    NATIVE_SYSTEM_BIN_PREFIXES,
    NATIVE_WINGET_PACKAGE_ID,
    NATIVE_WINGET_PATH_MARKERS,
)


class FrozenChannel(enum.StrEnum):
    """Native packaging channel that placed a frozen kbagent binary.

    :class:`enum.StrEnum` so members serialize straight into ``--json`` output
    and compare equal to their plain-string form.
    """

    CHOCOLATEY = "chocolatey"
    WINGET = "winget"
    HOMEBREW = "homebrew"
    DEBIAN = "debian"
    RPM = "rpm"
    #: A Linux system-bin install whose package manager could not be identified.
    SYSTEM = "system"
    #: Hand-unpacked archive (or any path we cannot attribute to a channel).
    ARCHIVE = "archive"


#: Exact upgrade command per channel: package identity comes from constants,
#: the verb is inherent to the tool. Channels absent from this map have no
#: single correct command -- they fall back to :data:`NATIVE_RELEASES_URL`.
_UPGRADE_COMMANDS: dict[FrozenChannel, str] = {
    FrozenChannel.CHOCOLATEY: f"choco upgrade {NATIVE_PACKAGE_NAME}",
    FrozenChannel.WINGET: f"winget upgrade {NATIVE_WINGET_PACKAGE_ID}",
    FrozenChannel.HOMEBREW: f"brew upgrade {NATIVE_PACKAGE_NAME}",
    FrozenChannel.DEBIAN: f"sudo apt-get install --only-upgrade {NATIVE_PACKAGE_NAME}",
    FrozenChannel.RPM: f"sudo dnf upgrade {NATIVE_PACKAGE_NAME}",
}


@dataclass(frozen=True)
class FrozenDistribution:
    """How the running frozen binary was installed, and how to upgrade it.

    Attributes:
        channel: Detected packaging channel.
        binary_path: The running binary's own path (``sys.executable``), kept so
            failure reports can show *which* install the guard matched on.
        upgrade_command: Exact copy-pasteable command, or None when the channel
            has no single correct one (hand-unpacked archive / unidentified
            system package). Machine-readable half of the pair.
        upgrade_hint: Always-populated human sentence naming the right action.
    """

    channel: FrozenChannel
    binary_path: str
    upgrade_command: str | None
    upgrade_hint: str


def is_frozen_build() -> bool:
    """Whether this process is a PyInstaller-frozen binary.

    ``sys.frozen`` is the documented marker and ``sys._MEIPASS`` (the unpacked
    bundle directory) is checked too: the pair is the standard belt-and-braces
    probe, and other freezers set only one of them.
    """
    return bool(getattr(sys, "frozen", False)) or hasattr(sys, "_MEIPASS")


def _classify_system_package_manager() -> FrozenChannel:
    """Distinguish a deb-based from an rpm-based host for a system-bin install.

    ``build/package/nfpm.yaml`` produces both a ``.deb`` and an ``.rpm`` that
    install to the same ``/usr/bin/kbagent``, so the path alone cannot say which
    one is present, and probing for the package manager binary is what
    distinguishes them.
    """
    if shutil.which("apt-get") is not None:
        return FrozenChannel.DEBIAN
    if shutil.which("dnf") is not None or shutil.which("yum") is not None:
        return FrozenChannel.RPM
    return FrozenChannel.SYSTEM


def _classify_channel(binary_path: str, *, platform: str | None = None) -> FrozenChannel:
    """Map the running binary's own path to the channel that placed it.

    Args:
        binary_path: Path of the running executable (``sys.executable``).
        platform: ``sys.platform`` override. Keyword-only and injectable so the
            per-OS branches are testable from any host.

    Returns:
        The detected channel, or :attr:`FrozenChannel.ARCHIVE` when the path
        cannot be attributed -- never a wrong-but-plausible guess.
    """
    resolved_platform = platform if platform is not None else sys.platform
    # Lower-case with forward slashes so one marker set matches every OS.
    normalized = binary_path.replace("\\", "/").casefold()

    if any(marker in normalized for marker in NATIVE_CHOCOLATEY_PATH_MARKERS):
        return FrozenChannel.CHOCOLATEY
    if any(marker in normalized for marker in NATIVE_WINGET_PATH_MARKERS):
        return FrozenChannel.WINGET
    if any(marker in normalized for marker in NATIVE_HOMEBREW_PATH_MARKERS):
        return FrozenChannel.HOMEBREW
    # deb/rpm are Linux-only. On macOS /usr/local/bin holds a hand-unpacked
    # archive (the Homebrew formula refuses Intel Macs outright), so claiming a
    # system package there would send the user to a package manager they do not
    # have.
    if resolved_platform.startswith("linux") and any(
        normalized.startswith(prefix) for prefix in NATIVE_SYSTEM_BIN_PREFIXES
    ):
        return _classify_system_package_manager()
    return FrozenChannel.ARCHIVE


def _build_upgrade_hint(channel: FrozenChannel, upgrade_command: str | None) -> str:
    """Render the human-facing upgrade sentence for a channel."""
    if upgrade_command is not None:
        return f"upgrade it with: {upgrade_command}"
    if channel is FrozenChannel.SYSTEM:
        return (
            f"upgrade the '{NATIVE_PACKAGE_NAME}' system package with your "
            f"distribution's package manager"
        )
    return f"re-download the signed archive from {NATIVE_RELEASES_URL}"


def detect_frozen_distribution() -> FrozenDistribution | None:
    """Describe the native install backing this process.

    Returns:
        A :class:`FrozenDistribution` when running as a frozen binary, or None
        for a normal Python (uv / pip / editable) install -- in which case the
        regular uv/pip self-update path is correct and must proceed untouched.
    """
    if not is_frozen_build():
        return None
    # For a PyInstaller onefile build sys.executable is the binary itself (the
    # unpacked bundle lives in sys._MEIPASS), which is exactly the path whose
    # location identifies the channel. Verified against a real frozen build.
    binary_path = sys.executable or ""
    channel = _classify_channel(binary_path)
    upgrade_command = _UPGRADE_COMMANDS.get(channel)
    return FrozenDistribution(
        channel=channel,
        binary_path=binary_path,
        upgrade_command=upgrade_command,
        upgrade_hint=_build_upgrade_hint(channel, upgrade_command),
    )
