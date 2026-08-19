# SQL Transformation Workflow (since v0.73.0)

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
- `--storage @storage.json` REPLACES `configuration.storage` wholesale --
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
