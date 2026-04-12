# kbagent Command Reference

All commands support `--json` for structured output. Multi-project flags (`--project`) can be repeated.

## Setup & Info
- `init [--from-global]` -- create local `.kbagent/` workspace in current directory
- `doctor [--fix]` -- health check for CLI config and MCP server
- `version` -- show version info and dependency update status
- `update` -- self-update to latest version
- `changelog [--limit N]` -- show recent changelog (default: last 5 versions). After auto-update, "What's new" is printed automatically. Manual trigger: `KBAGENT_UPDATED_FROM=0.17.0 kbagent version`
- `context` -- print full CLI reference for AI agents

## Project Management
- `project add --project NAME --url URL --token TOKEN` -- connect a project (token verified via API)
- `project list` -- list all connected projects (tokens masked)
- `project remove --project NAME` -- disconnect a project
- `project edit --project NAME [--url URL] [--token TOKEN]` -- update connection details
- `project status [--project NAME]` -- test connectivity and response time

## Organization
- `org setup --org-id ID --url URL [--dry-run] [--yes]` -- bulk-onboard all projects from an org (org admin, needs `KBC_MANAGE_API_TOKEN`)
- `org setup --project-ids 1,2,3 --url URL [--dry-run] [--yes]` -- onboard specific projects by ID (any project member, works with Personal Access Token via `KBC_MANAGE_API_TOKEN`)

## Component Discovery
- `component list [--project NAME] [--type TYPE] [--query "text"]` -- list/search components (AI-powered with `--query`)
- `component detail --component-id ID [--project NAME]` -- show component schema, docs URL, examples

## Configuration Browsing
- `config list [--project NAME] [--component-type TYPE] [--component-id ID] [--branch ID]` -- list configs across projects (branch-aware)
- `config detail --project NAME --component-id ID --config-id ID [--branch ID]` -- full config with parameters and rows (branch-aware)
- `config search --query PATTERN [--project NAME] [-i] [-r] [--branch ID]` -- search config bodies for string/regex (branch-aware)
- `config update --project NAME --component-id ID --config-id ID [--name N] [--description D]` -- update name/description
- `config delete --project NAME --component-id ID --config-id ID [--branch ID]` -- delete a configuration
- `config new --component-id ID [--project NAME] [--name NAME] [--output-dir DIR]` -- scaffold new config from component schema

## Job History
- `job list [--project NAME] [--component-id ID] [--config-id ID] [--status STATUS] [--limit N]` -- list jobs (default 50, max 500)
- `job detail --project NAME --job-id ID` -- full job detail with timing and result message

## Storage
- `storage buckets [--project NAME] [--branch ID]` -- list buckets with sharing/linked info (branch-aware)
- `storage bucket-detail --project NAME --bucket-id ID [--branch ID]` -- bucket detail with Snowflake paths (branch-aware)
- `storage tables --project NAME [--bucket-id ID] [--branch ID]` -- list tables, optionally by bucket (branch-aware)
- `storage table-detail --project NAME --table-id ID [--branch ID]` -- table detail with columns, types, primary key, row count (branch-aware)
- `storage create-bucket --project NAME --stage STAGE --name NAME [--description D] [--backend B] [--branch ID]` -- create bucket (branch-aware)
- `storage create-table --project NAME --bucket-id ID --name NAME --column COL:TYPE [...] [--primary-key COL] [--branch ID]` -- create typed table (branch-aware)
- `storage upload-table --project NAME --table-id ID --file PATH [--incremental] [--branch ID]` -- upload CSV (branch-aware)
- `storage download-table --project NAME --table-id ID [--output FILE] [--columns COL ...] [--limit N] [--branch ID]` -- export table to CSV (branch-aware)
- `storage delete-table --project NAME --table-id ID [--table-id ...] [--dry-run] [--yes] [--branch ID]` -- delete tables (branch-aware)
- `storage delete-bucket --project NAME --bucket-id ID [--bucket-id ...] [--force] [--dry-run] [--yes] [--branch ID]` -- delete buckets (branch-aware)

