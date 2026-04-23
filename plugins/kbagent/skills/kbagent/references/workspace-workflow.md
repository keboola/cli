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
# Step 4: Once the fix works, update the transformation config via MCP
kbagent --json tool call update_configuration \
  --project ALIAS \
  --input '{"component_id": "keboola.snowflake-transformation", "configuration_id": "CONFIG_ID", ...}'
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

# Query
kbagent --json workspace query \
  --project ALIAS \
  --workspace-id WS_ID \
  --sql "SELECT * FROM \"table1\" LIMIT 10"
```

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

## Shared/linked buckets -- different Snowflake database

Linked buckets (shared from another project) live in a **different Snowflake database**
than the current project's own tables. A workspace only mounts the current project's DB
by default, so querying linked bucket tables requires a fully-qualified 3-part name:

```sql
-- WRONG: linked bucket table not found in workspace DB
SELECT * FROM "in.c-shared-data"."my-table";

-- RIGHT: use the source project's database
SELECT * FROM "sapi_1507"."in.c-shared-data"."my-table";
```

To find the correct database for a linked bucket:

```bash
kbagent --json storage bucket-detail --project ALIAS --bucket-id in.c-shared-data
# Response includes snowflake_path with correct database for each table
```

The `bucket-detail` response resolves the source project and provides ready-to-use
fully-qualified Snowflake paths (e.g. `"sapi_1507"."in.c-shared-data"."my-table"`).

**Rule of thumb**: if `storage buckets` shows "Linked From" for a bucket, always use
`bucket-detail` to get the correct Snowflake path before querying in a workspace.

## Key details

- **Backend auto-detection**: workspace backend (snowflake, bigquery, etc.) is auto-detected from the project. No need to pass `--backend` unless you want to override it.
- **Workspace names**: `workspace list` shows user-given names (from `--name`), not internal IDs.
- **Password**: only returned on creation (headless) or after `workspace password` (reset)
- **Expiration**: workspaces expire server-side automatically
- **Quoting**: Snowflake converts unquoted identifiers to UPPERCASE. Always double-quote database, schema, and table names -- Keboola names are typically lowercase (e.g. `"sapi_901"."in.c-main"."users"`)
- **Query Service**: uses Storage API token for auth -- no Snowflake credentials needed in the query command
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
