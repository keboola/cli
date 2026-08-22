# Storage Describe Workflow

`kbagent storage describe-*` attaches human-readable descriptions to storage
buckets, tables, and columns so that downstream consumers (dashboards, the
Keboola UI, `kbagent storage buckets`/`tables`, AI agents) can surface
meaningful documentation rather than raw IDs. Bucket and table descriptions are
stored as metadata on the storage object; column descriptions are written to the
table's native definition *(since v0.88.0)*. All of them round-trip via
`storage bucket-detail` / `storage table-detail`.

## Quick reference

| Command | Purpose |
|---------|---------|
| `storage describe-bucket` | Set a bucket description |
| `storage describe-table` | Set a table description |
| `storage describe-column` | Set descriptions on one or more columns |
| `storage describe-batch` | Apply bucket/table/column descriptions from a YAML file |
| `storage describe-migrate` | Convert legacy pre-0.88.0 `KBC.column.*` metadata to the native endpoint |
| `storage bucket-detail` | Read back the bucket description |
| `storage table-detail` | Read back the table description and `column_details[].description` |

## When to use

- Onboarding a new project: document every source bucket, output table, and
  business-critical column so new engineers (or Kai) can self-serve.
- After a schema migration: refresh column descriptions so SQL reviews can
  spot intent mismatches.
- Before sharing a bucket cross-project: the description is visible in the
  receiving project's dashboard.
- From CI: write a batch YAML alongside the repo and call `describe-batch`
  after every `sync push` to keep documentation in lockstep with config.

## Storage model (what actually gets written)

Bucket and table descriptions are metadata entries on the object; column
descriptions are a native field on the table definition:

- **Bucket description** -- `KBC.description` (provider=user) on bucket metadata
- **Table description** -- `KBC.description` (provider=user) on table metadata
- **Column description** *(since v0.88.0)* -- written through
  `PUT /v2/storage/branch/{branch}/tables/{table_id}/definition`, the same
  endpoint the Keboola web UI uses. The call is asynchronous (a
  `tableDefinitionUpdate` storage job; kbagent polls it to completion) and always
  carries `isDescriptionSystemManaged: false`, which is what prevents the next
  component run's Output Mapping from overwriting a hand-authored description.
  The backend mirrors the value into `columnMetadata[{column}]`
  `KBC.description` for typed AND untyped tables, so a single write is visible
  to the UI, to the MCP server, and in the Snowflake `COMMENT` / BigQuery column
  description. Read them back via `storage table-detail`
  (`column_details[].description`).

Descriptions are `upsert`: calling `describe-*` with a new text replaces
whatever was there before. There is no append mode.

### Legacy convention (pre-0.88.0) and how to get rid of it

Before 0.88.0 kbagent stored each column description as a flat
`KBC.column.{column_name}.description` entry on the **table's** metadata. That
key was read by nothing except kbagent itself -- columns documented that way
appear blank in the Keboola UI, are invisible to the MCP server, and never reach
the warehouse. The mirroring is one-way, so a metadata write can never reach the
native field.

Existing entries are not lost. `storage table-detail` still reads them (last in
the precedence chain below) and reports them in `legacy_column_descriptions`;
`storage describe-migrate` converts them in bulk, and `describe-column` /
`describe-batch` convert whatever is left on the table they touch as part of the
same write. Migrated flat entries are **deleted** after a successful write --
otherwise clearing a column description later would be silently undone by the
read fallback resurrecting the old value.

### Read precedence (`storage table-detail`)

For each column, the description is resolved in this order:

1. the native `definition.columns[].definition.description` field
2. `columnMetadata[{column}]` entry with key `KBC.description` (for an alias
   table, the source table's `columnMetadata` is consulted when the alias itself
   has none -- parity with the MCP server)
3. the legacy flat `KBC.column.{column}.description` table-metadata entry

`table-detail` always returns `legacy_column_descriptions` (the columns still
backed by convention 3; an empty list when there are none) and prints a warning
in human mode when it is non-empty. Reading never writes, so it is safe with a
read-only token or under `--deny-writes`.

## Single-item: bucket

```bash
# Inline text
kbagent --json storage describe-bucket \
  --project ALIAS \
  --bucket-id in.c-sales \
  --text "Daily sales fact data, partitioned by region"

# From a file (markdown supported)
kbagent --json storage describe-bucket \
  --project ALIAS \
  --bucket-id in.c-sales \
  --file ./docs/sales-bucket.md

# From stdin (useful in pipelines)
echo "Generated description" | kbagent --json storage describe-bucket \
  --project ALIAS \
  --bucket-id in.c-sales \
  --stdin
```

Exactly one of `--text`, `--file`, `--stdin` must be provided.

Read back:

