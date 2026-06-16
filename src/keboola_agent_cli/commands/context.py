"""Context command - provides compact usage reference for AI agents.

Outputs a curated text block that any AI agent (Claude, Codex, Gemini, etc.)
can consume to understand how to use kbagent effectively.
For detailed workflows, install the kbagent Claude Code plugin or use
`kbagent <command> --help`.
"""

import typer

from .. import __version__
from ._helpers import get_formatter

AGENT_CONTEXT = f"""\
# kbagent - Keboola Agent CLI v{__version__}

## What is kbagent?

AI-friendly CLI for managing Keboola projects. Connect to multiple projects
across stacks, browse configs/jobs/lineage, sync configs as local files,
create workspaces for SQL debugging, and manage dev branches -- all with
structured JSON output for programmatic consumption.

## IMPORTANT: Set Conversation ID

Before running any kbagent commands, set KBAGENT_CONVERSATION_ID to a unique
identifier for the current conversation/session. This is REQUIRED for platform
observability -- all API requests will include the X-Conversation-ID header.

  export KBAGENT_CONVERSATION_ID="<unique-conversation-id>"

## Quick Start

  # Add a single project
  kbagent --json project add --project my-project --url https://connection.keboola.com --token YOUR_TOKEN

  # Or bulk-onboard all projects from an organization
  KBC_MANAGE_API_TOKEN=xxx kbagent --allow-env-manage-token --json org setup --org-id 123 --url https://connection.keboola.com --yes

  # Explore
  kbagent --json project list
  kbagent --json config list

## Global Flags

  --json / -j       JSON output (always use for programmatic parsing)
  --verbose / -v    Verbose output
  --no-color        Disable colors (auto-disabled in non-TTY)
  --config-dir      Override config directory path
  --deny-writes     Session-only firewall: block the WIDE NET -- every write, destructive, AND admin op (project add/remove/edit, org setup, all storage mutations)
  --deny-destructive  Session-only firewall: NARROW -- block only data-destructive ops in Keboola (delete-table/bucket/column, terminate-job, branch delete). Admin ops (project remove, org setup) stay allowed -- use --deny-writes for those

## All Commands

Use `kbagent <command> --help` for full flag details and examples.

### Project Management

  kbagent project add --project NAME --url URL --token TOKEN
    Add a new project connection. Token verified against API.

  kbagent project list
    List all connected projects (tokens always masked).

  kbagent project remove --project NAME
    Remove a project connection.

  kbagent project edit --project NAME [--url URL] [--token TOKEN] [--new-alias NEW]
    Edit project connection. Re-verifies token if changed. --new-alias renames
    the alias and cascades the rename through config.json and the nested sync
    directory at <cwd>/<old-alias>/. Lineage cache embeds the alias in FQNs
    and is NOT auto-updated; rebuild via `kbagent lineage build` after rename.

  kbagent project status [--project NAME]
    Test connectivity. Shows OK/ERROR with response time.

  kbagent project refresh --project ALIAS [--dry-run] [--force] [--yes] [--token-description ...] [--token-expires-in N]
  kbagent project refresh --all [--dry-run] [--force] [--yes] [--token-description ...] [--token-expires-in N]
    Refresh project tokens via Manage API. --all refreshes all projects. --force replaces non-expiring tokens.

  kbagent project description-get --project NAME
    Read the Keboola dashboard project description (markdown). Backed by
    the KBC.projectDescription metadata key on the default branch. Returns
    an empty string if no description has been set.

  kbagent project description-set --project NAME [--text STR | --file PATH | --stdin]
    Set the dashboard project description. Pass exactly one of --text,
    --file, or --stdin. Writes KBC.projectDescription to the default branch.

  kbagent project use ALIAS
    Pin ALIAS as the default project. Persists to config.json.
    Env var KBAGENT_PROJECT=ALIAS overrides the pin for a single shell/session;
    an explicit --project flag overrides both.

  kbagent project current
    Print the effective default project and its source (env / pin / none).
    Resolution order for single-project operations: --project > KBAGENT_PROJECT > pin.

  kbagent project info --project NAME
    Return full project details: ID, name, stack URL, default backend, enabled features,
    quota limits, and usage metrics. Useful for auditing project capabilities.

### Project Members & Invitations (since v0.29.0)

  Requires KBC_MANAGE_API_TOKEN (Manage API auth). Allowed roles: admin, guest, readOnly, share.

  kbagent project invite --project ALIAS --email EMAIL --role ROLE [--reason TEXT] [--dry-run]
    Send an invitation email. Re-inviting an existing invitee or member is a no-op
    (HTTP 400 from the Manage API; the service returns status="noop" with note
    "already_invited" / "already_member").

  kbagent project invite --from-csv FILE [--default-role ROLE] [--workers N] [--dry-run]
    Bulk invite. CSV must have a header row with columns: email, project (alias or
    numeric ID), role (optional if --default-role is given), reason (optional).
    Parallelised with ThreadPoolExecutor (default 8 workers). Per-row results in
    `rows[]` with status=ok|noop|failed; `failed_rows` ordering is not deterministic.

  kbagent project member-list --project ALIAS [--include-pending]
    List active project members. --include-pending also fetches pending invitations.

  kbagent project invitation-list --project ALIAS
    List pending (unaccepted) invitations only.

  kbagent project invitation-cancel --project ALIAS --email EMAIL [--invitation-id ID] [--yes]
    Cancel a pending invitation. Without --invitation-id, the service resolves the
    ID by listing pending invitations and matching --email (case-insensitive).

  kbagent project member-remove --project ALIAS --email EMAIL [--yes]
    Remove an active member (destructive). Service resolves --email to user_id.

  kbagent project member-set-role --project ALIAS --email EMAIL --role ROLE
    Change an existing member's role via PATCH /manage/projects/{{id}}/users/{{userId}}.

### Component Discovery

  kbagent component list [--project NAME] [--type TYPE] [--query "search"]
    List or AI-search available components. --type: extractor, writer, transformation, application.

  kbagent component detail --component-id ID [--project NAME]
    Show component docs, config schema, and examples count.

### Configuration Browsing

  kbagent config list [--project NAME] [--component-type TYPE] [--component-id ID] [--branch ID] [--include-rows]
    List configs from one/many/all projects. --project repeatable. Branch-aware.
    --include-rows extends each row with full configuration + rows bodies
    (significantly larger payload -- use only when bodies are needed).

  kbagent config detail --project NAME [--project NAME ...] --component-id ID [--config-id ID] [--branch ID] [--with-state]
    Full config detail. TWO MODES:
      - Single (--config-id given): returns the full config dict (shape unchanged).
      - Bulk (--config-id omitted): returns {{"configs": [...], "errors": [...]}}
        with every config of --component-id. --project repeatable for multi-project
        fan-out (one API call per project, not per config).
    --with-state attaches the runtime state dict. Single: dedicated get_config_state
    call. Bulk: include=state ridesalong (no N+1, still one call per project).
    --branch requires exactly one --project (branch IDs are per-project).
    Sandbox annotation (since v0.42.0, single-config mode only):
    --component-id keboola.sandboxes adds a sandbox_annotation block with
    sandbox_service_id (the misleading parameters.id) and storage_workspace_id
    (the actual Storage workspace ID -- resolved via workspace list lookup).
    Use storage_workspace_id with `kbagent workspace detail --workspace-id ID`,
    NOT parameters.id (which 404s).

  kbagent config update --project NAME --component-id ID --config-id ID [--name N] [--description D] [--configuration JSON|@file|-] [--configuration-file PATH] [--set PATH=VALUE ...] [--merge] [--dry-run] [--branch ID] [--allow-plaintext-on-encrypt-failure]
    Update config metadata and/or configuration content. --set targets a
    nested key (e.g. parameters.db.host=new-host). --merge deep-merges into
    existing config (preserves sibling keys). --dry-run previews changes.
    Paths are always relative to the configuration root. #-prefixed secrets
    auto-encrypt via the Encryption API before write (fail-closed; since
    0.54.0); --allow-plaintext-on-encrypt-failure overrides. --dry-run keeps
    plaintext in the diff. Note --set '#k=v' sets a top-level key; for a
    nested secret use --set 'parameters.#k=v'.
    Auto-normalize (0.28.0+; #245): parameters.blocks[].codes[].script
    strings are silently rewritten to arrays before pushing to Storage --
    SQL transformations split on statement boundaries (state machine
    respects 'string' / "ident" / $$..$$ / -- / # / // / /* */); Python /
    R / kds-team.app-custom-python wrap as [script]. The result envelope
    gains a normalizations: [{{path, action, before_type, after_type,
    after_length}}] field listing every change (empty when input was
    already valid). Closes the runtime "Expected array, got string" trap
    that the lax Storage API silently lets through.

  kbagent config set-default-bucket --project NAME --component-id ID --config-id ID (--bucket BUCKET_ID | --clear) [--dry-run] [--branch ID]
    Set or clear configuration.storage.output.default_bucket on a config without
    raw-mode JSON edits. --bucket/--clear are mutually exclusive. Read-modify-write
    that preserves sibling keys under storage.output and the rest of the config.
    No-op (with changed=false) when the value is already what you'd be setting.

  kbagent config rename --project NAME --component-id ID --config-id ID --name "New Name" [--branch ID] [--directory DIR]
    Rename a configuration. Updates name via API. If a local sync directory
    exists (.keboola/manifest.json), renames the directory and updates the
    manifest path. Uses git mv when inside a git repo for cleaner history.

  kbagent config delete --project NAME --component-id ID --config-id ID [--branch ID]
    Delete a configuration. Branch-aware.

  kbagent config new --component-id ID [--name NAME] [--project NAME] [--output-dir DIR]
                     [--push --no-files --description D --configuration JSON|@file|- --configuration-file PATH --no-validate --branch ID --dry-run --allow-plaintext-on-encrypt-failure]
    Default: generate boilerplate config from component schema (scaffold to --output-dir or stdout).
    With --push (0.33.0+): also create the config remotely via Storage API in one shot.
    --push requires --project AND a non-empty --name. --no-files skips the filesystem step
    entirely for FIIA-style one-shot creates. Schema validation runs by default when an explicit
    --configuration body is given (fail-closed; --no-validate opts out). Default body is {{}}
    (empty shell, validation auto-skipped). Works for ALL component types including
    keboola.snowflake-transformation (unlike tool call create_config which refuses it).

  kbagent config search --query PATTERN [--project NAME] [--component-type TYPE] [-i] [-r] [--branch ID]
    Search config bodies for string/regex. Reports match location in JSON tree. Branch-aware.

  kbagent config variables-set --project NAME --component-id ID --config-id ID --var KEY=VALUE [--var ...] [--replace] [--variables-id ID] [--values-id ID] [--branch ID] [--dry-run]
    Assign variables to any config. Auto-creates the backing keboola.variables + default row
    on first call and links the parent; subsequent calls update the same row (merge by default;
    --replace for full overwrite). Prefix KEY with # to auto-encrypt as a secret.

  kbagent config variables-get --project NAME --component-id ID --config-id ID [--branch ID]
    Read variable values attached to a config. Returns linked, variables_id, values_id, values.

  kbagent config variables-clear --project NAME --component-id ID --config-id ID [--branch ID] [--yes]
    Unlink variables from a config. Does NOT delete the underlying keboola.variables config
    (it may be shared). Delete it explicitly via `kbagent config delete` if needed.

### Config Metadata (folder organisation + arbitrary key/value)

  kbagent config metadata-list --project NAME --component-id ID --config-id ID [--branch ID]
    List all metadata entries on a configuration. Each entry: id, key, value, provider, timestamp.

  kbagent config get-metadata --project NAME --component-id ID --config-id ID --key KEY [--branch ID]
    Read a single metadata value by key. Exits 1 (NOT_FOUND) if absent.

  kbagent config set-metadata --project NAME --component-id ID --config-id ID --key KEY --value VALUE [--branch ID]
    Set (upsert) a metadata key/value on a configuration.

  kbagent config delete-metadata --project NAME --component-id ID --config-id ID --metadata-id ID [--branch ID] [--yes]
    Delete a configuration metadata entry by numeric ID (from metadata-list).

  kbagent config set-folder --project NAME --component-id ID --config-id ID --name "FolderName" [--branch ID]
    Sugar: writes KBC.configuration.folderName metadata. Groups the config in the Keboola UI.
    Pass --name "" to remove the folder assignment.

  kbagent config row-create --project NAME --component-id ID --config-id ID --name ROW_NAME [--description D] [--configuration JSON|@file|-] [--is-disabled] [--branch ID] [--allow-plaintext-on-encrypt-failure]
    Create a new configuration row. Returns the new row ID. Optional --configuration accepts JSON inline, @file, or stdin. #-prefixed secrets auto-encrypt before write (fail-closed; since 0.54.0).

  kbagent config row-update --project NAME --component-id ID --config-id ID --row-id ID [--name N] [--description D] [--configuration JSON|@file|-] [--is-disabled | --is-enabled] [--branch ID] [--allow-plaintext-on-encrypt-failure]
    Update an existing configuration row. Pass only the fields you want to change. #-prefixed secrets auto-encrypt before write (fail-closed; since 0.54.0).

  kbagent config row-delete --project NAME --component-id ID --config-id ID --row-id ID [--branch ID] [--yes]
    Delete a configuration row. Destructive; --yes to skip confirmation prompt.

  kbagent config oauth-url --project NAME --component-id ID --config-id ID [--redirect-url URL]
    Return the OAuth authorization URL for a component that uses OAuth authentication.
    Open the URL in a browser to complete the OAuth flow.

### Cross-Project Search

  kbagent search QUERY [--project NAME] [--type table|bucket|config|flow|data-app|transformation] [--search-type textual|config-based] [--limit N]
    Search for items across one or more projects. Textual mode (default) searches item names
    via the Storage API global-search endpoint. Config-based mode scans full configuration JSON bodies.
    --type is repeatable. --limit applies per project in textual mode (1-100, default 50).

### Job History

  kbagent job list [--project NAME] [--component-id ID] [--config-id ID] [--status STATUS] [--limit N]
    List jobs from Queue API. --status: processing, terminated, cancelled, success, error.

  kbagent job detail --project NAME --job-id ID
    Full job detail including result message and timing.

  kbagent job run --project NAME --component-id ID --config-id ID [--row-id ID ...] [--wait] [--timeout N] [--branch ID] [--mode run|debug] [--variable-values-id ID] [--no-variables] [--poll-strategy exponential|fixed] [--log-tail-lines N] [--idempotency-key KEY] [--force-rerun]
    Run a Queue API job. --row-id selects specific config rows (repeatable; omit to run entire config).
    --wait polls until job finishes. --timeout sets max wait in seconds (default 300). Branch-aware.
    When the config has linked variables (configuration.variables_id), kbagent auto-resolves
    a variableValuesId so the job binds to the deployed values row. --variable-values-id
    overrides; --no-variables skips resolution. Error code NO_VARIABLE_ROWS when the linked
    variables config has zero rows (run `kbagent config variables-set` to create one).
    Polling under --wait uses an exponential curve by default (2s x 30 -> 5s x 48 -> 15s);
    --poll-strategy fixed keeps a constant 1s interval. On FAILED/WARNING/TERMINATED, the last
    --log-tail-lines events (default 200, 0 disables -- recommended for automation pipelines) are
    surfaced as `logTail` in --json output.
    --mode run (default) writes to mapped output tables. --mode debug runs the component but
    redirects the output to a Storage File tagged `debug-<jobId>` instead of into destination
    buckets -- safe for dry-runs and for reproducing a failing run on a production configuration
    without touching production data. Invalid values exit 2 via Click choice gate (since v0.43.6).
    --json response shapes by exit code:
      - exit 0 (success): {{status:"ok", data:{{..., logTail?:[...]}}}}
      - exit 1 (QUEUE_JOB_FAILED, remote job status=error):
        {{status:"error", error:{{code:"QUEUE_JOB_FAILED", details:{{logTail:[...]}}}}}}
      - exit 4 (QUEUE_JOB_TIMEOUT, local timeout + remote kill also failed):
        {{status:"error", error:{{code:"QUEUE_JOB_TIMEOUT", retryable:true, details:{{logTail:[...]}}}}}}
      - exit 7 (JOB_TIMEOUT_TERMINATED, local timeout + remote kill succeeded):
        {{status:"error", error:{{code:"JOB_TIMEOUT_TERMINATED", details:{{job:{{...}}, logTail:[...]}}}}}}
    jq pattern: `.error.details.logTail? // .data.logTail? // []` picks up the tail regardless of exit.

  kbagent job terminate --project NAME (--job-id ID [--job-id ID ...] | --status any|created|waiting|processing [--component-id ID] [--config-id ID] [--branch ID] [--limit N]) [--dry-run] [--yes]
    Kill running jobs via Queue API (POST /jobs/{id}/kill). Use to stop runaway loops or pile-ups.
    Two modes: single/batch by --job-id, or bulk by --status. --status any covers all killable states
    (created+waiting+processing). Response partitions into killed / already_finished / not_found / failed.
    Idempotent: re-running on terminal jobs reports them as already_finished rather than failing.

### Storage

Note on branches: storage READ commands (buckets, bucket-detail, tables,
table-detail, files, file-detail) use the production endpoint by default,
even when a dev branch is active via `branch use`. The Storage API
branch-scoped endpoint returns only resources that were locally modified
in the dev branch, so a freshly-created branch lists nothing. Pass
`--branch ID` explicitly to query dev-branch-local tables/buckets.
Storage WRITE commands (create-*, upload-*, delete-*, file-upload, etc.)
remain branch-aware because modifying a dev branch is the expected intent.

  kbagent storage buckets [--project NAME] [--branch ID]
    List buckets with sharing/linked info. Shows source project for linked buckets.
    Uses production by default; pass --branch to query a dev branch explicitly.

  kbagent storage bucket-detail --project NAME --bucket-id BUCKET_ID [--branch ID]
    Bucket detail with backend-native direct-access paths. Resolves linked bucket source DB/dataset.
    Output adapts to the bucket's backend:
      - Snowflake -> snowflake_database / snowflake_schema / per-table snowflake_path quoted with "..."
      - BigQuery  -> bigquery_dataset (and bigquery_project when surfaced via API databaseName) /
                     per-table bigquery_path quoted with backticks `dataset`.`table` (or
                     `project`.`dataset`.`table` when project is known).
    Always-present backend-agnostic keys: sql_dialect ("snowflake" | "bigquery") and per-table sql_path.
    Prefer sql_path/sql_dialect in agent code instead of branching on backend yourself.
    Uses production by default; pass --branch to query a dev branch explicitly.

  kbagent storage tables [--project NAME ...] [--bucket-id BUCKET_ID] [--branch ID]
    List storage tables from one or more projects (in parallel). Omit --project
    to query all connected projects. Repeat --project for a specific subset.
    Multi-project by default, matching `storage buckets`, `config list`, `job list`.
    Each row is tagged with project_alias; per-project errors accumulate in the
    response envelope. --branch is only valid with a single --project.
    --bucket-id is applied independently per project; missing buckets are
    reported as per-project errors, not fatal.
    Uses production by default; pass --branch to query a dev branch explicitly.

  kbagent storage table-detail --project NAME --table-id TABLE_ID [--branch ID]
    Show detailed table info: columns (with types if available), primary key, row count, size, last import date.
    Uses production by default; pass --branch to query a dev branch explicitly.

  kbagent storage create-bucket --project NAME --stage STAGE --name BUCKET_NAME [--description D] [--backend B] [--branch ID]
    Create a new storage bucket. Stage must be "in" or "out". Branch-aware.
    On projects WITHOUT the `storage-branches` feature (legacy fake-branch), --branch
    writes succeed at the API level but the transformation runner ignores the bucket
    and creates a parallel `out.c-<branch_id>-*` bucket in the default branch at job
    time. Response includes `legacy_branch_storage: true` and human mode prints a
    warning when this applies. See storage-types-workflow.md.

  kbagent storage create-table --project NAME --bucket-id BUCKET_ID --name TABLE_NAME --column col:TYPE[(length)] [...] [--primary-key COL] [--not-null COL ...] [--default NAME=VALUE ...] [--branch ID] [--if-not-exists]
    Create a typed table. --column repeatable.
    - --if-not-exists (since 0.47.0): opt-in idempotency. On a duplicate-display-name failure,
      probe get-table-detail at the expected id and, if the table really exists, return
      `action: "skipped", skip_reason: "table already exists"` instead of raising. A different
      table with the same display name still surfaces the original error. Safe for parallel workers.
      Since 0.47.1: the skipped envelope reports the EXISTING table's actual `columns`/`primary_key`/`name`
      (not the request); requested values are mirrored under `requested_columns`/`requested_primary_key`,
      and `schema_drift: true` flags when the existing table diverges from what was requested.
    - Base types: STRING, INTEGER, NUMERIC, FLOAT, BOOLEAN, DATE, TIMESTAMP. Type defaults to STRING if omitted.
    - Native backend types with length pass through to the Storage API: VARCHAR(40), NUMBER(18,2), CHAR(10), TIMESTAMP_TZ, TIMESTAMP_NTZ, VARIANT, OBJECT, ARRAY, etc.
      The API validates type/length per backend; e.g. INTEGER(10) is rejected with "'10' is not valid length for INTEGER".
    - --not-null COL marks a column NOT NULL (nullable=false). Must match a defined --column name.
    - --default NAME=VALUE sets a DEFAULT expression. Booleans must be lowercase (true/false).
    - In a dev branch, the bucket is auto-materialized if it has not yet been written to in the branch
      (response includes auto_created_bucket=true). Mirrors the official Keboola Go CLI's EnsureBucketExists.
    - Auto-materialized buckets get KBC.createdBy.branch.id system metadata stamped on them,
      so transformation runners on branched-storage projects accept them as output destinations.
      A 403/5xx on the metadata write is logged and the create-table call still proceeds.
    - On legacy fake-branch projects (no `storage-branches` feature), response carries
      `legacy_branch_storage: true` and human mode prints a warning. The bucket and
      metadata stamp still happen but the runner will not use them -- it creates a
      parallel `out.c-<branch_id>-*` bucket in the default branch at job time.
    Branch-aware. Examples:
      --column pk:VARCHAR(40) --column amount:NUMERIC(18,2) --not-null pk --default amount=0
      --column ts:TIMESTAMP_TZ --column meta:VARIANT

  kbagent storage upload-table --project NAME --table-id TABLE_ID --file PATH [--incremental] [--delimiter D] [--enclosure E] [--no-auto-create] [--branch ID]
    Upload CSV into a table. Auto-creates bucket and table if missing (columns inferred as STRING from CSV header).
    Use --no-auto-create to require the table to already exist.
    Full load by default; --incremental to append rows. Supports files up to 5 GB via async file-first upload flow. Branch-aware.

  kbagent storage download-table --project NAME --table-id TABLE_ID [--output FILE] [--columns COL ...] [--limit N] [--where-column COL --where-value VAL ... [--where-operator eq|neq]] [--changed-since WHEN] [--changed-until WHEN] [--branch ID]
    Export table data to a local CSV file. Async export with streaming download.
    --where-column + --where-value (repeatable) + --where-operator eq|neq filter rows; --changed-since/--changed-until (unix ts or strtotime) filter by import time.
    Default filename: TABLE_NAME.csv. Use --columns to select columns (see table-detail for names).
    Use --limit to cap row count. Handles sliced files and gzip decompression transparently. Branch-aware.
  kbagent storage add-column --project NAME --table-id ID --column COL:TYPE[(length)] [--not-null] [--default VALUE] [--branch ID]
    Add a single column to an existing table (synchronous). Same name:TYPE(length) grammar as create-table --column.

  kbagent storage delete-table --project NAME --table-id ID [--table-id ...] [--force] [--dry-run] [--yes] [--branch ID]
    Delete one or more tables. Batch: repeat --table-id. --force to cascade-delete aliased tables. --dry-run to preview. Branch-aware.

  kbagent storage truncate-table --project NAME --table-id ID [--table-id ...] [--dry-run] [--yes] [--branch ID]
    Truncate one or more tables (delete all rows; preserve schema, primary key, descriptions, sharing edges, and dependents).
    Batch: repeat --table-id. Endpoint is async-via-job on every branch (the client polls to completion before returning;
    do not pass async=true -- the API rejects it). Idempotent (truncating an empty table is a no-op). Use this when re-seeding
    a table without losing the schema contract.

  kbagent storage delete-column --project NAME --table-id ID --column COL [--column ...] [--force] [--dry-run] [--yes] [--branch ID]
    Delete one or more columns from a table. Batch: repeat --column. --force when column is referenced by aliases. --dry-run to preview. Branch-aware.

  kbagent storage delete-bucket --project NAME --bucket-id ID [--bucket-id ...] [--force] [--dry-run] [--yes] [--branch ID]
    Delete one or more buckets. --force cascade-deletes tables. Linked/shared buckets protected. Branch-aware.

  kbagent storage swap-tables --project NAME --table-id ID --target-table-id ID --branch ID [--dry-run] [--yes]
    Swap two storage tables in any branch, including the default/production branch (POST /tables/{id}/swap). Both tables exchange physical positions;
    aliases are NOT transferred (they keep pointing at the same physical position and therefore expose the
    OTHER table's data after the swap). Use to promote a typed rebuild back into the original name without
    touching downstream config references. branch_id is mandatory (--branch or active branch via 'kbagent
    branch use'); service guards before any HTTP call when none is set. Any branch works, INCLUDING the
    default/production branch -- a default-branch swap is how a typed rebuild reaches prod (dev-branch merge
    does not carry storage schema).

  kbagent storage clone-table --project NAME --table-id ID --branch ID [--dry-run]
    Clone (pull) a production table into a dev branch (POST /tables/{id}/pull). On storage-branches projects a
    dev branch reads prod tables transparently until first write, so mutating a table's schema in the branch
    (swap-tables, dropping columns) first needs a branch-local copy. This materializes that copy (one-way:
    default -> branch). Branch is mandatory; service guards before any HTTP call when no branch is set.

### Storage Descriptions

  kbagent storage describe-bucket --project NAME --bucket-id ID [--text STR | --file PATH | --stdin] [--branch ID]
    Set the KBC.description metadata on a bucket (upsert). Visible in bucket-detail.

  kbagent storage describe-table --project NAME --table-id ID [--text STR | --file PATH | --stdin] [--branch ID]
    Set the KBC.description metadata on a table (upsert). Readable via table-detail --json .data.description.

  kbagent storage describe-column --project NAME --table-id ID --column NAME=DESC [--column ...] [--branch ID]
    Set per-column descriptions stored as KBC.column.{{name}}.description in table metadata (upsert).
    Readable via table-detail --json .data.column_details[].description.

  kbagent storage describe-batch --project NAME --from-file YAML [--branch ID]
    Apply bucket/table/column descriptions from a YAML file. Sections: buckets, tables, columns (all optional).
    Failures collected; one error does not abort remaining items.

### Storage Files

  kbagent storage files --project NAME [--tag TAG ...] [--limit N] [--offset N] [--query Q] [--branch ID]
    List Storage Files. --tag filters by tags (AND logic, repeat for multiple). --query for full-text search on name.
    Uses production by default; pass --branch to query a dev branch explicitly.

  kbagent storage file-upload --project NAME --file PATH [--name NAME] [--tag TAG ...] [--permanent] [--branch ID]
    Upload any file to Storage Files. --tag assigns tags (repeatable). --permanent prevents auto-deletion after 15 days.
    --name overrides the filename (default: local filename). Branch-aware.

  kbagent storage file-download --project NAME [--file-id ID | --tag TAG ...] [--output FILE]
    Download a Storage File. Either --file-id (by ID) or --tag (latest file matching all tags).
    --output sets local path (default: original filename). Handles sliced and gzipped files transparently.

  kbagent storage file-detail --project NAME --file-id ID
    Show file metadata: name, size, tags, sliced/permanent status, creator token. Does not download.

  kbagent storage file-delete --project NAME --file-id ID [--file-id ...] [--dry-run] [--yes]
    Delete one or more Storage Files. Batch: repeat --file-id. --dry-run to preview.

  kbagent storage file-tag --project NAME --file-id ID [--add TAG ...] [--remove TAG ...]
    Add and/or remove tags on a file in a single operation. Both --add and --remove are repeatable.

  kbagent storage load-file --project NAME --file-id ID --table-id TABLE_ID [--incremental] [--delimiter D] [--enclosure E] [--branch ID]
    Import an already-uploaded Storage File into a table. Useful for files uploaded by components or file-upload.
    --incremental to append rows. Branch-aware.

  kbagent storage unload-table --project NAME --table-id TABLE_ID [--columns COL ...] [--limit N] [--tag TAG ...] [--download] [--output FILE|DIR] [--file-type csv|parquet] [--branch ID]
    Export a table to a Storage File. The file stays in Keboola for other components to use.
    --tag assigns tags to the exported file. --download also saves it locally. Branch-aware.
    --file-type parquet produces a sliced Parquet file (CSV default). With --download, --output
    is a directory that will hold one .parquet file per slice plus _manifest.json.
    Default parquet directory: ./{{project}}/{{table_id}}.parquet/ (mirrors Keboola addressing).

### Data Streams (OpenTelemetry / OTLP)

  kbagent stream list --project NAME [--branch ID]
    List Data Streams sources (id, name, type, secret-free base endpoint).
  kbagent stream create-source --project NAME --name NAME [--type otlp|http] [--branch ID] [--if-not-exists] [--no-sinks] [--reveal]
    Create an OTLP (default) or HTTP source; polls the async task and returns the endpoint.
    For OTLP, auto-provisions the logs/metrics/traces sinks (bucket in.c-otlp-<source>) so data
    lands in Storage (idempotent; --no-sinks for a bare source).
    --if-not-exists returns an existing same-named source as status=skipped.
  kbagent stream detail [SOURCE_ID | --name NAME] --project NAME [--branch ID] [--reveal]
    Show base + per-signal endpoints (/v1/logs|/v1/traces|/v1/metrics), protocol http/protobuf,
    and destination bucket/tables (from sinks). The secret embedded in the OTLP URL is MASKED
    by default; pass --reveal to print it (e.g. to wire OTEL_EXPORTER_OTLP_ENDPOINT).
  kbagent stream delete SOURCE_ID --project NAME [--branch ID] [--dry-run] [--yes|--force]
    Delete a source (destructive; async task polled to completion).
  Notes: uses the per-project Storage token (no manage token). Control plane = stream.<region>
  derived from connection.<region>. The OTLP ingest host is stream-in.<region>, returned in
  source.otlp.url -- never derived. The raw Stream API does not auto-create sinks, so kbagent
  provisions the 3 OTLP sinks itself on create-source --type otlp (--no-sinks to opt out). Send
  OTLP/HTTP to <endpoint>/v1/logs|/v1/traces|/v1/metrics; data lands in in.c-otlp-<source>.* tables.

### Sharing (Cross-Project)

  kbagent sharing list [--project NAME]
    List shared buckets available for linking. Multi-project, uses regular token.

  kbagent sharing share --project ALIAS --bucket-id ID --type TYPE [--target-project-ids IDs] [--target-users EMAILS]
    Enable sharing on a bucket. Requires master token (KBC_MASTER_TOKEN_{{ALIAS}} or KBC_MASTER_TOKEN).
    Types: organization, organization-project, selected-projects, selected-users.

  kbagent sharing unshare --project ALIAS --bucket-id ID
    Disable sharing. Fails if linked buckets exist. Requires master token.

  kbagent sharing link --project ALIAS --source-project-id ID --bucket-id ID [--name NAME]
    Link a shared bucket into a project (read-only). Uses regular token.

  kbagent sharing unlink --project ALIAS --bucket-id ID
    Remove a linked bucket from a project. Uses regular token.

  kbagent sharing edges [--project NAME]
    Show cross-project data flow edges via bucket sharing. --project repeatable.

### Data Lineage

  kbagent lineage build --directory PATH --output PATH [--ai] [--refresh]
    Build column-level lineage graph from sync'd data. Scans all sync'd projects,
    detects dependencies via config mappings and SQL parsing, saves to cache file.
    Auto-detects both sync layouts: flat (./.keboola/manifest.json from
    sync pull --project X) and nested (./<alias>/.keboola/manifest.json from
    sync pull --all-projects). Emits a warning in response data if no projects
    are found. --refresh runs sync pull first. --ai generates
    .lineage_ai_tasks.json with AI analysis tasks for an AI agent to process
    (2-step flow).

  kbagent lineage show --load PATH [--upstream NODE] [--downstream NODE]
      [--column COL] [--columns] [--project ALIAS] [--depth N]
      [--format text|mermaid|html|er]
    Query upstream/downstream from cached lineage graph (from lineage build).
    Node identifiers: full FQN project-alias:bucket_id.table_name or just table_id.
    --columns shows column-level mapping. -c COL traces one column.

  kbagent lineage info --load PATH
    Show what's in a cached lineage graph: projects, tables, most connected nodes.

  kbagent lineage server --load PATH [--port N] [--host HOST]
    Start interactive lineage browser in the web browser. Sidebar-based node
    picker with mermaid/ER diagram rendering and export.

### Organization Management

  kbagent org setup --org-id ID --url URL [--dry-run] [--yes] [--token-description PREFIX] [--refresh]
    Bulk-onboard all org projects. Requires org-admin manage token. Idempotent.
    --refresh also refreshes tokens for already-registered projects with invalid tokens.

  kbagent org setup --project-ids 901,9621,10539 --url URL [--dry-run] [--yes] [--refresh]
    Non-admin mode: onboard specific projects by ID. Works with Personal Access Token (PAT).
    Use --org-id OR --project-ids (at least one required).
    Token via interactive hidden prompt by default; pass top-level
    --allow-env-manage-token to read KBC_MANAGE_API_TOKEN from env (CI/CD).
    Default-deny since 0.29.0 -- closes the AI-exfiltration risk where
    subprocesses inherit the manage token via env.

### Feature Flags (since v0.48.0)

  Requires a SUPER-ADMIN Manage API token (same kind as `org setup`). Same
  default-deny token policy: interactive hidden prompt by default; pass
  top-level --allow-env-manage-token to read KBC_MANAGE_API_TOKEN from env.
  --project resolves the stack URL (and, for project ops, the numeric
  project_id) from config -- the alias is the only handle you pass.

  kbagent feature list --project ALIAS
    Stack-wide feature catalogue (GET /manage/features).
  kbagent feature project-show --project ALIAS
    Features assigned to a project.
  kbagent feature project-add --project ALIAS --feature NAME [--dry-run] [--yes]
  kbagent feature project-remove --project ALIAS --feature NAME [--dry-run] [--yes]
    Enable / disable a feature on a project. add=admin, remove=destructive.
  kbagent feature user-show --project ALIAS --email EMAIL
  kbagent feature user-add --project ALIAS --email EMAIL --feature NAME [--dry-run] [--yes]
  kbagent feature user-remove --project ALIAS --email EMAIL --feature NAME [--dry-run] [--yes]
    Per-user features (GET/POST/DELETE /manage/users/{{email}}/features).

### Flows (Conditional Flows -- keboola.flow only; orchestrator dropped in 0.57.0)

  kbagent flow list [--project NAME] [--branch ID] [--with-schedules]
    List conditional flows (keboola.flow) across projects. Legacy keboola.orchestrator
    flows are NOT listed; their count is surfaced as legacy_orchestrator_count + a warning.
    --with-schedules enriches each row with {{schedule_id, cron, timezone, enabled}}
    entries from keboola.scheduler (one extra API call per project, NOT per flow).

  kbagent flow detail --project NAME --flow-id ID [--branch ID]
    Show phases, transitions (next[].goto + conditions), typed tasks, and full configuration.

  kbagent flow schema [--full --project NAME]
    Plain: print the offline conditional-flow YAML template. --full fetches and dumps the
    live JSON Schema from the stack (AI Service configurationSchema for keboola.flow) and
    REQUIRES --project (the schema is no longer bundled).

  kbagent flow validate --file YAML|@file|- [--project NAME]
    With --project: fetch the live schema from the stack -> full structural + semantic
    validation (fetch failure degrades to semantic-only + a note). Without --project:
    semantic-only validation + a note that structural validation was skipped (no schema
    source). Exit 0 valid, exit 2 on errors. --json adds {{valid, errors, warnings, notes}}.

  kbagent flow new --project NAME --name "Name" [--description D] [--file YAML|@file|-] [--branch ID]
    Create a new conditional flow. --file accepts YAML with 'phases' and 'tasks' keys.
    Validated against the LIVE conditional-flow schema fetched from the stack
    (INVALID_FLOW_DEFINITION on failure). A schema-fetch failure does NOT block the write:
    structural check skipped, semantic checks still run, a warning is surfaced.
    IDs are STRINGS; phases use next[].goto (a phase id or null); tasks are typed
    (job/notification/variable). Execute with: job run --component-id keboola.flow --config-id ID.

  kbagent flow update --project NAME --flow-id ID [--name N] [--description D] [--file YAML] [--branch ID]
    Update a flow's name, description, or phases/tasks. --file replaces both phases and tasks.
    Omitting --file leaves the flow body unchanged. Validated against the live conditional-flow
    schema on write (merge-aware; INVALID_FLOW_DEFINITION on failure; schema-fetch failure ->
    semantic-only + warning).

  kbagent flow delete --project NAME --flow-id ID [--branch ID] [--yes]
    Delete a flow. Does NOT remove associated keboola.scheduler configs.
    Run 'flow schedule-remove' first if you want to clean up schedules.

  kbagent flow schedule --project NAME --flow-id ID --cron "0 6 * * *" [--timezone TZ] [--enabled/--disabled] [--name NAME] [--branch ID]
    Upsert a cron schedule: updates the existing keboola.scheduler config if one exists, creates one
    otherwise. Calling twice with a new cron replaces the old schedule — no duplicates created.
    Schedules are stored as Storage API configs, not a separate scheduler service.

  kbagent flow schedule-remove --project NAME --flow-id ID [--branch ID] [--yes]
    Remove all schedules bound to this flow (deletes all matching keboola.scheduler configs).
    Idempotent: safe to run when no schedules exist.

### Schedule Discovery & Audit (Fleet-Wide)

  kbagent schedule list [--project NAME ...] [--enabled-only] [--branch ID]
    Fleet-wide list of every keboola.scheduler config across one, many, or all
    projects (multi-project fan-out; no --project means all). Each row shows
    project_alias, schedule_id, schedule_name, parent_component_id, parent_config_id,
    parent_name, cron, timezone, and enabled. Use --enabled-only to hide disabled
    schedules. Answers issue #195: "which flows are on cron triggers across N projects?".

  kbagent schedule detail --project NAME --schedule-id ID [--branch ID]
    Full detail for a single schedule: cron, timezone, enabled state, plus the
    parent config's component_id/config_id/name. Orphaned schedules (parent
    deleted) still return with parent_name="" -- never hard-fails.

  kbagent schedule find [--cron-window START-END] [--not-run-since DAYS] [--project NAME ...] [--branch ID]
    Audit filters, combinable with AND:
    * --cron-window "02:00-04:00" matches schedules whose cron's hour field
      is entirely inside the window. Hour-level approximation -- see gotchas.md.
    * --not-run-since N matches schedules whose parent config's latest job is
      older than N days (or never ran). Pass N=0 to force last_run_at lookup
      without applying a staleness filter.
    Columns last_run_at and matches_cron_window are ALWAYS present in the
    output but populated only when the corresponding filter is active --
    they stay None otherwise so LLM consumers do not treat unevaluated
    cells as positive match signals. Queue API is not branch-aware:
    --branch + --not-run-since still compares against production jobs.

### Development Branches

  kbagent branch list [--project NAME]
    List dev branches. --project repeatable.

  kbagent branch create --project ALIAS --name "name" [--description "..."]
    Create dev branch and auto-activate it. Async, CLI waits for completion.

  kbagent branch use --project ALIAS --branch ID
    Set existing branch as active for subsequent commands.

  kbagent branch reset --project ALIAS
    Reset to main/production branch.

  kbagent branch delete --project ALIAS --branch ID
    Delete branch (async). Auto-resets to main if it was active.

  kbagent branch merge --project ALIAS [--branch ID]
    Get KBC UI merge URL (does NOT merge via API). Resets active branch.

  kbagent branch metadata-list --project NAME [--branch ID|default]
    List all metadata entries on a branch (id, key, value, provider, timestamp).

  kbagent branch metadata-get --project NAME --key KEY [--branch ID|default]
    Read a single metadata value by key. Exits with NOT_FOUND if absent.

  kbagent branch metadata-set --project NAME --key KEY [--text STR | --file PATH | --stdin] [--branch ID|default]
    Set a metadata key/value. Useful for KBC.projectDescription and similar
    dashboard-visible fields. --branch defaults to "default" (main branch).

  kbagent branch metadata-delete --project NAME --metadata-id ID [--branch ID|default]
    Delete a metadata entry by its numeric ID (from metadata-list).

### Workspaces (SQL Debugging)

  kbagent workspace create --project ALIAS [--name NAME] [--backend TYPE] [--ui] [--read-only/--no-read-only]
    Create workspace. Backend auto-detected from project (or override with --backend). Default: headless (~1s). --ui: visible in KBC UI (~15s).
    Since 0.47.1, Snowflake headless creates return private_key and an empty password field; use key-pair auth.

  kbagent workspace list [--project NAME] [--orphaned] [--branch ID] [--qs-compatible]
    List workspaces. Read command: ignores active dev branch (production endpoint) with an Info banner;
    pass --branch to opt in. Each entry carries login_type, read_only, qs_compatible so callers can pick a
    Query-Service-compatible workspace without firing a probe query. --qs-compatible filters to RO +
    confirmed-whitelist loginType (canonical data-app shape). --orphaned shows orphaned workspaces.

  kbagent workspace detail --project ALIAS --workspace-id ID [--branch ID]
    Workspace connection details (no password). Includes login_type, read_only, qs_compatible.
    Read command: ignores active dev branch with an Info banner; pass --branch to opt in.

  kbagent workspace delete --project ALIAS --workspace-id ID
    Delete workspace. They also expire automatically.

  kbagent workspace password --project ALIAS --workspace-id ID
    Reset and return new workspace password.

  kbagent workspace load --project ALIAS --workspace-id ID --tables TABLE_ID [...] [--preserve]
    Load storage tables into workspace. --preserve keeps existing tables.

  kbagent workspace query --project ALIAS --workspace-id ID --sql "SQL" [--file F] [--transactional] [--full] [--limit N]
    Execute SQL via Query Service. No Snowflake credentials needed.
    Default reads results inline (fast JSON columns+rows), capped at --limit (default 500).
    --full uses the complete CSV export instead (slower, uncapped).

  kbagent workspace from-transformation --project ALIAS --component-id ID --config-id ID [--row-id ID]
    Create workspace from transformation config. Loads input tables automatically.

  kbagent workspace gc [--project NAME] [--dry-run] [--yes]
    Garbage-collect orphaned workspaces (keboola.sandboxes config missing). Use --dry-run to preview.

### Data Apps (Streamlit / Flask / Node deployments)

Lifecycle for `keboola.data-apps`. Combines the Storage API (config body --
git block, slug, runtime size, encrypted secrets) with the Data Science API
(/apps -- deployment record, state, URL, configVersion). Encapsulates the
§9 redeploy contract so callers cannot pin to the empty-shell v2.

  kbagent data-app list [--project NAME ...] [--branch ID]
    List data apps across one or many projects. Merges Data Science /apps
    index with Storage config names. Multi-project parallel.

  kbagent data-app detail --project NAME --app-id ID [--branch ID]
    Full merged view: state, desiredState, url, deployed configVersion, slug,
    runtime size, git settings (PAT redacted as <encrypted>).

  kbagent data-app create --project ALIAS --name NAME --slug SLUG --git-repo URL
    [--description STR | --description-file PATH] [--git-branch main]
    [--git-public/--no-git-public] [--git-username USER]
    [--git-pat-env VAR | --git-pat-file PATH | --git-pat-encrypted KBC::Project...]
    [--auth password|public] [--size tiny|small|medium|large] [--auto-suspend SECONDS]
    [--type python-js|python|streamlit|r|...] [--branch ID]
    [--no-deploy] [--wait] [--timeout SECONDS] [--keep-on-failure] [--dry-run]
    Create + configure + deploy in one call. Default `--auth password` mints
    a 20-char hex simpleAuth password (retrievable via `data-app password`).
    PAT input (private repo): env var (recommended) > file > pre-encrypted.
    Pre-encrypted PATs MUST start with KBC::Project (project-scoped KMS).
    Cleanup-in-finally if PUT or initial deploy fails (orphan shell deleted
    by default; --keep-on-failure preserves it for forensics).

  kbagent data-app deploy --project NAME --app-id ID [--config-version N]
    [--wait] [--timeout SECONDS] [--branch ID]
    The §9 redeploy contract. Default reads the latest Storage config version
    and pins to it; --config-version pins an older version (rollback).
    Always sends {{desiredState=running, configVersion, restartIfRunning=true}}
    together -- HTTP 422 otherwise.

  kbagent data-app start --project NAME --app-id ID [--wait] [--timeout SECONDS]
    Wake an auto-suspended data app at its currently-pinned configVersion.
    Distinct from deploy: does NOT bump the version.

  kbagent data-app stop --project NAME --app-id ID [--wait] [--timeout SECONDS]
    Stop a running data app. Preserves URL and Storage config; container is
    torn down.

  kbagent data-app delete --project NAME --app-id ID [--yes]
    Delete the deployment AND the Storage config (cascade, irreversible).
    URL is permanently retired. Confirmation prompt unless --yes.

  kbagent data-app password --project NAME --app-id ID
    Retrieve the simpleAuth password. Requires the Manage API token in
    addition to the project's Storage token. Token is read from interactive
    hidden prompt by default; pass top-level --allow-env-manage-token to
    use KBC_MANAGE_API_TOKEN from env (default-deny since 0.29.0). Never
    persisted, never logged. Password is auto-generated at create time
    and CANNOT be rotated -- delete and recreate the app to mint a new one.

  kbagent data-app logs --project NAME --app-id ID [--lines N] [--since ISO8601]
    Tail the container log buffer (Data Science /apps/{id}/logs/tail).
    Plain-text body covering the full spin-up trace ([TIMING] git_clone,
    Cloning into /app, uv install, supervisord boot, runtime stack traces).
    Default --lines 500; pass --lines 0 to fetch the full current buffer
    (no server-side cap). --lines and --since are mutually exclusive;
    --since requires a timezone (Z or +00:00). App must be running or
    recently-stopped -- never-started apps return 400 "App X is not
    running"; recover with 'kbagent data-app start' or 'data-app deploy'.
    Closes the gap where the upstream keboola-mcp-server's get_data_apps
    tool hardcodes a 20-line cap on log output (structurally too small to
    capture a healthy spin-up). The log buffer can echo runtime secrets
    the app printed to stdout/stderr -- consider hygiene before piping
    --json output into AI agent context.

  kbagent data-app secrets-set --project ALIAS --app-id ID --secret '#KEY=VALUE'
        [--secret '#KEY2=VALUE2' ...] [--secrets-file PATH] [--branch ID]
        [--allow-plaintext-on-encrypt-failure] [--dry-run] [--no-hint-next]
    Encrypt and write '#'-prefixed secrets into parameters.dataApp.secrets.
    Per-project KMS via the Encryption API; ciphertext does not cross
    projects (writeup §8). Read-modify-write at the service layer to
    preserve sibling keys; never use Storage merge=True for nested edits.
    The runtime exposes each key as an env var with '#' stripped, '-'
    replaced with '_', uppercased ('#my-api-key' -> 'MY_API_KEY').
    Adding a secret bumps the Storage version; the running container
    keeps the OLD config until the next 'kbagent data-app deploy'.

  kbagent data-app secrets-list --project ALIAS --app-id ID [--branch ID]
        [--show-fingerprint]
    List the keys in parameters.dataApp.secrets with derived runtime
    env-var names. Never echoes encrypted ciphertext in full and never
    decrypts. --show-fingerprint includes a short fingerprint per key.

  kbagent data-app secrets-get --project ALIAS --app-id ID --key 'KEY'
        [--branch ID]
    Show ONE key from parameters.dataApp.secrets. Leading '#' is OPTIONAL
    -- the block holds both encrypted secrets (#) and plain env-var
    values, both enumerated by secrets-list. ENCRYPTED secret -> metadata
    only (encrypted: true, value: null); the decrypted plaintext is NEVER
    echoed (Encryption API has no decrypt endpoint). PLAIN value -> the
    literal value (encrypted: false), already visible via config detail.
    NOT_FOUND on absent key (exact match); never enumerates siblings.

  kbagent data-app secrets-remove --project ALIAS --app-id ID --key 'KEY'
        [--key 'KEY2' ...] [--branch ID] [--yes] [--dry-run]
    Remove one or more keys (encrypted secrets OR plain env vars; leading
    '#' optional). Idempotent (missing keys -> exit 0, removed: 0).
    Destructive: a removal can break the running app at next deploy if it
    depends on the value. Confirmation prompt unless --yes or --json.

  kbagent data-app validate-repo --git-repo URL [--git-branch BRANCH]
        [--git-public/--no-git-public] [--git-pat-env VAR | --git-pat-file PATH]
        [--type python-js] [--strict]
    Pre-flight check that a git repo follows the Keboola data-app
    Golden Rule (https://help.keboola.com/data-apps/python-js/). Walks
    the repo via GitHub Contents + Trees API (<=5 calls -- 1 tree +
    up to 4 contents -- regardless of
    repo size); each check emits BLOCKING / WARN / OK with a help-doc
    citation. --type currently restricted to python-js; streamlit /
    pure-Python / R / Node-only follow-up. --strict treats WARNs as
    failures (exit 1).

### Project Sync

  kbagent sync init --project ALIAS [--directory DIR] [--git-branching] [--adopt-existing]
    Initialize sync working directory. --git-branching enables git-to-Keboola branch mapping.

  kbagent sync pull --project ALIAS [--all-projects] [--force] [--dry-run] [--with-samples] [--no-storage] [--no-jobs] [--job-limit N] [--branch ID]
    Download configs as local files. Idempotent, protects local modifications.
    --force (semantics corrected since 0.53.0): re-pull over locally-modified configs.
    A config edited locally whose remote is UNCHANGED is PRESERVED (its pending delta stays
    pushable -- NOT discarded, NOT silently re-stamped). A true merge conflict (the config
    changed BOTH locally and on the remote since the last pull) ABORTS the pull (exit 1,
    SYNC_CONFLICT) listing each conflict; resolve via sync diff then push-or-discard, then pull.
    To intentionally drop local edits, delete the file/dir and pull. Applies to rows too.
    --job-limit controls max recent jobs per config (default 5). For large projects,
    automatically falls back to per-config job fetching to ensure all configs get job history.
    Auto-detects renamed configs and renames local directories to match (uses git mv in git repos).
    --branch (since 0.47.0): per-invocation dev-branch override. Same semantics as sync push/diff.

  kbagent sync status [--directory DIR]
    Show local changes since last pull (SHA256-based). Also returns
    plaintext_secret_warnings (since 0.55.0): in-sync configs/rows whose
    #-secrets are still plaintext on the remote (pre-0.54.0 leak; #378). Fix =
    re-push on >=0.54.0 + rotate (version history keeps the plaintext).

  kbagent sync diff --project ALIAS [--all-projects] [--directory DIR] [--branch ID]
    3-way diff: local vs pull-time snapshot vs remote. Detects conflicts.
    --branch (since 0.47.0): per-invocation dev-branch override. Wins over
    manifest.branches[0] / 'branch use' active branch / git-branching mapping.
    Requires exactly one --project.

  kbagent sync push --project ALIAS [--all-projects] [--dry-run] [--force] [--allow-plaintext-on-encrypt-failure] [--branch ID] [--no-name-drift-warnings]
    Push local changes. Auto-encrypts secrets. Skips conflicts (pull first).
  kbagent sync clone --source DIR --target ALIAS --target-dir DIR [--bucket-map FILE] [--variable-values FILE] [--instance-rename FILE] [--dry-run] [--branch ID]
    Clone a reference synced tree into a fresh target project + parameterize it
    (bucket_map / variable_values / instance_rename overrides), then push so every
    config CREATEs fresh. keboola.flow task configIds + variable links remap
    reference->ULID. Idempotent (re-run -> no_changes); needs a fresh target.
    Fails if encryption fails (plaintext secrets never pushed). Use escape hatch flag only if you know what you are doing.
    Fresh-CREATE behavior (since 0.47.0): if the manifest contains a placeholder entry at
    (component_id, path), the create path updates it in place (no manifest duplication)
    and propagates any KBC.configuration.* metadata via set_config_metadata. Re-pushes
    against the now-real config id are naturally idempotent.
    Fresh-CREATE variable binding (since 0.47.2): when a keboola.variables config + its
    values row are created alongside a transformation in the same push, the transformation's
    variables_id / variables_values_id are rebound to the assigned ULIDs (not placeholder
    dirnames), the row's values are hoisted even when the scaffold row file has no _keboola
    block, and the row's placeholder parent is remapped before POST. job run then succeeds
    without a post-push config variables-set step.
    --branch (since 0.47.0): per-invocation dev-branch override. Same semantics as sync diff.
    When no <branch_name>/ subtree exists on disk (since 0.47.2), the local default tree
    (main/) is read as the source and promoted to the target branch; API writes still target
    the branch id.
    --no-name-drift-warnings (since 0.47.0): suppress the cosmetic name_drift_warnings
    array from the result envelope.

  kbagent sync branch-link --project ALIAS [--branch-id ID] [--branch-name NAME]
    Link git branch to Keboola dev branch. Auto-creates if needed.

  kbagent sync branch-unlink [--directory DIR]
    Remove git-to-Keboola branch mapping.

  kbagent sync branch-status [--directory DIR]
    Show current branch mapping status.

### Encryption

  kbagent encrypt values --project ALIAS --component-id ID --input JSON|@file|-  [--output-file PATH]
    Encrypt #-prefixed secret values via Keboola Encryption API (one-way, no decrypt).
    Scope: ComponentSecure (project + component). Use for MCP tool call workflows
    where ciphertext must exist before calling update_config / create_config.
    --input accepts: inline JSON, @file.json (from file), or - (from stdin).
    Already-encrypted values (KBC:: prefix) pass through unchanged.

### Semantic Layer (Metastore) (since v0.41.0)

Manage Keboola metastore models: datasets, metrics, relationships, constraints,
glossary terms. Metastore URL derived from stack URL by replacing `connection.`
with `metastore.`. Auth: same `X-StorageApi-Token` as Storage. Alias:
`kbagent sl ...` (hidden) is equivalent to `kbagent semantic-layer ...`.

  kbagent semantic-layer model list --project P
    List all semantic-layer models in a project.

  kbagent semantic-layer model create --project P --name N [--description D] [--sql-dialect Snowflake]
    Create a new model (default sql-dialect: Snowflake).

  kbagent semantic-layer model delete --project P --model M [--yes]
    Delete a model. Fails if the model still has child entities.

  kbagent semantic-layer show --project P [--model M] [--type T]
    Show a model's entities. --type filter: dataset|metric|relationship|constraint|glossary.
    Without --type prints a per-type count summary.

  kbagent semantic-layer search-context --project P [--pattern G ...] [--type model|dataset|metric|relationship|constraint|glossary|all] [--limit N]
    (since 0.47.0) Project-wide glob search across semantic-layer entity names.
    Mirrors the upstream keboola-mcp-server search_semantic_context tool so a
    downstream caller can verify the model is populated without an MCP dependency.
    Patterns are case-sensitive fnmatch, repeatable (union). Default pattern is "*".
    Default --type is "all" (every CHILD type; "model" searches semantic models).
    Returns {{project, contexts: [{{id, type, name, description, attributes}}], total_count}}.

  kbagent semantic-layer get-context --project P --context-id ID
    (since 0.47.0) Single-entry fetch by id, irrespective of type. Probes model first,
    then datasets/metrics/relationships/constraints/glossary in order; raises NOT_FOUND
    if no type matches (exit 1).

  kbagent semantic-layer validate --project P [--model M] [--deep]
    Basic structural checks (duplicates, dangling refs, sum-on-pct,
    constraint orphans, severity-suffix). --deep adds parallel Snowflake
    column-existence checks for phantom fields, phantom column refs, and
    AGG-on-STRING via in-process StorageService.

  kbagent semantic-layer export --project P [--model M] [--output PATH]
    Snapshot the model to a self-describing JSON file. Default path:
    ./sl_export_{{model_name}}_{{YYYYMMDD_HHMMSS}}.json.

  kbagent semantic-layer diff (--project-a A | --file-a P) (--project-b B | --file-b P) [--model-a M] [--model-b M]
    Three-way diff: project<->project, project<->file, file<->file. Output
    groups changes per entity type: added, removed, changed (with diff_keys).

  kbagent semantic-layer reference-data list|get|set|delete ... (since 0.55.0)
    Dimension-member records (semantic-reference-data): one record per
    dimension holding the full member list in a members[] array (e.g. a
    Chart of Accounts). Deliberately OUTSIDE build/export/diff/cascade.
    list --project P [--model M] -> dimension summaries (id, dimension,
    member_count). get --project P (--id ID | --dimension D) ->
    one record + all members (dimension is project-unique, so no model
    needed). set --project P [--model M] --dimension D
    --members-file PATH ('-' = stdin) [--dataset-id T] [--description X] ->
    create-or-replace, idempotent on dimension (project-wide lookup): an
    existing record is replaced in place via PUT (revision++), else POST.
    delete --project P --id ID [--yes]. Member keys mirror the DIM_COA
    columns (account_code, account_name, parent_code, is_leaf, ...).

  kbagent semantic-layer add metric|dataset|relationship|constraint|glossary ...
    Add one entity. Dataset auto-derives `fqn` from --table-id; --deep-fields
    fetches the storage schema and synthesises role-classified fields
    (PK_/FK_->key, *_DATE/*_DT->timestamp, numeric amount/value/rate->measure,
    else dimension). Constraint name regex `^[a-z][a-z0-9_]*$`, severity is
    error|warning|info (the 4-band health convention lives in the NAME suffix
    `_critical/_warning/_healthy/_review`, not the API severity). `--rule` is
    a STRING expression (e.g. "value >= 0"), NEVER an object.

  kbagent semantic-layer edit metric|dataset|constraint|relationship|glossary ...
    DELETE+POST (no PATCH on metastore). Metric rename cascades through every
    constraint referencing the old name (DELETE old + POST new with updated
    metrics[]); CODE_METRIC warning shown
    (re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")). On POST failure,
    rollback re-POSTs original_attrs and reports success/failure explicitly.
    --yes skips the confirm prompt. `edit relationship` accepts --new-from /
    --new-to / --new-on / --new-type (left|inner). `edit glossary` accepts
    --new-term (destructive cascade; requires --yes in non-TTY) / --new-definition.
    Partial-state envelope (since v0.41.10): when metric rename succeeds but
    one or more dependent constraints fail to repoint, the response sets
    `partial_state: true` at the top level + `recovery_hint: "<text>"`
    pointing at `semantic-layer validate` + manual `edit constraint
    --new-metrics ...`. Human-mode CLI prints a red `PARTIAL STATE` banner
    above the per-entry list. `edit_simple` (no-cascade variants) carries
    `partial_state: false, recovery_hint: null` for envelope uniformity.

  kbagent semantic-layer remove metric|dataset|constraint|relationship|glossary ...
    Destructive. `remove metric` pre-scans constraints whose metrics[] includes
    the target; warns about dangling DIM_METRIC_THRESHOLD refs. --yes skips the
    prompt but the orphan warning is always printed. Non-TTY without --yes
    refuses with exit 2. `remove relationship` and `remove glossary` are leaf
    removes -- no orphan-check (those entities aren't referenced by others).
    `remove glossary` identifies the entity by --term, not --name.

  kbagent semantic-layer import --project P --file PATH [--model M] [--types T,T,...] [--dry-run] [--yes] [--overwrite]
    Replay a snapshot. Default: skip on conflict. --overwrite opts into
    DELETE+POST. Dependency-ordered push (datasets -> metrics -> relationships
    -> glossary -> constraints).

  kbagent semantic-layer promote --from-project A --to-project B [--from-model M] [--to-model M] [--types ...] [--dry-run] [--yes]
    Cross-project copy with modelUUID rewrite. Classifies items NEW / IDENTICAL
    / CHANGED (deep-equality after stripping modelUUID + timestamps).
    Additive + overwrite only -- NEVER deletes target items absent from source.

  kbagent semantic-layer build --project P [--model M] --tables T,T,... [--dry-run] [--keep-on-failure] [--output PATH]
    Non-interactive heuristic builder. AI caveat: the ai_client has no
    arbitrary-JSON endpoint, so `build` falls back to a deterministic
    heuristic (one dataset + one COUNT(*) metric + one glossary entry per
    table; FQN derived; fields[] role-classified). Response carries
    `fallback_used: "heuristic"`. Push loop iterates all 5 child types in
    dependency order (fixes the long-standing sl-build skill bug where
    semantic-constraint was silently dropped). On push failure rolls back
    every successfully-POSTed child in reverse order + deletes the model
    if we created it (since v0.41.10); pass --keep-on-failure to preserve
    the partial state for forensic inspection (mirrors data-app create).

  kbagent semantic-layer token --encrypt --project P --component-id C
    Encrypt the project's storage token for transformation `user_properties`.
    Builds {{"#metastore_token": <token>}} and delegates to EncryptService.
    --encrypt is currently required; other modes refused with USAGE_ERROR.


### Self-call HTTP (inside `kbagent serve` subprocesses)

  kbagent http get PATH [--timeout SECONDS]
  kbagent http post PATH [--body JSON|@file|-] [--timeout SECONDS]
  kbagent http patch PATH [--body JSON|@file|-] [--timeout SECONDS]
  kbagent http delete PATH [--timeout SECONDS]
    Raw HTTP client against the running `kbagent serve`. Reads KBAGENT_SERVE_URL +
    KBAGENT_SERVE_TOKEN env vars (auto-injected into AI-agent / cli_command
    subprocesses by the scheduler). Example:
      kbagent http get /openapi.json     # browse server's OpenAPI schema
      kbagent http get /projects         # list projects via HTTP
      kbagent http post /agents/test --body @task.json
    Prefer this over forking `kbagent` CLI inside scheduled-agent tasks --
    `kbagent http` calls the serve directly so you always see the same config
    the operator configured (not the global ~/.config one). Outside a serve
    subprocess context the command refuses to run.

### Agent Tasks (CLI parity with the `/agents` REST surface)

  Reads/writes <config_dir>/agents.json -- the same on-disk format the
  cron loop inside `kbagent serve` consumes. CLI CRUD + ad-hoc `run`
  work offline; cron firing still requires the live server.

  Every subcommand below that takes TASK_ID / RUN_ID accepts it either
  positionally (`agent show TASK_ID`) or via flag (`--id` / `--task-id`,
  plus `--run-id` for run-detail / run-events) -- the flag form matches
  the rest of the CLI (`--job-id`, `--config-id`, ...).

  kbagent agent list
    List all registered tasks (id, name, cron, type, state, last/next run).

  kbagent agent show TASK_ID
    Full task detail including the action payload.

  kbagent agent create --name N [--description D] [--cron CRON] [--manual]
                       [--enabled/--disabled]
                       (--type ai_agent --cli claude|codex|gemini --prompt P
                                        [--extra-arg ARG ...] [--timeout SECONDS]
                       |--type cli_command --argv ARG [--argv ARG ...]
                                           [--timeout SECONDS]
                       |--type mcp_tool --tool TOOL [--mcp-project ALIAS]
                                        [--mcp-branch ID] [--input JSON|@file|-]
                                        [--timeout SECONDS]
                       |--from-file PATH|@path|-)
                       [--trigger-task-id ID --trigger-on success|error|always]
    Persist a new scheduled task. --manual skips the cron loop. Use
    --from-file for the full {{"type":..., "params":...}} JSON envelope
    when prompts/args grow large. --extra-arg on an ai_agent task is
    honored only when the kbagent process (serve, or this `agent` run)
    has a truthy KBAGENT_ALLOW_AI_EXTRA_ARGS env (since 0.60.2);
    otherwise the args are dropped with a warning.

  kbagent agent update TASK_ID [--name N] [--description D] [--cron C]
                                [--enabled/--disabled] [--manual/--auto]
                                [--clear-trigger]
                                [--trigger-task-id ID --trigger-on ...]
    Patch one or more fields. Omitted flags leave the field unchanged.
    --manual nulls next_run_at; --auto recomputes it from the cron expr.

  kbagent agent delete TASK_ID [--yes]
    Permanent removal. Run history on disk is preserved.

  kbagent agent run TASK_ID [--stream]
                            [--runtime-prompt TEXT | --runtime-input JSON|@file|-]
    Trigger immediately. --stream emits live events (one line per event;
    NDJSON in --json mode). --runtime-prompt appends ad-hoc text to an
    ai_agent's persisted prompt for this run only; --runtime-input merges
    arbitrary JSON into the action params.

  kbagent agent runs TASK_ID [--limit N]
    Run history (newest first).

  kbagent agent run-detail TASK_ID RUN_ID
    Single AgentRun record (status, summary, output, error).

  kbagent agent run-events TASK_ID RUN_ID
    Replay the persisted ai_agent event timeline.

  kbagent agent test [--type ... | --from-file PATH] [--stream] [--name N]
                     [common action flags from `create`]
    Execute an action ad-hoc -- nothing is persisted. Useful for
    sanity-checking a prompt / tool / argv before saving.

  kbagent agent cron-preview --cron "0 6 * * 1" [--count N]
    Validate a cron expression and show the next N firings (UTC, max 20).

  kbagent agent prompt-improve --goal "..." [--draft "..."]
                                [--cli claude|codex|gemini] [--project ALIAS]
                                [--extra-arg X ...] [--stream/--no-stream]
    AI-polished single-shot prompt for an unattended agent task. Spawns
    the chosen AI CLI with a meta-prompt; the final `done` event's
    `data.prompt` carries the cleaned body ready to paste into
    `agent create --prompt ...`. --extra-arg follows the same
    KBAGENT_ALLOW_AI_EXTRA_ARGS opt-in as `agent create` (since 0.60.2).

  See agent-tasks-cli-workflow.md skill reference for full walkthroughs.

### MCP Tools (Multi-Project)

  kbagent tool list [--project NAME] [--branch ID]
    List MCP tools with inputSchema. Use --json to inspect accepted parameters.

  kbagent tool call TOOL_NAME [--project NAME] [--input JSON|@file|-] [--branch ID]
    Call an MCP tool. Read tools auto-query all projects. Write tools need --project.
    --input accepts: inline JSON, @file.json (from file), or - (from stdin).
    --branch is a CLI flag (NOT a tool input param). Do not pass branch_id in --input.

### Kai -- Keboola AI Assistant (BETA)

  Requires the project to be added with its MASTER Storage API token (the
  auto-generated 'owner' token, not a custom one) and the 'AI Agent Chat'
  feature flag enabled on the project. Custom Storage API tokens cannot
  access Kai -- all `kbagent kai *` calls will fail with KAI_NOT_ENABLED.

  kbagent kai ping [--project NAME]
    Check Kai server health and MCP connection status.
    Fails with KAI_NOT_ENABLED if the project lacks the 'agent-chat' feature
    or was added with a non-master token.

  kbagent kai preflight [--project NAME]
    Inspect the configured token's Kai readiness WITHOUT raising. Returns
    {{ok, is_master_token, has_agent_chat_feature, token_description, error}}.
    Use this when you need to render a warning instead of failing — UIs and
    automation pre-flight checks should use this instead of `ping`.

  kbagent kai chat-detail --chat-id ID [--project NAME]
    Fetch the full message history of a single Kai chat. Returns a flat list
    of {{role, content, created_at}} records. Use to restore / continue a
    conversation with `kai chat --chat-id ID` or to export a transcript.

  kbagent kai ask --message "question" [--project NAME]
    One-shot question to Kai. Collects full response. Use --json for structured output.
    Kai has MCP access to project data -- use for Keboola-specific questions
    (e.g. "What tables do I have?", "Is it safe to drop bucket X?").

  kbagent kai chat --message "msg" [--chat-id ID] [--project NAME]
    Send message in a chat session. Use --chat-id to continue a conversation.
    Without --chat-id starts a new chat. Returns chat_id for continuation.

  kbagent kai history [--project NAME] [--limit N]
    List recent Kai chat sessions. Default limit: 10.

### Developer Portal (since v0.49.0)

  The `dev-portal` command group talks to `apps-api.keboola.com` (the Keboola
  Developer Portal) and lets component developers register and update components
  without leaving the terminal.

  **Safety contract**: reads are unrestricted. Writes (`create`, `patch`,
  `upload-icon`, `publish`, `deprecate`) always print the full pending request
  and then require the user to type a random hex code on a real TTY. There is
  no `--yes` flag and no env-var bypass; non-TTY shells exit 6. Use `--dry-run`
  to get a clean exit-0 preview (the agent-safe path).

  **Identity management** -- portal logins are stored per-alias in `config.json`:

    kbagent dev-portal identity add --alias vendor-keboola \\
      --username service.keboola.xxxxx --password ... --vendor keboola \\
      --role-hint vendor    # default; restricts PATCH to vendor endpoint
    kbagent dev-portal identity add --alias admin-keboola \\
      --username admin@keboola.com --role-hint admin --password-stdin
    kbagent dev-portal identity use vendor-keboola

  **`role_hint` is load-bearing (since v0.51.1)**: `vendor` (default) routes
  `dev-portal patch` to `PATCH /vendors/{{vendor}}/apps/{{app}}` (restricted
  schema); `admin` routes it to `PATCH /admin/apps/{{app}}` (permissive
  schema). The admin endpoint is the **only** way to set the 9 fields
  apps-api `.forbidden()`s on vendor: `complexity`, `categories`, `category`,
  `features`, `forwardToken`, `forwardTokenDetails`, `injectEnvironment`,
  `processTimeout`, `requiredMemory`. Sending any of those with a `vendor`
  identity fails fast at preflight with the exact command to switch
  identity (server-side it would have returned a misleading 422 saying
  "must be one of: easy, medium, hard"; that message is a known apps-api
  bug -- the field is actually `forbidden()`, not enum-validated).

  **`--password-stdin` (since v0.51.1)** works on TTY (hidden line-based
  prompt, Enter to confirm) AND on a pipe (`echo $PASS | … --password-stdin`,
  reads to EOF). Pre-0.51.1 the flag hung interactively because it always
  waited for EOF.

  **Read commands** (unrestricted; good for peer-config research):

    kbagent --json dev-portal list --vendor keboola
      List all apps for a vendor. Use for peer research: compare how existing
      extractors configure uiOptions, encryption, defaultBucket, etc.

    kbagent --json dev-portal get --app keboola.ex-db-mysql
      Full portal entry for one component. Pull two peers and compare.

  **Write commands** (require random-code TTY confirm; use --dry-run first):

    kbagent dev-portal create --vendor V --data FILE [--dry-run]
    kbagent dev-portal patch --app VENDOR.APP_ID (--data FILE | --property KEY ...) [--dry-run]
    kbagent dev-portal upload-icon --app VENDOR.APP_ID --file PATH [--dry-run]
    kbagent dev-portal publish --app VENDOR.APP_ID [--dry-run]
    kbagent dev-portal deprecate --app VENDOR.APP_ID [--dry-run]

  **Identity lifecycle**:

    kbagent dev-portal identity add / list / remove / edit / use / current / verify

  **Identity selection**: pass `--identity <alias>` on any command, or set the
  default with `dev-portal identity use <alias>`.

### Utility Commands

  kbagent init [--from-global] [--project ALIAS ...]
    Create local .kbagent/ workspace. --from-global copies existing projects;
    --project ALIAS (repeatable) copies only the named project(s) and implies
    --from-global.

  kbagent context
    Show this reference text.

  kbagent serve [--host HOST] [--port PORT] [--ui] [--ui-dist PATH] [--reload]
                [--log-level LVL] [--cors-origin ORIGIN] [--config-dir DIR]
    Launch the FastAPI HTTP server backing the web UI. Two modes:

    - `--ui` (single-process, recommended): bundles the built React SPA from
      `--ui-dist PATH` (default: shipped `web/frontend/dist`) and mounts it at
      `/`. The bearer token is injected via an HttpOnly `kbagent_session`
      cookie on the SPA bootstrap, so the browser is already authenticated
      and no token leaves the terminal. EventSource SSE connections use the
      same cookie; nothing leaks into URLs or proxy logs.
    - No `--ui`: API-only mode; the SPA must be served separately (the
      legacy three-process dev setup with web/backend + web/frontend).

    Prints the bearer token to stdout on startup (use it for `kbagent http`
    subprocesses). Requires the optional 'server' extra:
    `uv pip install -e ".[server]"`.

  kbagent doctor [--fix]
    Health checks. --fix auto-installs MCP server binary. Inside a sync working
    tree, the sync_secrets check (since 0.55.0) warns about in-sync configs that
    still hold plaintext #-secrets (#378); skipped outside a sync tree.

  kbagent version [--beta]
    Version info for kbagent + keboola-mcp-server. Reports both the locally
    installed version and the latest available; flags any staleness.
    --beta (since 0.42.0) reports the latest pre-release (beta / rc) instead
    of the latest stable. Same env override: KBAGENT_INCLUDE_PRERELEASE=1.

  kbagent update [--beta]
    Two-stage upgrade (since 0.30.1): kbagent itself AND keboola-mcp-server.
    The MCP server is detected (uv tool / pip env / uvx) and bumped via the
    matching command. Both stages always run, regardless of whether kbagent
    itself needed an upgrade. The same flow runs automatically on every
    kbagent startup -- the explicit `update` command forces a fresh check.
    --beta (since 0.42.0) opts into pre-release versions (PEP 440 betas/rc,
    e.g. 0.43.0b1). Without --beta the auto-update path uses GitHub's
    /releases/latest endpoint, which excludes prereleases server-side --
    stable users never silently land on a beta. Set
    KBAGENT_INCLUDE_PRERELEASE=1 in env to make every update in the session
    treat betas as installable without re-typing --beta.

  kbagent changelog [--limit N] [--full]
    Show recent changelog (what changed in each version). Default: last 5
    versions, one-line summary each; --full (-v) expands every note.

  kbagent permissions list [--category read|write|destructive|admin]
    List all operations with risk categories and current allowed/denied status.

  kbagent permissions show
    Show current active permission policy.

  kbagent permissions set --mode allow|deny [--allow PATTERN ...] [--deny PATTERN ...]
    Set firewall-style permission policy. Patterns: exact (branch.delete),
    glob (sync.*), category (cli:write, tool:read).

  kbagent permissions reset
    Remove all restrictions.

  kbagent permissions check OPERATION
    Check if operation is allowed. Exit 0=allowed, 6=denied. Reflects the
    EFFECTIVE policy: persisted policy MERGED with --deny-writes /
    --deny-destructive session flags (since 0.30.5; pre-0.30.5 consulted
    only the persisted policy and could mislead self-introspection).

## Tips for AI Agents

1. ALWAYS use --json flag for reliable, parseable output:
     kbagent --json project list

2. JSON response format:
     Success: {{"status": "ok", "data": ...}}
     Error:   {{"status": "error", "error": {{"code": "...", "message": "...", "retryable": true/false}}}}
   Check "retryable" -- if true, retry the operation.

3. Multi-project: most read commands accept repeatable --project flag.
   Omit --project to query ALL connected projects in parallel.

4. Tokens are always masked in output (e.g. 901-...XXXX) -- expected behavior.

5. Common workflow -- explore a project:
     kbagent --json project list
     kbagent --json config list --project prod
     kbagent --json config detail --project prod --component-id ID --config-id ID
     kbagent --json job list --project prod --status error --limit 10

6. Health check and setup:
     kbagent --json doctor           # full health check
     kbagent doctor --fix            # auto-install MCP server
     kbagent --json project status   # test all connections

7. Environment variables:
     KBAGENT_CONVERSATION_ID  Conversation/session ID (REQUIRED -- sent as X-Conversation-ID header)
     KBC_TOKEN                Storage API token (fallback for --token)
     KBC_STORAGE_API_URL      Default stack URL (fallback for --url)
     KBC_MANAGE_API_TOKEN     Manage API token (org setup, project refresh, data-app password).
                              Default-DENY since 0.29.0: pass --allow-env-manage-token
                              to opt in, otherwise this var is ignored and a TTY prompt
                              is required. Closes AI-exfiltration via subprocess env.
     KBC_MASTER_TOKEN         Master token for sharing ops (global fallback)
     KBC_MASTER_TOKEN_*       Per-project master token (e.g. KBC_MASTER_TOKEN_PROD)
     KBAGENT_CONFIG_DIR       Override config directory
     KBAGENT_PROJECT          Override the pinned default project for this shell/session (beats pin, loses to --project)
     KBAGENT_PROJECT_FROM_ENV Set to "1" (or true/yes/on) to synthesize an in-memory project under the
                              reserved alias __env__ from KBC_TOKEN + KBC_STORAGE_API_URL (since 0.50.0).
                              Headless / token-only mode: no `project add`, no config.json on disk. Use
                              `--project __env__` (or rely on it as the sole/default project). The token
                              lives in memory only -- it is NEVER persisted, even if a write op runs.
                              Works for both the CLI and `kbagent serve`. Fails fast if the flag is set
                              but KBC_TOKEN / KBC_STORAGE_API_URL are missing.
     KBAGENT_MAX_PARALLEL_WORKERS  Max concurrent threads for multi-project ops (default 10, max 100)
     KBAGENT_AUTO_UPDATE      Set to "false" to disable automatic update on startup
     KBAGENT_UPDATE_TIMEOUT   Integer seconds; overrides the 300s self-update subprocess timeout
                              (raise for slow WSL git+ source builds). Since 0.60.0 install/update
                              prefer a prebuilt wheel Release asset, so timeouts are rare.
     KBAGENT_UPDATED_FROM     Set to an older version to trigger "What's new" display on next run
     KBAGENT_MCP_TRANSPORT    MCP transport mode: "http" (default, persistent) or "stdio" (subprocess)
     KBAGENT_INCLUDE_PRERELEASE  Set to "1" (or "true"/"yes"/"on") to opt into pre-release versions for
                              `kbagent update` / `kbagent version` in this shell (equivalent to --beta flag,
                              since 0.43.3). NEVER affects the startup auto-update hook -- that path is
                              stable-channel-only by design so betas stay an explicit per-invocation choice.

8. Config resolution order:
     --config-dir flag > KBAGENT_CONFIG_DIR env > .kbagent/ in CWD/parents > ~/.config/keboola-agent-cli/

9. MCP tool parameters -- discover with `kbagent --json tool list`:
     - Only pass parameters defined in the tool's inputSchema via --input
     - branch_id is a CLI flag (--branch), NOT a tool input parameter
     - Example: get_configs uses "configs" (list of objects), not flat "config_id"
       kbagent --json tool call get_configs --project prod --branch 456 \\
         --input '{{"configs": [{{"component_id": "keboola.snowflake-transformation", "configuration_id": "12345"}}]}}'

10. Parquet export (typed analytics data, no CSV round-trip):
     # Export + download as Parquet dataset (default layout mirrors Keboola addressing)
     kbagent storage unload-table --project prod \\
       --table-id in.c-my-bucket.my-table \\
       --file-type parquet --download
     # -> ./prod/in.c-my-bucket.my-table.parquet/
     #      ├── <slice>.parquet
     #      └── _manifest.json   (underscore -> skipped by pyarrow/Spark/DuckDB)

     # Read the whole dataset in one line -- directory is a valid Parquet dataset:
     # python: pyarrow.parquet.read_table("./prod/in.c-my-bucket.my-table.parquet/")

     # Export only (stays in Keboola Storage Files, no local copy):
     kbagent --json storage unload-table --project prod \\
       --table-id in.c-bucket.t --file-type parquet --tag daily
     # -> {{"file_id": 123, "file_type": "parquet", "is_sliced": true, ...}}

     # Download an existing sliced .parquet Storage File (auto-detected):
     kbagent storage file-download --project prod --file-id 123 --output ./dir/

     Notes:
     - Parquet output is ALWAYS sliced -> directory, never a single file.
     - Default download path: ./{{project_alias}}/{{table_id}}.parquet/ -- override with --output.
     - CSV concat logic is never used for parquet; slices have their own footers.

## Exit Codes

  0  Success
  1  General error
  2  Usage error (invalid arguments)
  3  Authentication error (invalid or expired token)
  4  Network error (timeout, unreachable server)
  5  Configuration error (corrupt config, missing alias)
  6  Permission denied (operation blocked by policy)

When you receive a non-zero exit code, use --json to get structured error details.

## Claude Code Plugin

If you are using Claude Code, install the kbagent plugin for richer guidance:

  /plugin marketplace add keboola/cli
  /plugin install kbagent@keboola-agent-cli

The plugin provides a skill with detailed workflow references including:
- SQL transformation migration (input mapping removal, Snowflake paths)
- Workspace SQL debugging
- Development branch lifecycle
- Configuration scaffolding and sync (GitOps)
- Common Snowflake gotchas (MULTI_STATEMENT_COUNT, quoting, etc.)

The skill triggers automatically when you mention Keboola-related tasks.
Without the plugin, this `kbagent context` output is your standalone reference.
"""


def context_command(ctx: typer.Context) -> None:
    """Show usage instructions for AI agents interacting with Keboola."""
    formatter = get_formatter(ctx)

    if formatter.json_mode:
        # In JSON mode, output the context text as structured data
        data = {
            "version": __version__,
            "context": AGENT_CONTEXT,
        }
        formatter.output(data)
    else:
        formatter.console.print(AGENT_CONTEXT)
