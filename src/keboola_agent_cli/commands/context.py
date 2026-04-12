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
  KBC_MANAGE_API_TOKEN=xxx kbagent --json org setup --org-id 123 --url https://connection.keboola.com --yes

  # Explore
  kbagent --json project list
  kbagent --json config list

## Global Flags

  --json / -j     JSON output (always use for programmatic parsing)
  --verbose / -v  Verbose output
  --no-color      Disable colors (auto-disabled in non-TTY)
  --config-dir    Override config directory path

## All Commands

Use `kbagent <command> --help` for full flag details and examples.

### Project Management

  kbagent project add --project NAME --url URL --token TOKEN
    Add a new project connection. Token verified against API.

  kbagent project list
    List all connected projects (tokens always masked).

  kbagent project remove --project NAME
    Remove a project connection.

  kbagent project edit --project NAME [--url URL] [--token TOKEN]
    Edit project connection. Re-verifies token if changed.

  kbagent project status [--project NAME]
    Test connectivity. Shows OK/ERROR with response time.

  kbagent project refresh --project ALIAS [--dry-run] [--force] [--yes] [--token-description ...] [--token-expires-in N]
  kbagent project refresh --all [--dry-run] [--force] [--yes] [--token-description ...] [--token-expires-in N]
    Refresh project tokens via Manage API. --all refreshes all projects. --force replaces non-expiring tokens.

### Component Discovery

  kbagent component list [--project NAME] [--type TYPE] [--query "search"]
    List or AI-search available components. --type: extractor, writer, transformation, application.

  kbagent component detail --component-id ID [--project NAME]
    Show component docs, config schema, and examples count.

### Configuration Browsing

  kbagent config list [--project NAME] [--component-type TYPE] [--component-id ID] [--branch ID]
    List configs from one/many/all projects. --project repeatable. Branch-aware.

  kbagent config detail --project NAME --component-id ID --config-id ID [--branch ID]
    Full config detail including parameters and rows. Branch-aware.

  kbagent config update --project NAME --component-id ID --config-id ID [--name N] [--description D] [--branch ID]
    Update config name/description. Targets active dev branch if set.

  kbagent config delete --project NAME --component-id ID --config-id ID [--branch ID]
    Delete a configuration. Branch-aware.

  kbagent config new --component-id ID [--name NAME] [--project NAME] [--output-dir DIR]
    Generate boilerplate config from component schema. Use --output-dir to write files.

  kbagent config search --query PATTERN [--project NAME] [--component-type TYPE] [-i] [-r] [--branch ID]
    Search config bodies for string/regex. Reports match location in JSON tree. Branch-aware.

### Job History

  kbagent job list [--project NAME] [--component-id ID] [--config-id ID] [--status STATUS] [--limit N]
    List jobs from Queue API. --status: processing, terminated, cancelled, success, error.

  kbagent job detail --project NAME --job-id ID
    Full job detail including result message and timing.

  kbagent job run --project NAME --component-id ID --config-id ID [--row-id ID ...] [--wait] [--timeout N]
    Run a Queue API job. --row-id selects specific config rows (repeatable; omit to run entire config).
    --wait polls until job finishes. --timeout sets max wait in seconds (default 300).