```bash
kbagent --json storage bucket-detail --project ALIAS --bucket-id in.c-sales \
  | jq '.data.description, .data.metadata'
```

## Single-item: table

Identical shape to `describe-bucket`:

```bash
kbagent --json storage describe-table \
  --project ALIAS \
  --table-id in.c-sales.orders \
  --text "All sales orders, one row per line item"
```

Read back:

```bash
kbagent --json storage table-detail --project ALIAS --table-id in.c-sales.orders \
  | jq '.data.description, .data.column_details'
```

## Single-item: columns

`describe-column` takes **one or more** `--column NAME=DESCRIPTION` flags in
a single call. All entries are applied in one API roundtrip:

```bash
kbagent --json storage describe-column \
  --project ALIAS \
  --table-id in.c-sales.orders \
  --column "order_id=Unique order identifier" \
  --column "total=Order total in USD (gross)" \
  --column "created_at=Server-side creation timestamp (UTC)"
```

All requested column names are validated against the table BEFORE anything is
written *(since v0.88.0)*: a name that is not on the table aborts the command
with a usage error naming it. Pre-0.88.0 a typo was accepted and produced a
metadata entry nothing could ever read, which looked like a success.

The write is one asynchronous storage job per call, so a `describe-column` with
several `--column` flags is still a single roundtrip. If the table still carries
legacy `KBC.column.*` entries for OTHER columns, they are folded into the same
write and their flat keys deleted afterwards (conflicting and orphaned entries
are reported as skipped instead -- see `describe-migrate` below).

Read back via `storage table-detail`:

```json
{
  "data": {
    "table_id": "in.c-sales.orders",
    "description": "All sales orders, one row per line item",
    "column_details": [
      {"name": "order_id", "type": "INTEGER", "description": "Unique order identifier"},
      {"name": "total", "type": "NUMERIC", "description": "Order total in USD (gross)"}
    ]
  }
}
```

Human mode shows the same text in a `Description` column of the Columns table
*(since v0.89.0)* -- it appears only when at least one column has a description,
so an undocumented table renders exactly as before. On 0.88.0 the human table
had no such column at all, so use `--json` to verify a write on that version.

Columns without a matching metadata entry simply omit `description`.

## Batch: YAML schema

For more than a handful of items, hand-maintain a YAML file and apply it
with `storage describe-batch`. The schema has three top-level sections,
all optional:

```yaml
# descriptions.yaml
buckets:
  in.c-sales: |
    Sales fact and dimension tables.
    Refreshed nightly from the production OLTP via Keboola ex-db-postgres.
  in.c-marketing: Marketing funnel events

tables:
  in.c-sales.orders: All sales orders (one row per line item)
  in.c-sales.customers: Customer master list, PII-scrubbed
  in.c-marketing.events: Raw funnel events

columns:
  in.c-sales.orders:
    order_id: Unique order identifier
    total: Order total in USD (gross)
    created_at: Server-side creation timestamp (UTC)
  in.c-sales.customers:
    customer_id: Primary key
    email_hash: SHA-256 of the customer email (PII-scrubbed)
```

Apply it:

```bash
kbagent --json storage describe-batch \
  --project ALIAS \
  --from-file ./descriptions.yaml
```

Response shape:

```json
{
  "status": "ok",
  "data": {
    "project_alias": "ALIAS",
    "applied": [
      {"type": "bucket", "id": "in.c-sales", "description": "Sales fact..."},
      {"type": "table",  "id": "in.c-sales.orders", "description": "All sales orders..."},
      {"type": "columns", "id": "in.c-sales.orders", "columns": {"order_id": "...", "total": "..."}}
    ],
    "errors": [],
    "applied_count": 3,
    "error_count": 0
  }
}
```

