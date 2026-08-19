"""Tests for the permission engine (OPERATION_REGISTRY, PermissionEngine)."""

from typing import ClassVar

import pytest

from keboola_agent_cli.errors import PermissionDeniedError
from keboola_agent_cli.models import PermissionPolicy
from keboola_agent_cli.permissions import (
    FLAG_ESCALATIONS,
    OPERATION_REGISTRY,
    PermissionEngine,
    find_inert_patterns,
)


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

    def test_persisted_tool_patterns_are_inert(self) -> None:
        """A pre-0.85 policy carrying tool:* patterns loads and never matches.

        The `tool` command group and the MCP tool classifier are gone, so no
        operation string starts with `tool:` any more. The stale patterns fall
        through to the plain fnmatch branch, match nothing, and crash nothing.
        """
        policy = PermissionPolicy(mode="allow", deny=["tool:write", "tool:create_*"])
        engine = PermissionEngine(policy)
        assert engine.is_allowed("config.update") is True
        assert engine.is_allowed("job.run") is True
        assert engine.is_allowed("config.list") is True

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
        """Vojta's use case: block all writes, allow everything else."""
        policy = PermissionPolicy(mode="allow", deny=["cli:write"])
        engine = PermissionEngine(policy)
        # Reads allowed
        assert engine.is_allowed("config.list") is True
        assert engine.is_allowed("project.list") is True
        # Writes blocked
        assert engine.is_allowed("branch.create") is False
        assert engine.is_allowed("workspace.delete") is False


class TestPermissionEngineDenyMode:
    """Tests for mode='deny' (default-deny, allow enables specific ops)."""

    def test_empty_allow_denies_everything(self) -> None:
        policy = PermissionPolicy(mode="deny", allow=[])
        engine = PermissionEngine(policy)
        assert engine.is_allowed("config.list") is False
        assert engine.is_allowed("branch.create") is False

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
        policy = PermissionPolicy(mode="deny", allow=["cli:read"])
        engine = PermissionEngine(policy)
        assert engine.is_allowed("config.list") is True
        assert engine.is_allowed("job.detail") is True
        assert engine.is_allowed("branch.create") is False


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
        # Every registry command and every flag escalation -- nothing else.
        cli_ops = [op for op in ops if op["type"] == "cli"]
        assert len(cli_ops) == len(OPERATION_REGISTRY) + len(FLAG_ESCALATIONS)
        assert len(ops) == len(cli_ops)

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
        from keboola_agent_cli.commands.repl import _is_group

        click_app = typer.main.get_command(cli_module.app)
        assert _is_group(click_app)

        missing: list[str] = []

        def _walk(prefix: str, group: object) -> None:
            """Recurse into Click sub-apps, building dotted operation keys.

            Each leaf must be covered by EITHER its own entry
            (``semantic-layer.model.list``) OR a parent prefix entry
            (``semantic-layer.add`` covers ``add metric/dataset/...``). The
            parent-prefix form is correct only when every leaf shares the
            same risk classification.
            """
            cmds = getattr(group, "commands", None)
            if not cmds:
                # Leaf command — accept either its own key or any ancestor.
                if prefix in OPERATION_REGISTRY:
                    return
                parts = prefix.split(".")
                for i in range(len(parts) - 1, 0, -1):
                    if ".".join(parts[:i]) in OPERATION_REGISTRY:
                        return
                missing.append(prefix)
                return
            for cmd_name, cmd in cmds.items():
                # Skip hidden aliases (e.g. `sl` for `semantic-layer`).
                if getattr(cmd, "hidden", False):
                    continue
                op = f"{prefix}.{cmd_name}" if prefix else cmd_name
                _walk(op, cmd)

        for group_name, group_cmd in click_app.commands.items():
            if getattr(group_cmd, "hidden", False):
                continue
            _walk(group_name, group_cmd)

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


