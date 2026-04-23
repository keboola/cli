"""Tests for commands._helpers shared command-layer utilities."""

import pytest

from keboola_agent_cli.commands._helpers import map_error_to_exit_code
from keboola_agent_cli.errors import KeboolaApiError, map_error_code_to_type


class TestMapErrorToExitCode:
    """Tests for map_error_to_exit_code."""

    def test_map_error_to_exit_code_invalid_token(self) -> None:
        """INVALID_TOKEN error maps to exit code 3 (authentication error)."""
        exc = KeboolaApiError(
            message="Invalid token",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )
        assert map_error_to_exit_code(exc) == 3

    def test_map_error_to_exit_code_timeout(self) -> None:
        """TIMEOUT error maps to exit code 4 (network error)."""
        exc = KeboolaApiError(
            message="Request timed out",
            status_code=0,
            error_code="TIMEOUT",
            retryable=True,
        )
        assert map_error_to_exit_code(exc) == 4

    def test_map_error_to_exit_code_connection(self) -> None:
        """CONNECTION_ERROR maps to exit code 4 (network error)."""
        exc = KeboolaApiError(
            message="Connection refused",
            status_code=0,
            error_code="CONNECTION_ERROR",
            retryable=True,
        )
        assert map_error_to_exit_code(exc) == 4

    def test_map_error_to_exit_code_retry_exhausted(self) -> None:
        """RETRY_EXHAUSTED maps to exit code 4 (network error)."""
        exc = KeboolaApiError(
            message="Retries exhausted",
            status_code=503,
            error_code="RETRY_EXHAUSTED",
            retryable=False,
        )
        assert map_error_to_exit_code(exc) == 4

    def test_map_error_to_exit_code_other(self) -> None:
        """Unknown/other error codes map to exit code 1 (general error)."""
        exc = KeboolaApiError(
            message="Something went wrong",
            status_code=500,
            error_code="INTERNAL_ERROR",
            retryable=False,
        )
        assert map_error_to_exit_code(exc) == 1

    def test_map_error_to_exit_code_unknown(self) -> None:
        """Default UNKNOWN_ERROR maps to exit code 1."""
        exc = KeboolaApiError(message="Unknown error")
        assert map_error_to_exit_code(exc) == 1


class TestMapErrorCodeToType:
    """Tests for map_error_code_to_type."""

    def test_invalid_token_maps_to_authentication(self) -> None:
        """INVALID_TOKEN maps to authentication error type."""
        assert map_error_code_to_type("INVALID_TOKEN") == "authentication"

    def test_timeout_maps_to_network(self) -> None:
        """TIMEOUT maps to network error type."""
        assert map_error_code_to_type("TIMEOUT") == "network"

    def test_connection_error_maps_to_network(self) -> None:
        """CONNECTION_ERROR maps to network error type."""
        assert map_error_code_to_type("CONNECTION_ERROR") == "network"

    def test_retry_exhausted_maps_to_network(self) -> None:
        """RETRY_EXHAUSTED maps to network error type."""
        assert map_error_code_to_type("RETRY_EXHAUSTED") == "network"

    def test_not_found_maps_to_not_found(self) -> None:
        """NOT_FOUND maps to not_found error type."""
        assert map_error_code_to_type("NOT_FOUND") == "not_found"

    def test_config_error_maps_to_configuration(self) -> None:
        """CONFIG_ERROR maps to configuration error type."""
        assert map_error_code_to_type("CONFIG_ERROR") == "configuration"

    def test_validation_error_maps_to_validation(self) -> None:
        """VALIDATION_ERROR maps to validation error type."""
        assert map_error_code_to_type("VALIDATION_ERROR") == "validation"

    def test_unknown_code_maps_to_api(self) -> None:
        """Unrecognized error codes fall back to api type."""
        assert map_error_code_to_type("INTERNAL_ERROR") == "api"

    def test_generic_error_maps_to_api(self) -> None:
        """Generic ERROR code falls back to api type."""
        assert map_error_code_to_type("ERROR") == "api"


