"""Tests for permissions CLI commands and enforcement via CliRunner."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import typer
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import EXIT_PERMISSION_DENIED
from keboola_agent_cli.models import AppConfig, PermissionPolicy, ProjectConfig

runner = CliRunner()

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"


def _make_store(tmp_path: Path, policy: PermissionPolicy | None = None) -> ConfigStore:
    """Create a ConfigStore with optional permission policy."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    store = ConfigStore(config_dir=config_dir)
    config = AppConfig(
        projects={
            "test": ProjectConfig(
                stack_url="https://connection.keboola.com",
                token=TEST_TOKEN,
            )
        },
        permissions=policy,
    )
    store.save(config)
    return store


class TestPermissionsList:
    """Tests for `kbagent permissions list`."""

    def test_list_json_no_policy(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(app, ["--json", "permissions", "list"])
        assert result.exit_code == 0
        resp = json.loads(result.output)
        data = resp["data"]
        assert isinstance(data, list)
        assert len(data) > 0
        # All should be allowed when no policy
        for op in data:
            assert op["status"] == "allowed"

    def test_list_json_with_policy(self, tmp_path: Path) -> None:
        policy = PermissionPolicy(mode="allow", deny=["branch.delete"])
        store = _make_store(tmp_path, policy)
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(app, ["--json", "permissions", "list"])
        assert result.exit_code == 0
        resp = json.loads(result.output)
        data = resp["data"]
        branch_delete = next(op for op in data if op["name"] == "branch.delete")
        assert branch_delete["status"] == "denied"

    def test_list_filter_by_category(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(
                app, ["--json", "permissions", "list", "--category", "destructive"]
            )
        assert result.exit_code == 0
        resp = json.loads(result.output)
        data = resp["data"]
        for op in data:
            assert op["category"] == "destructive"

    def test_list_reflects_session_deny_writes(self, tmp_path: Path) -> None:
        """--deny-writes must flip 'allowed' -> 'denied' on write ops in the table."""
        store = _make_store(tmp_path)
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(app, ["--deny-writes", "--json", "permissions", "list"])
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        by_name = {op["name"]: op for op in data}

        # Write-classified op should show denied under session --deny-writes.
        assert by_name["project.add"]["status"] == "denied", (
            "permissions list ignored session --deny-writes"
        )
        # Read-classified op should still be allowed.
        assert by_name["project.list"]["status"] == "allowed"

    def test_list_includes_project_use_and_current(self, tmp_path: Path) -> None:
        """Registry entries for the PR5 commands are visible to the engine.

        Guards against a future refactor that accidentally drops them from
        OPERATION_REGISTRY -- unregistered commands would silently default
        to 'write' for category matching, which is fail-closed but invisible.
        """
        store = _make_store(tmp_path)
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(app, ["--json", "permissions", "list"])
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        by_name = {op["name"]: op for op in data}

        assert "project.use" in by_name, "project.use missing from permissions list"
        assert by_name["project.use"]["category"] == "write"

        assert "project.current" in by_name, "project.current missing from permissions list"
        assert by_name["project.current"]["category"] == "read"


class TestPermissionsShow:
    """Tests for `kbagent permissions show`."""

    def test_show_no_policy(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(app, ["--json", "permissions", "show"])
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["active"] is False

    def test_show_with_policy(self, tmp_path: Path) -> None:
        policy = PermissionPolicy(mode="allow", deny=["cli:write", "tool:write"])
        store = _make_store(tmp_path, policy)
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(app, ["--json", "permissions", "show"])
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["active"] is True
        assert data["mode"] == "allow"
        assert "cli:write" in data["deny"]
        assert "tool:write" in data["deny"]

    def test_show_reports_session_flags_without_persisted_policy(self, tmp_path: Path) -> None:
        """--deny-writes with no persisted policy must still report 'active'."""
        store = _make_store(tmp_path)
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(app, ["--deny-writes", "--json", "permissions", "show"])
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["active"] is True, (
            "permissions show reported inactive despite --deny-writes being set"
        )
        assert data["persisted"] is None
        assert "--deny-writes" in data["session_flags"]
        # Defensive: legacy top-level keys (mode/allow/deny) must NOT be
        # present when there is no persisted policy. Downstream consumers
        # that historically used `data["mode"]` should KeyError here and
        # be forced to read the new `session_flags` / `persisted` shape.
        assert "mode" not in data
        assert "allow" not in data
        assert "deny" not in data

    def test_show_reports_session_flags_alongside_persisted(self, tmp_path: Path) -> None:
        """Session flags are reported in addition to persisted policy."""
        policy = PermissionPolicy(mode="allow", deny=["branch.delete"])
        store = _make_store(tmp_path, policy)
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(
                app,
                [
                    "--deny-writes",
                    "--deny-destructive",
                    "--json",
                    "permissions",
                    "show",
                ],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        # Persisted policy still present + legacy top-level keys still there.
        assert data["persisted"]["deny"] == ["branch.delete"]
        assert data["deny"] == ["branch.delete"]  # legacy consumer compatibility
        # Session flags both reported.
        assert data["session_flags"] == ["--deny-writes", "--deny-destructive"]

    def test_show_human_mode_no_policy_no_session(self, tmp_path: Path) -> None:
        """Bare 'permissions show' still prints the legacy empty-state message."""
        store = _make_store(tmp_path)
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(app, ["permissions", "show"])
        assert result.exit_code == 0
        assert "No permission policy configured" in result.output


class TestPermissionsSet:
    """Tests for `kbagent permissions set`."""

    def test_set_deny_mode(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch(
                "keboola_agent_cli.commands.permissions.require_random_code_confirmation",
                return_value=None,
            ),
        ):
            MockStore.return_value = store
            result = runner.invoke(
                app,
                [
                    "--json",
                    "permissions",
                    "set",
                    "--mode",
                    "deny",
                    "--allow",
                    "cli:read",
                    "--allow",
                    "tool:read",
                ],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["mode"] == "deny"
        assert "cli:read" in data["allow"]
        assert "tool:read" in data["allow"]

        # Verify persisted
        config = store.load()
        assert config.permissions is not None
        assert config.permissions.mode == "deny"

    def test_set_allow_mode_with_deny(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch(
                "keboola_agent_cli.commands.permissions.require_random_code_confirmation",
                return_value=None,
            ),
        ):
            MockStore.return_value = store
            result = runner.invoke(
                app,
                [
                    "--json",
                    "permissions",
                    "set",
                    "--mode",
                    "allow",
                    "--deny",
                    "cli:write",
                    "--deny",
                    "tool:write",
                ],
            )
        assert result.exit_code == 0
        config = store.load()
        assert config.permissions is not None
        assert config.permissions.deny == ["cli:write", "tool:write"]

    def test_set_rejected_without_confirmation(self, tmp_path: Path) -> None:
        """set should fail when confirmation is not provided (non-interactive)."""
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch(
                "keboola_agent_cli.commands.permissions.require_random_code_confirmation",
                side_effect=typer.Exit(code=EXIT_PERMISSION_DENIED),
            ),
        ):
            MockStore.return_value = store
            result = runner.invoke(app, ["--json", "permissions", "set", "--mode", "allow"])
        assert result.exit_code == EXIT_PERMISSION_DENIED

    def test_set_invalid_mode(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(app, ["--json", "permissions", "set", "--mode", "invalid"])
        assert result.exit_code == 2


class TestPermissionsReset:
    """Tests for `kbagent permissions reset`."""

    def test_reset_removes_policy(self, tmp_path: Path) -> None:
        policy = PermissionPolicy(mode="allow", deny=["cli:write"])
        store = _make_store(tmp_path, policy)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch(
                "keboola_agent_cli.commands.permissions.require_random_code_confirmation",
                return_value=None,
            ),
        ):
            MockStore.return_value = store
            result = runner.invoke(app, ["--json", "permissions", "reset"])
        assert result.exit_code == 0
        config = store.load()
        assert config.permissions is None

    def test_reset_rejected_without_confirmation(self, tmp_path: Path) -> None:
        """reset should fail when confirmation is not provided."""
        policy = PermissionPolicy(mode="allow", deny=["cli:write"])
        store = _make_store(tmp_path, policy)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch(
                "keboola_agent_cli.commands.permissions.require_random_code_confirmation",
                side_effect=typer.Exit(code=EXIT_PERMISSION_DENIED),
            ),
        ):
            MockStore.return_value = store
            result = runner.invoke(app, ["--json", "permissions", "reset"])
        assert result.exit_code == EXIT_PERMISSION_DENIED
        # Policy should remain unchanged
        config = store.load()
        assert config.permissions is not None


class TestPermissionsCheck:
    """Tests for `kbagent permissions check`."""

    def test_check_allowed(self, tmp_path: Path) -> None:
        policy = PermissionPolicy(mode="allow", deny=["branch.delete"])
        store = _make_store(tmp_path, policy)
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(app, ["--json", "permissions", "check", "config.list"])
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["allowed"] is True

    def test_check_denied(self, tmp_path: Path) -> None:
        policy = PermissionPolicy(mode="allow", deny=["branch.delete"])
        store = _make_store(tmp_path, policy)
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(app, ["--json", "permissions", "check", "branch.delete"])
        assert result.exit_code == EXIT_PERMISSION_DENIED
        data = json.loads(result.output)["data"]
        assert data["allowed"] is False

    def test_check_no_policy_always_allowed(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(app, ["--json", "permissions", "check", "branch.delete"])
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["allowed"] is True

    def test_check_reflects_session_deny_writes(self, tmp_path: Path) -> None:
        """Issue #269 sec-19: ``permissions check`` must apply --deny-writes
        the same way ``permissions list`` does. Pre-fix it consulted only the
        persisted policy, so an AI agent inspecting its own session firewall
        got a misleading 'allowed' answer for write ops."""
        store = _make_store(tmp_path)  # no persisted policy
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(
                app,
                ["--deny-writes", "--json", "permissions", "check", "branch.create"],
            )
        assert result.exit_code == EXIT_PERMISSION_DENIED, (
            f"permissions check ignored --deny-writes: {result.output}"
        )
        data = json.loads(result.output)["data"]
        assert data["allowed"] is False

    def test_check_reflects_session_deny_destructive(self, tmp_path: Path) -> None:
        """``--deny-destructive`` is also picked up by ``permissions check``."""
        store = _make_store(tmp_path)
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(
                app,
                ["--deny-destructive", "--json", "permissions", "check", "branch.delete"],
            )
        assert result.exit_code == EXIT_PERMISSION_DENIED
        data = json.loads(result.output)["data"]
        assert data["allowed"] is False


class TestPermissionsEnforcement:
    """Tests for permission enforcement on CLI commands."""

    def test_denied_command_returns_exit_6(self, tmp_path: Path) -> None:
        """A command blocked by policy should return exit code 6."""
        policy = PermissionPolicy(mode="allow", deny=["branch.create"])
        store = _make_store(tmp_path, policy)
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(
                app,
                [
                    "--json",
                    "branch",
                    "create",
                    "--project",
                    "test",
                    "--name",
                    "my-branch",
                ],
            )
        assert result.exit_code == EXIT_PERMISSION_DENIED

    def test_allowed_command_proceeds(self, tmp_path: Path) -> None:
        """A read command should proceed when writes are blocked."""
        policy = PermissionPolicy(mode="allow", deny=["cli:write"])
        store = _make_store(tmp_path, policy)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
        ):
            MockStore.return_value = store
            service_instance = MockService.return_value
            service_instance.list_projects.return_value = []
            result = runner.invoke(app, ["--json", "project", "list"])
        # Should succeed (exit 0) since project.list is a read operation
        assert result.exit_code == 0

    def test_denied_workspace_delete(self, tmp_path: Path) -> None:
        """workspace delete should be blocked by cli:write deny."""
        policy = PermissionPolicy(mode="allow", deny=["cli:write"])
        store = _make_store(tmp_path, policy)
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(
                app,
                [
                    "--json",
                    "workspace",
                    "delete",
                    "--project",
                    "test",
                    "--workspace-id",
                    "123",
                ],
            )
        assert result.exit_code == EXIT_PERMISSION_DENIED

    def test_permissions_commands_always_accessible(self, tmp_path: Path) -> None:
        """permissions commands should work even in deny-all mode."""
        policy = PermissionPolicy(mode="deny", allow=[])
        store = _make_store(tmp_path, policy)
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            # permissions list should always work
            result = runner.invoke(app, ["--json", "permissions", "list"])
        assert result.exit_code == 0

    def test_top_level_command_enforcement(self, tmp_path: Path) -> None:
        """Top-level commands (init, doctor, etc.) should respect policy."""
        policy = PermissionPolicy(mode="allow", deny=["init"])
        store = _make_store(tmp_path, policy)
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(app, ["--json", "init"])
        assert result.exit_code == EXIT_PERMISSION_DENIED

    def test_help_allowed_on_denied_command(self, tmp_path: Path) -> None:
        """--help should work even on commands blocked by policy."""
        policy = PermissionPolicy(mode="allow", deny=["branch.delete"])
        store = _make_store(tmp_path, policy)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("sys.argv", ["kbagent", "branch", "delete", "--help"]),
        ):
            MockStore.return_value = store
            result = runner.invoke(app, ["branch", "delete", "--help"])
        # Should show help (exit 0), not permission denied
        assert result.exit_code == 0
        assert "delete" in result.output.lower()

    def test_help_allowed_on_denied_subapp(self, tmp_path: Path) -> None:
        """--help on the group level should work even when all subcommands are blocked."""
        policy = PermissionPolicy(mode="allow", deny=["cli:write"])
        store = _make_store(tmp_path, policy)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("sys.argv", ["kbagent", "branch", "--help"]),
        ):
            MockStore.return_value = store
            result = runner.invoke(app, ["branch", "--help"])
        assert result.exit_code == 0


class TestInitReadOnly:
    """Tests for `kbagent init --read-only`."""

    def test_init_read_only_creates_policy(self, tmp_path: Path) -> None:
        """init --read-only --from-global should create config with read-only permission policy."""
        work_dir = tmp_path / "project"
        work_dir.mkdir()
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        # Seed global config with a project so read-only won't refuse an empty workspace.
        global_store = ConfigStore(config_dir=global_dir)
        global_store.save(
            AppConfig(
                projects={
                    "prod": ProjectConfig(
                        stack_url="https://connection.keboola.com",
                        token=TEST_TOKEN,
                    )
                }
            )
        )

        with (
            patch("keboola_agent_cli.commands.init.Path.cwd", return_value=work_dir),
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        ):
            MockStore.return_value = global_store

            result = runner.invoke(app, ["--json", "init", "--from-global", "--read-only"])

        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["read_only"] is True

        # Verify the local config has the permission policy
        local_config_path = work_dir / ".kbagent" / "config.json"
        assert local_config_path.is_file()
        config_data = json.loads(local_config_path.read_text())
        assert config_data["permissions"]["mode"] == "allow"
        assert "cli:write" in config_data["permissions"]["deny"]
        assert "tool:write" in config_data["permissions"]["deny"]

        # Verify config.json is owner-read-only (0400). POSIX only: Windows
        # reports 0o666 for anything writable regardless of its ACL, so these
        # bits carry no information there -- the policy assertions above are
        # what matter on that platform, and they still run.
        if sys.platform != "win32":
            import stat

            file_mode = local_config_path.stat().st_mode
            assert not (file_mode & stat.S_IWUSR), "config.json should not be user-writable"
            assert not (file_mode & stat.S_IRGRP), "config.json should not be group-readable"
            assert file_mode & stat.S_IRUSR, "config.json should be owner-readable"

        # Verify .claude/settings.json was created with comprehensive deny rules
        claude_settings_path = work_dir / ".claude" / "settings.json"
        assert claude_settings_path.is_file()
        claude_settings = json.loads(claude_settings_path.read_text())
        deny_rules = claude_settings["permissions"]["deny"]
        # File operation blocks
        assert "Read(.kbagent/config.json)" in deny_rules
        assert "Edit(.kbagent/config.json)" in deny_rules
        assert "Write(.kbagent/config.json)" in deny_rules
        # Bash blocks: any command mentioning config, chmod, permissions, config-dir
        assert "Bash(*.kbagent/config.json*)" in deny_rules
        assert "Bash(*chmod*.kbagent*)" in deny_rules
        assert "Bash(kbagent permissions set*)" in deny_rules
        assert "Bash(*--config-dir*)" in deny_rules
        assert "Bash(*KBAGENT_CONFIG_DIR*)" in deny_rules
        assert "Bash(kbagent permissions reset*)" in deny_rules

    def test_init_read_only_refuses_empty_workspace(self, tmp_path: Path) -> None:
        """init --read-only with no projects must fail -- would lock the user out.

        Read-only chmod's config.json to 0400 and denies cli:write, so subsequent
        'project add' would be impossible. Refuse to create a useless workspace.
        """
        work_dir = tmp_path / "project"
        work_dir.mkdir()
        global_dir = tmp_path / "global"
        global_dir.mkdir()  # Empty global config -- nothing to copy

        with (
            patch("keboola_agent_cli.commands.init.Path.cwd", return_value=work_dir),
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        ):
            MockStore.return_value = ConfigStore(config_dir=global_dir)

            result = runner.invoke(app, ["--json", "init", "--read-only"])

        assert result.exit_code == 5
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["error"]["code"] == "CONFIG_ERROR"
        assert "empty workspace" in data["error"]["message"]
        # Ensure no config was written
        assert not (work_dir / ".kbagent" / "config.json").exists()

    def test_init_read_only_with_from_global(self, tmp_path: Path) -> None:
        """init --read-only --from-global should copy projects AND set read-only."""
        work_dir = tmp_path / "project"
        work_dir.mkdir()
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        global_store = ConfigStore(config_dir=global_dir)
        global_config = AppConfig(
            projects={
                "prod": ProjectConfig(
                    stack_url="https://connection.keboola.com",
                    token=TEST_TOKEN,
                )
            }
        )
        global_store.save(global_config)

        with (
            patch("keboola_agent_cli.commands.init.Path.cwd", return_value=work_dir),
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        ):
            MockStore.return_value = global_store

            result = runner.invoke(app, ["--json", "init", "--from-global", "--read-only"])

        assert result.exit_code == 0
        local_config_path = work_dir / ".kbagent" / "config.json"
        config_data = json.loads(local_config_path.read_text())
        assert "prod" in config_data["projects"]
        assert config_data["permissions"]["mode"] == "allow"
        assert "cli:write" in config_data["permissions"]["deny"]


class TestMcpToolPermission:
    """Tests for MCP tool permission enforcement in McpService."""

    def test_blocked_tool_raises(self, tmp_path: Path) -> None:
        """McpService._check_tool_permission should raise PermissionDeniedError."""
        from keboola_agent_cli.errors import PermissionDeniedError
        from keboola_agent_cli.services.mcp_service import McpService

        policy = PermissionPolicy(mode="allow", deny=["tool:write"])
        store = _make_store(tmp_path, policy)
        service = McpService(config_store=store)

        import pytest

        with pytest.raises(PermissionDeniedError):
            service._check_tool_permission("create_config")

    def test_allowed_tool_passes(self, tmp_path: Path) -> None:
        """Read tools should pass when only writes are blocked."""
        from keboola_agent_cli.services.mcp_service import McpService

        policy = PermissionPolicy(mode="allow", deny=["tool:write"])
        store = _make_store(tmp_path, policy)
        service = McpService(config_store=store)

        # Should not raise
        service._check_tool_permission("get_configs")
        service._check_tool_permission("list_buckets")

    def test_no_policy_allows_all_tools(self, tmp_path: Path) -> None:
        """Without policy, all tools should be allowed."""
        from keboola_agent_cli.services.mcp_service import McpService

        store = _make_store(tmp_path)
        service = McpService(config_store=store)

        # Should not raise
        service._check_tool_permission("create_config")
        service._check_tool_permission("delete_bucket")
