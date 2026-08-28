# Partition Table Workflow -- Add Time Partitioning or Clustering to BigQuery-backed Storage Tables

End-to-end procedure for an AI agent (or operator) to partition/cluster a BigQuery-backed Keboola Storage table via `storage create-table --source-table-id` and `storage swap-tables`, in-place without a separate BigQuery tool. This approach verifies that the physical partition/cluster spec was actually applied (via BigQuery's own `bq show`) and guards against permission denials and client timeouts on large tables.

Verified end-to-end 2026-08-25 against a real 6.29M-row BigQuery table on the europe-west3 stack. `storage create-table --source-table-id` really does copy data into a new table with the requested partitioning/clustering/primary key, and `storage swap-tables` really does exchange the physical tables while the Storage table id and identity stay put.

## Canonical pattern

```
[unpartitioned table]  -->  storage create-table  -->  [partitioned copy]
                                (new name, same data)          |
                                                                v
                            [table now partitioned]  <--  storage swap-tables
                            (downstream sees layout)      (physical exchange)
                                                                |
                                                                v
                                        [unpartitioned copy]
                                      (rollback artifact)
```

The partitioned copy is created under a new timestamped name (`<TABLE>_part_<YYYYMMDD>_<HHMMSS>`) to avoid collision and to preserve the original as a rollback artifact. After the swap, the original table id carries the new partition/cluster spec, and the `_part_<ts>` name holds the old unpartitioned data — useful for reverting if needed.

## Typical workflow

```bash
# 1. List projects and find the target's alias
kbagent --json project list

# 2. List branches to find the production (default) one
kbagent --json branch list --project ALIAS

# 3. Verify the source table exists and inspect its current schema
kbagent --json storage table-detail --project ALIAS --table-id BUCKET.TABLE
bq show --format=prettyjson PROJECT_ID:DATASET.TABLE

# 4. Check that the operations you're about to run are permitted
kbagent permissions check storage.create-table
kbagent permissions check storage.swap-tables

# 5. Create a partitioned copy (never the original name, always timestamped)
kbagent --json storage create-table \
  --project ALIAS --bucket-id BUCKET \
  --name TABLE_part_20260825_143022 \
  --source-table-id BUCKET.TABLE \
  --time-partitioning-type DAY --time-partitioning-field DATE_COL \
  --clustering-field COL1 --clustering-field COL2 \
  --primary-key PK_COL \
  --branch BRANCH_ID

# 6. Verify the partition/cluster spec was actually applied (not just registered)
bq show --format=prettyjson PROJECT_ID:DATASET.TABLE_part_20260825_143022

# 7. Dry-run the swap to confirm the two table ids
kbagent --json storage swap-tables --project ALIAS \
  --table-id BUCKET.TABLE \
  --target-table-id BUCKET.TABLE_part_20260825_143022 \
  --branch BRANCH_ID --dry-run

# 8. Swap for real
kbagent --json storage swap-tables --project ALIAS \
  --table-id BUCKET.TABLE \
  --target-table-id BUCKET.TABLE_part_20260825_143022 \
  --branch BRANCH_ID --yes

# 9. Verify the swap landed by checking the original table id now carries the partition/cluster spec
bq show --format=prettyjson PROJECT_ID:DATASET.TABLE

# 10. The partitioned table is now live under its original name.
# The unpartitioned copy sits under the _part_<ts> name as a rollback artifact.
# Do not delete it until the user has validated downstream impact.
```

## Key details

### Permission policy is mandatory

Before step 5, always check:
```bash
kbagent permissions check storage.create-table
kbagent permissions check storage.swap-tables
```

If either returns `allowed: false`, stop and ask the user to grant the operation themselves. **Never call `permissions set` or `permissions reset`** — permission widening is a bigger decision than the operation it's gating, and it may be a deliberate guardrail.

### Physical state is authority, not Storage's registered metadata

`kbagent storage table-detail` reports what Keboola *registered*, not what BigQuery actually has. After any create or swap, **always verify with `bq show`** — that's the authoritative source for partition/cluster/primary key specs and row counts.

After a swap, Keboola's own registered column-type metadata (from `storage table-detail`) can lag or fail to resync to match the new physical table. `bq show` will be correct; Storage's declared schema may not be. Don't rely on Storage's `definition` for validation.

### Client timeout does not mean failure

The client can report `STORAGE_JOB_TIMEOUT` (60-second poll limit) on a large table even though the async Storage job completed successfully server-side. When you see this:

1. **Do not automatically retry** — if the swap already landed, retrying would swap it right back.
2. **Check physical state first** with `bq show` on the original table id — if it now carries the partition/cluster spec, the swap succeeded and the timeout is just a polling artifact.
3. **Only retry if `bq show` confirms the swap did NOT land** (original table still has no partitioning).

### The pre-swap artifact is your rollback path

The original unpartitioned table sits under the `_part_<ts>` name after the swap. **Do not delete it** until the user has validated that downstream consumers handle the new partition/cluster spec correctly. This is the only way to revert if needed.

### Reverting a swap

`storage swap-tables` is symmetric. To revert, run the exact same swap command again (unchanged):

```bash
kbagent --json storage swap-tables --project ALIAS \
  --table-id BUCKET.TABLE \
  --target-table-id BUCKET.TABLE_part_20260825_143022 \
  --branch BRANCH_ID --dry-run

kbagent --json storage swap-tables --project ALIAS \
  --table-id BUCKET.TABLE \
  --target-table-id BUCKET.TABLE_part_20260825_143022 \
  --branch BRANCH_ID --yes
```

The second swap exchanges the physical tables right back: `TABLE` returns to unpartitioned data, and `TABLE_part_<ts>` holds the partitioned copy. Always get explicit confirmation first and verify with `bq show` afterward.

## When to use this workflow

Apply this workflow when:

- Adding time-partitioning to an existing Keboola Storage table without disrupting downstream configs.
- Adding or changing clustering keys on a live table.
- Adding or changing a primary key on a live table.
- The table is large enough that a full re-extract is more expensive than a swap.

Skip this workflow when:

- The table is **<100 rows and recreated daily by an extractor** — cheaper to fix the extractor's destination definition and re-extract.
- The target is an **alias** (`isAlias: true` in `storage table-detail`) — swap exchanges physical tables, not aliases; touch the underlying physical table instead.
- You need to **retype columns** — that requires a transformation job with column-type casting, not just a same-schema copy. See `typify-table-workflow.md` for the full procedure.

## What this does NOT cover

- **Consumer/downstream validation before swapping in production.** This procedure assumes the table has no critical downstream impact (e.g., it's internal, or only used by tools that ignore partition/cluster metadata). For tables with real consumers, rehearse the swap in a dev branch first — verify that downstream configs still run and produce the same results post-swap. See `typify-table-workflow.md` for a dev-branch rehearsal pattern.
- **Deciding whether a table should be partitioned, and on what key.** That's project-specific triage; this workflow assumes the partition/cluster spec is already decided.
- **Recording the conversion.** If your project tracks physical-layout changes (e.g., in a `docs/` decision ledger), update it by hand — this workflow doesn't know about your tracking convention.

## Gotchas reference

| Issue | Root cause | Prevention |
|-------|-----------|-----------|
| Swap landed server-side but client timed out | Large table + 60s poll limit | Check `bq show` before retrying |
| Original table id now has wrong partition/cluster spec | Created copy had wrong spec, didn't verify before swap | Always `bq show` the copy before swapping |
| Downstream configs fail post-swap | Downstream code assumes table has no partitioning | Rehearse in dev branch first (see typify-table-workflow.md) |
| Can't revert | Deleted the `_part_<ts>` artifact too early | Never delete pre-swap artifact until user approves |
| Permission denied on swap | kbagent permissions policy blocked the operation | Check `permissions check storage.swap-tables` first; ask user to grant via `permissions set` |
| Storage metadata still shows old schema after swap | Storage's metadata doesn't resync to physical state | Use `bq show` for validation, not `kbagent storage table-detail` |
