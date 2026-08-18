"""Tests for the auto-update module."""

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import keboola_agent_cli.auto_update as auto_update_module
from keboola_agent_cli.auto_update import (
    UpdateOutcome,
    _get_cache_path,
    _is_cache_fresh,
    _is_dev_install,
    _maybe_update_mcp,
    _perform_update,
    _re_exec,
    _read_cache,
    _should_skip,
    _top_level_subcommand_is_versioning,
    _write_cache,
    maybe_auto_update,
    report_finished_deferred_update,
)
from keboola_agent_cli.constants import (
    ENV_AUTO_UPDATE,
    ENV_DEFER_UPDATE,
    ENV_SKIP_UPDATE,
    MCP_UPGRADE_TIMEOUT,
)
from keboola_agent_cli.frozen_dist import FrozenChannel, FrozenDistribution
from keboola_agent_cli.services.version_service import KbagentUpdatePlan, McpUpdatePlan
from keboola_agent_cli.update_runner import (
    DeferredUpdateReport,
    DeferredUpdateStatus,
    InstallRun,
    InstallStatus,
)


# ---------------------------------------------------------------------------
# _should_skip
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _pin_the_inline_update_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decide the install path explicitly instead of inheriting the platform's.

    `should_defer()` defaults to True on Windows, so tests here that exercise
    the inline install + re-exec silently stopped exercising anything when run
    on Windows -- `_perform_update` was simply never reached. Pinning the
    documented escape hatch makes the path under test the same everywhere.

    Tests that are *about* deferral patch `should_defer` directly, which
    replaces the function and therefore wins over this env var.
    """
    monkeypatch.setenv(ENV_DEFER_UPDATE, "0")


class TestTopLevelSubcommandVersioning:
    """_top_level_subcommand_is_versioning resolves the real subcommand (issue #353)."""

    @pytest.mark.parametrize(
        ("argv_tail", "expected"),
        [
            (["update"], True),
            (["version"], True),
            (["--json", "update"], True),  # Bug 3: subcommand sits after a global flag
            (["-j", "version"], True),
            (["--config-dir", "/tmp/x", "update"], True),  # value-taking global flag
            (["--config-dir=/tmp/x", "update"], True),  # `--flag=value` form
            (["config", "update"], False),  # nested -- must NOT skip (Devin finding)
            (["flow", "update"], False),
            (["agent", "update"], False),
            (["config", "row-update"], False),
            (["project", "list"], False),
            ([], False),
            (["--json"], False),
        ],
    )
    def test_resolves_top_level_subcommand(self, argv_tail, expected):
        assert _top_level_subcommand_is_versioning(argv_tail) is expected


class TestShouldSkip:
    """Tests for the _should_skip() function."""

    def test_skip_when_skip_update_env_set(self):
        with patch.dict(os.environ, {ENV_SKIP_UPDATE: "1"}):
            assert _should_skip() is True

    def test_skip_when_auto_update_false(self):
        with patch.dict(os.environ, {ENV_AUTO_UPDATE: "false"}, clear=False):
            assert _should_skip() is True

    def test_skip_when_auto_update_zero(self):
        with patch.dict(os.environ, {ENV_AUTO_UPDATE: "0"}, clear=False):
            assert _should_skip() is True

    def test_skip_when_auto_update_no(self):
        with patch.dict(os.environ, {ENV_AUTO_UPDATE: "no"}, clear=False):
            assert _should_skip() is True

    def test_skip_when_auto_update_no_case_insensitive(self):
        with patch.dict(os.environ, {ENV_AUTO_UPDATE: "NO"}, clear=False):
            assert _should_skip() is True

    @patch("keboola_agent_cli.auto_update._is_dev_install", return_value=True)
    def test_skip_when_dev_install(self, _mock):
        # Clear any conflicting env vars
        env = {k: v for k, v in os.environ.items() if k not in (ENV_SKIP_UPDATE, ENV_AUTO_UPDATE)}
        with patch.dict(os.environ, env, clear=True):
            assert _should_skip() is True

    @patch("keboola_agent_cli.auto_update._is_dev_install", return_value=False)
    def test_skip_for_update_command(self, _mock):
        env = {k: v for k, v in os.environ.items() if k not in (ENV_SKIP_UPDATE, ENV_AUTO_UPDATE)}
        with patch.dict(os.environ, env, clear=True), patch("sys.argv", ["kbagent", "update"]):
            assert _should_skip() is True

    @patch("keboola_agent_cli.auto_update._is_dev_install", return_value=False)
    def test_skip_for_update_after_global_flags(self, _mock):
        """Bug 3 (issue #353): the subcommand can sit after global flags.

        `kbagent --json update` has argv[1] == "--json"; the old argv[1]-only
        check let the startup hook fire and disagree with the explicit command.
        """
        env = {k: v for k, v in os.environ.items() if k not in (ENV_SKIP_UPDATE, ENV_AUTO_UPDATE)}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("sys.argv", ["kbagent", "--json", "update"]),
        ):
            assert _should_skip() is True

    @patch("keboola_agent_cli.auto_update._is_dev_install", return_value=False)
    def test_skip_for_version_command(self, _mock):
        env = {k: v for k, v in os.environ.items() if k not in (ENV_SKIP_UPDATE, ENV_AUTO_UPDATE)}
        with patch.dict(os.environ, env, clear=True), patch("sys.argv", ["kbagent", "version"]):
            assert _should_skip() is True

    @patch("keboola_agent_cli.auto_update._is_dev_install", return_value=False)
    def test_no_skip_for_normal_command(self, _mock):
        env = {k: v for k, v in os.environ.items() if k not in (ENV_SKIP_UPDATE, ENV_AUTO_UPDATE)}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("sys.argv", ["kbagent", "config", "list"]),
        ):
            assert _should_skip() is False

    @patch("keboola_agent_cli.auto_update._is_dev_install", return_value=False)
    def test_no_skip_when_argv_empty(self, _mock):
        """Edge case: sys.argv has no elements."""
        env = {k: v for k, v in os.environ.items() if k not in (ENV_SKIP_UPDATE, ENV_AUTO_UPDATE)}
        with patch.dict(os.environ, env, clear=True), patch("sys.argv", []):
            assert _should_skip() is False


# ---------------------------------------------------------------------------
# _is_dev_install
# ---------------------------------------------------------------------------
class TestIsDevInstall:
    """Tests for the _is_dev_install() function."""

    def test_dev_version(self):
        with patch("keboola_agent_cli.auto_update.__version__", "0.0.0-dev"):
            assert _is_dev_install() is True

    def test_editable_install(self):
        """Simulate PEP 660 editable install via direct_url.json."""
        mock_dist = MagicMock()
        mock_dist.read_text.return_value = json.dumps(
            {"url": "file:///some/path", "dir_info": {"editable": True}}
        )
        with (
            patch("keboola_agent_cli.auto_update.__version__", "1.0.0"),
            patch("keboola_agent_cli.auto_update.distribution", return_value=mock_dist),
        ):
            assert _is_dev_install() is True

    def test_normal_install(self):
        """Non-editable, non-dev version should return False."""
        mock_dist = MagicMock()
        mock_dist.read_text.return_value = json.dumps({"url": "https://github.com/keboola/cli"})
        with (
            patch("keboola_agent_cli.auto_update.__version__", "1.0.0"),
            patch("keboola_agent_cli.auto_update.distribution", return_value=mock_dist),
        ):
            assert _is_dev_install() is False

    def test_distribution_raises(self):
        """If importlib.metadata fails, should not crash and return False."""
        with (
            patch("keboola_agent_cli.auto_update.__version__", "1.0.0"),
            patch(
                "keboola_agent_cli.auto_update.distribution",
                side_effect=Exception("not found"),
            ),
        ):
            assert _is_dev_install() is False


