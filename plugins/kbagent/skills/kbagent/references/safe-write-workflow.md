# Safe Config Write Workflow

## Why this exists

Two recurring incidents motivate this workflow:

1. **Stale-file overwrite.** Edit a config based on a local file from earlier
   in the session, push it back, and silently overwrite changes someone (or
   you) made in between. The classic case: `v106` was built from a local
   dump captured before `v105` landed, so the push of `v106` reverted the
   `v105` improvements.
2. **Blind push.** Apply a JSON edit without seeing the diff, discover only
   after the fact that you accidentally rewrote unrelated keys (storage input
   tables, parameters siblings, etc.). Without a `--dry-run` step, push is
   fire-and-forget.

The cycle below prevents both.

## The six-step cycle

Apply this for every write to a Keboola config, storage object, or branch.

### 1. Fetch fresh

Always re-read from the API right before you write. Never reuse a JSON dump
from a few commands back -- the version may already have moved.

```bash
kbagent --json config detail \
  --project prod \
  --component-id keboola.snowflake-transformation \
  --config-id 12345 > /tmp/config.fresh.json
```

If you fetched a config 10 minutes ago and want to write now, fetch again.
The cost of one extra GET is negligible compared to recovering from a
silently overwritten version.

### 2. Compute the change

Edit in memory (Python, `jq`) or in your head. Keep the change minimal: only
the fields you actually want to modify. Avoid sending the whole config
object back -- that maximizes the blast radius if any sibling key drifted.

For nested updates, prefer:
- `--set path=value` -- single key, shallow set
- `--configuration '{...}' --merge` -- partial JSON, deep-merged into existing

over:
- `--configuration '{...}'` (without `--merge`) -- replaces the entire
  configuration object with what you sent

### 3. Preview with `--dry-run`

```bash
kbagent --json config update \
  --project prod \
  --component-id keboola.snowflake-transformation \
  --config-id 12345 \
  --set "parameters.query=SELECT 1" \
  --dry-run
```

The dry-run output shows the diff between the current and proposed config
without applying it. Read it, render the relevant changes back to the user,
and pause.

### 4. Get user confirmation

Show the diff. Wait for an explicit "yes" before re-running the command
without `--dry-run`. This catches mistakes before they hit the API.

```bash
# After user confirms:
kbagent --json config update \
  --project prod \
  --component-id keboola.snowflake-transformation \
  --config-id 12345 \
  --set "parameters.query=SELECT 1"
```

### 5. Verify

Re-fetch the config and check the new version number and the field you
changed. Confirm to the user that the write landed.

```bash
kbagent --json config detail \
  --project prod \
  --component-id keboola.snowflake-transformation \
  --config-id 12345 | jq '{version, query: .configuration.parameters.query}'
```

### 6. Stop -- do not auto-run

Pushing a config and running the resulting transformation are always two
separate steps. Reasons:

- A previous job may still be running. Auto-triggering a second job stacks
  them and burns credits.
- The user may want to inspect the new config in the UI first.
- The user may want to schedule the run for off-hours.

Wait for an explicit user instruction before calling `job run` or any other
execution endpoint.

## Working from a git repository

By default, kbagent reads its config from `~/.config/keboola-agent-cli/`.
When you want a per-project setup (e.g. a git repo with its own Keboola
project list), initialize a local workspace:

```bash
cd /path/to/project
kbagent init                       # create empty .kbagent/ in current dir
# or
kbagent init --from-global         # copy ALL global projects into local .kbagent/
# or copy only specific project(s) (repeatable; implies --from-global):
kbagent init --project kosik-test  # copy just one project, skip the token re-entry
```

After that, kbagent automatically uses `.kbagent/` whenever you run from
this directory or any subdirectory. The lookup order is:

1. `--config-dir` flag
2. `KBAGENT_CONFIG_DIR` env var
3. `.kbagent/` in current or parent directories
4. `~/.config/keboola-agent-cli/` (global fallback)

This eliminates the "kbagent only works from home directory" friction --
once `.kbagent/` exists, you can run from any subdirectory of the repo.

## Anti-patterns

### Reusing a local file across edits

```bash
# BAD: fetched once at v105, edited and pushed multiple times
kbagent config detail ... > /tmp/config.json
# ... 10 minutes of editing ...
# meanwhile someone (or you) pushed v106
kbagent config update --configuration @/tmp/config.json
# Result: v107 is built from v105 + your edits, silently discarding v106.
```

