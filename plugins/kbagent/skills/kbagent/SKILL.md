---
name: kbagent
description: >
  Use when working with Keboola Connection projects via kbagent CLI.
  Covers: exploring and searching configurations (extractors, writers, transformations),
  browsing job history, analyzing cross-project data lineage, calling MCP tools
  across multiple projects, managing development branches, debugging SQL in
  temporary workspaces, bulk-onboarding organizations, syncing project configs
  as local files (GitOps), git-branching with Keboola dev branch isolation,
  sharing buckets across projects, linking shared data,
  encrypting secrets for MCP tool call workflows,
  uploading/downloading Storage Files with tag management,
  and syncing storage metadata and job history. Triggers: kbagent, Keboola project,
  keboola configs, keboola jobs, keboola lineage, keboola transformations,
  keboola MCP tools, keboola workspace, SQL debugging, keboola branches,
  keboola organization, keboola sharing, bucket sharing, link bucket,
  keboola sync, keboola git,
  keboola gitops, sync pull, sync push, sync diff, branch-link,
  search configs, find in configurations, audit configurations,
  input mapping migration, remove input mapping, Snowflake paths,
  MULTI_STATEMENT_COUNT, statement count error, SQL transformation migration,
  keboola encrypt, encrypt secrets, encrypt credentials, encrypt password,
  keboola encryption API, #password, #api_token, KBC::ProjectSecure,
  safe config write, dry-run preview, fresh fetch before edit,
  stale local config file, config version overwrite,
  default bucket, output bucket, default_bucket, storage.output,
  raw mode bucket override, custom output bucket name,
  data app, data apps, keboola data app, streamlit app, streamlit deployment,
  flask app, fastapi app, node app, python-js, deploy data app,
  data-app create, data-app deploy, data-app password, data-app start,
  app proxy, simpleAuth, app auto-suspend, configVersion, redeploy contract,
  Data Science API, /apps endpoint, app password, KBC::Project ciphertext,
  manage token, manage api token, KBC_MANAGE_API_TOKEN,
  KBC_MANAGE_TOKEN_, per-stack manage token, multi-stack manage token,
  multi-stack token resolution, manage token across stacks,
  no-env-manage-token, --no-env-manage-token,
  permissions deny-manage-env, permissions allow-manage-env,
  AI exfiltration, exfiltrate manage token, env-var manage token,
  read-only workspace, kbagent init --read-only, sandboxed agent token,
  local workspace, project directory, kbagent init.
---

# kbagent -- Keboola Agent CLI

## How to use this skill

This skill contains everything you need. The decision table below maps goals to commands.
For detailed workflows, see the `references/` docs linked at the bottom.

For **command flags and parameters**, use `kbagent <command> --help` (e.g. `kbagent config new --help`).

If kbagent is not installed or you need the full standalone reference, run `kbagent context`.

## Rules

1. **Always use `--json`**: `kbagent --json <command>` for parseable output
2. **Set conversation ID**: before first kbagent call, run `export KBAGENT_CONVERSATION_ID="<unique-id>"` (e.g. session UUID). All API requests include this as `X-Conversation-ID` header for platform observability.
3. **Multi-project by default**: read commands query ALL connected projects in parallel -- no need to loop
4. **Write commands need `--project`**: specify the target project alias
5. **Tokens are always masked** in output -- this is expected, not an error
6. **Use `--hint` for Python code generation**: `kbagent --hint client <command>` generates Python code using `KeboolaClient` (direct API), `kbagent --hint service <command>` generates code using the service layer with CLI config. See [programming-with-cli.md](references/programming-with-cli.md) for details.
7. **Always fetch fresh before write**: configs change between commands and across users. Re-fetch from the API immediately before any update; never reuse a config dump from earlier in the session. Stale local files are how `vN+1` silently overwrites someone else's `vN` changes.
8. **Always `--dry-run` first** for destructive operations (`config update`, `config delete`, `storage delete-*`, `branch delete`, `sync push`). Show the user the diff and get explicit confirmation before applying.
9. **Prefer CLI commands over MCP tools** where both exist (`config update` over `update_config`, `config detail` over `get_configs`). Lower latency, native dry-run/diff support, consistent JSON shape, and fewer parameter-shape footguns. Use MCP only for operations the CLI does not cover (e.g. `str_replace`, `list_append`, `run_component`).
10. **Never auto-run jobs after config changes**. `config update` (or `sync push`) and `job run` are always two separate steps. Wait for the user to confirm before triggering a run -- do not chain them.

