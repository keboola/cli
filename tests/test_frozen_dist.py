"""Tests for frozen-build detection and native-channel mapping.

Guards the invariant behind :mod:`keboola_agent_cli.frozen_dist`: a kbagent
delivered as a PyInstaller binary must never be handed a uv/pip install command,
because that installs a SECOND, unrelated copy instead of upgrading the running
one. See the module docstring for the empirical background.
"""

import sys
from unittest.mock import patch

import pytest

from keboola_agent_cli.constants import (
    NATIVE_PACKAGE_NAME,
    NATIVE_RELEASES_URL,
    NATIVE_WINGET_PACKAGE_ID,
)
from keboola_agent_cli.frozen_dist import (
    _UPGRADE_COMMANDS,
    FrozenChannel,
    _classify_channel,
    detect_frozen_distribution,
    is_frozen_build,
)

# Representative real install locations per channel. The Windows entries keep
# native backslashes on purpose -- normalization is part of what is under test.
CHOCOLATEY_PATH = r"C:\ProgramData\chocolatey\lib\keboola-cli2\tools\kbagent.exe"
WINGET_PATH = (
    r"C:\Users\me\AppData\Local\Microsoft\WinGet\Packages"
    r"\Keboola.KeboolaCLI2_Microsoft.Winget.Source_8wekyb3d8bbwe\kbagent.exe"
)
HOMEBREW_CELLAR_PATH = "/opt/homebrew/Cellar/keboola-cli2/0.77.0/bin/kbagent"
HOMEBREW_LINK_PATH = "/opt/homebrew/bin/kbagent"
LINUXBREW_PATH = "/home/linuxbrew/.linuxbrew/bin/kbagent"
SYSTEM_PATH = "/usr/bin/kbagent"


def _which_only(*available: str):
    """Build a ``shutil.which`` stub where only ``available`` binaries exist."""

    def fake_which(name: str, *args, **kwargs) -> str | None:
        return f"/usr/bin/{name}" if name in available else None

    return fake_which