class TestValidateBranchRequiresProject:
    """Tests for validate_branch_requires_project."""

    def test_validate_branch_requires_project_passes_when_both_set(self) -> None:
        """No error when both branch and project are provided."""
        from unittest.mock import MagicMock

        from keboola_agent_cli.commands._helpers import validate_branch_requires_project

        formatter = MagicMock(json_mode=False)
        formatter.err_console = MagicMock()
        # Should not raise
        validate_branch_requires_project(formatter, branch=123, project="prod")

    def test_validate_branch_requires_project_raises_when_branch_without_project(
        self,
    ) -> None:
        """Raises typer.Exit(code=2) when branch is set but project is not."""
        from unittest.mock import MagicMock

        import typer

        from keboola_agent_cli.commands._helpers import validate_branch_requires_project

        formatter = MagicMock(json_mode=False)
        formatter.err_console = MagicMock()

        with pytest.raises(typer.Exit) as exc_info:
            validate_branch_requires_project(formatter, branch=123, project=None)
        assert exc_info.value.exit_code == 2
        formatter.error.assert_called_once()

    def test_validate_branch_requires_project_passes_when_neither_set(self) -> None:
        """No error when neither branch nor project are provided."""
        from unittest.mock import MagicMock

        from keboola_agent_cli.commands._helpers import validate_branch_requires_project

        formatter = MagicMock(json_mode=False)
        formatter.err_console = MagicMock()
        # Should not raise
        validate_branch_requires_project(formatter, branch=None, project=None)


class TestResolveBranch:
    """Tests for resolve_branch."""

    def test_resolve_branch_explicit_branch_wins(self, tmp_config_dir) -> None:
        """Explicit --branch value is returned as-is, regardless of config."""
        from unittest.mock import MagicMock

        from keboola_agent_cli.commands._helpers import resolve_branch
        from keboola_agent_cli.config_store import ConfigStore
        from keboola_agent_cli.models import ProjectConfig

        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="tok-123",
                active_branch_id=999,
            ),
        )

        formatter = MagicMock(json_mode=False)
        formatter.err_console = MagicMock()

        project, branch_id = resolve_branch(store, formatter, "prod", 123)
        assert project == "prod"
        assert branch_id == 123

    def test_resolve_branch_uses_active_branch(self, tmp_config_dir) -> None:
        """When no explicit --branch, active_branch_id from config is used."""
        from unittest.mock import MagicMock

        from keboola_agent_cli.commands._helpers import resolve_branch
        from keboola_agent_cli.config_store import ConfigStore
        from keboola_agent_cli.models import ProjectConfig

        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="tok-123",
                active_branch_id=555,
            ),
        )

        formatter = MagicMock(json_mode=False)
        formatter.err_console = MagicMock()

        project, branch_id = resolve_branch(store, formatter, "prod", None)
        assert project == "prod"
        assert branch_id == 555
        # Should print info message in human mode
        formatter.err_console.print.assert_called_once()

    def test_resolve_branch_no_branch_returns_none(self, tmp_config_dir) -> None:
        """When no explicit --branch and no active branch, returns None."""
        from unittest.mock import MagicMock

        from keboola_agent_cli.commands._helpers import resolve_branch
        from keboola_agent_cli.config_store import ConfigStore
        from keboola_agent_cli.models import ProjectConfig

        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="tok-123",
            ),
        )

        formatter = MagicMock(json_mode=False)
        formatter.err_console = MagicMock()

        project, branch_id = resolve_branch(store, formatter, "prod", None)
        assert project == "prod"
        assert branch_id is None

    def test_ignore_active_branch_returns_none_when_active_set(self, tmp_config_dir) -> None:
        """With ignore_active_branch=True, implicit active_branch_id is skipped.

        Used by storage read commands so users with an active dev branch
        still see production tables/buckets by default.
        """
        from unittest.mock import MagicMock

        from keboola_agent_cli.commands._helpers import resolve_branch
        from keboola_agent_cli.config_store import ConfigStore
        from keboola_agent_cli.models import ProjectConfig

        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="tok-123",
                active_branch_id=15931,
            ),
        )

        formatter = MagicMock(json_mode=False)
        formatter.err_console = MagicMock()

        project, branch_id = resolve_branch(
            store, formatter, "prod", None, ignore_active_branch=True
        )
        assert project == "prod"
        assert branch_id is None
        # User must be told production is being used despite active dev branch.
        formatter.err_console.print.assert_called_once()
        msg = formatter.err_console.print.call_args.args[0]
        assert "production" in msg.lower()
        assert "15931" in msg

    def test_ignore_active_branch_does_not_override_explicit_branch(self, tmp_config_dir) -> None:
        """Explicit --branch wins even when ignore_active_branch=True."""
        from unittest.mock import MagicMock

        from keboola_agent_cli.commands._helpers import resolve_branch
        from keboola_agent_cli.config_store import ConfigStore
        from keboola_agent_cli.models import ProjectConfig

        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="tok-123",
                active_branch_id=15931,
            ),
        )

        formatter = MagicMock(json_mode=False)
        formatter.err_console = MagicMock()

        project, branch_id = resolve_branch(store, formatter, "prod", 99, ignore_active_branch=True)
        assert project == "prod"
        assert branch_id == 99

    def test_ignore_active_branch_no_config_returns_none(self, tmp_config_dir) -> None:
        """With ignore_active_branch=True and no active branch, still returns None."""
        from unittest.mock import MagicMock

        from keboola_agent_cli.commands._helpers import resolve_branch
        from keboola_agent_cli.config_store import ConfigStore
        from keboola_agent_cli.models import ProjectConfig

        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="tok-123",
            ),
        )

        formatter = MagicMock(json_mode=False)
        formatter.err_console = MagicMock()

        project, branch_id = resolve_branch(
            store, formatter, "prod", None, ignore_active_branch=True
        )
        assert project == "prod"
        assert branch_id is None
        # No info message needed -- there was no active branch to ignore.
        formatter.err_console.print.assert_not_called()

    def test_ignore_active_branch_json_mode_silent(self, tmp_config_dir) -> None:
        """In --json mode, ignore_active_branch still works but prints nothing."""
        from unittest.mock import MagicMock

        from keboola_agent_cli.commands._helpers import resolve_branch
        from keboola_agent_cli.config_store import ConfigStore
        from keboola_agent_cli.models import ProjectConfig

        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="tok-123",
                active_branch_id=15931,
            ),
        )

        formatter = MagicMock(json_mode=True)
        formatter.err_console = MagicMock()

        project, branch_id = resolve_branch(
            store, formatter, "prod", None, ignore_active_branch=True
        )
        assert project == "prod"
        assert branch_id is None
        formatter.err_console.print.assert_not_called()

    def test_ignore_active_branch_single_project_inferred(self, tmp_config_dir) -> None:
        """Without --project, if a single project has an active branch and
        ignore_active_branch=True, the project is still returned but branch_id is None.
        """
        from unittest.mock import MagicMock

        from keboola_agent_cli.commands._helpers import resolve_branch
        from keboola_agent_cli.config_store import ConfigStore
        from keboola_agent_cli.models import ProjectConfig

        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="tok-123",
                active_branch_id=15931,
            ),
        )

        formatter = MagicMock(json_mode=False)
        formatter.err_console = MagicMock()

        project, branch_id = resolve_branch(store, formatter, None, None, ignore_active_branch=True)
        assert project == "prod"
        assert branch_id is None
        formatter.err_console.print.assert_called_once()


