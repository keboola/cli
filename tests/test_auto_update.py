"""Tests for the auto-update module."""

import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from keboola_agent_cli.auto_update import (
    _get_cache_path,
    _is_cache_fresh,
    _is_dev_install,
    _maybe_update_mcp,
    _perform_update,
    _re_exec,
    _read_cache,
    _should_skip,
    _write_cache,
    maybe_auto_update,
)
from keboola_agent_cli.constants import ENV_AUTO_UPDATE, ENV_SKIP_UPDATE, MCP_UPGRADE_TIMEOUT


# ---------------------------------------------------------------------------
# _should_skip
# ---------------------------------------------------------------------------
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
        mock_dist.read_text.return_value = json.dumps(
            {"url": "https://github.com/padak/keboola_agent_cli"}
        )
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
class TestPerformUpdate:
    """Tests for the _perform_update() function."""

    @patch("shutil.which")
    @patch("subprocess.run")
    def test_update_with_uv_success(self, mock_run, mock_which):
        mock_which.return_value = "/usr/local/bin/uv"
        mock_run.return_value = MagicMock(returncode=0)
        assert _perform_update("2.0.0") is True
        # Verify uv was called
        call_args = mock_run.call_args
        assert "uv" in call_args[0][0][0]

    @patch("shutil.which")
    @patch("subprocess.run")
    def test_update_with_uv_failure(self, mock_run, mock_which):
        mock_which.return_value = "/usr/local/bin/uv"
        mock_run.return_value = MagicMock(returncode=1, stderr="error")
        assert _perform_update("2.0.0") is False

    @patch("shutil.which")
    @patch("subprocess.run")
    def test_update_pip_fallback(self, mock_run, mock_which):
        # uv not found, pip found
        mock_which.side_effect = lambda cmd: (
            None if cmd == "uv" else "/usr/bin/pip" if cmd == "pip" else None
        )
        mock_run.return_value = MagicMock(returncode=0)
        assert _perform_update("2.0.0") is True
        call_args = mock_run.call_args
        assert "pip" in call_args[0][0][0]

    @patch("shutil.which")
    def test_update_no_tools(self, mock_which):
        mock_which.return_value = None
        assert _perform_update("2.0.0") is False

    @patch("shutil.which")
    @patch("subprocess.run")
    def test_update_timeout(self, mock_run, mock_which):
        import subprocess as sp

        mock_which.return_value = "/usr/local/bin/uv"
        mock_run.side_effect = sp.TimeoutExpired(cmd="uv", timeout=120)
        assert _perform_update("2.0.0") is False


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
        alias still drive the orchestrator down the active path.
        """
        with (
            patch(
                "keboola_agent_cli.auto_update._maybe_update_mcp",
                return_value=None,
            ),
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
    def test_cache_fresh_no_fetch(self, mock_up_to_date, mock_fresh, mock_cache):
        mock_cache.return_value = {"last_check": time.time(), "latest_version": "1.0.0"}
        with patch("keboola_agent_cli.auto_update._fetch_kbagent_latest_version") as mock_fetch:
            maybe_auto_update()
            mock_fetch.assert_not_called()

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
    @patch("keboola_agent_cli.auto_update._perform_update", return_value=True)
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
        mock_update.assert_called_once_with("2.0.0")
        mock_reexec.assert_called_once()

    @patch("keboola_agent_cli.auto_update._read_cache", return_value=None)
    @patch("keboola_agent_cli.auto_update._fetch_kbagent_latest_version", return_value="2.0.0")
    @patch("keboola_agent_cli.auto_update._write_cache")
    @patch("keboola_agent_cli.auto_update._is_up_to_date", return_value=False)
    @patch("keboola_agent_cli.auto_update._perform_update", return_value=False)
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
        """If _perform_update returns False, re-exec should NOT be called."""
        maybe_auto_update()
        mock_update.assert_called_once()
        mock_reexec.assert_not_called()

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
        re-exec scenario explicitly.
        """
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
    @patch("keboola_agent_cli.auto_update._maybe_update_mcp")
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
    @patch("keboola_agent_cli.auto_update._perform_update", return_value=False)
    @patch("keboola_agent_cli.auto_update._maybe_update_mcp")
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
                "keboola_agent_cli.auto_update._maybe_update_mcp",
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
            patch("keboola_agent_cli.auto_update._maybe_update_mcp") as mock_mcp,
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
            patch("keboola_agent_cli.auto_update._maybe_update_mcp") as mock_mcp,
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
            patch("keboola_agent_cli.auto_update._maybe_update_mcp") as mock_mcp,
        ):
            maybe_auto_update()

        mock_fetch_kbagent.assert_not_called()
        mock_mcp.assert_not_called()
