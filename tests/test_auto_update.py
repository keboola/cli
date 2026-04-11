"""Tests for the auto-update module."""

import json
import os
import time
from unittest.mock import MagicMock, patch

from keboola_agent_cli.auto_update import (
    _get_cache_path,
    _is_cache_fresh,
    _is_dev_install,
    _perform_update,
    _re_exec,
    _read_cache,
    _should_skip,
    _write_cache,
    maybe_auto_update,
)
from keboola_agent_cli.constants import ENV_AUTO_UPDATE, ENV_SKIP_UPDATE


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

    @patch("keboola_agent_cli.auto_update._should_skip", return_value=True)
    def test_skip_conditions_respected(self, mock_skip):
        # Should return immediately without doing anything
        maybe_auto_update()
        mock_skip.assert_called_once()

    @patch("keboola_agent_cli.auto_update._should_skip", return_value=False)
    @patch("keboola_agent_cli.auto_update._read_cache")
    @patch("keboola_agent_cli.auto_update._is_cache_fresh", return_value=True)
    @patch("keboola_agent_cli.auto_update._is_up_to_date", return_value=True)
    def test_cache_fresh_no_fetch(self, mock_up_to_date, mock_fresh, mock_cache, mock_skip):
        mock_cache.return_value = {"last_check": time.time(), "latest_version": "1.0.0"}
        with patch("keboola_agent_cli.auto_update._fetch_kbagent_latest_version") as mock_fetch:
            maybe_auto_update()
            mock_fetch.assert_not_called()

    @patch("keboola_agent_cli.auto_update._should_skip", return_value=False)
    @patch("keboola_agent_cli.auto_update._read_cache", return_value=None)
    @patch("keboola_agent_cli.auto_update._fetch_kbagent_latest_version", return_value="2.0.0")
    @patch("keboola_agent_cli.auto_update._write_cache")
    @patch("keboola_agent_cli.auto_update._is_up_to_date", return_value=True)
    def test_cache_stale_fetches(
        self, mock_up_to_date, mock_write, mock_fetch, mock_cache, mock_skip
    ):
        maybe_auto_update()
        mock_fetch.assert_called_once()
        mock_write.assert_called_once_with("2.0.0")

    @patch("keboola_agent_cli.auto_update._should_skip", return_value=False)
    @patch("keboola_agent_cli.auto_update._read_cache", return_value=None)
    @patch("keboola_agent_cli.auto_update._fetch_kbagent_latest_version", return_value="1.0.0")
    @patch("keboola_agent_cli.auto_update._write_cache")
    @patch("keboola_agent_cli.auto_update._is_up_to_date", return_value=True)
    @patch("keboola_agent_cli.auto_update._perform_update")
    def test_up_to_date_no_update(
        self, mock_update, mock_up_to_date, mock_write, mock_fetch, mock_cache, mock_skip
    ):
        maybe_auto_update()
        mock_update.assert_not_called()

    @patch("keboola_agent_cli.auto_update._should_skip", return_value=False)
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
        mock_skip,
    ):
        maybe_auto_update()
        mock_update.assert_called_once_with("2.0.0")
        mock_reexec.assert_called_once()

    @patch("keboola_agent_cli.auto_update._should_skip", return_value=False)
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
        mock_skip,
    ):
        """If _perform_update returns False, re-exec should NOT be called."""
        maybe_auto_update()
        mock_update.assert_called_once()
        mock_reexec.assert_not_called()

    @patch("keboola_agent_cli.auto_update._should_skip", return_value=False)
    @patch("keboola_agent_cli.auto_update._read_cache", return_value=None)
    @patch("keboola_agent_cli.auto_update._fetch_kbagent_latest_version", return_value=None)
    @patch("keboola_agent_cli.auto_update._perform_update")
    def test_fetch_failure_continues(self, mock_update, mock_fetch, mock_cache, mock_skip):
        """If fetch returns None, should continue without updating."""
        maybe_auto_update()
        mock_update.assert_not_called()

    @patch(
        "keboola_agent_cli.auto_update._should_skip",
        side_effect=RuntimeError("kaboom"),
    )
    def test_exception_never_crashes(self, mock_skip):
        """Any exception inside maybe_auto_update must be swallowed."""
        # Should NOT raise
        maybe_auto_update()

    @patch("keboola_agent_cli.auto_update._should_skip", return_value=False)
    @patch("keboola_agent_cli.auto_update._read_cache")
    @patch("keboola_agent_cli.auto_update._is_cache_fresh", return_value=True)
    @patch("keboola_agent_cli.auto_update._is_up_to_date", return_value=None)
    @patch("keboola_agent_cli.auto_update._perform_update")
    def test_version_comparison_none_no_update(
        self, mock_update, mock_up_to_date, mock_fresh, mock_cache, mock_skip
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
