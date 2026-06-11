"""Shared constants for Keboola Agent CLI.

All magic numbers, default values, retry parameters, timeout settings,
and environment variable names are centralized here to avoid duplication
and ensure consistency across the codebase.
"""

import httpx

# --- Application identity ---
# Distribution/package name; single source for importlib.metadata lookup and
# the User-Agent product token that signs every Keboola API call.
APP_NAME: str = "keboola-agent-cli"

# --- Sentinel for missing metadata keys ---
# Distinguishes "key absent" from "value is None/null" in branch metadata lookups.
METADATA_NOT_FOUND = object()

# --- HTTP Retry Constants ---
RETRYABLE_STATUS_CODES: set[int] = {429, 500, 502, 503, 504}
MAX_RETRIES: int = 3
BACKOFF_BASE: float = 1.0  # seconds; delays: 1s, 2s, 4s

# --- HTTP Timeout ---
DEFAULT_TIMEOUT: httpx.Timeout = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)

# --- API Error Handling ---
MAX_API_ERROR_LENGTH: int = 500

# --- Developer Portal MFA ---
# Challenge type sent on the second `/auth/login` step (after the first call
# returns a `session` token). The apiary spec documents `SOFTWARE_TOKEN_MFA`
# (TOTP authenticator app) as the default and `SMS_MFA` as the only other
# member, but in practice the server 404s when the field is omitted, so we
# send it explicitly. If apps-api ever adds a third member (e.g. EMAIL_OTP),
# wire it in here and add a per-identity `mfa_challenge` field.
DP_MFA_CHALLENGE_TYPE: str = "SOFTWARE_TOKEN_MFA"

# --- UNEXPECTED_ERROR truncation ---
# Unhandled ``Exception`` messages surfaced to per-project error envelopes are
# truncated to this many characters before being returned. Exceptions can
# otherwise embed URLs with query params, response-buffer fragments, or, with
# ``--with-state``, OAuth refresh tokens from the runtime state dict. CWE-209.
UNEXPECTED_ERROR_MAX_MESSAGE_LEN: int = 256

# --- Default Stack URL ---
DEFAULT_STACK_URL: str = "https://connection.keboola.com"

# --- Token Description ---
DEFAULT_TOKEN_DESCRIPTION: str = "kbagent-cli"

# --- Project Member Roles ---
# Allowed values for project membership / invitation `role` field. Lifted from
# the Manage API's own validation error: `Role "X" is not valid. Allowed roles
# are: admin, guest, readOnly, share`. Verified empirically 2026-05-01 against
# connection.us-east4.gcp.keboola.com. If the API ever extends the list, the
# fix is to extend this tuple -- the engine already returns the new options in
# its validation error message.
PROJECT_ROLES: tuple[str, ...] = ("admin", "guest", "readOnly", "share")

# --- Bulk Invite Defaults ---
DEFAULT_INVITE_WORKERS: int = 8

# --- Job Limits ---
DEFAULT_JOB_LIMIT: int = 50
DEFAULT_JOBS_PER_CONFIG: int = 5
# Max groups: constrained by API rule jobsPerGroup * limit <= 500
DEFAULT_GROUPED_JOBS_LIMIT: int = 100
MAX_JOB_LIMIT: int = 500

# --- Retry-After Header ---
MAX_RETRY_AFTER_SECONDS: int = 60

# --- MCP Timeouts ---
DEFAULT_MCP_TOOL_TIMEOUT: int = 60
DEFAULT_MCP_INIT_TIMEOUT: int = 30

# --- MCP Concurrency ---
# 0 = unlimited (all projects run in parallel); set KBAGENT_MCP_MAX_SESSIONS to throttle
DEFAULT_MCP_MAX_SESSIONS: int = 0

# --- MCP HTTP Transport ---
# Transport mode: "http" (persistent server) or "stdio" (subprocess per call)
ENV_MCP_TRANSPORT: str = "KBAGENT_MCP_TRANSPORT"
DEFAULT_MCP_TRANSPORT: str = "stdio"
# Timeout for the persistent MCP server to start and be healthy
MCP_SERVER_STARTUP_TIMEOUT: float = 15.0
# Timeout for health check requests to persistent MCP server
MCP_SERVER_HEALTH_TIMEOUT: float = 2.0