## Data Lineage
- `lineage [--project NAME]` -- cross-project data flow via bucket sharing

## Development Branches
- `branch list [--project NAME]` -- list dev branches
- `branch create --project ALIAS --name "..." [--description "..."]` -- create and auto-activate branch
- `branch use --project ALIAS --branch ID` -- switch active branch
- `branch reset --project ALIAS` -- reset to main/production
- `branch delete --project ALIAS --branch ID` -- delete branch (resets if active)
- `branch merge --project ALIAS [--branch ID]` -- get merge URL (does NOT merge via API)

## Workspaces (SQL Debugging)
- `workspace create --project ALIAS [--name NAME] [--ui] [--read-only]` -- create workspace (headless ~1s, `--ui` ~15s)
- `workspace list [--project NAME]` -- list workspaces
- `workspace detail --project ALIAS --workspace-id ID` -- show connection details
- `workspace delete --project ALIAS --workspace-id ID` -- delete workspace
- `workspace password --project ALIAS --workspace-id ID` -- reset and return new password
- `workspace load --project ALIAS --workspace-id ID --tables TABLE_ID [...] [--preserve]` -- load storage tables
- `workspace query --project ALIAS --workspace-id ID --sql "..." [--file F] [--transactional]` -- run SQL via Query Service
- `workspace from-transformation --project ALIAS --component-id ID --config-id ID [--row-id ID]` -- workspace from existing transform

## MCP Tools
- `tool list [--project NAME] [--branch ID]` -- list available MCP tools (multi_project annotation)
- `tool call TOOL_NAME [--project NAME] [--input JSON|@file|-] [--branch ID]` -- call MCP tool (read = all projects, write = single). `--input` accepts inline JSON, `@file.json`, or `-` (stdin)

## Sync (GitOps)
- `sync init --project ALIAS [--directory DIR] [--git-branching]` -- initialize sync working directory
- `sync pull --project ALIAS [--all-projects] [--force] [--dry-run] [--with-samples] [--no-storage] [--no-jobs] [--job-limit N]` -- download configs to local files. For large projects (>100 configs), automatically fetches jobs per-config when the grouped API limit is insufficient
- `sync push --project ALIAS [--all-projects] [--dry-run] [--force] [--allow-plaintext-on-encrypt-failure]` -- push local changes (auto-encrypts secrets, fails if encryption fails)
- `sync diff --project ALIAS [--all-projects]` -- 3-way diff (local vs base vs remote), detects conflicts
- `sync status [--directory DIR]` -- show locally modified/added/deleted configs
- `sync branch-link --project ALIAS [--branch-id ID] [--branch-name NAME]` -- link git branch to Keboola dev branch
- `sync branch-unlink [--directory DIR]` -- remove git-to-Keboola branch mapping
- `sync branch-status [--directory DIR]` -- show current branch mapping

## Encryption
- `encrypt values --project ALIAS --component-id ID --input JSON|@file|- [--output-file PATH]` -- encrypt #-prefixed secrets via Keboola Encryption API (one-way, no decrypt). Scope: ComponentSecure (project + component). Use for MCP tool call workflows.

## Utility
- `init [--from-global]` -- create local `.kbagent/` workspace (per-directory isolation)
- `doctor [--fix]` -- health checks; `--fix` auto-installs MCP server binary
- `version` -- show version and check for MCP server updates
- `context` -- full usage instructions for AI agents

## Global Flags
| Flag | Description |
|------|-------------|
| `--json / -j` | Structured JSON output |
| `--verbose / -v` | Verbose output |
| `--no-color` | Disable colors |
| `--config-dir` | Override config directory |

## Environment Variables
| Variable | Purpose |
|----------|---------|
| `KBC_TOKEN` | Fallback for `--token` |
| `KBC_STORAGE_API_URL` | Default stack URL |
| `KBC_MANAGE_API_TOKEN` | Manage API token (org setup) |
| `KBAGENT_CONFIG_DIR` | Override config directory |

## Exit Codes
`0` success, `1` general error, `2` usage error, `3` auth error, `4` network error, `5` config error
