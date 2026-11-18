# SQL Transformation Workflow

Native authoring/editing of SQL transformations -- the CLI port of the
upstream `create_sql_transformation` / `update_sql_transformation` tools
(#396). Since v0.85.0 this IS the way: the MCP passthrough that exposed those
tools is gone.

## Create

```bash
kbagent transformation create --project prod \
    --name "Orders Daily Rollup" \
    --sql-file rollup.sql \
    --created-table orders_daily
```

- Component id is derived from the project `default_backend`
  (snowflake -> `keboola.snowflake-transformation`, bigquery ->
  `keboola.google-bigquery-transformation`). Any other backend fails fast --
  pass `--component-id` explicitly.
- The SQL is split one statement per `script[]` element (same splitter the
  sync engine uses); everything lands in a single block `Blocks` with one
  code `Code` -- identical to what the UI produces.
- Each `--created-table T` adds an output mapping
  `T -> out.c-<cleaned-transformation-name>.<T>`. The bucket name is derived
  from the transformation NAME at create time -- renaming the transformation
  later does NOT move the bucket. `--created-table` is a declarative mapping
  hint, not a guarantee the SQL actually creates that table.
- `--dry-run` prints the would-be payload without POSTing.

## Inspect (ALWAYS before edit)

```bash
kbagent --json transformation show --project prod --config-id 123456
```

- Prints the block/code tree with **synthetic positional ids** `b{i}` /
  `b{i}.c{j}` (block i, code j) plus storage mappings.
- Ids are NOT persisted anywhere -- they are re-derived from array positions
  on every call. **Any structural edit renumbers them.** Fetch fresh ids via
  `show` immediately before every `edit` (fresh-fetch rule).
- `--component-id` optional: all known SQL transformation components are
  probed until one returns the config.

## Edit

```bash
kbagent transformation edit --project prod --config-id 123456 \
    --change-description "split rollup into two blocks" \
    --op '{"op": "add_block", "name": "Cleanup", "position": 1}' \
    --op '{"op": "set_code", "block_id": "b0", "code_id": "b0.c0", "script": "SELECT 1;"}'
```

- 9 ops: `add_block`, `remove_block`, `rename_block`, `add_code`,
  `remove_code`, `rename_code`, `set_code`, `add_script`, `str_replace`.
- Ops in one invocation apply **sequentially against BATCH-START ids**: an
  element added mid-batch has no id until the next `show`; removed elements
  invalidate later positional references only on the NEXT invocation.
- Unknown block/code ids fail with the list of currently valid ids.
- `--storage JSON` / `--storage-file storage.json` REPLACES `configuration.storage` wholesale --
  include EVERY input/output mapping you want to keep, not just the new ones.
- `--dry-run` previews the resulting tree + op summary without any PUT.

## Gotchas

- Show-before-edit is not optional: positional ids drift after every
  structural change (the same sharp edge the upstream tool had).
- The output bucket is coupled to the create-time name; treat renames as a
  new-bucket event and update downstream input mappings accordingly.
- `transformation edit` preserves non-`blocks` keys inside `parameters`
  (variables links etc.) -- a deliberate improvement over the upstream tool,
  which replaced `parameters` wholesale.

## Stop a transformation on purpose (data checks)

*(since vNEXT)*

A data check that must fail the job when the data is wrong (a sum that must be
0, a table that must not be empty) is written with `ABORT_TRANSFORMATION`, never
as a `SELECT`:

```sql
SET ABORT_TRANSFORMATION = (
  SELECT IFF(COUNT(*) = 0, 'Check failed: MON_EXPORT_GATE is empty', '')
  FROM "MON_EXPORT_GATE"
);
```

- An empty string lets the job go on. Any other value stops it, and the job
  fails with `Transformation aborted with message "<value>"`, which sends the
  usual failure notification.
- Snowflake (`keboola.snowflake-transformation`) and BigQuery
  (`keboola.google-bigquery-transformation`) both support it. BigQuery declares
  the variable itself, so use `SET`, not `DECLARE`.
- **Never write the check as `SELECT ... 1/0`** (or a failing cast). The runtime
  never runs a statement that starts with `SELECT`, so such a check never stops
  the job. Until Snowflake transformation 1.7.1 a comment or a line break before
  the `SELECT` made it run by accident; since 1.7.1 it is skipped in every format,
  and the job succeeds with bad data and without a notification.
- `transformation create` / `edit` and `sync push` warn about every skipped
  `SELECT` (`SQL_SELECT_SKIPPED_BY_RUNTIME`). `kbagent doctor` lists them in all
  connected projects (check `skipped_selects`, `details.statements[]`);
  `ran_before_fix: true` marks a statement that ran until 1.7.1 -- the first
  candidates to rewrite as above. Rewrite only after the owner agrees: turning
  a skipped check back on makes jobs fail again.