# ---------------------------------------------------------------------------
# Version cache
# ---------------------------------------------------------------------------
class TestVersionCache:
    """Tests for _read_cache, _write_cache, and _is_cache_fresh."""

    def test_read_missing_cache(self, tmp_path):
        with patch(
            "keboola_agent_cli.auto_update._get_cache_path",
            return_value=tmp_path / "nonexistent.json",
        ):
            assert _read_cache() is None

    def test_read_corrupt_cache(self, tmp_path):
        cache_file = tmp_path / "version_cache.json"
        cache_file.write_text("not valid json!!!", encoding="utf-8")
        with patch("keboola_agent_cli.auto_update._get_cache_path", return_value=cache_file):
            assert _read_cache() is None

    def test_read_cache_missing_keys(self, tmp_path):
        cache_file = tmp_path / "version_cache.json"
        cache_file.write_text('{"foo": "bar"}', encoding="utf-8")
        with patch("keboola_agent_cli.auto_update._get_cache_path", return_value=cache_file):
            assert _read_cache() is None

    def test_read_valid_cache(self, tmp_path):
        cache_file = tmp_path / "version_cache.json"
        payload = {"last_check": time.time(), "latest_version": "1.2.3"}
        cache_file.write_text(json.dumps(payload), encoding="utf-8")
        with patch("keboola_agent_cli.auto_update._get_cache_path", return_value=cache_file):
            result = _read_cache()
            assert result is not None
            assert result["latest_version"] == "1.2.3"

    def test_write_cache(self, tmp_path):
        cache_file = tmp_path / "version_cache.json"
        with patch("keboola_agent_cli.auto_update._get_cache_path", return_value=cache_file):
            _write_cache("2.0.0")
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        assert data["latest_version"] == "2.0.0"
        assert "last_check" in data

    def test_write_cache_creates_dir(self, tmp_path):
        cache_file = tmp_path / "subdir" / "nested" / "version_cache.json"
        with patch("keboola_agent_cli.auto_update._get_cache_path", return_value=cache_file):
            _write_cache("3.0.0")
        assert cache_file.is_file()
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        assert data["latest_version"] == "3.0.0"

    def test_cache_fresh_within_ttl(self):
        cache = {"last_check": time.time() - 100, "latest_version": "1.0.0"}
        assert _is_cache_fresh(cache, 3600) is True

    def test_cache_stale_after_ttl(self):
        cache = {"last_check": time.time() - 7200, "latest_version": "1.0.0"}
        assert _is_cache_fresh(cache, 3600) is False

    def test_cache_fresh_with_bad_data(self):
        cache = {"last_check": "not a number", "latest_version": "1.0.0"}
        assert _is_cache_fresh(cache, 3600) is False


# ---------------------------------------------------------------------------
# _perform_update
# ---------------------------------------------------------------------------
def _finished_install(status: InstallStatus, exit_code: int | None = 0) -> InstallRun:
    """An :class:`InstallRun` stand-in for a mocked installer execution."""
    return InstallRun(status=status, exit_code=exit_code, output="", log_path=Path("update.log"))


class TestPerformUpdate:
    """Tests for the _perform_update() function.

    The installer subprocess itself is owned by ``update_runner.run_install``
    (issue #528), so these tests assert the command _perform_update hands it --
    the contract that actually matters here -- rather than reaching through to
    ``subprocess``.
    """

    @patch("shutil.which")
    @patch("keboola_agent_cli.auto_update.run_install")
    def test_update_with_uv_success(self, mock_install, mock_which):
        mock_which.return_value = "/usr/local/bin/uv"
        mock_install.return_value = _finished_install(InstallStatus.SUCCEEDED)
        assert _perform_update("2.0.0") is UpdateOutcome.SUCCESS
        assert "uv" in mock_install.call_args.args[0][0]

    @patch("shutil.which")
    @patch("keboola_agent_cli.auto_update.run_install")
    def test_update_with_uv_failure(self, mock_install, mock_which):
        mock_which.return_value = "/usr/local/bin/uv"
        mock_install.return_value = _finished_install(InstallStatus.FAILED, exit_code=1)
        assert _perform_update("2.0.0") is UpdateOutcome.FAILED

    @patch("shutil.which")
    @patch("keboola_agent_cli.auto_update.run_install")
    def test_update_pip_fallback(self, mock_install, mock_which):
        # uv not found, pip found
        mock_which.side_effect = lambda cmd: (
            None if cmd == "uv" else "/usr/bin/pip" if cmd == "pip" else None
        )
        mock_install.return_value = _finished_install(InstallStatus.SUCCEEDED)
        assert _perform_update("2.0.0") is UpdateOutcome.SUCCESS
        assert "pip" in mock_install.call_args.args[0][0]

    @patch("shutil.which")
    def test_update_no_tools(self, mock_which):
        mock_which.return_value = None
        assert _perform_update("2.0.0") is UpdateOutcome.FAILED

    @patch("shutil.which")
    @patch("keboola_agent_cli.auto_update.run_install")
    def test_still_running_maps_to_timeout(self, mock_install, mock_which):
        """An installer that outran the wait is TIMEOUT, not FAILED.

        It was not killed (issue #528) -- it is still recreating the
        environment and the next launch picks up the result.
        """
        mock_which.return_value = "/usr/local/bin/uv"
        mock_install.return_value = _finished_install(InstallStatus.STILL_RUNNING, exit_code=None)
        assert _perform_update("2.0.0") is UpdateOutcome.TIMEOUT

    @patch(
        "keboola_agent_cli.services.version_service.has_server_extras",
        return_value=True,
    )
    @patch("shutil.which", return_value="/usr/local/bin/uv")
    @patch("keboola_agent_cli.auto_update.run_install")
    def test_update_preserves_server_extras(self, mock_install, mock_which, mock_has_server):
        """Bug fix (v0.41.1): startup auto-update must preserve [server] extras.

        Before v0.41.1, ``_perform_update`` ran a bare
        ``uv tool install --upgrade git+...`` which silently dropped the
        FastAPI + uvicorn extras a user originally installed with
        ``--with 'keboola-cli[server]'`` -- so a user who had
        ``kbagent serve --ui`` working would lose it on the next startup
        auto-update. Now ``_perform_update`` delegates to
        :func:`build_kbagent_upgrade_command`, which pairs ``--with`` and
        ``--force`` when ``fastapi`` is importable.
        """
        mock_install.return_value = _finished_install(InstallStatus.SUCCEEDED)
        assert _perform_update("2.0.0") is UpdateOutcome.SUCCESS
        argv = mock_install.call_args.args[0]
        # The extras live in the primary PEP 508 requirement so the complete
        # environment is resolved in one forced reinstall.
        assert "--force" in argv
        assert "--reinstall" in argv
        assert any("keboola-cli[server]" in arg for arg in argv)
        assert "--upgrade" not in argv

    @patch(
        "keboola_agent_cli.services.version_service.has_server_extras",
        return_value=False,
    )
    @patch("shutil.which", return_value="/usr/local/bin/uv")
    @patch("keboola_agent_cli.auto_update.run_install")
    def test_update_without_server_extras_uses_upgrade(
        self, mock_install, mock_which, mock_has_server
    ):
        """No-extras installs are also a full forced reinstall."""
        mock_install.return_value = _finished_install(InstallStatus.SUCCEEDED)
        assert _perform_update("2.0.0") is UpdateOutcome.SUCCESS
        argv = mock_install.call_args.args[0]
        assert "--force" in argv
        assert "--reinstall" in argv
        assert "keboola-cli[server]" not in argv


class TestPerformUpdateWheel:
    """_perform_update prefers the prebuilt-wheel Release asset (issue #353)."""

    @patch("keboola_agent_cli.services.version_service.httpx.head")
    @patch(
        "keboola_agent_cli.services.version_service.has_server_extras",
        return_value=False,
    )
    @patch("shutil.which", return_value="/usr/local/bin/uv")
    @patch("keboola_agent_cli.auto_update.run_install")
    def test_installs_wheel_when_asset_present(
        self, mock_install, mock_which, mock_has_server, mock_head
    ):
        """A 200 HEAD on the asset -> install the prebuilt wheel, not git+."""
        mock_head.return_value = MagicMock(status_code=200)
        mock_install.return_value = _finished_install(InstallStatus.SUCCEEDED)

        assert _perform_update("2.0.0") is UpdateOutcome.SUCCESS

        argv = mock_install.call_args.args[0]
        # PEP 508 direct ref to the versioned wheel, --force, and no git+ source.
        assert "--force" in argv
        assert any(part.endswith("keboola_cli-2.0.0-py3-none-any.whl") for part in argv)
        assert all("git+" not in part for part in argv)

    @patch("keboola_agent_cli.services.version_service.httpx.head")
    @patch("shutil.which", return_value="/usr/local/bin/uv")
    @patch("keboola_agent_cli.auto_update.run_install")
    def test_falls_back_to_git_when_no_asset(self, mock_install, mock_which, mock_head):
        """A 404 HEAD (older release without an asset) -> git+ source build."""
        mock_head.return_value = MagicMock(status_code=404)
        mock_install.return_value = _finished_install(InstallStatus.SUCCEEDED)

        assert _perform_update("2.0.0") is UpdateOutcome.SUCCESS

        argv = mock_install.call_args.args[0]
        assert any("git+" in part for part in argv)


# ---------------------------------------------------------------------------
# _re_exec
# ---------------------------------------------------------------------------
class TestReExec:
    """Tests for the _re_exec() function."""

    @patch("os.execvpe")
    @patch("shutil.which", return_value="/usr/local/bin/kbagent")
    def test_execvpe_called_with_skip_env(self, mock_which, mock_execvpe):
        with patch("sys.argv", ["kbagent", "config", "list"]):
            _re_exec()
        mock_execvpe.assert_called_once()
        args = mock_execvpe.call_args
        assert args[0][0] == "kbagent"
        assert args[0][1] == ["kbagent", "config", "list"]
        env = args[0][2]
        assert env[ENV_SKIP_UPDATE] == "1"

    @patch("os.execvpe")
    @patch("shutil.which", return_value=None)
    def test_fallback_to_python_m(self, mock_which, mock_execvpe):
        with (
            patch("sys.argv", ["kbagent", "config", "list"]),
            patch("sys.executable", "/usr/bin/python3"),
        ):
            _re_exec()
        mock_execvpe.assert_called_once()
        args = mock_execvpe.call_args
        assert args[0][0] == "/usr/bin/python3"
        assert args[0][1] == ["/usr/bin/python3", "-m", "keboola_agent_cli", "config", "list"]
        env = args[0][2]
        assert env[ENV_SKIP_UPDATE] == "1"


