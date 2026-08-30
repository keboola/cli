# `kbagent serve` — REST endpoint reference

<!-- GENERATED FILE -- DO NOT EDIT BY HAND.
     Regenerate with `make endpoints-gen`; `make endpoints-check` gates it in CI. -->

Generated from the live FastAPI app by `scripts/gen_endpoint_reference.py`,
so this file cannot disagree with the server it documents. Architecture,
auth, and the concepts behind these routes live in
[`web-server.md`](web-server.md); a running server serves the same spec
interactively at `/docs` (Swagger) and `/openapi.json`.

**235 operations** across **205 paths** and **30 routers**.

Paths are shown as the server registers them. Reaching them through the
Node BFF (or single-process `--ui` mode) prefixes every path with `/api`.

## Project Management

### `auth` (3 operations)

Read/audit the current browser-login session and register its accessible projects as local aliases. `login` / `login-password` / `logout` have no endpoint here -- see `server/routers/auth.py`. Mirrors `kbagent auth status|register-projects` (partially).

| Method | Path | Summary |
|---|---|---|
| `GET` | `/auth/projects` | List the session's registerable project candidates |
| `POST` | `/auth/register-projects` | Register accessible projects as local aliases |
| `GET` | `/auth/status` | Session health for a stack |

### `projects` (11 operations)

Register, list, edit, and remove Keboola project aliases. Mirrors `kbagent project add|list|remove|edit|status|use|current|info`.

| Method | Path | Summary |
|---|---|---|
| `GET` | `/projects` | List registered projects |
| `POST` | `/projects` | Add a project |
| `POST` | `/projects/bulk-delete` | Remove multiple projects |
| `DELETE` | `/projects/{alias}` | Remove a project |
| `PATCH` | `/projects/{alias}` | Edit a project |
| `GET` | `/projects/status` | Check project connectivity |
| `GET` | `/projects/current` | Get the active project |
| `POST` | `/projects/use/{alias}` | Switch the active project |
| `GET` | `/projects/{alias}/info` | Get project metadata |
| `GET` | `/projects/{alias}/description` | Get the project description |
| `PUT` | `/projects/{alias}/description` | Set the project description |

### `members` (6 operations)

Invite users, list members and pending invitations, change roles, and remove members. Mirrors `kbagent project invite|member-*|invitation-*`.

| Method | Path | Summary |
|---|---|---|
| `GET` | `/members/{project}` | List members |
| `GET` | `/members/{project}/invitations` | List pending invitations |
| `POST` | `/members/{project}/invite` | Invite a single user |
| `POST` | `/members/{project}/invitations/cancel` | Cancel invitation |
| `POST` | `/members/{project}/remove` | Remove member |
| `POST` | `/members/{project}/set-role` | Change member role |

### `org` (2 operations)

Bulk-onboard an entire organization (Manage API). Requires the `X-Manage-Token` header on every request -- the manage token is never persisted in config. Mirrors `kbagent org setup|refresh`.

| Method | Path | Summary |
|---|---|---|
| `POST` | `/org/setup` | Onboard org / project list |
| `POST` | `/org/refresh` | Re-issue storage tokens |

### `feature` (7 operations)

List the stack feature-flag catalogue and enable/disable features on projects and users (Manage API). Requires the `X-Manage-Token` header (super-admin) on every request -- the manage token is never persisted in config. Mirrors `kbagent feature list|project-*|user-*`.

| Method | Path | Summary |
|---|---|---|
| `GET` | `/feature/{project}/list` | Stack feature catalogue |
| `GET` | `/feature/{project}/project-show` | Project's assigned features |
| `POST` | `/feature/{project}/project-add` | Enable a feature on a project |
| `POST` | `/feature/{project}/project-remove` | Disable a feature on a project |
| `GET` | `/feature/{project}/user-show` | User's assigned features |
| `POST` | `/feature/{project}/user-add` | Enable a feature on a user |
| `POST` | `/feature/{project}/user-remove` | Disable a feature on a user |

### `billing` (1 operation)

PAYG credit balance across projects (read-only). Purchase history / Stripe invoice IDs are not reachable with a project token. Mirrors `kbagent billing credits`.

| Method | Path | Summary |
|---|---|---|
| `GET` | `/billing/credits` | PAYG credit balance across projects |

### `token` (5 operations)

Scoped Storage API tokens -- mint (bucket read/write + component access + expiry), rotate, and revoke. A minted/rotated token's secret is returned ONCE; the acting token needs canManageTokens. Mirrors `kbagent token create|delete|refresh`.

