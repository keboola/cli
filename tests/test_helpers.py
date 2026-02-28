"""Tests for commands._helpers shared command-layer utilities."""

from keboola_agent_cli.commands._helpers import map_error_to_exit_code
from keboola_agent_cli.errors import KeboolaApiError


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
