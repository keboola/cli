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
  data-app logs, container logs, app logs, tail logs, build logs,
  app stdout, app stderr, troubleshoot data app, debug data app,
  app proxy, simpleAuth, app auto-suspend, configVersion, redeploy contract,
  Data Science API, /apps endpoint, app password, KBC::Project ciphertext,
  data-app secrets, app secrets, app runtime secrets, secrets-set,
  secrets-list, secrets-get, secrets-remove, encrypt app secret,
  app environment variable, validate repo, validate-repo,
  data-app golden rule, pre-flight repo check, repo structure check,
  local workspace, project directory, kbagent init,
  invite user, invite member, project invitation, manage members,
  list members, remove member, change role, project role,
  bulk invite, invite from CSV, project access, member management,
  manage token prompt, --allow-env-manage-token, KBC_MANAGE_API_TOKEN,
  feature flag, feature flags, list features, project features, user features,
  enable feature, disable feature, set feature flag, add feature, remove feature,
  early-adopter-preview, direct-access, pay-as-you-go, /manage/features,
  super admin token, super-admin feature, stack feature catalogue,
  data stream, data streams, keboola data streams, stream source, OTLP,
  OpenTelemetry, otel, OTLP endpoint, OTEL_EXPORTER_OTLP_ENDPOINT, telemetry ingest,
  logs metrics traces, stream create-source, stream detail, stream list,
  stream delete, otlp source, http source, stream-in, ingest endpoint,
  semantic-layer, semantic layer, semantic-layer model, metastore,
  semantic-metric, semantic-dataset, semantic-relationship,
  semantic-constraint, semantic-glossary, add metric, edit metric,
  rename metric, remove metric, validate model, validate semantic layer,
  promote model, semantic-layer build, semantic-layer export,
  semantic-layer diff, semantic-layer import, semantic-layer token,
  metric SQL, dataset FQN, constraint rule, threshold constraint,
  4-band health, _critical _warning _healthy _review, CODE_METRIC,
  DIM_METRIC_THRESHOLD, dangling metric FK, orphaned constraint,
  phantom field, AGG on STRING, SUM on PCT, deep validate,
  sl, kbagent sl, semantic layer wizard, sl-build, sl-add, sl-edit,
  reference data, semantic-layer reference-data, chart of accounts, COA,
  dimension members, account list, dimension data, member list,
  developer portal, dev-portal, apps-api, register component, vendor app,
  portal property, ui-options, encryption portal, defaultBucket portal,
  app icon, configurationSchema portal, publish component, deprecate component,
  kbagent dev-portal, portal identity, vendor login, service account portal.
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
6. **Always fetch fresh before write**: configs change between commands and across users. Re-fetch from the API immediately before any update; never reuse a config dump from earlier in the session. Stale local files are how `vN+1` silently overwrites someone else's `vN` changes.
7. **Always `--dry-run` first** for destructive operations (`config update`, `config delete`, `storage delete-*`, `branch delete`, `sync push`). Show the user the diff and get explicit confirmation before applying.
8. **Prefer CLI commands over MCP tools** where both exist (`config update` over `update_config`, `config detail` over `get_configs`). Lower latency, native dry-run/diff support, consistent JSON shape, and fewer parameter-shape footguns. Use MCP only for operations the CLI does not cover (e.g. `str_replace`, `list_append`, `run_component`).
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
| Update kbagent + keboola-mcp-server to the latest versions | `kbagent update` |
| Show recent changelog (what changed in each version) | `kbagent changelog` |
| Launch the kbagent HTTP API server | `kbagent serve` |
| Search for items (tables, buckets, configs, flows, …) by name or content | `kbagent search <QUERY>` |
| List all operations with their risk category and current allowed/denied status | `kbagent permissions list` |
| Show the current active permission policy | `kbagent permissions show` |
| Set the permission policy (firewall rules) | `kbagent permissions set --mode MODE` |
| Remove all permission restrictions | `kbagent permissions reset` |
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
| List available components from connected projects | `kbagent component list` |
| Show detailed information about a specific component | `kbagent component detail --component-id COMPONENT-ID` |
| List configurations from connected projects | `kbagent config list` |
| Show detailed information about one or many configurations | `kbagent config detail --component-id COMPONENT-ID` |
| Search through configuration bodies for a string or pattern | `kbagent config search --query QUERY` |
| Update a configuration's metadata and/or content | `kbagent config update --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Set or clear ``storage.output.default_bucket`` on a configuration | `kbagent config set-default-bucket --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Rename a configuration (update name via API + rename local sync directory) | `kbagent config rename --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID --name NAME` |
| Delete a configuration from a project | `kbagent config delete --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
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
| Requires master token. | `kbagent config oauth-url --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| List data apps across one or more registered projects | `kbagent data-app list` |
| Show merged Data Science + Storage detail for one data app | `kbagent data-app detail --project PROJECT --app-id APP-ID` |
| Create a Keboola data app end-to-end (POST + encrypt + PUT + deploy) | `kbagent data-app create --project PROJECT --name NAME --slug SLUG --git-repo GIT-REPO` |
| Deploy the latest Storage config (the §9 redeploy contract) | `kbagent data-app deploy --project PROJECT --app-id APP-ID` |
| Wake an auto-suspended data app at its currently-pinned configVersion | `kbagent data-app start --project PROJECT --app-id APP-ID` |
| Stop a running data app (preserves the URL and Storage config) | `kbagent data-app stop --project PROJECT --app-id APP-ID` |
| Delete the deployment AND the Storage config (cascade, irreversible) | `kbagent data-app delete --project PROJECT --app-id APP-ID` |
| Retrieve the simpleAuth password for a password-gated data app | `kbagent data-app password --project PROJECT --app-id APP-ID` |
| Tail the container logs for a deployed data app | `kbagent data-app logs --project PROJECT --app-id APP-ID` |
| Encrypt and write app-runtime secrets to the linked Storage config | `kbagent data-app secrets-set --project PROJECT --app-id APP-ID` |
| List the keys in parameters.dataApp.secrets, with derived runtime env-var names | `kbagent data-app secrets-list --project PROJECT --app-id APP-ID` |
| Show ONE key from parameters.dataApp.secrets | `kbagent data-app secrets-get --project PROJECT --app-id APP-ID --key KEY` |
| Remove one or more app-runtime secrets. | `kbagent data-app secrets-remove --project PROJECT --app-id APP-ID --key KEY` |
| Pre-flight check that a git repo follows the Keboola data-app Golden Rule | `kbagent data-app validate-repo --git-repo GIT-REPO` |
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
| Truncate (delete all rows from) one or more storage tables | `kbagent storage truncate-table --project PROJECT --table-id TABLE-ID` |
| Delete one or more columns from a storage table | `kbagent storage delete-column --project PROJECT --table-id TABLE-ID --column COLUMN` |
| Swap two storage tables (any branch, including the default/production branch) | `kbagent storage swap-tables --project PROJECT --table-id TABLE-ID --target-table-id TARGET-TABLE-ID` |
| Clone (pull) a production table into a development branch | `kbagent storage clone-table --project PROJECT --table-id TABLE-ID` |
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
| List conditional flows (keboola.flow) across projects | `kbagent flow list` |
| Show detailed conditional-flow information including phases and tasks | `kbagent flow detail --project PROJECT --flow-id FLOW-ID` |
| Print the conditional-flow YAML template, or --full for the live JSON Schema | `kbagent flow schema` |
| Validate a conditional-flow definition (schema + semantic checks) | `kbagent flow validate --file FILE` |
| Create a new conditional-flow (keboola.flow) configuration | `kbagent flow new --project PROJECT --name NAME` |
| Update a flow's name, description, or phases/tasks | `kbagent flow update --project PROJECT --flow-id FLOW-ID` |
| Delete a conditional-flow (keboola.flow) configuration | `kbagent flow delete --project PROJECT --flow-id FLOW-ID` |
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
| Encrypt the project's storage token for transformation `user_properties` | `kbagent semantic-layer token --project PROJECT --component-id COMPONENT-ID` |
| Build a semantic-layer model from a list of storage tables (non-interactive) | `kbagent semantic-layer build --project PROJECT` |
| Promote a model from one project to another (NEW + overwrite CHANGED; never deletes) | `kbagent semantic-layer promote --from-project FROM-PROJECT --to-project TO-PROJECT` |
| Replay a snapshot into a project. | `kbagent semantic-layer import --project PROJECT --file FILE` |
| Show the entities in a semantic-layer model | `kbagent semantic-layer show --project PROJECT` |
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
| MCP tools (multi-project read/write) | [mcp-workflow](references/mcp-workflow.md) |
| Workspace SQL debugging | [workspace-workflow](references/workspace-workflow.md) |
| **Agent Tasks via CLI** (`kbagent agent` CRUD + run + cron-preview + prompt-improve; cron / manual / chained; mcp_tool / cli_command / ai_agent action flavours) | [agent-tasks-cli-workflow](references/agent-tasks-cli-workflow.md) |
| **Agent Tasks via REST** (`kbagent http <verb> /agents...` from inside scheduled subprocesses; SSE streaming) | [agent-tasks-rest-workflow](references/agent-tasks-rest-workflow.md) |
| **Data apps** (create / deploy / start / stop / password / delete; the §9 redeploy contract) | [data-app-workflow](references/data-app-workflow.md) |
| Storage Files (upload, download, tags, load/unload) | [storage-files-workflow](references/storage-files-workflow.md) |
| **Data Streams (OTLP / OpenTelemetry)** (create/inspect OTLP source, masked secret-in-URL, OTEL_EXPORTER_OTLP_ENDPOINT) | [stream-workflow](references/stream-workflow.md) |
| **Storage column types** (native types, NOT NULL, DEFAULT, branch materialize) | [storage-types-workflow](references/storage-types-workflow.md) |
| **Typify a typeless table** (profile -> CTAS -> swap-tables -> validate -> handoff) | [typify-table-workflow](references/typify-table-workflow.md) |
| Bucket sharing & linking | [sharing-workflow](references/sharing-workflow.md) |
| **Project members & invitations** (single + bulk via CSV, role change, remove) | [member-workflow](references/member-workflow.md) |
| Dev branches | [branch-workflow](references/branch-workflow.md) |
| Encrypting secrets for MCP tools | [encrypt-workflow](references/encrypt-workflow.md) |
| Sync & Git-branching (GitOps) | [sync-workflow](references/sync-workflow.md) |
| Sync row-level internals (manifest v3, hoist, encryption) | [sync-rows-workflow](references/sync-rows-workflow.md) |
| **Variables (attach to any config)** | [variables-workflow](references/variables-workflow.md) |
| Reading synced data | [reading-synced-data](references/reading-synced-data.md) |
| SQL migration (input mapping removal) | [sql-migration-workflow](references/sql-migration-workflow.md) |
| **Semantic layer (metastore)** -- models, metrics, datasets, constraints, glossary; validate / export / diff / promote / build / token | [semantic-layer-workflow](references/semantic-layer-workflow.md) |
| **Developer Portal** (identity CRUD, list/get apps, create/patch/upload-icon/publish/deprecate; TTY-confirm on writes) | [dev-portal-workflow](references/dev-portal-workflow.md) |
| Response parsing gotchas | [gotchas](references/gotchas.md) |

## First-time setup

If kbagent is not yet installed:

```bash
uv tool install git+https://github.com/keboola/cli
# --prerelease=allow is required (issue #324): keboola-mcp-server pins a
# pre-release-only transitive dep (toon-format), which uv refuses by default.
uv tool install --prerelease=allow keboola-mcp-server
kbagent doctor --fix
```

Then add projects:

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
