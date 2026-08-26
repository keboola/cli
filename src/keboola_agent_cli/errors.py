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
    # An upstream HTTP 401 whose own error text does NOT describe a bad or
    # expired credential -- see `TOKEN_VALIDITY_ERROR_MARKERS` (issue #711).
    # Same "authentication" category and exit code 3 as INVALID_TOKEN; the
    # distinction is that the fault is upstream, so rotating the token is not
    # the fix.
    AUTH_REJECTED = "AUTH_REJECTED"

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
    # Per-project envelope fallback in multi-project commands: the operation
    # raised something that carried no code of its own (see
    # `services.base.project_error_entry`).
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"
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
    # `workspace load` refused to start a COPY of a table larger than
    # WORKSPACE_LOAD_COPY_GUARD_BYTES without confirmation / --force.
    WORKSPACE_LOAD_COPY_TOO_LARGE = "WORKSPACE_LOAD_COPY_TOO_LARGE"

    # Sync
    PARENT_CONFIG_NOT_TRACKED = "PARENT_CONFIG_NOT_TRACKED"
    VARIABLE_LINK_UNRESOLVED = "VARIABLE_LINK_UNRESOLVED"
    SYNC_CONFLICT = "SYNC_CONFLICT"
    SYNC_LEGACY_BOUNDARY = "SYNC_LEGACY_BOUNDARY"

    # Encryption
    ENCRYPTION_FAILED = "ENCRYPTION_FAILED"

    # Job / queue (extensions from 0.22.0)
    JOB_TIMEOUT_TERMINATED = "JOB_TIMEOUT_TERMINATED"

    # Flow (new in 0.22.0)
    SCHEDULE_DELETE_FAILED = "SCHEDULE_DELETE_FAILED"
    # Conditional-flow validation (replaces INVALID_FLOW_DAG; since 0.57.0)
    INVALID_FLOW_DEFINITION = "INVALID_FLOW_DEFINITION"

    # Data apps (new in 0.27.0)
    DATA_APP_BUILD_FAILED = "DATA_APP_BUILD_FAILED"
    DATA_APP_DEPLOY_TIMEOUT = "DATA_APP_DEPLOY_TIMEOUT"
    DATA_APP_INVALID_GIT = "DATA_APP_INVALID_GIT"

    # Data apps - secrets + validate-repo (new in 0.28.0)
    DATA_APP_INVALID_SECRET = "DATA_APP_INVALID_SECRET"
    DATA_APP_INVALID_REPO = "DATA_APP_INVALID_REPO"
    DATA_APP_REPO_VALIDATION_BLOCKING = "DATA_APP_REPO_VALIDATION_BLOCKING"

    # Developer Portal (since 0.48.0)
    DP_LOGIN_FAILED = "DP_LOGIN_FAILED"
    DP_MFA_REQUIRED = "DP_MFA_REQUIRED"
    DP_APP_NOT_FOUND = "DP_APP_NOT_FOUND"
    DP_PUBLISH_REQUIREMENTS_MISSING = "DP_PUBLISH_REQUIREMENTS_MISSING"
    DP_ICON_UPLOAD_FAILED = "DP_ICON_UPLOAD_FAILED"

    # Programmatic auth / browser login (since 0.80.0)
    AUTH_NOT_SUPPORTED_ON_STACK = "AUTH_NOT_SUPPORTED_ON_STACK"
    AUTH_FLOW_TIMEOUT = "AUTH_FLOW_TIMEOUT"
    AUTH_FLOW_DENIED = "AUTH_FLOW_DENIED"
    AUTH_FLOW_EXPIRED = "AUTH_FLOW_EXPIRED"
    AUTH_BROWSER_UNAVAILABLE = "AUTH_BROWSER_UNAVAILABLE"
    AUTH_STATE_MISMATCH = "AUTH_STATE_MISMATCH"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"

    # Password-grant login (since 0.81.0)
    AUTH_MFA_INVALID = "AUTH_MFA_INVALID"

    # Billing / Pay-As-You-Go (since #594)
    PAYG_NOT_AVAILABLE = "PAYG_NOT_AVAILABLE"

    # Merge requests (DMD-1899). The merge 409 has four causes in two wire
    # shapes; these split exactly where the backend does (see
    # docs/merge-requests-notes.md). Mapped in MergeRequestService.merge() --
    # only the service knows the 409 came from the merge endpoint, and the
    # conflict shape carries no machine string code for http_base to key on.
    MR_NOT_READY_TO_MERGE = "MR_NOT_READY_TO_MERGE"
    MR_MERGE_CONFLICT = "MR_MERGE_CONFLICT"


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