| Method | Path | Summary |
|---|---|---|
| `GET` | `/token/list` | List Storage tokens across projects |
| `GET` | `/token/{project}/list` | List the project's Storage tokens |
| `POST` | `/token/{project}/create` | Mint a scoped Storage token |
| `POST` | `/token/{project}/delete` | Revoke a Storage token (destructive) |
| `POST` | `/token/{project}/refresh` | Rotate a Storage token |

## Configurations

### `configs` (26 operations)

Browse, search, update, and manage component configurations and rows (variables, metadata, folder, default bucket, OAuth URL). Mirrors `kbagent config *`.

| Method | Path | Summary |
|---|---|---|
| `GET` | `/configs` | List component configurations |
| `GET` | `/configs/search` | Search configurations by pattern |
| `GET` | `/configs/examples/{component_id}` | Get configuration examples for a component |
| `GET` | `/configs/{project}/{component_id}/{config_id}` | Get configuration detail |
| `PATCH` | `/configs/{project}/{component_id}/{config_id}` | Update a configuration |
| `DELETE` | `/configs/{project}/{component_id}/{config_id}` | Delete a configuration |
| `POST` | `/configs/{project}/{component_id}/{config_id}/restore` | Restore a configuration from the trash |
| `GET` | `/configs/trash/{project}` | List trashed configurations |
| `POST` | `/configs/{project}/{component_id}` | Create a configuration |
| `POST` | `/configs/{project}/{component_id}/{config_id}/clone` | Clone a configuration |
| `POST` | `/configs/{project}/{component_id}/{config_id}/set-default-bucket` | Set or clear default bucket |
| `POST` | `/configs/{project}/{component_id}/{config_id}/rename` | Rename a configuration |
| `GET` | `/configs/{project}/{component_id}/{config_id}/metadata` | List configuration metadata |
| `GET` | `/configs/{project}/{component_id}/{config_id}/metadata/{key}` | Get a metadata value |
| `PUT` | `/configs/{project}/{component_id}/{config_id}/metadata/{key}` | Set a metadata value |
| `DELETE` | `/configs/{project}/{component_id}/{config_id}/metadata/{metadata_id}` | Delete a metadata entry |
| `POST` | `/configs/{project}/{component_id}/{config_id}/folder` | Move configuration to a folder |
| `POST` | `/configs/{project}/{component_id}/{config_id}/rows` | Create a configuration row |
| `PATCH` | `/configs/{project}/{component_id}/{config_id}/rows/{row_id}` | Update a configuration row |
| `DELETE` | `/configs/{project}/{component_id}/{config_id}/rows/{row_id}` | Delete a configuration row |
| `GET` | `/configs/{project}/{component_id}/{config_id}/oauth-url` | Get OAuth authorization URL |
| `GET` | `/configs/{project}/{component_id}/{config_id}/state` | Get configuration (or row) state |
| `PUT` | `/configs/{project}/{component_id}/{config_id}/state` | Set configuration (or row) state |
| `GET` | `/configs/{project}/{component_id}/{config_id}/variables` | Get configuration variables |
| `PUT` | `/configs/{project}/{component_id}/{config_id}/variables` | Set configuration variables |
| `DELETE` | `/configs/{project}/{component_id}/{config_id}/variables` | Clear configuration variables |

### `components` (4 operations)

Discover components (extractors, writers, applications, transformations) and fetch their JSON schemas. Mirrors `kbagent component list|detail`.

| Method | Path | Summary |
|---|---|---|
| `GET` | `/components` | List components |
| `GET` | `/components/{component_id}` | Get component detail |
| `POST` | `/components/{component_id}/scaffold` | Scaffold a new component config |
| `POST` | `/components/{component_id}/actions/{action}` | Run a synchronous component action |

### `transformations` (3 operations)

SQL transformations -- create from a SQL script, inspect the block/code tree, and apply positional edit operations. Mirrors `kbagent transformation create|show|edit`.

| Method | Path | Summary |
|---|---|---|
| `POST` | `/transformations/{project}` | Create a SQL transformation |
| `GET` | `/transformations/{project}/{config_id}` | Show a SQL transformation's block tree |
| `PATCH` | `/transformations/{project}/{config_id}` | Edit a SQL transformation with operations |

### `encrypt` (1 operation)

Encrypt secret values for a specific project + component using the Keboola encryption API. Mirrors `kbagent encrypt values`.