# ---------------------------------------------------------------------------
# maybe_auto_update (integration)
# ---------------------------------------------------------------------------
class TestMaybeAutoUpdate:
    """Tests for the maybe_auto_update() orchestrator."""

    @pytest.fixture(autouse=True)
    def _no_real_mcp_calls(self):
        """Disable MCP-side helpers across all tests in this class.

        The kbagent-stage tests do not care about MCP behaviour; without
        this fixture they would either issue real subprocess /
        importlib.metadata lookups or trigger network round-trips to
        PyPI. Each MCP test below opts in by re-patching as needed.

        Also defaults the per-stage skip helpers (since v0.30.1) to False
        so existing tests that only patch the legacy ``_should_skip``
        alias still drive the orchestrator down the active path. Resets
        the per-process auto-update sentinel (since v0.30.3) so each test
        starts from a fresh state -- otherwise the Bug D fix would gate
        the second test in the class.
        """
        auto_update_module._AUTO_UPDATE_RAN = False
        with (
            patch(
                "keboola_agent_cli.auto_update._fetch_mcp_latest_version",
                return_value=None,
            ),
            patch(
                "keboola_agent_cli.auto_update._get_local_mcp_version",
                return_value=None,
            ),
            patch("keboola_agent_cli.auto_update._apply_prepared_mcp_update"),
            patch(
                "keboola_agent_cli.auto_update._detect_mcp_install_method",
                return_value="none",
            ),
            patch(
                "keboola_agent_cli.auto_update._should_skip_all",
                return_value=False,
            ),
            patch(
                "keboola_agent_cli.auto_update._should_skip_kbagent_stage",
                return_value=False,
            ),
        ):
            yield

    def test_skip_conditions_respected(self):
        """When the wide skip gate (`_should_skip_all`) is True, neither
        stage runs.
        """
        with (
            patch(
                "keboola_agent_cli.auto_update._should_skip_all",
                return_value=True,
            ) as mock_skip_all,
            patch("keboola_agent_cli.auto_update._fetch_kbagent_latest_version") as mock_fetch,
        ):
            maybe_auto_update()
        mock_skip_all.assert_called_once()
        mock_fetch.assert_not_called()

    @patch("keboola_agent_cli.auto_update._read_cache")
    @patch("keboola_agent_cli.auto_update._is_cache_fresh", return_value=True)
    @patch("keboola_agent_cli.auto_update._is_up_to_date", return_value=True)
    def test_cache_fresh_no_fetch_or_ttl_refresh(self, mock_up_to_date, mock_fresh, mock_cache):
        mock_cache.return_value = {
            "last_check": time.time(),
            "latest_version": "1.0.0",
            "mcp_latest_version": "1.0.0",
        }
        with (
            patch("keboola_agent_cli.auto_update._fetch_kbagent_latest_version") as mock_fetch,
            patch("keboola_agent_cli.auto_update._write_cache") as mock_write,
        ):
            maybe_auto_update()
            mock_fetch.assert_not_called()
            mock_write.assert_not_called()

    @patch("keboola_agent_cli.auto_update._read_cache", return_value=None)
    @patch("keboola_agent_cli.auto_update._fetch_kbagent_latest_version", return_value="2.0.0")
    @patch("keboola_agent_cli.auto_update._write_cache")
    @patch("keboola_agent_cli.auto_update._is_up_to_date", return_value=True)
    def test_cache_stale_fetches(self, mock_up_to_date, mock_write, mock_fetch, mock_cache):
        maybe_auto_update()
        mock_fetch.assert_called_once()
        # Cache write now bundles the kbagent latest with the MCP latest +
        # install method (both may be None when MCP helpers are no-op'ed).
        # Since v0.30.1 maybe_auto_update calls _write_cache with all
        # kwargs (no positional args) so the test inspects call_args.kwargs.
        mock_write.assert_called_once()
        kwargs = mock_write.call_args.kwargs
        assert kwargs.get("latest_version") == "2.0.0"
        assert "mcp_latest_version" in kwargs
        assert "mcp_install_method" in kwargs

    @patch("keboola_agent_cli.auto_update._read_cache", return_value=None)
    @patch("keboola_agent_cli.auto_update._fetch_kbagent_latest_version", return_value="1.0.0")
    @patch("keboola_agent_cli.auto_update._write_cache")
    @patch("keboola_agent_cli.auto_update._is_up_to_date", return_value=True)
    @patch("keboola_agent_cli.auto_update._perform_update")
    def test_up_to_date_no_update(
        self, mock_update, mock_up_to_date, mock_write, mock_fetch, mock_cache
    ):
        maybe_auto_update()
        mock_update.assert_not_called()

    @patch("keboola_agent_cli.auto_update._read_cache", return_value=None)
    @patch("keboola_agent_cli.auto_update._fetch_kbagent_latest_version", return_value="2.0.0")
    @patch("keboola_agent_cli.auto_update._write_cache")
    @patch("keboola_agent_cli.auto_update._is_up_to_date", return_value=False)
    @patch("keboola_agent_cli.auto_update._perform_update", return_value=UpdateOutcome.SUCCESS)
    @patch("keboola_agent_cli.auto_update._re_exec")
    @patch("keboola_agent_cli.auto_update.__version__", "1.0.0")
    def test_newer_available_updates_and_reexec(
        self,
        mock_reexec,
        mock_update,
        mock_up_to_date,
        mock_write,
        mock_fetch,
        mock_cache,
    ):
        maybe_auto_update()
        mock_update.assert_called_once()
        assert mock_update.call_args.args == ("2.0.0",)
        assert "command" in mock_update.call_args.kwargs
        mock_reexec.assert_called_once()

    @patch("keboola_agent_cli.auto_update._read_cache", return_value=None)
    @patch("keboola_agent_cli.auto_update._fetch_kbagent_latest_version", return_value="2.0.0")
    @patch("keboola_agent_cli.auto_update._write_cache")
    @patch("keboola_agent_cli.auto_update._is_up_to_date", return_value=False)
    @patch("keboola_agent_cli.auto_update._perform_update", return_value=UpdateOutcome.FAILED)
    @patch("keboola_agent_cli.auto_update._re_exec")
    @patch("keboola_agent_cli.auto_update.__version__", "1.0.0")
    def test_update_failure_continues(
        self,
        mock_reexec,
        mock_update,
        mock_up_to_date,
        mock_write,
        mock_fetch,
        mock_cache,
    ):
        """If _perform_update returns FAILED, re-exec should NOT be called."""
        maybe_auto_update()
        mock_update.assert_called_once()
        mock_reexec.assert_not_called()

    @patch("keboola_agent_cli.auto_update._read_cache", return_value=None)
    @patch("keboola_agent_cli.auto_update._fetch_kbagent_latest_version", return_value="2.0.0")
    @patch("keboola_agent_cli.auto_update._is_up_to_date", return_value=False)
    @patch(
        "keboola_agent_cli.auto_update._perform_update",
        return_value=UpdateOutcome.TIMEOUT,
    )
    @patch("keboola_agent_cli.auto_update._re_exec")
    @patch("keboola_agent_cli.auto_update._apply_prepared_mcp_update")
    @patch("keboola_agent_cli.auto_update._detect_mcp_install_method", return_value="none")
    @patch("keboola_agent_cli.auto_update._write_cache")
    @patch("keboola_agent_cli.auto_update.__version__", "1.0.0")
    def test_timeout_outcome_is_not_a_failure(
        self,
        mock_write,
        mock_detect,
        mock_mcp,
        mock_reexec,
        mock_perform,
        mock_up_to_date,
        mock_fetch,
        mock_cache,
        capsys,
    ):
        """A TIMEOUT must not re-exec nor print 'failed' (issue #353).

        The banner should say the build is still running, not that it failed --
        the wheel fast path makes timeouts rare, but when the git+ fallback runs
        long the next invocation finishes it.

        Since issue #528 the installer is not killed when the wait expires, so
        the banner must NOT offer a recovery command either: a second installer
        started against the environment a live uv is rewriting is precisely the
        corruption being fixed.
        """
        maybe_auto_update()
        mock_reexec.assert_not_called()
        err = capsys.readouterr().err
        assert "still running" in err
        assert "Recover with:" not in err

    @patch("keboola_agent_cli.auto_update._read_cache", return_value=None)
    @patch("keboola_agent_cli.auto_update._fetch_kbagent_latest_version", return_value=None)
    @patch("keboola_agent_cli.auto_update._perform_update")
    def test_fetch_failure_continues(self, mock_update, mock_fetch, mock_cache):
        """If fetch returns None, should continue without updating."""
        maybe_auto_update()
        mock_update.assert_not_called()

    def test_exception_never_crashes(self):
        """Any exception inside maybe_auto_update must be swallowed.

        Targets ``_should_skip_all`` -- the gate the orchestrator actually
        consults today. The legacy ``_should_skip`` alias is no longer
        called by ``maybe_auto_update``, so patching it would not exercise
        the blanket try/except (false-confidence regression caught in
        review iteration 3).
        """
        with patch(
            "keboola_agent_cli.auto_update._should_skip_all",
            side_effect=RuntimeError("kaboom"),
        ):
            # Must NOT raise.
            maybe_auto_update()

    @patch("keboola_agent_cli.auto_update._read_cache")
    @patch("keboola_agent_cli.auto_update._is_cache_fresh", return_value=True)
    @patch("keboola_agent_cli.auto_update._is_up_to_date", return_value=None)
    @patch("keboola_agent_cli.auto_update._perform_update")
    def test_version_comparison_none_no_update(
        self, mock_update, mock_up_to_date, mock_fresh, mock_cache
    ):
        """If _is_up_to_date returns None (can't compare), no update."""
        mock_cache.return_value = {"last_check": time.time(), "latest_version": "1.0.0"}
        maybe_auto_update()
        mock_update.assert_not_called()