In human mode, a Rich progress spinner shows per-item progress ("Describing
bucket in.c-sales", "Describing table in.c-sales.orders", ...) so large
batches do not look frozen. The spinner is suppressed under `--json` so
structured output is the only thing on stdout.

## Partial-failure semantics

`describe-batch` does **not** abort on the first error. Each item is
attempted independently; failures are collected into `errors[]` and the
batch continues:

```json
{
  "data": {
    "applied": [{"type": "bucket", "id": "in.c-good", ...}],
    "errors": [
      {"type": "bucket", "id": "in.c-typo", "error": "Bucket in.c-typo not found"},
      {"type": "table",  "id": "in.c-x.missing", "error": "Table not found"}
    ],
    "applied_count": 1,
    "error_count": 2
  }
}
```

The CLI exits **1** when `error_count > 0`. In scripts, always inspect the
`errors[]` list -- a zero exit alone does not mean the whole batch went in
without issues (it means there were no partial failures). A non-zero exit
means *some* items failed; the successful items still landed.

This tolerance applies to **API** failures. A malformed file is a usage error
instead: if a section is not a mapping of ID to description (a `tables:` list,
a scalar under a `columns:` table ID, a document that is not a mapping at all),
the whole file is rejected **before the first write** with
`INVALID_ARGUMENT` and exit **2**, and the message names the offending key plus
its actual type. Nothing is half-applied, so fixing the file and re-running is
always safe.

## Migrating legacy column descriptions (since v0.88.0)

A project that was documented with kbagent 0.87.0 or older still has its column
descriptions in the invisible flat convention. Find them with `table-detail`:

```bash
kbagent --json storage table-detail --project ALIAS --table-id in.c-sales.orders \
  | jq '.data.legacy_column_descriptions'
```

Convert them with `storage describe-migrate`. Always scan first:

```bash
# 1. What would change? (no writes at all)
kbagent --json storage describe-migrate --project ALIAS --dry-run

# 2. Narrow the scope if you want to go table by table or bucket by bucket
kbagent --json storage describe-migrate --project ALIAS --bucket-id in.c-sales --dry-run
kbagent --json storage describe-migrate --project ALIAS \
  --table-id in.c-sales.orders --table-id in.c-sales.customers --dry-run

# 3. Apply (interactive confirm unless --yes)
kbagent --json storage describe-migrate --project ALIAS --bucket-id in.c-sales --yes
```

`--table-id` (repeatable) and `--bucket-id` are mutually exclusive; with neither,
every table in the project is scanned. Tables without legacy keys are skipped
silently and only counted.

Per-column rules:

- **conflict** -- the column already has a *different* visible description
  (native field or `columnMetadata`). The legacy value is NOT written and its
  flat key is NOT deleted; the entry is reported in `skipped[]` with both values
  so you can decide. The newer, visible value wins by default.
- **orphan** -- the flat key names a column that no longer exists on the table.
  Skipped and left in place unless you pass `--prune-orphans`, which deletes it.
- **identical** -- the visible description already matches the legacy value.
  Nothing is written; the redundant flat key is deleted.
- otherwise the value is migrated, and the flat key is deleted after the write
  succeeds. A failed cleanup is reported (`reason: "delete_failed"`) but never
  fails the command -- the description is already durable.

Per-table failures are collected into `errors[]` and never abort the run
(convention #11), so one inaccessible table does not stop a project-wide sweep.
Re-running is safe: a table with no legacy keys left is a no-op.

## End-to-end example: onboarding a new bucket

```bash
# 1. Create the bucket and tables (or sync them from another project)
kbagent storage create-bucket --project ALIAS --stage in --name c-sales
kbagent storage create-table --project ALIAS --bucket-id in.c-sales --name orders \
  --column order_id:INTEGER --column total:NUMERIC --primary-key order_id

# 2. Apply all descriptions from a tracked YAML file
kbagent --json storage describe-batch \
  --project ALIAS \
  --from-file ./docs/keboola/descriptions.yaml

# 3. Verify by reading back
kbagent --json storage table-detail --project ALIAS --table-id in.c-sales.orders \
  | jq '{description: .data.description, columns: .data.column_details}'
```

## Precedence vs the native description field (buckets and tables)

Columns follow their own chain -- see "Read precedence" above; this section is
about the bucket-level and table-level description only.

The Storage API has a native `description` field on buckets and tables, but
it is only settable at creation time. Anything you set with `describe-*`
lives on the metadata endpoint. When both are present, `storage bucket-detail`
/ `storage table-detail` surface the metadata value (the one you wrote with
`describe-*`). The native field is the fallback for legacy objects where
no metadata entry exists. System-provided `KBC.description` entries (e.g.
those auto-stamped by components) are filtered out on read-back -- only
entries with `provider="user"` are considered the canonical description.

## Key behaviors

- `describe-*` is **upsert** -- no append mode; re-running replaces the value.
- Column descriptions go through the native `.../tables/{id}/definition`
  endpoint *(since v0.88.0)* with `isDescriptionSystemManaged: false`; the
  backend mirrors them into `columnMetadata` `KBC.description`, so the UI, the
  MCP server and the warehouse all see them. Legacy flat
  `KBC.column.{name}.description` keys are read as a last-resort fallback,
  reported in `legacy_column_descriptions`, and converted by
  `storage describe-migrate`.
- Unknown column names abort `describe-column` / `describe-batch` before any
  write *(since v0.88.0)*.
- `describe-batch` is **partial-failure-tolerant** -- check `errors[]` even
  on exit code 0.
- All commands support `--branch ID` to target a dev branch.
- Read back via `storage bucket-detail` / `storage table-detail` -- the
  `metadata` field on those responses contains the raw metadata array if
  you need to inspect timestamps or providers.
- Non-user (`system`) `KBC.description` entries are ignored on read-back;
  they do not override the native `description` field.