| Method | Path | Summary |
|---|---|---|
| `POST` | `/encrypt/values` | Encrypt secret values |

## Data

### `storage` (32 operations)

Buckets, tables, columns, files. Create, upload, download, describe, swap, delete. Mirrors `kbagent storage *`.

| Method | Path | Summary |
|---|---|---|
| `GET` | `/storage/buckets` | List storage buckets |
| `GET` | `/storage/buckets/{project}/{bucket_id}` | Get bucket detail |
| `POST` | `/storage/buckets/{project}` | Create a bucket |
| `DELETE` | `/storage/buckets/{project}` | Delete buckets |
| `POST` | `/storage/buckets/{project}/{bucket_id}/describe` | Set bucket description |
| `GET` | `/storage/tables` | List storage tables |
| `GET` | `/storage/table-detail/{project}/{table_id}` | Get table detail |
| `GET` | `/storage/table-preview/{project}/{table_id}` | Preview table rows |
| `GET` | `/storage/table-download/{project}/{table_id}` | Download table as CSV |
| `POST` | `/storage/tables/{project}` | Create a table |
| `DELETE` | `/storage/tables/{project}` | Delete tables |
| `POST` | `/storage/tables/{project}/upload` | Upload data into a table |
| `POST` | `/storage/tables/{project}/truncate` | Truncate tables |
| `DELETE` | `/storage/columns/{project}/{table_id}` | Delete table columns |
| `POST` | `/storage/columns/{project}/{table_id}` | Add a table column |
| `POST` | `/storage/tables/{project}/{table_id}/swap` | Swap two tables |
| `POST` | `/storage/tables/{project}/{table_id}/pull` | Clone a table into a dev branch |
| `POST` | `/storage/tables/{project}/{table_id}/snapshots` | Create a table snapshot |
| `GET` | `/storage/snapshots/{project}/{table_id}` | List table snapshots |
| `GET` | `/storage/snapshot-detail/{project}/{snapshot_id}` | Get snapshot detail |
| `DELETE` | `/storage/snapshots/{project}` | Delete snapshots |
| `POST` | `/storage/table-from-snapshot/{project}` | Create a NEW table from a snapshot |
| `POST` | `/storage/tables/{project}/{table_id}/describe` | Set table description |
| `POST` | `/storage/columns/{project}/{table_id}/describe` | Set column descriptions |
| `POST` | `/storage/columns/{project}/describe-migrate` | Migrate legacy column descriptions |
| `GET` | `/storage/files` | List storage files |
| `POST` | `/storage/files/upload` | Upload a file to Storage |
| `GET` | `/storage/files/{project}/{file_id}` | Get file detail |
| `GET` | `/storage/files/{project}/{file_id}/download` | Download a file |
| `DELETE` | `/storage/files/{project}` | Delete files |
| `POST` | `/storage/files/{project}/{file_id}/tag` | Add or remove file tags |
| `POST` | `/storage/files/{project}/load-to-table` | Load a file into a table |

### `stream` (4 operations)

Data Streams (OpenTelemetry / OTLP) -- list, create, and delete ingest sources and retrieve their endpoints. The OTLP URL embeds a secret that is masked unless `reveal=true`. Mirrors `kbagent stream list|create-source|detail|delete`.

| Method | Path | Summary |
|---|---|---|
| `GET` | `/stream/{project}/list` | List Data Streams sources |
| `POST` | `/stream/{project}/create-source` | Create an OTLP/HTTP source |
| `GET` | `/stream/{project}/detail` | Source endpoints + destination |
| `POST` | `/stream/{project}/delete` | Delete a source (destructive) |

### `search` (1 operation)

Cross-resource search over tables, buckets, configs, flows, data-apps, and transformations. Mirrors `kbagent search`.

| Method | Path | Summary |
|---|---|---|
| `GET` | `/search` | Search across projects |

### `sharing` (6 operations)

Share buckets across projects and inspect the sharing graph (edges). Mirrors `kbagent sharing *`.

| Method | Path | Summary |
|---|---|---|
| `GET` | `/sharing` | List shared buckets |
| `GET` | `/sharing/edges` | List cross-project sharing edges |
| `POST` | `/sharing/{project}/share` | Share a bucket |
| `POST` | `/sharing/{project}/unshare/{bucket_id}` | Unshare a bucket |
| `POST` | `/sharing/{project}/link` | Link a shared bucket |
| `POST` | `/sharing/{project}/unlink/{bucket_id}` | Unlink a shared bucket |