## Safe write workflow

For any operation that modifies a Keboola config or storage object, follow this order. See [safe-write-workflow](references/safe-write-workflow.md) for the detailed runbook with examples and anti-patterns.

1. **Fetch fresh** from the API (e.g. `kbagent --json config detail ...`) -- never reuse a local file from earlier in the session.
2. **Compute the change** in memory or via `jq`/Python. Keep the diff small and targeted; prefer `--set path=value` or `--merge` over full-config replacement.
3. **Preview with `--dry-run`** (e.g. `kbagent --json config update ... --dry-run`). Show the user what will change.
4. **Get user confirmation** before re-running the same command without `--dry-run`.
5. **Verify** by re-fetching the config and inspecting the new version.
6. **Stop**. Do NOT auto-trigger `job run`, transformation execution, or any side-effecting follow-up. The user decides when to run.

When working inside a git repository or project directory, run `kbagent init` (or `kbagent init --from-global`) once to create a local `.kbagent/` workspace. After that, kbagent works from any subdirectory of the project -- no need to `cd ~` first.

## Choosing the right approach

<!-- BEGIN AUTO-GENERATED COMMANDS -->
| Goal | Command |
|------|---------|
| Update kbagent to the latest version | `kbagent update` |
| Show recent changelog (what changed in each version) | `kbagent changelog` |
| List all operations with their risk category and current allowed/denied status | `kbagent permissions list` |
| Show the current active permission policy | `kbagent permissions show` |
| Set the permission policy (firewall rules) | `kbagent permissions set --mode MODE` |
| Remove all permission restrictions | `kbagent permissions reset` |
| Refuse to read manage tokens from environment variables | `kbagent permissions deny-manage-env` |
| Re-allow reading manage tokens from environment variables (default) | `kbagent permissions allow-manage-env` |
| Check if a specific operation is allowed | `kbagent permissions check <OPERATION>` |
| Add a new Keboola project connection | `kbagent project add --project ALIAS` |
| List all connected Keboola projects | `kbagent project list` |
| Remove a Keboola project connection | `kbagent project remove --project ALIAS` |
| Edit an existing Keboola project connection | `kbagent project edit --project ALIAS` |
| Test connectivity to connected Keboola projects | `kbagent project status` |
| Refresh expired or invalid Storage API tokens | `kbagent project refresh` |
| Pin <alias> as the default project for subsequent commands | `kbagent project use <ALIAS>` |
| Show the effective default project | `kbagent project current` |
| Get the Keboola dashboard project description | `kbagent project description-get --project PROJECT` |
| Set the Keboola dashboard project description (markdown) | `kbagent project description-set --project PROJECT` |
| Set up projects and register them in the kbagent config | `kbagent org setup --url URL` |
| List available components from connected projects | `kbagent component list` |
| Show detailed information about a specific component | `kbagent component detail --component-id COMPONENT-ID` |
| List configurations from connected projects | `kbagent config list` |
| Show detailed information about one or many configurations | `kbagent config detail --component-id COMPONENT-ID` |
| Search through configuration bodies for a string or pattern | `kbagent config search --query QUERY` |
| Update a configuration's metadata and/or content | `kbagent config update --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Set or clear ``storage.output.default_bucket`` on a configuration | `kbagent config set-default-bucket --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Rename a configuration (update name via API + rename local sync directory) | `kbagent config rename --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID --name NAME` |
| Delete a configuration from a project | `kbagent config delete --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Generate boilerplate configuration files for a Keboola component | `kbagent config new --component-id COMPONENT-ID` |
| List all metadata entries on a configuration | `kbagent config metadata-list --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Read a single metadata value by key | `kbagent config get-metadata --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID --key KEY` |
| Set a metadata key/value on a configuration (upsert) | `kbagent config set-metadata --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID --key KEY --value VALUE` |
| Delete a configuration metadata entry by its numeric ID | `kbagent config delete-metadata --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID --metadata-id METADATA-ID` |
| Set the folder (KBC.configuration.folderName) on a configuration | `kbagent config set-folder --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID --name NAME` |
| Assign variables to a config (auto-creates backing keboola.variables on first call) | `kbagent config variables-set --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Read the current variable values attached to a config | `kbagent config variables-get --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Unlink variables from a config (does NOT delete the underlying keboola.variables) | `kbagent config variables-clear --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| List data apps across one or more registered projects | `kbagent data-app list` |
| Show merged Data Science + Storage detail for one data app | `kbagent data-app detail --project PROJECT --app-id APP-ID` |
| Create a Keboola data app end-to-end (POST + encrypt + PUT + deploy) | `kbagent data-app create --project PROJECT --name NAME --slug SLUG --git-repo GIT-REPO` |
| Deploy the latest Storage config (the §9 redeploy contract) | `kbagent data-app deploy --project PROJECT --app-id APP-ID` |
| Wake an auto-suspended data app at its currently-pinned configVersion | `kbagent data-app start --project PROJECT --app-id APP-ID` |
| Stop a running data app (preserves the URL and Storage config) | `kbagent data-app stop --project PROJECT --app-id APP-ID` |
| Delete the deployment AND the Storage config (cascade, irreversible) | `kbagent data-app delete --project PROJECT --app-id APP-ID` |
| Retrieve the simpleAuth password for a password-gated data app | `kbagent data-app password --project PROJECT --app-id APP-ID` |
| List jobs from connected projects | `kbagent job list` |
| Show detailed information about a specific job | `kbagent job detail --project PROJECT --job-id JOB-ID` |
| Run a job for a component configuration | `kbagent job run --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Terminate one or more Queue API jobs (use to stop runaway or stuck jobs) | `kbagent job terminate --project PROJECT` |
| List storage buckets with sharing/linked bucket information | `kbagent storage buckets` |
| Show detailed bucket info including backend-native direct access paths | `kbagent storage bucket-detail --project PROJECT --bucket-id BUCKET-ID` |
| List storage tables from one or more projects | `kbagent storage tables` |
| Show detailed table info including columns and types | `kbagent storage table-detail --project PROJECT --table-id TABLE-ID` |
| Create a new storage bucket | `kbagent storage create-bucket --project PROJECT --stage STAGE --name NAME` |
| Create a new storage table with typed columns | `kbagent storage create-table --project PROJECT --bucket-id BUCKET-ID --name NAME --column COLUMN` |
| Upload a CSV file into a storage table | `kbagent storage upload-table --project PROJECT --table-id TABLE-ID --file FILE` |
| Export a storage table to a local CSV file | `kbagent storage download-table --project PROJECT --table-id TABLE-ID` |
| Delete one or more storage tables | `kbagent storage delete-table --project PROJECT --table-id TABLE-ID` |
| Delete one or more columns from a storage table | `kbagent storage delete-column --project PROJECT --table-id TABLE-ID --column COLUMN` |
| Delete one or more storage buckets | `kbagent storage delete-bucket --project PROJECT --bucket-id BUCKET-ID` |
| Set the description on a storage bucket | `kbagent storage describe-bucket --project PROJECT --bucket-id BUCKET-ID` |
| Set the description on a storage table | `kbagent storage describe-table --project PROJECT --table-id TABLE-ID` |
| Set descriptions on one or more columns of a storage table | `kbagent storage describe-column --project PROJECT --table-id TABLE-ID --column COLUMN` |
| Apply descriptions to buckets, tables, and columns from a YAML file | `kbagent storage describe-batch --project PROJECT --from-file FROM-FILE` |
| List Storage Files with optional tag filtering | `kbagent storage files --project PROJECT` |
| Show Storage File metadata (without downloading) | `kbagent storage file-detail --project PROJECT --file-id FILE-ID` |
| Upload a local file to Storage Files | `kbagent storage file-upload --project PROJECT --file FILE` |
| Download a Storage File to local disk | `kbagent storage file-download --project PROJECT` |
| Add and/or remove tags on a Storage File | `kbagent storage file-tag --project PROJECT --file-id FILE-ID` |
| Delete one or more Storage Files | `kbagent storage file-delete --project PROJECT --file-id FILE-ID` |
| Load a Storage File into a table | `kbagent storage load-file --project PROJECT --file-id FILE-ID --table-id TABLE-ID` |
| Export a table to a Storage File | `kbagent storage unload-table --project PROJECT --table-id TABLE-ID` |
| List shared buckets available for linking | `kbagent sharing list` |
| Enable sharing on a bucket | `kbagent sharing share --project PROJECT --bucket-id BUCKET-ID --type SHARING-TYPE` |
| Disable sharing on a bucket | `kbagent sharing unshare --project PROJECT --bucket-id BUCKET-ID` |
| Link a shared bucket into a project | `kbagent sharing link --project PROJECT --source-project-id SOURCE-PROJECT-ID --bucket-id BUCKET-ID` |
| Remove a linked bucket from a project | `kbagent sharing unlink --project PROJECT --bucket-id BUCKET-ID` |
| Show cross-project data flow edges via bucket sharing | `kbagent sharing edges` |
| Build column-level lineage graph from sync'd data | `kbagent lineage build --output OUTPUT` |
| Show what's in a cached lineage graph | `kbagent lineage info --load LOAD` |
| Query upstream/downstream dependencies from a cached lineage graph | `kbagent lineage show --load LOAD` |
| Start a local web server with interactive lineage browser | `kbagent lineage server --load LOAD` |
| Check Kai server health and MCP connection status | `kbagent kai ping` |
| Ask Kai a one-shot question and get the full response | `kbagent kai ask --message MESSAGE` |
| Send a message to Kai in a chat session | `kbagent kai chat --message MESSAGE` |
| List recent Kai chat sessions | `kbagent kai history` |
| List all flows (keboola.orchestrator + keboola.flow) across projects | `kbagent flow list` |
| Show detailed flow information including phases and tasks | `kbagent flow detail --project PROJECT --flow-id FLOW-ID` |
| Print the YAML format expected by 'flow new' and 'flow update' | `kbagent flow schema` |
| Create a new flow configuration | `kbagent flow new --project PROJECT --name NAME` |
| Update a flow's name, description, or phases/tasks | `kbagent flow update --project PROJECT --flow-id FLOW-ID` |
| Delete a flow configuration | `kbagent flow delete --project PROJECT --flow-id FLOW-ID` |
| Bind a cron schedule to a flow (upsert: creates or updates) | `kbagent flow schedule --project PROJECT --flow-id FLOW-ID --cron CRON` |
| Remove all schedules bound to a flow (deletes keboola.scheduler configs) | `kbagent flow schedule-remove --project PROJECT --flow-id FLOW-ID` |
| List cron schedules (keboola.scheduler configs) across projects | `kbagent schedule list` |
| Show full detail for a single cron schedule | `kbagent schedule detail --project PROJECT --schedule-id SCHEDULE-ID` |
| Audit schedules by cron window or job-freshness | `kbagent schedule find` |
| List development branches from connected projects | `kbagent branch list` |
| Create a new development branch and auto-activate it | `kbagent branch create --project PROJECT --name NAME` |
| Set an existing development branch as active | `kbagent branch use --project PROJECT --branch BRANCH` |
| Reset the active branch back to main/production | `kbagent branch reset --project PROJECT` |
| Delete a development branch | `kbagent branch delete --project PROJECT --branch BRANCH` |
| Get the KBC UI merge URL for a development branch | `kbagent branch merge --project PROJECT` |
| List all metadata entries on a branch | `kbagent branch metadata-list --project PROJECT` |
| Read a single metadata value by key | `kbagent branch metadata-get --project PROJECT --key KEY` |
| Set a metadata key/value on a branch | `kbagent branch metadata-set --project PROJECT --key KEY` |
| Delete a branch metadata entry by its numeric ID | `kbagent branch metadata-delete --project PROJECT --metadata-id METADATA-ID` |
| Create a new workspace | `kbagent workspace create --project PROJECT` |
| List workspaces from connected projects | `kbagent workspace list` |
| Show workspace details (password NOT included) | `kbagent workspace detail --project PROJECT --workspace-id WORKSPACE-ID` |
| Delete a workspace | `kbagent workspace delete --project PROJECT --workspace-id WORKSPACE-ID` |
| Reset workspace password and show the new one | `kbagent workspace password --project PROJECT --workspace-id WORKSPACE-ID` |
| Load tables into a workspace | `kbagent workspace load --project PROJECT --workspace-id WORKSPACE-ID --tables TABLES` |
| Execute SQL query in a workspace via Query Service | `kbagent workspace query --project PROJECT --workspace-id WORKSPACE-ID` |
| Garbage-collect orphaned workspaces | `kbagent workspace gc` |
| Create a workspace from a transformation config | `kbagent workspace from-transformation --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| List available MCP tools from the keboola-mcp-server | `kbagent tool list` |
| Call an MCP tool on keboola-mcp-server | `kbagent tool call <TOOL-NAME>` |
| Initialize a sync working directory for a Keboola project | `kbagent sync init --project PROJECT` |
| Download configurations from a Keboola project to local files | `kbagent sync pull` |
| Show which local configurations have been modified, added, or deleted | `kbagent sync status` |
| Show detailed diff between local and remote configurations | `kbagent sync diff` |
| Push local configuration changes to a Keboola project | `kbagent sync push` |
| Link the current git branch to a Keboola development branch | `kbagent sync branch-link --project PROJECT` |
| Remove the branch mapping for the current git branch | `kbagent sync branch-unlink` |
| Show the branch mapping status for the current git branch | `kbagent sync branch-status` |
| Encrypt #-prefixed secret values for a Keboola component | `kbagent encrypt values --project PROJECT --component-id COMPONENT-ID --input INPUT-DATA` |
<!-- END AUTO-GENERATED COMMANDS -->

### Sync pull notable flags

| Flag | Effect |
|------|--------|
| `--with-samples` | Download CSV data previews (tables >30 columns auto-trimmed to first 30) |
| `--job-limit N` | Max recent jobs per config (default 5) |
| `--no-storage` | Skip storage bucket/table metadata |
| `--no-jobs` | Skip per-config job history |
| `--sample-limit N` | Max rows per sample (default 100) |
| `--max-samples N` | Max tables to sample (default 50) |

## Response format

All JSON responses follow one of two shapes:

**Success:**
```json
{"status": "ok", "data": ...}
```

**Error:**
```json
{"status": "error", "error": {"code": "ERROR_CODE", "message": "...", "retryable": true}}
```

Check the `retryable` field -- if `true`, retry the operation.

For detailed response parsing rules and common pitfalls, see [gotchas](references/gotchas.md).

## Workflow references

| Workflow | Reference |
|----------|-----------|
| All commands cheat sheet | [commands-reference](references/commands-reference.md) |
| **Safe config write workflow** (fetch → dry-run → confirm → push) | [safe-write-workflow](references/safe-write-workflow.md) |
| Creating new configurations | [scaffold-workflow](references/scaffold-workflow.md) |
| MCP tools (multi-project read/write) | [mcp-workflow](references/mcp-workflow.md) |
| Workspace SQL debugging | [workspace-workflow](references/workspace-workflow.md) |
| **Data apps** (create / deploy / start / stop / password / delete; the §9 redeploy contract) | [data-app-workflow](references/data-app-workflow.md) |
| Storage Files (upload, download, tags, load/unload) | [storage-files-workflow](references/storage-files-workflow.md) |
| **Storage column types** (native types, NOT NULL, DEFAULT, branch materialize) | [storage-types-workflow](references/storage-types-workflow.md) |
| Bucket sharing & linking | [sharing-workflow](references/sharing-workflow.md) |
| Dev branches | [branch-workflow](references/branch-workflow.md) |
| Encrypting secrets for MCP tools | [encrypt-workflow](references/encrypt-workflow.md) |
| Sync & Git-branching (GitOps) | [sync-workflow](references/sync-workflow.md) |
| Sync row-level internals (manifest v3, hoist, encryption) | [sync-rows-workflow](references/sync-rows-workflow.md) |
| **Variables (attach to any config)** | [variables-workflow](references/variables-workflow.md) |
| Reading synced data | [reading-synced-data](references/reading-synced-data.md) |
| SQL migration (input mapping removal) | [sql-migration-workflow](references/sql-migration-workflow.md) |
| Response parsing gotchas | [gotchas](references/gotchas.md) |

## First-time setup

If kbagent is not yet installed:

```bash
uv tool install git+https://github.com/padak/keboola_agent_cli
uv tool install keboola-mcp-server
kbagent doctor --fix
```

Then add projects:

```bash
# Single project
kbagent --json project add --project prod --url https://connection.keboola.com --token YOUR_TOKEN

# Or bulk-onboard from organization (org admin)
KBC_MANAGE_API_TOKEN=xxx kbagent --json org setup --org-id 123 --url https://connection.keboola.com --yes

# Or onboard specific projects (any project member, uses Personal Access Token)
KBC_MANAGE_API_TOKEN=xxx kbagent --json org setup --project-ids 901,9621,10539 --url https://connection.keboola.com --yes
```
