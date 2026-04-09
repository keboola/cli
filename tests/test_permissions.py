"""Tests for the permission engine (OPERATION_REGISTRY, PermissionEngine, classify_mcp_tool)."""

import pytest

from keboola_agent_cli.errors import PermissionDeniedError
from keboola_agent_cli.models import PermissionPolicy
from keboola_agent_cli.permissions import (
    OPERATION_REGISTRY,
    PermissionEngine,
    classify_mcp_tool,
)


class TestClassifyMcpTool:
    """Tests for classify_mcp_tool()."""

    def test_read_tools(self) -> None:
        assert classify_mcp_tool("get_buckets") == "read"
        assert classify_mcp_tool("list_configs") == "read"
        assert classify_mcp_tool("search") == "read"
        assert classify_mcp_tool("find_tables") == "read"
        assert classify_mcp_tool("docs_query") == "read"

    def test_write_tools(self) -> None:
        assert classify_mcp_tool("create_config") == "write"
        assert classify_mcp_tool("update_config") == "write"
        assert classify_mcp_tool("add_tag") == "write"
        assert classify_mcp_tool("set_metadata") == "write"

    def test_destructive_tools(self) -> None:
        assert classify_mcp_tool("delete_config") == "destructive"
        assert classify_mcp_tool("remove_tag") == "destructive"


class TestOperationRegistry:
    """Tests for the OPERATION_REGISTRY."""

    def test_all_operations_have_valid_categories(self) -> None:
        valid_categories = {"read", "write", "destructive", "admin"}
        for name, category in OPERATION_REGISTRY.items():
            assert category in valid_categories, f"{name} has invalid category: {category}"

    def test_known_read_operations(self) -> None:
        assert OPERATION_REGISTRY["config.list"] == "read"
        assert OPERATION_REGISTRY["job.detail"] == "read"
        assert OPERATION_REGISTRY["project.list"] == "read"
        assert OPERATION_REGISTRY["tool.list"] == "read"

    def test_known_write_operations(self) -> None:
        assert OPERATION_REGISTRY["branch.create"] == "write"
        assert OPERATION_REGISTRY["config.update"] == "write"
        assert OPERATION_REGISTRY["workspace.load"] == "write"

    def test_known_destructive_operations(self) -> None:
        assert OPERATION_REGISTRY["branch.delete"] == "destructive"
        assert OPERATION_REGISTRY["workspace.delete"] == "destructive"
        assert OPERATION_REGISTRY["config.delete"] == "destructive"

    def test_known_admin_operations(self) -> None:
        assert OPERATION_REGISTRY["org.setup"] == "admin"
        assert OPERATION_REGISTRY["project.add"] == "admin"
        assert OPERATION_REGISTRY["project.remove"] == "admin"


class TestPermissionEngineNoPolicy:
    """Tests for PermissionEngine with no policy (None)."""

    def test_no_policy_allows_everything(self) -> None:
        engine = PermissionEngine(None)
        assert engine.is_allowed("branch.delete") is True
        assert engine.is_allowed("tool:create_config") is True
        assert engine.is_allowed("anything.at.all") is True

    def test_no_policy_active_is_false(self) -> None:
        engine = PermissionEngine(None)
        assert engine.active is False

    def test_check_or_raise_passes(self) -> None:
        engine = PermissionEngine(None)
        engine.check_or_raise("branch.delete")  # Should not raise