## Execution

### `jobs` (5 operations)

Run components, inspect job history, terminate running jobs. Mirrors `kbagent job list|detail|run|terminate`.

| Method | Path | Summary |
|---|---|---|
| `GET` | `/jobs` | List jobs across projects |
| `GET` | `/jobs/{project}/{job_id}` | Get job detail |
| `POST` | `/jobs/{project}/run` | Run a component configuration |
| `POST` | `/jobs/{project}/terminate` | Terminate running jobs |
| `GET` | `/jobs/{project}/{job_id}/stream` | Stream job status and logs (SSE) |

### `flows` (12 operations)

Orchestrator and Flow CRUD, scheduling, run history. Mirrors `kbagent flow *`.

| Method | Path | Summary |
|---|---|---|
| `POST` | `/flows/validate` | Validate a conditional-flow definition |
| `GET` | `/flows/examples` | Show bundled example flow configurations |
| `GET` | `/flows/{project}/schema` | Fetch the live conditional-flow JSON Schema |
| `GET` | `/flows` | List flows across projects |
| `GET` | `/flows/{project}/{config_id}` | Get flow detail |
| `PATCH` | `/flows/{project}/{config_id}` | Update an existing flow |
| `DELETE` | `/flows/{project}/{config_id}` | Delete a flow |
| `POST` | `/flows/{project}` | Create a new flow |
| `GET` | `/flows/{project}/{config_id}/schedules` | List schedules for a flow |
| `GET` | `/flows/{project}/{config_id}/triggers` | List every trigger kbagent can see |
| `POST` | `/flows/{project}/{config_id}/schedule` | Set a cron schedule on a flow |
| `DELETE` | `/flows/{project}/{config_id}/schedule` | Remove a flow schedule |

### `schedules` (3 operations)

Cron-style schedules attached to flows / configurations. Mirrors `kbagent schedule list|detail|find`.

| Method | Path | Summary |
|---|---|---|
| `GET` | `/schedules` | List schedules |
| `GET` | `/schedules/{project}/{schedule_id}` | Get schedule detail |
| `GET` | `/schedules/find/query` | Search schedules by criteria |

### `notifications` (5 operations)

Flow Notifications-tab recipients (Notification Service subscriptions) -- audit + write across projects. Mirrors `kbagent notification list|detail|create|delete|replace-recipient`.

| Method | Path | Summary |
|---|---|---|
| `GET` | `/notifications` | List notification subscriptions |
| `GET` | `/notifications/{project}/{subscription_id}` | Get subscription detail |
| `DELETE` | `/notifications/{project}/{subscription_id}` | Delete a notification subscription |
| `POST` | `/notifications/{project}` | Create a notification subscription |
| `POST` | `/notifications/{project}/{subscription_id}/replace-recipient` | Replace a subscription's recipient |

### `data-apps` (18 operations)

Streamlit / R / Python data apps -- create, deploy, start/stop, manage secrets. Mirrors `kbagent data-app *`.

| Method | Path | Summary |
|---|---|---|
| `GET` | `/data-apps` | List data apps across projects |
| `GET` | `/data-apps/{project}/{app_id}` | Get data app detail |
| `DELETE` | `/data-apps/{project}/{app_id}` | Delete a data app |
| `POST` | `/data-apps/{project}` | Create a data app |
| `POST` | `/data-apps/{project}/{app_id}/deploy` | Deploy a data app version |
| `POST` | `/data-apps/{project}/{app_id}/start` | Start a data app |
| `POST` | `/data-apps/{project}/{app_id}/stop` | Stop a data app |
| `GET` | `/data-apps/{project}/{app_id}/password` | Get data app access password |
| `GET` | `/data-apps/{project}/{app_id}/logs` | Tail data app container logs |
| `GET` | `/data-apps/{project}/{app_id}/secrets` | List data app secrets |
| `PUT` | `/data-apps/{project}/{app_id}/secrets` | Set data app secrets |
| `GET` | `/data-apps/{project}/{app_id}/secrets/{key}` | Get a single data app secret |
| `POST` | `/data-apps/{project}/{app_id}/secrets/remove` | Remove data app secrets |
| `POST` | `/data-apps/validate-repo` | Validate a data app git repo |
| `GET` | `/data-apps/{project}/{app_id}/git-repo` | Get a data app's git repository |
| `GET` | `/data-apps/{project}/{app_id}/git-repo/credentials` | List managed git credentials |
| `POST` | `/data-apps/{project}/{app_id}/git-repo/credentials` | Create a managed git credential |
| `GET` | `/data-apps/{project}/{app_id}/runs` | List a data app's deployment attempts |

