# Typify Workflow -- Convert Typeless Storage Tables to Native Data Types

End-to-end procedure for an AI agent (or operator) to take a Storage table
that has no column types (every column stored as `STRING(16777216)` --
the Keboola default before #25) and rebuild it with proper Snowflake /
BigQuery native types **without breaking downstream configs**. The
canonical move is:

```
[typeless table]  -->  profile in workspace  -->  CTAS to typed sibling
                                                      |
                                                      v
                       [typed table]  <-- swap-tables --  [typed sibling]
                       (downstream sees real types)        (now holds the old typeless data)
```

**Two-stage model -- rehearse in a dev branch, apply in production.**
Dev-branch merge propagates only *configurations*, NOT storage table
schema (see `gotchas.md` "Dev-branch merge carries only configurations"),
so a swap done inside a dev branch never reaches production via merge. The
dev branch is therefore a **rehearsal**: profile the data, build the typed
sibling, swap, and run downstream configs against it to *prove the typed
schema is consumer-safe*. Once proven, **discard the branch** and run the
real build + swap directly in the production (default) branch -- a
default-branch swap is supported (verified live) and is the only path that
actually retypes the production table. Aliases stay put across the swap
(they expose the OTHER table's data after -- see `gotchas.md`
"swap-tables aliases stay put").

Since v0.28.0 (`storage swap-tables` + `config update` script[]
auto-normalize); `storage clone-table` (v0.52.0) materializes a prod table
into a branch when the rehearsal needs the original branch-local on
storage-branches projects.

## Phase 0 -- Decide if you should do this

Skip the workflow when:

- The table is **<100 rows** and recreated daily by an extractor.
  Cheaper to update the extractor's destination definition + delete +
  re-extract than to swap. Fix the source.
- The table is an **alias** (`isAlias: true` in `storage table-detail`).
  Swap operates on physical positions; aliases keep pointing at the
  same physical position, so swapping the source through them is
  surprising. Touch the underlying physical table instead.
- The user wants to **add a new column with a type** to an existing
  typeless table -- that's `storage create-table` retype-on-add, not
  this workflow.

Apply the workflow when:

- The table has **historical data you cannot re-extract** (manual upload,
  derived dataset, deleted source).
- Many configs reference the table by name and would break if you
  renamed it.
- You want zero-downtime: the typeless table keeps serving until the
  swap, and the swap is sub-15-second on Snowflake.

## Phase 1 -- Isolate in a dev branch

```bash
# Create + auto-activate a dev branch dedicated to this rebuild
kbagent --json branch create \
  --project ALIAS \
  --name "typify-<table-slug>"
# -> branch_id, branch_name; activated=true

# Verify the active branch is set on the project
kbagent --json project status --project ALIAS
# -> branch field shows the new branch_id
```

Why a dev branch (this is a **rehearsal**, not the thing that ships):

- You use the branch to prove the typed schema is downstream-safe. The
  production retype (Phase 8) repeats the build + swap in the default
  branch -- merge does NOT carry the swapped schema to prod, only configs.
- Production transformations and writers keep targeting the typeless
  original; the rehearsal is invisible to them.
- All the writes below (`storage create-table`, `workspace query`
  CTAS, `swap-tables`) are scoped to the branch by `branch use`'s active-branch
  resolution.
- If anything goes wrong, `kbagent branch delete --branch <ID>` rolls
  back the entire experiment in one call.

The Storage API enforces this for swap-tables specifically: without an
active branch (or `--branch <ID>`), `swap-tables` exits 5 with `ConfigError`
before any HTTP call. The branch is **mandatory** for this workflow, not
optional.

## Phase 2 -- Profile the typeless table in a workspace

```bash
# 2a. Create an ad-hoc workspace in the active branch
kbagent --json workspace create --project ALIAS --name "typify-<table-slug>"
# -> workspace_id, host, database, schema, user, password
# SAVE THE PASSWORD -- it cannot be retrieved later.

# 2b. Load the typeless table
kbagent --json workspace load \
  --project ALIAS \
  --workspace-id W_ID \
  --tables in.c-foo.data
# Loaded as "in.c-foo.data" inside the workspace schema.

# 2c. Profile each column. Run ONE query per column-set you want to
#     fingerprint -- numeric, datelike, varchar bound, etc.

# Length + cardinality for STRING -> VARCHAR(N) decision
kbagent --json workspace query --project ALIAS --workspace-id W_ID --sql '
  SELECT
    MIN(LENGTH(id)) AS id_min, MAX(LENGTH(id)) AS id_max, COUNT(DISTINCT id) AS id_distinct,
    MIN(LENGTH(name)) AS name_min, MAX(LENGTH(name)) AS name_max, COUNT(DISTINCT name) AS name_distinct,
    COUNT(*) AS total_rows
  FROM "in.c-foo.data";
'

# Datelike check -- TRY_TO_TIMESTAMP returns NULL on parse failure, so any
# row that fails parse is a row that is NOT a valid timestamp.
kbagent --json workspace query --project ALIAS --workspace-id W_ID --sql '
  SELECT
    SUM(CASE WHEN TRY_TO_TIMESTAMP(created_at) IS NULL AND created_at IS NOT NULL THEN 1 ELSE 0 END) AS bad_ts,
    SUM(CASE WHEN TRY_TO_DATE(birthday) IS NULL AND birthday IS NOT NULL THEN 1 ELSE 0 END) AS bad_date
  FROM "in.c-foo.data";
'

# Numeric check -- scale + precision discovery
kbagent --json workspace query --project ALIAS --workspace-id W_ID --sql '
  SELECT
    SUM(CASE WHEN TRY_TO_NUMBER(amount) IS NULL AND amount IS NOT NULL THEN 1 ELSE 0 END) AS bad_amt,
    MAX(LENGTH(SPLIT_PART(amount, ''.'', 2))) AS max_scale,
    MAX(LENGTH(REPLACE(amount, ''.'', ''''))) AS max_precision
  FROM "in.c-foo.data";
'

# Boolean / enum check
kbagent --json workspace query --project ALIAS --workspace-id W_ID --sql '
  SELECT is_paid, COUNT(*) FROM "in.c-foo.data" GROUP BY is_paid ORDER BY 2 DESC;
'
```

Decision rules from the profile output (Snowflake):

| Profile signal | Recommended Snowflake type |
|---|---|
| `min/max LENGTH <= 40`, `COUNT(DISTINCT) > 10000` (looks like a key) | `VARCHAR(40)` |
| `min/max LENGTH <= 256`, free text | `VARCHAR(256)` (round up to nearest power of 2) |
| `MAX LENGTH > 1024` | `STRING` (i.e., `VARCHAR(16777216)`) -- accept the full length |
| `bad_ts == 0` and `created_at IS NOT NULL` for most rows | `TIMESTAMP_NTZ` (or `TIMESTAMP_TZ` if the source carries an offset) |
| `bad_date == 0` | `DATE` |
| `bad_amt == 0`, `max_scale > 0` | `NUMBER(p, s)` with `p = max_precision + 2`, `s = max_scale` |
| `bad_amt == 0`, `max_scale == 0` | `INTEGER` |
| 2 distinct values: `('true','false')` / `('Y','N')` / `('1','0')` | `BOOLEAN` (and rewrite values in CTAS) |

Round numeric scale **up** to leave headroom for new rows the
extractor might bring later (e.g., `NUMBER(18,2)` for amounts, not
`NUMBER(7,2)` -- the latter overflows the moment a $99,999.99
transaction lands).

For VARCHAR length, **never set it tighter than the observed max** unless
the user explicitly accepts the truncation risk. Better wider than
re-rebuilding because a single 41-char string broke the load.

## Phase 3 -- Build the typed sibling

```bash
# 3a. Create the typed sibling using kbagent. The native types map
#     1:1 to the API; see storage-types-workflow.md for the inventory.
kbagent --json storage create-table \
  --project ALIAS \
  --bucket-id in.c-foo \
  --name data_typed \
  --column "id:VARCHAR(40)" \
  --column "name:VARCHAR(256)" \
  --column "amount:NUMBER(18,2)" \
  --column "created_at:TIMESTAMP_NTZ" \
  --column "is_paid:BOOLEAN" \
  --primary-key id \
  --not-null id \
  --not-null amount \
  --default amount=0
# auto_created_bucket: false (bucket already exists in branch from Phase 2)
# legacy_branch_storage: false (assuming the project has the storage-branches feature)
```

Why a sibling name, not a temp suffix:

- After the swap, the **sibling holds the old typeless data**. The user
  may want to keep it as a rollback artifact for one or two release
  cycles (`data_change_log`, `data_pre_typify`, etc.). Choose a name
  that documents this -- not `_tmp` or `_v2`.
- The sibling lives in the **same bucket** as the original; both
  participate in the swap.

```bash
# 3b. Copy data from typeless original into the typed sibling. Two
#     options -- pick based on data volume.

# Option A: in-workspace CTAS-style INSERT (good for <100M rows; Snowflake
# only). Fast (single-statement); rollback is "drop and re-INSERT".
kbagent --json workspace query --project ALIAS --workspace-id W_ID --sql '
  INSERT INTO "in.c-foo.data_typed" (id, name, amount, created_at, is_paid)
  SELECT
    id::VARCHAR(40),
    name::VARCHAR(256),
    TRY_TO_NUMBER(amount, 18, 2),
    TRY_TO_TIMESTAMP(created_at),
    CASE WHEN is_paid IN (''true'', ''Y'', ''1'') THEN TRUE ELSE FALSE END
  FROM "in.c-foo.data";
'

# Option B: SQL transformation that runs as a Keboola job (good for
# repeatable / production pattern; works on BigQuery too). Authoring
# cost is higher but gets job logs + audit trail.
#   1. kbagent config new --component-id keboola.snowflake-transformation \
#        --name "typify data" --project ALIAS
#   2. Edit the config to set input mapping = data, output mapping = data_typed
#   3. kbagent config update ... with the SELECT body above
#   4. kbagent job run --component-id ... --config-id ... --branch <ID> --wait
```

Verify the copy:

```bash
# Sanity-check row counts match
kbagent --json workspace query --project ALIAS --workspace-id W_ID --sql '
  SELECT
    (SELECT COUNT(*) FROM "in.c-foo.data") AS old_count,
    (SELECT COUNT(*) FROM "in.c-foo.data_typed") AS new_count;
'
# old_count == new_count -- otherwise the CASTs dropped rows. Investigate.

# Check NULL columns in the typed copy that were not NULL originally
kbagent --json workspace query --project ALIAS --workspace-id W_ID --sql '
  SELECT
    SUM(CASE WHEN amount IS NULL THEN 1 ELSE 0 END) AS amt_nulls,
    SUM(CASE WHEN created_at IS NULL THEN 1 ELSE 0 END) AS ts_nulls
  FROM "in.c-foo.data_typed";
'
# Compare with the same in `data`. If `_typed` has more nulls, the
# CASTs silently failed -- usually a profile mistake (TIMESTAMP_NTZ vs
# TIMESTAMP_TZ, or NUMBER scale too small). Loop back to Phase 2.
```

## Phase 4 -- Validate downstream consumers in the dev branch

The point of the dev branch is to run real configs against the typed
table BEFORE you swap. This catches consumer code that breaks on type
changes -- e.g., a transformation that did `column::STRING` casts and
no longer needs them, or a writer that forces VARCHAR(20) and now
overflows.

```bash
# 4a. List configs that use the original table. Either as input or as
#     a downstream destination. (lineage build is the heavy version of
#     this; for one table, a search is enough.)
kbagent --json config search --project ALIAS \
  --query "in.c-foo.data" --ignore-case
# Returns each config that references the table_id literally.

# 4b. Pick the most representative SQL transformation downstream. Run
#     it in the dev branch. Because the dev branch's "data" is still
#     the typeless original (we have not swapped yet), this run
#     verifies the BASELINE works -- catches branch-isolation bugs
#     before they get blamed on the typify.
kbagent --json job run \
  --project ALIAS \
  --component-id keboola.snowflake-transformation \
  --config-id <DOWNSTREAM_CONFIG> \
  --branch <BRANCH_ID> \
  --wait

# 4c. Now perform the swap (Phase 5), then re-run the same downstream
#     transformation. If it succeeds with identical row counts, you have
#     end-to-end coverage that the typed schema is consumer-compatible.
```

For BigQuery dialect callers, also validate `bigquery_path` consumers
(see `storage-describe-workflow.md`'s `bucket-detail` section -- BQ
emits backtick-quoted `\`dataset\`.\`table\`` paths since v0.25.3).

## Phase 5 -- Swap (in the rehearsal branch)

This swap happens in the dev branch to prove the typed schema works; the
production swap is repeated in Phase 8.

```bash
# 5.0. storage-branches projects ONLY: the swap is a write, and the dev
#      branch still reads the original 'data' transparently from prod, so
#      the swap fails with a misleading "bucket not found" until 'data' is
#      materialized branch-local. Pull it in first. ('data_typed', built
#      in Phase 3, is already branch-local.) Skip on legacy-branch projects
#      -- check with: kbagent project info --project ALIAS | grep storage-branches
kbagent --json storage clone-table \
  --project ALIAS \
  --table-id in.c-foo.data \
  --branch <BRANCH_ID>

# 5a. Dry-run first. Should report dry_run: true, never call the API.
kbagent --json storage swap-tables \
  --project ALIAS \
  --table-id in.c-foo.data \
  --target-table-id in.c-foo.data_typed \
  --branch <BRANCH_ID> \
  --dry-run

# 5b. Actual swap. Storage API queues an async storage job
#     (operationName: tableSwap); the kbagent client polls to
#     completion before returning. ~10s on Snowflake.
kbagent --json storage swap-tables \
  --project ALIAS \
  --table-id in.c-foo.data \
  --target-table-id in.c-foo.data_typed \
  --branch <BRANCH_ID> \
  --yes
# response.status == "success" on completion.

# 5c. Verify column types swapped.
kbagent --json storage table-detail \
  --project ALIAS \
  --table-id in.c-foo.data \
  --branch <BRANCH_ID>
# Look at .data.column_details[].native_type / .length -- they should
# match the typed sibling's schema, not the typeless original.
```

After the swap:

- `in.c-foo.data` -- now has the **typed** schema (was on `data_typed`).
- `in.c-foo.data_typed` -- now holds the **typeless** rows (was on `data`).
- Aliases pointing at either table keep pointing at the same physical
  position, so they expose the OTHER table's data. If any downstream
  config refers to an alias, run a manual sanity check on it before
  applying the retype in production (Phase 8).

## Phase 6 -- Smoke-test downstream

Re-run the same downstream transformation from Phase 4 against the
swapped table. The branch is still active.

```bash
kbagent --json job run \
  --project ALIAS \
  --component-id keboola.snowflake-transformation \
  --config-id <DOWNSTREAM_CONFIG> \
  --branch <BRANCH_ID> \
  --wait

# Verify the result
kbagent --json job detail \
  --project ALIAS \
  --job-id <JOB_ID>
# status == "success"; rowsCount matches Phase 4's run.
```

Run two or three downstream configs of different shapes (transformation
with input mapping, writer with output mapping, possibly a flow that
chains them). Any that fail at runtime indicate the new schema is too
narrow / too strict / a CAST you missed in Phase 3.

If a transformation now does `WHERE col = '0'` on what is now an
INTEGER column, it will compare wrong values silently -- the test should
verify row counts, not just job exit status. Ideally diff
`data_typed` (= the old typeless rows) against the swapped `data`
on a key column to confirm row-level identity.

## Phase 7 -- Tear down the rehearsal branch

Once Phases 4-6 prove the typed schema is consumer-safe, the dev branch
has done its job. **Nothing in it ships** -- merge will not carry the
swapped schema to production (only configs merge). Delete the branch
(this also drops the branch-local `data_typed` sibling):

```bash
kbagent branch delete --project ALIAS --branch <BRANCH_ID> --yes
```

Keep a written record of what the rehearsal proved -- the Phase 2 profile
summary and the Phase 4/6 downstream job results -- because Phase 8
repeats the build in production with the same type decisions.

## Phase 8 -- Apply the retype in production

Because dev-branch merge does not carry storage schema (see `gotchas.md`
"Dev-branch merge carries only configurations"), the real retype runs in
the **production (default) branch**, repeating the validated build:

```bash
# 8a. Resolve the default (production) branch ID.
kbagent --json branch list --project ALIAS
#     -> the entry with isDefault=true; call it <PROD_BRANCH_ID>.

# 8b. Build the typed sibling in PRODUCTION using the exact types the
#     rehearsal validated (Phase 2/3): same create-table + data copy as
#     Phase 3, but targeting the default branch.
kbagent --json storage create-table --project ALIAS \
  --bucket-id in.c-foo --name data_typed \
  --column id:VARCHAR(40) --column amount:"NUMBER(18,2)" --branch <PROD_BRANCH_ID>
#     ...then copy rows in (in-workspace INSERT or an SQL transformation,
#     exactly as in Phase 3 Option A / B).

# 8c. Swap in production. A default-branch swap is supported.
kbagent --json storage swap-tables --project ALIAS \
  --table-id in.c-foo.data \
  --target-table-id in.c-foo.data_typed \
  --branch <PROD_BRANCH_ID> --yes
#     -> in.c-foo.data now carries the typed schema in production.

# 8d. Smoke-test a downstream config in production, then clean up.
kbagent storage delete-table --project ALIAS --table-id in.c-foo.data_typed --yes
```

Two production-only cautions the rehearsal does not surface:

- **Inconsistency window.** Between 8b (copy) and 8c (swap), upstream
  writers may append rows to the live `data`. Either quiesce the upstream
  load for the swap window, or run a final incremental catch-up INSERT
  right before the swap. The swap itself is atomic and sub-15s on Snowflake.
- **Rollback.** `data_typed` (now holding the old typeless rows) is the
  rollback artifact -- re-swap to undo -- until you delete it in 8d.

Hand the user a structured summary before running 8c. Recommended shape:

```text
TYPIFY READY TO APPLY IN PRODUCTION -- in.c-foo.data (project ALIAS)

Rehearsal branch <BRANCH_ID> proved the schema is downstream-safe:
  rows: 1,234,567
  id:   STRING -> VARCHAR(40)   (max observed length: 36)
  amount: STRING -> NUMBER(18,2) (max precision: 14, max scale: 2)
  created_at: STRING -> TIMESTAMP_NTZ (0 parse failures across 1.2M rows)
  is_paid: STRING -> BOOLEAN (values: 'true' (840k), 'false' (390k))
  downstream config <DOWNSTREAM_CONFIG_ID>: green pre- and post-swap in the
    branch, rows_out unchanged.

Production plan (default branch <PROD_BRANCH_ID>):
  1. create in.c-foo.data_typed with the types above
  2. copy rows (quiesce writers or do a final catch-up INSERT first)
  3. swap-tables in.c-foo.data <-> in.c-foo.data_typed
  4. smoke-test <DOWNSTREAM_CONFIG_ID>, then delete in.c-foo.data_typed

Rollback (pre-cleanup): re-run swap-tables to put the typeless table back.
```

The rehearsal branch is already gone (Phase 7); there is no merge step.

## Failure modes to anticipate

- **Phase 2 profile under-reports max length.** A row added between
  Phase 2 and Phase 3 with a longer string fails Phase 3's INSERT.
  Mitigation: add `+10%` headroom to VARCHAR(N) bounds; ALWAYS round up
  to a power of 2.
- **Phase 3 silent CAST nulls.** `TRY_TO_NUMBER` returns NULL on parse
  failure; if `not_null=true` is set on the column, the INSERT raises
  at the offending row. If `not_null=false`, NULLs accumulate silently.
  The Phase 3 verification block (NULL counts before/after) is the
  guard.
- **Phase 4 baseline that "worked in main" but fails in the dev
  branch.** Usually a config references a bucket that was not
  auto-materialized in the branch yet (see
  `storage-types-workflow.md` "Dev-branch auto-materialize"). Run
  `kbagent storage tables --branch <ID>` to verify all input tables
  are visible.
- **Swap on `legacy_branch_storage` projects** (no `storage-branches`
  feature flag). `swap-tables` will run, but the runner uses
  `out.c-<branch_id>-*` rewriting and may not see the swap when jobs
  execute. See `storage-types-workflow.md` "Fake-branch vs storage-branches"
  for the full implications. On legacy-branch projects, do the work in
  `main` -- the dev-branch isolation guarantee does not hold.
- **Aliases.** If the user has aliased the original table into another
  project, the alias keeps pointing at the same physical position. After
  the swap, alias consumers see the OTHER table's data. Either rewrite
  the aliases as part of this workflow (`storage delete-table` the
  alias + `sharing link` against the new physical) or document this in
  the handoff so the user is not surprised three days later.

## Cross-references

- `storage-types-workflow.md` -- column type inventory, native type
  whitelist, dev-branch auto-materialize semantics, "Promoting a typed
  rebuild back into the original name" (Phase 5 mechanics).
- `branch-workflow.md` -- branch lifecycle, active-branch persistence,
  merge-via-UI policy.
- `workspace-workflow.md` -- ad-hoc workspace creation + `workspace load`
  + `workspace query` patterns. The `from-transformation` mode is the
  alternative to ad-hoc workspaces when you want to debug an existing
  config that is failing.
- `gotchas.md` -- "swap-tables aliases stay put (since v0.28.0)";
  "config update auto-normalizes script[] (since v0.28.0)" if the
  Phase 3 Option B SQL transformation route is taken.
