"""Tests for the session-token sentinel helpers (auth/sentinel.py)."""

import pytest

from keboola_agent_cli.auth.sentinel import (
    is_session_token,
    make_session_token,
    parse_session_project_id,
    require_static_token,
)
from keboola_agent_cli.errors import ErrorCode, SessionAuthUnsupportedError


class TestSentinelRoundTrip:
    """make_session_token / is_session_token / parse_session_project_id agree."""

    def test_round_trip(self) -> None:
        token = make_session_token(12345)
        assert token == "kbc-session://12345"
        assert is_session_token(token) is True
        assert parse_session_project_id(token) == 12345

    def test_static_token_is_not_a_session_token(self) -> None:
        static_token = "12345-67890-abcdefghijklmnop"
        assert is_session_token(static_token) is False
        assert parse_session_project_id(static_token) is None


class TestInvalidSentinelBodies:
    """A sentinel with a non-numeric/empty body is still caught by is_session_token,
    so guards never leak it as a credential, but parse_session_project_id yields None."""

    @pytest.mark.parametrize(
        "token",
        [
            "kbc-session://",
            "kbc-session://not-a-number",
            "kbc-session://12.5",
            "kbc-session://-1abc",
        ],
    )
    def test_invalid_body_is_still_recognised_as_sentinel(self, token: str) -> None:
        assert is_session_token(token) is True
        assert parse_session_project_id(token) is None

    def test_empty_string_is_not_a_sentinel(self) -> None:
        assert is_session_token("") is False
        assert parse_session_project_id("") is None


class TestRequireStaticToken:
    """require_static_token raises SessionAuthUnsupportedError on a sentinel token."""

    def test_static_token_passes_through(self) -> None:
        # Must not raise.
        require_static_token("12345-67890-abcdefghijklmnop", feature="The MCP server subprocess")

    def test_session_token_raises_with_correct_error_code(self) -> None:
        with pytest.raises(SessionAuthUnsupportedError) as exc_info:
            require_static_token(make_session_token(1), feature="The MCP server subprocess")

        error = exc_info.value
        assert error.error_code == ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK
        assert error.feature == "The MCP server subprocess"
        assert "The MCP server subprocess" in error.message
        assert "kbagent project add" in error.message

    def test_remedy_is_appended_to_message(self) -> None:
        with pytest.raises(SessionAuthUnsupportedError) as exc_info:
            require_static_token(
                make_session_token(1),
                feature="kbagent kai",
                remedy="Or run 'kbagent kai ask' from a static-token project.",
            )

        assert "Or run 'kbagent kai ask'" in exc_info.value.message

    def test_is_a_config_error(self) -> None:
        """SessionAuthUnsupportedError subclasses ConfigError so every existing
        `except ConfigError` handler already routes it to exit 5."""
        from keboola_agent_cli.errors import ConfigError

        with pytest.raises(ConfigError):
            require_static_token(make_session_token(1), feature="x")