# --- Storage Job Polling ---
STORAGE_JOB_POLL_INTERVAL: float = 1.0  # seconds between polls
STORAGE_JOB_MAX_WAIT: float = 60.0  # max seconds to wait for a storage job
IMPORT_JOB_MAX_WAIT: float = 600.0  # 10 min for table import jobs (large files)

# --- Queue Job Polling ---
# Piecewise curve matching FIIA's existing Queue API polling contract
# (same cadence as the official keboola-as-code Go CLI): fast initial polls
# to catch short jobs, then relax so multi-hour orchestrations don't spam
# the API. Each tuple is (interval_seconds, max_polls_at_this_interval);
# count=0 means "continue at this interval indefinitely" (only valid on the
# last segment). Total first-phase time: 2s * 30 + 5s * 48 = 300s = 5 min,
# after which we settle at 15s forever.
JOB_POLL_CURVE: tuple[tuple[float, int], ...] = (
    (2.0, 30),
    (5.0, 48),
    (15.0, 0),
)
VALID_POLL_STRATEGIES: frozenset[str] = frozenset({"exponential", "fixed"})
DEFAULT_POLL_STRATEGY: str = "exponential"
# Queue API "mode" body field for job creation. "run" is the normal execution
# path (writes to mapped output tables); "debug" runs the component the same
# way but redirects the output to a Storage File tagged "debug-" + the job id
# instead of into destination buckets. Useful for dry-runs and reproducing
# failures without touching production tables.
VALID_JOB_MODES: frozenset[str] = frozenset({"run", "debug"})
DEFAULT_JOB_MODE: str = "run"
# Default log-tail length surfaced on FAILED/WARNING/TERMINATED jobs.
DEFAULT_LOG_TAIL_LINES: int = 200
# Upper bound to prevent accidentally pulling tens of thousands of events
# from a long-running job.
MAX_LOG_TAIL_LINES: int = 5000
# Seconds to wait after issuing kill_job() during timeout-cancellation for
# the job to transition to a terminal state before we return.
JOB_TERMINATE_GRACE_SECONDS: float = 10.0
# Poll cadence while waiting inside the terminate grace window; capped so we
# never overshoot the deadline by more than one interval on latency-sensitive
# callers (see _terminate_and_wait in services/job_service.py).
JOB_TERMINATE_POLL_INTERVAL: float = 1.0

# --- File Upload Timeout ---
FILE_UPLOAD_TIMEOUT: httpx.Timeout = httpx.Timeout(
    connect=30.0, read=300.0, write=3600.0, pool=30.0
)

# --- File Download Timeout ---
FILE_DOWNLOAD_TIMEOUT: httpx.Timeout = httpx.Timeout(
    connect=30.0, read=3600.0, write=10.0, pool=30.0
)

# --- File Download Streaming ---
# Chunk size for streamed downloads. Bounded buffer keeps peak RSS small even
# for multi-GB tables (see GitHub issue #187: 200MB parquet slices OOM'd on 2GB RAM
# hosts when loaded whole-body via response.content).
FILE_DOWNLOAD_CHUNK_SIZE: int = 1024 * 1024  # 1 MiB

# --- Export Job ---
EXPORT_JOB_MAX_WAIT: float = 600.0  # 10 min for table export jobs (large tables)

# --- Parallel Workers ---
MAX_PARALLEL_WORKERS_LIMIT: int = 100

# --- Config Resolution ---
ENV_CONFIG_DIR: str = "KBAGENT_CONFIG_DIR"
LOCAL_CONFIG_DIR_NAME: str = ".kbagent"

# --- Project Pin ---
# Overrides the persisted `default_project` pin for a single invocation/session.
ENV_KBAGENT_PROJECT: str = "KBAGENT_PROJECT"

