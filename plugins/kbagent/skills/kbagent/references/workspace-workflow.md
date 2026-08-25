# Workspace Workflow -- SQL Debugging

Workspaces let you run SQL against real project data without triggering full Keboola jobs.
Use this to debug failing transformations iteratively.

## Option A: Debug an existing transformation

Best when you have a failing transformation and want to reproduce the error:

```bash
# Step 1: Create workspace from transformation (auto-loads input tables)
kbagent --json workspace from-transformation \
  --project ALIAS \
  --component-id keboola.snowflake-transformation \
  --config-id CONFIG_ID

# Response includes: workspace_id, host, database, schema, user, password
# SAVE THE PASSWORD -- it cannot be retrieved later!
```

```bash
# Step 2: Run the original SQL to reproduce the error
# Default (0.59.0+): results come back inline as JSON columns+rows (fast),
# capped at --limit rows (default 500). Add --full for the complete CSV export
# (slower, uncapped) when you need every row.
kbagent --json workspace query \
  --project ALIAS \
  --workspace-id WS_ID \
  --sql "SELECT ..."
```

```bash
# Step 3: Iterate on fixes (no need to run full jobs)
kbagent --json workspace query \
  --project ALIAS \
  --workspace-id WS_ID \
  --sql "SELECT fixed_query ..."
```

```bash
# Step 4: Once the fix works, update the transformation config
kbagent --json transformation edit \
  --project ALIAS \
  --config-id CONFIG_ID \
  --change-description "Fix the query validated in the workspace" \
  --op '{"op": "set_code", "code_id": "b0.c0", "script": ["SELECT fixed_query ..."]}'
```

```bash
# Step 5: Clean up (optional -- workspaces expire automatically)
kbagent --json workspace delete --project ALIAS --workspace-id WS_ID
```

## Option B: Ad-hoc workspace

Best when you want to explore data or run arbitrary queries:

```bash
# Create empty workspace
kbagent --json workspace create --project ALIAS --name "debug-ws"

# Load specific tables (drops existing tables first)
# Default auto-picks the load type per table (clone when eligible, else copy) --
# see "Load types" below.
kbagent --json workspace load \
  --project ALIAS \
  --workspace-id WS_ID \
  --tables in.c-bucket.table1 \
  --tables in.c-bucket.table2

# Or load while keeping existing tables in the workspace
kbagent --json workspace load \
  --project ALIAS \
  --workspace-id WS_ID \
  --tables in.c-bucket.table3 \
  --preserve

# Force clone explicitly -- fails loudly (HTTP 400) instead of silently
# falling back to copy if the table turns out not to be clone-eligible.
# --force is irrelevant here: the size guard only ever applies to copy.
kbagent --json workspace load \
  --project ALIAS \
  --workspace-id WS_ID \
  --tables in.c-bucket.table1 \
  --load-type clone

# Force a real COPY of a table you know is huge and not clone-eligible --
# --force is required (there is no prompt under --json), and a COPY really
# moves the data so it can outrun the 300s default --timeout.
kbagent --json workspace load \
  --project ALIAS \
  --workspace-id WS_ID \
  --tables in.c-bucket.huge_table \
  --load-type copy \
  --force \
  --timeout 600

# Query
kbagent --json workspace query \
  --project ALIAS \
  --workspace-id WS_ID \
  --sql "SELECT * FROM \"table1\" LIMIT 10"
```

## Load types: clone / copy / view

