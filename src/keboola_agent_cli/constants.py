"""Shared constants for Keboola Agent CLI.

All magic numbers, default values, retry parameters, timeout settings,
and environment variable names are centralized here to avoid duplication
and ensure consistency across the codebase.
"""

import httpx

# --- HTTP Retry Constants ---
RETRYABLE_STATUS_CODES: set[int] = {429, 500, 502, 503, 504}
MAX_RETRIES: int = 3
BACKOFF_BASE: float = 1.0  # seconds; delays: 1s, 2s, 4s

# --- HTTP Timeout ---
DEFAULT_TIMEOUT: httpx.Timeout = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)

# --- API Error Handling ---
MAX_API_ERROR_LENGTH: int = 500

# --- Default Stack URL ---
DEFAULT_STACK_URL: str = "https://connection.keboola.com"

# --- Token Description ---
DEFAULT_TOKEN_DESCRIPTION: str = "kbagent-cli"

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

# --- Storage Write Validation ---
VALID_COLUMN_TYPES: frozenset[str] = frozenset(
    {"STRING", "INTEGER", "NUMERIC", "FLOAT", "BOOLEAN", "DATE", "TIMESTAMP"}
)

# --- File Upload Timeout ---
FILE_UPLOAD_TIMEOUT: httpx.Timeout = httpx.Timeout(
    connect=30.0, read=300.0, write=3600.0, pool=30.0
)

# --- File Download Timeout ---
FILE_DOWNLOAD_TIMEOUT: httpx.Timeout = httpx.Timeout(
    connect=30.0, read=3600.0, write=10.0, pool=30.0
)

# --- Export Job ---
EXPORT_JOB_MAX_WAIT: float = 600.0  # 10 min for table export jobs (large tables)

# --- Parallel Workers ---
MAX_PARALLEL_WORKERS_LIMIT: int = 100

# --- Config Resolution ---
ENV_CONFIG_DIR: str = "KBAGENT_CONFIG_DIR"
LOCAL_CONFIG_DIR_NAME: str = ".kbagent"

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

# --- Version Check ---
VERSION_CHECK_TIMEOUT: float = 4.0  # seconds for fetching latest version from remote
MCP_PYPI_URL: str = "https://pypi.org/pypi/keboola-mcp-server/json"
KBAGENT_GITHUB_REPO: str = "padak/keboola_agent_cli"
KBAGENT_INSTALL_SOURCE: str = "git+https://github.com/padak/keboola_agent_cli"

# --- Auto-Update ---
ENV_AUTO_UPDATE: str = "KBAGENT_AUTO_UPDATE"
ENV_SKIP_UPDATE: str = "KBAGENT_SKIP_UPDATE"
AUTO_UPDATE_CHECK_INTERVAL: int = 3600  # 1 hour TTL for version cache
VERSION_CACHE_FILENAME: str = "version_cache.json"

# --- AI Service ---
AI_SERVICE_TIMEOUT: httpx.Timeout = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
SECRET_PLACEHOLDER: str = "<YOUR_SECRET>"

# --- Permission Exit Code ---
EXIT_PERMISSION_DENIED: int = 6

# --- Domain Validation Constants ---
VALID_COMPONENT_TYPES: list[str] = ["extractor", "writer", "transformation", "application"]
VALID_STATUSES: list[str] = ["processing", "terminated", "cancelled", "success", "error"]

# --- Query Service ---
QUERY_JOB_POLL_INTERVAL: float = 1.0  # seconds between polls for query job status
QUERY_JOB_MAX_WAIT: float = 120.0  # max seconds to wait for a query job

# --- Workspace Defaults ---
DEFAULT_WORKSPACE_BACKEND: str = "snowflake"

# --- Sync / Git Workflow ---
KEBOOLA_DIR_NAME: str = ".keboola"
MANIFEST_FILENAME: str = "manifest.json"
BRANCH_MAPPING_FILENAME: str = "branch-mapping.json"
CONFIG_FILENAME: str = "_config.yml"
MANIFEST_VERSION: int = 2
DEFAULT_NAMING_BRANCH: str = "{branch_name}"
DEFAULT_NAMING_CONFIG: str = "{component_type}/{component_id}/{config_name}"
DEFAULT_NAMING_CONFIG_ROW: str = "rows/{config_row_name}"
DEFAULT_NAMING_SCHEDULER: str = "schedules/{config_name}"
DEFAULT_NAMING_SHARED_CODE: str = "_shared/{target_component_id}"
DEFAULT_NAMING_SHARED_CODE_ROW: str = "codes/{config_row_name}"
DEFAULT_NAMING_VARIABLES: str = "variables"
DEFAULT_NAMING_VARIABLES_VALUES: str = "values/{config_row_name}"
DEFAULT_NAMING_DATA_APP: str = "app/{component_id}/{config_name}"
# Aliases used by sync subsystem
CONFIG_YML_VERSION: int = MANIFEST_VERSION
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