# --- Headless / env-only project (issue #359) ---
# Opt-in flag that makes ConfigStore synthesize an in-memory project from
# KBC_TOKEN + KBC_STORAGE_API_URL, so a daemon / container / CI can run kbagent
# (CLI or `serve`) with no `kbagent project add` and no config.json on disk.
# Explicit opt-in (not mere presence of KBC_TOKEN) avoids a phantom project
# surprising a dev who exported KBC_TOKEN only for `kbagent project add`.
ENV_PROJECT_FROM_ENV: str = "KBAGENT_PROJECT_FROM_ENV"
# Reserved alias for the synthesized project. Double-underscore marks it as a
# synthetic, never-persisted entry that cannot collide with a user alias.
ENV_PROJECT_ALIAS: str = "__env__"

# --- Environment Variable Names ---
ENV_MAX_PARALLEL_WORKERS: str = "KBAGENT_MAX_PARALLEL_WORKERS"
ENV_KBC_TOKEN: str = "KBC_TOKEN"
ENV_KBC_STORAGE_API_URL: str = "KBC_STORAGE_API_URL"
ENV_KBC_MANAGE_API_TOKEN: str = "KBC_MANAGE_API_TOKEN"
ENV_KBC_MASTER_TOKEN: str = "KBC_MASTER_TOKEN"
ENV_MCP_TOOL_TIMEOUT: str = "KBAGENT_MCP_TOOL_TIMEOUT"
ENV_MCP_INIT_TIMEOUT: str = "KBAGENT_MCP_INIT_TIMEOUT"
ENV_MCP_MAX_SESSIONS: str = "KBAGENT_MCP_MAX_SESSIONS"
ENV_CONVERSATION_ID: str = "KBAGENT_CONVERSATION_ID"

# --- Serve subprocess context (since v0.7.x) ---
# Injected by `kbagent serve` into scheduled-agent subprocess env so AI CLIs
# (claude / codex / gemini) and plain `kbagent` invocations can talk to the
# live HTTP API instead of reading possibly-stale local config. Pair them:
# ENV_KBAGENT_SERVE_URL points at the FastAPI bind URL and ENV_KBAGENT_SERVE_TOKEN
# is the bearer token printed at serve startup. Used by `kbagent http <verb>`.
ENV_KBAGENT_SERVE_URL: str = "KBAGENT_SERVE_URL"
ENV_KBAGENT_SERVE_TOKEN: str = "KBAGENT_SERVE_TOKEN"

# --- Upstream-chain context (set when a task is triggered as a downstream) ---
# Populated only on runs spawned by another task's ``trigger`` field; absent
# on cron-driven or manually-invoked runs. AI agents read these to fetch the
# upstream output via `kbagent http get /agents/<task>/runs/<run>`.
ENV_KBAGENT_UPSTREAM_TASK_ID: str = "KBAGENT_UPSTREAM_TASK_ID"
ENV_KBAGENT_UPSTREAM_RUN_ID: str = "KBAGENT_UPSTREAM_RUN_ID"
ENV_KBAGENT_UPSTREAM_STATUS: str = "KBAGENT_UPSTREAM_STATUS"

# Default timeout for `kbagent http` requests. AI agents poll endpoints
# during multi-step tasks; long enough for slow Storage table listings,
# short enough to fail fast on a dead serve.
HTTP_DEFAULT_TIMEOUT: float = 60.0

# --- AI helper subprocess timeouts (seconds) ---
# Each of the three stateless AI helpers spawns a local claude / codex /
# gemini CLI via stream_ai_agent_events; these caps protect against a stuck
# CLI camping on the SSE connection. Centralised here so they stay
# governable from one place instead of three router files.
#
# - Prompt helper:  rewrite a draft prompt for a scheduled agent. Short
#   single-turn task, no tool calls expected -> 180s.
# - SQL helper:     write workspace SQL. May round-trip through
#   `kbagent storage bucket-detail` + INFORMATION_SCHEMA queries -> 180s.
# - Chat helper:    generic Local AI co-pilot. Can involve several tool
#   invocations + a longer summary -> 300s, matching the longest
#   credible single-turn budget without leaving stuck connections open.
AI_PROMPT_HELPER_TIMEOUT: float = 180.0
AI_SQL_HELPER_TIMEOUT: float = 180.0
AI_CHAT_HELPER_TIMEOUT: float = 300.0

