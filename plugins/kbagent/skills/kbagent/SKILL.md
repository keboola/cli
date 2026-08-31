---
name: kbagent
description: >
  Use when working with Keboola Connection projects via the kbagent CLI.
  Covers: exploring and searching configurations, job history, data
  lineage, dev branches, workspace SQL debugging, GitOps config sync,
  bucket sharing and linking, encrypting secrets,
  Storage tables, files, and snapshots, data apps,
  flows and schedules, invitations,
  feature flags, OTLP data streams, scoped Storage tokens, the semantic
  layer, Developer Portal, browser login,
  first-time setup and logout in any client.
  Triggers: kbagent, Keboola, keboola
  config, keboola job, keboola lineage, keboola sync, gitops, dev branch,
  data app, streamlit deploy, semantic layer, sl, dev-portal,
  data stream, OTLP, scoped token, encrypt secrets,
  feature flag, flow schedule, invite member, SQL transformation edit,
  sync action, keboola docs, table snapshot, auth, login, sign in,
  PAYG credits, flow notifications, alert recipients, config trash,
  restore config, zero-copy clone, workspace load type,
  set up keboola, setup, connect project, logout, sign out.
---

# kbagent -- Keboola Agent CLI

## How to use this skill

This skill contains everything you need. The decision table below maps goals to commands.
For detailed workflows, see the `references/` docs linked at the bottom.

For **command flags and parameters**, use `kbagent <command> --help` (e.g. `kbagent config new --help`).

If kbagent is not installed or you need the full standalone reference, run `kbagent context`.

## Rules