### `workspaces` (10 operations)

Snowflake / BigQuery workspaces -- CRUD, load tables, run SQL via Query Service, GC orphans. Mirrors `kbagent workspace *`.

| Method | Path | Summary |
|---|---|---|
| `GET` | `/workspaces` | List workspaces across projects |
| `POST` | `/workspaces/{project}` | Create a workspace |
| `GET` | `/workspaces/{project}/{workspace_id}` | Get workspace detail |
| `DELETE` | `/workspaces/{project}/{workspace_id}` | Delete a workspace |
| `POST` | `/workspaces/{project}/{workspace_id}/password` | Reset workspace password |
| `POST` | `/workspaces/{project}/{workspace_id}/load` | Load tables into a workspace |
| `POST` | `/workspaces/{project}/{workspace_id}/query` | Run SQL in a workspace |
| `POST` | `/workspaces/sql/improve/stream` | Stream AI SQL helper (SSE) |
| `POST` | `/workspaces/{project}/from-transformation` | Create workspace from a transformation |
| `POST` | `/workspaces/gc` | Garbage-collect orphaned workspaces |

## Development

### `branches` (10 operations)

Dev branch lifecycle (create / use / reset / delete / merge) and branch metadata. Mirrors `kbagent branch *`.

| Method | Path | Summary |
|---|---|---|
| `GET` | `/branches` | List branches |
| `POST` | `/branches/{project}` | Create a branch |
| `POST` | `/branches/{project}/use` | Pin the active branch |
| `POST` | `/branches/{project}/reset` | Reset to the default branch |
| `DELETE` | `/branches/{project}/{branch_id}` | Delete a branch |
| `GET` | `/branches/{project}/merge-url` | Get the branch merge URL |
| `GET` | `/branches/{project}/metadata` | List branch metadata |
| `GET` | `/branches/{project}/metadata/{key}` | Get a branch metadata value |
| `PUT` | `/branches/{project}/metadata/{key}` | Set a branch metadata value |
| `DELETE` | `/branches/{project}/metadata/{metadata_id}` | Delete a branch metadata entry |

### `lineage` (8 operations)

Build and query cross-project data lineage (table-level and column-level). Mirrors `kbagent lineage build|show|info`.

| Method | Path | Summary |
|---|---|---|
| `GET` | `/lineage/edges` | List cross-project lineage edges |
| `POST` | `/lineage/build` | Build deep column-level lineage |
| `POST` | `/lineage/show` | Query a built lineage graph |
| `GET` | `/lineage/info` | Show lineage cache summary |
| `GET` | `/lineage/browser` | Open lineage browser UI |
| `GET` | `/lineage/data` | Return raw lineage JSON |
| `GET` | `/lineage/walk` | Walk lineage graph from a node |
| `GET` | `/lineage/mermaid` | Render lineage as Mermaid |

### `semantic-layer` (21 operations)

Model, validate, import/export, diff, promote, and build semantic layer artifacts (datasets, metrics, relationships, constraints, glossary). Mirrors `kbagent semantic-layer *`.

| Method | Path | Summary |
|---|---|---|
| `GET` | `/semantic-layer/models` | List semantic-layer models |
| `POST` | `/semantic-layer/models` | Create a semantic-layer model |
| `DELETE` | `/semantic-layer/models/{model}` | Delete a semantic-layer model |
| `GET` | `/semantic-layer/show` | Show model entities |
| `GET` | `/semantic-layer/validate` | Validate a semantic-layer model |
| `GET` | `/semantic-layer/search-context` | Search semantic contexts by name pattern |
| `GET` | `/semantic-layer/get-context` | Fetch one semantic context by id |
| `GET` | `/semantic-layer/schema` | Fetch JSON Schemas of semantic object types |
| `GET` | `/semantic-layer/export` | Export model snapshot |
| `POST` | `/semantic-layer/diff` | Diff two semantic-layer snapshots |
| `POST` | `/semantic-layer/items/{kind}` | Add an entity to a model |
| `PUT` | `/semantic-layer/items/{kind}/{name}` | Edit a model entity |
| `DELETE` | `/semantic-layer/items/{kind}/{name}` | Remove a model entity |
| `POST` | `/semantic-layer/import` | Import a snapshot into a project |
| `POST` | `/semantic-layer/promote` | Promote a model between projects |
| `POST` | `/semantic-layer/build` | Build a model from tables |
| `POST` | `/semantic-layer/token/encrypt` | Encrypt storage token for transformation |
| `GET` | `/semantic-layer/reference-data` | List reference-data records |
| `PUT` | `/semantic-layer/reference-data` | Create or replace a reference-data record |
| `GET` | `/semantic-layer/reference-data/{record_id}` | Get one reference-data record |
| `DELETE` | `/semantic-layer/reference-data/{record_id}` | Delete a reference-data record |