# --- Version Check ---
VERSION_CHECK_TIMEOUT: float = 4.0  # seconds for fetching latest version from remote
MCP_PYPI_URL: str = "https://pypi.org/pypi/keboola-mcp-server/json"
KBAGENT_GITHUB_REPO: str = "keboola/cli"
KBAGENT_INSTALL_SOURCE: str = "git+https://github.com/keboola/cli"

# --- MCP self-upgrade (since v0.30.1) ---
# Subprocess timeout for the `keboola_mcp_server --version` probe and the
# `uv tool list` install-method probe. These are local subprocess calls,
# so 5s leaves plenty of headroom for cold-start CPython without slowing
# kbagent startup observably.
MCP_PROBE_TIMEOUT: float = 5.0
# Subprocess timeout for the actual upgrade command (`uv tool upgrade` /
# `pip install -U` / `uvx --refresh`). Network bound; 180s tolerates a
# slow PyPI link plus the worst-case dependency-resolution cost.
MCP_UPGRADE_TIMEOUT: float = 180.0
# Pre-release opt-in flags for installing/upgrading keboola-mcp-server (#324).
#
# keboola-mcp-server >= 1.55.0 declares a pre-release-only transitive
# dependency: ``toon-format~=0.9.0b1``. On PyPI ``toon-format`` ships exactly
# two releases -- ``0.1.0`` (stable) and ``0.9.0b1`` (pre-release) -- so the
# ``~=0.9.0b1`` constraint can ONLY be satisfied by the pre-release.
#
# uv refuses pre-releases by default. Crucially, ``--prerelease=if-necessary``
# does NOT help here: a *stable* toon-format (0.1.0) exists, so uv decides a
# pre-release is "not necessary", then fails because 0.1.0 violates the pin.
# Only ``--prerelease=allow`` resolves it (verified empirically, uv 0.10.x).
# Without the flag ``uv tool upgrade``/``install`` silently backtracks to the
# last MCP release predating the pin (v1.32.0) and exits 0 -- pinning the
# fleet to a stale server while reporting a newer version is "available".
#
# Scoped to the MCP environment only; never affects the kbagent self-update
# channel (which stays stable-only unless --beta).
MCP_UV_PRERELEASE_FLAG: str = "--prerelease=allow"
MCP_PIP_PRERELEASE_FLAG: str = "--pre"

# --- Auto-Update ---
ENV_AUTO_UPDATE: str = "KBAGENT_AUTO_UPDATE"
ENV_SKIP_UPDATE: str = "KBAGENT_SKIP_UPDATE"
AUTO_UPDATE_CHECK_INTERVAL: int = 3600  # 1 hour TTL for version cache
VERSION_CACHE_FILENAME: str = "version_cache.json"
# Self-update subprocess timeout. Previously a hardcoded 120s in two places,
# which falsely tripped on WSL where the git+ source build (npm/React SPA) runs
# for minutes (issue #353). The prebuilt-wheel install is a seconds download, so
# 300s is ample; KBAGENT_UPDATE_TIMEOUT raises it for the slow git+ fallback.
UPDATE_TIMEOUT_SECONDS: int = 300
ENV_UPDATE_TIMEOUT: str = "KBAGENT_UPDATE_TIMEOUT"

# --- AI Service ---
AI_SERVICE_TIMEOUT: httpx.Timeout = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)

