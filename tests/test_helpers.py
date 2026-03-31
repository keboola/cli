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
