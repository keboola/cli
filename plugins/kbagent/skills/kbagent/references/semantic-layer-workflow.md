# Semantic Layer Workflow -- Models, Metrics, Constraints

The Keboola semantic layer (aka **metastore**) is a project-scoped catalogue
of datasets, metrics, relationships, constraints, and glossary terms. It is
served from a separate API at `metastore.<stack>` (derived from
`connection.<stack>` by string-substitution; cloud/region-agnostic). Auth is
the same `X-StorageApi-Token` as Storage.

`kbagent semantic-layer ...` (alias `kbagent sl ...`, hidden) wraps the
metastore so AI agents and CI scripts don't roll their own `urllib` loops.
For one-line command reference, see
[commands-reference.md](commands-reference.md#semantic-layer-metastore-since-v0340).
For the live-validated metastore contract surprises (constraint rule shape,
name regex, CODE_METRIC cascade), see
[gotchas.md](gotchas.md#semantic-layer-constraint-rule-is-a-string-not-an-object-since-v0340).

## When to use what

| Goal | Command |
|------|---------|
| Show what's in a project's models | `semantic-layer model list` then `semantic-layer show` |
| Pre-flight a model for structural / phantom-field issues | `semantic-layer validate [--deep]` |
| Back up before destructive edits | `semantic-layer export` |
| Compare a dev model to prod | `semantic-layer diff --project-a dev --project-b prod` |
| Add one entity (metric / dataset / etc.) | `semantic-layer add <kind>` |
| Rename a metric (safely cascade) | `semantic-layer edit metric --new-name` |
| Remove a metric (orphan-check first) | `semantic-layer remove metric` |
| Restore from a snapshot | `semantic-layer import --file ... --dry-run` then real |
| Promote dev -> prod | `semantic-layer promote --from-project dev --to-project prod` |
| Bootstrap a model from storage tables | `semantic-layer build --tables ...` (heuristic) |
| Encrypt the storage token for a Python container | `semantic-layer token --encrypt` |

---

## Workflow 1 -- Inspect a project's semantic layer

```bash
# 1. Enumerate models in the project
kbagent --json semantic-layer model list --project prod
# -> {"models": [{"id": "<uuid>", "name": "core_model", "sql_dialect": "Snowflake", ...}]}

# 2. Show entity counts for the model
kbagent --json semantic-layer show --project prod --model core_model
# -> {"datasets": [...], "metrics": [...], "relationships": [...], ...}

# 3. Drill into one type
kbagent --json semantic-layer show --project prod --model core_model --type metric
kbagent --json semantic-layer show --project prod --model core_model --type constraint

# 3b. Fetch the JSON Schema of a type before building/validating one (0.73.0+).
#     Server-fetched, so it always matches the deployed metastore version.
kbagent --json semantic-layer schema --project prod --type metric,dataset
kbagent --json semantic-layer schema --project prod --all
# -> {"project": "prod", "schemas": [{"type": "metric", "schema": {...}}, ...]}

# 4. Validate (no Snowflake probe -- fast)
kbagent --json semantic-layer validate --project prod --model core_model

# 5. Deep-validate (parallel column-existence probe via StorageService)
kbagent --json semantic-layer validate --project prod --model core_model --deep
```

When `--model` is omitted and the project has exactly one model, the CLI
auto-selects it. With more than one model, omitting `--model` exits 2 /
`USAGE_ERROR` -- always run `model list` first if you're unsure.

`validate` reports `errors[]` (block work) and `warnings[]` (review).
The `type` field on each entry is one of the UPPER_SNAKE strings below;
filter your jq with these exact values:

- `DUPLICATE` -- two entities of the same type share a name (error).
- `CONSTRAINT_ORPHAN` -- a constraint's `metrics[]` names a metric
  that no longer exists (error).
- `DANGLING_METRIC` -- a metric's `dataset` is a tableId no dataset
  references (error).
- `DANGLING_RELATIONSHIP` -- a relationship's `from`/`to` is a tableId
  no dataset references (error).
- `SUM_ON_PCT` -- `SUM(...)` on a column whose name suggests a
  percentage / ratio (error).
- `SEVERITY_SUFFIX` -- a constraint name doesn't end in
  `_critical/_warning/_healthy/_review` (warning).
- `PHANTOM_FIELD` (`--deep` only) -- a dataset declares a field that
  isn't in the Snowflake table's actual columns (error).
- `METRIC_PHANTOM` (`--deep` only) -- a column referenced in metric
  `sql` isn't in the table's actual columns (error).
- `AGG_ON_STRING` (`--deep` only) -- `SUM(...)` / `AVG(...)` over a
  Snowflake STRING column (error).
- `DEEP_FETCH_FAILED` (`--deep` only) -- couldn't fetch the Snowflake
  schema for a dataset; deep checks for that dataset are skipped (warning).

---

## Workflow 2 -- Export and back up before destructive edits

ALWAYS export before a rename, a remove, or a promote. The metastore has
no soft-delete and no version history; the snapshot is the only restore
path.

```bash
# 1. Snapshot to disk (default path stamped with model name + timestamp)
kbagent --json semantic-layer export --project prod --model core_model
# -> {"path": "./sl_export_core_model_20260514_120030.json", "counts": {...}}

# 2. Verify the snapshot has every type
jq '. | {datasets: .datasets | length, metrics: .metrics | length, ...}' \
  ./sl_export_core_model_20260514_120030.json

# 3. Make the change
kbagent semantic-layer edit metric --project prod --model core_model \
    --name revenue_growth --new-name revenue_growth_qoq --yes

# 4. Diff the live model against the snapshot to confirm the diff is
#    exactly what you intended (nothing else moved)
kbagent --json semantic-layer diff \
    --file-a ./sl_export_core_model_20260514_120030.json \
    --project-b prod --model-b core_model
```

The snapshot is self-describing (carries `model.meta`, `schemaVersion`,
per-entity dicts) and round-trips through `semantic-layer import` for
a full restore.

---

## Workflow 3 -- Add a metric with a paired range constraint

The recommended pattern is "validate SQL first, then create the metric,
then attach a constraint" -- not the other way around (a constraint
referencing a nonexistent metric is rejected at POST time).

```bash
# 1. Sanity-check the metric SQL against a workspace BEFORE creating it.
#    (Optional but recommended for non-trivial expressions.)
kbagent --json workspace create --project prod --name sl-sanity
# -> grab workspace_id
kbagent --json workspace load --project prod --workspace-id W \
    --tables out.c-revenue.fact_orders
kbagent --json workspace query --project prod --workspace-id W \
    --sql "SELECT SUM(amount) FROM \"out.c-revenue.fact_orders\""
# Confirm the SQL works; clean up the workspace
kbagent workspace delete --project prod --workspace-id W

# 2. Add the metric (--dataset is a Storage tableId, NOT a dataset name)
kbagent semantic-layer add metric \
    --project prod --model core_model \
    --name revenue \
    --sql 'SUM(amount)' \
    --dataset out.c-revenue.fact_orders \
    --description 'Total revenue across all orders'

# 3. Attach a range constraint (rule is a STRING, name regex
#    ^[a-z][a-z0-9_]*$, severity is the 3-value API enum)
kbagent semantic-layer add constraint \
    --project prod --model core_model \
    --name revenue_non_negative_warning \
    --constraint-type inequality \
    --rule 'value >= 0' \
    --metrics revenue \
    --severity warning

# 4. Add the _critical band as a separate constraint
kbagent semantic-layer add constraint \
    --project prod --model core_model \
    --name revenue_minimum_critical \
    --constraint-type inequality \
    --rule 'value >= 1000' \
    --metrics revenue \
    --severity error

# 5. Validate (the constraint name suffix should match the severity)
kbagent --json semantic-layer validate --project prod --model core_model
```

Note: `--rule` is a STRING (`"value >= 0"`), NEVER a `{bounds: ...}`
object. The `sl-builder` skill docs are wrong on this -- the live API
rejects the object shape with HTTP 400. See [gotchas.md](gotchas.md).

The 4-band health convention (`_critical / _warning / _healthy /
_review`) lives in the constraint NAME suffix. The API `severity` is a
separate 3-value enum (`error | warning | info`). Typical pairings:
`_critical` -> `error`, `_warning` -> `warning`, `_healthy` -> `info`,
`_review` -> `info`. There is no automatic mapping; the operator sets
both. `validate` warns when they drift.

---

## Workflow 4 -- Rename a metric safely

The single biggest footgun in the semantic layer: a metric rename
changes `CODE_METRIC` (the downstream join key in
`DIM_METRIC_THRESHOLD` / `FACT_METRIC_*`), which silently breaks SQL
joins that pinned the old value.

```bash
# 0. ALWAYS export first (rename is destructive in the cascade sense)
kbagent --json semantic-layer export --project prod --model core_model

# 1. Run the rename. kbagent prints:
#    - old/new CODE_METRIC (computed via
#      re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_"))
#    - the list of constraints whose metrics[] will be cascaded
#    - a confirm prompt (suppress with --yes after auditing)
kbagent semantic-layer edit metric \
    --project prod --model core_model \
    --name revenue_growth \
    --new-name revenue_growth_qoq
# Will print something like:
#   CODE_METRIC: REVENUE_GROWTH -> REVENUE_GROWTH_QOQ
#   Will DELETE+POST the following constraints (cascade):
#     - revenue_growth_minimum_warning
#     - revenue_growth_band_review
#   Proceed? [y/N]:

# 2. Verify the cascade
kbagent --json semantic-layer show --project prod --model core_model --type constraint \
    | jq '.constraints[] | select(.metrics | index("revenue_growth_qoq"))'

# 3. CRITICAL FOLLOW-UP: audit downstream SQL that joins on CODE_METRIC.
#    Anywhere your pipeline has
#      JOIN dim_metric_threshold dmt
#        ON dmt.CODE_METRIC = 'REVENUE_GROWTH'
#    needs updating to
#        ON dmt.CODE_METRIC = 'REVENUE_GROWTH_QOQ'
#    These joins WILL silently start returning empty rows otherwise.
```

If the new POST fails (e.g. the new name collides), the service
re-POSTs `original_attrs` to roll back and reports rollback
success/failure in the response envelope's `rollback` field. If the
rollback itself fails, the model is left in a partial state -- run
`semantic-layer validate` immediately.

**Partial cascade state (since v0.41.10)**: the cascade has per-item
rollback only -- each constraint DELETE+POST rolls back individually.
If the metric rename succeeds but M of N dependent constraints fail
to repoint, the envelope sets `partial_state: true` at the top level
and a `recovery_hint` string. Human-mode CLI prints a bright red
`PARTIAL STATE` banner. Use the recovery recipe:

```bash
# 1. Diagnose: surface every dangling constraint reference
kbagent --json semantic-layer validate --project prod --model core_model

# 2. Re-cascade each failed constraint manually
kbagent semantic-layer edit constraint --project prod --model core_model \
    --name revenue_growth_minimum_warning --new-metrics revenue_growth_qoq
```

Atomic two-phase commit was rejected as disproportionate: the
metastore has no PATCH endpoint, so every cascade 'stage' is itself a
DELETE+POST that can fail; true atomicity would require side-staging
every cascade item.

---

## Workflow 5 -- Remove a metric (with orphan-check)

```bash
# 0. Export first
kbagent --json semantic-layer export --project prod --model core_model

# 1. Pre-flight scan: what constraints reference this metric?
kbagent --json semantic-layer show --project prod --model core_model --type constraint \
    | jq '.constraints[] | select(.metrics | index("revenue_growth"))'

# 2. If there are orphans-to-be, EITHER remove them first
kbagent semantic-layer remove constraint \
    --project prod --model core_model --name revenue_growth_minimum_warning --yes
# OR plan a soft-delete via rename:
#   kbagent semantic-layer edit metric --new-name revenue_growth_archived_20260514

# 3. Run remove. The orphan warning is ALWAYS printed, even with --yes.
kbagent semantic-layer remove metric \
    --project prod --model core_model --name revenue_growth
# Output:
#   Removing metric 'revenue_growth' will orphan 0 constraint(s):
#   (or a list if you skipped step 2)
#   These constraints will have a dangling reference in DIM_METRIC_THRESHOLD.
#   Delete metric 'revenue_growth' anyway? [y/N]:

# 4. Verify
kbagent --json semantic-layer validate --project prod --model core_model
```

Non-TTY invocations without `--yes` refuse with exit 2 -- the warning
is non-suppressible. CI scripts MUST pass `--yes` AND audit the orphan
list AHEAD OF TIME (e.g. via `show --type constraint`); kbagent prints
the warning but does not block on `--yes`.

---

## Workflow 6 -- Promote a model dev -> prod

```bash
# 1. Export both sides (safety net)
kbagent --json semantic-layer export --project dev --output /tmp/dev.json
kbagent --json semantic-layer export --project prod --output /tmp/prod.json

# 2. Diff to preview what will move
kbagent --json semantic-layer diff --project-a dev --project-b prod
# -> shows added[] / removed[] / changed[] per type with diff_keys

# 3. Dry-run the promote: classifies items NEW / IDENTICAL / CHANGED
#    (deep-equality after stripping modelUUID + timestamps)
kbagent --json semantic-layer promote \
    --from-project dev --to-project prod --dry-run \
    | jq '.metrics, .constraints'
# Inspect:
#   metrics.new          -- count of items the promote will POST
#   metrics.overwritten  -- count of CHANGED items that will be DELETE+POSTed
#   metrics.identical    -- count of items skipped (already match)
#   metrics.changes[]    -- per-item diff with diff_keys
#   metrics.failed[]     -- per-item failures (e.g. dry-run validators)

# 4. If the dry-run looks right, run for real. --yes skips the confirm.
kbagent --json semantic-layer promote \
    --from-project dev --to-project prod --yes

# 5. Verify
kbagent --json semantic-layer validate --project prod --deep
```

`promote` is **additive + overwrite only** -- it NEVER deletes items
from prod that aren't in dev. To remove items, do that explicitly with
`semantic-layer remove` after the promote. This is intentional: it
prevents a partial dev model from wiping prod-only entities (e.g. an
emergency hotfix metric).

Use `--types datasets,metrics` to scope the promote to specific entity
types (e.g. promote metric changes without touching constraints).

---

## Workflow 7 -- Bootstrap a model from storage tables

```bash
# 1. Decide on the table set
kbagent --json storage tables --project prod --bucket-id out.c-revenue \
    | jq -r '.tables[].id'

# 2. Dry-run the build to inspect the generated JSON
kbagent --json semantic-layer build \
    --project prod \
    --tables out.c-revenue.fact_orders,out.c-revenue.dim_customers \
    --dry-run \
    --output /tmp/built.json
# Response carries: fallback_used: "heuristic"
# Generated: 2 datasets, 2 metrics (COUNT(*)), 0 relationships,
#            0 constraints, 2 glossary entries

# 3. If the scaffold looks right, push it (omit --dry-run; omit --model
#    to create a new model)
kbagent --json semantic-layer build \
    --project prod \
    --tables out.c-revenue.fact_orders,out.c-revenue.dim_customers \
    --name revenue_model

# 4. Refine with real business logic
kbagent semantic-layer add metric \
    --project prod --model revenue_model \
    --name revenue --sql 'SUM(amount)' \
    --dataset out.c-revenue.fact_orders

kbagent semantic-layer add relationship \
    --project prod --model revenue_model \
    --name orders_to_customers \
    --from out.c-revenue.fact_orders \
    --to out.c-revenue.dim_customers \
    --on 'fact_orders.customer_id = dim_customers.id'
```

**AI caveat**: `build` falls back to a DETERMINISTIC heuristic because
the kbagent AI Service client has no JSON-generation endpoint as of
v0.41.0. The heuristic synthesises:

- One dataset per `--tables` entry, with FQN derived and `fields[]`
  role-classified (PK_/FK_->key, *_DATE/*_DT->timestamp,
  numeric amount/value/rate->measure, else dimension).
- One `COUNT(*)` metric per dataset.
- One glossary entry per table.
- No relationships, no constraints.

Treat `build` output as a **starting scaffold**, not a finished model.
The push loop walks all 5 child types in dependency order -- this
fixes a long-standing `sl-build` skill bug where `semantic-constraint`
was silently dropped.

For richer AI-assisted generation (full SQL analysis, relationship
inference, paired range constraints), the `sl-build` skill in
`04_AI_Kit/ai-kit/` is the right tool. The two are interoperable via
the same metastore contract; bridge between them as needed.

**Rollback on push failure (since v0.41.10)**: if a child POST fails
mid-push, the service walks the list of successfully-POSTed children
in REVERSE PUSH_ORDER and DELETEs each one, then DELETEs the model
itself if we created it during this call. The wrapped error carries
`details.rollback = {attempted, posted_children, deleted,
failed_deletes, model_created_here, model_deleted, model_uuid}`.
Pass `--keep-on-failure` (mirrors `data-app create --keep-on-failure`)
to preserve the partial state for forensic inspection -- useful when
you want to inspect what got POSTed before the failure:

```bash
# Preserve everything on failure for inspection
kbagent --json semantic-layer build \
    --project prod \
    --tables out.c-revenue.fact_orders \
    --keep-on-failure \
    --name forensic_test_model
# On failure: model + N children remain; error.details.rollback shows
# {"attempted": false, "reason": "keep_on_failure", "posted_children": 3, ...}
# Tear down by hand when done: `kbagent semantic-layer remove ...` per
# child, then `kbagent semantic-layer model delete --yes`.

# Default: rollback runs automatically, no orphans left behind
kbagent --json semantic-layer build \
    --project prod \
    --tables out.c-revenue.fact_orders \
    --name production_model
# On failure: model + every successfully-POSTed child get DELETEd;
# error.details.rollback shows {"attempted": true, "deleted": 3, ...}
```

When you pass `--model EXISTING_NAME` (update mode), the model itself
is NEVER deleted on rollback -- only the children we POSTed during
this specific call get torn down.

---

## Workflow 8 -- Encrypt the token for a Python transformation that needs metastore access

Use case: a `keboola.python-transformation-v2` (or
`kds-team.app-custom-python`) container needs to call the metastore at
runtime (e.g. to look up the current constraint thresholds before
writing to `FACT_METRIC_VALUES`). The container's
`user_properties` block carries the Storage API token, but it must be
encrypted -- the same encryption flow as `data-app secrets-set`.

```bash
# 1. Encrypt the project's storage token for the target component
kbagent semantic-layer token \
    --encrypt \
    --project prod \
    --component-id keboola.python-transformation-v2
# Output (human mode):
#   Encrypted token for component keboola.python-transformation-v2
#   in project prod:
#   {
#     "#metastore_token": "KBC::ProjectSecure::..."
#   }
#   Paste the JSON above into the transformation's `user_properties` block.

# 2. Paste into user_properties via config update --set
kbagent semantic-layer token \
    --encrypt --project prod \
    --component-id keboola.python-transformation-v2 \
    | jq -r '.encrypted["#metastore_token"]' > /tmp/cipher.txt
CIPHER=$(cat /tmp/cipher.txt)
kbagent config update \
    --project prod \
    --component-id keboola.python-transformation-v2 \
    --config-id 12345 \
    --set "runtime.user_properties.#metastore_token=$CIPHER"

# 3. Inside the Python container, read it from env:
#   import os
#   token = os.environ["METASTORE_TOKEN"]   # auto-derived: '#' stripped, uppercased
#   # then call metastore.<stack>.keboola.com with X-StorageApi-Token: $token
```

`semantic-layer token --encrypt` is a thin wrapper around the existing
`encrypt values` flow -- it just builds the `{"#metastore_token":
<token>}` payload automatically from the project's stored Storage API
token (no config-file digging required). Other modes are refused with
`USAGE_ERROR` (exit 2); `--encrypt` is the only supported mode in v0.41.0.

---

## Reference: metastore contract gotchas

For the live-validated metastore contract surprises -- where the
`sl-builder` skill docs diverge from the actual API -- see:

- [gotchas.md > Semantic-layer constraint `rule` is a STRING, not an object](gotchas.md#semantic-layer-constraint-rule-is-a-string-not-an-object-since-v0340)
- [gotchas.md > Constraint name regex `^[a-z][a-z0-9_]*$` AND the 3-vs-4 severity split](gotchas.md#constraint-name-regex-a-za-z0-9_-and-the-3-vs-4-severity-split-since-v0340)
- [gotchas.md > Metric rename auto-cascades through `CODE_METRIC`](gotchas.md#metric-rename-auto-cascades-through-code_metric-since-v0340)
- [gotchas.md > Removing a metric corrupts `DIM_METRIC_THRESHOLD` downstream](gotchas.md#removing-a-metric-corrupts-dim_metric_threshold-downstream-since-v0340)
- [gotchas.md > `semantic-layer build` is a HEURISTIC fallback, not full AI](gotchas.md#semantic-layer-build-is-a-heuristic-fallback-not-full-ai-since-v0340)

Quick reminders:

- **POST envelope**: `{name, data, branch: "main", schemaVersion:
  "1.0.0", scope: "project"}` -> 201 with `{data: {type, id,
  attributes, meta}}`. kbagent handles this.
- **Duplicate-name POST -> 409 Conflict** (post go-monorepo PR #513) with
  `"Object with this name already exists in this project"`, or **500** with
  `"Failed to create meta object"` on legacy stacks. kbagent normalizes both
  into `ErrorCode.ALREADY_EXISTS` (since v0.43.5). 409 is non-retryable so
  the fix-deployed path avoids the `MAX_RETRIES` round-trips the 500 path
  still pays.
- **DELETE -> 204** empty body.
- **No PATCH endpoint** -- every "edit" is DELETE+POST. kbagent's
  `edit metric / dataset / relationship / constraint / glossary`
  (five sub-subcommands matching `add`) rolls back on POST failure.
  Same five types are available under `remove`.
- **`X-StorageApi-Token` is the only auth** -- no separate metastore
  token. `kbagent semantic-layer token --encrypt` encrypts the
  STORAGE token for a `user_properties` slot named `#metastore_token`,
  which is just a convention the Python container reads at runtime.