class TestResolveProjectAlias:
    """Tests for resolve_project_alias() (write-op precedence)."""

    def _build(self, tmp_config_dir) -> tuple:
        from unittest.mock import MagicMock

        from keboola_agent_cli.config_store import ConfigStore
        from keboola_agent_cli.models import ProjectConfig
        from keboola_agent_cli.services.project_service import ProjectService

        store = ConfigStore(config_dir=tmp_config_dir)
        for alias, pid in (("prod", 1), ("stage", 2)):
            store.add_project(
                alias,
                ProjectConfig(
                    stack_url="https://connection.keboola.com",
                    token=f"t-{alias}",
                    project_name=alias,
                    project_id=pid,
                ),
            )
        service = ProjectService(config_store=store)
        ctx = MagicMock()
        ctx.obj = {"project_service": service}
        formatter = MagicMock(json_mode=False)
        return ctx, formatter, store

    def test_explicit_wins_over_env_and_pin(self, tmp_config_dir, monkeypatch) -> None:
        from keboola_agent_cli.commands._helpers import resolve_project_alias

        monkeypatch.setenv("KBAGENT_PROJECT", "stage")
        ctx, formatter, _ = self._build(tmp_config_dir)
        assert resolve_project_alias(ctx, formatter, explicit="prod") == "prod"

    def test_env_beats_pin(self, tmp_config_dir, monkeypatch) -> None:
        from keboola_agent_cli.commands._helpers import resolve_project_alias

        monkeypatch.setenv("KBAGENT_PROJECT", "stage")
        ctx, formatter, _ = self._build(tmp_config_dir)
        # pin is prod (first-added); env overrides to stage
        assert resolve_project_alias(ctx, formatter, explicit=None) == "stage"

    def test_pin_used_when_no_env(self, tmp_config_dir, monkeypatch) -> None:
        from keboola_agent_cli.commands._helpers import resolve_project_alias

        monkeypatch.delenv("KBAGENT_PROJECT", raising=False)
        ctx, formatter, _ = self._build(tmp_config_dir)
        assert resolve_project_alias(ctx, formatter, explicit=None) == "prod"

    def test_fail_hard_multi_no_pin(self, tmp_config_dir, monkeypatch) -> None:
        import typer

        from keboola_agent_cli.commands._helpers import resolve_project_alias

        monkeypatch.delenv("KBAGENT_PROJECT", raising=False)
        ctx, formatter, store = self._build(tmp_config_dir)
        cfg = store.load()
        cfg.default_project = ""
        store.save(cfg)

        with pytest.raises(typer.Exit) as exc_info:
            resolve_project_alias(ctx, formatter, explicit=None)
        assert exc_info.value.exit_code == 5
        formatter.error.assert_called_once()


