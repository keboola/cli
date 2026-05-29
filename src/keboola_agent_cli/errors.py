"""Error types and helpers for Keboola Agent CLI."""

from enum import StrEnum


class ErrorCode(StrEnum):
    """Stable machine-readable error codes emitted by kbagent.

    ``str`` mixin means values compare equal to their plain-string equivalents
    and serialise as plain strings in JSON output -- no wire-format change.

    Versioning: adding a new code = minor bump; renaming / removing = major bump.
    """

    # Auth / access
    INVALID_TOKEN = "INVALID_TOKEN"
    ACCESS_DENIED = "ACCESS_DENIED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    MISSING_MASTER_TOKEN = "MISSING_MASTER_TOKEN"
    UNAUTHORIZED = "UNAUTHORIZED"  # Bearer-auth rejection by `kbagent serve` (0.40.0+)

    # Network / transport
    TIMEOUT = "TIMEOUT"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"

    # API / generic
    API_ERROR = "API_ERROR"
    NOT_FOUND = "NOT_FOUND"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    INVALID_FORMAT = "INVALID_FORMAT"
    USAGE_ERROR = "USAGE_ERROR"
    MISSING_PARAMETER = "MISSING_PARAMETER"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"
    # `kbagent serve` HTTP envelope (0.40.0+)
    HTTP_ERROR = "HTTP_ERROR"  # Generic HTTP-layer error (Starlette HTTPException passthrough)
    INTERNAL_ERROR = "INTERNAL_ERROR"  # Uncaught exception inside a route handler

    # Configuration
    CONFIG_ERROR = "CONFIG_ERROR"
    NOT_INITIALIZED = "NOT_INITIALIZED"
    INIT_ERROR = "INIT_ERROR"

    # Jobs
    QUEUE_JOB_FAILED = "QUEUE_JOB_FAILED"
    QUEUE_JOB_TIMEOUT = "QUEUE_JOB_TIMEOUT"
    STORAGE_JOB_FAILED = "STORAGE_JOB_FAILED"
    STORAGE_JOB_TIMEOUT = "STORAGE_JOB_TIMEOUT"
    QUERY_JOB_FAILED = "QUERY_JOB_FAILED"
    QUERY_JOB_TIMEOUT = "QUERY_JOB_TIMEOUT"

    # Variables
    NO_VARIABLE_ROWS = "NO_VARIABLE_ROWS"
    MALFORMED_VARIABLES_ROW = "MALFORMED_VARIABLES_ROW"

    # Storage
    UPLOAD_FAILED = "UPLOAD_FAILED"
    EXPORT_EMPTY_MANIFEST = "EXPORT_EMPTY_MANIFEST"
    EXPORT_NO_FILE = "EXPORT_NO_FILE"
    EXPORT_NO_URL = "EXPORT_NO_URL"
    NOT_SLICED = "NOT_SLICED"
    FILE_NO_URL = "FILE_NO_URL"

    # I/O
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    DIR_NOT_FOUND = "DIR_NOT_FOUND"
    READ_ERROR = "READ_ERROR"
    WRITE_ERROR = "WRITE_ERROR"
    INPUT_ERROR = "INPUT_ERROR"

    # Lineage
    NODE_NOT_FOUND = "NODE_NOT_FOUND"

    # Sharing
    INVALID_SHARING_TYPE = "INVALID_SHARING_TYPE"
    NOT_LINKED_BUCKET = "NOT_LINKED_BUCKET"

    # KAI (AI Service)
    KAI_ERROR = "KAI_ERROR"
    KAI_NOT_ENABLED = "KAI_NOT_ENABLED"

    # Workspace / Query
    MISSING_QUERY = "MISSING_QUERY"
    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"

    # Sync
    PARENT_CONFIG_NOT_TRACKED = "PARENT_CONFIG_NOT_TRACKED"
    VARIABLE_LINK_UNRESOLVED = "VARIABLE_LINK_UNRESOLVED"

    # Encryption
    ENCRYPTION_FAILED = "ENCRYPTION_FAILED"

    # Job / queue (extensions from 0.22.0)
    JOB_TIMEOUT_TERMINATED = "JOB_TIMEOUT_TERMINATED"

    # Flow (new in 0.22.0)
    INVALID_FLOW_DAG = "INVALID_FLOW_DAG"
    SCHEDULE_DELETE_FAILED = "SCHEDULE_DELETE_FAILED"

    # Data apps (new in 0.27.0)
    DATA_APP_BUILD_FAILED = "DATA_APP_BUILD_FAILED"
    DATA_APP_DEPLOY_TIMEOUT = "DATA_APP_DEPLOY_TIMEOUT"
    DATA_APP_INVALID_GIT = "DATA_APP_INVALID_GIT"

    # Data apps - secrets + validate-repo (new in 0.28.0)
    DATA_APP_INVALID_SECRET = "DATA_APP_INVALID_SECRET"
    DATA_APP_INVALID_REPO = "DATA_APP_INVALID_REPO"
    DATA_APP_REPO_VALIDATION_BLOCKING = "DATA_APP_REPO_VALIDATION_BLOCKING"


def mask_token(token: str) -> str:
    """Mask a Keboola Storage API token for safe display.

    Preserves the prefix (part before the first dash) and the last 4 characters,
    replacing the middle with '...'.

    Examples:
        mask_token("901-55555-fakeTestTokenDoNotUseXXXXXXXX")
        -> "901-...XXXX"

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
    """Raised when a Keboola API call fails.

    Optional ``details`` payload lets the service layer attach structured
    context (e.g. a fetched log tail, the remote-cancelled job dict) that
    the command layer surfaces in ``--json`` mode without changing the
    stable top-level error envelope. Keep keys small and side-effect-free;
    PR9 will lock this schema down with a versioned enum.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        error_code: str | ErrorCode = ErrorCode.UNKNOWN_ERROR,
        retryable: bool = False,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        self.retryable = retryable
        self.details: dict = details if details is not None else {}


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
    ErrorCode.INVALID_TOKEN: "authentication",
    ErrorCode.MISSING_MASTER_TOKEN: "authentication",
    ErrorCode.TIMEOUT: "network",
    ErrorCode.CONNECTION_ERROR: "network",
    ErrorCode.RETRY_EXHAUSTED: "network",
    ErrorCode.NOT_FOUND: "not_found",
    ErrorCode.CONFIG_ERROR: "configuration",
    ErrorCode.VALIDATION_ERROR: "validation",
    ErrorCode.PERMISSION_DENIED: "authorization",
}


def map_error_code_to_type(error_code: str) -> str:
    """Map a machine-readable error code to a broad error type category."""
    return _ERROR_CODE_TO_TYPE.get(error_code, "api")
