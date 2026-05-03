# Gotchas -- Response Parsing and Common Pitfalls

## `storage swap-tables` is dev-branch only and aliases stay put (since v0.28.0)

- `kbagent storage swap-tables --project P --table-id A --target-table-id B
  --branch <ID>` swaps two tables' physical positions in a dev branch
  (`POST /v2/storage/branch/{branch}/tables/{id}/swap`).
- The Storage API rejects this on production. The service refuses with
  exit 5 / `ConfigError` *before* any HTTP call when neither `--branch`
  nor an active branch (via `branch use`) is set.
- **Aliases are NOT transferred.** They keep pointing at the same
  physical position, so after the swap they expose the OTHER table's
  data. Plan downstream config rewrites if any aliased consumer relies
  on schema, not data.
- Typical use: AI agent profiles a typeless table, builds a typed
  rebuild called `<name>_change_log` via CTAS in a dev branch, then
  swaps it back into the original name. After merging the branch the
  original table now carries the typed schema with no downstream config
  rewrite required.

## Manage token: env var is ignored without `--allow-env-manage-token` (since v0.28.0)

- `KBC_MANAGE_API_TOKEN` is no longer auto-resolved on the three
  surfaces that consume it (`kbagent org setup`,
  `kbagent project refresh`, `kbagent data-app password`). Default
  behaviour on 0.28.0+ is **default-deny**: the env var is ignored, a
  TTY hidden-input prompt is shown instead. With no TTY (CI / cron /
  systemd / `< /dev/null`) the resolver exits **2** with the message
  `Error: No manage token available. Run interactively, or pass
  --allow-env-manage-token to read KBC_MANAGE_API_TOKEN from env.`
- To opt in for CI/CD, pass the top-level flag:
  `kbagent --allow-env-manage-token --json org setup ...`. The flag
  belongs in front of the subcommand (it is a top-level option, mirroring
  `--deny-writes`). The flag is session-only -- not persisted, no
  env-var equivalent (intentional; an env-var equivalent would re-create
  the AI-exfiltration hole this default-deny is closing).
- When the env var IS set but the flag IS NOT, you will see a one-shot
  stderr warning `Warning: KBC_MANAGE_API_TOKEN found in environment
  but ignored. Pass --allow-env-manage-token to opt in.`. This is
  informational; the resolver still falls through to the TTY prompt
  (or exits 2 if no TTY). Do NOT suppress this warning by piping stderr
  away -- it tells CI maintainers exactly what to fix.
- The default-deny exists to close the AI-exfiltration risk: any
  subprocess running as the same user (including the AI agent itself)
  inherits env vars, so a manage token in env is reachable by anyone
  who can read `os.environ` or shell out raw `curl`. Default-deny means
  human admin work uses TTY (no env exposure) and CI must explicitly
  say "yes I trust this env" via the flag.
- Storage tokens are unaffected: `KBC_TOKEN` (storage API) keeps
  resolving from env as before.

## `data-app deploy` is required after `config update` -- the running container does NOT auto-pick-up new config versions (since v0.27.0)

- `kbagent config update --component-id keboola.data-apps ...` bumps the
  Storage config version; the deployed container keeps running at the
  OLD version. The Data Science deployment record's `configVersion`
  field is a *pinned pointer* that does not auto-advance when Storage
  advances.
- To roll out the new config, run `kbagent data-app deploy --project P
  --app-id N` (optionally with `--wait`). The CLI reads the latest
  Storage version and `PATCH`es the deployment with the §9 trio
  `{desiredState=running, configVersion, restartIfRunning=true}`.
- **Do NOT** call `PATCH /apps/{id} {desiredState:running}` directly --
  the API silently pins to whatever `configVersion` the deployment
  already had (often the empty shell from `POST /apps`), and the runner
  errors `dataApp.git.repository is required in /data/config.json` with
  no top-level error surfaced. The CLI's `data-app deploy` always sends
  the trio together; sending only `configVersion` returns HTTP 422.
- Same goes for `kbagent data-app start`: it WAKES an auto-suspended app
  at the currently-pinned version. It does NOT roll out new code or
  config -- use `data-app deploy` for that.

## Cross-project KMS ciphertext does NOT decrypt; re-encrypt per project (since v0.27.0)

- The Encryption API's `KBC::Project*` ciphertext is bound to the
  **target project's KMS key**. A `#password` encrypted in project A
  will not decrypt in project B; the Storage API accepts the value but
  the runner fails the `git clone` with "Invalid cipher text for key
  #password" at deploy time.
- `kbagent data-app create` always re-encrypts the plaintext PAT under
  the target project's KMS via the project's Encryption API. Pass the
  PAT via `--git-pat-env VAR` (recommended; no argv leak) or
  `--git-pat-file PATH`. Pre-encrypted ciphertext (`--git-pat-encrypted
  KBC::Project...`) is accepted only when it was encrypted under the
  same project's KMS -- the service refuses to write plaintext if the
  encryption round-trip does not return a project-scoped ciphertext.
- Practical implication: you cannot copy-paste a `KBC::Project*` value
  from one project's `keboola.data-apps` config into another's.

## Transient `state == stopped` during initial data-app deploy is not a failure (since v0.27.0)

- After `data-app create` (or any `data-app deploy --wait`), polling
  may observe `state == stopped` once for ~5-15s before the container
  reaches `running`. This is normal: the platform transitions
  `created → stopped → starting → running` while spinning up the
  runtime. A naive poll that exits on `stopped` would falsely report
  a failure.
- The CLI's `--wait` flag refuses to treat `stopped` as terminal while
  `desiredState == running`. Only `state == running` (success) and
  `state == error` (build failure) and `--timeout` exhaustion are
  terminal in that mode.
- A LATER `state == stopped` (after the app has been running a while)
  is a different beast: it means the platform auto-suspended the
  container after `autoSuspendAfterSeconds` of inactivity. Hit the URL
  to wake it (auto-restart triggers a 30-60s cold boot) or run
  `kbagent data-app start --app-id N`.

## `default_bucket` is per-config and only an output prefix (since 0.26.0)

- `kbagent config set-default-bucket` writes
  `configuration.storage.output.default_bucket`. The Storage API uses this
  value as the bucket for any output table whose `destination` is unset.
  Tables that pin `destination: in.c-...` ignore it.
- The setting lives on the configuration, not the project; configs that
  share a destination bucket each need their own value.
- "Clear" leaves an empty `storage.output: {}` if no other keys live there
  -- intentional, mirrors how `set` creates intermediate parents. Storage
  API treats both `output: {}` and a missing `output` as "use the default
  derived bucket name".
- This is the same setting the support article describes as "raw mode":
  https://keboola.atlassian.net/wiki/spaces/SUP/pages/3770155030/.