# --- Data Streams (Stream API) ---
# The Stream control-plane API lives on a separate host derived from the
# Storage URL by replacing 'connection.' with 'stream.' (same scheme as
# 'ai.'/'queue.'), authenticated with the Storage API token (X-StorageApi-Token).
# The OTLP *ingestion* endpoint (stream-in.<region>/otlp/...) is NOT derived --
# it is returned by the API in the source's `otlp.url` field.
STREAM_API_TIMEOUT: httpx.Timeout = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
# Branch ref used by the Stream API path /v1/branches/<branch>/...; "default"
# targets the project's default (production) branch.
STREAM_DEFAULT_BRANCH: str = "default"
# Source/delete operations return an async Task; poll GET /v1/tasks/<id> until
# `isFinished`. Interval between polls and the overall ceiling.
STREAM_TASK_POLL_INTERVAL: float = 1.0  # seconds between task polls
STREAM_TASK_TIMEOUT: float = 60.0  # max seconds to wait for a Stream task
# OTLP/HTTP per-signal sub-paths appended to the source's base endpoint, and the
# wire protocol every OTLP source speaks. Surfaced by `kbagent stream detail`.
OTLP_SIGNAL_PATHS: tuple[str, ...] = ("v1/logs", "v1/traces", "v1/metrics")
OTLP_PROTOCOL: str = "http/protobuf"
# Signals (and their destination table names) auto-provisioned for an OTLP source.
# Creating a source via the raw Stream API does NOT create sinks, so kbagent
# provisions one table sink per signal (matching the Keboola UI) so data actually
# lands. Bucket = OTLP_BUCKET_PREFIX + sourceId; table = the signal name.
OTLP_SINK_SIGNALS: tuple[str, ...] = ("logs", "metrics", "traces")
OTLP_BUCKET_PREFIX: str = "in.c-otlp-"
# Universal, signal-agnostic sink mapping: an auto id, the ingest datetime, and a
# `body` column that captures the full flattened OTLP record as JSON. Users can
# refine per-signal column mappings in the Keboola UI afterwards.
OTLP_SINK_COLUMNS: tuple[dict[str, str], ...] = (
    {"type": "uuid", "name": "id"},
    {"type": "datetime", "name": "datetime"},
    {"type": "body", "name": "body"},
)

# --- Project Feature Flags ---
# `storage-branches` enables the modern dev-branch storage isolation:
# transformation runner / output-mapping consult bucket metadata
# (KBC.createdBy.branch.id) and use the /v2/storage/branch/<id>/* endpoints.
# Projects WITHOUT this feature still accept POST /v2/storage/branch/<id>/buckets,
# but the runner ignores those buckets and creates parallel `out.c-<branch_id>-*`
# buckets in the default branch (legacy "fake-branch" path). kbagent's
# branch-aware writes surface a `legacy_branch_storage: true` flag on such
# projects so callers know the materialized bucket will be unused by the runner.
# See plugins/kbagent/skills/kbagent/references/storage-types-workflow.md.
STORAGE_BRANCHES_FEATURE: str = "storage-branches"

# --- Global Search ---
# Feature flag that gates the Storage API ``GET /v2/storage/global-search``
# endpoint used by ``kbagent search`` (textual mode). Projects without this
# flag receive a 404; ``SearchService`` checks the flag pre-flight and
# returns a descriptive per-project error rather than letting the raw 404
# bubble up.
GLOBAL_SEARCH_FEATURE: str = "global-search"

# --- OAuth ---
# Host of the Keboola-hosted OAuth wizard used by ``kbagent config oauth-url``.
# Constant across all stacks (EU/US/AWS/GCP/Azure); the per-stack difference
# is reflected in the ``sapiUrl`` query parameter, not the wizard host.
OAUTH_HOST: str = "external.keboola.com"
OAUTH_PATH: str = "/oauth/index.html"

# --- Kai (Keboola AI Assistant) ---
KAI_FEATURE_FLAG: str = "agent-chat"
KAI_REQUEST_TIMEOUT: float = 300.0  # 5 min for non-streaming requests
KAI_STREAM_TIMEOUT: float = 600.0  # 10 min for SSE streaming responses
SECRET_PLACEHOLDER: str = "<YOUR_SECRET>"

# --- Changelog rendering ---
# `kbagent changelog` shows a one-line summary per version by default (--full
# expands). A summary is the note's first sentence, capped at this many chars
# (cut on a word boundary) so a verbose release note collapses to a scannable
# headline instead of a wall of text.
CHANGELOG_HEADLINE_MAX_CHARS: int = 160

# --- Job Run ---
DEFAULT_JOB_RUN_TIMEOUT: float = 300.0  # 5 min default for --wait polling

