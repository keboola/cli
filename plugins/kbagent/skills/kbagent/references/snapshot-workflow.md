# Table Snapshot Workflow

Table snapshots (since v0.75.0, issue #512) are point-in-time backups of a
Storage table -- data, columns, and primary key -- stored by the platform.
A snapshot can later be restored as a **NEW** table in any existing bucket.
Typical uses: a safety net before a risky schema change or re-seed, and
"copy this table as it was" duplication.

## Quick reference

| Command | Purpose | Permission |
|---------|---------|------------|
| `storage snapshot-create` | Snapshot a table (async job; receipt carries `snapshot_id`) | write |
| `storage snapshots` | List a table's snapshots | read |
| `storage snapshot-detail` | One snapshot's detail (embeds the source table) | read |
| `storage table-from-snapshot` | Restore a snapshot as a NEW table | write |
| `storage snapshot-delete` | Delete snapshots (forecloses restores) | destructive |

## Backup before a risky change

```bash
kbagent --json storage snapshot-create \
  --project ALIAS \
  --table-id in.c-main.customers \
  --description "before typify 2026-07-22"
```

- Returns `snapshot_id` -- **save it**; restores are addressed by it.
- Async `tableSnapshotCreate` storage job; the CLI polls to completion.
- The snapshot captures data + columns + primary key at this moment.
  Later imports/schema changes on the source table do not touch it.

## Find a snapshot later

```bash
kbagent --json storage snapshots --project ALIAS --table-id in.c-main.customers
kbagent --json storage snapshot-detail --project ALIAS --snapshot-id 954
```

- List is per source table; entries carry `id`, `createdTime`, `description`,
  `creatorToken`.
- Snapshot IDs are **global** (not table-scoped): `snapshot-detail` embeds the
  source `table` object (id, columns, primaryKey), so a bare ID can always be
  traced back to its origin table.

## Restore as a new table

```bash
kbagent --json storage table-from-snapshot \
  --project ALIAS \
  --snapshot-id 954 \
  --bucket-id in.c-main \
  --name customers_restored \
  --dry-run          # preview first, then re-run without it
```

Three traps (all verified live):

1. **`--name` is REQUIRED.** The API rejects an omitted/empty name
   (`Table create option "name" is required and cannot be empty`).
2. **No overwrite.** Restoring onto an existing table name fails with a
   duplicate-name error. Restore under a NEW name, verify the data, then
   promote with `storage swap-tables` (or `delete-table` + rename pattern).
3. **The destination bucket must already exist** -- any bucket works, not
   just the source one.

The restore is an async storage job; the CLI polls to completion, so the
receipt's `table.rowsCount` is authoritative. Verify with:

```bash
kbagent --json storage table-detail --project ALIAS --table-id in.c-main.customers_restored
```

## Roll a table back to a snapshot (full pattern)

There is no in-place restore. The safe sequence:

1. `storage table-from-snapshot ... --name customers_rollback` (new table)
2. `storage table-detail` on the new table -- verify rows/columns/PK
3. `storage swap-tables --table-id in.c-main.customers --target-table-id in.c-main.customers_rollback --branch <ID> --yes`
   (any branch incl. production; aliases are NOT transferred -- see
   storage-types-workflow)
4. Optionally `storage delete-table` the swapped-out copy once satisfied

## Cleanup

```bash
kbagent --json storage snapshot-delete --project ALIAS --snapshot-id 954 [--snapshot-id 955] [--dry-run] [--yes]
```

- Destructive: restores from that snapshot become impossible. The source
  table is untouched.
- Batch-tolerant: one failing ID does not abort the rest; exit 1 when any
  ID failed (`failed[]` in JSON).

## Anti-patterns

- Exporting to CSV as a "backup" -- loses column types and primary key;
  snapshots keep both.
- Restoring straight onto the production table name -- duplicate-name error;
  use the restore-then-swap pattern above.
- Expecting `create-table --snapshot-id` -- not a thing; the restore goes
  through the classic `tables-async` endpoint, `create-table` uses
  `tables-definition`, which does not accept snapshots.