class SyncConflictError(Exception):
    """Raised when ``sync pull --force`` would overwrite locally-modified
    configs whose remote **also** changed since the last pull -- a true 3-way
    merge conflict (local and remote both diverged from the synced base).

    ``--force`` deliberately bypasses the "preserve locally-modified files"
    guard, so without this check it would silently adopt the edited on-disk
    file as the new synced baseline (issue: force-pull baseline corruption).
    Rather than discard un-pushed work, the pull aborts *before writing
    anything* and asks the user to resolve each conflict (push or discard
    local edits, then pull again).

    ``conflicts`` carries one dict per conflicting config/row so the command
    layer can list them. Each dict has ``component_id``, ``config_id``,
    ``config_name``, ``path``, ``scope`` (``"config"`` or ``"row"``), and an
    optional ``row_id``.
    """

    def __init__(self, conflicts: list[dict[str, str]]) -> None:
        self.conflicts = conflicts
        n = len(conflicts)
        plural = "s" if n != 1 else ""
        message = (
            f"{n} config{plural} ha{'ve' if n != 1 else 's'} un-pushed local "
            f"edits AND changed on the remote since the last pull (merge "
            f"conflict). `sync pull --force` refuses to overwrite them so your "
            f"local work is not lost. Resolve each conflict first: review with "
            f"`kbagent sync diff`, then either `kbagent sync push` your local "
            f"edits or discard them and pull again -- or run "
            f"`kbagent sync pull --theirs` to discard ALL local changes and "
            f"take the remote version."
        )
        super().__init__(message)
        self.message = message
        self.error_code = ErrorCode.SYNC_CONFLICT


class PermissionDeniedError(Exception):
    """Raised when an operation is blocked by the permission policy."""

    def __init__(self, operation: str, message: str = "") -> None:
        if not message:
            message = f"Operation '{operation}' is blocked by the active permission policy."
        super().__init__(message)
        self.operation = operation
        self.message = message


class SessionAuthUnsupportedError(ConfigError):
    """Raised when a session-registered project (``kbc-session://`` sentinel token)
    reaches a code path that only understands static Storage tokens.

    v1 wires bearer sessions through the Storage and Manage clients. Everything
    outside those paths fails fast here -- the AI / data-science / metastore /
    stream / Scheduler clients, the ``sharing`` master-token path, and the
    importable SDK; the authoritative list is
    ``SESSION_UNSUPPORTED_FEATURES`` in ``services/_auth_registration.py``. The
    Developer Portal client is absent from it because it authenticates with its
    own identity, never a project token. Failing fast beats sending the literal
    sentinel string as a credential, which
    yields an opaque 401 or, worse, gets the sentinel encrypted and persisted as
    if it were a real token. ``kbagent serve`` is **not** among them: it reaches
    Storage and Manage by delegating to those same already-guarded services, so
    session projects do work through it (``server/dependencies.py``).

    Also raised outside the sentinel guards by
    ``ConfigStore._reject_session_credential_swap``, where the project stays
    registered and only its credential *type* is at stake -- which is why the
    default remedy below steps aside for a caller-supplied one.
    """

    def __init__(self, feature: str, *, remedy: str = "") -> None:
        message = f"{feature} does not support browser-login (session) projects yet."
        # A caller that knows its own recovery path replaces the generic
        # suggestion rather than being appended to it: the two are not always
        # compatible, and a message carrying both reads as self-contradictory.
        if remedy:
            message = f"{message} {remedy}"
        else:
            message = (
                f"{message} Point the project at a static Storage token instead: "
                "`kbagent project edit --project <alias> --token <token>` converts the "
                "alias you are already using, replacing its session credential. Use "
                "`kbagent project add --project <new-alias> --url <stack> --token <token>` "
                "only for a genuinely new alias -- `project add` rejects one that already exists."
            )
        super().__init__(message)
        self.feature = feature
        self.error_code = ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK


# Sparse by design: only codes whose broad type differs from the ``"api"``
# default `map_error_code_to_type` returns. Catch-alls (`UNKNOWN_ERROR`,
# `INTERNAL_ERROR`, `UNEXPECTED_ERROR`, ...) and the API/job/storage
# families are deliberately absent -- a new ErrorCode member needs an entry here
# only when "api" would be wrong for it.
_ERROR_CODE_TO_TYPE: dict[str, str] = {
    ErrorCode.INVALID_TOKEN: "authentication",
    ErrorCode.AUTH_REJECTED: "authentication",
    ErrorCode.MISSING_MASTER_TOKEN: "authentication",
    ErrorCode.TIMEOUT: "network",
    ErrorCode.CONNECTION_ERROR: "network",
    ErrorCode.RETRY_EXHAUSTED: "network",
    ErrorCode.NOT_FOUND: "not_found",
    ErrorCode.CONFIG_ERROR: "configuration",
    ErrorCode.VALIDATION_ERROR: "validation",
    ErrorCode.SYNC_CONFLICT: "conflict",
    ErrorCode.SYNC_LEGACY_BOUNDARY: "conflict",
    ErrorCode.PERMISSION_DENIED: "authorization",
    ErrorCode.DP_LOGIN_FAILED: "authentication",
    ErrorCode.DP_MFA_REQUIRED: "authentication",
    ErrorCode.DP_APP_NOT_FOUND: "not_found",
    ErrorCode.DP_PUBLISH_REQUIREMENTS_MISSING: "validation",
    ErrorCode.DP_ICON_UPLOAD_FAILED: "api",
    ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK: "configuration",
    ErrorCode.AUTH_FLOW_TIMEOUT: "authentication",
    ErrorCode.AUTH_FLOW_DENIED: "authentication",
    ErrorCode.AUTH_FLOW_EXPIRED: "authentication",
    ErrorCode.AUTH_BROWSER_UNAVAILABLE: "authentication",
    ErrorCode.AUTH_STATE_MISMATCH: "authentication",
    ErrorCode.SESSION_EXPIRED: "authentication",
    ErrorCode.SESSION_NOT_FOUND: "authentication",
    ErrorCode.AUTH_MFA_INVALID: "authentication",
    # Not reachable from today's only emitter: `BillingService` puts
    # PAYG_NOT_AVAILABLE in its per-project `errors` list, which renders via
    # `formatter.warning()` and never passes through `formatter.error()`.
    # Classified anyway so the first single-project billing command to raise
    # it inherits the right category instead of silently taking the "api"
    # default -- a missing project feature is a configuration problem.
    ErrorCode.PAYG_NOT_AVAILABLE: "configuration",
    # A refused-by-us safety guard, not an upstream fault: nothing was sent to
    # the API, and the caller fixes it by re-issuing the request with --force.
    ErrorCode.WORKSPACE_LOAD_COPY_TOO_LARGE: "validation",
    ErrorCode.MR_NOT_READY_TO_MERGE: "conflict",
    ErrorCode.MR_MERGE_CONFLICT: "conflict",
}


def map_error_code_to_type(error_code: str) -> str:
    """Map a machine-readable error code to a broad error type category."""
    return _ERROR_CODE_TO_TYPE.get(error_code, "api")
