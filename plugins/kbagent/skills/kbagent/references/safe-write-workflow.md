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

Wait for an explicit user instruction before calling `job run`, `flow run` or
any other execution endpoint. For `flow run`, show the `--dry-run` selection and
its warnings first ([flow-workflow](flow-workflow.md#run-a-flow)).

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

### After a token rotation: `INVALID_TOKEN` in a local `.kbagent/`

*(since vNEXT)* (#955) `init --from-global` copies the tokens **once**; the
local directory is not linked to the global config. When the old tokens stop
working (expired, or revoked after the user minted new ones in the global
config), commands in the local directory that call Keboola report
`INVALID_TOKEN` (exit 3; `project status` shows an error row and exits 1),
while the same project works from `~`. Fix it with
`project refresh` in the local directory, which first takes the global
config's working token:

1. **Preview**, keeping the same target and working directory:
   `kbagent --json project refresh --project ALIAS --dry-run` (or
   `--all-projects`). It never asks for a Manage token. Read every list:
   - `data.projects_adopted[]`, `action: would_adopt_from_global`: the global
     config holds a working token for it (`global_alias` names the entry).
   - `data.projects_refreshed[]`, `action: would_refresh`: a NEW token would
     be minted, which needs the Manage token. This does not prove the global
     config has no working token: adoption also needs the local check to say
     `INVALID_TOKEN` (a timeout, 5xx or `AUTH_REJECTED` skips it) and exactly
     one distinct global static token for the same project and stack, other
     than the local one. Retry a transient error first.
   - `data.projects_valid[]` (left alone), `data.projects_skipped[]` (session
     or no project ID), `data.projects_failed[]` (check failed; exit 1, also
     in a dry-run).
2. **Tell the user what will happen and get a yes** (it rewrites the local
   config; `--json` skips the CLI's own confirmation): which aliases take the
   global token, and that the taken token is whatever the global config holds
   for the project, possibly with a broader scope than the dead one (a
   restricted read-only token can become an admin token).
3. **Run it** with the same selector:
   `kbagent --json project refresh --project ALIAS`. When only adoptions are
   needed it succeeds without a Manage token. When something must be minted
   it asks for the Manage token on a terminal (`--json` does not suppress that
   prompt); without a terminal it exits 2 and writes **nothing**, the
   adoptions included. Then ask the user to run
   `kbagent project refresh --project ALIAS` (or `--all-projects`) themselves
   in a terminal, from the same directory. Never ask for the Manage token in
   chat, never pass `--allow-env-manage-token` on your own. Exit 1 means some
   project landed in `data.projects_failed`; only the entries in
   `projects_adopted` and `projects_refreshed` were written.
4. **Verify**: `kbagent --json project status --project ALIAS` returns
   `data[].status == "ok"`.

Do not:
- **Edit `.kbagent/config.json` or copy tokens by hand.** Never read either
  config file; `project refresh` moves the token without printing it.
- **Pass `--force`** to "make it work": `--force` never takes the global
  token, always mints new ones (Manage token needed) and also replaces
  tokens that still work.
- **Delete `.kbagent/` and re-run `init`.** That loses the active project,
  the active branch, the permission policy and local-only aliases.

Notes:
- Only adoption from the global config is limited to a token the API rejects
  as `INVALID_TOKEN`; a valid local token is never touched. Another API error
  during the check (timeout, 5xx, `AUTH_REJECTED`) falls back to minting; a
  non-API failure lands the project in `projects_failed`.
- `projects_failed` with "changed concurrently": another process edited the
  alias during the run; run the command again. If the message says a new
  Storage token was created but not saved, give the user its ID so they can
  delete it (Keboola UI or `token delete --token-id ID`) before rerunning.
- A read-only directory (`init --read-only`, `deny cli:write`) refuses
  `project refresh`, the dry-run included (exit 6): the user must lift the
  policy themselves.
- Browser-login (session) projects are skipped; their remedy is
  `kbagent auth login`, not `project refresh`.
- `--token-description` / `--token-expires-in` apply to minted tokens only.
- Minting a token in the global config does not revoke the old one: a local
  directory needs this only once its own copy stops working.

## Anti-patterns

### Reusing a local file across edits

```bash
# BAD: fetched once at v105, edited and pushed multiple times
kbagent config detail ... > /tmp/config.json
# ... 10 minutes of editing ...
# meanwhile someone (or you) pushed v106
kbagent config update --project prod --configuration-file /tmp/config.json
# Result: v107 is built from v105 + your edits, silently discarding v106.
```

```bash
# GOOD: re-fetch immediately before each push
kbagent config detail ... > /tmp/config.json   # fresh
# ... edit ...
kbagent config update --project prod --configuration-file /tmp/config.json --merge --dry-run
# preview, confirm, then push without --dry-run
```

### Sending the whole configuration when changing one field

```bash
# BAD: replaces the entire .configuration with what you sent
kbagent config update --project prod ... --configuration "$(cat /tmp/full-config.json)"
# Result: any drift in storage.input.tables or parameters siblings is wiped.
```

```bash
# GOOD: targeted change, deep-merged
kbagent config update --project prod ... --set "parameters.query=SELECT 1"

# GOOD: partial JSON merged into existing
kbagent config update --project prod ... --configuration '{"parameters":{"query":"SELECT 1"}}' --merge

# GOOD: dedicated command for storage.output.default_bucket (preserves all siblings)
kbagent config set-default-bucket --project prod ... --bucket in.c-preferred-name
kbagent config set-default-bucket --project prod ... --clear
```

### Pushing then immediately running

```bash
# BAD
kbagent config update ...
kbagent job run ...   # do NOT chain
```

```bash
# GOOD: push, report to user, wait for "yes, run it"
kbagent config update --project prod ... --dry-run     # preview
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

Many writes have no `--dry-run`. Check `--help` before you rely on it.
Destructive commands without `--dry-run` include:

- `branch delete`, `branch metadata-delete`
- `config row-delete`, `config delete-metadata`, `config variables-clear`
- `workspace delete`, `data-app delete`, `notification delete`, `token delete`
- `http delete`
- the `merge-request` transitions (`request-review`, `approve`, `merge`, ...)

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

## Unattended deploy pipelines

In CI there is no human for step 4. Replace it with checks the pipeline
enforces: parse every `_config.yml`, review `sync push --dry-run`, and never
pass `--force` without that review. See
[gitops-cicd-workflow.md](gitops-cicd-workflow.md).
