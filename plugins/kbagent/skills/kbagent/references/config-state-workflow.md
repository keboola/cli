# Config State Workflow -- Reading and Seeding Runtime State

`config state-get` / `config state-set` (since v0.84.2, #593) read and write a
configuration's runtime `state` -- the checkpoint dict incremental components
persist between jobs (last sync cursors, `lastImportId`, OAuth intermediate
data). This closes the gap where the only write path was the Keboola UI
(`/raw` -> *Update State* tab), and where `config update --set 'state...'`
looked successful but silently no-oped (see
[gotchas](gotchas.md#config-update---set-state-is-now-a-hard-error-not-a-silent-no-op-since-v0842)).

## When to use this

- **Backfill / replay**: reprocess from a chosen checkpoint after a
  downstream bug, without re-importing everything.
- **Reset after a bad run**: a component wrote a corrupt checkpoint and keeps
  skipping data until state is corrected.
- **Seeding a dev branch**: `branch create` always starts with `state: {}`
  (a fresh runtime state, not a copy of production's). Testing incremental
  behaviour on a branch at all requires seeding.
- **Migrating `processed_tags`/`query` -> `changed_since: adaptive`** in file
  input mapping (Keboola is retiring the former; see the [changelog
  announcement](https://changelog.keboola.com/deprecating-processed-tags-and-query-in-file-input-mapping/)).
  This is the case that forced #593 -- see the dedicated section below.

## Quick reference

| Command | Purpose | Permission |
|---------|---------|------------|
| `config state-get` | Read root or row state | read |
| `config state-set` | Write root or row state (guarded) | write |

```bash
kbagent --json config state-get --project ALIAS --component-id keboola.python-transformation-v2 --config-id 25344315
kbagent --json config state-get --project ALIAS --component-id keboola.python-transformation-v2 --config-id 25344315 --row-id row1

kbagent --json config state-set --project ALIAS --component-id keboola.python-transformation-v2 --config-id 25344315 \
  --state '{"storage": {"input": {"files": [{"tags": ["import"], "lastImportId": "176200172"}]}}}' \
  --branch 1234 --dry-run     # preview first, then re-run without --dry-run
```

## Root vs row state

- **Root state** (`state-get`/`state-set` without `--row-id`) is the
  configuration-level checkpoint. This is what `config detail --with-state`
  shows as the top-level `state` key.
- **Row state** (`--row-id ROW_ID`) is that row's own checkpoint, embedded in
  the row object under `config detail`'s `rows[]` array.
- **For row-based components the root state node is unused** -- if your
  configuration has rows (per-table extractors, per-endpoint writers, ...),
  read/write the *row* state, not the root. Writing to the root on a
  row-based config is a no-op for the component's actual behaviour, even
  though the write itself succeeds.
- A missing/typo'd `--row-id` fails loudly (named in the error), it does not
  silently return or write an empty `{}` -- that would repeat the exact class
  of bug #593 is about.

## Migration playbook: `processed_tags`/`query` -> `changed_since: adaptive`

The API-verified trap: `changed_since: adaptive` with an **empty** state does
not mean "start watching from now" -- it triggers a full reload of the
component's entire file history (see
[gotchas](gotchas.md#changed_since-adaptive-with-empty-state-reloads-the-entire-file-history-since-v0842)).
Since a dev branch always starts with `state: {}`, validating this migration
on a branch -- the safe place to test it -- reproduces the full reload every
time unless you seed state first. This is the one manual step (issue #593)
that used to require the Keboola UI; it is now fully scriptable.

1. **Create an isolated branch** (see
   [branch-workflow](branch-workflow.md)):

   ```bash
   kbagent --json branch create --project ALIAS --name "migrate-adaptive-25344315"
   ```

   This auto-activates the branch; subsequent commands default to it. Note
   its `branch_id` for the explicit `--branch` flags below (state and job
   commands accept it explicitly too, which is clearer in scripts).

2. **Edit the file input mapping** to drop `processed_tags`/`query` and add
   `"changedSince": "adaptive"`. Fetch fresh, change only the mapping, push
   back (`config update`) or edit locally and `sync push` if the project is
   under GitOps sync -- see [sync-workflow](sync-workflow.md) and the
   [safe-write-workflow](safe-write-workflow.md) (fetch -> dry-run -> confirm
   -> push).

3. **Find the checkpoint to seed** (see below), then seed the branch's state
   *before* the first run:

   ```bash
   kbagent --json config state-set --project ALIAS --component-id keboola.python-transformation-v2 \
     --config-id 25344315 --branch <branch_id> \
     --state '{"storage": {"input": {"files": [{"tags": ["import"], "lastImportId": "<checkpoint>"}]}}}' \
     --dry-run   # verify the diff, then drop --dry-run to apply
   ```

   Seed root or row state depending on whether the config uses rows (see
   above). This is the safer direction: an empty state is what triggers the
   full reload, a seeded checkpoint is what avoids it.

4. **Run the job on the branch** and let it complete:

   ```bash
   kbagent --json job run --project ALIAS --component-id keboola.python-transformation-v2 \
     --config-id 25344315 --branch <branch_id> --wait
   ```

   Watch the run duration and file count -- a seeded checkpoint should
   produce a run comparable to a normal incremental run (seconds, only new
   files), not a full-history reload (minutes, thousands of files).

5. **Verify the state advanced**:

   ```bash
   kbagent --json config state-get --project ALIAS --component-id keboola.python-transformation-v2 \
     --config-id 25344315 --branch <branch_id>
   ```

   `lastImportId` (or the equivalent cursor) should have moved past your
   seeded checkpoint to the newest file processed.

6. **Merge when satisfied.** `branch merge` returns a Keboola UI URL for
   manual review -- it does not merge automatically:

   ```bash
   kbagent --json branch merge --project ALIAS
   ```

   Branch merge propagates **configs only, not state** -- production's state
   is untouched by the merge, so repeat the seed step (3) against production
   with production's own checkpoint before the first production run under
   the new mapping, if production also starts from a cleared/uncertain
   state. If production already has a healthy `state` under the old mapping
   shape, verify with `state-get` whether it needs reshaping for `adaptive`
   before relying on it as-is.

## Finding the right checkpoint

`lastImportId` (or your component's equivalent cursor field) should point at
a file the component has already durably processed, so the next run picks up
only what comes after it. Two common sources:

- **An already-healthy production state**: `config state-get` on the
  production config (if one exists under the old mapping) may already carry
  a comparable checkpoint you can carry over.
- **The Storage Files listing**: `kbagent --json storage files --project ALIAS --tag TAG --limit N`
  lists files with their ids and creation times sorted for inspection --
  pick the id of the most recent file you know is already fully imported
  downstream, and seed with that.

Never guess a future or non-existent id "to be safe" -- an id past the true
checkpoint skips real files; use `--dry-run` and `state-get` to confirm
before committing to a value on a config that matters.

## State document shape for file input mapping

The shape mirrors the file input mapping's own `tags` selection criteria:
the state you write should describe the same tag set the mapping filters on,
and `tags` is an array, not a single string.

**Treat the example below as structure, not as authoritative types.** Issue
#593 verified the `lastImportId` checkpoint field itself, but a component's
state document is component-defined -- whether an id serializes as a string
or a number is not something to infer from a doc example. The reliable move
is always the same: `state-get` an already-migrated config in the same
project, and mirror exactly what comes back. Seeding a wrong shape does not
error; it silently behaves like an empty state, which for `adaptive` means
the full-history reload this whole workflow exists to avoid.

```json
{
  "storage": {
    "input": {
      "files": [
        {
          "tags": ["import"],
          "lastImportId": "176200172"
        }
      ]
    }
  }
}
```

If your input mapping selects files by more than one tag set (multiple
entries under `storage.input.files` in the configuration itself), the state
document needs one matching entry per selection. When unsure, `state-get` a
comparable already-migrated config in the same project as ground truth
before writing.

## When to use `--dry-run`

Always, the first time, on any config that matters. `--dry-run` computes the
current-vs-new diff without writing (same shape as `config update --dry-run`)
and never prompts. A no-op write (new state identical to current) is
detected and skipped even without `--dry-run` (`changed: false`, no API
call) -- so re-running the seed step is safe and idempotent.

`state-set` without `--dry-run` is a guarded write: it prompts for
interactive confirmation unless `--yes` is passed or the command runs under
`--json` (consistent with the rest of the `config` group -- `--json` skips
the prompt without requiring `--yes`, since this is a `write`, not
`destructive`, operation).

## Anti-patterns

- **`config update --set 'state.x=y'`** -- rejected since v0.84.2 (exit 2);
  use `state-set` instead. See [gotchas](gotchas.md) for the full history.
- **Seeding with an empty `{}`** to "reset and be safe" on an `adaptive`
  file input mapping -- this is the *expensive* direction for this specific
  mapping type (full reload), the inverse of the usual "clear state = safe
  reset" assumption for other incremental strategies.
- **Merging a branch and assuming production's state came along** -- branch
  merge propagates configs, not runtime state; seed production separately.
- **Writing root state on a row-based component** -- succeeds but has no
  effect on the component's actual incremental behaviour; use `--row-id`.