# --- Job Terminate ---
# States where POST /jobs/{id}/kill returns HTTP 200; any other state yields 400.
KILLABLE_JOB_STATUSES: frozenset[str] = frozenset({"created", "waiting", "processing"})

# --- Workspace Query Service compatibility (since v0.42.0, closes #304) ---
# Storage API workspace endpoint exposes `connection.loginType`. The Query
# Service (POST /v2/storage/branch/{ID}/workspaces/{WS}/query) accepts only a
# subset of these and rejects the rest with HTTP 400 and
# `code: storage.executeQuery.notSupportedLoginType`. Before #304 the only
# way to learn whether a workspace was Query-Service-compatible was to fire a
# query and read the failure -- the issue cost ~30 min of trial-and-error
# per data-app onboarding.
#
# Conservative WHITELIST semantics (false negatives over false positives):
#   present  -> qs_compatible=True (confirmed working in production)
#   absent   -> qs_compatible=False; hint to try anyway because the policy
#               varies per stack (snowflake-legacy-service worked on
#               connection.keboola.com but failed on the GCP us-east4 stack
#               in the original issue report)
#
# Verified 2026-05-18 against project 901 on connection.keboola.com:
#   snowflake-service-keypair: PASS
#   snowflake-person-sso:      PASS
#   snowflake-person-keypair:  PASS (required for new Snowflake sandboxes)
#   snowflake-legacy-service:  PASS here, FAIL on GCP us-east4 (issue #304)
#   default (legacy 2016 ws):  FAIL ('JWT token is invalid')
#
# Extend ONLY after empirical confirmation across at least one non-AWS stack.
#
# This whitelist is SNOWFLAKE-SCOPED. BigQuery compatibility lives in its own
# set below because the `default` loginType means opposite things per backend
# (see BIGQUERY_WORKSPACE_LOGIN_TYPE). Compatibility is therefore keyed by
# (backend, loginType) -- see `_classify_qs_compatibility`.
SNOWFLAKE_WORKSPACE_LOGIN_TYPE: str = "snowflake-person-keypair"
QUERY_SERVICE_COMPATIBLE_LOGIN_TYPES: frozenset[str] = frozenset(
    {
        "snowflake-service-keypair",
        "snowflake-person-sso",
        SNOWFLAKE_WORKSPACE_LOGIN_TYPE,
    }
)

# --- BigQuery Query Service compatibility (since v0.58.0) ---
# BigQuery workspaces carry a single `default` loginType -- the sandbox API does
# not expose Snowflake-style variants for BigQuery. The Query Service accepts it:
# verified 2026-06-04 against project 9621 on connection.keboola.com, where a
# `SELECT` against a read-only `default` BigQuery workspace returns rows.
#
# CRITICAL: `default` is on the BigQuery whitelist but deliberately OFF the
# Snowflake one above. Snowflake ALSO mints a `default` loginType (legacy 2016
# workspaces) which the Query Service REJECTS ('JWT token is invalid'). Keying
# compatibility on loginType alone would wrongly green-light those legacy
# Snowflake workspaces, so `_classify_qs_compatibility` dispatches on backend.
BIGQUERY_WORKSPACE_LOGIN_TYPE: str = "default"
QUERY_SERVICE_COMPATIBLE_LOGIN_TYPES_BIGQUERY: frozenset[str] = frozenset(
    {BIGQUERY_WORKSPACE_LOGIN_TYPE}
)

# --- Permission Exit Code ---
EXIT_PERMISSION_DENIED: int = 6
# --- Job-timeout Exit Code ---
# Distinct from the general "1" exit code so scripts can tell
# "local --timeout elapsed and we cancelled the remote job" apart
# from "job failed on its own". The retryable-with-longer-timeout
# QUEUE_JOB_TIMEOUT case (kill itself failed) stays at exit 4.
EXIT_JOB_TIMEOUT_TERMINATED: int = 7

# --- Domain Validation Constants ---
VALID_COMPONENT_TYPES: list[str] = ["extractor", "writer", "transformation", "application"]
VALID_STATUSES: list[str] = ["processing", "terminated", "cancelled", "success", "error"]

