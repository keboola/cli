# Sync Workflow -- GitOps for Keboola Configurations

Sync lets you manage Keboola configurations as local files with full git integration.

## Variable values deployment (since v0.21.0)

`sync push` now deploys **config rows**, not just parent configs. This unlocks the
most common GitOps use case: deploying `keboola.variables` values through git.

```bash
# 1. Pull a project that contains a keboola.variables config + values row.
kbagent sync pull --project prod

# 2. Edit the row YAML directly. For keboola.variables and keboola.shared-code
#    rows, the configuration keys (`values`, `code_content`, etc.) are hoisted
#    to the top level of the YAML so edits are natural.
cat > main/variables/<name>/values/main/_config.yml <<EOF
version: 2
name: main
description: ""
values:
  - {name: year_start, value: "2025", type: string}
  - {name: region, value: "us-west", type: string}
_keboola:
  component_id: keboola.variables
  row_id: <row_id>
EOF

# 3. Push -- the row is written to
#    PUT /v2/storage/components/keboola.variables/configs/{id}/rows/{rowId}
#    with the configuration set to {"values": [...]} verbatim.
kbagent sync push --project prod
```

`#`-prefixed secret keys inside row YAMLs are encrypted via the Encryption API
before push, same as parent configs. Encryption failure aborts the push
(fail-closed); use `--allow-plaintext-on-encrypt-failure` only for debugging.

For the row-level internals (manifest v3, per-row hashes, hoisted payloads,
untracked row detection, secret encryption contract), see
[`sync-rows-workflow.md`](./sync-rows-workflow.md). For the ergonomic
alternative that skips the YAML round-trip entirely, see
[`variables-workflow.md`](./variables-workflow.md).

## All-projects workflow (recommended)

```bash
# Download all configured projects in one command
# Includes: configs, storage metadata (buckets/tables), per-config job history
mkdir keboola && cd keboola
kbagent sync pull --all-projects

# Include data samples (CSV previews from largest tables)
kbagent sync pull --all-projects --with-samples

# Customize what gets pulled
kbagent sync pull --all-projects --job-limit 10        # more job history per config
kbagent sync pull --all-projects --no-storage --no-jobs # configs only (faster)

# Check status across all projects (compact one-liner per project)
kbagent sync diff --all-projects

# Push changes from all projects
kbagent sync push --all-projects --dry-run   # preview
kbagent sync push --all-projects             # apply
```

Each project gets its own subdirectory (named by alias). Projects are processed in parallel.

## Reconciling a drifted tree with production (since v0.72.0)

When production was edited directly (by people or other tooling) and the local
tree no longer matches, `--theirs` is the supported reset -- never hand-edit
`.keboola/manifest.json`:

```bash
# Audit drift first (3-way: local vs pull-time snapshot vs remote)
kbagent sync diff --project prod

# Remote wins everywhere: overwrites locally-modified configs/rows, restores
# deleted/missing files, resolves true merge conflicts by taking remote.
kbagent sync pull --project prod --theirs

# Verify: must report "No differences found"
kbagent sync diff --project prod
```

Related semantics (all since v0.72.0):

- Plain `sync pull` re-materializes a tracked config whose local dir was
  deleted (delete-dir-then-pull refetches; delete-dir-then-PUSH still deletes
  the remote config -- the direction of the command picks the winner).
- Config-level enabled/disabled state round-trips: `_config.yml` carries
  `is_disabled: true` for disabled configs (absent = enabled), `sync diff`
  shows the drift, push updates the remote state when the key is present.
- A manifest entry that was registered but never materialized (empty
  `pull_hash`, no files -- pre-0.72 name-collision phantom) is excluded from
  push delete-planning and reported as `never_fetched`; pull heals it.
- `sync status` is a LOCAL-only check (says so explicitly) -- use `sync diff`
  for a local-vs-production audit.

## Per-invocation dev-branch override (since v0.47.0)

`sync push`, `sync pull`, and `sync diff` accept `--branch <id>` to target a
dev branch for a single invocation. The override beats every other branch
source: `manifest.branches[0]`, the project's `active_branch_id` (`branch use`),
and the git-branching `branch-mapping.json`.

```bash
# Push the current working tree to dev branch 388072 without `branch use`
# or `sync branch-link` first. Required exactly one --project.
kbagent sync push --project prod --branch 388072

# Same dev branch on pull (`sync diff` accepts it too).
kbagent sync pull --project prod --branch 388072
kbagent sync diff --project prod --branch 388072
```

Use cases:
- Spin up a throwaway dev branch via `kbagent branch create`, push a
  candidate change to it for testing, then `kbagent branch delete` to clean
  up — all without touching the persisted active-branch state.