*(since vNEXT, #687)* **Cheapest first**: skip the load entirely if you can
(see "Don't load at all" below) > `clone` (zero-copy, seconds, any table
size) > `view` (zero-storage, narrower eligibility) > `copy` (real data
movement, size-guarded). `workspace load` supports all three as
`--load-type`. **Default (no `--load-type`)**: kbagent decides per table,
mirroring the server's own eligibility rules -- zero-copy `clone` when the
table is on the same backend as the workspace with a full (unfiltered) load
and no external-schema/Analytics-Hub source bucket, otherwise a plain
`copy`. Check `tables[].clone_ineligible_reason` in the JSON to see why a
given table fell back to `copy`.

- **`--load-type clone`** -- zero-copy, metadata-only, finishes in seconds
  regardless of table size. Fails loudly (server HTTP 400) if any requested
  table is not clone-eligible -- it does not silently fall back to `copy`.
- **`--load-type copy`** -- always a real data copy. A table over 1 GiB
  needs interactive confirmation or `--force`. The confirmation prompt only
  exists in human mode (no `--json`); `--json` (the default per Rule 1
  above) never prompts, so a scripted `--json` load of a table that might
  cross 1 GiB needs `--force` up front or it is refused outright. Check
  `dataSizeBytes` via `storage table-detail` beforehand if you want to know
  which one you'll need without a failed dry run.
- **`--load-type view`** -- zero-storage, read-only view, no data movement
  at all. BigQuery: any same-backend bucket. Snowflake: only an
  external-schema bucket with the project feature
  `input-mapping-read-only-storage`.

**Verify what actually happened**: read `tables[].load_type` and
`tables[].data_size_bytes` in the JSON result rather than assume -- with
the auto-default, two requests that look identical can each take a
different path per table.

Loading a large table can take a while even as a copy job; `--timeout`
defaults to 300s for `workspace load` (other storage-job commands keep
60s). If it times out, the storage job **keeps running server-side** --
the error names the job id and how to poll it
(`GET /v2/storage/jobs/{id}`) -- and the CLI now exits 4 (retryable), not 1.

**Don't load at all for read-only analytics.** If you only need to query
production data -- no writes, no need for a workspace-local copy -- a
project with the read-only input-mapping feature exposes it directly to
any workspace via the `KBC_<STACK>_<PROJECT>` shared database.
`workspace query` runs against it with zero load and zero extra storage;
reach for `workspace load` only when the data must actually live inside
the workspace.

## Option C: UI-visible workspace

Use `--ui` when the workspace should appear in the Keboola UI Workspaces tab (slower, ~15s):

```bash
kbagent --json workspace create --project ALIAS --name "shared-debug" --ui
```

## SQL from file

For multi-line SQL, use `--file` instead of `--sql`:

```bash
kbagent --json workspace query \
  --project ALIAS \
  --workspace-id WS_ID \
  --file query.sql
```

## Fast inline results vs `--full` -- mind the result-set volume (since v0.59.0)

`workspace query` has two ways to retrieve results. **Pick based on how many rows
you actually need**, not by habit.

**Default (fast, inline):** reads the result set straight from the Query Service
as JSON via `GET /api/v1/queries/{job}/{stmt}/results`. No file is produced. Each
statement comes back with structured `columns` + `rows` (plus `row_count`,
`total_rows`, `truncated`) and a synthesized `csv_data` for back-compat.

- **Paginated / capped** at `--limit` rows (default 500). When the warehouse has
  more rows than were fetched, the statement is flagged `truncated: true` (with
  `total_rows` = the real count) and the CLI prints
  `Showing first N of TOTAL rows. Use --full for the complete result set.`
- This is the right default for **inspection, row counts, sampling, schema
  checks, and iterating on a fix** -- exactly the workspace-debugging loop.

**`--full` (complete CSV export, slower):** materializes the *entire* result set
as a CSV file through the warehouse UNLOAD path
(`GET .../export?fileType=csv`), then downloads it. Uncapped -- you get every
row -- but it pays a file-export round-trip (warehouse -> object storage ->
download) on every call. Under `--full` the statement carries only `csv_data`
(no structured `columns`/`rows`).

```bash
# Fast: first 500 rows inline (default). Add --limit to widen/narrow the page.
kbagent --json workspace query --project ALIAS --workspace-id WS_ID \
  --sql 'SELECT * FROM "in.c-main"."events"' --limit 1000

# Complete: every row via CSV export (slower -- use only when you need them all).
kbagent --json workspace query --project ALIAS --workspace-id WS_ID \
  --sql 'SELECT * FROM "in.c-main"."events"' --full
```

**Decision guide:**

- **Just looking / counting / sampling?** Use the default. Faster, and the
  `truncated` flag tells you whether there is more.
- **Need a complete extract?** Use `--full` -- but **think about the volume
  first**. `--full` pulls the whole result set into a single CSV string in
  memory; a `SELECT *` over millions of rows is slow and memory-hungry. Narrow
  the query (`SELECT` only the columns you need, add a `WHERE`/`LIMIT`) before
  reaching for `--full`.
- **Bulk-exporting an actual Storage table** (not an arbitrary query)? Prefer
  `storage unload-table` / `storage download-table` -- they stream sliced files
  and are built for volume, whereas `workspace query --full` is for ad-hoc SQL.

**API floor:** the `/results` endpoint requires `100 <= pageSize <= 100000`.
kbagent always requests a valid page size and trims to `--limit` locally, so a
small `--limit` (e.g. `--limit 5`) works fine -- it does not shrink the wire
`pageSize` below the API minimum.

## Shared/linked buckets -- different database/dataset

Linked buckets (shared from another project) live in a **different database
(Snowflake) or dataset (BigQuery)** than the current project's own tables. A
workspace only mounts the current project's DB/dataset by default, so querying
linked-bucket tables requires a fully-qualified path:

```sql
-- Snowflake -- WRONG: linked bucket table not found in workspace DB
SELECT * FROM "in.c-shared-data"."my-table";
-- Snowflake -- RIGHT: use the source project's database
SELECT * FROM "sapi_1507"."in.c-shared-data"."my-table";

-- BigQuery -- WRONG: dataset alone is not enough across projects
SELECT * FROM `in_c_shared_data`.`my_table`;
-- BigQuery -- RIGHT: include the source GCP project
SELECT * FROM `kbc-bq-1507`.`in_c_shared_data`.`my_table`;
```

To find the correct path for a linked bucket, always use `bucket-detail` -- it
adapts to the bucket's backend and emits a ready-to-use, dialect-correct path:

```bash
kbagent --json storage bucket-detail --project ALIAS --bucket-id in.c-shared-data
# Response includes:
#   sql_dialect    -- "snowflake" or "bigquery"
#   tables[].sql_path  -- backend-correct quoting (always present)
# Plus backend-specific keys:
#   Snowflake -> snowflake_database / snowflake_schema / snowflake_path ("...")
#   BigQuery  -> bigquery_dataset (+ bigquery_project when available) /
#                bigquery_path (`...`)
```

Prefer `sql_path` in agent code -- it is correctly quoted for the bucket's
backend without you having to branch on dialect yourself. (since v0.25.3)

**BigQuery FQN caveat**: on Keboola-managed BQ projects the Storage API
returns `databaseName: ""`, so `bigquery_path` ends up dataset-qualified only
(`` `dataset`.`table` ``). To query a linked BQ bucket cross-project, ask the
user for the source GCP project name explicitly and prepend it.

**Rule of thumb**: if `storage buckets` shows "Linked From" for a bucket,
always run `bucket-detail` and use its `sql_path` before querying in a
workspace.

## Key details

- **Backend auto-detection**: workspace backend (snowflake, bigquery, etc.) is auto-detected from the project. No need to pass `--backend` unless you want to override it.
- **Workspace names**: `workspace list` shows user-given names (from `--name`), not internal IDs.
- **Password**: only returned on creation (headless) or after `workspace password` (reset)
- **Expiration**: workspaces expire server-side automatically
- **Quoting** (dialect-specific):
    - **Snowflake**: converts unquoted identifiers to UPPERCASE. Always double-quote database, schema, and table names -- Keboola names are typically lowercase (e.g. `"sapi_901"."in.c-main"."users"`).
    - **BigQuery**: requires backticks (`` ` ``), not double quotes; the dataset name is normalized to underscores (e.g. `` `in_c_main`.`users` ``).
    - Easiest path: read `tables[].sql_path` from `bucket-detail` -- it is already correctly quoted for the bucket's backend (since v0.25.3).
- **Query Service**: uses Storage API token for auth -- no warehouse credentials needed in the query command. Backend-agnostic: runs SELECTs against **both Snowflake and BigQuery** workspaces (BigQuery since v0.58.0; the path was always identical, the gap was classification). BigQuery workspaces carry `login_type: "default"` and are `qs_compatible: true` from v0.58.0 -- earlier versions mislabeled them `false`.
- **Transactional mode**: add `--transactional` to wrap SQL in a transaction

## Orphan detection + garbage collection (since v0.22.0)

Workspaces are backed by `keboola.sandboxes` configs. When a config is deleted
out-of-band (UI cleanup, another CLI, force-delete script), the workspace
record itself can linger. These are **orphaned** workspaces -- they have a
workspace row but no sandbox config.

```bash
# Show only orphaned workspaces
kbagent workspace list --project prod --orphaned

# Preview what would be deleted (no side effects)
kbagent workspace gc --project prod --dry-run

# Delete all orphaned workspaces with confirmation
kbagent workspace gc --project prod

# Skip interactive confirmation (useful in CI or agent workflows)
kbagent workspace gc --project prod --yes
```

Behavior:

- **`workspace list --orphaned`** filters the normal list to workspaces whose
  sandbox config cannot be resolved. Output shape matches `workspace list`.
- **`workspace gc`** deletes each orphaned workspace one by one. Per-workspace
  failures accumulate into `errors[]` without stopping the batch -- one
  locked sandbox does not prevent the rest from being cleaned up.
- **`--dry-run`** surfaces the would-be-deleted list via `data.would_delete[]`
  in JSON mode and a Rich table in human mode.
- Multi-project: `workspace list --orphaned` / `workspace gc` accept
  repeatable `--project` or run against all connected projects when omitted.
- Registered as `destructive` in the permission engine -- blocked by
  `--deny-destructive` / `--deny-writes`.