# --- Query Service ---
QUERY_JOB_POLL_INTERVAL: float = 1.0  # seconds between polls for query job status
QUERY_JOB_MAX_WAIT: float = 120.0  # max seconds to wait for a query job
# Fast inline result path (GET .../results) -- reads the already-computed result
# set as JSON instead of materializing a CSV file via the warehouse UNLOAD path
# (GET .../export). The default path fetches at most QUERY_RESULTS_DEFAULT_LIMIT
# rows, accumulated in pages of QUERY_RESULTS_PAGE_SIZE and trimmed to the limit.
# The endpoint enforces 100 <= pageSize <= 100000, so QUERY_RESULTS_PAGE_SIZE is a
# fixed valid page size -- it is NOT derived from --limit (a small limit would 400).
# Two DISTINCT concepts that happen to share the value 500:
#   DEFAULT_LIMIT = user-facing row cap (the --limit default; freely tunable).
#   PAGE_SIZE     = API wire constraint -- rows per /results request, which the
#                   endpoint requires to be within 100..100000. Independent of
#                   --limit; the service pages by PAGE_SIZE and trims to --limit.
QUERY_RESULTS_DEFAULT_LIMIT: int = 500  # default --limit for `workspace query` fast path
QUERY_RESULTS_PAGE_SIZE: int = 500  # rows per /results page (API requires 100..100000)

# --- Workspace Defaults ---
DEFAULT_WORKSPACE_BACKEND: str = "snowflake"

# --- Sync / Git Workflow ---
KEBOOLA_DIR_NAME: str = ".keboola"
MANIFEST_FILENAME: str = "manifest.json"
BRANCH_MAPPING_FILENAME: str = "branch-mapping.json"
CONFIG_FILENAME: str = "_config.yml"
MANIFEST_VERSION: int = 3
DEFAULT_NAMING_BRANCH: str = "{branch_name}"
DEFAULT_NAMING_CONFIG: str = "{component_type}/{component_id}/{config_name}"
DEFAULT_NAMING_CONFIG_ROW: str = "rows/{config_row_name}"
DEFAULT_NAMING_SCHEDULER: str = "schedules/{config_name}"
DEFAULT_NAMING_SHARED_CODE: str = "_shared/{target_component_id}"
DEFAULT_NAMING_SHARED_CODE_ROW: str = "codes/{config_row_name}"
DEFAULT_NAMING_VARIABLES: str = "variables"
DEFAULT_NAMING_VARIABLES_VALUES: str = "values/{config_row_name}"
DEFAULT_NAMING_DATA_APP: str = "app/{component_id}/{config_name}"
# _config.yml file-format version is independent of the manifest schema version.
# Manifest v3 introduces ManifestConfigRow.metadata (row-level pull hashes) but does
# not change the on-disk YAML shape, so CONFIG_YML_VERSION stays at 2.
CONFIG_YML_VERSION: int = 2
SANITIZE_NAME_MAX_LENGTH: int = 100

# --- Sync Pull: Storage & Jobs ---
JOBS_FILENAME: str = "_jobs.jsonl"
STORAGE_DIR_NAME: str = "storage"
STORAGE_BUCKETS_FILENAME: str = "buckets.json"
STORAGE_SAMPLES_DIR_NAME: str = "samples"
DEFAULT_SAMPLE_LIMIT: int = 100
DEFAULT_MAX_SAMPLES: int = 50
ENCRYPTED_COLUMN_PREFIX: str = "#"
ENCRYPTED_COLUMN_MASK: str = "***ENCRYPTED***"

# --- Ignored Components ---
# Components that are always excluded from sync operations (pull/push/diff).
# These are managed through separate APIs and have volatile internal state.
ALWAYS_IGNORED_COMPONENTS: frozenset[str] = frozenset(
    {
        "keboola.sandboxes",  # Workspaces API; parameters.id is volatile
    }
)

# --- Diff Engine ---
DIFF_MAX_DEPTH: int = 3  # max nesting depth for deep_diff detail output
DIFF_MAX_LINES: int = 20  # max number of diff detail lines per config change
ENCRYPTED_PLACEHOLDER: str = "<ENCRYPTED>"  # placeholder for encrypted values during comparison
