"""Error types and helpers for Keboola Agent CLI."""


def mask_token(token: str) -> str:
    """Mask a Keboola Storage API token for safe display.

    Preserves the prefix (part before the first dash) and the last 4 characters,
    replacing the middle with '...'.

    Examples:
        mask_token("901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k")
        -> "901-...pt0k"

        mask_token("abc") -> "***"
        mask_token("") -> "***"
    """
    if len(token) < 8:
        return "***"

    dash_index = token.find("-")
    if dash_index == -1 or dash_index >= len(token) - 4:
        return "***"

    prefix = token[:dash_index]
    last4 = token[-4:]
    return f"{prefix}-...{last4}"


class KeboolaApiError(Exception):
    """Raised when a Keboola API call fails."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        error_code: str = "UNKNOWN_ERROR",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        self.retryable = retryable


class ConfigError(Exception):
    """Raised when there is a configuration problem."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PermissionDeniedError(Exception):
    """Raised when an operation is blocked by the permission policy."""

    def __init__(self, operation: str, message: str = "") -> None:
        if not message:
            message = f"Operation '{operation}' is blocked by the active permission policy."
        super().__init__(message)
        self.operation = operation
        self.message = message


_ERROR_CODE_TO_TYPE: dict[str, str] = {
    "INVALID_TOKEN": "authentication",
    "TIMEOUT": "network",
    "CONNECTION_ERROR": "network",
    "RETRY_EXHAUSTED": "network",
    "NOT_FOUND": "not_found",
    "CONFIG_ERROR": "configuration",
    "VALIDATION_ERROR": "validation",
    "PERMISSION_DENIED": "authorization",
}


def map_error_code_to_type(error_code: str) -> str:
    """Map a machine-readable error code to a broad error type category."""
    return _ERROR_CODE_TO_TYPE.get(error_code, "api")