class TestApplyFirewallFlags:
    """Tests for cli.apply_firewall_flags (session-only policy merge)."""

    def test_no_flags_returns_persisted_as_is(self) -> None:
        from keboola_agent_cli.cli import apply_firewall_flags
        from keboola_agent_cli.models import PermissionPolicy

        persisted = PermissionPolicy(mode="allow", allow=[], deny=["branch.delete"])
        result = apply_firewall_flags(persisted, deny_writes=False, deny_destructive=False)
        assert result is persisted

    def test_no_flags_no_persisted_returns_none(self) -> None:
        from keboola_agent_cli.cli import apply_firewall_flags

        result = apply_firewall_flags(None, deny_writes=False, deny_destructive=False)
        assert result is None

    def test_deny_writes_synthesizes_fresh_policy(self) -> None:
        from keboola_agent_cli.cli import apply_firewall_flags

        result = apply_firewall_flags(None, deny_writes=True, deny_destructive=False)
        assert result is not None
        assert result.mode == "allow"
        assert "cli:write" in result.deny
        assert "tool:write" in result.deny

    def test_deny_destructive_synthesizes_fresh_policy(self) -> None:
        from keboola_agent_cli.cli import apply_firewall_flags

        result = apply_firewall_flags(None, deny_writes=False, deny_destructive=True)
        assert result is not None
        assert "cli:destructive" in result.deny
        assert "tool:destructive" in result.deny
        assert "cli:write" not in result.deny

    def test_flags_merge_with_persisted_deny_no_duplicates(self) -> None:
        from keboola_agent_cli.cli import apply_firewall_flags
        from keboola_agent_cli.models import PermissionPolicy

        persisted = PermissionPolicy(mode="allow", allow=[], deny=["branch.delete", "cli:write"])
        result = apply_firewall_flags(persisted, deny_writes=True, deny_destructive=False)
        assert result is not None
        # Existing cli:write preserved (no dup); tool:write appended; custom entry kept.
        assert result.deny.count("cli:write") == 1
        assert "tool:write" in result.deny
        assert "branch.delete" in result.deny
        # Mode preserved.
        assert result.mode == persisted.mode

    def test_flags_preserve_persisted_mode_deny_mode(self) -> None:
        from keboola_agent_cli.cli import apply_firewall_flags
        from keboola_agent_cli.models import PermissionPolicy

        persisted = PermissionPolicy(mode="deny", allow=["cli:read"], deny=[])
        result = apply_firewall_flags(persisted, deny_writes=True, deny_destructive=False)
        assert result is not None
        assert result.mode == "deny"
        assert result.allow == ["cli:read"]
        assert "cli:write" in result.deny

    def test_both_flags_combine(self) -> None:
        from keboola_agent_cli.cli import apply_firewall_flags

        result = apply_firewall_flags(None, deny_writes=True, deny_destructive=True)
        assert result is not None
        # Both prefixes present.
        assert {"cli:write", "tool:write", "cli:destructive", "tool:destructive"} <= set(
            result.deny
        )

    def test_flags_do_not_mutate_persisted(self) -> None:
        from keboola_agent_cli.cli import apply_firewall_flags
        from keboola_agent_cli.models import PermissionPolicy

        persisted = PermissionPolicy(mode="allow", allow=[], deny=["branch.delete"])
        before = list(persisted.deny)
        apply_firewall_flags(persisted, deny_writes=True, deny_destructive=True)
        assert persisted.deny == before, "persisted.deny was mutated in place"
