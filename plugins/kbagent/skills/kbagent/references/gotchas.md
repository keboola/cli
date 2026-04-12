# Gotchas -- Response Parsing and Common Pitfalls

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

## SQL migration: do NOT use global text replace

When migrating SQL transformations (e.g. removing input mapping and replacing
aliases with direct Snowflake paths), **never use global find & replace**.
A table name like `"orders"` also appears as a **column name** in many places.

Global replace will corrupt:

```sql
-- BEFORE: "orders" is a column name in SELECT
SUM(a."orders") AS "orders"

-- AFTER global replace: column becomes a table path (WRONG!)
SUM(a."tmp.orders") AS "tmp.orders"  -- no such column
```

```sql
-- BEFORE: "country_locality" is a FK column in JOIN ON
ON pcl."country_locality" = cl."id"

-- AFTER global replace: FK column becomes full path (WRONG!)
ON pcl."sapi_1507"."in.c-keboola-ex-db-mysql"."country_locality" = cl."id"
```

**Safe migration approach:**
1. Build a complete destination→source map from `storage.input.tables`
2. Replace ONLY in `FROM` and `JOIN` table-reference positions (not columns)
3. After migration, verify with these regex checks:
   - `alias\."sapi_\d+"` → FK column incorrectly expanded (e.g. `a."sapi_1507"...`)
   - `ON.*=\s*"sapi_\d+"` → bare FK in JOIN ON replaced with path
   - `"tmp\.\w+"` used as column name (not after FROM/JOIN)
4. Verify ALL destination aliases were replaced (none should remain in SQL)
5. Workspace tables created by earlier code blocks (e.g. `"tmp.orders"`) must
   NOT be replaced -- they are runtime artifacts, not input mapping aliases

See [sql-migration-workflow](sql-migration-workflow.md) for the complete
step-by-step procedure.

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

## Common mistakes

- **Forgetting `--json`**: without it, output is human-formatted Rich text, not parseable
- **Assuming `data.projects`**: `project list` returns data as a flat list
- **Passing manage token as argument**: use env var `KBC_MANAGE_API_TOKEN` instead
- **Polling after branch create**: kbagent already waits for async completion
- **Not saving workspace password**: only returned once on creation
- **Putting SQL in _config.yml**: SQL transformations must use `transform.sql` with block markers (see above)
- **Auto-running jobs after config update**: never start a job automatically after pushing config changes -- let the user decide when to run