- Scripted / scheduled flows where the branch id comes from an upstream
  job (e.g. a CI pipeline computes the branch id and passes it via env).

Mutually exclusive with `--all-projects` at the CLI layer (branch id is
per-project; the validator returns exit 2 + `USAGE_ERROR` if combined).
The override is per-invocation only — it does not write into the manifest
or the config store, so a subsequent command without `--branch` falls back
to the normal priority chain.

**Promote the default tree to a target branch (since v0.47.2).** When the
target branch has no materialized `<branch_name>/` subtree on disk, `sync push
--branch <id>` reads the **default tree** (`main/`) as the source and promotes
it to the target branch, instead of failing with `Config file not found`.
Source (read) and target (API write) are decoupled; API calls still target the
branch id. So the common "I only have `main/` locally, push it to a fresh dev
branch" flow now works with a single command:

```bash
# main/ on disk, no feature-x/ subtree -> main/ is promoted to branch 388072
kbagent sync push --project prod --branch 388072
```

When a per-branch subtree *does* exist (multi-branch-directory users), the
target subtree is used as before — behaviour is unchanged.

## Switching back to production after a `--branch` pull (since v0.89.0)

`sync pull --branch <dev>` does two things: it materializes the
`<branch_name>/` subtree **and** it re-targets every `manifest.configurations`
entry to that dev branch. The `main/` tree stays on disk but is no longer
tracked — it is an orphan of the branch the manifest left behind.

So after `branch reset` (or any production `sync diff` / `sync push`), the
manifest and the tree you are diffing disagree. kbagent detects that and reports
it instead of acting on it:

```bash
kbagent sync pull --project prod --branch 51406   # manifest now tracks 51406
kbagent branch reset --project prod
kbagent sync diff --project prod                  # target: production
# Warning: 17 config(s) belong to a different branch than the one being synced.
#   They are excluded from the diff and from push planning -- run
#   'kbagent sync pull' to re-target the manifest to this branch, or remove
#   the stale directory
```

- Configs whose id **does** exist on the target branch are still diffed, from
  the file in the target's own tree (`main/`) — so an ordinary production diff
  after a dev pull reports `unchanged`, not a create.
- Configs that exist **only** on the dev branch are excluded entirely. Promote
  them with `kbagent branch merge`, never by pushing them to production.
- `--json` carries the full list under `orphaned` (`component_id`, `config_id`,
  `path`, `branch_id`, `branch_path`, `exists_on_target`, `reason`, `hint`) plus
  a `summary.orphaned` counter; human mode previews the first 10.
- The fix: `kbagent sync pull --project prod` re-targets the manifest back to
  production. Then diff/push behave normally and the warning disappears.

Before this landed, that production diff classified the whole orphaned `main/`
tree as `added` with an empty `config_id`, and `sync push` would have created a
duplicate of every production config (issue #649).

## Fresh-CREATE writeback (since v0.47.0)

If you (or a tool like FIIA) seed `.keboola/manifest.json` with placeholder
entries before the first `sync push`, the writeback updates each placeholder
**in place** rather than appending a new entry. Pre-v0.47.0 this produced
manifests of length 2N after one push (placeholders + new entries both
retained); from v0.47.0 the manifest stays at length N and the placeholder
entry's id is updated to the API-assigned ULID. Re-pushes against the
now-real id are naturally idempotent.

If a placeholder entry's `metadata` dict contains `KBC.configuration.*`
keys (e.g. `KBC.configuration.folderName`), they are propagated to the
metadata API immediately after the create call. This was the previous
"set folderName via `config set-folder` after push" workaround for
fresh-create flows; from v0.47.0 a single push handles it.

**Variable links are resolved on fresh CREATE (since v0.47.2).** When the
placeholder tree includes a `keboola.variables` config + default-values row and
a transformation that cross-references them, one `sync push` now produces a
*runnable* transformation:

- the row's top-level `values: [...]` reach the API body even when the scaffold
  row file has no `_keboola` block (KFR-04);
- a row whose parent `keboola.variables` config is created in the same push is
  POSTed against the assigned ULID, not the placeholder (KFR-05, previously
  `PARENT_CONFIG_NOT_TRACKED`);
- the transformation's `configuration.variables_id` / `variables_values_id` are
  rebound from placeholders to the assigned ULIDs via a post-create
  `update_config` PUT (KFR-03, previously `job run` failed with `Variable
  configuration "<placeholder>" not found`).

This removes the need for a post-push `config variables-set` step. If a
placeholder can't be matched and the binding is ambiguous (zero or >1
`keboola.variables` configs created this push), the link is left untouched and a
`variable_link` entry appears in the push `errors` array — never a silently
broken link. A clean re-push reports `no_changes`.