class TestPermissionEngineAllowMode:
    """Tests for mode='allow' (default-allow, deny blocks specific ops)."""

    def test_empty_deny_allows_everything(self) -> None:
        policy = PermissionPolicy(mode="allow", deny=[])
        engine = PermissionEngine(policy)
        assert engine.is_allowed("branch.delete") is True
        assert engine.is_allowed("tool:create_config") is True

    def test_exact_deny(self) -> None:
        policy = PermissionPolicy(mode="allow", deny=["branch.delete"])
        engine = PermissionEngine(policy)
        assert engine.is_allowed("branch.delete") is False
        assert engine.is_allowed("branch.create") is True
        assert engine.is_allowed("branch.list") is True

    def test_glob_deny(self) -> None:
        policy = PermissionPolicy(mode="allow", deny=["sync.*"])
        engine = PermissionEngine(policy)
        assert engine.is_allowed("sync.push") is False
        assert engine.is_allowed("sync.pull") is False
        assert engine.is_allowed("sync.status") is False
        assert engine.is_allowed("branch.create") is True

    def test_category_cli_write_deny(self) -> None:
        policy = PermissionPolicy(mode="allow", deny=["cli:write"])
        engine = PermissionEngine(policy)
        # Write ops blocked
        assert engine.is_allowed("branch.create") is False
        assert engine.is_allowed("config.update") is False
        # Destructive ops also blocked (cli:write includes destructive and admin)
        assert engine.is_allowed("branch.delete") is False
        assert engine.is_allowed("org.setup") is False
        # Read ops still allowed
        assert engine.is_allowed("config.list") is True
        assert engine.is_allowed("job.detail") is True

    def test_category_tool_write_deny(self) -> None:
        policy = PermissionPolicy(mode="allow", deny=["tool:write"])
        engine = PermissionEngine(policy)
        assert engine.is_allowed("tool:create_config") is False
        assert engine.is_allowed("tool:delete_bucket") is False
        assert engine.is_allowed("tool:get_configs") is True
        assert engine.is_allowed("tool:list_buckets") is True

    def test_tool_glob_deny(self) -> None:
        policy = PermissionPolicy(mode="allow", deny=["tool:create_*"])
        engine = PermissionEngine(policy)
        assert engine.is_allowed("tool:create_config") is False
        assert engine.is_allowed("tool:create_bucket") is False
        assert engine.is_allowed("tool:delete_config") is True
        assert engine.is_allowed("tool:get_configs") is True

    def test_deny_with_allow_override(self) -> None:
        """Allow list can override deny for specific operations."""
        policy = PermissionPolicy(
            mode="allow",
            deny=["cli:write"],
            allow=["branch.create"],
        )
        engine = PermissionEngine(policy)
        # branch.create overridden by allow
        assert engine.is_allowed("branch.create") is True
        # Other writes still blocked
        assert engine.is_allowed("branch.delete") is False
        assert engine.is_allowed("config.update") is False

    def test_vojta_use_case(self) -> None:
        """Vojta's use case: block all write tools, allow everything else."""
        policy = PermissionPolicy(mode="allow", deny=["cli:write", "tool:write"])
        engine = PermissionEngine(policy)
        # Reads allowed
        assert engine.is_allowed("config.list") is True
        assert engine.is_allowed("tool:get_configs") is True
        assert engine.is_allowed("tool.list") is True
        assert engine.is_allowed("project.list") is True
        # Writes blocked
        assert engine.is_allowed("branch.create") is False
        assert engine.is_allowed("tool:create_config") is False
        assert engine.is_allowed("workspace.delete") is False
        assert engine.is_allowed("tool:delete_bucket") is False


class TestPermissionEngineDenyMode:
    """Tests for mode='deny' (default-deny, allow enables specific ops)."""

    def test_empty_allow_denies_everything(self) -> None:
        policy = PermissionPolicy(mode="deny", allow=[])
        engine = PermissionEngine(policy)
        assert engine.is_allowed("config.list") is False
        assert engine.is_allowed("tool:get_configs") is False

    def test_exact_allow(self) -> None:
        policy = PermissionPolicy(mode="deny", allow=["config.list", "project.list"])
        engine = PermissionEngine(policy)
        assert engine.is_allowed("config.list") is True
        assert engine.is_allowed("project.list") is True
        assert engine.is_allowed("branch.create") is False

    def test_category_cli_read_allow(self) -> None:
        policy = PermissionPolicy(mode="deny", allow=["cli:read"])
        engine = PermissionEngine(policy)
        assert engine.is_allowed("config.list") is True
        assert engine.is_allowed("job.detail") is True
        assert engine.is_allowed("branch.create") is False
        assert engine.is_allowed("branch.delete") is False

    def test_category_tool_read_allow(self) -> None:
        policy = PermissionPolicy(mode="deny", allow=["tool:read"])
        engine = PermissionEngine(policy)
        assert engine.is_allowed("tool:get_configs") is True
        assert engine.is_allowed("tool:list_buckets") is True
        assert engine.is_allowed("tool:create_config") is False

    def test_deny_overrides_allow(self) -> None:
        """In deny mode, explicit deny still wins over allow."""
        policy = PermissionPolicy(
            mode="deny",
            allow=["cli:read"],
            deny=["config.list"],
        )
        engine = PermissionEngine(policy)
        # config.list matches both allow (cli:read) and deny (exact)
        # deny wins
        assert engine.is_allowed("config.list") is False
        # Other reads still allowed
        assert engine.is_allowed("job.detail") is True

    def test_read_only_mode(self) -> None:
        """Full read-only: allow only reads."""
        policy = PermissionPolicy(mode="deny", allow=["cli:read", "tool:read"])
        engine = PermissionEngine(policy)
        assert engine.is_allowed("config.list") is True
        assert engine.is_allowed("tool:get_configs") is True
        assert engine.is_allowed("branch.create") is False
        assert engine.is_allowed("tool:create_config") is False


