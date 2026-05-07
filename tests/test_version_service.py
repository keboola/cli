"""Tests for VersionService - version detection and update checks."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from keboola_agent_cli.constants import MCP_UPGRADE_TIMEOUT
from keboola_agent_cli.services.version_service import (
    MCP_BINARY_NAME,
    MCP_PACKAGE_NAME,
    VersionService,
    _detect_mcp_install_method,
    _fetch_mcp_latest_version,
    _get_local_mcp_version,
    _is_up_to_date,
    _is_uvx_available,
    _perform_mcp_update,
    _uv_tool_list_get_mcp_version,
    _uv_tool_list_has_mcp,
)


class TestIsUvxAvailable:
    """Tests for _is_uvx_available()."""

    @patch("keboola_agent_cli.services.version_service.shutil.which")
    def test_uvx_found(self, mock_which: MagicMock) -> None:
        mock_which.return_value = "/usr/local/bin/uvx"
        assert _is_uvx_available() is True

    @patch("keboola_agent_cli.services.version_service.shutil.which")
    def test_uvx_not_found(self, mock_which: MagicMock) -> None:
        mock_which.return_value = None
        assert _is_uvx_available() is False


class TestFetchMcpLatestVersion:
    """Tests for _fetch_mcp_latest_version()."""

    @patch("keboola_agent_cli.services.version_service.httpx.get")
    def test_success(self, mock_get: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {"info": {"version": "1.46.0"}}
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        assert _fetch_mcp_latest_version() == "1.46.0"

    @patch("keboola_agent_cli.services.version_service.httpx.get")
    def test_http_error(self, mock_get: MagicMock) -> None:
        import httpx

        mock_get.side_effect = httpx.HTTPError("connection failed")
        assert _fetch_mcp_latest_version() is None


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

    @pytest.fixture(autouse=True)
    def _no_real_mcp_probe(self):
        """Stub out the MCP detection helpers to avoid real subprocess calls."""
        with (
            patch(
                "keboola_agent_cli.services.version_service._get_local_mcp_version",
                return_value="1.46.0",
            ),
            patch(
                "keboola_agent_cli.services.version_service._detect_mcp_install_method",
                return_value="uv_tool",
            ),
        ):
            yield

    @patch("keboola_agent_cli.services.version_service._fetch_mcp_latest_version")
    @patch("keboola_agent_cli.services.version_service._is_uvx_available")
    def test_mcp_auto_updates(
        self,
        mock_uvx: MagicMock,
        mock_mcp_latest: MagicMock,
    ) -> None:
        mock_uvx.return_value = True
        mock_mcp_latest.return_value = "1.46.0"

        svc = VersionService()
        result = svc.get_versions()

        assert result["kbagent"]["version"] is not None
        deps = result["dependencies"]
        assert len(deps) == 1

        mcp_dep = deps[0]
        assert mcp_dep["name"] == "keboola-mcp-server"
        assert mcp_dep["auto_updates"] is True
        assert mcp_dep["uvx_available"] is True
        assert mcp_dep["latest_version"] == "1.46.0"
        # New fields (since v0.30.1)
        assert mcp_dep["version"] == "1.46.0"
        assert mcp_dep["up_to_date"] is True
        assert mcp_dep["install_method"] == "uv_tool"
        assert "uv tool upgrade" in mcp_dep["upgrade_command"]

    @patch("keboola_agent_cli.services.version_service._fetch_mcp_latest_version")
    @patch("keboola_agent_cli.services.version_service._is_uvx_available")
    def test_uvx_not_available(
        self,
        mock_uvx: MagicMock,
        mock_mcp_latest: MagicMock,
    ) -> None:
        mock_uvx.return_value = False
        mock_mcp_latest.return_value = "1.46.0"

        svc = VersionService()
        result = svc.get_versions()

        mcp_dep = result["dependencies"][0]
        assert mcp_dep["uvx_available"] is False

    @patch("keboola_agent_cli.services.version_service._fetch_mcp_latest_version")
    @patch("keboola_agent_cli.services.version_service._is_uvx_available")
    def test_remote_check_fails(
        self,
        mock_uvx: MagicMock,
        mock_mcp_latest: MagicMock,
    ) -> None:
        mock_uvx.return_value = True
        mock_mcp_latest.return_value = None

        svc = VersionService()
        result = svc.get_versions()

        mcp_dep = result["dependencies"][0]
        assert mcp_dep["latest_version"] is None
        assert mcp_dep["up_to_date"] is None  # cannot compare without latest


# ---------------------------------------------------------------------------
# _get_local_mcp_version (since v0.30.1)
# ---------------------------------------------------------------------------


class TestGetLocalMcpVersion:
    """Tests for the local-MCP-version detection helper.

    Since v0.30.2 the resolution order is ``uv tool list`` -> importlib.metadata
    -> ``keboola_mcp_server --version`` (best-effort future fallback). The
    upstream binary today does NOT honour ``--version`` and instead prints
    its usage block; the path therefore yields no match by design.
    """

    @patch("keboola_agent_cli.services.version_service.shutil.which")
    @patch("keboola_agent_cli.services.version_service.subprocess.run")
    def test_uv_tool_list_returns_version(self, mock_run: MagicMock, mock_which: MagicMock) -> None:
        """Preferred path: read version straight from `uv tool list` output.

        This is the v0.30.2 fix path -- the previous --version probe
        returned None because the upstream binary has no --version flag.
        """
        mock_which.side_effect = lambda c: "/usr/local/bin/uv" if c == "uv" else None
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "keboola-agent-cli v0.30.1\n"
                "- kbagent\n"
                f"{MCP_PACKAGE_NAME} v1.59.1\n"
                f"- {MCP_BINARY_NAME}\n"
            ),
            stderr="",
        )
        assert _get_local_mcp_version() == "1.59.1"

    @patch("keboola_agent_cli.services.version_service.shutil.which", return_value=None)
    def test_no_uv_no_binary_no_metadata_returns_none(self, mock_which: MagicMock) -> None:
        """All three resolution paths fail -> None."""
        with patch(
            "importlib.metadata.version",
            side_effect=__import__("importlib.metadata").metadata.PackageNotFoundError(
                MCP_PACKAGE_NAME
            ),
        ):
            assert _get_local_mcp_version() is None

    @patch("keboola_agent_cli.services.version_service.shutil.which")
    @patch(
        "keboola_agent_cli.services.version_service.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="x", timeout=5),
    )
    def test_uv_tool_list_timeout_falls_through(
        self, mock_run: MagicMock, mock_which: MagicMock
    ) -> None:
        """`uv tool list` timeout does not crash; falls through to next path."""
        mock_which.return_value = "/usr/local/bin/uv"
        with patch(
            "importlib.metadata.version",
            side_effect=__import__("importlib.metadata").metadata.PackageNotFoundError(
                MCP_PACKAGE_NAME
            ),
        ):
            assert _get_local_mcp_version() is None

    @patch("keboola_agent_cli.services.version_service.shutil.which")
    @patch("keboola_agent_cli.services.version_service.subprocess.run")
    def test_real_world_no_version_flag_returns_none(
        self, mock_run: MagicMock, mock_which: MagicMock
    ) -> None:
        """Regression: keboola_mcp_server --version prints USAGE help, not a version.

        Pre-v0.30.2 the --version path picked the python3 minor (e.g. 3.12)
        out of the usage line and returned the wrong value, OR returned None
        when the regex required X.Y.Z. Lock the contract: the binary's
        usage-help output must NOT yield a spurious version match.
        """
        # Use the actual upstream output verbatim (verified locally on v1.59.1).
        usage_output = (
            "usage: python -m keboola-mcp-server [-h]\n"
            "                                    [--transport {stdio,streamable-http,http-compat}]\n"
            "                                    [--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}]\n"
        )
        # uv unavailable so we land on the binary path.
        mock_which.side_effect = lambda c: (
            f"/usr/local/bin/{MCP_BINARY_NAME}" if c == MCP_BINARY_NAME else None
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=usage_output, stderr="")

        with patch(
            "importlib.metadata.version",
            side_effect=__import__("importlib.metadata").metadata.PackageNotFoundError(
                MCP_PACKAGE_NAME
            ),
        ):
            # The 3.12 in "python3.12" inside a usage line must NOT be returned.
            # Since v0.30.2 the cleaned-output filter strips usage: lines.
            assert _get_local_mcp_version() is None

    @patch("keboola_agent_cli.services.version_service.shutil.which")
    @patch("keboola_agent_cli.services.version_service.subprocess.run")
    def test_future_binary_with_version_flag(
        self, mock_run: MagicMock, mock_which: MagicMock
    ) -> None:
        """Forward-compat: when a future MCP server adds --version, we read it.

        Locks the fallback path so we do not silently lose this capability
        once the upstream binary catches up.
        """
        mock_which.side_effect = lambda c: (
            f"/usr/local/bin/{MCP_BINARY_NAME}" if c == MCP_BINARY_NAME else None
        )
        mock_run.return_value = MagicMock(
            returncode=0, stdout="keboola_mcp_server 2.0.0\n", stderr=""
        )
        with patch(
            "importlib.metadata.version",
            side_effect=__import__("importlib.metadata").metadata.PackageNotFoundError(
                MCP_PACKAGE_NAME
            ),
        ):
            assert _get_local_mcp_version() == "2.0.0"


# ---------------------------------------------------------------------------
# _uv_tool_list_get_mcp_version -- version extractor (since v0.30.2)
# ---------------------------------------------------------------------------


class TestUvToolListGetMcpVersion:
    """Tests for the version extractor pulled out of `uv tool list` output."""

    def test_exact_match_returns_version(self) -> None:
        stdout = f"{MCP_PACKAGE_NAME} v1.59.1\n- {MCP_BINARY_NAME}\n"
        assert _uv_tool_list_get_mcp_version(stdout) == "1.59.1"

    def test_real_world_uv_tool_list_output(self) -> None:
        """Regression: the exact format `uv tool list` emits (multi-tool case)."""
        stdout = (
            "agnes-the-ai-analyst v2.1.0\n"
            "- da\n"
            "juncture v0.41.3\n"
            "- juncture\n"
            "keboola-agent-cli v0.30.1\n"
            "- kbagent\n"
            f"{MCP_PACKAGE_NAME} v1.59.1\n"
            f"- {MCP_BINARY_NAME}\n"
        )
        assert _uv_tool_list_get_mcp_version(stdout) == "1.59.1"

    def test_not_listed_returns_none(self) -> None:
        stdout = "keboola-agent-cli v0.30.1\n- kbagent\n"
        assert _uv_tool_list_get_mcp_version(stdout) is None

    def test_similar_named_package_rejected(self) -> None:
        """`keboola-mcp-server-foo` is NOT the package; reject."""
        stdout = "keboola-mcp-server-foo v0.1.0\n- foo-bin\n"
        assert _uv_tool_list_get_mcp_version(stdout) is None

    def test_indented_binary_line_ignored(self) -> None:
        """Indented `- keboola-mcp-server` under another tool must NOT match."""
        stdout = "some-other-tool v1.0.0\n    - keboola-mcp-server-helper\n"
        assert _uv_tool_list_get_mcp_version(stdout) is None

    def test_malformed_version_token_rejected(self) -> None:
        """Second token must look like vX.Y.Z; otherwise None."""
        stdout = f"{MCP_PACKAGE_NAME} not-a-version\n- {MCP_BINARY_NAME}\n"
        assert _uv_tool_list_get_mcp_version(stdout) is None

    def test_strips_leading_v(self) -> None:
        stdout = f"{MCP_PACKAGE_NAME} v2.0.0\n- {MCP_BINARY_NAME}\n"
        assert _uv_tool_list_get_mcp_version(stdout) == "2.0.0"

    def test_pre_release_version(self) -> None:
        """Tolerate suffixes like `v1.59.1.dev0` (uv may include pre-release tags)."""
        stdout = f"{MCP_PACKAGE_NAME} v1.59.1.dev0\n- {MCP_BINARY_NAME}\n"
        # Returns the X.Y.Z prefix; suffix is ignored by the regex anchor.
        assert _uv_tool_list_get_mcp_version(stdout) == "1.59.1.dev0"

    def test_empty_input(self) -> None:
        assert _uv_tool_list_get_mcp_version("") is None


# ---------------------------------------------------------------------------
# _uv_tool_list_has_mcp -- robust parser (B-2 fix, since v0.30.1)
# ---------------------------------------------------------------------------


class TestUvToolListHasMcp:
    """Tests for the robust ``uv tool list`` parser.

    Pre-fix: ``MCP_PACKAGE_NAME in stdout`` would substring-match
    similarly-named tools, indented binary listings, and even hint /
    warning text. The new parser does per-line, exact first-token
    equality.
    """

    def test_exact_package_match(self) -> None:
        stdout = f"{MCP_PACKAGE_NAME} v1.59.1\n- {MCP_BINARY_NAME}\n"
        assert _uv_tool_list_has_mcp(stdout) is True

    def test_no_match_when_only_indented_binary_line(self) -> None:
        """Pre-fix `keboola-mcp-server` substring would also live in the
        binary line `- keboola_mcp_server` (different tool's listing).
        After the fix, indented lines are skipped.
        """
        stdout = "some-other-tool v1.0.0\n    - keboola_mcp_server_helper\n"
        assert _uv_tool_list_has_mcp(stdout) is False

    def test_no_match_for_similar_named_package(self) -> None:
        """A hypothetical `keboola-mcp-server-foo` would substring-match
        but is NOT the same tool. Exact first-token equality rejects it.
        """
        stdout = "keboola-mcp-server-foo v0.1.0\n- foo-bin\n"
        assert _uv_tool_list_has_mcp(stdout) is False

    def test_match_with_other_tools_listed(self) -> None:
        """The package is found alongside other unrelated tools."""
        stdout = (
            "keboola-agent-cli v0.30.0\n"
            "- kbagent\n"
            f"{MCP_PACKAGE_NAME} v1.59.1\n"
            f"- {MCP_BINARY_NAME}\n"
            "ruff v0.1.0\n"
            "- ruff\n"
        )
        assert _uv_tool_list_has_mcp(stdout) is True

    def test_empty_input(self) -> None:
        assert _uv_tool_list_has_mcp("") is False

    def test_blank_lines_only(self) -> None:
        assert _uv_tool_list_has_mcp("\n\n   \n\t\n") is False

    def test_trailing_whitespace_does_not_break_match(self) -> None:
        stdout = f"{MCP_PACKAGE_NAME}   v1.59.1   \n"
        assert _uv_tool_list_has_mcp(stdout) is True


# ---------------------------------------------------------------------------
# _detect_mcp_install_method (since v0.30.1)
# ---------------------------------------------------------------------------


class TestDetectMcpInstallMethod:
    """Tests for the MCP-install-method detector."""

    @patch("keboola_agent_cli.services.version_service.shutil.which")
    @patch("keboola_agent_cli.services.version_service.subprocess.run")
    def test_uv_tool(self, mock_run: MagicMock, mock_which: MagicMock) -> None:
        mock_which.side_effect = lambda c: {
            MCP_BINARY_NAME: f"/home/user/.local/bin/{MCP_BINARY_NAME}",
            "uv": "/usr/local/bin/uv",
        }.get(c)
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=f"{MCP_PACKAGE_NAME} v1.59.1\n",
            stderr="",
        )
        assert _detect_mcp_install_method() == "uv_tool"

    @patch("keboola_agent_cli.services.version_service.shutil.which")
    @patch("keboola_agent_cli.services.version_service.subprocess.run")
    def test_pip_env_when_binary_present_but_not_in_uv_tool(
        self, mock_run: MagicMock, mock_which: MagicMock
    ) -> None:
        mock_which.side_effect = lambda c: {
            MCP_BINARY_NAME: f"/usr/local/bin/{MCP_BINARY_NAME}",
            "uv": "/usr/local/bin/uv",
        }.get(c)
        mock_run.return_value = MagicMock(returncode=0, stdout="other-tool v1.0.0\n", stderr="")
        assert _detect_mcp_install_method() == "pip_env"

    @patch("keboola_agent_cli.services.version_service.shutil.which", return_value=None)
    def test_uvx_fallback(self, mock_which: MagicMock) -> None:
        # Two passes through which: fail for binary + uv, succeed for uvx.
        mock_which.side_effect = lambda c: "/usr/local/bin/uvx" if c == "uvx" else None
        with patch(
            "importlib.metadata.distribution",
            side_effect=__import__("importlib.metadata").metadata.PackageNotFoundError(
                MCP_PACKAGE_NAME
            ),
        ):
            assert _detect_mcp_install_method() == "uvx"

    @patch("keboola_agent_cli.services.version_service.shutil.which", return_value=None)
    def test_none_when_nothing_available(self, mock_which: MagicMock) -> None:
        with patch(
            "importlib.metadata.distribution",
            side_effect=__import__("importlib.metadata").metadata.PackageNotFoundError(
                MCP_PACKAGE_NAME
            ),
        ):
            assert _detect_mcp_install_method() == "none"


# ---------------------------------------------------------------------------
# _perform_mcp_update (since v0.30.1)
# ---------------------------------------------------------------------------


class TestPerformMcpUpdate:
    """Tests for ``_perform_mcp_update``."""

    @patch("keboola_agent_cli.services.version_service.shutil.which")
    @patch("keboola_agent_cli.services.version_service.subprocess.run")
    def test_uv_tool_success(self, mock_run: MagicMock, mock_which: MagicMock) -> None:
        mock_which.return_value = "/usr/local/bin/uv"
        mock_run.return_value = MagicMock(returncode=0, stdout="upgraded", stderr="")
        ok, info = _perform_mcp_update(method="uv_tool")
        assert ok is True
        assert "upgraded" in info
        # Verify the command shape we are about to run.
        cmd = mock_run.call_args.args[0]
        assert "tool" in cmd and "upgrade" in cmd and MCP_PACKAGE_NAME in cmd

    @patch("keboola_agent_cli.services.version_service.shutil.which")
    @patch("keboola_agent_cli.services.version_service.subprocess.run")
    def test_pip_env_success(self, mock_run: MagicMock, mock_which: MagicMock) -> None:
        mock_which.return_value = "/usr/local/bin/pip"
        mock_run.return_value = MagicMock(returncode=0, stdout="upgraded via pip", stderr="")
        ok, _info = _perform_mcp_update(method="pip_env")
        assert ok is True
        cmd = mock_run.call_args.args[0]
        assert "install" in cmd and "--upgrade" in cmd

    @patch("keboola_agent_cli.services.version_service.shutil.which")
    @patch("keboola_agent_cli.services.version_service.subprocess.run")
    def test_uvx_promotes_to_uv_tool_install(
        self, mock_run: MagicMock, mock_which: MagicMock
    ) -> None:
        """Bug B fix from issue #263: uvx-cache install is promoted to a
        persistent ``uv tool install --upgrade``.

        Pre-fix: command was ``uvx --refresh --from <pkg> <bin> --version``,
        which failed because the upstream MCP binary does not honour
        ``--version``. The cache refresh succeeded but the trailing
        --version probe exited non-zero, so the upgrade banner reported
        failure even though the refresh worked.

        Post-fix: command is ``uv tool install --upgrade <pkg>``, which
        equivalent-refreshes AND moves the binary to PATH so subsequent
        runs use the faster ``uv_tool`` detection path.
        """
        mock_which.return_value = "/usr/local/bin/uv"
        mock_run.return_value = MagicMock(returncode=0, stdout="installed", stderr="")
        ok, _info = _perform_mcp_update(method="uvx")
        assert ok is True
        cmd = mock_run.call_args.args[0]
        assert "tool" in cmd and "install" in cmd and "--upgrade" in cmd
        assert MCP_PACKAGE_NAME in cmd
        # The broken --version arg must be GONE from the uvx upgrade path.
        assert "--version" not in cmd

    @patch("keboola_agent_cli.services.version_service.shutil.which", return_value=None)
    def test_uvx_promotion_requires_uv(self, mock_which: MagicMock) -> None:
        """If the uvx promotion needs `uv` and `uv` is missing, fail clearly."""
        ok, info = _perform_mcp_update(method="uvx")
        assert ok is False
        assert "uv not found" in info

    def test_none_returns_false(self) -> None:
        ok, info = _perform_mcp_update(method="none")
        assert ok is False
        assert "not installed" in info

    @patch("keboola_agent_cli.services.version_service.shutil.which")
    @patch(
        "keboola_agent_cli.services.version_service.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="x", timeout=MCP_UPGRADE_TIMEOUT),
    )
    def test_timeout(self, mock_run: MagicMock, mock_which: MagicMock) -> None:
        mock_which.return_value = "/usr/local/bin/uv"
        ok, info = _perform_mcp_update(method="uv_tool", timeout=MCP_UPGRADE_TIMEOUT)
        assert ok is False
        assert "timed out" in info


# ---------------------------------------------------------------------------
# VersionService.self_update -- two-stage upgrade (since v0.30.1)
# ---------------------------------------------------------------------------


class TestSelfUpdateTwoStage:
    """Tests for the kbagent + MCP combined upgrade path."""

    @patch("keboola_agent_cli.services.version_service._fetch_kbagent_latest_version")
    @patch("keboola_agent_cli.services.version_service._fetch_mcp_latest_version")
    @patch("keboola_agent_cli.services.version_service._get_local_mcp_version")
    @patch("keboola_agent_cli.services.version_service._detect_mcp_install_method")
    @patch("keboola_agent_cli.services.version_service._perform_mcp_update")
    def test_both_up_to_date_no_subprocesses(
        self,
        mock_perform: MagicMock,
        mock_detect: MagicMock,
        mock_local: MagicMock,
        mock_mcp_latest: MagicMock,
        mock_kbagent_latest: MagicMock,
    ) -> None:
        from keboola_agent_cli import __version__

        mock_kbagent_latest.return_value = __version__
        mock_mcp_latest.return_value = "1.59.1"
        mock_local.return_value = "1.59.1"
        mock_detect.return_value = "uv_tool"

        svc = VersionService()
        result = svc.self_update()

        assert result["updated"] is False
        assert result["kbagent"]["updated"] is False
        assert result["mcp"]["updated"] is False
        mock_perform.assert_not_called()

    @patch("keboola_agent_cli.services.version_service._fetch_kbagent_latest_version")
    @patch("keboola_agent_cli.services.version_service._fetch_mcp_latest_version")
    @patch("keboola_agent_cli.services.version_service._get_local_mcp_version")
    @patch("keboola_agent_cli.services.version_service._detect_mcp_install_method")
    @patch("keboola_agent_cli.services.version_service._perform_mcp_update")
    def test_only_mcp_stale_kbagent_uptodate_still_runs_mcp(
        self,
        mock_perform: MagicMock,
        mock_detect: MagicMock,
        mock_local: MagicMock,
        mock_mcp_latest: MagicMock,
        mock_kbagent_latest: MagicMock,
    ) -> None:
        from keboola_agent_cli import __version__

        mock_kbagent_latest.return_value = __version__  # kbagent up-to-date
        mock_mcp_latest.return_value = "1.59.1"
        # Bug E fix: pre-upgrade returns 1.49.0; post-upgrade returns 1.59.1.
        # The version delta is what flips `updated` to True (not just the
        # subprocess exit code). Side-effect list models the two calls.
        mock_local.side_effect = ["1.49.0", "1.59.1"]
        mock_detect.return_value = "uv_tool"
        mock_perform.return_value = (True, "ok")

        svc = VersionService()
        result = svc.self_update()

        # Critical: kbagent up-to-date does NOT short-circuit MCP stage.
        assert result["mcp"]["updated"] is True
        assert result["updated"] is True
        mock_perform.assert_called_once()

    @patch("keboola_agent_cli.services.version_service._fetch_kbagent_latest_version")
    @patch("keboola_agent_cli.services.version_service._fetch_mcp_latest_version")
    @patch("keboola_agent_cli.services.version_service._get_local_mcp_version")
    @patch("keboola_agent_cli.services.version_service._detect_mcp_install_method")
    @patch("keboola_agent_cli.services.version_service._perform_mcp_update")
    def test_subprocess_succeeds_but_version_unchanged_reports_not_updated(
        self,
        mock_perform: MagicMock,
        mock_detect: MagicMock,
        mock_local: MagicMock,
        mock_mcp_latest: MagicMock,
        mock_kbagent_latest: MagicMock,
    ) -> None:
        """Bug E regression from issue #263.

        Real reproducer (from @ottomansky's trace on v0.30.2):
        ``uv tool upgrade keboola-mcp-server`` exits 0, but uv's
        resolver backtracked to the previously installed v1.32.0
        because v1.59.1 declares a fastmcp constraint the venv cannot
        satisfy. Pre-fix: kbagent reported success and the message
        said "Upgraded keboola-mcp-server (1.32.0 -> 1.32.0)". Post-fix:
        ``updated`` is False, message contains a diagnostic pointing to
        ``uv tool install --reinstall`` with no false-success claim.
        """
        from keboola_agent_cli import __version__

        mock_kbagent_latest.return_value = __version__
        mock_mcp_latest.return_value = "1.59.1"
        # Both pre and post probes return v1.32.0 -- subprocess "succeeded"
        # but the version did not change.
        mock_local.side_effect = ["1.32.0", "1.32.0"]
        mock_detect.return_value = "uv_tool"
        mock_perform.return_value = (True, "no upgrade needed")

        svc = VersionService()
        result = svc.self_update()

        # The lie -- "subprocess exit 0 = upgrade happened" -- is the bug.
        # Truth: pre and post versions are identical, so updated == False.
        assert result["mcp"]["updated"] is False
        # Diagnostic message points the user at `uv tool install --reinstall`
        # so they can investigate the underlying packaging conflict.
        assert "still v1.32.0" in result["mcp"]["message"]
        assert "uv tool install --reinstall" in result["mcp"]["message"]
        # The overall `updated` flag also reflects no change (kbagent itself
        # was up-to-date in this scenario).
        assert result["updated"] is False