`sync push --no-name-drift-warnings` (since v0.47.0) suppresses the
cosmetic `name_drift_warnings` array on the result envelope. The
detection still runs; only the report is dropped. Useful for downstream
tools that already audit drift their own way (e.g. FIIA's
`var-07-fi-daily-date-refresh` pattern legitimately differs from the
canonical kbagent naming and the warnings are noise).

## Adopting an existing kbc Go CLI checkout (since v0.22.0)

If you already have a `.keboola/manifest.json` produced by the official
`kbc` Go CLI (keboola-as-code), `kbagent` can adopt it in place instead of
overwriting:

```bash
cd /path/to/existing-kbc-checkout

# Adopt the manifest as-is; validates project_id against the alias token
kbagent sync init --project prod --adopt-existing
```

Behavior:

- **Idempotent.** The existing `manifest.json` is re-used, not rewritten.
  Re-running `--adopt-existing` is a no-op.
- **Validated.** `project_id` from the manifest is checked against the
  token's project via `verify_token`. A mismatch exits 5 (`CONFIG_ERROR`)
  with a clear message -- no silent adoption of someone else's checkout.
- **Fall-through.** If no manifest exists, `--adopt-existing` falls
  through to the normal init path.
- **Without the flag**, `sync init` still refuses to overwrite an
  existing manifest (prior behavior unchanged).

Use this when migrating a team from the Go CLI to kbagent without
re-pulling all configs.

## Single-project workflow

```bash
# Pull auto-inits if no manifest exists
kbagent --json sync pull --project prod

# Edit locally -- configs are in _config.yml, description in _description.md,
# SQL in transform.sql, Python in code.py
# Use any IDE, get git diffs, code review, etc.

# Review changes
kbagent --json sync status                         # what changed locally
kbagent --json sync diff --project prod            # 3-way diff vs remote

# Push
kbagent --json sync push --project prod --dry-run  # preview
kbagent --json sync push --project prod            # apply
```

## File format

Every config directory contains:

| File | Purpose |
|------|---------|
| `_config.yml` | YAML config (name, parameters, storage) |
| `_description.md` | Description as readable Markdown (always separate) |
| `_jobs.jsonl` | Recent jobs for this config (JSONL: id, status, timing, errors) |

Depending on component type, additional files are extracted:

| Component type | Extra files |
|---------------|-------------|
| Snowflake transformation | `transform.sql` (SQL with `/* ===== BLOCK: ... ===== */` markers) |
| Python transformation | `transform.py` + `pyproject.toml` (dependencies) |
| Custom Python app | `code.py` + `pyproject.toml` |
| Flow/orchestrator | phases, tasks, schedules inline in `_config.yml` |

Storage metadata is also pulled (read-only, not tracked in manifest):

| Path | Purpose |
|------|---------|
| `storage/buckets.json` | All buckets with metadata |
| `storage/tables/{bucket}/{table}.json` | Per-table schema, columns, row count, size |
| `storage/samples/{bucket}/{table}/sample.csv` | Data samples (opt-in: `--with-samples`) |

## Branch use workflow (simple dev branches)

Use `branch use` to work with dev branches without git-branching setup.
Each branch gets its own directory on disk.

```bash
# Pull production first
kbagent sync pull --project prod --force

# Create a dev branch (auto-activates)
kbagent --json branch create --project prod --name "fix-etl"
# -> Branch 'fix-etl' activated

# Pull the dev branch -- configs go into a separate directory
kbagent sync pull --project prod --force
# -> Pulled 42 configurations into fix-etl/

# Edit configs in fix-etl/, push changes
kbagent sync push --project prod

# When done, merge and switch back
kbagent --json branch merge --project prod   # returns merge URL
kbagent sync pull --project prod --force      # refresh main/
```

### How it works

- `sync pull` detects the active branch and auto-registers it in `manifest.json`
- Each dev branch gets a sanitized directory name (e.g. branch "My Feature" -> `my-feature/`)
- The manifest tracks which branch each config belongs to
- Switching branches (`branch use` / `branch reset`) changes where pull writes and push reads
- `sync diff` and `sync push` also respect the active branch
- Each pull re-targets `manifest.configurations` to the branch it pulled, so
  after switching back you must `sync pull` again before diff/push is meaningful
  — see "Switching back to production after a `--branch` pull" above

### Directory structure