class TestPermissionEngineMetaCommands:
    """permissions.* commands must always be allowed (prevent lockout)."""

    def test_meta_always_allowed_in_allow_mode(self) -> None:
        policy = PermissionPolicy(mode="allow", deny=["permissions.*"])
        engine = PermissionEngine(policy)
        assert engine.is_allowed("permissions.list") is True
        assert engine.is_allowed("permissions.show") is True
        assert engine.is_allowed("permissions.set") is True
        assert engine.is_allowed("permissions.reset") is True
        assert engine.is_allowed("permissions.check") is True

    def test_meta_always_allowed_in_deny_mode(self) -> None:
        policy = PermissionPolicy(mode="deny", allow=[])
        engine = PermissionEngine(policy)
        assert engine.is_allowed("permissions.list") is True
        assert engine.is_allowed("permissions.reset") is True


class TestPermissionEngineCheckOrRaise:
    """Tests for check_or_raise()."""

    def test_raises_on_denied(self) -> None:
        policy = PermissionPolicy(mode="allow", deny=["branch.delete"])
        engine = PermissionEngine(policy)
        with pytest.raises(PermissionDeniedError) as exc_info:
            engine.check_or_raise("branch.delete")
        assert exc_info.value.operation == "branch.delete"
        assert "branch.delete" in exc_info.value.message

    def test_passes_on_allowed(self) -> None:
        policy = PermissionPolicy(mode="allow", deny=["branch.delete"])
        engine = PermissionEngine(policy)
        engine.check_or_raise("config.list")  # Should not raise


class TestPermissionEngineListOperations:
    """Tests for list_operations()."""

    def test_returns_all_operations(self) -> None:
        engine = PermissionEngine(None)
        ops = engine.list_operations()
        # Should have all CLI ops + 3 MCP categories
        cli_ops = [op for op in ops if op["type"] == "cli"]
        mcp_ops = [op for op in ops if op["type"] == "mcp"]
        assert len(cli_ops) == len(OPERATION_REGISTRY)
        assert len(mcp_ops) == 3

    def test_status_reflects_policy(self) -> None:
        policy = PermissionPolicy(mode="allow", deny=["branch.delete"])
        engine = PermissionEngine(policy)
        ops = engine.list_operations()
        branch_delete = next(op for op in ops if op["name"] == "branch.delete")
        config_list = next(op for op in ops if op["name"] == "config.list")
        assert branch_delete["status"] == "denied"
        assert config_list["status"] == "allowed"


class TestFailClosed:
    """Unknown CLI operations should be treated as 'write' for category matching."""

    def test_unknown_op_blocked_by_cli_write(self) -> None:
        """New commands not in OPERATION_REGISTRY are blocked by cli:write."""
        policy = PermissionPolicy(mode="allow", deny=["cli:write"])
        engine = PermissionEngine(policy)
        # This operation doesn't exist in OPERATION_REGISTRY
        assert engine.is_allowed("newfeature.create") is False

    def test_unknown_op_not_matched_by_cli_read(self) -> None:
        """Unknown operations default to 'write', not 'read'."""
        policy = PermissionPolicy(mode="deny", allow=["cli:read"])
        engine = PermissionEngine(policy)
        assert engine.is_allowed("newfeature.create") is False

    def test_unknown_op_allowed_when_no_category_deny(self) -> None:
        """Unknown ops allowed in allow-mode when only specific ops are denied."""
        policy = PermissionPolicy(mode="allow", deny=["branch.delete"])
        engine = PermissionEngine(policy)
        assert engine.is_allowed("newfeature.create") is True


class TestOperationRegistryCompleteness:
    """Verify OPERATION_REGISTRY covers all commands registered in cli.py."""

    def test_all_subapp_commands_registered(self) -> None:
        """Every command in every sub-app should have a registry entry."""

        # Get the Click command object
        import typer.main

        from keboola_agent_cli import cli as cli_module

        click_app = typer.main.get_command(cli_module.app)

        missing = []
        for group_name, group_cmd in click_app.commands.items():
            if hasattr(group_cmd, "commands"):
                # It's a sub-app (Click Group)
                for cmd_name in group_cmd.commands:
                    op = f"{group_name}.{cmd_name}"
                    if op not in OPERATION_REGISTRY:
                        missing.append(op)
            else:
                # Top-level command
                if group_name not in OPERATION_REGISTRY:
                    missing.append(group_name)

        assert missing == [], (
            f"Commands missing from OPERATION_REGISTRY: {missing}. "
            "Add them to permissions.py to ensure they are covered by permission policies."
        )


class TestPermissionPolicyValidation:
    """Tests for PermissionPolicy model validation."""

    def test_valid_modes(self) -> None:
        PermissionPolicy(mode="allow")
        PermissionPolicy(mode="deny")

    def test_invalid_mode(self) -> None:
        with pytest.raises(ValueError, match="must be 'allow' or 'deny'"):
            PermissionPolicy(mode="invalid")

    def test_defaults(self) -> None:
        policy = PermissionPolicy()
        assert policy.mode == "allow"
        assert policy.allow == []
        assert policy.deny == []