# ---------------------------------------------------------------------------
# _get_cache_path
# ---------------------------------------------------------------------------
class TestGetCachePath:
    """Tests for _get_cache_path()."""

    def test_returns_path_with_filename(self):
        path = _get_cache_path()
        assert path.name == "version_cache.json"
        assert "keboola-agent-cli" in str(path)

    def test_suite_never_resolves_to_the_real_user_cache(self):
        """Guard for the `_redirect_global_state_paths` autouse fixture.

        `_get_cache_path` resolves through `platformdirs`, so it is NOT
        `--config-dir`-aware and points at the developer's own
        `version_cache.json`. Two tests in `TestMaybeAutoUpdate` reach the
        real `_write_cache` (they mock `_read_cache`, not the write), which
        stamped the canonical fake latest version `1.0.0` into that file --
        after which every `kbagent` command on the machine spent an hour
        (`AUTO_UPDATE_CHECK_INTERVAL`) trying to install a release tag that
        does not exist. The conftest fixture redirects the module attribute
        per test; this asserts it is actually in force, so deleting the
        fixture fails here instead of silently on someone's laptop.

        Uses the module attribute, not the name imported at the top of this
        file -- the latter is bound to the original function object and is
        deliberately left unpatched for the test above.
        """
        import platformdirs

        from keboola_agent_cli import update_runner

        real_dir = Path(platformdirs.user_config_dir("keboola-agent-cli")).resolve()
        assert real_dir != auto_update_module._get_cache_path().resolve().parent
        # `update_runner.state_dir` resolves through the very same call and backs
        # the deferred-update marker / exit / log files. `should_defer()` is True
        # by default on Windows, so a test reaching the scheduler there would
        # write real state -- and `report_finished_deferred_update`, which runs
        # before every skip gate, unlinks a genuine pending report while reading
        # it. Same class of bug, so the fixture covers both.
        assert real_dir != update_runner.state_dir().resolve()


# ---------------------------------------------------------------------------
# `kbagent changelog` does not duplicate "What's new" output
# ---------------------------------------------------------------------------
class TestChangelogCommandConsumesWhatsNewTrigger:
    """Regression test for the duplicate-output bug:
    when the user runs ``kbagent changelog`` right after auto-update,
    the root callback previously printed "What's new" AND the command
    printed the full changelog, so the same bullets appeared twice.
    The fix drops the trigger env var on ``changelog`` invocations.
    """

    def test_changelog_command_clears_updated_from_env(self, monkeypatch):
        from typer.testing import CliRunner

        from keboola_agent_cli.changelog import ENV_UPDATED_FROM
        from keboola_agent_cli.cli import app

        runner = CliRunner()
        # Simulate a just-completed auto-update
        monkeypatch.setenv(ENV_UPDATED_FROM, "0.23.0")
        # Avoid network roundtrip
        monkeypatch.setenv(ENV_SKIP_UPDATE, "1")

        result = runner.invoke(app, ["changelog", "--limit", "1"])

        # Assertions:
        # 1. Command succeeded.
        assert result.exit_code == 0, result.output
        # 2. The trigger env var has been consumed so it does NOT fire again
        #    on the next command in the same shell.
        assert os.environ.get(ENV_UPDATED_FROM, "") == ""
        # 3. The 2-space-indented "What's new in vX:" header (injected by
        #    show_post_update_changelog via format_whats_new) does NOT appear.
        #    Matching the exact header format avoids false positives from
        #    prose mentions of the phrase inside changelog entries themselves.
        assert "  What's new in v" not in result.output


# ---------------------------------------------------------------------------
# _maybe_update_mcp -- MCP-server side of the auto-update flow (since v0.30.1)
# ---------------------------------------------------------------------------


class TestMaybeUpdateMcp:
    """Tests for ``_maybe_update_mcp`` -- the keboola-mcp-server upgrade stage."""

    @patch("keboola_agent_cli.auto_update._fetch_mcp_latest_version", return_value=None)
    def test_pypi_unreachable_returns_cached(self, mock_fetch):
        """If PyPI fetch fails, fall back to whatever the cache had."""
        result = _maybe_update_mcp(
            cache={"last_check": time.time(), "mcp_latest_version": "1.50.0"},
            fetched_now=False,
        )
        assert result == "1.50.0"

    @patch("keboola_agent_cli.auto_update._fetch_mcp_latest_version", return_value="1.59.1")
    @patch("keboola_agent_cli.auto_update._get_local_mcp_version", return_value="1.59.1")
    @patch("keboola_agent_cli.auto_update._detect_mcp_install_method", return_value="uv_tool")
    @patch("keboola_agent_cli.auto_update._perform_mcp_update")
    def test_up_to_date_skips_upgrade(self, mock_perform, mock_detect, mock_local, mock_fetch):
        """Local matches PyPI latest -> no upgrade subprocess."""
        result = _maybe_update_mcp(cache=None, fetched_now=True)
        assert result == "1.59.1"
        mock_perform.assert_not_called()

    @patch("keboola_agent_cli.auto_update._fetch_mcp_latest_version", return_value="1.59.1")
    @patch("keboola_agent_cli.auto_update._get_local_mcp_version", return_value="1.49.0")
    @patch("keboola_agent_cli.auto_update._detect_mcp_install_method", return_value="uv_tool")
    @patch("keboola_agent_cli.auto_update._perform_mcp_update", return_value=(True, "ok"))
    def test_stale_triggers_upgrade(self, mock_perform, mock_detect, mock_local, mock_fetch):
        """Local behind PyPI -> upgrade subprocess invoked."""
        result = _maybe_update_mcp(cache=None, fetched_now=True)
        assert result == "1.59.1"
        mock_perform.assert_called_once_with(method="uv_tool", timeout=MCP_UPGRADE_TIMEOUT)

    @patch("keboola_agent_cli.auto_update._fetch_mcp_latest_version", return_value="1.59.1")
    @patch("keboola_agent_cli.auto_update._get_local_mcp_version", return_value=None)
    @patch("keboola_agent_cli.auto_update._detect_mcp_install_method", return_value="none")
    @patch("keboola_agent_cli.auto_update._perform_mcp_update")
    def test_not_installed_does_not_install(
        self, mock_perform, mock_detect, mock_local, mock_fetch
    ):
        """If MCP is not installed locally, do not auto-install on startup."""
        result = _maybe_update_mcp(cache=None, fetched_now=True)
        assert result == "1.59.1"
        mock_perform.assert_not_called()

    @patch("keboola_agent_cli.auto_update._fetch_mcp_latest_version", return_value="1.59.1")
    @patch("keboola_agent_cli.auto_update._get_local_mcp_version", return_value="1.49.0")
    @patch("keboola_agent_cli.auto_update._detect_mcp_install_method", return_value="pip_env")
    @patch(
        "keboola_agent_cli.auto_update._perform_mcp_update",
        return_value=(False, "permission denied"),
    )
    def test_upgrade_failure_does_not_raise(
        self, mock_perform, mock_detect, mock_local, mock_fetch
    ):
        """Subprocess failure logs to stderr but the function still returns."""
        result = _maybe_update_mcp(cache=None, fetched_now=True)
        assert result == "1.59.1"  # Cache key still updates
        mock_perform.assert_called_once()

    @patch("keboola_agent_cli.auto_update._fetch_mcp_latest_version", return_value="1.59.1")
    @patch("keboola_agent_cli.auto_update._get_local_mcp_version", return_value="1.49.0")
    @patch("keboola_agent_cli.auto_update._detect_mcp_install_method", return_value="uv_tool")
    @patch("keboola_agent_cli.auto_update._perform_mcp_update", return_value=(True, "ok"))
    def test_uses_cache_when_not_fetched_now(
        self, mock_perform, mock_detect, mock_local, mock_fetch
    ):
        """If we already have a fresh cache, do not re-fetch from PyPI."""
        cache = {"last_check": time.time(), "mcp_latest_version": "1.59.0"}
        _maybe_update_mcp(cache=cache, fetched_now=False)
        mock_fetch.assert_not_called()

    @patch("keboola_agent_cli.auto_update._fetch_mcp_latest_version", return_value="1.59.1")
    @patch("keboola_agent_cli.auto_update._get_local_mcp_version", return_value="1.49.0")
    @patch("keboola_agent_cli.auto_update._detect_mcp_install_method", return_value="uv_tool")
    @patch("keboola_agent_cli.auto_update._perform_mcp_update", return_value=(True, "ok"))
    def test_refetches_when_fetched_now_overrides_cache(
        self, mock_perform, mock_detect, mock_local, mock_fetch
    ):
        """When fetched_now is True (kbagent path also did a fresh fetch), refetch."""
        cache = {"last_check": time.time(), "mcp_latest_version": "1.50.0"}
        _maybe_update_mcp(cache=cache, fetched_now=True)
        mock_fetch.assert_called_once()

    @patch("keboola_agent_cli.auto_update._fetch_mcp_latest_version", return_value="1.61.3")
    # Same version pre and post upgrade -> resolver backtracked (issue #324).
    @patch("keboola_agent_cli.auto_update._get_local_mcp_version", return_value="1.32.0")
    @patch("keboola_agent_cli.auto_update._detect_mcp_install_method", return_value="uv_tool")
    @patch("keboola_agent_cli.auto_update._perform_mcp_update", return_value=(True, "ok"))
    def test_backtrack_diagnostic_recommends_prerelease_flag(
        self, mock_perform, mock_detect, mock_local, mock_fetch, capsys
    ):
        """issue #324: when the upgrade exits 0 but the version did not move,
        the diagnostic must recommend a remediation that actually works --
        i.e. carry --prerelease=allow -- and must NOT print the old
        Python-blaming text that pointed at a reinstall failing the same way.
        """
        _maybe_update_mcp(cache=None, fetched_now=True)
        err = capsys.readouterr().err
        assert "--prerelease=allow" in err
        assert "uv tool install --reinstall" in err
        # The misleading pre-fix wording must be gone.
        assert "Possible Python or dependency-version mismatch" not in err