```
project-root/
  .keboola/manifest.json   # branches: [{id: 123, path: "main"}, {id: 456, path: "fix-etl"}]
  main/                     # production configs
    extractor/...
    transformation/...
  fix-etl/                  # dev branch configs (separate directory)
    extractor/...
    transformation/...
```

## Git-branching workflow (recommended for teams)

Maps git branches to Keboola dev branches for safe parallel development.

```bash
# Initialize with git-branching
git init
kbagent --json sync init --project prod --git-branching
kbagent --json sync pull --project prod
git add -A && git commit -m "initial sync"

# Create feature branch
git checkout -b feature/new-etl
kbagent --json sync branch-link --project prod
# -> Creates Keboola dev branch "feature/new-etl"
# -> All sync commands now auto-target this dev branch

# Work on the feature branch
# Edit _config.yml, transform.sql, etc.
kbagent --json sync diff --project prod     # compares vs dev branch
kbagent --json sync push --project prod     # pushes to dev branch ONLY

# Production is NEVER touched from feature branches
# Unlinked branches are BLOCKED from sync operations
```

### Branch mapping

Stored in `.keboola/branch-mapping.json`:

```json
{
  "mappings": {
    "main": {"id": null, "name": "Main"},
    "feature/new-etl": {"id": "123456", "name": "feature/new-etl"}
  }
}
```

- `id: null` = production (default branch)
- `id: "123456"` = Keboola dev branch
- Sync commands auto-resolve the target branch from the current git branch

### Merge back to production

1. Merge in Keboola UI: `kbagent branch merge --project prod` (returns URL)
2. Git merge: `git checkout main && git merge feature/new-etl`
3. Sync merged state: `kbagent --json sync pull --project prod`
4. Cleanup: `kbagent sync branch-unlink` + delete git branch

## 3-way diff

`sync diff` uses a 3-way comparison (local vs pull-time base vs remote):

| Change type | Meaning | Action |
|------------|---------|--------|
| MODIFIED | Local changed, remote unchanged | Safe to push |
| REMOTE MODIFIED | Remote changed, local unchanged | Run pull to fetch |
| CONFLICT | Both sides changed | Resolve manually, then push |
| ADDED | New local config | Push creates it |
| DELETED | Local file removed | Push deletes from remote |

## Key behaviors

- **Pull is idempotent**: re-running pull when nothing changed writes zero files
- **Pull protects local edits**: locally-modified files are skipped by default
- **`--force` is conflict-aware (since 0.53.0)**: see below -- it no longer blindly overwrites
- **Push only sends local changes**: remote_modified and conflict changes are skipped
- **Encrypted values**: nonce differences are ignored in diff (no false positives)
- **New configs**: push auto-assigns IDs from the API, updates manifest
- **Storage metadata is read-only**: not tracked in manifest, excluded from diff/push
- **Jobs are per-config**: `_jobs.jsonl` shows recent N jobs (default 5) with status + timing
- **Data samples auto-trim**: tables with >30 columns export only first 30 (API sync limit)
- **Encrypted columns masked**: columns starting with `#` show `***ENCRYPTED***` in samples

## Ignored components

*(since vNEXT)* Some components are excluded from every sync operation (`pull`, `diff`,
`push`) because they are managed through separate APIs and have volatile
internal state:

- **Always ignored** -- `keboola.sandboxes` (Workspaces API) and
  `keboola.mcp-server-tool` (the Keboola MCP server auto-creates one empty
  workspace-record config per project it touches, `configuration: {}`, name
  like `mcp-workspace-<hex>`). This is a hardcoded floor; no flag disables it.
- **Project-configurable** -- the manifest field `ignoredComponents` in
  `.keboola/manifest.json` adds project-specific exclusions on top of the
  hardcoded list, without waiting for an upstream kbagent release:

  ```json
  {
    "ignoredComponents": ["keboola.some-noisy-component"]
  }
  ```

  The two sets are **unioned**: a component is ignored if it is hardcoded OR
  listed in `ignoredComponents`.

- **Pull cleanup.** When a component becomes ignored (either newly hardcoded
  by a kbagent upgrade, or newly added to `ignoredComponents`), the next
  `sync pull` drops its manifest entries and removes their local directories.
  Those removals are reported with pull action `"ignored"` -- distinct from
  `"removed"`, which means the config was genuinely deleted on the remote.
- **Diff/push never see ignored configs**, on either side of the comparison:
  a stale local directory for an ignored component is filtered out too, so it
  can never be classified as `DELETED` and pushed as a remote deletion.
- **Un-ignoring** a component: remove it from `ignoredComponents` and run
  `sync pull` again -- it re-materializes like any newly-tracked config.

## `sync pull --force` is conflict-aware (since 0.53.0)