## AI & Tools

### `kai` (6 operations)

Keboola AI (Kai) -- ping, preflight, single-shot ask, chat with history. Mirrors `kbagent kai *`.

| Method | Path | Summary |
|---|---|---|
| `GET` | `/kai/ping` | Kai liveness probe |
| `GET` | `/kai/preflight` | Inspect Kai readiness |
| `POST` | `/kai/ask` | Single-shot Kai question |
| `POST` | `/kai/chat` | Continue a Kai chat |
| `GET` | `/kai/history` | List recent Kai chats |
| `GET` | `/kai/chat/{chat_id}` | Replay one chat |

### `documentation` (1 operation)

Ask the official Keboola documentation natural-language questions (AI Service docs Q&A). Served under `/documentation` -- NOT `/docs`, which is the auth-exempt Swagger UI namespace. Mirrors `kbagent docs query`.

| Method | Path | Summary |
|---|---|---|
| `POST` | `/documentation/query` | Ask the Keboola documentation a question |

### `ai-chat` (1 operation)

Server-side streaming AI chat (SSE) used by the kbagent web UI. No CLI equivalent.

| Method | Path | Summary |
|---|---|---|
| `POST` | `/ai/chat/stream` | Stream a local AI chat response |

### `agents` (15 operations)

Scheduled / on-demand AI agent tasks. Mirrors `kbagent agent list|show|create|update|delete|run|runs|test`, which reads and writes the same `agents.json` offline. What is server-only is the CRON LOOP: it runs inside `kbagent serve`, so a task with a schedule only fires while the server is up.

| Method | Path | Summary |
|---|---|---|
| `GET` | `/agents` | List scheduled tasks |
| `POST` | `/agents` | Create a scheduled task |
| `GET` | `/agents/{task_id}` | Fetch one task |
| `PATCH` | `/agents/{task_id}` | Update a task |
| `DELETE` | `/agents/{task_id}` | Delete a task |
| `POST` | `/agents/{task_id}/run` | Run a task now (blocking) |
| `POST` | `/agents/{task_id}/run/stream` | Run a task now (SSE stream) |
| `GET` | `/agents/{task_id}/runs` | List recent runs |
| `GET` | `/agents/{task_id}/runs/{run_id}` | Fetch one run |
| `GET` | `/agents/{task_id}/runs/{run_id}/events` | Replay run event timeline |
| `POST` | `/agents/test` | Dry-run an action (no persistence) |
| `POST` | `/agents/test/stream` | Dry-run an action (SSE stream) |
| `GET` | `/agents/cron/preview` | Preview cron firings |
| `POST` | `/agents/prompt/improve` | AI-rewrite a prompt (blocking) |
| `POST` | `/agents/prompt/improve/stream` | AI-rewrite a prompt (SSE stream) |

## Read-only

### `dev-portal` (2 operations)

Developer Portal app discovery -- list a vendor's apps, get one app's full entry. Mirrors `kbagent dev-portal list|get`. Writes and identity management are CLI-only (TTY-confirmed).

| Method | Path | Summary |
|---|---|---|
| `GET` | `/dev-portal/apps` | List Developer Portal apps for a vendor |
| `GET` | `/dev-portal/apps/{app}` | Get one Developer Portal app |

## System

### `health` (6 operations)

Liveness ping, auth-info bootstrap, version, changelog, and doctor checks. `/health/ping` is the only public endpoint -- everything else requires Bearer auth.

| Method | Path | Summary |
|---|---|---|
| `GET` | `/health/ping` | Liveness check |
| `GET` | `/health/auth-info` | Show authentication scheme |
| `GET` | `/version` | Show the kbagent version |
| `GET` | `/ui-config` | Web UI bootstrap configuration |
| `GET` | `/changelog` | List release notes |
| `GET` | `/doctor` | Run health diagnostics |
