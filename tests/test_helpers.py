"""Tests for commands._helpers shared command-layer utilities."""

import os
from collections.abc import Callable
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from keboola_agent_cli.commands import _helpers
from keboola_agent_cli.commands._helpers import (
    discard_token_file,
    map_error_to_exit_code,
    read_password_stdin,
    resolve_storage_token_input,
    token_came_from_command_line,
)
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError, map_error_code_to_type


class TestReadPasswordStdin:
    """`--password-stdin` must work in BOTH TTY mode (hidden getpass prompt,
    Enter to confirm) AND pipe mode (read until EOF) -- the original version
    called `sys.stdin.read()` unconditionally, which hung interactively
    until the user sent Ctrl-D. Shared by `auth login-password` and
    `dev-portal identity add`/`edit` (PR #565 round 2 dedup)."""

    def test_tty_uses_getpass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "pw-typed\n")
        assert read_password_stdin() == "pw-typed"

    def test_pipe_reads_until_eof(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import io
        import sys as _sys

        fake_stdin = io.StringIO("pw-piped\n")
        # Use monkeypatch.setattr (not direct attribute assignment) -- ty rejects
        # `fake_stdin.isatty = lambda: False` because the slot expects `(self) -> bool`
        # and the lambda's signature is `() -> Literal[False]`. monkeypatch handles
        # the duck-typed override cleanly without a ty: ignore.
        monkeypatch.setattr(fake_stdin, "isatty", lambda: False)
        monkeypatch.setattr(_sys, "stdin", fake_stdin)
        assert read_password_stdin() == "pw-piped"


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

    def test_map_error_to_exit_code_storage_job_timeout(self) -> None:
        """STORAGE_JOB_TIMEOUT maps to exit 4, like the queue-job equivalent.

        The Storage API has no cancel, so a timeout means the job is still
        running server-side -- scripts must be able to tell that apart from a
        real failure, which the previous fall-through to exit 1 prevented.
        """
        exc = KeboolaApiError(
            message="Storage job 1 did not complete within 300.0s",
            status_code=504,
            error_code=ErrorCode.STORAGE_JOB_TIMEOUT,
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

    def test_catch_all_codes_take_the_api_default(self) -> None:
        """`_ERROR_CODE_TO_TYPE` lists only codes whose type differs from the
        default, so the catch-alls are deliberately absent from it -- the same
        treatment `UNKNOWN_ERROR` / `INTERNAL_ERROR` / `KAI_ERROR` already get.
        Pinned so adding a member does not imply the map must grow with it."""
        assert map_error_code_to_type("UNEXPECTED_ERROR") == "api"
        assert map_error_code_to_type("UNKNOWN_ERROR") == "api"


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
        assert result.deny == ["cli:write"]

    def test_deny_destructive_synthesizes_fresh_policy(self) -> None:
        from keboola_agent_cli.cli import apply_firewall_flags

        result = apply_firewall_flags(None, deny_writes=False, deny_destructive=True)
        assert result is not None
        assert "cli:destructive" in result.deny
        assert "cli:write" not in result.deny

    def test_flags_merge_with_persisted_deny_no_duplicates(self) -> None:
        from keboola_agent_cli.cli import apply_firewall_flags
        from keboola_agent_cli.models import PermissionPolicy

        persisted = PermissionPolicy(mode="allow", allow=[], deny=["branch.delete", "cli:write"])
        result = apply_firewall_flags(persisted, deny_writes=True, deny_destructive=False)
        assert result is not None
        # Existing cli:write preserved (no dup); custom entry kept.
        assert result.deny.count("cli:write") == 1
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
        # Both categories present.
        assert {"cli:write", "cli:destructive"} <= set(result.deny)

    def test_flags_do_not_mutate_persisted(self) -> None:
        from keboola_agent_cli.cli import apply_firewall_flags
        from keboola_agent_cli.models import PermissionPolicy

        persisted = PermissionPolicy(mode="allow", allow=[], deny=["branch.delete"])
        before = list(persisted.deny)
        apply_firewall_flags(persisted, deny_writes=True, deny_destructive=True)
        assert persisted.deny == before, "persisted.deny was mutated in place"


class TestResolveManageToken:
    """Tests for resolve_manage_token default-deny + opt-in behaviour (since 0.28.0).

    The contract: KBC_MANAGE_API_TOKEN is ignored unless the caller passes
    allow_env=True (plumbed from --allow-env-manage-token at the CLI). When
    ignored, a one-shot stderr warning is emitted and the resolver falls
    through to the TTY-prompt path. With no env and no TTY, it exits 2 with
    an error naming the opt-in flag.
    """

    _SENTINEL = "kbagent-test-sentinel-token-9c4f"

    def test_returns_env_when_allow_env_true(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from keboola_agent_cli.commands._helpers import resolve_manage_token

        monkeypatch.setenv("KBC_MANAGE_API_TOKEN", self._SENTINEL)
        result = resolve_manage_token(allow_env=True)
        assert result == self._SENTINEL
        captured = capsys.readouterr()
        assert "found in environment but ignored" not in captured.err
        assert self._SENTINEL not in captured.out
        assert self._SENTINEL not in captured.err

    def test_default_deny_warns_and_falls_through_to_tty(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from keboola_agent_cli.commands import _helpers
        from keboola_agent_cli.commands._helpers import resolve_manage_token

        monkeypatch.setenv("KBC_MANAGE_API_TOKEN", self._SENTINEL)
        # Force the TTY branch.
        monkeypatch.setattr(_helpers.sys.stdin, "isatty", lambda: True, raising=False)
        # typer.prompt would block on real stdin in tests; replace it.
        monkeypatch.setattr(_helpers.typer, "prompt", lambda *a, **k: "from-prompt")
        result = resolve_manage_token()  # allow_env defaults False
        assert result == "from-prompt"
        err = capsys.readouterr().err
        assert "KBC_MANAGE_API_TOKEN found in environment" in err
        assert "--allow-env-manage-token" in err

    def test_default_deny_no_tty_no_env_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import typer

        from keboola_agent_cli.commands import _helpers
        from keboola_agent_cli.commands._helpers import resolve_manage_token

        monkeypatch.delenv("KBC_MANAGE_API_TOKEN", raising=False)
        monkeypatch.setattr(_helpers.sys.stdin, "isatty", lambda: False, raising=False)
        with pytest.raises(typer.Exit) as exc_info:
            resolve_manage_token()
        assert exc_info.value.exit_code == 2
        err = capsys.readouterr().err
        assert "--allow-env-manage-token" in err
        assert "Run interactively" in err

    def test_default_deny_with_env_no_tty_warns_then_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import typer

        from keboola_agent_cli.commands import _helpers
        from keboola_agent_cli.commands._helpers import resolve_manage_token

        monkeypatch.setenv("KBC_MANAGE_API_TOKEN", self._SENTINEL)
        monkeypatch.setattr(_helpers.sys.stdin, "isatty", lambda: False, raising=False)
        with pytest.raises(typer.Exit) as exc_info:
            resolve_manage_token()
        assert exc_info.value.exit_code == 2
        err = capsys.readouterr().err
        # Both messages on stderr in this branch.
        assert "found in environment but ignored" in err
        assert "Run interactively" in err
        # And the sentinel is never echoed.
        assert self._SENTINEL not in err

    def test_no_env_tty_prompts_normally(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from keboola_agent_cli.commands import _helpers
        from keboola_agent_cli.commands._helpers import resolve_manage_token

        monkeypatch.delenv("KBC_MANAGE_API_TOKEN", raising=False)
        monkeypatch.setattr(_helpers.sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(_helpers.typer, "prompt", lambda *a, **k: "tty-token")
        result = resolve_manage_token()
        assert result == "tty-token"
        err = capsys.readouterr().err
        assert "found in environment but ignored" not in err

    def test_token_value_never_appears_in_captured_output(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Regression pin: the sentinel must never leak to stdout/stderr."""
        from keboola_agent_cli.commands._helpers import resolve_manage_token

        monkeypatch.setenv("KBC_MANAGE_API_TOKEN", self._SENTINEL)
        result = resolve_manage_token(allow_env=True)
        captured = capsys.readouterr()
        assert result == self._SENTINEL  # returned, but not printed
        assert self._SENTINEL not in captured.out
        assert self._SENTINEL not in captured.err

    def test_allow_env_with_unset_env_and_no_tty_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The flag is permission, not promise: with the flag set but no env
        AND no TTY, we still exit 2. Pins the no-source-available failure
        mode for cron/CI diagnostics."""
        import typer

        from keboola_agent_cli.commands import _helpers
        from keboola_agent_cli.commands._helpers import resolve_manage_token

        monkeypatch.delenv("KBC_MANAGE_API_TOKEN", raising=False)
        monkeypatch.setattr(_helpers.sys.stdin, "isatty", lambda: False, raising=False)
        with pytest.raises(typer.Exit) as exc_info:
            resolve_manage_token(allow_env=True)
        assert exc_info.value.exit_code == 2
        err = capsys.readouterr().err
        assert "Run interactively" in err
        # No phantom warning when env was actually empty.
        assert "found in environment but ignored" not in err


class TestRequireRandomCodeConfirmation:
    def test_non_tty_exits_with_permission_denied(self, monkeypatch):
        # stdin isatty -> False
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        with pytest.raises(typer.Exit) as exc:
            from keboola_agent_cli.commands._helpers import require_random_code_confirmation

            require_random_code_confirmation("delete the universe")
        assert exc.value.exit_code == 6  # EXIT_PERMISSION_DENIED

    def test_correct_code_accepted(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(
            "keboola_agent_cli.commands._helpers.secrets.token_hex",
            lambda n: "deadbeef",
        )
        monkeypatch.setattr("builtins.input", lambda: "deadbeef")
        from keboola_agent_cli.commands._helpers import require_random_code_confirmation

        # Returns None on success
        assert require_random_code_confirmation("patch app") is None

    def test_wrong_code_exits(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(
            "keboola_agent_cli.commands._helpers.secrets.token_hex",
            lambda n: "deadbeef",
        )
        monkeypatch.setattr("builtins.input", lambda: "wrongcode")
        with pytest.raises(typer.Exit) as exc:
            from keboola_agent_cli.commands._helpers import require_random_code_confirmation

            require_random_code_confirmation("patch app")
        assert exc.value.exit_code == 6

    def test_eof_exits(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(
            "keboola_agent_cli.commands._helpers.secrets.token_hex",
            lambda n: "deadbeef",
        )

        def raise_eof():
            raise EOFError

        monkeypatch.setattr("builtins.input", raise_eof)
        with pytest.raises(typer.Exit) as exc:
            from keboola_agent_cli.commands._helpers import require_random_code_confirmation

            require_random_code_confirmation("patch app")
        assert exc.value.exit_code == 6


class TestResolveStorageTokenInput:
    """`project add` / `project edit` accept a Storage token from four sources.

    Everything except `--token` keeps the value off the command line, and so
    out of the shell history, the kbagent REPL history file, and a process
    listing. The sources are mutually exclusive, and only a command-line
    `--token` earns the shell-history warning -- a value that Typer merged in
    from KBC_TOKEN was never typed, so warning about it would be a lie.
    """

    _SENTINEL = "9999-kbagent-test-sentinel-storage-token"

    def _write_token_file(self, tmp_path: Path, mode: int = 0o600) -> Path:
        path = tmp_path / "token.txt"
        path.write_text(f"{self._SENTINEL}\n", encoding="utf-8")
        path.chmod(mode)
        return path

    # ----- one source at a time -----

    def test_stdin_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            _helpers, "read_password_stdin", lambda label="Password: ": self._SENTINEL
        )
        result = resolve_storage_token_input(token=None, token_stdin=True, required=True)
        assert result == self._SENTINEL

    def test_file_source_strips_trailing_newline(self, tmp_path: Path) -> None:
        path = self._write_token_file(tmp_path)
        assert resolve_storage_token_input(token=None, token_file=path, required=True) == (
            self._SENTINEL
        )

    def test_env_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CI_KBC_TOKEN", self._SENTINEL)
        result = resolve_storage_token_input(token=None, token_env="CI_KBC_TOKEN", required=True)
        assert result == self._SENTINEL

    def test_token_from_cli_returns_value(self) -> None:
        result = resolve_storage_token_input(token=self._SENTINEL, required=True)
        assert result == self._SENTINEL

    # ----- the shell-history warning -----

    def test_cli_token_on_a_tty_warns_about_shell_history(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(_helpers.sys.stdin, "isatty", lambda: True, raising=False)
        resolve_storage_token_input(token=self._SENTINEL, required=True, token_from_cli=True)
        assert "shell history" in capsys.readouterr().err

    def test_env_token_never_warns(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A KBC_TOKEN value reaches the resolver in the same parameter as
        `--token`, but it was never typed, so it is not in the history."""
        monkeypatch.setattr(_helpers.sys.stdin, "isatty", lambda: True, raising=False)
        resolve_storage_token_input(token=self._SENTINEL, required=True, token_from_cli=False)
        assert "shell history" not in capsys.readouterr().err

    def test_no_tty_does_not_warn(self, capsys: pytest.CaptureFixture[str]) -> None:
        resolve_storage_token_input(token=self._SENTINEL, required=True, token_from_cli=True)
        assert "shell history" not in capsys.readouterr().err

    # ----- mutual exclusion -----

    def test_two_explicit_sources_are_rejected(self, tmp_path: Path) -> None:
        path = self._write_token_file(tmp_path)
        with pytest.raises(typer.BadParameter, match="Specify exactly one of"):
            resolve_storage_token_input(
                token=None, token_stdin=True, token_file=path, required=True
            )

    def test_cli_token_conflicts_with_an_explicit_source(self, tmp_path: Path) -> None:
        path = self._write_token_file(tmp_path)
        with pytest.raises(typer.BadParameter, match="Specify exactly one of"):
            resolve_storage_token_input(token=self._SENTINEL, token_file=path, required=True)

    def test_env_token_loses_to_an_explicit_source(self, tmp_path: Path) -> None:
        """KBC_TOKEN is implicit, so it does not collide -- explicit wins."""
        path = self._write_token_file(tmp_path)
        result = resolve_storage_token_input(
            token="9999-value-from-the-environment",
            token_file=path,
            required=True,
            token_from_cli=False,
        )
        assert result == self._SENTINEL

    def test_keep_token_file_without_token_file_is_rejected(self) -> None:
        with pytest.raises(typer.BadParameter, match="only together with --token-file"):
            resolve_storage_token_input(token=None, keep_token_file=True, required=False)

    # ----- bad input -----

    def test_empty_file_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.txt"
        path.write_text("   \n", encoding="utf-8")
        with pytest.raises(typer.BadParameter, match="is empty"):
            resolve_storage_token_input(token=None, token_file=path, required=True)

    def test_unreadable_file_is_rejected(self, tmp_path: Path) -> None:
        # A directory makes read_text() raise OSError on every platform, which is
        # exactly what the resolver must turn into a BadParameter. chmod(0o000)
        # would not achieve this on Windows (nor as root on POSIX), so it is not
        # a portable way to produce an unreadable path.
        not_a_file = tmp_path / "a_directory"
        not_a_file.mkdir()
        with pytest.raises(typer.BadParameter, match="Cannot read token file"):
            resolve_storage_token_input(token=None, token_file=not_a_file, required=True)

    @pytest.mark.parametrize("value", ["", None])
    def test_unset_or_empty_named_variable_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, value: str | None
    ) -> None:
        if value is None:
            monkeypatch.delenv("CI_KBC_TOKEN", raising=False)
        else:
            monkeypatch.setenv("CI_KBC_TOKEN", value)
        with pytest.raises(typer.BadParameter, match="unset or empty"):
            resolve_storage_token_input(token=None, token_env="CI_KBC_TOKEN", required=True)

    # ----- file mode -----

    @pytest.mark.skipif(
        os.name == "nt",
        reason="POSIX mode bits are not the access-control mechanism on Windows: "
        "chmod cannot narrow an ACL, stat reports 0o666, and the check "
        "short-circuits before grading them (same reason as "
        "test_auth_state_store.py / test_doctor_service.py)",
    )
    def test_world_readable_file_warns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = self._write_token_file(tmp_path, mode=0o644)
        resolve_storage_token_input(token=None, token_file=path, required=True)
        assert "accessible to other users" in capsys.readouterr().err

    @pytest.mark.skipif(
        os.name == "nt",
        reason="POSIX mode bits are not the access-control mechanism on Windows: "
        "chmod cannot narrow an ACL, stat reports 0o666, and the check "
        "short-circuits before grading them",
    )
    def test_private_file_does_not_warn(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = self._write_token_file(tmp_path, mode=0o600)
        resolve_storage_token_input(token=None, token_file=path, required=True)
        assert "accessible to other users" not in capsys.readouterr().err

    @pytest.mark.skipif(
        os.name != "nt",
        reason="verifies the real Windows behaviour: the mode check is skipped, "
        "so a path that stat reports as group/other-accessible raises no warning",
    )
    def test_permission_check_is_skipped_on_windows(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = self._write_token_file(tmp_path, mode=0o644)
        assert resolve_storage_token_input(token=None, token_file=path, required=True) == (
            self._SENTINEL
        )
        assert "accessible to other users" not in capsys.readouterr().err

    # ----- nothing given -----

    def test_optional_token_returns_none_without_prompting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`project edit --new-alias` passes no token and must not be asked for one."""
        monkeypatch.setattr(_helpers.sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(
            _helpers.typer, "prompt", lambda *a, **k: pytest.fail("must not prompt")
        )
        assert resolve_storage_token_input(token=None, required=False) is None

    def test_required_token_prompts_on_a_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_helpers.sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(_helpers.typer, "prompt", lambda *a, **k: "prompted-token")
        assert resolve_storage_token_input(token=None, required=True) == "prompted-token"

    def test_required_token_without_a_tty_exits_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(typer.Exit) as excinfo:
            resolve_storage_token_input(token=None, required=True)
        assert excinfo.value.exit_code == 2
        err = capsys.readouterr().err
        for flag in ("--token-stdin", "--token-file", "--token-env", "KBC_TOKEN"):
            assert flag in err

    # ----- leak regression -----

    def test_token_value_never_appears_in_captured_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Regression pin: no source may echo the token to stdout or stderr."""
        monkeypatch.setenv("CI_KBC_TOKEN", self._SENTINEL)
        monkeypatch.setattr(
            _helpers, "read_password_stdin", lambda label="Password: ": self._SENTINEL
        )
        monkeypatch.setattr(_helpers.sys.stdin, "isatty", lambda: True, raising=False)
        token_file = self._write_token_file(tmp_path, mode=0o644)
        sources: list[Callable[[], str | None]] = [
            lambda: resolve_storage_token_input(token=self._SENTINEL, required=True),
            lambda: resolve_storage_token_input(token=None, token_stdin=True, required=True),
            lambda: resolve_storage_token_input(token=None, token_file=token_file, required=True),
            lambda: resolve_storage_token_input(
                token=None, token_env="CI_KBC_TOKEN", required=True
            ),
        ]
        for resolve in sources:
            assert resolve() == self._SENTINEL
            captured = capsys.readouterr()
            assert self._SENTINEL not in captured.out
            assert self._SENTINEL not in captured.err


class TestDiscardTokenFile:
    """`--token-file` is a carrier, not a store, so the file goes once the
    command has succeeded. Never on a preview, never on a failure, and never
    loudly enough to fail a command that already did its work."""

    def _token_file(self, tmp_path: Path) -> Path:
        path = tmp_path / "token.txt"
        path.write_text("9999-carrier\n", encoding="utf-8")
        return path

    def test_deletes_the_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = self._token_file(tmp_path)
        discard_token_file(path)
        assert not path.exists()
        assert "Deleted the token file" in capsys.readouterr().err

    def test_keep_leaves_the_file_and_warns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = self._token_file(tmp_path)
        discard_token_file(path, keep=True)
        assert path.exists()
        assert "still holds the token" in capsys.readouterr().err

    def test_dry_run_leaves_the_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = self._token_file(tmp_path)
        discard_token_file(path, dry_run=True)
        assert path.exists()
        assert "--dry-run" in capsys.readouterr().err

    def test_no_file_is_a_no_op(self, capsys: pytest.CaptureFixture[str]) -> None:
        discard_token_file(None)
        assert capsys.readouterr().err == ""

    def test_unlink_failure_warns_but_does_not_raise(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """discard must warn, not raise, when the unlink fails -- a read-only
        mount (a Kubernetes secret, a CI volume) is a legitimate setup, and the
        command has already succeeded by this point.

        A directory makes unlink() raise OSError on every platform, so it stands
        in for the read-only-mount failure without depending on POSIX directory
        permissions -- chmod(0o500) on a directory does not block deletion of a
        file inside it on Windows."""
        not_a_file = tmp_path / "a_directory"
        not_a_file.mkdir()
        discard_token_file(not_a_file)
        assert not_a_file.exists()
        assert "could not delete" in capsys.readouterr().err


class TestTokenCameFromCommandLine:
    """Regression pin for the vendored-Click trap.

    Typer ships its own copy of Click, so `click.core.ParameterSource` and the
    enum a running context returns are different classes. The first version of
    this check compared them with `==`, which is always False -- the
    shell-history warning went silent and `--token` stopped conflicting with
    the other sources. Matching on the member name survives both copies.
    """

    def _probe_app(self) -> tuple[typer.Typer, list[bool]]:
        seen: list[bool] = []
        app = typer.Typer()

        @app.command()
        def probe(
            ctx: typer.Context,
            token: str | None = typer.Option(None, envvar="PROBE_TOKEN"),
        ) -> None:
            seen.append(token_came_from_command_line(ctx))

        return app, seen

    def test_command_line_value_is_reported_as_typed(self) -> None:
        app, seen = self._probe_app()
        result = CliRunner().invoke(app, ["--token", "typed-value"])
        assert result.exit_code == 0, result.output
        assert seen == [True]

    def test_environment_value_is_not_reported_as_typed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROBE_TOKEN", "env-value")
        app, seen = self._probe_app()
        result = CliRunner().invoke(app, [])
        assert result.exit_code == 0, result.output
        assert seen == [False]
