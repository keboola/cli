# Gotchas -- Response Parsing and Common Pitfalls

## `job run` now auto-resolves variable values

Transformations with linked `keboola.variables` used to run against empty strings
unless the caller hand-wired a `variableValuesId` at the HTTP layer. `kbagent job run`
now auto-resolves it: reads `configuration.variables_id` from the parent config
(root of the configuration body — same key `VariablesService` writes), picks
`configuration.variables_values_id` if set, else the first row of the linked
variables config.

**Override knobs:**
- `--variable-values-id ROW_ID` -- use a specific values row (CI runs, what-if analysis).
- `--no-variables` -- skip resolution entirely (components without variables, or
  intentionally running with empty bindings).

**Error cases:**
- `NO_VARIABLE_ROWS` -- the linked `keboola.variables` config exists but has zero
  rows. Fix: `kbagent config variables-set --project X --component-id C --config-id I --var KEY=VALUE`.
- The JSON response now carries `resolvedVariableValuesId` when the resolver fired,
  so you can verify the job bound to the right row.

`--variable-values-id` and `--no-variables` are mutually exclusive; passing both
returns exit 2 / `INVALID_ARGUMENT` before any API call.

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
| 4 | Network error (timeout, unreachable) |
| 5 | Configuration error (corrupt config, missing alias) |

## Token handling

- Tokens are always masked in output (e.g. `901-...pt0k`) -- this is normal
- Token can be passed via `--token`, `KBC_TOKEN` env var, or interactive prompt
- Manage API token: only via `KBC_MANAGE_API_TOKEN` env var or interactive prompt (never as CLI argument)
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
- **Passing manage token as argument**: use env var `KBC_MANAGE_API_TOKEN` instead
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