class TestIsFrozenBuild:
    """The two PyInstaller markers, independently and together."""

    def test_plain_python_is_not_frozen(self):
        """The test suite itself runs under a normal interpreter."""
        assert is_frozen_build() is False

    def test_sys_frozen_marker(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert is_frozen_build() is True

    def test_meipass_marker_alone(self, monkeypatch):
        """``sys._MEIPASS`` without ``sys.frozen`` still counts."""
        monkeypatch.setattr(sys, "_MEIPASS", "/tmp/_MEI123", raising=False)
        assert is_frozen_build() is True

    def test_frozen_false_is_not_frozen(self, monkeypatch):
        """A falsy ``sys.frozen`` must not be read as truthy."""
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        assert is_frozen_build() is False


class TestClassifyChannel:
    """Path -> channel mapping, exercised across OSes from any host."""

    @pytest.mark.parametrize(
        ("binary_path", "platform", "expected"),
        [
            (CHOCOLATEY_PATH, "win32", FrozenChannel.CHOCOLATEY),
            (WINGET_PATH, "win32", FrozenChannel.WINGET),
            (HOMEBREW_CELLAR_PATH, "darwin", FrozenChannel.HOMEBREW),
            # sys.executable may report the symlink rather than the Cellar file.
            (HOMEBREW_LINK_PATH, "darwin", FrozenChannel.HOMEBREW),
            (LINUXBREW_PATH, "linux", FrozenChannel.HOMEBREW),
            # Unattributable paths degrade to ARCHIVE, never to a wrong guess.
            ("/Users/me/Downloads/kbagent", "darwin", FrozenChannel.ARCHIVE),
            ("/home/me/.local/bin/kbagent", "linux", FrozenChannel.ARCHIVE),
        ],
    )
    def test_channel_detected(self, binary_path, platform, expected):
        assert _classify_channel(binary_path, platform=platform) is expected

    def test_windows_path_case_is_ignored(self):
        """Windows paths are case-insensitive; detection must be too."""
        assert (
            _classify_channel(CHOCOLATEY_PATH.upper(), platform="win32") is FrozenChannel.CHOCOLATEY
        )

    @pytest.mark.parametrize("platform", ["darwin", "linux"])
    def test_usr_local_bin_is_an_archive_not_a_system_package(self, platform):
        """/usr/local/bin is a hand-unpacked archive on EVERY platform.

        nfpm installs the deb/rpm exclusively to /usr/bin (build/package/
        nfpm.yaml); nothing we ship ever writes /usr/local/bin. Attributing it
        to the system package manager would hand a Debian/Ubuntu user
        `apt-get install --only-upgrade keboola-cli2` for a package that was
        never installed -- it fails with "unable to locate package" and points
        nowhere. The Linux case is the one that bit: `apt-get` exists on
        essentially every such host, so the misattribution was silent.
        """
        with patch("keboola_agent_cli.frozen_dist.shutil.which", _which_only("apt-get")):
            assert _classify_channel("/usr/local/bin/kbagent", platform=platform) is (
                FrozenChannel.ARCHIVE
            )

    def test_empty_path_degrades_to_archive(self):
        """A missing sys.executable must not raise."""
        assert _classify_channel("", platform="linux") is FrozenChannel.ARCHIVE


class TestLinuxSystemPackageManager:
    """/usr/bin on Linux is deb or rpm -- resolved by probing the manager."""

    def test_apt_host_is_debian(self):
        with patch("keboola_agent_cli.frozen_dist.shutil.which", _which_only("apt-get")):
            assert _classify_channel(SYSTEM_PATH, platform="linux") is FrozenChannel.DEBIAN

    def test_dnf_host_is_rpm(self):
        with patch("keboola_agent_cli.frozen_dist.shutil.which", _which_only("dnf")):
            assert _classify_channel(SYSTEM_PATH, platform="linux") is FrozenChannel.RPM

    def test_yum_host_is_rpm(self):
        """Older RHEL/CentOS ship yum without dnf."""
        with patch("keboola_agent_cli.frozen_dist.shutil.which", _which_only("yum")):
            assert _classify_channel(SYSTEM_PATH, platform="linux") is FrozenChannel.RPM

    def test_apt_wins_over_dnf_when_both_present(self):
        """Deterministic on a host carrying both (e.g. via a compat package)."""
        with patch("keboola_agent_cli.frozen_dist.shutil.which", _which_only("apt-get", "dnf")):
            assert _classify_channel(SYSTEM_PATH, platform="linux") is FrozenChannel.DEBIAN

    def test_no_package_manager_stays_generic(self):
        """Never invent a manager we could not find."""
        with patch("keboola_agent_cli.frozen_dist.shutil.which", _which_only()):
            assert _classify_channel(SYSTEM_PATH, platform="linux") is FrozenChannel.SYSTEM


class TestDetectFrozenDistribution:
    """The public entry point consumed by both update paths."""

    def test_non_frozen_returns_none(self):
        """A normal Python install must keep the uv/pip path untouched."""
        assert detect_frozen_distribution() is None

    def test_frozen_chocolatey(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", CHOCOLATEY_PATH)
        dist = detect_frozen_distribution()
        assert dist is not None
        assert dist.channel is FrozenChannel.CHOCOLATEY
        assert dist.upgrade_command == f"choco upgrade {NATIVE_PACKAGE_NAME}"
        assert dist.upgrade_hint.endswith(f"choco upgrade {NATIVE_PACKAGE_NAME}")
        assert dist.binary_path == CHOCOLATEY_PATH

    def test_frozen_homebrew(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", HOMEBREW_CELLAR_PATH)
        dist = detect_frozen_distribution()
        assert dist is not None
        assert dist.upgrade_command == f"brew upgrade {NATIVE_PACKAGE_NAME}"

    def test_frozen_winget_uses_publisher_qualified_id(self, monkeypatch):
        """WinGet addresses the package by Publisher.Package, not by name."""
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", WINGET_PATH)
        dist = detect_frozen_distribution()
        assert dist is not None
        assert dist.upgrade_command == f"winget upgrade {NATIVE_WINGET_PACKAGE_ID}"

    def test_frozen_archive_has_no_command_but_a_usable_hint(self, monkeypatch):
        """No package manager owns this install -- point at the release page."""
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", "/Users/me/bin/kbagent")
        dist = detect_frozen_distribution()
        assert dist is not None
        assert dist.channel is FrozenChannel.ARCHIVE
        assert dist.upgrade_command is None
        assert NATIVE_RELEASES_URL in dist.upgrade_hint

    def test_unidentified_system_package_hint_names_the_package(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", SYSTEM_PATH)
        monkeypatch.setattr(sys, "platform", "linux")
        with patch("keboola_agent_cli.frozen_dist.shutil.which", _which_only()):
            dist = detect_frozen_distribution()
        assert dist is not None
        assert dist.channel is FrozenChannel.SYSTEM
        assert dist.upgrade_command is None
        assert NATIVE_PACKAGE_NAME in dist.upgrade_hint


class TestNoChannelRecommendsPythonTooling:
    """The whole point of the module, asserted directly.

    A frozen binary has no uv/pip tool environment behind it. If any channel
    ever grows a uv/pip command, the original bug is back: the user installs a
    second kbagent that shadows theirs on PATH while the real one stays stale.
    """

    @pytest.mark.parametrize("channel", list(FrozenChannel))
    def test_upgrade_command_is_never_a_python_installer(self, channel):
        command = _UPGRADE_COMMANDS.get(channel, "")
        assert "uv tool" not in command
        assert "pip install" not in command
        assert "uvx" not in command

    @pytest.mark.parametrize("channel", list(FrozenChannel))
    def test_every_channel_yields_an_actionable_hint(self, channel):
        """No channel may leave the user without a next step."""
        from keboola_agent_cli.frozen_dist import _build_upgrade_hint

        hint = _build_upgrade_hint(channel, _UPGRADE_COMMANDS.get(channel))
        assert hint.strip()
        assert "uv tool" not in hint