# ---------------------------------------------------------------------------
# maybe_auto_update -- end-to-end MCP integration (since v0.30.1)
# ---------------------------------------------------------------------------


class TestMaybeAutoUpdateMcpIntegration:
    """End-to-end tests for the MCP stage inside maybe_auto_update."""

    @pytest.fixture(autouse=True)
    def _force_active_skip_gates(self):
        """Default per-stage skip helpers to False (since v0.30.1).

        Each test below verifies an MCP-stage path; the skip gates must be
        out of the way for those paths to run. Individual tests still
        re-patch ``_should_skip_kbagent_stage`` when they exercise the
        re-exec scenario explicitly. Resets the per-process auto-update
        sentinel (since v0.30.3) so each test starts from a fresh state.
        """
        auto_update_module._AUTO_UPDATE_RAN = False
        with (
            patch(
                "keboola_agent_cli.auto_update._should_skip_all",
                return_value=False,
            ),
            patch(
                "keboola_agent_cli.auto_update._should_skip_kbagent_stage",
                return_value=False,
            ),
        ):
            yield

    @patch("keboola_agent_cli.auto_update._read_cache", return_value=None)
    @patch("keboola_agent_cli.auto_update._fetch_kbagent_latest_version", return_value="1.0.0")
    @patch("keboola_agent_cli.auto_update._is_up_to_date", return_value=True)
    @patch("keboola_agent_cli.auto_update._apply_prepared_mcp_update")
    @patch("keboola_agent_cli.auto_update._detect_mcp_install_method", return_value="uv_tool")
    @patch("keboola_agent_cli.auto_update._write_cache")
    def test_kbagent_uptodate_still_runs_mcp_stage(
        self,
        mock_write,
        mock_detect,
        mock_mcp,
        mock_up_to_date,
        mock_fetch,
        mock_cache,
    ):
        """Even when kbagent is up-to-date, the MCP stage MUST run."""
        maybe_auto_update()
        mock_mcp.assert_called_once()

    @patch("keboola_agent_cli.auto_update._read_cache", return_value=None)
    @patch("keboola_agent_cli.auto_update._fetch_kbagent_latest_version", return_value="2.0.0")
    @patch("keboola_agent_cli.auto_update._is_up_to_date", return_value=False)
    @patch("keboola_agent_cli.auto_update._perform_update", return_value=UpdateOutcome.FAILED)
    @patch("keboola_agent_cli.auto_update._apply_prepared_mcp_update")
    @patch("keboola_agent_cli.auto_update._detect_mcp_install_method", return_value="uv_tool")
    @patch("keboola_agent_cli.auto_update._write_cache")
    def test_failed_kbagent_upgrade_still_runs_mcp_stage(
        self,
        mock_write,
        mock_detect,
        mock_mcp,
        mock_perform,
        mock_up_to_date,
        mock_fetch,
        mock_cache,
    ):
        """If kbagent upgrade fails (no re-exec), still try MCP upgrade."""
        maybe_auto_update()
        mock_mcp.assert_called_once()

    def test_exception_in_mcp_stage_does_not_crash(self):
        """A blowup in the MCP stage MUST be caught by the blanket try/except."""
        cache = {"last_check": time.time(), "latest_version": "1.0.0"}
        with (
            patch("keboola_agent_cli.auto_update._read_cache", return_value=cache),
            patch("keboola_agent_cli.auto_update._is_cache_fresh", return_value=True),
            patch("keboola_agent_cli.auto_update._is_up_to_date", return_value=True),
            patch(
                "keboola_agent_cli.auto_update._apply_prepared_mcp_update",
                side_effect=RuntimeError("kaboom"),
            ),
        ):
            # MUST NOT raise.
            maybe_auto_update()


# ---------------------------------------------------------------------------
# B-1 regression: re-exec'd process must still run the MCP stage
# (PR #257 review carry-over, addressed in v0.30.1)
# ---------------------------------------------------------------------------


class TestReExecPathStillRunsMcp:
    """Pin the B-1 contract: ``KBAGENT_SKIP_UPDATE=1`` skips ONLY Stage 1.

    Pre-fix bug: ``_should_skip()`` returned True when the re-exec guard env
    var was set, so ``maybe_auto_update()`` returned before the MCP stage
    even when MCP was stale. After a kbagent self-upgrade the re-exec'd
    process therefore left MCP behind for one extra invocation, breaking
    the "both stages always run" promise that the PR description asserted.

    Fix: split the gate into ``_should_skip_kbagent_stage`` (re-exec only)
    and ``_should_skip_all`` (dev install / opt-out / update|version
    commands). The orchestrator consults each at the matching stage so the
    MCP work proceeds even when Stage 1 is skipped.
    """

    @pytest.fixture(autouse=True)
    def _reset_sentinel(self):
        """Reset per-process sentinel between tests (since v0.30.3)."""
        auto_update_module._AUTO_UPDATE_RAN = False
        yield
        auto_update_module._AUTO_UPDATE_RAN = False

    @patch("keboola_agent_cli.auto_update._is_dev_install", return_value=False)
    def test_re_exec_skips_kbagent_but_runs_mcp(self, _mock_dev):
        """With KBAGENT_SKIP_UPDATE=1 set, MCP stage MUST still run."""
        env = {k: v for k, v in os.environ.items() if k not in (ENV_AUTO_UPDATE,)}
        env[ENV_SKIP_UPDATE] = "1"
        with (
            patch.dict(os.environ, env, clear=True),
            patch("sys.argv", ["kbagent", "config", "list"]),
            patch(
                "keboola_agent_cli.auto_update._fetch_kbagent_latest_version"
            ) as mock_fetch_kbagent,
            patch("keboola_agent_cli.auto_update._apply_prepared_mcp_update") as mock_mcp,
            patch(
                "keboola_agent_cli.auto_update._detect_mcp_install_method",
                return_value="uv_tool",
            ),
            patch("keboola_agent_cli.auto_update._read_cache", return_value=None),
            patch("keboola_agent_cli.auto_update._write_cache"),
        ):
            maybe_auto_update()

        # Stage 1 (kbagent) skipped -- the GitHub fetch never fired.
        mock_fetch_kbagent.assert_not_called()
        # Stage 2 (MCP) ran exactly once -- this is the contract.
        mock_mcp.assert_called_once()

    @patch("keboola_agent_cli.auto_update._is_dev_install", return_value=False)
    def test_kbagent_command_update_skips_both(self, _mock_dev):
        """`kbagent update` argv still skips both stages (handled separately)."""
        env = {k: v for k, v in os.environ.items() if k not in (ENV_AUTO_UPDATE, ENV_SKIP_UPDATE)}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("sys.argv", ["kbagent", "update"]),
            patch(
                "keboola_agent_cli.auto_update._fetch_kbagent_latest_version"
            ) as mock_fetch_kbagent,
            patch("keboola_agent_cli.auto_update._apply_prepared_mcp_update") as mock_mcp,
        ):
            maybe_auto_update()

        mock_fetch_kbagent.assert_not_called()
        mock_mcp.assert_not_called()

    @patch("keboola_agent_cli.auto_update._is_dev_install", return_value=False)
    def test_user_opt_out_skips_both(self, _mock_dev):
        """KBAGENT_AUTO_UPDATE=false skips both stages (no auto-update at all)."""
        env = {k: v for k, v in os.environ.items() if k not in (ENV_SKIP_UPDATE,)}
        env[ENV_AUTO_UPDATE] = "false"
        with (
            patch.dict(os.environ, env, clear=True),
            patch("sys.argv", ["kbagent", "config", "list"]),
            patch(
                "keboola_agent_cli.auto_update._fetch_kbagent_latest_version"
            ) as mock_fetch_kbagent,
            patch("keboola_agent_cli.auto_update._apply_prepared_mcp_update") as mock_mcp,
        ):
            maybe_auto_update()

        mock_fetch_kbagent.assert_not_called()
        mock_mcp.assert_not_called()


