"""Tests for DoctorService - health check logic extracted from doctor command."""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.models import (
    AppConfig,
    PermissionPolicy,
    ProjectConfig,
    TokenVerifyResponse,
)
from keboola_agent_cli.services.doctor_service import DoctorService


def _make_mock_client(
    project_name: str = "Test Project",
    project_id: int = 1234,
) -> MagicMock:
    """Create a mock KeboolaClient with verify_token returning valid data."""
    mock_client = MagicMock()
    mock_client.verify_token.return_value = TokenVerifyResponse(
        token_id="12345",
        token_description="My Token",
        project_id=project_id,
        project_name=project_name,
        owner_name=project_name,
    )
    return mock_client


def _make_failing_client(error: KeboolaApiError) -> MagicMock:
    """Create a mock KeboolaClient whose verify_token raises the given error."""
    mock_client = MagicMock()
    mock_client.verify_token.side_effect = error
    return mock_client


class TestDoctorServiceCheckConfigFile:
    """Tests for DoctorService._check_config_file() - config file existence and permissions."""

    def test_config_file_not_found(self, tmp_config_dir: Path) -> None:
        """When config file does not exist, returns 'warn' status."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = DoctorService(config_store=store)

        result = service._check_config_file()

        assert result["check"] == "config_file"
        assert result["status"] == "warn"
        assert "not found" in result["message"]

    def test_config_file_exists_correct_permissions(self, tmp_config_dir: Path) -> None:
        """When config file exists with 0600 permissions, returns 'pass' status."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "test",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-xxx-testtoken1234",
                project_name="Test",
                project_id=1234,
            ),
        )
        service = DoctorService(config_store=store)

        result = service._check_config_file()

        assert result["check"] == "config_file"
        assert result["status"] == "pass"
        # Windows takes the early return: mode bits there describe nothing the
        # user controls, so the check reports the file without grading it.
        expected = "not checked on Windows" if os.name == "nt" else "correct permissions"
        assert expected in result["message"]

    @pytest.mark.skipif(
        os.name == "nt",
        reason="POSIX mode bits are not the access-control mechanism on Windows; "
        "os.chmod(0o644) there does not produce a group-readable file, and the "
        "check deliberately short-circuits before grading them",
    )
    def test_config_file_wrong_permissions(self, tmp_config_dir: Path) -> None:
        """When config file exists with wrong permissions, returns 'warn' status."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "test",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-xxx-testtoken1234",
                project_name="Test",
                project_id=1234,
            ),
        )
        # Change permissions to 0644
        store.config_path.chmod(0o644)

        service = DoctorService(config_store=store)

        result = service._check_config_file()

        assert result["check"] == "config_file"
        assert result["status"] == "warn"
        assert "permissions" in result["message"]
        assert "0o644" in result["message"]


class TestDoctorServiceCheckConfigSource:
    """Tests for DoctorService._check_config_source() - config source reporting."""

    def test_reports_global_source(self, tmp_config_dir: Path) -> None:
        """Reports global config source."""
        store = ConfigStore(config_dir=tmp_config_dir, source="global")
        service = DoctorService(config_store=store)

        result = service._check_config_source()

        assert result["check"] == "config_source"
        assert result["status"] == "pass"
        assert "global" in result["message"]

    def test_reports_local_source(self, tmp_config_dir: Path) -> None:
        """Reports local config source."""
        store = ConfigStore(config_dir=tmp_config_dir, source="local")
        service = DoctorService(config_store=store)

        result = service._check_config_source()

        assert result["check"] == "config_source"
        assert result["status"] == "pass"
        assert "local" in result["message"]

    def test_config_source_in_run_checks(self, tmp_config_dir: Path) -> None:
        """run_checks includes config_source as first check."""
        store = ConfigStore(config_dir=tmp_config_dir, source="local")
        service = DoctorService(config_store=store)

        result = service.run_checks()

        assert result["checks"][0]["check"] == "config_source"


class TestDoctorServiceCheckConfigValid:
    """Tests for DoctorService._check_config_valid() - config file validation."""

    def test_no_config_file_returns_skip(self, tmp_config_dir: Path) -> None:
        """When config file does not exist, returns 'skip' status."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = DoctorService(config_store=store)

        result, config = service._check_config_valid()

        assert result["check"] == "config_valid"
        assert result["status"] == "skip"
        assert config is None

    def test_valid_config_file(self, tmp_config_dir: Path) -> None:
        """When config file is valid JSON with projects, returns 'pass'."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "test",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-xxx-testtoken1234",
                project_name="Test",
                project_id=1234,
            ),
        )

        service = DoctorService(config_store=store)

        result, config = service._check_config_valid()

        assert result["check"] == "config_valid"
        assert result["status"] == "pass"
        assert "1 project" in result["message"]
        assert config is not None
        assert len(config.projects) == 1

    def test_invalid_json_config(self, tmp_config_dir: Path) -> None:
        """When config file contains invalid JSON, returns 'fail'."""
        store = ConfigStore(config_dir=tmp_config_dir)
        config_path = tmp_config_dir / "config.json"
        config_path.write_text("not valid json {{{", encoding="utf-8")
        config_path.chmod(0o600)

        service = DoctorService(config_store=store)

        result, config = service._check_config_valid()

        assert result["check"] == "config_valid"
        assert result["status"] == "fail"
        assert "not valid JSON" in result["message"]
        assert config is None

    def test_valid_json_invalid_structure(self, tmp_config_dir: Path) -> None:
        """When config file is valid JSON but has invalid structure, returns 'fail'."""
        store = ConfigStore(config_dir=tmp_config_dir)
        config_path = tmp_config_dir / "config.json"
        # Valid JSON, but invalid structure for AppConfig
        config_path.write_text('{"projects": "not-a-dict"}', encoding="utf-8")
        config_path.chmod(0o600)

        service = DoctorService(config_store=store)

        result, config = service._check_config_valid()

        assert result["check"] == "config_valid"
        assert result["status"] == "fail"
        assert "invalid structure" in result["message"]
        assert config is None


class TestDoctorServiceCheckConnectivity:
    """Tests for DoctorService._check_connectivity() - API connectivity checks."""

    def test_no_config_returns_skip(self, tmp_config_dir: Path) -> None:
        """When config is None, returns skip for connectivity."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = DoctorService(config_store=store)

        results = service._check_connectivity(None)

        assert len(results) == 1
        assert results[0]["check"] == "connectivity"
        assert results[0]["status"] == "skip"

    def test_successful_connectivity(self, tmp_config_dir: Path) -> None:
        """When API responds successfully, returns 'pass' with response time."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-xxx-testtoken1234",
                project_name="Production",
                project_id=1234,
            ),
        )
        config = store.load()

        mock_client = _make_mock_client(project_name="Production", project_id=1234)
        service = DoctorService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        results = service._check_connectivity(config)

        assert len(results) == 1
        assert results[0]["check"] == "connectivity"
        assert results[0]["status"] == "pass"
        assert "Production" in results[0]["message"]
        assert "response_time_ms" in results[0]
        mock_client.close.assert_called_once()

    def test_connectivity_failure(self, tmp_config_dir: Path) -> None:
        """When API call fails, returns 'fail' with error details."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "bad",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-badtoken-abcdef1234",
                project_name="Bad",
                project_id=9999,
            ),
        )
        config = store.load()

        error = KeboolaApiError(
            message="Invalid token",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )
        fail_client = _make_failing_client(error)
        service = DoctorService(
            config_store=store,
            client_factory=lambda url, token: fail_client,
        )

        results = service._check_connectivity(config)

        assert len(results) == 1
        assert results[0]["check"] == "connectivity"
        assert results[0]["status"] == "fail"
        assert "Invalid token" in results[0]["message"]
        assert results[0]["error_code"] == "INVALID_TOKEN"
        fail_client.close.assert_called_once()

    def test_multiple_projects_mixed(self, tmp_config_dir: Path) -> None:
        """With multiple projects, each gets its own connectivity check."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-xxx-testtoken1234",
                project_name="Production",
                project_id=1234,
            ),
        )
        store.add_project(
            "bad",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-badtoken-abcdef1234",
                project_name="Bad",
                project_id=9999,
            ),
        )
        config = store.load()

        error = KeboolaApiError(
            message="Forbidden",
            status_code=403,
            error_code="ACCESS_DENIED",
            retryable=False,
        )

        def factory(url: str, token: str) -> MagicMock:
            if "badtoken" in token:
                return _make_failing_client(error)
            return _make_mock_client(project_name="Production", project_id=1234)

        service = DoctorService(
            config_store=store,
            client_factory=factory,
        )

        results = service._check_connectivity(config)

        assert len(results) == 2
        statuses = {r["alias"]: r["status"] for r in results}
        assert statuses["prod"] == "pass"
        assert statuses["bad"] == "fail"


class TestDoctorServiceCheckVersion:
    """Tests for DoctorService._check_version() - CLI version check."""

    def test_version_check_passes(self, tmp_config_dir: Path) -> None:
        """Version check always returns 'pass' with version string."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = DoctorService(config_store=store)

        result = service._check_version()

        assert result["check"] == "version"
        assert result["status"] == "pass"
        assert "kbagent v" in result["message"]