1. **Always use `--json`**: `kbagent --json <command>` for parseable output
2. **Set conversation ID**: pass `--conversation-id "<unique-id>"` (e.g. session UUID) on every kbagent call -- `kbagent --json --conversation-id <id> <command>`. All API requests include it as the `X-Conversation-ID` header for platform observability. **Do not use a standalone `export`**: agent harnesses (Claude Code included) do not persist shell state between tool calls, so the variable is gone by the next command -- and prefixing each command with `export ...` stops it matching a `Bash(kbagent ...)` permission allow-rule. For a whole session, set `KBAGENT_CONVERSATION_ID` in the harness's own env block (Claude Code: `settings.json` -> `env`) instead.
3. **Multi-project by default**: read commands query ALL connected projects in parallel -- no need to loop
4. **Write commands need `--project`**: specify the target project alias
5. **Tokens are always masked** in output -- this is expected, not an error
6. **Always fetch fresh before write**: configs change between commands and across users. Re-fetch from the API immediately before any update; never reuse a config dump from earlier in the session. Stale local files are how `vN+1` silently overwrites someone else's `vN` changes.
7. **Always `--dry-run` first** for destructive operations (`config update`, `config delete`, `storage delete-*`, `branch delete`, `sync push`). Show the user the diff and get explicit confirmation before applying.
8. **There is no MCP passthrough.** `kbagent tool list` / `tool call` and `agent --type mcp_tool` were removed in v0.85.0 -- every catalog tool has a native command. If a user names an old tool (`update_config`, `get_configs`, `query_data`, ...), map it via `docs/mcp-migration.md` in the repo and run the native command instead.
9. **Never auto-run jobs after config changes**. `config update` (or `sync push`) and `job run` are always two separate steps. Wait for the user to confirm before triggering a run -- do not chain them.

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
| Launch the kbagent HTTP API server | `kbagent serve` |
| Search for items (tables, buckets, configs, flows, …) by name or content | `kbagent search <QUERY>` |
| List all operations with their risk category and current allowed/denied status | `kbagent permissions list` |
| Show the current active permission policy | `kbagent permissions show` |
| Set the permission policy (firewall rules) | `kbagent permissions set --mode MODE` |
| Remove all permission restrictions | `kbagent permissions reset` |
| Check if a specific operation is allowed | `kbagent permissions check <OPERATION>` |
| Sign in to a Keboola stack via browser login (PKCE) or device code | `kbagent auth login` |
| Sign in via email + password (+ TOTP if the account has MFA) -- no browser | `kbagent auth login-password --email EMAIL` |
| Show the programmatic-auth session health for a stack | `kbagent auth status` |
| Revoke and clear the local programmatic-auth session for a stack | `kbagent auth logout` |
| Register accessible projects from the current session as local aliases | `kbagent auth register-projects` |
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
| Show detailed project metadata | `kbagent project info --project PROJECT` |
| Invite a user (or many users via CSV) to one or more projects | `kbagent project invite` |
| List active members of a project (and optionally pending invitations) | `kbagent project member-list --project PROJECT` |
| List pending project invitations | `kbagent project invitation-list --project PROJECT` |
| Cancel a pending invitation | `kbagent project invitation-cancel --project PROJECT --email EMAIL` |
| Remove an active member from a project (destructive) | `kbagent project member-remove --project PROJECT --email EMAIL` |
| Change an existing member's role (PATCH) | `kbagent project member-set-role --project PROJECT --email EMAIL --role ROLE` |
| Set up projects and register them in the kbagent config | `kbagent org setup --url URL` |
| List all feature flags defined on the stack | `kbagent feature list --project PROJECT` |
| Show feature flags assigned to a project | `kbagent feature project-show --project PROJECT` |
| Enable a feature flag on a project | `kbagent feature project-add --project PROJECT --feature FEATURE` |
| Disable a feature flag on a project (destructive) | `kbagent feature project-remove --project PROJECT --feature FEATURE` |
| Show feature flags assigned to a user | `kbagent feature user-show --project PROJECT --email EMAIL` |
| Enable a feature flag on a user | `kbagent feature user-add --project PROJECT --email EMAIL --feature FEATURE` |
| Disable a feature flag on a user (destructive) | `kbagent feature user-remove --project PROJECT --email EMAIL --feature FEATURE` |
| Mint a scoped Storage API token (secret shown once) | `kbagent token create --project PROJECT --description DESCRIPTION` |
| List the project's Storage API tokens (no secrets -- those are mint-only) | `kbagent token list --project PROJECT` |
| Revoke a Storage API token immediately (destructive; only non-master tokens) | `kbagent token delete --project PROJECT --token-id TOKEN-ID` |
| Rotate a token: generate a new value and invalidate the old one (secret shown once) | `kbagent token refresh --project PROJECT --token-id TOKEN-ID` |
| Show the current PAYG credit balance for one or more projects | `kbagent billing credits` |
| List available components from connected projects | `kbagent component list` |
| Show detailed information about a specific component | `kbagent component detail --component-id COMPONENT-ID` |
| Run a synchronous component action such as testConnection | `kbagent component sync-action <ACTION-NAME> --component-id COMPONENT-ID --project PROJECT` |
| List configurations from connected projects | `kbagent config list` |
| Show detailed information about one or many configurations | `kbagent config detail --component-id COMPONENT-ID` |
| Show sample configuration JSON examples for a component | `kbagent config examples --component-id COMPONENT-ID` |
| Search through configuration bodies for a string or pattern | `kbagent config search --query QUERY` |
| Update a configuration's metadata and/or content | `kbagent config update --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Set or clear ``storage.output.default_bucket`` on a configuration | `kbagent config set-default-bucket --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Rename a configuration (update name via API + rename local sync directory) | `kbagent config rename --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID --name NAME` |
| Soft-delete a configuration into the trash (restorable) | `kbagent config delete --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Generate boilerplate configuration files for a Keboola component, optionally creating the config remotely in one shot | `kbagent config new --component-id COMPONENT-ID` |
| List all metadata entries on a configuration | `kbagent config metadata-list --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Read a single metadata value by key | `kbagent config get-metadata --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID --key KEY` |
| Set a metadata key/value on a configuration (upsert) | `kbagent config set-metadata --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID --key KEY --value VALUE` |
| Delete a configuration metadata entry by its numeric ID | `kbagent config delete-metadata --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID --metadata-id METADATA-ID` |
| Set the folder (KBC.configuration.folderName) on a configuration | `kbagent config set-folder --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID --name NAME` |
| Assign variables to a config (auto-creates backing keboola.variables on first call) | `kbagent config variables-set --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Read the current variable values attached to a config | `kbagent config variables-get --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Unlink variables from a config (does NOT delete the underlying keboola.variables) | `kbagent config variables-clear --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Create a new configuration row | `kbagent config row-create --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID --name NAME` |
| Update an existing configuration row | `kbagent config row-update --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID --row-id ROW-ID` |
| Delete a configuration row | `kbagent config row-delete --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID --row-id ROW-ID` |
| Read the runtime ``state`` dict of a configuration or one of its rows | `kbagent config state-get --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Overwrite the runtime ``state`` dict of a configuration or one of its rows | `kbagent config state-set --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID --state STATE` |
| Duplicate a configuration, whole -- including runtime, storage and authorization | `kbagent config clone --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID --name NAME` |
| Requires master token. | `kbagent config oauth-url --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Restore a configuration from the trash (undo of 'config delete') | `kbagent config restore --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| List configurations in the trash (restorable via 'config restore') | `kbagent config trash-list --project PROJECT` |
| List data apps across one or more registered projects | `kbagent data-app list` |
| Show merged Data Science + Storage detail for one data app | `kbagent data-app detail --project PROJECT --app-id APP-ID` |
| Create a Keboola data app end-to-end (POST + encrypt + PUT + deploy) | `kbagent data-app create --project PROJECT --name NAME --slug SLUG` |
| Deploy the latest Storage config (the §9 redeploy contract) | `kbagent data-app deploy --project PROJECT --app-id APP-ID` |
| Wake an auto-suspended data app at its currently-pinned configVersion | `kbagent data-app start --project PROJECT --app-id APP-ID` |
| Stop a running data app (preserves the URL and Storage config) | `kbagent data-app stop --project PROJECT --app-id APP-ID` |
| Delete the deployment AND the Storage config (cascade, irreversible) | `kbagent data-app delete --project PROJECT --app-id APP-ID` |
| Retrieve the simpleAuth password for a password-gated data app | `kbagent data-app password --project PROJECT --app-id APP-ID` |
| Tail the container logs for a deployed data app | `kbagent data-app logs --project PROJECT --app-id APP-ID` |
| List a data app's recent deployment attempts (runs), newest first | `kbagent data-app runs --project PROJECT --app-id APP-ID` |
| Pre-flight check that a git repo follows the Keboola data-app Golden Rule | `kbagent data-app validate-repo --git-repo GIT-REPO` |
| Show the clone URLs of a data app's configured git repository | `kbagent data-app git-repo --project PROJECT --app-id APP-ID` |
| List the credentials of a data app's MANAGED git repository | `kbagent data-app git-credentials --project PROJECT --app-id APP-ID` |
| Create a git credential (SSH key or HTTP token) for a MANAGED repo | `kbagent data-app git-credentials-create --project PROJECT --app-id APP-ID --type CRED-TYPE --permissions PERMISSIONS` |
| Encrypt and write app-runtime secrets to the linked Storage config | `kbagent data-app secrets-set --project PROJECT --app-id APP-ID` |
| List the keys in parameters.dataApp.secrets, with derived runtime env-var names | `kbagent data-app secrets-list --project PROJECT --app-id APP-ID` |
| Show ONE key from parameters.dataApp.secrets | `kbagent data-app secrets-get --project PROJECT --app-id APP-ID --key KEY` |
| Remove one or more app-runtime secrets. | `kbagent data-app secrets-remove --project PROJECT --app-id APP-ID --key KEY` |
| List jobs from connected projects | `kbagent job list` |
| Show detailed information about a specific job | `kbagent job detail --project PROJECT --job-id JOB-ID` |
| Run a job for a component configuration | `kbagent job run --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Terminate one or more Queue API jobs (use to stop runaway or stuck jobs) | `kbagent job terminate --project PROJECT` |
| List storage buckets with sharing/linked bucket information | `kbagent storage buckets` |
| Show detailed bucket info including backend-native direct access paths | `kbagent storage bucket-detail --project PROJECT --bucket-id BUCKET-ID` |
| List storage tables from one or more projects | `kbagent storage tables` |
| Show detailed table info including columns, types and physical layout | `kbagent storage table-detail --project PROJECT --table-id TABLE-ID` |
| Create a new storage bucket | `kbagent storage create-bucket --project PROJECT --stage STAGE --name NAME` |
| Create a new storage table with typed columns | `kbagent storage create-table --project PROJECT --bucket-id BUCKET-ID --name NAME` |
| Upload a CSV file into a storage table | `kbagent storage upload-table --project PROJECT --table-id TABLE-ID --file FILE` |
| Export a storage table to a local CSV file | `kbagent storage download-table --project PROJECT --table-id TABLE-ID` |
| Delete one or more storage tables | `kbagent storage delete-table --project PROJECT --table-id TABLE-ID` |
| Truncate (delete all rows from) one or more storage tables | `kbagent storage truncate-table --project PROJECT --table-id TABLE-ID` |
| Add a single column to an existing table (synchronous, typed) | `kbagent storage add-column --project PROJECT --table-id TABLE-ID --column COLUMN` |
| Delete one or more columns from a storage table | `kbagent storage delete-column --project PROJECT --table-id TABLE-ID --column COLUMN` |
| Swap two storage tables (any branch, including the default/production branch) | `kbagent storage swap-tables --project PROJECT --table-id TABLE-ID --target-table-id TARGET-TABLE-ID` |
| Clone (pull) a production table into a development branch | `kbagent storage clone-table --project PROJECT --table-id TABLE-ID` |
| Delete one or more storage buckets | `kbagent storage delete-bucket --project PROJECT --bucket-id BUCKET-ID` |
| List Storage Files with optional tag filtering | `kbagent storage files --project PROJECT` |
| Show Storage File metadata (without downloading) | `kbagent storage file-detail --project PROJECT --file-id FILE-ID` |
| Upload a local file to Storage Files | `kbagent storage file-upload --project PROJECT --file FILE` |
| Download a Storage File to local disk | `kbagent storage file-download --project PROJECT` |
| Add and/or remove tags on a Storage File | `kbagent storage file-tag --project PROJECT --file-id FILE-ID` |
| Delete one or more Storage Files | `kbagent storage file-delete --project PROJECT --file-id FILE-ID` |
| Load a Storage File into a table | `kbagent storage load-file --project PROJECT --file-id FILE-ID --table-id TABLE-ID` |
| Export a table to a Storage File | `kbagent storage unload-table --project PROJECT --table-id TABLE-ID` |
| List snapshots of a table | `kbagent storage snapshots --project PROJECT --table-id TABLE-ID` |
| Create a snapshot of a table (data + columns + primary key) | `kbagent storage snapshot-create --project PROJECT --table-id TABLE-ID` |
| Show one snapshot's detail (source table, creation time, description) | `kbagent storage snapshot-detail --project PROJECT --snapshot-id SNAPSHOT-ID` |
| Delete one or more table snapshots (the source tables are untouched) | `kbagent storage snapshot-delete --project PROJECT --snapshot-id SNAPSHOT-ID` |
| Create a NEW table from an existing snapshot (snapshot restore) | `kbagent storage table-from-snapshot --project PROJECT --snapshot-id SNAPSHOT-ID --bucket-id BUCKET-ID --name NAME` |
| Set the description on a storage bucket | `kbagent storage describe-bucket --project PROJECT --bucket-id BUCKET-ID` |
| Set the description on a storage table | `kbagent storage describe-table --project PROJECT --table-id TABLE-ID` |
| Set descriptions on one or more columns of a storage table | `kbagent storage describe-column --project PROJECT --table-id TABLE-ID --column COLUMN` |
| Apply descriptions to buckets, tables, and columns from a YAML file | `kbagent storage describe-batch --project PROJECT --from-file FROM-FILE` |
| Convert legacy KBC.column.* descriptions to the native definition endpoint | `kbagent storage describe-migrate --project PROJECT` |
| List Data Streams sources in a project | `kbagent stream list --project PROJECT` |
| Create an OTLP (or HTTP) source and return its endpoint | `kbagent stream create-source --project PROJECT --name NAME` |
| Show a source's endpoints, protocol, and destination tables | `kbagent stream detail [SOURCE-ID] --project PROJECT` |
| Delete a Data Streams source (destructive) | `kbagent stream delete <SOURCE-ID> --project PROJECT` |
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
| Check whether the configured token can use Kai (master token + AI Agent Chat) | `kbagent kai preflight` |
| Fetch the full message history of a single Kai chat | `kbagent kai chat-detail --chat-id CHAT-ID` |
| List recent Kai chat sessions | `kbagent kai history` |
| Ask the Keboola documentation a natural language question | `kbagent docs query <QUESTION>` |
| Create a SQL transformation from a SQL script | `kbagent transformation create --name NAME` |
| Show a SQL transformation's block/code tree with positional IDs | `kbagent transformation show --config-id CONFIG-ID` |
| Edit a SQL transformation's blocks/codes with positional operations | `kbagent transformation edit --config-id CONFIG-ID --change-description CHANGE-DESCRIPTION` |
| List conditional flows (keboola.flow) across projects | `kbagent flow list` |
| Show detailed conditional-flow information including phases and tasks | `kbagent flow detail --project PROJECT --flow-id FLOW-ID` |
| Print the conditional-flow YAML template, or --full for the JSON Schema | `kbagent flow schema` |
| Show bundled example flow configurations (offline, no project needed) | `kbagent flow examples` |
| Validate a conditional-flow definition (schema + semantic checks) | `kbagent flow validate --file FILE` |
| Create a new conditional-flow (keboola.flow) configuration | `kbagent flow new --project PROJECT --name NAME` |
| Update a flow's name, description, or phases/tasks | `kbagent flow update --project PROJECT --flow-id FLOW-ID` |
| Delete a conditional-flow (keboola.flow) configuration | `kbagent flow delete --project PROJECT --flow-id FLOW-ID` |
| Bind a cron schedule to a flow (upsert: creates or updates) | `kbagent flow schedule --project PROJECT --flow-id FLOW-ID --cron CRON` |
| Remove all schedules bound to a flow (deletes keboola.scheduler configs) | `kbagent flow schedule-remove --project PROJECT --flow-id FLOW-ID` |
| Show every trigger kbagent can see for a flow (cron + table triggers) | `kbagent flow triggers --project PROJECT --flow-id FLOW-ID` |
| List cron schedules (keboola.scheduler configs) across projects | `kbagent schedule list` |
| Show full detail for a single cron schedule | `kbagent schedule detail --project PROJECT --schedule-id SCHEDULE-ID` |
| Audit schedules by cron window or job-freshness | `kbagent schedule find` |
| List notification subscriptions (Flow Notifications tab) across projects | `kbagent notification list` |
| Show one notification subscription, including its raw filter list | `kbagent notification detail --project PROJECT --subscription-id SUBSCRIPTION-ID` |
| Create a notification subscription | `kbagent notification create --project PROJECT --event EVENT --channel CHANNEL --address ADDRESS` |
| Delete a notification subscription | `kbagent notification delete --project PROJECT --subscription-id SUBSCRIPTION-ID` |
| Replace a subscription's recipient | `kbagent notification replace-recipient --project PROJECT --subscription-id SUBSCRIPTION-ID --address ADDRESS` |
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
| Initialize a sync working directory for a Keboola project | `kbagent sync init --project PROJECT` |
| Download configurations from a Keboola project to local files | `kbagent sync pull` |
| Show which local configurations have been modified, added, or deleted | `kbagent sync status` |
| Show detailed diff between local and remote configurations | `kbagent sync diff` |
| Push local configuration changes to a Keboola project | `kbagent sync push` |
| Clone a reference project into a fresh target, parameterised by overrides | `kbagent sync clone --source SOURCE --target TARGET --target-dir TARGET-DIR` |
| Link the current git branch to a Keboola development branch | `kbagent sync branch-link --project PROJECT` |
| Remove the branch mapping for the current git branch | `kbagent sync branch-unlink` |
| Show the branch mapping status for the current git branch | `kbagent sync branch-status` |
| Encrypt #-prefixed secret values for a Keboola component | `kbagent encrypt values --project PROJECT --component-id COMPONENT-ID --input INPUT-DATA` |
| Encrypt the project's storage token for transformation `user_properties` | `kbagent semantic-layer token --project PROJECT --component-id COMPONENT-ID` |
| Build a semantic-layer model from a list of storage tables (non-interactive) | `kbagent semantic-layer build --project PROJECT` |
| Promote a model from one project to another (NEW + overwrite CHANGED; never deletes) | `kbagent semantic-layer promote --from-project FROM-PROJECT --to-project TO-PROJECT` |
| Replay a snapshot into a project. | `kbagent semantic-layer import --project PROJECT --file FILE` |
| Show the entities in a semantic-layer model | `kbagent semantic-layer show --project PROJECT` |
| Fetch the server-side JSON Schema of semantic object types | `kbagent semantic-layer schema --project PROJECT` |
| Snapshot a semantic-layer model to a self-describing JSON file | `kbagent semantic-layer export --project PROJECT` |
| Diff two semantic-layer snapshots (project↔project, project↔file, file↔file) | `kbagent semantic-layer diff` |
| Validate a semantic-layer model | `kbagent semantic-layer validate --project PROJECT` |
| Search semantic-layer entities across a project by name pattern | `kbagent semantic-layer search-context --project PROJECT` |
| Fetch a single semantic-layer entity by id, irrespective of its type | `kbagent semantic-layer get-context --project PROJECT --context-id CONTEXT-ID` |
| List all semantic-layer models in a project | `kbagent semantic-layer model list --project PROJECT` |
| Create a new semantic-layer model | `kbagent semantic-layer model create --project PROJECT --name NAME` |
| Delete a semantic-layer model and cascade-delete its children | `kbagent semantic-layer model delete --project PROJECT --model MODEL` |
| Add a metric to a semantic-layer model | `kbagent semantic-layer add metric --project PROJECT --name NAME --sql SQL --dataset DATASET` |
| Add a dataset (FQN derived from tableId) | `kbagent semantic-layer add dataset --project PROJECT --name NAME --table-id TABLE-ID` |
| Add a relationship between two datasets | `kbagent semantic-layer add relationship --project PROJECT --name NAME --from FROM- --to TO --on ON` |
| Add a constraint | `kbagent semantic-layer add constraint --project PROJECT --name NAME --constraint-type CONSTRAINT-TYPE --rule RULE --metrics METRICS` |
| Add a glossary term | `kbagent semantic-layer add glossary --project PROJECT --term TERM` |
| Edit a metric. | `kbagent semantic-layer edit metric --project PROJECT --name NAME` |
| Edit a dataset (no cascade — metric.dataset uses tableId, not name) | `kbagent semantic-layer edit dataset --project PROJECT --name NAME` |
| Edit a constraint (DELETE+POST, with local validators) | `kbagent semantic-layer edit constraint --project PROJECT --name NAME` |
| Edit a relationship (DELETE+POST). | `kbagent semantic-layer edit relationship --project PROJECT --name NAME` |
| Edit a glossary term. | `kbagent semantic-layer edit glossary --project PROJECT --term TERM` |
| Remove a metric. | `kbagent semantic-layer remove metric --project PROJECT --name NAME` |
| Remove a dataset | `kbagent semantic-layer remove dataset --project PROJECT --name NAME` |
| Remove a constraint | `kbagent semantic-layer remove constraint --project PROJECT --name NAME` |
| Remove a relationship. | `kbagent semantic-layer remove relationship --project PROJECT --name NAME` |
| Remove a glossary term. | `kbagent semantic-layer remove glossary --project PROJECT --term TERM` |
| List reference-data records (dimension summaries; use ``get`` for members) | `kbagent semantic-layer reference-data list --project PROJECT` |
| Fetch one record (all members) by ``--id`` or by ``--dimension`` | `kbagent semantic-layer reference-data get --project PROJECT` |
| Create or replace a reference-data record (keyed by dimension) | `kbagent semantic-layer reference-data set --project PROJECT --dimension DIMENSION --members-file MEMBERS-FILE` |
| Delete a reference-data record by UUID (server-side soft-delete) | `kbagent semantic-layer reference-data delete --project PROJECT --id ID-` |
| Encrypt the project's storage token for transformation `user_properties` | `kbagent sl token --project PROJECT --component-id COMPONENT-ID` |
| Build a semantic-layer model from a list of storage tables (non-interactive) | `kbagent sl build --project PROJECT` |
| Promote a model from one project to another (NEW + overwrite CHANGED; never deletes) | `kbagent sl promote --from-project FROM-PROJECT --to-project TO-PROJECT` |
| Replay a snapshot into a project. | `kbagent sl import --project PROJECT --file FILE` |
| Show the entities in a semantic-layer model | `kbagent sl show --project PROJECT` |
| Fetch the server-side JSON Schema of semantic object types | `kbagent sl schema --project PROJECT` |
| Snapshot a semantic-layer model to a self-describing JSON file | `kbagent sl export --project PROJECT` |
| Diff two semantic-layer snapshots (project↔project, project↔file, file↔file) | `kbagent sl diff` |
| Validate a semantic-layer model | `kbagent sl validate --project PROJECT` |
| Search semantic-layer entities across a project by name pattern | `kbagent sl search-context --project PROJECT` |
| Fetch a single semantic-layer entity by id, irrespective of its type | `kbagent sl get-context --project PROJECT --context-id CONTEXT-ID` |
| List all semantic-layer models in a project | `kbagent sl model list --project PROJECT` |
| Create a new semantic-layer model | `kbagent sl model create --project PROJECT --name NAME` |
| Delete a semantic-layer model and cascade-delete its children | `kbagent sl model delete --project PROJECT --model MODEL` |
| Add a metric to a semantic-layer model | `kbagent sl add metric --project PROJECT --name NAME --sql SQL --dataset DATASET` |
| Add a dataset (FQN derived from tableId) | `kbagent sl add dataset --project PROJECT --name NAME --table-id TABLE-ID` |
| Add a relationship between two datasets | `kbagent sl add relationship --project PROJECT --name NAME --from FROM- --to TO --on ON` |
| Add a constraint | `kbagent sl add constraint --project PROJECT --name NAME --constraint-type CONSTRAINT-TYPE --rule RULE --metrics METRICS` |
| Add a glossary term | `kbagent sl add glossary --project PROJECT --term TERM` |
| Edit a metric. | `kbagent sl edit metric --project PROJECT --name NAME` |
| Edit a dataset (no cascade — metric.dataset uses tableId, not name) | `kbagent sl edit dataset --project PROJECT --name NAME` |
| Edit a constraint (DELETE+POST, with local validators) | `kbagent sl edit constraint --project PROJECT --name NAME` |
| Edit a relationship (DELETE+POST). | `kbagent sl edit relationship --project PROJECT --name NAME` |
| Edit a glossary term. | `kbagent sl edit glossary --project PROJECT --term TERM` |
| Remove a metric. | `kbagent sl remove metric --project PROJECT --name NAME` |
| Remove a dataset | `kbagent sl remove dataset --project PROJECT --name NAME` |
| Remove a constraint | `kbagent sl remove constraint --project PROJECT --name NAME` |
| Remove a relationship. | `kbagent sl remove relationship --project PROJECT --name NAME` |
| Remove a glossary term. | `kbagent sl remove glossary --project PROJECT --term TERM` |
| List reference-data records (dimension summaries; use ``get`` for members) | `kbagent sl reference-data list --project PROJECT` |
| Fetch one record (all members) by ``--id`` or by ``--dimension`` | `kbagent sl reference-data get --project PROJECT` |
| Create or replace a reference-data record (keyed by dimension) | `kbagent sl reference-data set --project PROJECT --dimension DIMENSION --members-file MEMBERS-FILE` |
| Delete a reference-data record by UUID (server-side soft-delete) | `kbagent sl reference-data delete --project PROJECT --id ID-` |
| GET an endpoint on the running kbagent serve | `kbagent http get <PATH>` |
| POST to an endpoint on the running kbagent serve | `kbagent http post <PATH>` |
| PATCH an endpoint on the running kbagent serve | `kbagent http patch <PATH>` |
| DELETE an endpoint on the running kbagent serve | `kbagent http delete <PATH>` |
| List all registered agent tasks | `kbagent agent list` |
| Show one task's full configuration | `kbagent agent show [TASK-ID]` |
| Register a new scheduled task | `kbagent agent create --name NAME` |
| Patch one or more fields on a task. | `kbagent agent update [TASK-ID]` |
| Remove a task. | `kbagent agent delete [TASK-ID]` |
| Trigger a task immediately (does not wait for the next cron firing) | `kbagent agent run [TASK-ID]` |
| Show the run history of a task (most recent first) | `kbagent agent runs [TASK-ID]` |
| Show a single AgentRun record (status, summary, output, error) | `kbagent agent run-detail [TASK-ID] [RUN-ID]` |
| Replay the persisted event timeline of an ai_agent run (line-by-line) | `kbagent agent run-events [TASK-ID] [RUN-ID]` |
| Execute an action ad-hoc (no persistence, no scheduling) | `kbagent agent test` |
| Show the next N firings of a cron expression | `kbagent agent cron-preview --cron CRON` |
| Polish a plain-English goal into an unattended-agent-ready prompt | `kbagent agent prompt-improve --goal GOAL` |
| List Developer Portal apps for a vendor | `kbagent dev-portal list --vendor VENDOR` |
| Show the full Developer Portal entry for one app | `kbagent dev-portal get --app APP` |
| Create (register) a new app in the Developer Portal. | `kbagent dev-portal create --vendor VENDOR --data DATA` |
| Patch one or more properties of an existing Developer Portal app. | `kbagent dev-portal patch --app APP` |
| Upload a 128x128 PNG icon for a Developer Portal app. | `kbagent dev-portal upload-icon --app APP --file FILE` |
| Publish an app in the Developer Portal (requests Keboola review). | `kbagent dev-portal publish --app APP` |
| Deprecate an app in the Developer Portal (hides it, blocks new configs). | `kbagent dev-portal deprecate --app APP` |
| Add a Developer Portal identity (verifies creds before persisting) | `kbagent dev-portal identity add --alias ALIAS --username USERNAME` |
| List configured Developer Portal identities | `kbagent dev-portal identity list` |
| Remove a Developer Portal identity | `kbagent dev-portal identity remove --alias ALIAS` |
| Edit fields on a Developer Portal identity (or rename it) | `kbagent dev-portal identity edit --alias ALIAS` |
| Set the default Developer Portal identity | `kbagent dev-portal identity use <ALIAS>` |
| Show the alias of the default Developer Portal identity | `kbagent dev-portal identity current` |
| Probe a Developer Portal identity by logging in | `kbagent dev-portal identity verify` |
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
| **SQL transformations** (create / show / edit; the show-before-edit rule for positional block/code ids) | [transformation-workflow](references/transformation-workflow.md) |
| Workspace SQL debugging | [workspace-workflow](references/workspace-workflow.md) |
| **Agent Tasks via CLI** (`kbagent agent` CRUD + run + cron-preview + prompt-improve; cron / manual / chained; cli_command / ai_agent action flavours) | [agent-tasks-cli-workflow](references/agent-tasks-cli-workflow.md) |
| **Agent Tasks via REST** (`kbagent http <verb> /agents...` from inside scheduled subprocesses; SSE streaming) | [agent-tasks-rest-workflow](references/agent-tasks-rest-workflow.md) |
| **Data apps** (create / deploy / start / stop / password / delete; the §9 redeploy contract) | [data-app-workflow](references/data-app-workflow.md) |
| Storage Files (upload, download, tags, load/unload) | [storage-files-workflow](references/storage-files-workflow.md) |
| **Table snapshots** (point-in-time backup; restore as a NEW table; `--name` required, no overwrite) | [snapshot-workflow](references/snapshot-workflow.md) |
| **Python library** (`from keboola_agent_cli import Client` -- in-process query + Storage Files, no CLI/daemon/config-dir) | [library-workflow](references/library-workflow.md) |
| **Data Streams (OTLP / OpenTelemetry)** (create/inspect OTLP source, masked secret-in-URL, OTEL_EXPORTER_OTLP_ENDPOINT) | [stream-workflow](references/stream-workflow.md) |
| **Storage column types** (native types, NOT NULL, DEFAULT, branch materialize) | [storage-types-workflow](references/storage-types-workflow.md) |
| **Partition/cluster a BigQuery table** (create partitioned copy -> swap-tables -> revert pattern; STORAGE_JOB_TIMEOUT handling; physical verification via bq show) | [partition-table-workflow](references/partition-table-workflow.md) |
| **Typify a typeless table** (profile -> CTAS -> swap-tables -> validate -> handoff) | [typify-table-workflow](references/typify-table-workflow.md) |
| Bucket sharing & linking | [sharing-workflow](references/sharing-workflow.md) |
| **Project members & invitations** (single + bulk via CSV, role change, remove) | [member-workflow](references/member-workflow.md) |
| **Billing / PAYG credits** (balance only; the shape of the invoice-history gap; PAYG_NOT_AVAILABLE; units) | [billing-workflow](references/billing-workflow.md) |
| Dev branches | [branch-workflow](references/branch-workflow.md) |
| Encrypting secrets before a config write | [encrypt-workflow](references/encrypt-workflow.md) |
| Sync & Git-branching (GitOps) | [sync-workflow](references/sync-workflow.md) |
| Sync row-level internals (manifest v3, hoist, encryption) | [sync-rows-workflow](references/sync-rows-workflow.md) |
| **Promote configs source -> destination project** (from-scratch GitHub Actions pull -> validate -> push pipeline built on `sync`; PR-gated, cross-project dry-run diff) -- a **separate skill**, not a reference doc | [kbagent-promotion-pipeline](../kbagent-promotion-pipeline/SKILL.md) |
| **Migrating a `kbc` (keboola-as-code) GitHub CI/CD pipeline to kbagent sync** | [kbagent-cicd-migration](../kbagent-cicd-migration/SKILL.md) (sibling skill) |
| **Variables (attach to any config)** | [variables-workflow](references/variables-workflow.md) |
| Reading synced data | [reading-synced-data](references/reading-synced-data.md) |
| SQL migration (input mapping removal) | [sql-migration-workflow](references/sql-migration-workflow.md) |
| **Semantic layer (metastore)** -- models, metrics, datasets, constraints, glossary; validate / export / diff / promote / build / token | [semantic-layer-workflow](references/semantic-layer-workflow.md) |
| **Developer Portal** (identity CRUD, list/get apps, create/patch/upload-icon/publish/deprecate; TTY-confirm on writes) | [dev-portal-workflow](references/dev-portal-workflow.md) |
| **Config metadata** (list/get/set/delete arbitrary key-value metadata on a configuration) | [config-metadata-workflow](references/config-metadata-workflow.md) |
| **Storage descriptions** (describe bucket / table / column, batch from YAML) | [storage-describe-workflow](references/storage-describe-workflow.md) |
| **Deep column-level lineage** (`lineage build --ai`, column graph, ER + HTML output) | [lineage-deep-workflow](references/lineage-deep-workflow.md) |
| **Session permissions firewall** (`--deny-writes` / `--deny-destructive`, persisted policies, `permissions check`) | [permissions-workflow](references/permissions-workflow.md) |
| **Kai** (project-aware AI Q&A: ping / preflight / ask / chat / history) | [kai-workflow](references/kai-workflow.md) |
| **Programmatic auth** (browser login: PKCE/device flow, `auth login`/`status`/`logout`; never in a foreground shell, never unattended -- attended agents drive `--device-code` in the background and relay the code) | [auth-workflow](references/auth-workflow.md) |
| Response parsing gotchas | [gotchas](references/gotchas.md) |

## First-time setup

**In Claude Code with this plugin installed, `/kbagent:setup` is the
one-command path.** It installs the CLI if missing, connects a project, and
verifies with `kbagent doctor`. Every step is conditional, so it is safe to
re-run on a half-finished setup, and it announces each step as it goes.

**In every other client there is no slash command** -- Claude Desktop has no
slash-command surface at all, Cursor may not expose one, and a plain chat
with a shell has none either. There you run the same flow inline: the steps
below ARE that flow, and this skill is what the user's natural-language ask
("set up Keboola", "connect my project", "log me out") should trigger.

**The plugin is an upgrade, not a prerequisite.** kbagent is a CLI: in any
client with a shell, `kbagent project add` plus the steps below is a complete
setup. `kbagent context` prints the full command reference and is the manual
substitute for this skill -- it teaches any agent the whole surface. And
`doctor`'s `claude_plugin` check warns or skips when the plugin is absent;
that is informational and never means setup failed.

**Login is shared across clients.** The session lives in the same local
config directory (`auth.json` next to `config.json`), so signing in once --
in any client, or in a plain terminal -- is inherited by all of them. Always
check the existing state before starting a login.

### 1. Is the CLI installed?

```bash
kbagent version
```

If the command is not found, install it, then re-check:

```bash
curl -LsSf https://raw.githubusercontent.com/keboola/cli/main/install.sh | sh
# the installer puts kbagent on PATH for its own process only --
# 'source $HOME/.local/bin/env' or open a new shell
```

### 2. Is a project already connected?

```bash
kbagent --json project list      # non-empty -> already connected, skip to step 4
kbagent --json auth status       # exit 0 = a session is already signed in
```

### 3. Connect -- the login ladder

Take the first rung that fits the situation.

**a. Browser login, driven by the agent (attended sessions).**
`kbagent auth login` must **never** run in a foreground tool shell -- it
blocks until the human finishes, and a foreground shell timeout (~120s)
kills it mid-flight -- and **never** from an unattended or headless task.
But in an attended session, when a background shell is available, the agent
should drive it rather than handing it off:

```bash
# run this in a BACKGROUND shell, not a foreground one
kbagent auth login --device-code --stack https://connection.keboola.com --register-projects
```

- The verification URL and user code are printed immediately, before polling
  starts. In human output they go to stdout; with `--json` the panel goes to
  **stderr**, so capture `2>&1`. Prefer human mode here.
- Relay the URL and the code to the user in chat. The CLI also best-effort
  opens the browser on the host machine.
- Confirm completion with `kbagent --json auth status`: exit 0 means signed
  in (`live` / `refreshed` / `degraded`), exit 3 means not yet
  (`missing` / `expired`). Poll that -- never re-run login blind.
- `auth.json` is written atomically on success only, so an abandoned or
  killed attempt corrupts nothing.
- `--register-projects` is fully non-interactive and registers every
  accessible project; without it, use `kbagent auth register-projects --all`
  afterwards.
- **No background shell available?** Then hand the exact command to the
  user's own terminal and wait for them to confirm before continuing.

**b. Unattended: `auth login-password`** -- the headless path, when the task
was given real account credentials (a dedicated service account, never a
human's own):

```bash
KBC_LOGIN_EMAIL=... KBC_LOGIN_PASSWORD=... KBC_LOGIN_TOTP_SECRET=... \
  kbagent auth login-password --password-stdin --register-projects <<< "$KBC_LOGIN_PASSWORD"