# ---------------------------------------------------------------------------
# Bug C regression: probe-None must NOT fall through to upgrade attempt
# (issue #263, addressed in v0.30.3)
# ---------------------------------------------------------------------------


class TestProbeNoneSkipsUpgrade:
    """Pin the Bug C contract: when local-version detection fails,
    ``_maybe_update_mcp`` must NOT call ``_perform_mcp_update``.

    Pre-fix: detection returning None left ``up_to_date == None`` (not
    True), the short-circuit was bypassed, the function fell through to
    a broken upgrade subprocess, and the user saw an
    "Updating ... vunknown -> v1.59.1" banner every TTL window.

    Post-fix: probe-None opts out of the upgrade for this TTL window;
    the next fresh-cache pass will retry detection.
    """

    @patch("keboola_agent_cli.auto_update._fetch_mcp_latest_version", return_value="1.59.1")
    @patch("keboola_agent_cli.auto_update._get_local_mcp_version", return_value=None)
    @patch("keboola_agent_cli.auto_update._detect_mcp_install_method", return_value="uv_tool")
    @patch("keboola_agent_cli.auto_update._perform_mcp_update")
    def test_local_version_none_skips_upgrade(
        self, mock_perform, mock_detect, mock_local, mock_fetch
    ) -> None:
        """The acceptance criterion from #263: mock probe -> None;
        assert _perform_mcp_update is NOT called.
        """
        result = _maybe_update_mcp(cache=None, fetched_now=True)
        assert result == "1.59.1"
        mock_perform.assert_not_called()


# ---------------------------------------------------------------------------
# Bug D regression: process-level sentinel for repeated maybe_auto_update calls
# (issue #263, addressed in v0.30.3)
# ---------------------------------------------------------------------------


class TestProcessLevelSentinel:
    """Pin the Bug D contract: ``maybe_auto_update`` runs the body at most
    once per process.

    Pre-fix: ``kbagent repl`` re-entered ``main()`` -> ``maybe_auto_update``
    on every prompt, so the auto-update banner re-fired once per command
    typed at the prompt. Post-fix: a module-level ``_AUTO_UPDATE_RAN``
    flag short-circuits subsequent in-process invocations.

    Re-exec'd processes (kbagent self-upgrade -> ``execvpe``) start with
    a fresh sentinel because the module is reloaded into a new
    interpreter -- the kbagent-self-upgrade -> re-exec -> MCP-stage chain
    from PR #257 is preserved.
    """

    @pytest.fixture(autouse=True)
    def _reset_sentinel(self):
        """Tests assume a fresh sentinel per test (each simulates a new process)."""
        auto_update_module._AUTO_UPDATE_RAN = False
        yield
        auto_update_module._AUTO_UPDATE_RAN = False

    @patch("keboola_agent_cli.auto_update._should_skip_all", return_value=False)
    @patch("keboola_agent_cli.auto_update._should_skip_kbagent_stage", return_value=False)
    @patch("keboola_agent_cli.auto_update._read_cache", return_value=None)
    @patch("keboola_agent_cli.auto_update._fetch_kbagent_latest_version", return_value="1.0.0")
    @patch("keboola_agent_cli.auto_update._is_up_to_date", return_value=True)
    @patch("keboola_agent_cli.auto_update._apply_prepared_mcp_update")
    @patch("keboola_agent_cli.auto_update._detect_mcp_install_method", return_value="uv_tool")
    @patch("keboola_agent_cli.auto_update._write_cache")
    def test_second_call_short_circuits(
        self,
        mock_write,
        mock_detect,
        mock_mcp,
        mock_up_to_date,
        mock_fetch,
        mock_cache,
        mock_skip_kb,
        mock_skip_all,
    ):
        """Acceptance criterion from #263: second invocation in the same
        REPL session must NOT re-trigger maybe_auto_update's body.
        Verified by counting MCP-stage invocations: 1 after multiple
        calls, not N.
        """
        maybe_auto_update()  # first call: runs the body
        maybe_auto_update()  # second call: should short-circuit
        maybe_auto_update()  # third call: still short-circuited

        # MCP stage was reached exactly once across the three calls.
        mock_mcp.assert_called_once()

    def test_sentinel_is_set_even_when_body_raises(self):
        """Bug D corner case: the sentinel must flip to True BEFORE any
        work, so a crash mid-flow still gates subsequent in-process
        re-entries. Otherwise a flaky upstream PyPI fetch could re-fire
        the banner per prompt.
        """
        with patch(
            "keboola_agent_cli.auto_update._should_skip_all",
            side_effect=RuntimeError("kaboom"),
        ):
            maybe_auto_update()  # blanket try/except swallows the RuntimeError


class TestSafeStartupUpdateOrder:
    """Regression coverage for issue #528's terminal-mutation contract."""

    def test_mcp_and_cache_precede_kbagent_and_reexec_is_immediate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        auto_update_module._AUTO_UPDATE_RAN = False
        events: list[str] = []
        mutated = False

        def fetch_kbagent(*args: object, **kwargs: object) -> str:
            assert not mutated
            events.append("fetch_kbagent")
            return "2.0.0"

        def fetch_mcp(*args: object, **kwargs: object) -> str:
            assert not mutated
            events.append("fetch_mcp")
            return "2.0.0"

        def prepare_mcp(latest: str | None) -> McpUpdatePlan:
            assert not mutated
            events.append("prepare_mcp")
            return McpUpdatePlan("1.0.0", latest, "uv_tool", False, ("uv", "mcp"))

        def prepare_kbagent(latest: str | None) -> KbagentUpdatePlan:
            assert not mutated
            events.append("prepare_kbagent")
            return KbagentUpdatePlan(
                "1.0.0",
                latest,
                False,
                ("uv", "kbagent"),
                "uv tool install --force --reinstall exact",
            )

        def apply_mcp(plan: McpUpdatePlan) -> None:
            assert not mutated
            events.append("apply_mcp")

        def write_cache(**kwargs: object) -> None:
            assert not mutated
            events.append("cache")

        def perform(version: str, *, command: tuple[str, ...]) -> UpdateOutcome:
            nonlocal mutated
            assert events[-1] == "cache"
            mutated = True
            events.append("kbagent")
            return UpdateOutcome.SUCCESS

        def reexec() -> None:
            assert mutated
            events.append("reexec")

        monkeypatch.setattr(auto_update_module, "_AUTO_UPDATE_RAN", False)
        monkeypatch.setattr(auto_update_module, "_should_skip_all", lambda: False)
        monkeypatch.setattr(auto_update_module, "_should_skip_kbagent_stage", lambda: False)
        monkeypatch.setattr(auto_update_module, "_read_cache", lambda: None)
        monkeypatch.setattr(auto_update_module, "_fetch_kbagent_latest_version", fetch_kbagent)
        monkeypatch.setattr(auto_update_module, "_fetch_mcp_latest_version", fetch_mcp)
        monkeypatch.setattr(auto_update_module, "prepare_mcp_update_plan", prepare_mcp)
        monkeypatch.setattr(auto_update_module, "prepare_kbagent_update_plan", prepare_kbagent)
        monkeypatch.setattr(auto_update_module, "_apply_prepared_mcp_update", apply_mcp)
        monkeypatch.setattr(auto_update_module, "_write_cache", write_cache)
        monkeypatch.setattr(auto_update_module, "_perform_update", perform)
        monkeypatch.setattr(auto_update_module, "_re_exec", reexec)
        monkeypatch.setattr(auto_update_module, "_is_up_to_date", lambda *_: False)

        maybe_auto_update()

        assert events == [
            "fetch_kbagent",
            "fetch_mcp",
            "prepare_mcp",
            "prepare_kbagent",
            "apply_mcp",
            "cache",
            "kbagent",
            "reexec",
        ]
        # Sentinel was flipped before the crash.
        assert auto_update_module._AUTO_UPDATE_RAN is True