class TestDoctorServiceCheckConversationId:
    """Tests for DoctorService._check_conversation_id()."""

    def test_conversation_id_warn_when_not_set(self, tmp_config_dir: Path, monkeypatch) -> None:
        """Warns when KBAGENT_CONVERSATION_ID is not set."""
        monkeypatch.delenv("KBAGENT_CONVERSATION_ID", raising=False)
        store = ConfigStore(config_dir=tmp_config_dir)
        service = DoctorService(config_store=store)

        result = service._check_conversation_id()

        assert result["check"] == "conversation_id"
        assert result["status"] == "warn"
        assert "not set" in result["message"]

    def test_conversation_id_pass_when_set(self, tmp_config_dir: Path, monkeypatch) -> None:
        """Passes when KBAGENT_CONVERSATION_ID is set."""
        monkeypatch.setenv("KBAGENT_CONVERSATION_ID", "test-conv-123")
        store = ConfigStore(config_dir=tmp_config_dir)
        service = DoctorService(config_store=store)

        result = service._check_conversation_id()

        assert result["check"] == "conversation_id"
        assert result["status"] == "pass"
        assert "test-conv-123" in result["message"]


class TestDoctorServiceRunChecks:
    """Tests for DoctorService.run_checks() - full health check orchestration."""

    def test_run_checks_returns_all_checks_and_summary(self, tmp_config_dir: Path) -> None:
        """run_checks returns a complete structure with checks and summary."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = DoctorService(config_store=store)

        result = service.run_checks()

        assert "checks" in result
        assert "summary" in result
        assert len(result["checks"]) >= 4  # source, file, valid, connectivity, version
        assert "total" in result["summary"]
        assert "passed" in result["summary"]
        assert "failed" in result["summary"]
        assert "warnings" in result["summary"]
        assert "skipped" in result["summary"]
        assert "healthy" in result["summary"]

    def test_run_checks_no_config_is_healthy(self, tmp_config_dir: Path) -> None:
        """With no config file, run_checks is still healthy (no failures)."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = DoctorService(config_store=store)

        result = service.run_checks()

        assert result["summary"]["healthy"] is True
        assert result["summary"]["failed"] == 0

    def test_run_checks_with_connectivity_failure_is_unhealthy(self, tmp_config_dir: Path) -> None:
        """When a connectivity check fails, healthy is False."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "bad",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-badtoken-abcdef1234",
                project_name="Bad",
                project_id=9999,
            ),
        )

        error = KeboolaApiError(
            message="Invalid token",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )
        fail_client = _make_failing_client(error)
        service = DoctorService(
            config_store=store,
            client_factory=lambda url, token: fail_client,
        )

        result = service.run_checks()

        assert result["summary"]["healthy"] is False
        assert result["summary"]["failed"] >= 1

    def test_run_checks_has_no_mcp_server_check(self, tmp_config_dir: Path) -> None:
        """The MCP server availability probe was removed in v0.85.0.

        The `mcp_tool_tasks` tombstone check stays -- it reports agent tasks
        that still use the removed action, and has nothing to do with probing
        for a server binary.
        """
        store = ConfigStore(config_dir=tmp_config_dir)
        service = DoctorService(config_store=store)

        result = service.run_checks()

        names = {c["check"] for c in result["checks"]}
        assert "mcp_server" not in names
        assert "mcp_tool_tasks" in names


class TestDoctorServiceCheckClaudePlugin:
    """Tests for DoctorService._check_claude_plugin()."""

    def test_skip_when_claude_not_installed(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch
    ) -> None:
        """When ~/.claude/ is absent, returns 'skip' (not a failure)."""
        # Redirect Path.home() to a tmp dir with no .claude/ inside
        fake_home = tmp_path / "no-claude-here"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = DoctorService._check_claude_plugin()

        assert result["check"] == "claude_plugin"
        assert result["status"] == "skip"
        assert "Claude Code not detected" in result["message"]
        # Caveat: this check only covers Claude Code's own cache layout, so it
        # cannot see an install made by another client (e.g. Cursor) of the
        # same marketplace, and the plugin is optional -- `kbagent context` is
        # the client-agnostic substitute.
        assert "only covers Claude Code" in result["message"]
        assert "Cursor" in result["message"]
        assert "kbagent context" in result["message"]

    def test_warn_when_plugin_missing(self, tmp_path: Path, monkeypatch) -> None:
        """When Claude Code is installed but plugin is absent, warn with install commands."""
        fake_home = tmp_path / "has-claude"
        (fake_home / ".claude").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = DoctorService._check_claude_plugin()

        assert result["status"] == "warn"
        assert "/plugin marketplace add keboola/ai-kit" in result["message"]
        assert "/plugin install kbagent@keboola-claude-kit" in result["message"]
        # Same caveat as the skip branch: only Claude Code is detectable here,
        # and the plugin itself is optional.
        assert "only covers Claude Code" in result["message"]
        assert "Cursor" in result["message"]
        assert "kbagent context" in result["message"]

    def test_warn_when_plugin_root_exists_but_empty(self, tmp_path: Path, monkeypatch) -> None:
        """Plugin root dir with no version subdir still counts as not-installed."""
        fake_home = tmp_path / "has-empty-root"
        plugin_root = fake_home / ".claude" / "plugins" / "cache" / "keboola-claude-kit" / "kbagent"
        plugin_root.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = DoctorService._check_claude_plugin()

        assert result["status"] == "warn"

    def test_pass_with_version_from_cache_subdir(self, tmp_path: Path, monkeypatch) -> None:
        """Claude Code caches plugins under <root>/<plugin>/<version>/; derive version from dir name."""
        fake_home = tmp_path / "has-plugin"
        version_dir = (
            fake_home
            / ".claude"
            / "plugins"
            / "cache"
            / "keboola-claude-kit"
            / "kbagent"
            / "0.24.0"
        )
        version_dir.mkdir(parents=True)

        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = DoctorService._check_claude_plugin()

        assert result["status"] == "pass"
        assert "v0.24.0" in result["message"]
        assert result["plugin_version"] == "0.24.0"
        # Installed from the current marketplace -> no migration nagging.
        assert "deprecated" not in result["message"]

    def test_pass_from_legacy_marketplace_cache_dir_carries_migration_note(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A copy installed from this repo's deprecated marketplace still passes.

        The `keboola-agent-cli` marketplace entry is a shim kept alive so existing
        installs keep updating; the cache path is the only signal of where the copy
        came from, so a legacy path passes but carries the reinstall instructions.
        """
        fake_home = tmp_path / "legacy-marketplace"
        version_dir = (
            fake_home / ".claude" / "plugins" / "cache" / "keboola-agent-cli" / "kbagent" / "0.24.0"
        )
        version_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = DoctorService._check_claude_plugin()

        assert result["status"] == "pass"
        assert result["plugin_version"] == "0.24.0"
        assert "deprecated keboola-agent-cli marketplace" in result["message"]
        assert "/plugin marketplace add keboola/ai-kit" in result["message"]
        assert "/plugin install kbagent@keboola-claude-kit" in result["message"]

    def test_pass_prefers_current_marketplace_when_both_cached(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """With both cache dirs present, the current marketplace wins and no note is added."""
        fake_home = tmp_path / "both-marketplaces"
        cache = fake_home / ".claude" / "plugins" / "cache"
        (cache / "keboola-claude-kit" / "kbagent" / "0.24.0").mkdir(parents=True)
        (cache / "keboola-agent-cli" / "kbagent" / "0.20.0").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = DoctorService._check_claude_plugin()

        assert result["status"] == "pass"
        assert result["plugin_version"] == "0.24.0"
        assert "keboola-claude-kit" in result["plugin_path"]
        assert "deprecated" not in result["message"]

    def test_pass_reports_legacy_copy_when_it_is_the_newer_one(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """With both cache dirs present, the NEWER copy wins even when it is the legacy one.

        This is the state the ai-kit publication flip creates: ai-kit trails a cli
        release, so a user with both installs can hold a newer copy under the legacy
        `keboola-agent-cli` dir. Probing the current marketplace first and stopping
        there reported the older claude-kit copy, dropped the migration note, and
        aimed the drift hint at a path the user was not running.
        """
        fake_home = tmp_path / "legacy-is-newer"
        cache = fake_home / ".claude" / "plugins" / "cache"
        (cache / "keboola-claude-kit" / "kbagent" / "0.20.0").mkdir(parents=True)
        (cache / "keboola-agent-cli" / "kbagent" / "0.24.0").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = DoctorService._check_claude_plugin()

        assert result["status"] == "pass"
        # The newer copy is the reported one -- version, path and marketplace agree.
        assert result["plugin_version"] == "0.24.0"
        assert "keboola-agent-cli" in result["plugin_path"]
        assert result["plugin_marketplace"] == "keboola-agent-cli"
        assert "v0.24.0" in result["message"]
        assert "v0.20.0" not in result["message"]
        # A newest-copy-under-the-shim install must still get the migration note.
        assert "deprecated keboola-agent-cli marketplace" in result["message"]
        assert "/plugin install kbagent@keboola-claude-kit" in result["message"]
        # ...and the drift hint must name the marketplace of the copy being reported,
        # not the other one sitting alongside it.
        assert "/plugin update kbagent@keboola-agent-cli" in result["message"]
        assert "kbagent@keboola-claude-kit` in Claude Code to sync" not in result["message"]

    def test_pass_prefers_current_marketplace_on_a_version_tie(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Same version cached under both dirs: report the current marketplace, no note.

        It is the same code either way, so there is nothing to migrate off and no
        reason to nag -- the tie must not be resolved by iteration order alone.
        """
        fake_home = tmp_path / "version-tie"
        cache = fake_home / ".claude" / "plugins" / "cache"
        (cache / "keboola-claude-kit" / "kbagent" / "0.24.0").mkdir(parents=True)
        (cache / "keboola-agent-cli" / "kbagent" / "0.24.0").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = DoctorService._check_claude_plugin()

        assert result["status"] == "pass"
        assert result["plugin_version"] == "0.24.0"
        assert result["plugin_marketplace"] == "keboola-claude-kit"
        assert "deprecated" not in result["message"]

    def test_pass_reports_legacy_copy_when_current_root_is_empty(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A present-but-empty current root must not mask a real legacy install."""
        fake_home = tmp_path / "empty-current-root"
        cache = fake_home / ".claude" / "plugins" / "cache"
        (cache / "keboola-claude-kit" / "kbagent").mkdir(parents=True)
        (cache / "keboola-agent-cli" / "kbagent" / "0.24.0").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = DoctorService._check_claude_plugin()

        assert result["status"] == "pass"
        assert result["plugin_version"] == "0.24.0"
        assert result["plugin_marketplace"] == "keboola-agent-cli"
        assert "deprecated keboola-agent-cli marketplace" in result["message"]

    def test_pass_picks_newest_across_marketplaces_by_pep440_not_string_order(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Cross-marketplace comparison orders by PEP 440, not by dir-name string.

        A string compare puts "0.100.0" below "0.90.0", so once the minor version
        rolls past 99 a plain sort would report a stale copy as the newest -- and
        the repo is already at 0.90.x.
        """
        fake_home = tmp_path / "digit-count-rollover"
        cache = fake_home / ".claude" / "plugins" / "cache"
        (cache / "keboola-claude-kit" / "kbagent" / "0.90.0").mkdir(parents=True)
        (cache / "keboola-agent-cli" / "kbagent" / "0.100.0").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = DoctorService._check_claude_plugin()

        assert result["status"] == "pass"
        assert result["plugin_version"] == "0.100.0"
        assert result["plugin_marketplace"] == "keboola-agent-cli"
        assert "deprecated keboola-agent-cli marketplace" in result["message"]

    def test_pass_picks_newest_when_both_dirs_hold_several_versions(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Multiple cached versions on both sides: the single global newest is reported."""
        fake_home = tmp_path / "many-versions"
        cache = fake_home / ".claude" / "plugins" / "cache"
        for v in ("0.18.0", "0.24.0"):
            (cache / "keboola-claude-kit" / "kbagent" / v).mkdir(parents=True)
        for v in ("0.20.0", "0.23.0"):
            (cache / "keboola-agent-cli" / "kbagent" / v).mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = DoctorService._check_claude_plugin()

        assert result["status"] == "pass"
        assert result["plugin_version"] == "0.24.0"
        assert result["plugin_marketplace"] == "keboola-claude-kit"
        assert "deprecated" not in result["message"]

    def test_pass_unparseable_dir_name_loses_to_a_real_version(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A junk dir name sorts below every real version but never hides an install."""
        fake_home = tmp_path / "junk-dirname"
        cache = fake_home / ".claude" / "plugins" / "cache"
        (cache / "keboola-claude-kit" / "kbagent" / "not-a-version").mkdir(parents=True)
        (cache / "keboola-agent-cli" / "kbagent" / "0.24.0").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = DoctorService._check_claude_plugin()

        assert result["status"] == "pass"
        assert result["plugin_version"] == "0.24.0"
        assert result["plugin_marketplace"] == "keboola-agent-cli"

    def test_pass_lone_unparseable_dir_name_still_reported(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """With nothing else cached, an unparseable dir name is still a pass, not a warn."""
        fake_home = tmp_path / "only-junk-dirname"
        (
            fake_home / ".claude" / "plugins" / "cache" / "keboola-claude-kit" / "kbagent" / "main"
        ).mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = DoctorService._check_claude_plugin()

        assert result["status"] == "pass"
        assert result["plugin_version"] == "main"

    def test_pass_names_the_marketplace_of_the_reported_copy(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The message names the marketplace the reported copy came from."""
        fake_home = tmp_path / "names-marketplace"
        (
            fake_home
            / ".claude"
            / "plugins"
            / "cache"
            / "keboola-claude-kit"
            / "kbagent"
            / "0.24.0"
        ).mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = DoctorService._check_claude_plugin()

        assert result["status"] == "pass"
        assert "from the keboola-claude-kit marketplace" in result["message"]
        assert result["plugin_marketplace"] == "keboola-claude-kit"

    def test_warn_when_only_legacy_root_exists_but_empty(self, tmp_path: Path, monkeypatch) -> None:
        """An empty legacy plugin root is still not-installed (no false pass)."""
        fake_home = tmp_path / "empty-legacy-root"
        (fake_home / ".claude" / "plugins" / "cache" / "keboola-agent-cli" / "kbagent").mkdir(
            parents=True
        )
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = DoctorService._check_claude_plugin()

        assert result["status"] == "warn"

    def test_pass_with_manifest_version_overrides_dirname(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """If plugin.json has a version, it takes precedence over the subdir name."""
        fake_home = tmp_path / "has-manifest"
        version_dir = (
            fake_home
            / ".claude"
            / "plugins"
            / "cache"
            / "keboola-claude-kit"
            / "kbagent"
            / "0.20.0"
        )
        (version_dir / ".claude-plugin").mkdir(parents=True)
        (version_dir / ".claude-plugin" / "plugin.json").write_text(
            '{"name": "kbagent", "version": "0.24.0"}',
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = DoctorService._check_claude_plugin()

        assert result["status"] == "pass"
        assert result["plugin_version"] == "0.24.0"

    def test_pass_with_drift_warning_when_versions_mismatch(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """If plugin version != CLI version, pass still but include drift hint."""
        fake_home = tmp_path / "drifted"
        version_dir = (
            fake_home / ".claude" / "plugins" / "cache" / "keboola-claude-kit" / "kbagent" / "0.1.0"
        )
        version_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = DoctorService._check_claude_plugin()

        assert result["status"] == "pass"
        assert "v0.1.0" in result["message"]
        assert "plugin update kbagent" in result["message"]

    def test_pass_falls_back_to_dirname_when_manifest_broken(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """If plugin.json is unparseable, version still comes from the subdir name."""
        fake_home = tmp_path / "broken-manifest"
        version_dir = (
            fake_home
            / ".claude"
            / "plugins"
            / "cache"
            / "keboola-claude-kit"
            / "kbagent"
            / "0.24.0"
        )
        (version_dir / ".claude-plugin").mkdir(parents=True)
        (version_dir / ".claude-plugin" / "plugin.json").write_text(
            "not json at all",
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = DoctorService._check_claude_plugin()

        assert result["status"] == "pass"
        assert "v0.24.0" in result["message"]

    def test_run_checks_includes_plugin_check(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch
    ) -> None:
        """run_checks includes the Claude Code plugin check in its output."""
        # Point home somewhere without .claude -> skip status
        fake_home = tmp_path / "no-claude"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        store = ConfigStore(config_dir=tmp_config_dir)
        service = DoctorService(config_store=store)
        result = service.run_checks()

        plugin_checks = [c for c in result["checks"] if c["check"] == "claude_plugin"]
        assert len(plugin_checks) == 1
        assert plugin_checks[0]["status"] == "skip"


class TestDoctorServiceCheckInertPermissionPatterns:
    """Tests for DoctorService._check_inert_permission_patterns() (check 9)."""

    def test_skip_without_persisted_policy(self) -> None:
        result = DoctorService._check_inert_permission_patterns(AppConfig())

        assert result["check"] == "inert_permission_patterns"
        assert result["status"] == "skip"

    def test_skip_when_config_could_not_be_parsed(self) -> None:
        result = DoctorService._check_inert_permission_patterns(None)

        assert result["status"] == "skip"

    def test_pass_when_policy_has_no_tool_patterns(self) -> None:
        config = AppConfig(permissions=PermissionPolicy(mode="allow", deny=["cli:write"]))

        result = DoctorService._check_inert_permission_patterns(config)

        assert result["status"] == "pass"
        assert "details" not in result

    def test_warn_lists_the_inert_patterns(self) -> None:
        config = AppConfig(
            permissions=PermissionPolicy(
                mode="deny",
                allow=["tool:read", "cli:read"],
                deny=["tool:write"],
            )
        )

        result = DoctorService._check_inert_permission_patterns(config)

        assert result["status"] == "warn"
        assert "tool:read" in result["message"]
        assert "tool:write" in result["message"]
        assert "docs/mcp-migration.md" in result["message"]
        assert result["details"]["patterns"] == ["tool:read", "tool:write"]
        assert result["details"]["inert_since"] == "0.85.0"
        assert result["details"]["mode"] == "deny"

    def test_warn_lists_generic_dead_patterns_without_tool_prefix(self) -> None:
        """Generalized detection (issue #688): a typo'd pattern is flagged too,
        with no MCP-migration hint or `inert_since` (that context is meaningless
        for a pattern that never had anything to do with the retired `tool:`
        namespace)."""
        config = AppConfig(
            permissions=PermissionPolicy(mode="allow", deny=["stroage.upload-table", "cli:reed"])
        )

        result = DoctorService._check_inert_permission_patterns(config)

        assert result["status"] == "warn"
        assert "stroage.upload-table" in result["message"]
        assert "cli:reed" in result["message"]
        assert "match no known operation" in result["message"]
        assert "docs/mcp-migration.md" not in result["message"]
        assert result["details"]["patterns"] == ["stroage.upload-table", "cli:reed"]
        assert "inert_since" not in result["details"]
        assert result["details"]["mode"] == "allow"

    def test_warn_mentions_both_hints_for_a_mixed_policy(self) -> None:
        """A policy carrying both a `tool:` pattern and a plain typo gets both hints."""
        config = AppConfig(
            permissions=PermissionPolicy(mode="allow", deny=["tool:write", "cli:reed"])
        )

        result = DoctorService._check_inert_permission_patterns(config)

        assert result["status"] == "warn"
        assert "docs/mcp-migration.md" in result["message"]
        assert "kbagent permissions list" in result["message"]
        assert result["details"]["inert_since"] == "0.85.0"

    def test_check_is_registered_in_run_checks(self, tmp_config_dir: Path) -> None:
        store = ConfigStore(config_dir=tmp_config_dir)
        config = store.load()
        config.permissions = PermissionPolicy(mode="deny", allow=["tool:read"])
        store.save(config)

        service = DoctorService(config_store=store)
        checks = service.run_checks()["checks"]

        matching = [c for c in checks if c["check"] == "inert_permission_patterns"]
        assert len(matching) == 1
        assert matching[0]["status"] == "warn"
