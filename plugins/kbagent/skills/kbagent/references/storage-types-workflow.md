# Storage column types in `create-table`

`kbagent storage create-table` accepts three flavours of `--column` spec and
two attribute flags. This reference covers the full surface, the dev-branch
auto-materialize behaviour, and the common pitfalls.

## Syntax

```
--column name                      # bare name -> STRING (backend default length)
--column name:TYPE                 # e.g. id:INTEGER, name:STRING
--column name:TYPE(length)         # e.g. amount:NUMERIC(18,2), pk:VARCHAR(40)
--not-null COLUMN                  # repeatable; marks a defined column NOT NULL
--default NAME=VALUE               # repeatable; sets a DEFAULT expression
```

`TYPE` is uppercased and passed through to the Storage API unmodified. The
API validates type/length pairs per backend and returns precise errors --
e.g. `INTEGER(10)` fails with `"'10' is not valid length for INTEGER"`.
This means:

- Any native backend type is accepted at the CLI level -- there is no
  whitelist to maintain.
- The CLI does only syntactic validation (valid identifier, length is
  digits + commas).
- Semantic errors come from Keboola with actionable messages.

## Type inventory (Snowflake)

Base Keboola types accepted everywhere:

| CLI | Snowflake result |
|---|---|
| `STRING` | `VARCHAR(16777216)` (max size) |
| `INTEGER` | `NUMBER(38,0)` |
| `NUMERIC` | `NUMBER(38,9)` |
| `FLOAT` | `FLOAT` |
| `BOOLEAN` | `BOOLEAN` |
| `DATE` | `DATE` |
| `TIMESTAMP` | `TIMESTAMP_NTZ` |

Native types you can use when base defaults are too wide or too coarse:

| CLI | Stored as | Note |
|---|---|---|
| `VARCHAR(n)` | `VARCHAR(n)` | exact width |
| `CHAR(n)` | `VARCHAR(n)` | alias |
| `TEXT` | `VARCHAR(16777216)` | alias for STRING |
| `NUMBER(p,s)` | `NUMBER(p,s)` | precision + scale |
| `DECIMAL(p,s)` | `NUMBER(p,s)` | alias |
| `INT` / `BIGINT` | `NUMBER(38,0)` | Snowflake aliases for INTEGER |
| `DOUBLE` | `FLOAT` | alias |
| `TIMESTAMP_NTZ` | `TIMESTAMP_NTZ(9)` | no-timezone |
| `TIMESTAMP_LTZ` | `TIMESTAMP_LTZ(9)` | session-local |
| `TIMESTAMP_TZ` | `TIMESTAMP_TZ(9)` | explicit timezone |
| `TIME` | `TIME` | time-of-day only |
| `VARIANT` | `VARIANT` | JSON-ish |
| `OBJECT` | `OBJECT` | struct-like |
| `ARRAY` | `ARRAY` | arrays |

BigQuery, Redshift, Synapse: their native types pass through too. The CLI
does not validate them; the Storage API does.

## Attribute flags

| Flag | Maps to `definition` field | Notes |
|---|---|---|
| `--not-null COL` | `nullable: false` | Must reference a column defined by a `--column`; unknown names fail fast (exit 2) |
| `--default NAME=VALUE` | `default: "VALUE"` | Booleans must be lowercase (`true`/`false`) -- `FALSE` is rejected by API. Empty `VALUE` (e.g. `--default foo=`) is accepted and produces an empty-string default |

## Dev-branch auto-materialize

Keboola dev branches have an isolated storage namespace: a production bucket
is readable from a branch (transparent fallback) but a branch-scoped **write**
against an unmaterialized bucket returns `Bucket not found`.

`kbagent storage create-table --branch <ID>` handles this automatically:

1. Check bucket existence in the branch (`GET /v2/storage/branch/{id}/buckets/{bucket_id}`).
2. On 404, create the bucket in the branch with the same stage+name
   (mirrors the official Go CLI's `EnsureBucketExists`).
3. Stamp `KBC.createdBy.branch.id = <branch_id>` system metadata on the
   freshly-created bucket (see "Branched-storage metadata stamp" below).
4. Then proceed with the table creation.

The response surfaces this via `auto_created_bucket: true`. Production
writes (no `--branch`) never materialize anything.

### Branched-storage metadata stamp

On projects with the **branched storage** feature flag enabled, the
transformation runner's `output-mapping` library
(`Storage/BucketCreator::checkDevBucketMetadata`) refuses to write into a
dev-branch bucket that does not carry the `KBC.createdBy.branch.id` system
metadata equal to the current branch ID. The error surfaces as:

```
Trying to create a table in the development bucket "X" on branch "Y"
(ID "Z"), but the bucket is not assigned to any development branch.
```

Storage API does **not** auto-populate that key on
`POST /v2/storage/branch/<id>/buckets`, so kbagent stamps it explicitly
right after creation (provider=`system` -- `user` is rejected on the
reserved `KBC.*` namespace). Failure of the metadata write is logged but
**non-fatal**: the table-create call still proceeds. If a user lacks
bucket-metadata permission, the runner will surface the original
"not assigned" error later, which is no worse than today.

The same bug exists in the Go CLI's `EnsureBucketExists` -- tracked in
[`keboola/connection`](https://github.com/keboola/connection) as a
backend-side fix request, but kbagent users hit it first and need a
client-side workaround.

Closes #224.

### Fake-branch vs `storage-branches`: when `--branch X` is a no-op for the runner

Keboola Storage has **two parallel branch-isolation models**:

| Model | Triggered by | Bucket isolation | Runner behavior |
|---|---|---|---|
| `storage-branches` (modern) | Project owner has the `storage-branches` feature flag | `POST /v2/storage/branch/<id>/buckets` produces a **branch-scoped bucket**; bucket carries `KBC.createdBy.branch.id` metadata; visible only via `--branch <id>` view | Reads/writes the branch-scoped bucket directly. The metadata stamp from §"Branched-storage metadata stamp" is **required** -- without it the runner aborts with "bucket is not assigned to any development branch". |
| Legacy fake-branch | Project owner does **not** have `storage-branches` | The same `POST /branch/<id>/buckets` call also succeeds, but at job time the runner **rewrites bucket IDs**: a transformation that targets `out.c-foo.tbl` writes to `out.c-<branch_id>-foo.tbl` in the **default branch** instead. | Creates its own `out.c-<branch_id>-*` bucket with literal branch ID in the bucket name. The kbagent-materialized bucket is never read or written by the runner. |

**What kbagent does on a write call with `--branch X`** (`storage create-bucket
--branch X` and `storage create-table --branch X`):

1. Runs the existing auto-materialize + metadata-stamp logic.
2. Calls `verify_token()` once per session (cache lives on the
   `KeboolaClient` instance) and inspects `owner.features` for
   `"storage-branches"`.
3. If absent, surfaces `legacy_branch_storage: true` in the JSON response
   and prints a Rich `[yellow]Warning:[/yellow]` line in human mode
   explaining that the runner will create a parallel bucket. Behavior of
   the actual API call is unchanged -- the warning is purely informational.
4. If present, the field is `false` and no warning is printed.

**How an AI agent should react to `legacy_branch_storage: true`:**

- Do **not** plan downstream "look in `out.c-foo` for the result" steps
  after a transformation runs. The result lives in
  `out.c-<branch_id>-foo` in the default branch. Use that ID for
  follow-up queries / table-detail / unload.
- The kbagent-materialized bucket (`out.c-foo` in `--branch X` view) is
  reachable from the branch view and from direct Snowflake queries, but
  is otherwise an orphan. Garbage-collect it manually if the user does
  not need it.
- The right long-term fix is upstream Storage migrating the project to
  `storage-branches`. kbagent does not implement automatic bucket-name
  rewrites because magic ID rewrites are confusing in cleanup commands.

**Reproducing the difference**: project 10539 (`padak-2-0`) is the canonical
fake-branch test target; project 10546 (`kbagent-e2e`) is `storage-branches`
ON. Direct comparison:

```bash
# verify the feature flag delta
kbagent --json project status --project kbagent-e2e   # has storage-branches
kbagent --json project status --project padak-2-0     # does not

# fake-branch project: warning fires
kbagent storage create-bucket --project padak-2-0 --branch <ID> \
    --stage out --name probe
#   → "Warning: this project uses legacy fake-branch storage..."

# storage-branches project: no warning
kbagent storage create-bucket --project kbagent-e2e --branch <ID> \
    --stage out --name probe
#   → no warning line
```

## Examples

Basic typed table (backward-compatible, unchanged):

```bash
kbagent --json storage create-table \
  --project prod --bucket-id in.c-sales --name orders \
  --column id:INTEGER --column customer_id:INTEGER --column amount:NUMERIC \
  --primary-key id
```

Tighter Snowflake types after profiling an existing table:

```bash
kbagent --json storage create-table \
  --project prod --bucket-id in.c-slack --name messages \
  --column pkey:VARCHAR\(40\) \
  --column channel_id:VARCHAR\(20\) \
  --column tz_offset:NUMBER\(6,0\) \
  --column num_members:NUMBER\(3,0\) \
  --column ts:TIMESTAMP_TZ \
  --column ch_name:VARCHAR\(80\) \
  --column is_admin:BOOLEAN \
  --primary-key pkey \
  --not-null pkey --not-null ts \
  --default num_members=0 --default is_admin=false
```

(Escape the parentheses in bash with `\(...\)`, or wrap the whole spec in
single quotes: `--column 'pkey:VARCHAR(40)'`.)

Dev branch with implicit bucket materialization:

```bash
# Production bucket exists but the branch is fresh -- kbagent creates
# the branch-scoped bucket before creating the table.
kbagent --json storage create-table \
  --project prod --branch 1234567 \
  --bucket-id in.c-archive --name snapshot \
  --column id:INTEGER --column payload:VARIANT

# Response includes "auto_created_bucket": true.
```

## Gotchas

- `BOOLEAN` default must be lowercase: `--default flag=false` (uppercase `FALSE`
  is rejected with `storage.tables.definitionValidation` -- the API message
  is clear but easy to miss).
- `INTEGER(10)` is invalid: Keboola's `INTEGER` base type ignores length.
  If you want a narrow integer, use `--column age:NUMBER(3,0)` instead.
- `--not-null` and `--default` names must match a `--column` name exactly
  (case-sensitive). Typos exit 2 (`INVALID_ARGUMENT`) before any API call.
- `auto_created_bucket: true` is informational, not an error. Check
  the field in JSON mode; in human mode it is shown as a yellow note under
  the created-table banner.

## Promoting a typed rebuild back into the original name

Common pattern: an existing typeless table needs proper column types. AI
agent profiles the data in a workspace, builds a typed copy via CTAS, and
needs to flip the typed copy back into the original name so downstream
configs (extractors, transformations, writers) keep working unchanged.

```bash
# 1. Rehearse in a dev branch (validate the typed schema against downstream
#    configs). The REAL retype is then repeated in the default/production
#    branch -- dev-branch merge does NOT carry storage schema. For the full
#    rehearsal-then-production procedure, see typify-table-workflow.md.
#    <ID> below is the rehearsal branch; for the production run pass the
#    default-branch ID instead.
kbagent branch create --project prod --name typify-data
kbagent branch use --project prod --branch <ID>

# 2. In a workspace, build a typed CTAS into a sibling table
kbagent workspace create --project prod
# Default auto-picks clone (fast, any size) when eligible, else copy --
# on a large table that falls back to copy, --json refuses without --force.
# See workspace-workflow.md "Load types" for the full clone/copy/view rules.
kbagent workspace load --workspace-id W --tables in.c-foo.data
kbagent workspace query --workspace-id W --sql "
  CREATE TABLE \"in.c-foo.data_change_log\" AS
  SELECT id::VARCHAR(40) AS id, amount::NUMBER(18,2) AS amount
  FROM \"in.c-foo.data\"
"
# (or use kbagent storage create-table + an SQL transformation)

# 2b. storage-branches projects only: the dev branch reads 'data'
#     transparently until first write, so swap (a write) fails with a
#     misleading "bucket not found" until 'data' is materialized
#     branch-local. Pull it in first. (data_change_log, built by the
#     in-branch CTAS above, is already branch-local. Skip on
#     legacy-branch projects.)
kbagent storage clone-table \
  --project prod \
  --table-id in.c-foo.data \
  --branch <ID>

# 3. Swap: the typed copy becomes 'data', the typeless original moves
#    to 'data_change_log'. Aliases stay put -- they expose the OTHER
#    table's data after the swap.
kbagent storage swap-tables \
  --project prod \
  --table-id in.c-foo.data \
  --target-table-id in.c-foo.data_change_log \
  --branch <ID> --yes

# 4. There is NO merge step: dev-branch merge carries only configs, not
#    storage schema. Once the rehearsal proves the schema is safe, delete
#    the branch and repeat steps 2-3 in the default/production branch
#    (pass the default-branch ID to --branch on swap-tables).
kbagent branch delete --project prod --branch <ID> --yes
```

Rules:
- **storage-branches projects:** `swap-tables` operates on branch-local
  tables. The original (`in.c-foo.data`) is read transparently from prod
  until first write, so the swap fails with a misleading "bucket not
  found" until you `clone-table` it into the branch (step 2b). The typed
  sibling built by the in-branch CTAS is already branch-local. Legacy
  fake-branch projects don't need this.
- branch_id is mandatory: the service refuses with exit 5 (`ConfigError`)
  before any HTTP if `--branch` is missing AND no active branch is set via
  `branch use`. Any branch works, INCLUDING the default/production branch
  -- a default-branch swap is how the retype reaches prod (the earlier
  "rejected on production" claim was wrong).
- Aliases keep pointing at the same physical position, i.e. they expose
  the OTHER table's data after the swap. If your downstream relies on
  alias-by-name, validate post-swap before applying in production.
- The Storage API queues the swap as an async storage job
  (`operationName: tableSwap`); the kbagent client polls the job to
  completion before returning, so callers can rely on the schemas being
  exchanged on return. Real swaps observed at ~10s on Snowflake.
- The swap is symmetric; there is no rollback besides swapping again
  (or aborting the dev branch).

## BigQuery repartition via `create-table --source-table-id`

On **BigQuery** you can produce the repartitioned/re-clustered copy in a
single call instead of hand-writing a CTAS transformation: `create-table`
copies an existing table's data into a new partition/clustering layout,
then `swap-tables` flips it into place.

```bash
# 1. Copy in.c-main.events into a new DAY-partitioned, tenant-clustered table.
#    Schema (columns, NOT NULL) is derived from the source -> NO --column.
kbagent storage create-table --project prod --bucket-id in.c-main \
  --name events_repart --source-table-id in.c-main.events \
  --time-partitioning-type DAY --time-partitioning-field created_at \
  --clustering-field tenant_id --primary-key id

# 2. Inspect the copy, then swap it into the original's place.
kbagent storage table-detail --project prod --table-id in.c-main.events_repart
kbagent storage swap-tables --project prod --table-id in.c-main.events \
  --target-table-id in.c-main.events_repart --branch <DEFAULT_BRANCH_ID> --yes

# 3. Verify the layout actually landed on the production name (0.88.0+, #621).
#    The table ID is identical whether or not the swap happened -- the layout is
#    the only thing that tells the two apart.
kbagent --json storage table-detail --project prod --table-id in.c-main.events \
  | jq '.data.definition | {timePartitioning, clustering, requirePartitionFilter}'
```

### Budget both steps on a large table

*(since vNEXT, #713)*

Steps 1 and 2 both move real data. Until vNEXT they inherited the 60s
`STORAGE_JOB_MAX_WAIT` meant for metadata jobs -- measured on an 800 MB table,
create takes ~15s and the swap ~31s, so a table a few times larger reported
`STORAGE_JOB_TIMEOUT` on a job that was still running and would succeed. Both
now default to **300s** and accept `--timeout SECONDS`:

```bash
kbagent storage create-table --project prod --bucket-id in.c-main \
  --name events_repart --source-table-id in.c-main.events \
  --time-partitioning-type DAY --time-partitioning-field created_at \
  --timeout 3600
```

**A timeout is not a failure.** kbagent stops watching; the Storage job keeps
running server-side. The error names the job id -- poll
`GET /v2/storage/jobs/{id}`, or just run step 3. Never blindly re-issue the
swap: repeating one that already landed swaps the tables straight back.

On 0.87.0 and earlier `table-detail` dropped `definition`, so step 3 was not
possible from kbagent at all. `create-table`'s own output is not a substitute:
it echoes the layout you *requested*, and its `--if-not-exists` skip path
reports `null` for all three layout keys rather than re-deriving the existing
table's. Note `.definition.partitions` is one entry per physical partition
(thousands on a long-lived daily table) -- select the keys you need, as above,
rather than dumping the whole object.

Rules:
- **BigQuery only.** `--source-table-id` and the partition/clustering flags
  (`--time-partitioning-*`, `--range-partitioning-*`, `--clustering-field`)
  are rejected on a non-BigQuery project by a one-call backend pre-flight
  (exit 2) before the create is issued. The connection API also rejects
  them server-side (422) as a backstop.
- **`--column` XOR `--source-table-id`.** The two are mutually exclusive;
  the schema in source mode is derived from the source, so `--column` /
  `--not-null` / `--default` must not be supplied.
- **Partitioning shapes.** Time partitioning needs `--time-partitioning-type`
  (DAY/HOUR/MONTH/YEAR); range partitioning needs all of
  `--range-partitioning-field/-start/-end/-interval` (bounds are strings).
  Time and range partitioning are mutually exclusive.
- **Sources.** Regular tables, linked-bucket tables, and persisted aliases
  (materialized views) are valid copy sources. A non-persisted alias is
  rejected 422.
- The partition/clustering flags also work on a plain `--column` create
  (same BigQuery-only rule) when you want a fresh empty table in a specific
  layout rather than a copy.