class TestDevPortalPermissions:
    """Keys reflect actual Typer paths: `dev-portal.<command>` for the top-level
    sub-app, `dev-portal.identity.<command>` for the identity sub-Typer. Both
    sub-apps carry callbacks that compose those keys via check_cli_permission.

    Categories follow the data-app.secrets-* precedent: credential add/edit are
    `write` (not `admin`); admin is reserved for org-level ops. Publish is
    `admin` (requests Keboola review), deprecate is `destructive` (hides app).
    """

    DP_OPS: ClassVar[dict[str, str]] = {
        # parent descent
        "dev-portal.identity": "read",
        # identity sub-app leaves
        "dev-portal.identity.add": "write",
        "dev-portal.identity.list": "read",
        "dev-portal.identity.edit": "write",
        "dev-portal.identity.remove": "write",
        "dev-portal.identity.use": "write",
        "dev-portal.identity.current": "read",
        "dev-portal.identity.verify": "read",
        # top-level dev-portal commands
        "dev-portal.list": "read",
        "dev-portal.get": "read",
        "dev-portal.create": "write",
        "dev-portal.patch": "write",
        "dev-portal.upload-icon": "write",
        "dev-portal.publish": "admin",
        "dev-portal.deprecate": "destructive",
    }

    def test_registry_contains_all_dev_portal_ops(self):
        from keboola_agent_cli.permissions import OPERATION_REGISTRY

        for op, expected_cat in self.DP_OPS.items():
            assert OPERATION_REGISTRY.get(op) == expected_cat, op


class TestFlagEscalations:
    """`auth logout --remove-projects` carries the admin class the bare command does not.

    Deleting config.json project entries is the same observable effect as the
    admin-class `project remove`, so a policy denying `cli:admin` to keep an
    agent out of the project registry must not leave a way in through `auth`.
    """

    def test_escalated_operation_is_admin_class(self) -> None:
        assert FLAG_ESCALATIONS["auth.logout --remove-projects"] == "admin"

    def test_escalation_is_not_a_registry_key(self) -> None:
        # OPERATION_REGISTRY must hold exactly one key per live command;
        # scripts/check_command_sync.py rejects any key matching no command.
        assert "auth.logout --remove-projects" not in OPERATION_REGISTRY

    def test_denying_admin_blocks_the_flag_but_not_the_command(self) -> None:
        policy = PermissionPolicy(mode="allow", deny=["cli:admin"])
        engine = PermissionEngine(policy)
        assert engine.is_allowed("auth.logout") is True
        assert engine.is_allowed("auth.logout --remove-projects") is False

    def test_allowing_admin_permits_both(self) -> None:
        policy = PermissionPolicy(mode="deny", allow=["cli:admin", "auth.logout"])
        engine = PermissionEngine(policy)
        assert engine.is_allowed("auth.logout") is True
        assert engine.is_allowed("auth.logout --remove-projects") is True

    def test_cli_write_still_covers_the_escalation(self) -> None:
        # cli:write spans write, destructive and admin, so a policy denying all
        # writes must not be loosened by the escalation.
        policy = PermissionPolicy(mode="allow", deny=["cli:write"])
        engine = PermissionEngine(policy)
        assert engine.is_allowed("auth.logout --remove-projects") is False

    def test_listed_so_a_caller_can_see_the_higher_class(self) -> None:
        engine = PermissionEngine(PermissionPolicy(mode="allow", deny=["cli:admin"]))
        entry = next(
            op for op in engine.list_operations() if op["name"] == "auth.logout --remove-projects"
        )
        assert entry["category"] == "admin"
        assert entry["status"] == "denied"


class TestFindInertPatterns:
    """Tests for find_inert_patterns() -- dead rules in a pre-0.85 policy."""

    def test_none_policy_returns_empty(self) -> None:
        assert find_inert_patterns(None) == []

    def test_returns_only_tool_namespace_patterns(self) -> None:
        policy = PermissionPolicy(
            mode="deny",
            allow=["tool:read", "cli:read", "config.list"],
            deny=["tool:write", "branch.delete", "storage.*"],
        )
        assert find_inert_patterns(policy) == ["tool:read", "tool:write"]

    def test_clean_policy_returns_empty(self) -> None:
        policy = PermissionPolicy(mode="allow", deny=["cli:write", "branch.delete"])
        assert find_inert_patterns(policy) == []

    def test_duplicates_are_reported_once(self) -> None:
        policy = PermissionPolicy(mode="deny", allow=["tool:read"], deny=["tool:read"])
        assert find_inert_patterns(policy) == ["tool:read"]