- UI exposure is tracked under epic
  [KBCP-108](https://keboola.atlassian.net/browse/KBCP-108).
- Validated against three component types (`kds-team.ex-google-cloud-storage`,
  `keboola.ex-cnb-exchange-rates`, `ex-generic-v2`) -- the runner honors the
  setting at job time regardless of whether the component is row-based or
  what `parameters.config.outputBucket` (Generic Extractor's component-internal
  bucket key) says. The Storage `default_bucket` always wins for tables that
  don't pin their own `destination`.

## `config detail` has a bulk mode (since 0.23.0)

- **Omit `--config-id`** to get every configuration under `--component-id`
  as `{"configs": [...], "errors": [...]}`. Each row is tagged with
  `project_alias` and `branch_id`. Shape is identical to `config list`,
  `storage tables`, etc. Use this instead of forking 100 parallel
  `config detail` subprocesses -- one request per project, not per config.
- **Single-config shape unchanged.** Passing `--config-id` returns the
  original flat dict (`.id`, `.name`, `.configuration`, `.rows`, ...) --
  callers that already parse this shape are unaffected.
- **`--config-id` + multiple `--project` is rejected** (exit 2 /
  `INVALID_ARGUMENT`). A single config lives in exactly one project; the
  CLI refuses to guess. Drop `--config-id` for multi-project fan-out.
- **`--branch` requires exactly one `--project`** in both modes (branch
  IDs are per-project; bulk across branches would mix meanings).
- Bulk mode uses the same `list_components_with_configs` call `config search`
  already uses (include=configuration,rows) -- filtering to `--component-id`
  happens in memory. `config list` returns every component's summary;
  `config detail --component-id X` returns every configuration body of
  component X. Different use cases, same underlying endpoint.

## `config list --include-rows` payload size warning (since 0.23.0)

- The default `config list` response is summary-level: just name,
  description, component, last_modified, folder per config. Cheap and fast.
- `--include-rows` switches the service to
  `list_components_with_configs(include=configuration,rows)` so each row
  carries the full `configuration` dict and its `rows` list. Payload
  grows proportionally to configuration complexity -- a project with
  heavy Snowflake writers can easily return 5-10x more bytes. Use only
  when you actually need the bodies (bulk audit dashboards, scripted
  review across many projects). For just finding strings, prefer
  `config search` -- same endpoint, tighter response.

## `config detail --with-state` runtime-state fetch (since 0.23.0)

- The `state` dict on a configuration is mutable runtime data components
  persist between jobs (last sync cursors, auth refresh tokens, OAuth
  intermediate state). It is **not** part of the summary; you have to
  opt in with `--with-state`.
- **Single mode:** adds one dedicated call to `get_config_state`
  (`GET /v2/storage/components/{cid}/configs/{id}` -- the dedicated
  `.../state` resource is not implemented by Storage API; the state
  field rides on the detail response, which is what `get_config_state`
  reads). The returned `state` key always holds the latest snapshot.
- **Bulk mode:** does NOT fan out one HTTP call per config. Instead it
  adds `include=state` to the single `list_components_with_configs`
  call, so a project with 100 configs returns 100 states in one
  request. No N+1. Parallelism bound by `BaseService._run_parallel`'s
  thread pool (default max_parallel_workers = 10; overridable via
  `KBAGENT_MAX_PARALLEL_WORKERS` or `config.json`).
- Most configs return `state: {}` -- this is normal (the component has
  never written state yet, or state was cleared). Treat `{}` as
  "no state", not an error.

## Variables: attach, don't manage (since 0.21.0)

- `keboola.variables` is an implementation detail. Use
  `kbagent config variables-set/get/clear` -- you never need to create,
  list, or link variables configs manually.
- First `variables-set` auto-creates a sibling `keboola.variables` config
  named `<parent-name>-vars` and links the parent. Subsequent sets update
  the same default row.
- **`variables-clear` does NOT delete the backing variables config** -- it
  may be shared across multiple configs. To actually remove it, run
  `kbagent config delete --component-id keboola.variables --config-id <id>`
  after verifying nothing else references it.
- `--var #KEY=plain` -> encrypted via Encryption API before reaching Storage.
  Fail-closed: encryption failure aborts with `ENCRYPTION_FAILED`. Use
  `--allow-plaintext-on-encrypt-failure` only for bootstrap/debug.
- `--replace` drops any existing keys not in the current `--var` set.
  Default is merge.
- Full workflow + response shapes: see
  [variables-workflow.md](variables-workflow.md).

## `job run` auto-resolves variable values (since 0.21.0)

Transformations with linked `keboola.variables` used to run against empty
strings unless the caller hand-wired a `variableValuesId` at the HTTP
layer. `kbagent job run` now auto-resolves it: reads
`configuration.variables_id` from the parent config (root of the
configuration body -- same key `VariablesService` writes), picks
`configuration.variables_values_id` if set, else the first row of the
linked variables config.

- **Override knobs**: `--variable-values-id ROW_ID` pins a specific row
  (CI runs, what-if analysis); `--no-variables` skips resolution
  entirely. Mutually exclusive -- passing both returns exit 2 /
  `INVALID_ARGUMENT` before any API call.
- **`NO_VARIABLE_ROWS`** -- the linked `keboola.variables` config exists
  but has zero rows. Fix:
  `kbagent config variables-set --project X --component-id C --config-id I --var KEY=VALUE`.
- **`MALFORMED_VARIABLES_ROW`** -- Storage API returned a first row
  without a usable `id`. Fails loud rather than silently submitting with
  empty bindings.
- **Empty `--variable-values-id ""`** (or whitespace) rejected at CLI
  layer with `INVALID_ARGUMENT` -- same silent-omission class as the
  above, caught at a different layer.
- JSON response carries `resolvedVariableValuesId` when the resolver
  fired, so callers verify the binding without a second `job detail`
  round-trip.

## Sync: row deploy & manifest v3 (since 0.21.0)

- `sync push` **does** deploy config rows now (previously silently skipped).
  Row changes in the `pushed_details` array carry `"is_row": true` and
  `"parent_config_id": "..."` so you can distinguish them from parent config ops.
- For `keboola.variables` and `keboola.shared-code` rows, the row's
  `configuration` keys are **hoisted** to the top level of the local YAML
  (`values:`, `code_content:`, etc.) -- NOT wrapped under
  `_configuration_extra`. Edit them directly at the top level.
- `.keboola/manifest.json` auto-upgrades from v2 to v3 on the next successful
  pull or push. v3 adds `rows[].metadata` with per-row pull hashes. v2
  manifests still load cleanly; a downgrade to an older kbagent still reads
  the file via `extra="allow"`.
- Encryption failure on a row push raises `ENCRYPTION_FAILED` from the
  service. If it escapes the per-change handler it maps to CLI exit 1
  (general); if caught per-change it lands in `result["errors"][]` with the
  same code. Fail-closed either way. Use
  `--allow-plaintext-on-encrypt-failure` ONLY for debugging.
- **`keboola.variables` row secrets live in `{name, value}` list
  elements**, not dict keys. An early version of the encryption walker
  only scanned `#`-prefixed dict keys and silently shipped plaintext for
  `values: [{name: '#x', value: '...'}]`; fixed before 0.21.0 shipped
  via `_is_secret_name_value_pair`. (`keboola.shared-code` rows carry
  `code_content: [string]` and have no secrets, so the walker correctly
  never fires there.) If you add a new row-hoist component with yet
  another secret shape, extend the walker -- don't patch callers.
- Row-level deployment internals (manifest v3 hashes, 3-way diff, untracked
  row detection, `ROW_HOIST_COMPONENTS`): see
  [`sync-rows-workflow.md`](sync-rows-workflow.md).

## Response structure varies by command

Not all commands return data the same way. Key differences:

| Command | `data` contains |
|---------|----------------|
| `project list` | A **list** directly (not `data.projects`) |
| `config list` | `{"configs": [...]}` |
| `job list` | `{"jobs": [...]}` |
| `lineage show` | `{"lineage_links": [...], "errors": [...]}` |
| `tool list` | `{"tools": [...]}` |
| `tool call` | `{"results": [...]}` (one per project) |
| `workspace list` | `{"workspaces": [...], "errors": [...]}` |
| `branch list` | `{"branches": [...]}` |
| `config search` | `{"matches": [...], "errors": [...], "stats": {...}}` |
| `storage table-detail` | `{"table_id": ..., "columns": [...], "column_details": [...]}` |
| `storage download-table` | `{"table_id": ..., "output_path": ..., "file_size_bytes": N}` |
| `job terminate` | `{"killed": [...], "already_finished": [...], "not_found": [...], "failed": [...]}` -- four-way partition, NOT a simple success/failure. Always inspect each bucket |

Always check the actual response structure rather than assuming a pattern.

## Multi-project error accumulation

Commands that query multiple projects collect errors per-project without stopping.
One project failing does not block others. Check the `errors` array:

```json
{
  "status": "ok",
  "data": {
    "configs": [...],
    "errors": [
      {"project_alias": "broken-proj", "error_code": "AUTH_ERROR", "message": "..."}
    ]
  }
}
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error |
| 2 | Usage error (invalid arguments) |
| 3 | Authentication error (invalid or expired token) |
| 4 | Network error (timeout, unreachable) -- includes `QUEUE_JOB_TIMEOUT` (local gave up AND the remote-kill attempt failed; the remote job may still be running) |
| 5 | Configuration error (corrupt config, missing alias) |
| 6 | Permission denied (blocked by firewall / `--deny-writes` / `--deny-destructive`) |
| 7 | `JOB_TIMEOUT_TERMINATED` -- `job run --timeout` elapsed AND the remote job was successfully cancelled (since 0.22.0). Scripts can distinguish "we killed it" from "it failed on its own" (exit 1) from "it's still running" (exit 4). |

## `job run --wait` polling + log tail (since 0.22.0)

- Polling follows an exponential curve by default: **2s x 30 -> 5s x 48 -> 15s forever**. For a short test job or a test that needs fast turnaround, pass `--poll-strategy fixed` to force the legacy 1s fixed interval.
- On terminal non-success (`error` / `warning` / `terminated`), kbagent fetches the last N Storage Events and attaches them as `logTail` on the response. Controlled by `--log-tail-lines N` (default 200, max 5000, `0` disables).
  - **Errors:** `error.details.logTail` carries the tail when the job surfaces as an exception (exit 1 / `QUEUE_JOB_FAILED`, exit 4 / `QUEUE_JOB_TIMEOUT`).
  - **Non-error terminals** (`warning` / `terminated`): `logTail` is attached to the top-level result dict (exit 0).
- `--timeout N` is a **local** deadline. When it elapses, kbagent issues `POST /jobs/{id}/kill` against the Queue API. Two outcomes:
  - Kill succeeded -> exit **7** with `details.job` + `details.logTail`. The remote is definitely cancelled.
  - Kill failed -> exit **4** with `details.logTail`, `retryable=True`. The remote **may still be running**; investigate before retrying.
- Inspecting events outside of `job run`: `kbagent job detail --project X --job-id N` does not fetch the log tail. To get the raw event stream, call the Storage Events API directly (`GET /v2/storage/events?runId=<runId>`) with the project token.

## `--deny-writes` / `--deny-destructive` firewall (since 0.22.0)

- Session-only. Flags synthesize a `PermissionPolicy` for the current invocation and merge it with any persisted policy in `config.json`. **Never** written to disk.
- Classes: `--deny-writes` blocks `cli:write` + `tool:write` (covers write+destructive+admin). `--deny-destructive` is narrower -- blocks only `cli:destructive` + `tool:destructive`; pure write ops like `storage create-bucket` stay allowed.
- Blocked operation exits **6** with `error.code = PERMISSION_DENIED`. Read commands stay unaffected.
- Safe to run under either flag without mutating the saved policy -- useful when your agent needs a one-shot read-only run on a machine with a write-enabled config.

## `storage create-table` native types + dev-branch materialize (since 0.25.0)

- **Native types pass through to the Storage API.** `--column pk:VARCHAR(40)`,
  `--column amount:NUMERIC(18,2)`, `--column ts:TIMESTAMP_TZ`,
  `--column meta:VARIANT` all work -- the CLI does only syntactic validation
  (valid identifier, digits+commas length) and Keboola validates
  type/length semantics per backend. Any backend-specific native type
  (Snowflake, BigQuery, Redshift, Synapse) is accepted. The earlier
  whitelist (`STRING, INTEGER, NUMERIC, FLOAT, BOOLEAN, DATE, TIMESTAMP`)
  was removed in 0.25.0.
- **The Storage API derives `basetype`** (`VARCHAR`→`STRING`, `NUMBER`→
  `NUMERIC`, `TIMESTAMP_TZ`→`TIMESTAMP`, `VARIANT`→`STRING`). Do NOT pass
  `basetype` manually; the API will override or reject it.
- **`INTEGER(10)` is invalid.** Keboola's `INTEGER` base type rejects
  length (`'10' is not valid length for INTEGER`). Use
  `--column age:NUMBER(3,0)` instead for narrow integers.
- **`BOOLEAN` defaults must be lowercase.** `--default flag=false`
  succeeds; `--default flag=FALSE` fails with
  `storage.tables.definitionValidation`. The value is normalised to
  uppercase in storage but the input requires lowercase.
- **`--not-null` / `--default` must reference a defined column.**
  Typos exit 2 (`INVALID_ARGUMENT`) before any API call.
- **Dev-branch `create-table` auto-materializes the bucket** when the
  target bucket has not been written to in the branch yet (mirrors the
  official Keboola Go CLI's `EnsureBucketExists`). Response includes
  `auto_created_bucket: true` when this happens. Production writes
  (no `--branch`) never materialize anything.
- **Auto-materialized buckets get `KBC.createdBy.branch.id` stamped**
  (since 0.25.1). On projects with **branched storage** feature flag ON,
  the transformation runner's `output-mapping` rejects buckets without
  this system metadata with `bucket is not assigned to any development
  branch.` kbagent stamps it automatically; the metadata write is
  best-effort (a 403/5xx is logged, create-table still proceeds). If
  another tool created the bucket via raw `POST /v2/storage/branch/<id>/buckets`
  and bypassed kbagent, the bucket will be missing the stamp -- you can
  re-stamp it manually with the Storage API metadata endpoint
  (`POST .../buckets/<id>/metadata` with `provider=system`, key
  `KBC.createdBy.branch.id`, value = branch ID). See
  [storage-types-workflow.md#branched-storage-metadata-stamp-since-0251](../references/storage-types-workflow.md). Closes #224.
- **`storage buckets --branch ID` returns only locally-modified buckets**
  in the branch -- a fresh dev branch lists nothing. That is Storage API
  behaviour, not a CLI bug. Use `storage buckets` (no `--branch`) to see
  production buckets that the branch can read-through.

See [storage-types-workflow.md](storage-types-workflow.md) for the full
type inventory and examples.

## Legacy fake-branch storage warning on `--branch` writes (since 0.25.2)

- **What it is.** Projects without the `storage-branches` feature flag use
  Keboola's legacy fake-branch storage. Writes via `kbagent storage
  create-bucket --branch X` and `storage create-table --branch X` succeed
  at the Storage API level, but the **transformation runner ignores those
  buckets** -- at job time it rewrites `out.c-foo.tbl` to
  `out.c-<X>-foo.tbl` and creates a parallel bucket in the **default
  branch** with the literal branch ID embedded in the bucket name.
- **What kbagent does.** Both write paths consult `verify_token().features`
  once per session (cached on the client) and surface
  `legacy_branch_storage: true` in the JSON response on fake-branch projects.
  Human mode prints a Rich `[yellow]Warning:[/yellow]` line below the success
  message. The behavior of the API call itself is unchanged -- the bucket is
  still created and metadata still stamped (best-effort) -- only the warning
  is new. On `storage-branches`=ON projects the field is `false` and no
  warning is printed.
- **Why it matters for AI agents.** A `kbagent storage create-table --branch
  X` followed by a transformation that targets the same bucket on a
  fake-branch project will see two buckets after the job runs: the one
  kbagent materialized (orphaned, reachable only from `--branch X` view) and
  the one the runner created (`out.c-<X>-...`, in default branch). When the
  user is debugging "why isn't my data here?" the answer is: it's in the
  runner-created bucket, not the kbagent-materialized one. Read the
  warning and surface it to the user.
- **Detection in your own scripts.** Inspect `data.legacy_branch_storage` on
  the JSON response, or call `verify_token().features` directly and check
  for `"storage-branches"`. Both `create-bucket --branch` and `create-table
  --branch` paths surface the flag identically.
- **Migration path.** The right long-term fix is for Keboola Storage to
  finish migrating fake-branch projects to `storage-branches`. Until then,
  the warning is the cleanest signal kbagent can give without changing the
  user-facing command surface. See `storage-types-workflow.md` for the full
  fake-branch vs storage-branches mechanics.

## `sync init --adopt-existing` (since 0.22.0)

- Adopts a `.keboola/manifest.json` written by the kbc Go CLI **in place** instead of overwriting. Idempotent; re-running is a no-op.
- Validates `project_id` from the manifest against the token via `verify_token`. Mismatch exits 5 (`CONFIG_ERROR`) with guidance -- never silently adopts someone else's checkout.
- If no manifest exists, `--adopt-existing` falls through to the normal init path (no error).

## Token handling

- Tokens are always masked in output (e.g. `901-...pt0k`) -- this is normal
- Token can be passed via `--token`, `KBC_TOKEN` env var, or interactive prompt
- Manage API token (since v0.28.0): default-deny on env -- via interactive hidden prompt; pass top-level `--allow-env-manage-token` to opt in to `KBC_MANAGE_API_TOKEN`. Never as CLI argument. See the `(since v0.28.0)` entry at the top of this file.
- Master token for sharing: `KBC_MASTER_TOKEN_{ALIAS}` (e.g. `KBC_MASTER_TOKEN_PROD`) or `KBC_MASTER_TOKEN` as global fallback. Alias is uppercased, hyphens become underscores. Required for `sharing share` and `sharing unshare`; `sharing list/link/unlink` use regular project tokens.

## MCP tool call gotchas

- **Read tools** (multi_project=true): automatically query all projects. No `--project` needed.
- **Write tools** (multi_project=false): require `--project` to specify the target.
- **Auto-expand**: tools like `get_tables` that need `bucket_ids` auto-resolve them by calling `get_buckets` first.
- **Input validation**: tool input is validated against the tool's `inputSchema` before dispatch.
  Only pass parameters defined in the schema. Unexpected parameters cause Pydantic validation errors.
- **Branch scope**: when active branch is set, MCP tools and config commands automatically scope to that branch.
  `branch_id` is a **CLI flag** (`--branch`), NOT a tool input parameter -- do not pass it inside `--input`.
  Config read commands (`config list`, `config detail`, `config search`) also support `--branch`.
- **Storage read commands are the exception**: `storage buckets`, `storage bucket-detail`,
  `storage tables`, `storage table-detail`, and `storage files` **ignore the implicit active
  dev branch** and query production by default. The Storage API branch-scoped endpoint only
  returns resources locally modified in the dev branch (empty for a fresh branch), so
  auto-scoping would surprise users with "No tables found". Explicit `--branch ID` still
  works. Storage **write** commands (create-*, upload-*, delete-*, file-*) stay branch-aware.
- **Schema discovery**: use `kbagent --json tool list` to inspect each tool's `inputSchema` and find
  accepted parameters. For example, `get_configs` takes `configs` (a list of `{component_id, configuration_id}`
  objects), not a flat `config_id` string.

## Conversation ID

Set `KBAGENT_CONVERSATION_ID` env var before running kbagent commands. All API
requests include it as `X-Conversation-ID` header for platform observability.
If unset, the header is omitted.

## Config resolution order

kbagent looks for configuration in this order:
1. `--config-dir` flag
2. `KBAGENT_CONFIG_DIR` env var
3. `.kbagent/` in current or parent directories (local workspace)
4. `~/.config/keboola-agent-cli/` (global)

Use `kbagent init` to create a local `.kbagent/` workspace for per-directory isolation.

## `KBAGENT_PROJECT` environment variable

Lets callers override the default project for one shell/session without editing
`config.json`. A few non-obvious rules:

- **Empty string counts as unset.** `KBAGENT_PROJECT=""` (or a value consisting
  only of whitespace) is treated exactly like the variable not being set at
  all. This follows the standard Unix shell convention and prevents a stray
  `export KBAGENT_PROJECT=` from silently breaking every subsequent command.
- **Points to an unregistered alias -> hard fail.** If the env var names an
  alias that is NOT in your configured projects, write-ops (the ones that
  consult the pin) fail with `CONFIG_ERROR` and exit code 5. Repair either by
  running `kbagent project use <valid-alias>` and unsetting the env var, or by
  `unset KBAGENT_PROJECT`. The CLI will not fall back silently to the persisted
  pin -- that would mask a misconfiguration.
- **Precedence for resolving the target project** (highest wins):
  1. `--project <alias>` CLI flag (explicit per-command)
  2. `KBAGENT_PROJECT` env var
  3. Persisted pin (`default_project` in `config.json`, set via
     `kbagent project use <alias>`)
  4. Sole-project fallback (if exactly one project is configured)
  5. Hard fail with `CONFIG_ERROR` (no ambiguous defaulting)
- `kbagent project current` reports which of (2) or (3) is active and flags
  when the env var points to an unregistered alias, so you can diagnose
  precedence issues without reading the source.

## config update vs MCP update_config

For updating configuration content, prefer `kbagent config update` over MCP's `update_config` tool:

| Feature | CLI `config update` | MCP `update_config` |
|---------|--------------------|--------------------|
| Path reference | Configuration root (`parameters.db.host`) | Relative to `parameters` (`db.host`) |
| Deep merge | `--merge` preserves all sibling keys | Must use correct path or risk data loss |
| Dry-run preview | `--dry-run` shows diff without applying | Not available |
| Performance | ~1s (direct API call) | ~3-4s (MCP subprocess overhead) |
| Input source | Inline JSON, `@file.json`, stdin (`-`) | Inline JSON only |

**Key difference**: CLI paths start from the configuration root. MCP paths are relative to
the `parameters` object. Using `path: "parameters.tables"` in MCP actually resolves to
`parameters.parameters.tables` (double nesting), which causes confusing failures.

**When to use MCP's `update_config`**: Only for `str_replace` and `list_append` operations
which are not available in the CLI command. For `set` operations, always prefer CLI.

**Examples:**
```bash
# Set a single nested value (--set implies merge)
kbagent --json config update --project P --component-id C --config-id ID \
  --set "parameters.db.host=new-host.example.com"

# Deep-merge a partial JSON (preserves all siblings)
kbagent --json config update --project P --component-id C --config-id ID \
  --configuration '{"parameters": {"tables": {"new": "data"}}}' --merge

# Preview changes before applying
kbagent --json config update --project P --component-id C --config-id ID \
  --set "parameters.config.debug=false" --dry-run

# Update from a file
kbagent --json config update --project P --component-id C --config-id ID \
  --configuration-file updated-config.json --merge
```

## Batch size limits for update_sql_transformation

When using `update_sql_transformation` with `str_replace` operations, **limit batches
to 50 operations maximum**. Larger batches (150+) may trigger a Storage Events API
size limit: the replacements are applied and a new version is created, but the MCP
server fails to log the change event and returns `isError: true` with
`400 Bad Request: Request too large`. This creates a confusing state where changes
were saved but the tool reports failure.

Workaround for large refactors (e.g. removing `AS` from 200 table aliases):
1. Split operations into batches of 50
2. Call `update_sql_transformation` once per batch
3. Verify each batch succeeded before sending the next

## SQL transformation file layout

When creating or editing SQL transformations via sync, SQL code must go in
`transform.sql`, NOT in `_config.yml`. The `_config.yml` for transformations
should have `parameters: {}` (empty).

**Wrong** -- putting SQL in `_config.yml` parameters:
```yaml
# DO NOT DO THIS -- SQL will be split per line and each line executed separately
parameters:
  blocks:
    - name: Block 1
      codes:
        - name: Code 1
          script:
            - CREATE TABLE foo AS
            - "    SELECT col1"
            - "    FROM bar;"
```

**Correct** -- SQL in `transform.sql`, config has empty parameters:
```yaml
# _config.yml
parameters: {}
```
```sql
-- transform.sql
/* ===== BLOCK: Block 1 ===== */

/* ===== CODE: Code 1 ===== */
CREATE TABLE foo AS
    SELECT col1
    FROM bar;
```

See `scaffold-workflow.md` for the complete file structure reference.

## `config update` auto-normalizes `script[]` from string to array (since v0.28.0)

The Storage API silently accepts a string for `parameters.blocks[].codes[].script`,
but the Keboola runtime validator rejects it with:

```
Invalid type for path "root.parameters.blocks.0.codes.X.script".
Expected "array", but got "string"
```

The trap: the failed PUT returns 200, the version increments, the UI looks
fine -- the crash happens only when the job runs (often hours later, e.g. by
the scheduler), with no attribution back to the offending write. Reported in
issue #245 after a programmatic refactor of 3 production Snowflake
transformations.

`kbagent config update` (and any wrapper that takes a full configuration --
`--configuration`, `--configuration-file`, `--set parameters.blocks.0.codes.0.script=...`,
and dry-run preview) now closes the gap on the write side **before** the
Storage API touch:

- **SQL transformations** (Snowflake / Synapse / Oracle / Redshift /
  BigQuery / DuckDB, plus fragment fallback for `*-exasol-transformation`,
  `*-teradata-transformation`, etc.): the string is split on statement
  boundaries via the existing `split_statements()` state machine that
  already powers `kbagent sync push`. The splitter respects `'...'` /
  `"..."` / `$$...$$` / `--` / `#` / `//` / `/* ... */`, so semicolons
  inside string literals and block comments do NOT cause splits.
- **Python / R / `kds-team.app-custom-python`** and any other component
  sharing the `parameters.blocks[].codes[].script` shape: the string is
  wrapped as a single-element array `[script]`. Statement-level split
  does not apply -- the runtime treats the script as one code chunk.
- **Already-array `script` values pass through unchanged.**

Observability: every normalization is surfaced.
- JSON mode: the result envelope gains
  `"normalizations": [{"path": "parameters.blocks[0].codes[0].script",
  "action": "sql_split", "before_type": "str", "after_type": "list",
  "after_length": 3}]`. Empty list when nothing was normalized.
- Human mode: a yellow `Auto-normalized N script field(s) to array
  (string -> list). See --json for details.` warning followed by a
  per-element trace.
- `--dry-run`: the `new_configuration` field already reflects the
  post-normalize shape, so the preview matches what would actually land.

**The trap still exists when bypassing kbagent.** Direct
`PUT /v2/storage/components/{component}/configs/{config}` calls (curl,
custom Python, the MCP `update_sql_transformation` / `create_sql_transformation`
tools as of MCP v1.59.x) do NOT inherit this normalization. If an LLM agent
is composing the configuration JSON itself, prefer
`kbagent config update --configuration ...` over raw REST or MCP tool calls
for SQL transformations -- that way the normalization fires regardless of
upstream client behaviour.

Bonus fix in 0.28.0: `kbagent sync push` previously did NOT split semicolons
in BigQuery / DuckDB transformations because those component IDs were
missing from `SQL_TRANSFORMATION_COMPONENTS`. Push collapsed multiple
statements into one `script` element, mirroring closed issue #119 on a
different backend. The 0.28.0 registry now covers BQ / DuckDB explicitly,
plus fragment-based fallback for future / self-hosted SQL backends.

## Snowflake: MULTI_STATEMENT_COUNT

Keboola sends each code block to Snowflake as a single query batch via the ODBC
driver. Snowflake's default `MULTI_STATEMENT_COUNT = 1` means **only one SQL
statement per batch**. If a code block contains multiple statements (e.g.
`SET` + `CREATE TABLE` + `CREATE TABLE`), the job fails with:

```
Actual statement count N did not match the desired statement count 1
```

**Fix:** Add this as the **first code block** in your transformation:

```sql
ALTER SESSION SET MULTI_STATEMENT_COUNT = 0;
```

This allows unlimited statements per code block. The setting persists for the
entire transformation session. Many existing transformations already have this
-- check before adding a duplicate.

**Note:** This is NOT a Keboola bug. It is a Snowflake ODBC driver default.
Semicolons between statements are required and are NOT the problem -- the
session parameter is.

## Snowflake: identifier quoting (case sensitivity)

Snowflake converts **unquoted identifiers to UPPERCASE**. This means:
- `sapi_226` without quotes → Snowflake looks for `SAPI_226` → **not found**
- `"sapi_226"` with quotes → Snowflake uses `sapi_226` as-is → **works**

**Rule:** Always double-quote ALL parts of Snowflake direct-access paths:

```sql
-- CORRECT: all three parts quoted
SELECT * FROM "sapi_1507"."in.c-keboola-ex-db-mysql"."orders"

-- WRONG: database name unquoted → becomes SAPI_1507
SELECT * FROM sapi_1507."in.c-keboola-ex-db-mysql"."orders"
```

This applies to linked bucket paths (`sapi_NNNN`), native bucket paths, and
any identifier containing dots, hyphens, or lowercase letters.

## SQL editing: do NOT use global text replace on identifiers

This applies to ANY operation that rewrites a table or column name in SQL:

- **Renaming** -- changing a table name (`"orders"` → `"objednavky"`)
- **Migration** -- removing input mapping, replacing aliases with direct
  Snowflake paths (`"orders"` → `"sapi_1507"."in.c-db"."orders"`)
- **Refactoring** -- consolidating duplicate workspace tables, changing
  prefixes (`"tmp.X"` → `"stg.X"`)

In all of these, **never use global find & replace**. A table name like
`"orders"` almost always also appears as a **column name** somewhere
(FK reference in `JOIN ON`, aggregation alias in `SELECT`, `WHERE` clause).

Global replace corrupts every scenario:

```sql
-- BEFORE: rename table "orders" → "objednavky"
SELECT SUM(a."orders") AS "orders" FROM "orders" a

-- AFTER global replace (WRONG!):
SELECT SUM(a."objednavky") AS "objednavky" FROM "objednavky" a
-- The column reference and the SELECT alias were renamed too --
-- only the FROM table should have changed.
```

```sql
-- BEFORE: migrate "orders" alias to Snowflake path
SUM(a."orders") AS "orders"

-- AFTER global replace (WRONG!): column becomes a table path
SUM(a."tmp.orders") AS "tmp.orders"  -- no such column
```

```sql
-- BEFORE: "country_locality" is a FK column in JOIN ON
ON pcl."country_locality" = cl."id"

-- AFTER global replace (WRONG!): FK column becomes full path
ON pcl."sapi_1507"."in.c-keboola-ex-db-mysql"."country_locality" = cl."id"
```

**Safe approach (for any rename, migration, or refactor):**

1. Replace ONLY in **table-reference positions**:
   - After `FROM` keyword
   - After `JOIN` keyword
   - In `CREATE ... TABLE "name"` declarations
   - In `INSERT INTO "name"` / `UPDATE "name"` / `DELETE FROM "name"`
2. Do NOT replace in:
   - Column references: `a."orders"`, `SUM("orders")`, `"orders" AS "orders"`
   - `JOIN ON` conditions: `ON a."col_name" = b."id"`
   - `WHERE` conditions, string literals (`'... orders ...'`)
3. **Context detection heuristic:**
   - Preceded by a dot (`alias."name"`) → column, skip
   - Preceded by `FROM` / `JOIN` keyword → table, replace
   - Inside `SELECT` list (between commas, no FROM yet) → column, skip
4. After editing, verify with regex:
   - **Rename**: search for the new name in column positions
     (`alias\."newname"`, `SUM\("newname"\)`) -- must be zero hits
   - **Migration**: `alias\."sapi_\d+"`, `ON.*=\s*"sapi_\d+"`,
     `"tmp\.\w+"` used as column
   - Verify all old occurrences in table positions are gone
5. Workspace tables created by earlier code blocks (e.g. `"tmp.orders"`)
   must NOT be replaced -- they are runtime artifacts, not aliases.

For input mapping migration specifically, see
[sql-migration-workflow](sql-migration-workflow.md) for the full
step-by-step procedure including building the destination→source map.

## Workspace table name conflicts

When multiple code blocks in a transformation create a workspace table with
the **same name** but different schemas, downstream code blocks may fail
because they expect columns from the original version.

**Example:** Code 0 (Setup) creates `"tmp.carts"` with all MySQL columns.
Code 23 later creates `"tmp.carts"` with only 3 columns. Code 25 then fails
because it needs column `"user"` which Code 23's version doesn't have.

**Rule:** When a conflict exists, rename the **secondary** table by adding a
numeric postfix (`"tmp.carts2"`). Keep the original name for the "source"
table (typically the Setup/materialization code that creates the full copy).
Update all references in the code that creates and uses the renamed table.

## Auto-update

kbagent automatically checks for updates on every invocation. When a newer version
is available on PyPI, it installs the update and re-executes the same command
seamlessly. This is transparent -- no user action required.

- Opt-out: `KBAGENT_AUTO_UPDATE=false`
- Version cache: checks PyPI at most once per hour
- Skipped for: dev/editable installs, `update`/`version` commands
- Never crashes the CLI -- update failures are silently ignored

## `lineage build` and sync layouts

`lineage build` reads synced data from disk and supports both layouts produced by
`kbagent sync pull`:

- **Flat** (after `sync pull --project X`): `./.keboola/manifest.json` directly in CWD.
- **Nested** (after `sync pull --all-projects`): `./<alias>/.keboola/manifest.json`
  for each project side by side.

Pass the matching directory to `--directory` / `-d`:

- Flat: `kbagent lineage build -d . -o lineage.json`
- Nested: `kbagent lineage build -d /path/to/parent -o lineage.json`

If the scan finds zero projects, the build still writes the cache file but
emits a warning (both in the human-readable output and as a `warnings` array
in `--json` mode) with a hint about the expected layouts. In JSON mode, inspect
`result["data"]["warnings"]` to detect this situation programmatically.

## Sync and dev branches

When an active branch is set (`branch use --branch ID`), sync commands automatically
scope to that branch:

- `sync pull` writes configs into a **separate directory** named after the branch
  (e.g. `fix-etl/` instead of `main/`)
- `sync diff` and `sync push` read/write from the correct branch directory
- The manifest tracks all branches in `manifest.branches[]`
- Switching back to main (`branch reset`) makes sync target `main/` again

This means you can have production and dev branch configs side by side on disk
without them overwriting each other.

## --hint mode: generate Python code

Use `--hint` to generate equivalent Python code instead of executing a command:

```bash
kbagent --hint client config list --project myproj   # direct API calls
kbagent --hint service config list --project myproj  # service layer with CLI config
```

Two modes:
- **`--hint client`**: generates code using `KeboolaClient` with explicit URL + token
- **`--hint service`**: generates code using the service layer with `ConfigStore`

Important: `--hint` requires a value (`client` or `service`). Writing just `--hint`
without a value will cause a parsing error.

See [docs/hint-mode.md](../../../../../docs/hint-mode.md) for full documentation.

## Common mistakes

- **Forgetting `--json`**: without it, output is human-formatted Rich text, not parseable
- **Assuming `data.projects`**: `project list` returns data as a flat list
- **Passing manage token as argument**: use the interactive prompt (default since v0.28.0), or `--allow-env-manage-token` + `KBC_MANAGE_API_TOKEN` env var for CI
- **Polling after branch create**: kbagent already waits for async completion
- **Not saving workspace password**: only returned once on creation
- **Putting SQL in _config.yml**: SQL transformations must use `transform.sql` with block markers (see above)
- **Auto-running jobs after config update**: never start a job automatically after pushing config changes -- let the user decide when to run

## Project description vs branch description

The "description" shown on the Keboola project dashboard is **not** the same
field as a branch's `description` attribute:

- **Dashboard project description** = `KBC.projectDescription` metadata on the
  **default (main) branch**. Set via `kbagent project description-set` (or
  generically `kbagent branch metadata-set --key KBC.projectDescription --branch default`)
- **Dev branch description** = the `description` field on a dev branch record.
  Set via `kbagent branch create --description "..."`; visible in the branch
  switcher and synced as `description.md` by the kbc CLI

They live at different endpoints in the Storage API
(`/v2/storage/branch/{id}/metadata` vs. `/v2/storage/dev-branches/{id}`),
so setting a branch's description will **not** update the dashboard.

## Storage descriptions: key convention + precedence + partial failures

`kbagent storage describe-bucket / describe-table / describe-column / describe-batch`
write descriptive metadata onto storage objects. Three behaviors are easy to miss:

- **Column descriptions use a metadata-key convention, not a column endpoint.**
  The Keboola Storage API has no user-writable column-level metadata endpoint,
  so `describe-column` stores each description as a `KBC.column.{name}.description`
  entry on the **table's** metadata (upsert). `storage table-detail` reads them
  back via the same key and surfaces them under `column_details[].description`.
  Renaming or deleting a column does NOT automatically clean these entries up
  (they remain on the table's metadata under the old name). Same convention for
  table and bucket descriptions: stored as `KBC.description` (provider=user) on
  the object's metadata.
- **`describe-batch` is partial-failure-tolerant.** Item-level errors are
  collected into `result.errors[]` but the batch keeps processing the remaining
  items. The CLI exits non-zero only if `error_count > 0`, so in scripts always
  inspect `errors[]` (or at least `error_count`) rather than relying solely on
  the exit code — and when consuming `--json` output, never trust a zero-exit
  as "everything applied."
- **Description-field precedence: metadata wins.** When both the native Storage
  API `description` field and a user-provided `KBC.description` (provider=user)
  metadata entry are present, `storage bucket-detail` / `storage table-detail`
  surface the **metadata value**. The native field is only settable at object
  creation time via the Storage API; all user updates flow through the metadata
  endpoint, so the metadata entry is the authoritative source. `KBC.description`
  entries whose provider is not `user` (e.g. `system`) are ignored during
  read-back and the native field is used as fallback.
- **`storage bucket-detail` is dialect-aware** *(since v0.25.3)*. Output adapts
  to the bucket's backend:
  - **Snowflake**: `snowflake_database` / `snowflake_schema` and per-table
    `snowflake_path` quoted with `"DB"."schema"."table"`.
  - **BigQuery**: `bigquery_dataset` (and `bigquery_project` when surfaced via
    API `databaseName`) and per-table `bigquery_path` quoted with backticks
    (`` `dataset`.`table` `` or `` `project`.`dataset`.`table` ``).
  Backend-agnostic keys `sql_dialect` (`"snowflake"` / `"bigquery"`) and
  per-table `sql_path` are always present -- prefer them in agent code instead
  of branching on backend yourself. The misleading `snowflake_database` /
  `snowflake_schema` / `snowflake_path` keys are **NOT** emitted on BigQuery
  results in 0.25.3+. *Pre-0.25.3 behaviour:* the function unconditionally
  emitted Snowflake-style keys with double quotes regardless of backend; on a
  BigQuery bucket this produced syntactically invalid SQL (BQ requires
  backticks) AND a fabricated `f"sapi_{project_id}"` database name (BQ has no
  such naming convention). If you see a 0.25.2-or-older bucket-detail JSON
  saved offline against a BQ project, treat the `snowflake_*` fields as
  garbage. The `f"sapi_{project_id}"` Snowflake fallback (when `backendPath`
  is missing) still fires for Snowflake buckets but no longer for BigQuery.
- **BigQuery `databaseName` is usually empty** *(since v0.25.3)*. On Keboola-
  managed BQ projects the Storage API returns `databaseName: ""`, so
  `bucket-detail` cannot construct a fully-qualified `project.dataset.table`
  path -- the resulting `bigquery_path` is dataset-qualified only
  (`` `dataset`.`table` ``) and `bigquery_project` is the empty string. If the
  user needs a full FQN (e.g. for a query against the GCP console or for an
  external tool), ask them for the GCP project name explicitly. On BYODB BQ
  projects `databaseName` is populated and the full FQN is emitted.

## `job terminate` quirks

Queue API's kill endpoint (`POST /jobs/{id}/kill`) has a few non-obvious behaviors the
CLI hides via its four-bucket response, but they matter when interpreting results:

- **Kill is asynchronous.** A successful `killed` entry has
  `desiredStatus=terminating` but the actual `status` does not change immediately.
  The job transitions to `cancelled` (if it was `waiting`) or `terminated`
  (if it was `processing`) within a few seconds. Poll `job detail` for
  `isFinished=true` before assuming it's done.
- **`processing` is transient in the middle of termination.** Between the
  accepted kill and the terminal state, you may briefly observe
  `status=terminating` -- still `isFinished=false`. Don't treat it as an error.
- **Re-terminating a finished job is safe.** Queue API returns HTTP 400 for
  already-terminal jobs; the CLI reports them in `already_finished` rather than
  `failed`. This also covers race conditions where a job finishes between
  `list` and `terminate`.
- **Bogus or already-`success`/`error` IDs hit an inconsistency:** Queue API
  returns HTTP 500 with body `code=404`. The CLI verifies via GET: if the job
  exists and is finished, it lands in `already_finished`; if GET returns 404,
  it lands in `not_found`.
- **`--status` filter is client-side for branches.** Queue API's `/search/jobs`
  does not accept a branch parameter, so `--branch ID` is applied by filtering
  the listed jobs on `branchId`. If you need pristine branch scoping, consider
  using the IDs returned from `job list --status processing` and passing them
  explicitly with `--job-id`.
- **`--status any` is the right default for runaway cleanup.** It fetches all
  recent jobs (no status filter) and keeps only `created`/`waiting`/`processing`
  client-side. Picking a single status misses the other killable states -- e.g.
  a runaway loop often piles up `waiting` jobs while you're typing
  `--status processing`.

## Parquet export: slices, not a single file

- `storage unload-table --file-type parquet` always produces a **sliced** output.
  With `--download`, the result is a **directory** (`./{project}/{table_id}.parquet/`),
  never a single `.parquet` file. If your code expects a single file, adapt it to
  read the directory as a Parquet dataset:
  ```python
  import pyarrow.parquet as pq
  t = pq.read_table("./ALIAS/in.c-bucket.table.parquet/")
  ```
- **Never concatenate Parquet slices.** Each slice is a self-contained Parquet
  file with its own footer. Binary concatenation (how CSV slices are merged)
  would produce an invalid file. For the same reason, `storage file-download`
  auto-detects sliced `.parquet` files and routes them to the per-slice
  downloader -- there is no flag to force single-file mode.
- The manifest sidecar is written as `_manifest.json` (**with a leading
  underscore**). This is intentional: Hive/Spark/pyarrow parquet readers skip
  files starting with `_` or `.` when scanning a directory as a dataset, so the
  manifest is preserved for traceability without breaking direct reads. Same
  convention as `_SUCCESS`, `_metadata`, `_common_metadata` in Hadoop.
- The default path `./{project_alias}/{table_id}.parquet/` mirrors Keboola
  addressing. When exporting multiple tables, each ends up in a predictable
  subdirectory and there is no risk of name collisions. Override with
  `--output DIR` if you need a custom location.

## Flow: default `--component-id` differs between commands

- `kbagent flow new` defaults to **`keboola.flow`** (the newer format).
- `kbagent flow detail / update / delete / schedule / schedule-remove` all
  default to **`keboola.orchestrator`** (the legacy format, since most
  existing flows still use it).
- Consequence: if you create a flow with `flow new` and then call
  `flow detail` without `--component-id`, you will get a `NOT_FOUND` error
  because kbagent looks up the ID under `keboola.orchestrator`. Always pass
  `--component-id keboola.flow` when round-tripping a flow you just created
  via `flow new` (or, equivalently, pass `--component-id keboola.orchestrator`
  on `flow new` to keep things consistent).
- `flow list` returns both component IDs and surfaces `component_id` on each
  row — use it to confirm which variant a flow lives under before issuing
  detail/update/delete/schedule commands.

## `schedule find --cron-window` is an hour-field approximation

`kbagent schedule find --cron-window "02:00-04:00"` is an **audit helper, not a real cron evaluator**. It parses the *hour* field of the cron expression and asks whether every hour at which the cron fires falls inside the passed window. It deliberately does **not** account for minute precision, day-of-month restrictions, or day-of-week restrictions.

- **Minutes in `--cron-window` are syntactic sugar, but still validated.** The spec `02:00-04:00` is accepted in the `HH:MM-HH:MM` format because it matches how people describe time windows; the matcher itself only uses the hour part. A cron expression `*/10 2-4 * * *` (every 10 minutes within hours 2-4) matches `--cron-window "02:30-04:30"` exactly the same as `--cron-window "02:00-04:00"`. Minute values outside `00-59` (e.g. `02:70-04:88`) are still rejected at parse time so obviously malformed inputs fail loudly.
- **Hour field `*` (fires every hour) never matches a bounded window.** This is intentional: from an audit standpoint, "fires every hour" is the opposite of "confined to a 2-hour window". If you wanted to catch those, pass `--cron-window "00:00-23:00"` (or skip the window filter entirely).
- **Wrap-around windows are not supported.** `--cron-window "22:00-02:00"` returns an error. The error message itself points you at the workaround: split into two passes (`22:00-23:00` and `00:00-02:00`) and union the results in your script.
- **`,` lists, `-` ranges, and `*/N` steps on the hour field are all expanded.** Unparseable inputs fail safe to "no match" rather than "match everything" -- cleanup audits should never accidentally widen.
- **Day-of-week / day-of-month restrictions are ignored.** A cron that only runs on Mondays is matched as if it fired every day. For most audit use-cases this is the right default: "which schedules *can* fire in this window?" is more operationally useful than "which schedules *will* fire today?".

If you need full cron semantics (e.g. "what's the next time this cron fires?") pipe the schedule list into `croniter` or a similar library from your own script -- the CLI deliberately stays out of that space.

## `schedule find` without filters -- `last_run_at` and `matches_cron_window` are `null`

`kbagent schedule find` always emits `last_run_at` and `matches_cron_window` on every row, but **populates them only when the corresponding filter is active**. Without filters both columns are `null`; with `--cron-window` only `matches_cron_window` is populated; with `--not-run-since` only `last_run_at` is populated.

Why not always populate? Because `last_run_at` costs one `list_jobs(limit=1)` Queue API call per unique parent config per project -- paying that unconditionally, to populate a column nobody asked for, is a pointless audit-wide latency hit. `matches_cron_window` is only meaningful relative to a user-supplied window.

- **LLM/agent callers:** do not treat `matches_cron_window: true` as an affirmative signal unless `filters.cron_window` in the response envelope is populated. Before 0.23.0 this defaulted to `true` everywhere, which was misleading.
- **Force `last_run_at` population without filtering:** pass `--not-run-since 0`. That fires the Queue API lookup for every row (N extra calls per project) and returns every row (no staleness filter applied because any past timestamp counts as stale at threshold 0 and `null` also counts as stale).
- **`--not-run-since` + `--branch <DEV_ID>`:** the Queue API has no branch parameter. The timestamp comparison still hits production jobs, so schedules in a dev branch that were freshly deployed will register as stale even if their parent ran on main moments ago.
- **`_fetch_latest_job_ts` silently returns `None` on Queue API errors** -- permission problems on Queue API are invisible in `errors[]`. If one project shows a suspiciously uniform "never ran" cluster, run `kbagent job list --project <alias>` to sanity-check the token.

## `schedule list` + `schedule find` payload size scales with project size, not schedule count

Both commands issue one `list_components_with_configs(branch_id=...)` call per project. That endpoint returns **every** component's configurations + rows + full configuration bodies -- not just `keboola.scheduler`. For a 50-component x 5-config project that is 250 configurations on the wire per project just to extract a handful of scheduler configs + parent names.

The trade-off is deliberate: one big call avoids the O(unique-parents) round-trip a smaller `list_component_configs("keboola.scheduler")` + per-parent `get_config_detail` path would cost, and the parent-name join happens in memory for free. For typical 14-project audits this finishes in seconds. If you encounter memory pressure on unusually large projects, split the audit per-project (`--project X`) instead of fanning out wide. `flow list --with-schedules` uses the lighter per-component path because it does not need the parent-name join that schedule-side audits do.

## Flow: `schedule` is an upsert (no `schedule-update`)

- `kbagent flow schedule` creates a `keboola.scheduler` config on first run
  and **updates the existing one in-place** on subsequent runs. Running it
  twice with different `--cron` values replaces the schedule — it does not
  create a second one. That's why there is no separate `flow schedule-update`
  command.
- To inspect or remove schedules: `kbagent flow schedule-remove` deletes all
  scheduler configs that target the flow. Pair it with `--dry-run` to see the
  affected configs (cron + timezone) without calling `delete_config`.