### Storage

  kbagent storage buckets [--project NAME] [--branch ID]
    List buckets with sharing/linked info. Shows source project for linked buckets. Branch-aware.

  kbagent storage bucket-detail --project NAME --bucket-id BUCKET_ID [--branch ID]
    Bucket detail with Snowflake direct access paths. Resolves linked bucket source DB. Branch-aware.

  kbagent storage tables --project NAME [--bucket-id BUCKET_ID] [--branch ID]
    List storage tables, optionally filtered by bucket. Branch-aware.

  kbagent storage table-detail --project NAME --table-id TABLE_ID [--branch ID]
    Show detailed table info: columns (with types if available), primary key, row count, size, last import date. Branch-aware.

  kbagent storage create-bucket --project NAME --stage STAGE --name BUCKET_NAME [--description D] [--backend B] [--branch ID]
    Create a new storage bucket. Stage must be "in" or "out". Branch-aware.

  kbagent storage create-table --project NAME --bucket-id BUCKET_ID --name TABLE_NAME --column col:TYPE [...] [--primary-key COL] [--branch ID]
    Create a typed table. --column repeatable. Types: STRING, INTEGER, NUMERIC, FLOAT, BOOLEAN, DATE, TIMESTAMP.
    Column type defaults to STRING if omitted (e.g. --column name is equivalent to --column name:STRING). Branch-aware.

  kbagent storage upload-table --project NAME --table-id TABLE_ID --file PATH [--incremental] [--delimiter D] [--enclosure E] [--no-auto-create] [--branch ID]
    Upload CSV into a table. Auto-creates bucket and table if missing (columns inferred as STRING from CSV header).
    Use --no-auto-create to require the table to already exist.
    Full load by default; --incremental to append rows. Supports files up to 5 GB via async file-first upload flow. Branch-aware.

  kbagent storage download-table --project NAME --table-id TABLE_ID [--output FILE] [--columns COL ...] [--limit N] [--branch ID]
    Export table data to a local CSV file. Async export with streaming download.
    Default filename: TABLE_NAME.csv. Use --columns to select columns (see table-detail for names).
    Use --limit to cap row count. Handles sliced files and gzip decompression transparently. Branch-aware.

  kbagent storage delete-table --project NAME --table-id ID [--table-id ...] [--dry-run] [--yes] [--branch ID]
    Delete one or more tables. Batch: repeat --table-id. --dry-run to preview. Branch-aware.

  kbagent storage delete-bucket --project NAME --bucket-id ID [--bucket-id ...] [--force] [--dry-run] [--yes] [--branch ID]
    Delete one or more buckets. --force cascade-deletes tables. Linked/shared buckets protected. Branch-aware.

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

### Data Lineage

  kbagent lineage [--project NAME]
    Cross-project data lineage via bucket sharing. --project repeatable.

### Organization Management

  kbagent org setup --org-id ID --url URL [--dry-run] [--yes] [--token-description PREFIX] [--refresh]
    Bulk-onboard all org projects. Requires org-admin manage token. Idempotent.
    --refresh also refreshes tokens for already-registered projects with invalid tokens.

  kbagent org setup --project-ids 901,9621,10539 --url URL [--dry-run] [--yes] [--refresh]
    Non-admin mode: onboard specific projects by ID. Works with Personal Access Token (PAT).
    Use --org-id OR --project-ids (at least one required).
    Token via KBC_MANAGE_API_TOKEN env var or interactive prompt.

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

### Workspaces (SQL Debugging)

  kbagent workspace create --project ALIAS [--name NAME] [--backend TYPE] [--ui] [--read-only/--no-read-only]
    Create workspace. Backend auto-detected from project (or override with --backend). Default: headless (~1s). --ui: visible in KBC UI (~15s).

  kbagent workspace list [--project NAME]
    List workspaces. --project repeatable.

  kbagent workspace detail --project ALIAS --workspace-id ID
    Workspace connection details (no password).

  kbagent workspace delete --project ALIAS --workspace-id ID
    Delete workspace. They also expire automatically.

  kbagent workspace password --project ALIAS --workspace-id ID
    Reset and return new workspace password.

  kbagent workspace load --project ALIAS --workspace-id ID --tables TABLE_ID [...] [--preserve]
    Load storage tables into workspace. --preserve keeps existing tables.

  kbagent workspace query --project ALIAS --workspace-id ID --sql "SQL" [--file F] [--transactional]
    Execute SQL via Query Service. No Snowflake credentials needed.

  kbagent workspace from-transformation --project ALIAS --component-id ID --config-id ID [--row-id ID]
    Create workspace from transformation config. Loads input tables automatically.

