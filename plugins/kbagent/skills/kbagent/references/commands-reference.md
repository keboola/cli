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
- `project description-get --project NAME` -- read the dashboard project description (KBC.projectDescription on the default branch). Returns `{"description": ""}` if not set, not an error
- `project description-set --project NAME [--text STR | --file PATH | --stdin]` -- set the dashboard project description (markdown). Pass exactly one of `--text`, `--file`, or `--stdin`. Writes to `KBC.projectDescription` on the default branch -- always the main branch, regardless of any active dev branch
- `project use ALIAS` -- pin `ALIAS` as the persistent default project. Stored as `default_project` in config.json. Overridden at runtime by `KBAGENT_PROJECT=ALIAS` (env, beats pin) and by `--project ALIAS` (CLI flag, beats both)
- `project current` -- print the effective default project and its source (`env` / `pin` / `none`). Reports both the env override AND the persisted pin so misconfigurations are visible. Returns `{"alias": null, "source": "none"}` when neither is set

## Permission flags (top-level, session-only)
- `--deny-writes` -- block all write/destructive/admin operations for this single invocation. Merges with any persisted permission policy; never written to config.json. Exit code 6 (PERMISSION_DENIED) on blocked operations
- `--deny-destructive` -- block only destructive operations (delete-table, delete-bucket, terminate-job, etc.) for this invocation. Pure-write ops like create-table stay allowed. Use this when you want to keep build-up capabilities but lock out tear-downs
- Both flags compose: `kbagent --deny-writes --deny-destructive ...` is the safest read-only run

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
- `config update --project NAME --component-id ID --config-id ID [--name N] [--description D] [--configuration JSON|@file|-] [--configuration-file PATH] [--set PATH=VALUE ...] [--merge] [--dry-run] [--branch ID]` -- update metadata and/or configuration content. `--set` targets a nested key (e.g. `parameters.db.host=new-host`). `--merge` deep-merges into existing config (preserves sibling keys). `--dry-run` previews changes without applying. Paths are relative to the configuration root (unlike MCP's `update_config` which uses paths relative to `parameters`)
- `config rename --project NAME --component-id ID --config-id ID --name "New Name" [--branch ID] [--directory DIR]` -- rename a configuration (API update + local sync directory rename with git mv support)
- `config delete --project NAME --component-id ID --config-id ID [--branch ID]` -- delete a configuration
- `config new --component-id ID [--project NAME] [--name NAME] [--output-dir DIR]` -- scaffold new config from component schema
- `config variables-set --project NAME --component-id ID --config-id ID --var KEY=VALUE [--var ...] [--replace] [--variables-id ID] [--values-id ID] [--branch ID] [--dry-run] [--allow-plaintext-on-encrypt-failure] [--yes]` -- attach variable values to a config. Auto-creates a sibling `keboola.variables` config + default row on first use and links it via the parent's `runtime.variables_id` / `variables_values_id`. Defaults to merge; `--replace` drops keys not in `--var`. `#`-prefixed values encrypt via the Encryption API (fail-closed; exit non-zero on `ENCRYPTION_FAILED`). See `variables-workflow.md`
- `config variables-get --project NAME --component-id ID --config-id ID [--branch ID]` -- resolve `variables_id` + `values_id` from the parent config and fetch the current KEY=VALUE map. Returns `{linked: bool, variables_id, values_id, values}`; `linked=false` means the parent has no variables attached
- `config variables-clear --project NAME --component-id ID --config-id ID [--branch ID] [--yes]` -- unlink variables from the parent config (strips `variables_id` + `variables_values_id`). **Does NOT delete** the backing `keboola.variables` config -- use `config delete` explicitly if you've verified nothing else references it
- `config metadata-list --project NAME --component-id ID --config-id ID [--branch ID]` -- list all metadata entries on a configuration (id, key, value, provider, timestamp). Branch-aware
- `config get-metadata --project NAME --component-id ID --config-id ID --key KEY [--branch ID]` -- read a single metadata value by key. Exits with `NOT_FOUND` (exit 1) if absent
- `config set-metadata --project NAME --component-id ID --config-id ID --key KEY --value VALUE [--branch ID]` -- set (upsert) a metadata key/value on a configuration. Common keys: `KBC.configuration.folderName`, plus any custom `KBC.*` agent-facing tags
- `config delete-metadata --project NAME --component-id ID --config-id ID --metadata-id ID [--branch ID] [--yes]` -- delete a configuration metadata entry by its numeric ID (from `metadata-list`)
- `config set-folder --project NAME --component-id ID --config-id ID --name FOLDER [--branch ID]` -- set (or clear, with empty `--name`) the `KBC.configuration.folderName` metadata, which groups configs into named folders in the Keboola UI. See `config-metadata-workflow.md`

## Job History
- `job list [--project NAME] [--component-id ID] [--config-id ID] [--status STATUS] [--limit N]` -- list jobs (default 50, max 500)
- `job detail --project NAME --job-id ID` -- full job detail with timing and result message
- `job run --project NAME --component-id ID --config-id ID [--row-id ID ...] [--wait] [--timeout N] [--branch ID] [--variable-values-id ID] [--no-variables]` -- run a job, optionally wait for completion (branch-aware). For configs with linked `keboola.variables` (root-level `configuration.variables_id`), kbagent auto-resolves a `variableValuesId` so transformations bind to the deployed values row. `--variable-values-id` overrides; `--no-variables` skips resolution. `NO_VARIABLE_ROWS` when the linked variables config has zero rows -- fix via `kbagent config variables-set`.
- `job terminate --project NAME (--job-id ID [--job-id ...] | --status any|created|waiting|processing [--component-id ID] [--config-id ID] [--branch ID] [--limit N]) [--dry-run] [--yes]` -- kill running Queue API jobs. Use to stop runaway loops or clean up pile-ups from repeated `job run` calls. Two modes: by ID (single/batch) or by filter (`--status any` catches every killable state). Response partitions IDs into `killed / already_finished / not_found / failed`; safe to re-run idempotently. Kill is async -- poll `job detail` for `isFinished=true`.

## Storage
- `storage buckets [--project NAME] [--branch ID]` -- list buckets with sharing/linked info (branch-aware)
- `storage bucket-detail --project NAME --bucket-id ID [--branch ID]` -- bucket detail with Snowflake paths (branch-aware)
- `storage tables [--project NAME ...] [--bucket-id ID] [--branch ID]` -- list tables across all connected projects in parallel (multi-project by default, same as `storage buckets`); repeat `--project` to target a subset; `--bucket-id` is applied independently per project (missing buckets become per-project errors); `--branch` requires exactly one `--project`
- `storage table-detail --project NAME --table-id ID [--branch ID]` -- table detail with columns, types, primary key, row count (branch-aware)
- `storage create-bucket --project NAME --stage STAGE --name NAME [--description D] [--backend B] [--branch ID]` -- create bucket (branch-aware)
- `storage create-table --project NAME --bucket-id ID --name NAME --column COL:TYPE [...] [--primary-key COL] [--branch ID]` -- create typed table (branch-aware)
- `storage upload-table --project NAME --table-id ID --file PATH [--incremental] [--branch ID]` -- upload CSV (branch-aware)
- `storage download-table --project NAME --table-id ID [--output FILE] [--columns COL ...] [--limit N] [--branch ID]` -- export table to CSV (branch-aware)
- `storage delete-table --project NAME --table-id ID [--table-id ...] [--force] [--dry-run] [--yes] [--branch ID]` -- delete tables, --force cascade-deletes aliased tables (branch-aware)
- `storage delete-column --project NAME --table-id ID --column COL [--column ...] [--force] [--dry-run] [--yes] [--branch ID]` -- delete columns from a table (branch-aware)
- `storage delete-bucket --project NAME --bucket-id ID [--bucket-id ...] [--force] [--dry-run] [--yes] [--branch ID]` -- delete buckets (branch-aware)

## Storage Files
- `storage files --project NAME [--tag TAG ...] [--limit N] [--offset N] [--query Q] [--branch ID]` -- list Storage Files, optionally filtered by tag/query
- `storage file-detail --project NAME --file-id ID` -- file metadata (size, tags, sliced, provider)
- `storage file-upload --project NAME --file PATH [--name NAME] [--tag TAG ...] [--permanent] [--branch ID]` -- upload a file to Storage
- `storage file-download --project NAME [--file-id ID | --tag TAG ...] [--output FILE|DIR]` -- download a Storage File. Auto-detects sliced `.parquet` files and writes per-slice into a directory (never concatenates -- parquet slices have their own footers)
- `storage file-tag --project NAME --file-id ID [--add TAG ...] [--remove TAG ...]` -- add/remove tags on a file
- `storage file-delete --project NAME --file-id ID [--file-id ...] [--dry-run] [--yes]` -- delete Storage Files
- `storage load-file --project NAME --file-id ID --table-id ID [--incremental] [--delimiter D] [--enclosure E] [--branch ID]` -- import a Storage File into a table (CSV)
- `storage unload-table --project NAME --table-id ID [--columns COL ...] [--limit N] [--tag TAG ...] [--download] [--output FILE|DIR] [--file-type csv|parquet] [--branch ID]` -- export a table to a Storage File. `--file-type parquet` produces sliced Parquet; `--download` saves each slice as its own file under `./{project}/{table_id}.parquet/` (default) together with `_manifest.json`

## Data Lineage
- `lineage build -d DIR -o FILE [--refresh] [--ai]` -- build column-level lineage graph from sync'd data
- `lineage show -l FILE --downstream "project:table" [--columns] [-c COL] [--format text|mermaid|html|er]` -- query downstream dependencies from cache
- `lineage show -l FILE --upstream "project:table" [--columns] [-c COL] [--format text|mermaid|html|er]` -- query upstream dependencies from cache
- `lineage info -l FILE` -- show graph contents: projects, tables, most connected nodes
- `lineage server -l FILE [--port N]` -- interactive lineage browser in web browser
- `sharing edges [--project NAME]` -- cross-project data flow edges via bucket sharing

## Development Branches
- `branch list [--project NAME]` -- list dev branches
- `branch create --project ALIAS --name "..." [--description "..."]` -- create and auto-activate branch
- `branch use --project ALIAS --branch ID` -- switch active branch
- `branch reset --project ALIAS` -- reset to main/production
- `branch delete --project ALIAS --branch ID` -- delete branch (resets if active)
- `branch merge --project ALIAS [--branch ID]` -- get merge URL (does NOT merge via API)
- `branch metadata-list --project NAME [--branch ID|default]` -- list all metadata entries on a branch (id, key, value, provider, timestamp). `--branch` defaults to `default` (main branch)
- `branch metadata-get --project NAME --key KEY [--branch ID|default]` -- read a single metadata value by key. Exits with `NOT_FOUND` (exit 1) if absent
- `branch metadata-set --project NAME --key KEY [--text STR | --file PATH | --stdin] [--branch ID|default]` -- set a key/value. Useful for `KBC.projectDescription` and similar dashboard-visible fields. Pass exactly one of `--text`, `--file`, or `--stdin`
- `branch metadata-delete --project NAME --metadata-id ID [--branch ID|default]` -- delete a metadata entry by its numeric ID (from `metadata-list`)

## Workspaces (SQL Debugging)
- `workspace create --project ALIAS [--name NAME] [--ui] [--read-only]` -- create workspace (headless ~1s, `--ui` ~15s)
- `workspace list [--project NAME ...] [--orphaned]` -- list workspaces. `--project` repeatable for multi-project; `--orphaned` filters to workspaces whose backing `keboola.sandboxes` config is missing
- `workspace detail --project ALIAS --workspace-id ID` -- show connection details
- `workspace delete --project ALIAS --workspace-id ID` -- delete workspace
- `workspace password --project ALIAS --workspace-id ID` -- reset and return new password
- `workspace load --project ALIAS --workspace-id ID --tables TABLE_ID [...] [--preserve]` -- load storage tables
- `workspace query --project ALIAS --workspace-id ID --sql "..." [--file F] [--transactional]` -- run SQL via Query Service
- `workspace gc [--project NAME ...] [--dry-run] [--yes]` -- garbage-collect orphaned workspaces (and any lingering `keboola.sandboxes` configs). `--dry-run` previews without deleting; `--project` repeatable, omit to GC across all connected projects
- `workspace from-transformation --project ALIAS --component-id ID --config-id ID [--row-id ID]` -- workspace from existing transform

## MCP Tools
- `tool list [--project NAME] [--branch ID]` -- list available MCP tools (multi_project annotation)
- `tool call TOOL_NAME [--project NAME] [--input JSON|@file|-] [--branch ID]` -- call MCP tool (read = all projects, write = single). `--input` accepts inline JSON, `@file.json`, or `-` (stdin)

## Kai (Keboola AI Assistant)
- `kai ping [--project NAME]` -- check Kai server health and MCP connection status
- `kai ask --message "question" [--project NAME]` -- one-shot question to Kai, collects full response
- `kai chat --message "msg" [--chat-id ID] [--project NAME]` -- send message in a chat session, returns chat_id for continuation
- `kai history [--project NAME] [--limit N]` -- list recent Kai chat sessions (default limit: 10)

## Flows (Orchestrator)
- `flow list [--project NAME] [--branch ID]` -- list all flows (keboola.orchestrator + keboola.flow) across one or all projects
- `flow detail --project NAME --flow-id ID [--component-id keboola.orchestrator|keboola.flow] [--branch ID]` -- full phase/task breakdown; groups tasks by phase, lists orphan tasks
- `flow schema` -- print YAML template for flow configuration (phases + tasks); use with `--file @-` or save to a file
- `flow new --project NAME --name NAME [--component-id keboola.orchestrator|keboola.flow] [--description D] [--file @path.yaml|-|JSON] [--branch ID]` -- create a flow; DAG validated before API call; default component: keboola.flow
- `flow update --project NAME --flow-id ID [--component-id ID] [--name N] [--description D] [--file @path.yaml|-|JSON] [--branch ID]` -- update name, description, or phases/tasks; requires at least one of --name/--description/--file
- `flow delete --project NAME --flow-id ID [--component-id ID] [--branch ID] [--yes]` -- delete a flow config (confirmation guard)
- `flow schedule --project NAME --flow-id ID --cron "0 6 * * *" [--component-id ID] [--timezone TZ] [--disabled] [--branch ID]` -- attach a cron schedule (stored as keboola.scheduler config); replaces any existing schedule
- `flow schedule-remove --project NAME --flow-id ID [--component-id ID] [--branch ID] [--yes]` -- remove all cron schedules attached to a flow; idempotent

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
| `--hint client\|service` | Generate Python code instead of executing (see [programming-with-cli.md](programming-with-cli.md)) |

## Environment Variables
| Variable | Purpose |
|----------|---------|
| `KBC_TOKEN` | Fallback for `--token` |
| `KBC_STORAGE_API_URL` | Default stack URL |
| `KBC_MANAGE_API_TOKEN` | Manage API token (org setup) |
| `KBAGENT_CONFIG_DIR` | Override config directory |

## Exit Codes
`0` success, `1` general error, `2` usage error, `3` auth error, `4` network error, `5` config error