```

**c. Static Storage token** -- always works, no browser and no account
credentials:

```bash
# Single project
kbagent --json project add --project prod --url https://connection.keboola.com --token YOUR_TOKEN

# Or bulk-onboard from organization (org admin)
# Manage token: interactive prompt by default; for CI add --allow-env-manage-token
# alongside KBC_MANAGE_API_TOKEN (required since v0.29.0).
KBC_MANAGE_API_TOKEN=xxx kbagent --allow-env-manage-token --json org setup --org-id 123 --url https://connection.keboola.com --yes

# Or onboard specific projects (any project member, uses Personal Access Token)
KBC_MANAGE_API_TOKEN=xxx kbagent --allow-env-manage-token --json org setup --project-ids 901,9621,10539 --url https://connection.keboola.com --yes
```

### 4. Verify

```bash
kbagent --json doctor
```

A `claude_plugin` warn or skip is expected outside Claude Code and does not
make the setup incomplete.

### Logging out

Unlike login, logout is fully agent-runnable. Check the state first, then
revoke:

```bash
kbagent --json auth status                  # what is signed in, on which stack
kbagent --json auth logout                  # revoke + delete the local session
kbagent auth logout --remove-projects --yes # also drop session-registered aliases
```

Use `--json` or `--yes` so the command does not stop on the confirmation
prompt. `--remove-projects` only removes aliases backed by this session's
sentinel token; a static-token project is never touched -- remove those with
`kbagent project remove --project ALIAS`. Because the session is shared, a
logout signs the user out of every client at once.

### Installing this plugin

This plugin ships through Keboola's `keboola-claude-kit` marketplace, published
from `keboola/ai-kit`. It is optional (see above) -- the CLI alone is a working
setup. Per client:

- **Claude Code:**

  ```
  /plugin marketplace add keboola/ai-kit
  /plugin install kbagent@keboola-claude-kit
  ```

- **Cursor:** add the marketplace through the UI. It **requires the full
  URL** -- `https://github.com/keboola/ai-kit`. The short `keboola/ai-kit`
  form that Claude Code accepts is rejected there with an opaque
  `[invalid_argument] Error`.
- **Claude Desktop:** Settings (Customise) -> Plugins -> add the
  `keboola/ai-kit` marketplace, then install `kbagent`. Claude Desktop has
  **no slash commands**, so `/kbagent:setup` and `/keboola` are unavailable
  there -- setup and everything else happen through this skill's
  natural-language triggers instead.

Copies installed from the older `keboola-agent-cli` marketplace (this CLI's own
repo) still work and still update, but that entry is deprecated -- moving to
`keboola/ai-kit` is how a user gets the maintained one.
