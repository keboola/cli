"""Error types and helpers for Keboola Agent CLI."""

from dataclasses import dataclass
from enum import StrEnum, unique

from .constants import EXIT_JOB_TIMEOUT_TERMINATED, EXIT_PERMISSION_DENIED


@unique
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
    # A safety prompt in human mode without a terminal and without --yes
    # (spec S4, decision D6). Exit 2; the hint names --yes.
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
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
    # A client-side pre-flight found the project lacks a required feature
    # flag (raised via FeatureNotEnabledError). The value matches the string
    # SearchService already emits in its per-project error envelopes, so the
    # two surfaces agree.
    FEATURE_NOT_ENABLED = "FEATURE_NOT_ENABLED"

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
    LINK_UNRESOLVED = "LINK_UNRESOLVED"
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
    # shapes; these split exactly where the backend does (wire-truth notes on
    # branch ms/merge-requests-rfcs): storage.mergeRequests.notReadyToMerge
    # vs storage.mergeRequests.validation, both in the body's top-level
    # `code`. Mapped in MergeRequestService.merge() -- only the service
    # knows the 409 came from the merge endpoint.
    MR_NOT_READY_TO_MERGE = "MR_NOT_READY_TO_MERGE"
    MR_MERGE_CONFLICT = "MR_MERGE_CONFLICT"

    # Updates (#791, CLI-51). After an automatic update to a higher major the
    # first write or destructive command is refused once (exit 6). Reads before
    # it get the same code as a `warnings[]` entry. UPDATED_ACROSS_BREAKING is only a
    # `warnings[]` entry: the update crossed versions with `BREAKING:` notes.
    UPDATED_ACROSS_MAJOR = "UPDATED_ACROSS_MAJOR"
    UPDATED_ACROSS_BREAKING = "UPDATED_ACROSS_BREAKING"


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


def request_outcome_unknown(exc: KeboolaApiError) -> bool:
    """Could a failed, non-replayed POST still have changed state on the server?

    Mirrors how ``BaseHttpClient._request`` classifies a POST it did not
    replay. A 5xx, or a failure with no HTTP response other than one proven
    undelivered, means the request may have reached the server and only the
    answer was lost. Undelivered for certain: a connect error, and a connect /
    pool timeout (raised ``retryable=True``; a read/write timeout is raised
    ``retryable=False``). A 4xx is a definitive refusal -- nothing changed.
    """
    if exc.status_code >= 500:
        return True
    if exc.status_code != 0:
        return False
    if exc.error_code == ErrorCode.CONNECTION_ERROR:
        return False
    return not (exc.error_code == ErrorCode.TIMEOUT and exc.retryable)


class ConfigError(Exception):
    """Raised when there is a configuration problem."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class FeatureNotEnabledError(ConfigError):
    """Raised by a client-side pre-flight when the project lacks a feature flag.

    A missing feature often surfaces server-side as an opaque 403 (or 404)
    indistinguishable from a role denial -- only a pre-flight can word the
    real error. Carries ``error_code`` so a ``--json`` consumer can tell
    "feature not enabled" from every other :class:`ConfigError` shape
    (precedent: ``PAYG_NOT_AVAILABLE`` -- a missing project feature is a
    configuration problem, and ``SessionAuthUnsupportedError`` for the
    ConfigError-with-a-code pattern).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.error_code = ErrorCode.FEATURE_NOT_ENABLED


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

    After CLI-13 the service clients build a bearer for a session, so the
    sentinel reaches nearly every command. Only three features still fail fast
    here -- ``kbagent kai``, ``semantic-layer token --encrypt``, and the
    importable SDK; the authoritative list is ``SESSION_UNSUPPORTED_FEATURES``
    in ``services/_auth_registration.py``. Failing fast beats sending the literal
    sentinel string as a credential, which
    yields an opaque 401 or, worse, gets the sentinel encrypted and persisted as
    if it were a real token. ``kbagent serve`` reaches the supported services by
    delegating to those same already-guarded clients, so session projects do
    work through it (``server/dependencies.py``).

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
                "`kbagent project edit --project <alias> --token-stdin` converts the "
                "alias you are already using, replacing its session credential. Use "
                "`kbagent project add --project <new-alias> --url <stack> --token-stdin` "
                "only for a genuinely new alias -- `project add` rejects one that already exists."
            )
        super().__init__(message)
        self.feature = feature
        self.error_code = ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK


@dataclass(frozen=True)
class ErrorClass:
    """The broad ``error_type`` and the process exit code of one :class:`ErrorCode`."""

    error_type: str
    exit_code: int


# The values `ErrorClass.error_type` may take. `error.error_type` in the
# `--json` envelope is one of them.
ERROR_TYPES: frozenset[str] = frozenset(
    {
        "api",
        "authentication",
        "authorization",
        "configuration",
        "conflict",
        "network",
        "not_found",
        "validation",
    }
)

# Spec rule E1 (docs/cli-interface-spec.md#E1): every ErrorCode has one entry,
# and `map_error_to_exit_code` reads the exit code from here. The exit codes
# follow the table in docs/cli-interface-spec.md#exit-codes: 1 general, 2 usage
# or validation, 3 authentication, 4 network or a local wait that timed out, 5
# configuration, 6 permission policy, 7 job terminated after its timeout.
# docs/error-codes.md lists the same pairs, and `scripts/check_error_codes.py`
# fails when a code has no entry or the doc disagrees with this table.
# An entry's exit keeps the code's existing behavior, and its type is the
# category. So a few entries do not follow the type-to-exit pattern
# (WORKSPACE_LOAD_COPY_TOO_LARGE validation/1, INVALID_FORMAT api/2,
# FILE_NOT_FOUND not_found/2).
ERROR_CODE_TABLE: dict[str, ErrorClass] = {
    # Auth / access
    ErrorCode.INVALID_TOKEN: ErrorClass("authentication", 3),
    ErrorCode.ACCESS_DENIED: ErrorClass("api", 1),
    ErrorCode.PERMISSION_DENIED: ErrorClass("authorization", EXIT_PERMISSION_DENIED),
    ErrorCode.MISSING_MASTER_TOKEN: ErrorClass("authentication", 3),
    ErrorCode.UNAUTHORIZED: ErrorClass("api", 1),
    ErrorCode.AUTH_REJECTED: ErrorClass("authentication", 3),
    # Network / transport
    ErrorCode.TIMEOUT: ErrorClass("network", 4),
    ErrorCode.CONNECTION_ERROR: ErrorClass("network", 4),
    ErrorCode.RETRY_EXHAUSTED: ErrorClass("network", 4),
    # API / generic
    ErrorCode.API_ERROR: ErrorClass("api", 1),
    ErrorCode.NOT_FOUND: ErrorClass("not_found", 1),
    ErrorCode.ALREADY_EXISTS: ErrorClass("api", 1),
    ErrorCode.VALIDATION_ERROR: ErrorClass("validation", 2),
    ErrorCode.INVALID_ARGUMENT: ErrorClass("validation", 2),
    ErrorCode.INVALID_FORMAT: ErrorClass("api", 2),
    ErrorCode.USAGE_ERROR: ErrorClass("validation", 2),
    # A safety prompt nobody can answer: refused by us before any API call (S4).
    ErrorCode.CONFIRMATION_REQUIRED: ErrorClass("validation", 2),
    ErrorCode.MISSING_PARAMETER: ErrorClass("validation", 2),
    ErrorCode.UNKNOWN_ERROR: ErrorClass("api", 1),
    ErrorCode.UNEXPECTED_ERROR: ErrorClass("api", 1),
    ErrorCode.HTTP_ERROR: ErrorClass("api", 1),
    ErrorCode.INTERNAL_ERROR: ErrorClass("api", 1),
    # Configuration
    ErrorCode.CONFIG_ERROR: ErrorClass("configuration", 5),
    ErrorCode.NOT_INITIALIZED: ErrorClass("api", 1),
    ErrorCode.INIT_ERROR: ErrorClass("api", 1),
    ErrorCode.FEATURE_NOT_ENABLED: ErrorClass("configuration", 5),
    # Jobs
    ErrorCode.QUEUE_JOB_FAILED: ErrorClass("api", 1),
    # The local wait gave up and the remote job may still be running.
    ErrorCode.QUEUE_JOB_TIMEOUT: ErrorClass("api", 4),
    ErrorCode.STORAGE_JOB_FAILED: ErrorClass("api", 1),
    # The Storage API has no cancel, so the job is still running server-side.
    ErrorCode.STORAGE_JOB_TIMEOUT: ErrorClass("api", 4),
    ErrorCode.QUERY_JOB_FAILED: ErrorClass("api", 1),
    ErrorCode.QUERY_JOB_TIMEOUT: ErrorClass("api", 1),
    # Variables
    ErrorCode.NO_VARIABLE_ROWS: ErrorClass("api", 1),
    ErrorCode.MALFORMED_VARIABLES_ROW: ErrorClass("api", 1),
    # Storage
    ErrorCode.UPLOAD_FAILED: ErrorClass("api", 1),
    ErrorCode.EXPORT_EMPTY_MANIFEST: ErrorClass("api", 1),
    ErrorCode.EXPORT_NO_FILE: ErrorClass("api", 1),
    ErrorCode.EXPORT_NO_URL: ErrorClass("api", 1),
    ErrorCode.NOT_SLICED: ErrorClass("api", 1),
    ErrorCode.FILE_NO_URL: ErrorClass("api", 1),
    # I/O
    ErrorCode.FILE_NOT_FOUND: ErrorClass("not_found", 2),
    ErrorCode.DIR_NOT_FOUND: ErrorClass("api", 1),
    ErrorCode.READ_ERROR: ErrorClass("api", 1),
    ErrorCode.WRITE_ERROR: ErrorClass("api", 1),
    ErrorCode.INPUT_ERROR: ErrorClass("api", 2),
    # Lineage
    ErrorCode.NODE_NOT_FOUND: ErrorClass("api", 1),
    # Sharing
    ErrorCode.INVALID_SHARING_TYPE: ErrorClass("api", 1),
    ErrorCode.NOT_LINKED_BUCKET: ErrorClass("api", 1),
    # KAI (AI Service)
    ErrorCode.KAI_ERROR: ErrorClass("api", 1),
    ErrorCode.KAI_NOT_ENABLED: ErrorClass("api", 1),
    # Workspace / Query
    ErrorCode.MISSING_QUERY: ErrorClass("api", 2),
    ErrorCode.WORKSPACE_NOT_FOUND: ErrorClass("api", 1),
    # Updates (CLI-51). A refusal by kbagent itself, with the same exit code 6 as
    # a policy denial.
    ErrorCode.UPDATED_ACROSS_MAJOR: ErrorClass("authorization", EXIT_PERMISSION_DENIED),
    # Only a `warnings[]` entry, never an error exit.
    ErrorCode.UPDATED_ACROSS_BREAKING: ErrorClass("api", 1),
    # A refused-by-us safety guard, not an upstream fault: nothing was sent to
    # the API, and the caller fixes it by re-issuing the request with --force.
    ErrorCode.WORKSPACE_LOAD_COPY_TOO_LARGE: ErrorClass("validation", 1),
    # Sync
    ErrorCode.PARENT_CONFIG_NOT_TRACKED: ErrorClass("api", 1),
    ErrorCode.VARIABLE_LINK_UNRESOLVED: ErrorClass("api", 1),
    ErrorCode.LINK_UNRESOLVED: ErrorClass("api", 1),
    ErrorCode.SYNC_CONFLICT: ErrorClass("conflict", 1),
    ErrorCode.SYNC_LEGACY_BOUNDARY: ErrorClass("conflict", 1),
    # Encryption
    ErrorCode.ENCRYPTION_FAILED: ErrorClass("api", 1),
    # Job / queue: local --timeout elapsed and the remote job was cancelled.
    ErrorCode.JOB_TIMEOUT_TERMINATED: ErrorClass("api", EXIT_JOB_TIMEOUT_TERMINATED),
    # Flow
    ErrorCode.SCHEDULE_DELETE_FAILED: ErrorClass("api", 1),
    ErrorCode.INVALID_FLOW_DEFINITION: ErrorClass("api", 1),
    # Data apps
    ErrorCode.DATA_APP_BUILD_FAILED: ErrorClass("api", 1),
    ErrorCode.DATA_APP_DEPLOY_TIMEOUT: ErrorClass("api", 1),
    ErrorCode.DATA_APP_INVALID_GIT: ErrorClass("api", 1),
    ErrorCode.DATA_APP_INVALID_SECRET: ErrorClass("api", 2),
    ErrorCode.DATA_APP_INVALID_REPO: ErrorClass("api", 1),
    ErrorCode.DATA_APP_REPO_VALIDATION_BLOCKING: ErrorClass("api", 1),
    # Developer Portal
    ErrorCode.DP_LOGIN_FAILED: ErrorClass("authentication", 1),
    ErrorCode.DP_MFA_REQUIRED: ErrorClass("authentication", 1),
    ErrorCode.DP_APP_NOT_FOUND: ErrorClass("not_found", 1),
    ErrorCode.DP_PUBLISH_REQUIREMENTS_MISSING: ErrorClass("validation", 1),
    ErrorCode.DP_ICON_UPLOAD_FAILED: ErrorClass("api", 1),
    # Programmatic auth / browser login. `kbagent auth login` is the remedy for
    # the session codes that exit 3.
    ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK: ErrorClass("configuration", 5),
    ErrorCode.AUTH_FLOW_TIMEOUT: ErrorClass("authentication", 4),
    ErrorCode.AUTH_FLOW_DENIED: ErrorClass("authentication", 3),
    ErrorCode.AUTH_FLOW_EXPIRED: ErrorClass("authentication", 1),
    ErrorCode.AUTH_BROWSER_UNAVAILABLE: ErrorClass("authentication", 1),
    ErrorCode.AUTH_STATE_MISMATCH: ErrorClass("authentication", 1),
    ErrorCode.SESSION_EXPIRED: ErrorClass("authentication", 3),
    ErrorCode.SESSION_NOT_FOUND: ErrorClass("authentication", 3),
    ErrorCode.AUTH_MFA_INVALID: ErrorClass("authentication", 3),
    # Billing. Today's only emitter puts PAYG_NOT_AVAILABLE in a per-project
    # `errors` list, which never passes through `formatter.error()`.
    ErrorCode.PAYG_NOT_AVAILABLE: ErrorClass("configuration", 1),
    # Merge requests
    ErrorCode.MR_NOT_READY_TO_MERGE: ErrorClass("conflict", 1),
    ErrorCode.MR_MERGE_CONFLICT: ErrorClass("conflict", 1),
}


def classify_error_code(error_code: str) -> ErrorClass:
    """The table entry of ``error_code``. A string that is no ErrorCode gets UNKNOWN_ERROR's."""
    return ERROR_CODE_TABLE.get(error_code, ERROR_CODE_TABLE[ErrorCode.UNKNOWN_ERROR])


def map_error_code_to_type(error_code: str) -> str:
    """Map a machine-readable error code to a broad error type category."""
    return classify_error_code(error_code).error_type


def exit_code_for(error_code: str) -> int:
    """The process exit code for ``error_code`` (spec rule E1)."""
    return classify_error_code(error_code).exit_code