```bash
# GOOD: re-fetch immediately before each push
kbagent config detail ... > /tmp/config.json   # fresh
# ... edit ...
kbagent config update --configuration @/tmp/config.json --merge --dry-run
# preview, confirm, then push without --dry-run
```

### Sending the whole configuration when changing one field

```bash
# BAD: replaces the entire .configuration with what you sent
kbagent config update ... --configuration "$(cat /tmp/full-config.json)"
# Result: any drift in storage.input.tables or parameters siblings is wiped.
```

```bash
# GOOD: targeted change, deep-merged
kbagent config update ... --set "parameters.query=SELECT 1"

# GOOD: partial JSON merged into existing
kbagent config update ... --configuration '{"parameters":{"query":"SELECT 1"}}' --merge

# GOOD: dedicated command for storage.output.default_bucket (preserves all siblings)
kbagent config set-default-bucket ... --bucket in.c-preferred-name
kbagent config set-default-bucket ... --clear
```

### Pushing then immediately running

```bash
# BAD
kbagent config update ...
kbagent job run ...   # do NOT chain
```

```bash
# GOOD: push, report to user, wait for "yes, run it"
kbagent config update ... --dry-run     # preview
kbagent config update ...               # after user confirms
# stop here; tell user the new version is in. Wait for explicit run instruction.
```

### Editing transformation SQL via full-config update

For SQL transformations, only update the specific `script` arrays inside
`parameters.blocks[N].codes[M]`, not the whole config. Sending the whole
config can wipe `storage.input.tables` / `storage.output.tables`. See
[sql-migration-workflow](sql-migration-workflow.md) for the safe pattern.
For targeted edits prefer `kbagent transformation edit` (`str_replace` and
8 other ops, applied against ids from a fresh `transformation show`).

## Multi-user / multi-session teams

When several people (or several Claude sessions) edit the same project:

- Always fetch fresh just before write -- the version may have advanced.
- Capture the `version` field from `config detail` and report it back to the
  user. If the version doesn't match what you expect (e.g. you fetched
  `v105` and now see `v107`), stop and re-evaluate before writing.
- For coordinated changes across multiple commits, prefer the
  [sync workflow](sync-workflow.md) (per-branch local checkout with
  explicit `pull` / `diff` / `push`) over ad-hoc `config update`.

## Deleting a configuration is reversible -- deleting it twice was not

`kbagent config delete` soft-deletes into the Storage trash and has
`--dry-run` *(since v0.89.0)*. Undo with `kbagent config restore`; list what is
restorable with `kbagent config trash-list`.

Never blind-retry a delete on an older kbagent: the raw Storage API PURGES
permanently when a DELETE lands on a configuration already in the trash, so on
0.88.0 and earlier a retry-after-timeout destroyed the configuration with its
versions, rows and metadata. Since 0.89.0 kbagent locates the configuration
first and answers `already_in_trash` (exit 0) instead of sending that second
DELETE. See [gotchas.md](gotchas.md).

## When dry-run is not available

A few destructive operations don't (yet) have `--dry-run`:

- some `branch` operations

For these, **describe the intended change to the user in plain English
first** and get confirmation before invoking. The verbal preview replaces
`--dry-run`. Better still: prefer a command that has `--dry-run` (e.g.
`kbagent config update`) over a raw REST write.

## Workspace table conflicts in transformations

When editing a SQL transformation that has multiple code blocks, also check
for workspace table name conflicts before pushing:

- Scan all `CREATE OR REPLACE TABLE "tmp.X"` statements across blocks.
- If the same `"tmp.X"` is created in two different blocks with different
  schemas, downstream blocks reading the original schema will fail at
  runtime with `invalid identifier '"column_name"'`.
- Fix: rename the secondary table (e.g. `"tmp.X2"`) and update its
  references within that block.

This isn't covered by `--dry-run` -- you have to spot it during step 2
(compute the change). See `gotchas.md` "Workspace table name conflicts" for
detail.

## Renaming SQL identifiers in transformations

When renaming a table inside a SQL transformation (e.g. `"orders"` →
`"objednavky"`), do NOT use global find & replace. The same name almost
always also appears as a **column** somewhere -- FK in `JOIN ON`,
aggregation alias in `SELECT`, `WHERE` condition. Global replace will
rename the column too and break the SQL silently.

Replace only in **table-reference positions**: after `FROM`, after `JOIN`,
in `CREATE ... TABLE` declarations. The same rule applies when migrating
to direct Snowflake paths or changing table prefixes.

See `gotchas.md` "SQL editing: do NOT use global text replace on
identifiers" for the full pattern, examples, and verification regexes.