### Project Sync

  kbagent sync init --project ALIAS [--directory DIR] [--git-branching]
    Initialize sync working directory. --git-branching enables git-to-Keboola branch mapping.

  kbagent sync pull --project ALIAS [--all-projects] [--force] [--dry-run] [--with-samples] [--no-storage] [--no-jobs] [--job-limit N]
    Download configs as local files. Idempotent, protects local modifications.
    --job-limit controls max recent jobs per config (default 5). For large projects,
    automatically falls back to per-config job fetching to ensure all configs get job history.

  kbagent sync status [--directory DIR]
    Show local changes since last pull (SHA256-based).

  kbagent sync diff --project ALIAS [--all-projects] [--directory DIR]
    3-way diff: local vs pull-time snapshot vs remote. Detects conflicts.

  kbagent sync push --project ALIAS [--all-projects] [--dry-run] [--force] [--allow-plaintext-on-encrypt-failure]
    Push local changes. Auto-encrypts secrets. Skips conflicts (pull first).
    Fails if encryption fails (plaintext secrets never pushed). Use escape hatch flag only if you know what you are doing.

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

### MCP Tools (Multi-Project)

  kbagent tool list [--project NAME] [--branch ID]
    List MCP tools with inputSchema. Use --json to inspect accepted parameters.

  kbagent tool call TOOL_NAME [--project NAME] [--input JSON|@file|-] [--branch ID]
    Call an MCP tool. Read tools auto-query all projects. Write tools need --project.
    --input accepts: inline JSON, @file.json (from file), or - (from stdin).
    --branch is a CLI flag (NOT a tool input param). Do not pass branch_id in --input.

### Utility Commands

  kbagent init [--from-global]
    Create local .kbagent/ workspace. --from-global copies existing projects.

  kbagent context
    Show this reference text.

  kbagent doctor [--fix]
    Health checks. --fix auto-installs MCP server binary.

  kbagent version
    Version info, update check for kbagent and MCP server.

  kbagent update
    Self-update kbagent to latest version (via uv tool install --upgrade).

  kbagent changelog [--limit N]
    Show recent changelog (what changed in each version). Default: last 5 versions.

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
    Check if operation is allowed. Exit 0=allowed, 6=denied.

## Tips for AI Agents

1. ALWAYS use --json flag for reliable, parseable output:
     kbagent --json project list

2. JSON response format:
     Success: {{"status": "ok", "data": ...}}
     Error:   {{"status": "error", "error": {{"code": "...", "message": "...", "retryable": true/false}}}}
   Check "retryable" -- if true, retry the operation.

3. Multi-project: most read commands accept repeatable --project flag.
   Omit --project to query ALL connected projects in parallel.

4. Tokens are always masked in output (e.g. 901-...pt0k) -- expected behavior.

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
     KBC_MANAGE_API_TOKEN     Manage API token (for org setup)
     KBC_MASTER_TOKEN         Master token for sharing ops (global fallback)
     KBC_MASTER_TOKEN_*       Per-project master token (e.g. KBC_MASTER_TOKEN_PROD)
     KBAGENT_CONFIG_DIR       Override config directory
     KBAGENT_MAX_PARALLEL_WORKERS  Max concurrent threads for multi-project ops (default 10, max 100)
     KBAGENT_AUTO_UPDATE      Set to "false" to disable automatic update on startup
     KBAGENT_UPDATED_FROM     Set to an older version to trigger "What's new" display on next run
     KBAGENT_MCP_TRANSPORT    MCP transport mode: "http" (default, persistent) or "stdio" (subprocess)

8. Config resolution order:
     --config-dir flag > KBAGENT_CONFIG_DIR env > .kbagent/ in CWD/parents > ~/.config/keboola-agent-cli/

9. MCP tool parameters -- discover with `kbagent --json tool list`:
     - Only pass parameters defined in the tool's inputSchema via --input
     - branch_id is a CLI flag (--branch), NOT a tool input parameter
     - Example: get_configs uses "configs" (list of objects), not flat "config_id"
       kbagent --json tool call get_configs --project prod --branch 456 \\
         --input '{{"configs": [{{"component_id": "keboola.snowflake-transformation", "configuration_id": "12345"}}]}}'

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

  /plugin marketplace add padak/keboola_agent_cli
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