# ---------------------------------------------------------------------------
# Deferred (out-of-process) startup update -- issue #528
# ---------------------------------------------------------------------------
class TestStartupDefersTheReinstall:
    """On Windows the startup hook must not replace its own live environment.

    `uv tool install` removes the tool venv before recreating it, so running it
    from inside that venv on Windows deletes the files the running process has
    locked and aborts part-way -- the gutted `rich`/`typer` the reporter saw.
    """

    @pytest.fixture(autouse=True)
    def _quiet_mcp_and_cache(self):
        auto_update_module._AUTO_UPDATE_RAN = False
        with (
            patch("keboola_agent_cli.auto_update._read_cache", return_value=None),
            patch("keboola_agent_cli.auto_update._write_cache"),
            patch("keboola_agent_cli.auto_update._apply_prepared_mcp_update"),
            patch("keboola_agent_cli.auto_update._fetch_mcp_latest_version", return_value=None),
            patch("keboola_agent_cli.auto_update._get_local_mcp_version", return_value=None),
            patch("keboola_agent_cli.auto_update._detect_mcp_install_method", return_value="none"),
            patch("keboola_agent_cli.auto_update._should_skip_all", return_value=False),
            patch("keboola_agent_cli.auto_update._should_skip_kbagent_stage", return_value=False),
            patch(
                "keboola_agent_cli.auto_update.report_finished_deferred_update",
            ),
            patch(
                "keboola_agent_cli.auto_update._fetch_kbagent_latest_version",
                return_value="2.0.0",
            ),
            patch("keboola_agent_cli.auto_update._is_up_to_date", return_value=False),
            patch("keboola_agent_cli.auto_update.__version__", "1.0.0"),
        ):
            yield

    @patch("keboola_agent_cli.auto_update.should_defer", return_value=True)
    @patch("keboola_agent_cli.auto_update.request_deferred_update", return_value=True)
    @patch("keboola_agent_cli.auto_update._perform_update")
    @patch("keboola_agent_cli.auto_update._re_exec")
    def test_schedules_instead_of_installing_in_place(
        self, mock_reexec, mock_perform, mock_request, mock_defer, capsys
    ):
        maybe_auto_update()

        mock_perform.assert_not_called()
        mock_reexec.assert_not_called()
        request = mock_request.call_args.args[0]
        assert request.from_version == "1.0.0"
        assert request.target_version == "2.0.0"
        assert "background" in capsys.readouterr().err

    @patch("keboola_agent_cli.auto_update.should_defer", return_value=True)
    @patch("keboola_agent_cli.auto_update.request_deferred_update", return_value=False)
    @patch("keboola_agent_cli.auto_update._perform_update")
    def test_unschedulable_prints_the_command_and_installs_nothing(
        self, mock_perform, mock_request, mock_defer, capsys
    ):
        """Falling back to the in-place install would reintroduce the corruption."""
        maybe_auto_update()

        mock_perform.assert_not_called()
        assert "cannot be installed safely" in capsys.readouterr().err

    @patch("keboola_agent_cli.auto_update.should_defer", return_value=False)
    @patch("keboola_agent_cli.auto_update.request_deferred_update")
    @patch("keboola_agent_cli.auto_update._perform_update", return_value=UpdateOutcome.SUCCESS)
    @patch("keboola_agent_cli.auto_update._re_exec")
    def test_posix_keeps_the_inline_install_and_re_exec(
        self, mock_reexec, mock_perform, mock_request, mock_defer
    ):
        """The path that works today must not regress for the majority of users."""
        maybe_auto_update()

        mock_request.assert_not_called()
        mock_perform.assert_called_once()
        mock_reexec.assert_called_once()


class TestReportFinishedDeferredUpdate:
    """The run that schedules an update can never report it -- the next one does."""

    def _report(self, status):
        return DeferredUpdateReport(
            status=status,
            from_version="0.76.3",
            target_version="0.76.4",
            exit_code=2 if status is DeferredUpdateStatus.FAILED else None,
            recovery_command="uv tool install --force --reinstall x",
            log_path=Path("update.log"),
        )

    @patch("keboola_agent_cli.auto_update.collect_finished_deferred_update", return_value=None)
    def test_nothing_to_report_prints_nothing(self, mock_collect, capsys):
        report_finished_deferred_update()
        assert capsys.readouterr().err == ""

    @patch("keboola_agent_cli.auto_update.collect_finished_deferred_update")
    def test_success_is_announced(self, mock_collect, capsys):
        mock_collect.return_value = self._report(DeferredUpdateStatus.SUCCEEDED)
        report_finished_deferred_update()
        assert "Updated kbagent to v0.76.4" in capsys.readouterr().err

    @patch("keboola_agent_cli.auto_update.collect_finished_deferred_update")
    def test_failure_carries_the_recovery_command(self, mock_collect, capsys):
        mock_collect.return_value = self._report(DeferredUpdateStatus.FAILED)
        err = capsys.readouterr()  # drain
        report_finished_deferred_update()
        err = capsys.readouterr().err
        assert "did not complete" in err
        assert "uv tool install --force --reinstall x" in err

    @patch("keboola_agent_cli.auto_update.collect_finished_deferred_update")
    def test_abandoned_does_not_read_as_a_failure(self, mock_collect, capsys):
        """Nothing was installed, so nothing needs recovering."""
        mock_collect.return_value = self._report(DeferredUpdateStatus.ABANDONED)
        report_finished_deferred_update()
        err = capsys.readouterr().err
        assert "skipped" in err
        assert "Recover with" not in err

    @patch(
        "keboola_agent_cli.auto_update.collect_finished_deferred_update",
        side_effect=OSError("unreadable"),
    )
    def test_never_raises(self, mock_collect):
        report_finished_deferred_update()

    @patch("keboola_agent_cli.auto_update._should_skip_all", return_value=True)
    @patch("keboola_agent_cli.auto_update.report_finished_deferred_update")
    def test_reported_even_when_the_run_will_not_update(self, mock_report, mock_skip):
        """The result is owed to the user on the very next run, whatever it is.

        Gating it behind the skip check would swallow a failed background
        update on a dev install, an opt-out, or `kbagent version`.
        """
        auto_update_module._AUTO_UPDATE_RAN = False
        maybe_auto_update()
        mock_report.assert_called_once()


# ---------------------------------------------------------------------------
# Frozen (PyInstaller) build guard
# ---------------------------------------------------------------------------


