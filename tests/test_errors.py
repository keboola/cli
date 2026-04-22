"""Tests for error types and helpers."""

from keboola_agent_cli.errors import ConfigError, ErrorCode, KeboolaApiError, mask_token


class TestMaskToken:
    """Tests for the mask_token() function."""

    def test_normal_token(self) -> None:
        """A standard Keboola token is masked to show prefix and last 4 chars."""
        result = mask_token("901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k")
        assert result == "901-...pt0k"

    def test_short_prefix_token(self) -> None:
        """Token with a short numeric prefix still masks correctly."""
        result = mask_token("5-abcdefghijklmnop")
        assert result == "5-...mnop"

    def test_empty_string(self) -> None:
        """Empty string returns the safe mask placeholder."""
        result = mask_token("")
        assert result == "***"

    def test_short_token(self) -> None:
        """Very short tokens (< 8 chars) return the safe mask placeholder."""
        assert mask_token("abc") == "***"
        assert mask_token("ab-cde") == "***"
        assert mask_token("1234567") == "***"

    def test_token_exactly_8_chars_with_dash(self) -> None:
        """Token with exactly 8 chars and a dash in a valid position."""
        result = mask_token("a-123456")
        assert result == "a-...3456"

    def test_no_dash_in_token(self) -> None:
        """Token without any dash returns the safe mask placeholder."""
        result = mask_token("abcdefghijklmnop")
        assert result == "***"

    def test_dash_at_end(self) -> None:
        """Token with dash near the end where prefix would consume too much."""
        result = mask_token("abcdefghijklmno-")
        assert result == "***"

    def test_multiple_dashes(self) -> None:
        """Token with multiple dashes uses only the first dash for prefix."""
        result = mask_token("901-123-abcdefghijk")
        assert result == "901-...hijk"


class TestKeboolaApiError:
    """Tests for KeboolaApiError exception."""

    def test_basic_creation(self) -> None:
        """Error can be created with all attributes."""
        err = KeboolaApiError(
            message="Token is invalid",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )
        assert str(err) == "Token is invalid"
        assert err.message == "Token is invalid"
        assert err.status_code == 401
        assert err.error_code == "INVALID_TOKEN"
        assert err.retryable is False

    def test_default_values(self) -> None:
        """Error has sensible defaults for optional fields."""
        err = KeboolaApiError(message="Something failed")
        assert err.status_code == 0
        assert err.error_code == "UNKNOWN_ERROR"
        assert err.retryable is False

    def test_is_exception(self) -> None:
        """KeboolaApiError is a proper Exception subclass."""
        err = KeboolaApiError(message="test")
        assert isinstance(err, Exception)


class TestConfigError:
    """Tests for ConfigError exception."""

    def test_basic_creation(self) -> None:
        """ConfigError stores the message."""
        err = ConfigError(message="Config file is corrupted")
        assert str(err) == "Config file is corrupted"
        assert err.message == "Config file is corrupted"

    def test_is_exception(self) -> None:
        """ConfigError is a proper Exception subclass."""
        err = ConfigError(message="test")
        assert isinstance(err, Exception)


class TestErrorCode:
    """Tests for the ErrorCode enum."""

    def test_str_equality(self) -> None:
        """ErrorCode members compare equal to their plain string values."""
        assert ErrorCode.QUEUE_JOB_FAILED == "QUEUE_JOB_FAILED"
        assert ErrorCode.PERMISSION_DENIED == "PERMISSION_DENIED"
        assert ErrorCode.CONFIG_ERROR == "CONFIG_ERROR"

    def test_is_str(self) -> None:
        """ErrorCode is a subtype of str -- usable wherever a str is expected."""
        code = ErrorCode.NOT_FOUND
        assert isinstance(code, str)

    def test_json_serialisation(self) -> None:
        """ErrorCode serialises to its plain string value in json.dumps."""
        import json

        assert json.dumps(ErrorCode.UPLOAD_FAILED) == '"UPLOAD_FAILED"'

    def test_no_duplicate_values(self) -> None:
        """Every ErrorCode member has a unique string value."""
        values = [c.value for c in ErrorCode]
        assert len(values) == len(set(values)), "Duplicate ErrorCode values detected"

    def test_known_codes_present(self) -> None:
        """Spot-check that key codes defined in the spec are present."""
        required = {
            "INVALID_TOKEN",
            "PERMISSION_DENIED",
            "TIMEOUT",
            "CONNECTION_ERROR",
            "RETRY_EXHAUSTED",
            "API_ERROR",
            "NOT_FOUND",
            "CONFIG_ERROR",
            "QUEUE_JOB_FAILED",
            "QUEUE_JOB_TIMEOUT",
            "STORAGE_JOB_FAILED",
            "NO_VARIABLE_ROWS",
            "MALFORMED_VARIABLES_ROW",
            "UPLOAD_FAILED",
            "UNKNOWN_ERROR",
            "ENCRYPTION_FAILED",
            "WORKSPACE_NOT_FOUND",
        }
        member_names = {c.name for c in ErrorCode}
        missing = required - member_names
        assert not missing, f"Required ErrorCode members missing: {missing}"

    def test_keboolaapierror_default_uses_enum(self) -> None:
        """KeboolaApiError default error_code is an ErrorCode member."""
        err = KeboolaApiError(message="oops")
        assert err.error_code == ErrorCode.UNKNOWN_ERROR
        assert err.error_code == "UNKNOWN_ERROR"

    def test_keboolaapierror_accepts_enum(self) -> None:
        """KeboolaApiError can be constructed with an ErrorCode member."""
        err = KeboolaApiError(
            message="job died",
            error_code=ErrorCode.QUEUE_JOB_FAILED,
        )
        assert err.error_code == "QUEUE_JOB_FAILED"
        assert err.error_code == ErrorCode.QUEUE_JOB_FAILED
