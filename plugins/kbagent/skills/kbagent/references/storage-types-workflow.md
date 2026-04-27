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

### Branched-storage metadata stamp (since 0.25.1)

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
- Client-mode `--hint` returns raw CLI strings in `columns=[...]`; adapt
  them to the API shape `[{"name": ..., "definition": {...}}]` before
  sending via `KeboolaClient.create_table()`. Service-mode `--hint` uses
  the service layer which does the parsing for you.