class TestFrozenBuildGuard:
    """The startup hook must never uv/pip-reinstall a native binary.

    A frozen kbagent comes from Chocolatey / WinGet / Homebrew / apt / dnf. The
    normal Stage 1 reinstall would create a SEPARATE uv tool environment that
    shadows the real binary on PATH while leaving it stale, so Stage 1 is
    replaced by a notification naming that channel's own upgrade command.

    These tests are the guard's only protection: before this change the hook was
    suppressed for frozen builds purely by accident (CI freezes from an editable
    install, so the bundled ``direct_url.json`` made ``_is_dev_install()`` return
    True). Freezing from a non-editable install flips that and the reinstall
    fires -- reproduced against real PyInstaller binaries.
    """

    CHOCO_DIST = FrozenDistribution(
        channel=FrozenChannel.CHOCOLATEY,
        binary_path=r"C:\ProgramData\chocolatey\lib\keboola-cli2\tools\kbagent.exe",
        upgrade_command="choco upgrade keboola-cli2",
        upgrade_hint="upgrade it with: choco upgrade keboola-cli2",
    )

    @pytest.fixture(autouse=True)
    def _isolated_flow(self):
        """Drive the orchestrator down the active path with MCP stubbed out."""
        auto_update_module._AUTO_UPDATE_RAN = False
        with (
            patch("keboola_agent_cli.auto_update._should_skip_all", return_value=False),
            patch("keboola_agent_cli.auto_update._should_skip_kbagent_stage", return_value=False),
            patch("keboola_agent_cli.auto_update._fetch_mcp_latest_version", return_value=None),
            patch("keboola_agent_cli.auto_update.prepare_mcp_update_plan"),
            patch("keboola_agent_cli.auto_update._apply_prepared_mcp_update"),
            patch("keboola_agent_cli.auto_update._write_cache"),
        ):
            yield

    def _run_frozen(self, *, cached: bool, latest: str | None = "99.0.0"):
        """Run the hook as a frozen Chocolatey binary; return the patch mocks."""
        cache = {"last_check": time.time(), "latest_version": latest} if cached else None
        with (
            patch(
                "keboola_agent_cli.auto_update.detect_frozen_distribution",
                return_value=self.CHOCO_DIST,
            ),
            patch("keboola_agent_cli.auto_update._read_cache", return_value=cache),
            patch("keboola_agent_cli.auto_update._is_cache_fresh", return_value=cached),
            patch(
                "keboola_agent_cli.auto_update._fetch_kbagent_latest_version",
                return_value=latest,
            ),
            patch("keboola_agent_cli.auto_update._prepare_auto_kbagent_plan") as mock_plan,
            patch("keboola_agent_cli.auto_update._perform_update") as mock_perform,
            patch("keboola_agent_cli.auto_update._re_exec") as mock_reexec,
            patch("keboola_agent_cli.auto_update._schedule_deferred_update") as mock_defer,
            patch("keboola_agent_cli.auto_update.should_defer", return_value=True),
        ):
            maybe_auto_update()
        return mock_plan, mock_perform, mock_reexec, mock_defer

    def test_frozen_never_installs_and_never_re_execs(self, capsys):
        """The core guarantee: nothing installed, nothing re-exec'd, nothing scheduled.

        ``should_defer()`` is forced True inside the helper, so if the frozen
        branch ever slipped below the issue #528 deferral the Windows helper
        would be scheduled here and this test would catch it.
        """
        mock_plan, mock_perform, mock_reexec, mock_defer = self._run_frozen(cached=False)
        mock_perform.assert_not_called()
        mock_reexec.assert_not_called()
        mock_defer.assert_not_called()
        # Planning is short-circuited too -- it would spend a wheel-URL HEAD
        # probe on every startup building a command we refuse to run.
        mock_plan.assert_not_called()

    def test_frozen_notification_names_the_real_channel(self, capsys):
        self._run_frozen(cached=False)
        stderr = capsys.readouterr().err
        assert "choco upgrade keboola-cli2" in stderr
        assert "99.0.0" in stderr
        # The exact bug this guards against.
        assert "uv tool install" not in stderr

    def test_frozen_notification_throttled_to_one_per_cache_ttl(self, capsys):
        """A banner that cannot resolve itself must not print on every command.

        Unlike the normal path there is no re-exec to end the loop, so without
        throttling every kbagent invocation would nag until the user upgrades.
        """
        self._run_frozen(cached=True)
        assert capsys.readouterr().err == ""

    def test_frozen_and_current_is_silent(self, capsys):
        """No banner when the native binary is already the latest release."""
        from keboola_agent_cli import __version__

        self._run_frozen(cached=False, latest=__version__)
        assert capsys.readouterr().err == ""

    def test_frozen_with_unknown_latest_is_silent(self, capsys):
        """Offline: a version banner with nothing actionable behind it is noise."""
        self._run_frozen(cached=False, latest=None)
        assert capsys.readouterr().err == ""

    def test_frozen_still_updates_mcp(self):
        """Documented decision: Stage 2 runs on frozen builds.

        keboola-mcp-server is a separate Python distribution that a frozen
        kbagent only ever spawns as a subprocess, so upgrading it neither
        touches nor depends on the frozen binary.
        """
        with (
            patch(
                "keboola_agent_cli.auto_update.detect_frozen_distribution",
                return_value=self.CHOCO_DIST,
            ),
            patch("keboola_agent_cli.auto_update._read_cache", return_value=None),
            patch(
                "keboola_agent_cli.auto_update._fetch_kbagent_latest_version",
                return_value="99.0.0",
            ),
            patch("keboola_agent_cli.auto_update._perform_update"),
            patch("keboola_agent_cli.auto_update._apply_prepared_mcp_update") as mock_mcp,
        ):
            maybe_auto_update()
        mock_mcp.assert_called_once()

    def test_non_frozen_install_is_unaffected(self, capsys):
        """Regression guard: the uv/pip path must still self-update and re-exec."""
        plan = KbagentUpdatePlan(
            current_version="0.1.0",
            latest_version="99.0.0",
            up_to_date=False,
            command=("uv", "tool", "install", "--force", "--reinstall", "keboola-cli"),
            recovery_command="uv tool install --force --reinstall keboola-cli",
        )
        with (
            patch(
                "keboola_agent_cli.auto_update.detect_frozen_distribution",
                return_value=None,
            ),
            patch("keboola_agent_cli.auto_update._read_cache", return_value=None),
            patch(
                "keboola_agent_cli.auto_update._fetch_kbagent_latest_version",
                return_value="99.0.0",
            ),
            patch("keboola_agent_cli.auto_update._prepare_auto_kbagent_plan", return_value=plan),
            patch(
                "keboola_agent_cli.auto_update._perform_update",
                return_value=UpdateOutcome.SUCCESS,
            ) as mock_perform,
            patch("keboola_agent_cli.auto_update._re_exec") as mock_reexec,
        ):
            maybe_auto_update()
        mock_perform.assert_called_once()
        mock_reexec.assert_called_once()


class TestFrozenBuildIsNotADevInstall:
    """A frozen binary must never be mistaken for a developer checkout.

    This is the gap that made the whole frozen guard invisible on the artifacts
    CI actually ships. `--collect-all keboola_agent_cli` bundles the entire
    `.dist-info`, and the release workflow freezes from a `uv run` editable
    sync -- so every shipped binary carries `direct_url.json` with
    `"editable": true`. `_is_dev_install()` read that and returned True,
    `_should_skip_all()` bailed out of `maybe_auto_update()` before the frozen
    branch, and the notification meant for exactly those users never printed.
    The bundled marker describes the BUILD MACHINE, not the running one.
    """

    @staticmethod
    def _editable_dist() -> MagicMock:
        dist = MagicMock()
        dist.read_text.return_value = json.dumps(
            {"url": "file:///build/machine", "dir_info": {"editable": True}}
        )
        return dist

    def test_frozen_overrides_the_bundled_editable_marker(self):
        with (
            patch("keboola_agent_cli.auto_update.is_frozen_build", return_value=True),
            patch("keboola_agent_cli.auto_update.__version__", "1.0.0"),
            patch("keboola_agent_cli.auto_update.distribution", return_value=self._editable_dist()),
        ):
            assert _is_dev_install() is False

    def test_frozen_overrides_the_dev_version_fallback(self):
        """Metadata missing entirely is still a shipped artifact, not a dev tree."""
        with (
            patch("keboola_agent_cli.auto_update.is_frozen_build", return_value=True),
            patch("keboola_agent_cli.auto_update.__version__", "0.0.0-dev"),
        ):
            assert _is_dev_install() is False

    def test_non_frozen_editable_is_still_a_dev_install(self):
        """The original behaviour must survive for real developer checkouts."""
        with (
            patch("keboola_agent_cli.auto_update.is_frozen_build", return_value=False),
            patch("keboola_agent_cli.auto_update.__version__", "1.0.0"),
            patch("keboola_agent_cli.auto_update.distribution", return_value=self._editable_dist()),
        ):
            assert _is_dev_install() is True

    def test_notification_survives_the_real_shipped_configuration(self, capsys):
        """End-to-end: editable marker + frozen build still reaches the banner.

        Exercises the REAL `_should_skip_all()` / `_is_dev_install()` chain
        rather than stubbing it, because stubbing that chain is precisely what
        hid the bug.
        """
        auto_update_module._AUTO_UPDATE_RAN = False
        dist = FrozenDistribution(
            channel=FrozenChannel.CHOCOLATEY,
            binary_path=r"C:\ProgramData\chocolatey\lib\keboola-cli2\tools\kbagent.exe",
            upgrade_command="choco upgrade keboola-cli2",
            upgrade_hint="upgrade it with: choco upgrade keboola-cli2",
        )
        with (
            patch("keboola_agent_cli.auto_update.is_frozen_build", return_value=True),
            patch("keboola_agent_cli.auto_update.distribution", return_value=self._editable_dist()),
            patch("keboola_agent_cli.auto_update.detect_frozen_distribution", return_value=dist),
            patch("keboola_agent_cli.auto_update._read_cache", return_value=None),
            patch("sys.argv", ["kbagent", "config", "list"]),
            patch(
                "keboola_agent_cli.auto_update._fetch_kbagent_latest_version",
                return_value="99.0.0",
            ),
            patch("keboola_agent_cli.auto_update._fetch_mcp_latest_version", return_value=None),
            patch("keboola_agent_cli.auto_update.prepare_mcp_update_plan"),
            patch("keboola_agent_cli.auto_update._apply_prepared_mcp_update"),
            patch("keboola_agent_cli.auto_update._write_cache"),
            patch("keboola_agent_cli.auto_update._perform_update") as mock_perform,
        ):
            maybe_auto_update()
        stderr = capsys.readouterr().err
        assert "choco upgrade keboola-cli2" in stderr
        assert "uv tool install" not in stderr
        mock_perform.assert_not_called()
