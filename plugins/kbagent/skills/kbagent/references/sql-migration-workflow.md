# SQL Transformation Migration Workflow

## When to use

- Removing input mapping from a Snowflake transformation and replacing aliases
  with direct Snowflake paths (e.g. `"orders"` → `"sapi_1507"."in.c-db"."orders"`)
- Materializing tables (adding `CREATE OR REPLACE TABLE "tmp.X" AS` before existing queries)
- Consolidating or splitting transformation code blocks

## Critical rules

1. **Never use global text replace** -- table names overlap with column names
2. **Always work on the correct branch** -- verify branch ID before every read/write
3. **Never send the entire config object** when updating SQL -- only update
   `parameters.blocks[N].codes[M].script` to avoid overwriting storage mappings
4. **Never auto-run jobs** after pushing config -- let the user trigger manually
5. **Always fetch fresh config from API** before edits -- never use stale local files

## Step 1: Build the destination map

Extract the complete alias→Snowflake path mapping from the transformation's
`storage.input.tables`:

```
destination (alias in SQL)  →  source (Snowflake path)
─────────────────────────────────────────────────────
"orders"                    →  "sapi_1507"."in.c-keboola-ex-db-mysql"."orders"
"in.salestargets"           →  "sapi_226"."in.c-ex-google-drive-slevomat"."CZ-Targets-Targets-SR"
```

**Important:** Include ALL entries -- don't skip unusual names like
`CZ-Targets-Targets-SR`, `in.cmteams`, or `in.agenttargets`. These are easy
to miss with pattern matching.

## Step 2: Identify workspace tables

Scan all code blocks for `CREATE ... TABLE "tmp.X"` statements. These are
workspace tables created at runtime -- they must NOT be replaced. Build a list:

```
"tmp.orders", "tmp.carts", "tmp.products", "tmp.vouchers", ...
```

## Step 3: Replace aliases (context-aware)

For each alias in the destination map:

1. **Replace ONLY in table-reference positions:**
   - `FROM "alias"` → `FROM "sapi_NNN"."bucket"."table"`
   - `JOIN "alias"` → `JOIN "sapi_NNN"."bucket"."table"`
   - `CREATE ... TABLE ... AS SELECT ... FROM "alias"` (same rule)

2. **Do NOT replace in:**
   - Column references: `a."orders"`, `SUM("orders")`, `"orders" AS "orders"`
   - JOIN ON conditions: `ON a."column_name" = b."id"`
   - WHERE conditions: `WHERE "column_name" = 'value'`
   - String literals: `'some text with orders in it'`

3. **Context detection heuristic:**
   - If preceded by a dot (e.g. `alias."name"`), it's a column → skip
   - If preceded by FROM/JOIN keyword, it's a table → replace
   - If preceded by a comma in SELECT list, it's a column → skip

## Step 4: Verify completeness

Run these verification checks on the resulting SQL:

### 4a. No remaining aliases
Search for every destination alias from Step 1. None should remain in the SQL
(except where it matches a workspace table name from Step 2).

### 4b. No FK column corruption
Search for these patterns that indicate a column was incorrectly replaced:

```
# FK column with alias prefix expanded to path
regex: \b\w+\."sapi_\d+"

# Bare FK in ON condition replaced with path  
regex: ON.*[=]\s*"sapi_\d+"

# Workspace table name used as column
regex: (?<=\.)"tmp\.\w+"
```

### 4c. No duplicate paths
Search for doubled Snowflake paths (indicates replace was applied twice):
```
regex: "sapi_\d+".*"sapi_\d+".*"sapi_\d+"
```

### 4d. All identifiers quoted
Verify database names like `sapi_226` are always quoted. Unquoted identifiers
become UPPERCASE in Snowflake and will fail:
```
# WRONG (unquoted)
sapi_226."in.c-db"."table"

# CORRECT (quoted)
"sapi_226"."in.c-db"."table"
```

## Step 5: Check workspace table conflicts

If migration materialized new tables (e.g. `"tmp.orders"`), check if any
later code block also creates a table with the same name:

1. Scan ALL code blocks for `CREATE ... TABLE "tmp.X"`
2. If the same `"tmp.X"` appears in multiple code blocks with different schemas:
   - Keep the original name for the "source" table (typically the Setup/first code)
   - Rename the secondary table with a numeric postfix: `"tmp.X2"`
   - Update ALL references to the renamed table within its code block

## Step 6: Test incrementally

After migration, run the transformation and expect it may fail. Common first
failures:

| Error | Likely cause |
|-------|-------------|
| `Object 'alias_name' does not exist` | Alias not replaced (Step 4a missed it) |
| `invalid identifier '"tmp.X"'` | Column corrupted to workspace table name (Step 4b) |
| `Actual statement count N did not match desired count 1` | Missing `ALTER SESSION SET MULTI_STATEMENT_COUNT = 0` |
| `Database 'SAPI_226' does not exist` | Unquoted database name (Step 4d) |
| `invalid identifier '"column"'` | Workspace table conflict (Step 5) |

## Anti-patterns to avoid

- **Sending entire config object via API**: This overwrites storage mappings
  (input/output tables). Only send the specific `script` array for each code block.
- **Reading config from wrong branch**: Always specify the dev branch ID when
  fetching config. `kbagent config detail` supports `--branch ID`.
- **Working from stale local file**: Always fetch fresh from API before edits.
  Local files like `/tmp/current_config.json` go stale after every version bump.
- **Applying the same replacement twice**: Guard against re-replacing already-migrated
  paths. Check if the string already contains `sapi_` before replacing.
