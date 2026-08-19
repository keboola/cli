# Storage Describe Workflow

`kbagent storage describe-*` attaches human-readable descriptions to storage
buckets, tables, and columns so that downstream consumers (dashboards, the
the Keboola UI, `kbagent storage buckets`/`tables`, AI agents) can surface meaningful
documentation rather than raw IDs. Descriptions are stored as metadata on
the storage object and round-trip via `storage bucket-detail` / `storage
table-detail`.

## Quick reference

| Command | Purpose |
|---------|---------|
| `storage describe-bucket` | Set a bucket description |
| `storage describe-table` | Set a table description |
| `storage describe-column` | Set descriptions on one or more columns |
| `storage describe-batch` | Apply bucket/table/column descriptions from a YAML file |
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

Descriptions are stored as metadata entries on the object:

- **Bucket description** -- `KBC.description` (provider=user) on bucket metadata
- **Table description** -- `KBC.description` (provider=user) on table metadata
- **Column description** -- `KBC.column.{column_name}.description` on the
  **table's** metadata. Keboola has no user-writable column-metadata endpoint,
  so this key convention is the storage layer for column descriptions. Read
  them back via `storage table-detail` (`column_details[].description`).

Descriptions are `upsert`: calling `describe-*` with a new text replaces
whatever was there before. There is no append mode.

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

Column descriptions live under `KBC.column.{name}.description` on the
**table's** metadata -- they are NOT attached to the column record itself.
If you rename or delete a column, the old key lingers until you manually
clean it up (there is no `--delete-column-description` command today).

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

## Precedence vs the native description field

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
- Column descriptions piggy-back on table metadata via the
  `KBC.column.{name}.description` key convention.
- `describe-batch` is **partial-failure-tolerant** -- check `errors[]` even
  on exit code 0.
- All commands support `--branch ID` to target a dev branch.
- Read back via `storage bucket-detail` / `storage table-detail` -- the
  `metadata` field on those responses contains the raw metadata array if
  you need to inspect timestamps or providers.
- Non-user (`system`) `KBC.description` entries are ignored on read-back;
  they do not override the native `description` field.