`--force` no longer blindly overwrites locally-modified configs. It branches on
the 3-way diff state per config (and per row):

- **Local edited, remote UNCHANGED** -> the file and its sync baseline are
  **preserved**. The pending delta stays visible to `sync diff` / `sync push`.
  `--force` does **not** discard the edit and does **not** silently re-stamp the
  baseline (that was the pre-0.53.0 data-loss bug).
- **Local edited AND remote also changed** since the last pull (a true merge
  conflict) -> the pull **aborts before writing anything** with exit code 1 and
  error code `SYNC_CONFLICT`, listing every conflicting config/row. Resolve with
  `sync diff`, then `sync push` your edits (or discard them), then pull again.
- **Local untouched, remote changed** -> `--force` takes remote as before.

> Safe to run `sync pull --force` to refresh an unrelated config even while you
> have un-pushed edits elsewhere: non-conflicting edits survive; a real conflict
> stops you loudly instead of losing work. To intentionally drop a local edit,
> delete the file (or the config directory) and pull.

## Cloning a reference project (`sync clone`, since v0.63.0)

`sync clone` builds a new customer/instance project by **copying a golden
reference tree and parameterizing it**, instead of re-implementing the copy +
id-rewrite + push surgery by hand.

```bash
kbagent sync clone \
  --source ./golden-reference \
  --target new-customer-alias \
  --target-dir ./clones/new-customer \
  --bucket-map ./overrides/buckets.json \
  --variable-values ./overrides/vars.json \
  --instance-rename ./overrides/rename.json
```

Override files are JSON or YAML objects:
- `--bucket-map` → `{ "in.c-ref": "in.c-prod", "out.c-ref": "out.c-prod" }` — rewrites the bucket prefix of every storage input `source` / output `destination`.
- `--variable-values` → `{ "db_host": "prod-db", "api_base": "https://..." }` — overrides matching `keboola.variables` row values.
- `--instance-rename` → `{ "extractor/keboola.ex-db/Acme": "extractor/keboola.ex-db/Globex" }` — renames config dirs + manifest paths.

All three files must be **flat `{id: scalar}` mappings** (since v0.89.0) — a
nested mapping, list, or empty (`null`) value is rejected with `CONFIG_ERROR`
(exit 5) naming the offending key and its actual type, instead of being
silently stringified into a bogus ID.

**Why it just works on a fresh target:** the reference's config ids do not exist
in the target project, so the push diff classifies every config as `added` and
assigns new ULIDs. Because the push's `created_id_map` is keyed by the reference
id, the **Phase-C** transformation variable links **and the Phase-D
`keboola.flow` task `configId`s** remap reference→ULID automatically — no manual
"remap orchestrator task" pass. The push result carries `flow_task_remaps`.

**Idempotent:** re-running with an existing `--target-dir` skips the copy +
overrides and just pushes, so a completed clone reports `no_changes` /
`created: 0`. Re-running is safe.

**Fresh-target requirement:** clone refuses (`CONFIG_ERROR`) if the target
project already contains the reference's configs — it will not UPDATE a
stranger's config. Use a new/empty target project.

**In-process SDK:** `SyncService.clone_project(source, target_alias, target_dir,
overrides={...})` returns a typed `CloneResult` (exported from
`keboola_agent_cli`). This is the tested SDK surface a scaffold/provisioning tool
should call instead of hand-rolling the mechanics.

> **`--dry-run` still writes to disk.** A clone dry-run copies the reference tree
> into `--target-dir`, applies the overrides, and saves the manifest -- it only
> skips the *push*. So `--target-dir` exists on disk afterward (the diff it
> reports is computed from that materialised tree). Use a throwaway path, or
> delete it after inspecting.

## Promoting between two live projects (dev -> prod) in CI

`sync clone` seeds a *fresh* target once. For an ongoing **promotion loop**
between two already-live projects -- pull from source, review the
destination-side diff in a PR, merge to ship -- there is a dedicated sibling
skill: [kbagent-promotion-pipeline](../../kbagent-promotion-pipeline/SKILL.md).
It generates the three GitHub Actions workflows (pull / validate / push) around
the same `sync pull` + `sync push --dry-run` + `sync push` primitives described
above, using `KBAGENT_PROJECT_FROM_ENV=1` + `--project __env__` so no token is
ever written to `config.json`.

The one non-obvious mechanic it encodes: the tracked directory's
`.keboola/manifest.json` stays bound to the **destination** project's config
ids forever, so the source pull goes through a throwaway scratch directory and
only its *content* is merged in. Pulling the source straight into the tracked
directory would overwrite that id mapping and make every later push create
duplicates in the destination instead of updating.
