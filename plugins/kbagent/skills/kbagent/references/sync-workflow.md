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
- **Pull protects local edits**: modified files are skipped (use `--force` to overwrite)
- **Push only sends local changes**: remote_modified and conflict changes are skipped
- **Encrypted values**: nonce differences are ignored in diff (no false positives)
- **New configs**: push auto-assigns IDs from the API, updates manifest
- **Storage metadata is read-only**: not tracked in manifest, excluded from diff/push
- **Jobs are per-config**: `_jobs.jsonl` shows recent N jobs (default 5) with status + timing
- **Data samples auto-trim**: tables with >30 columns export only first 30 (API sync limit)
- **Encrypted columns masked**: columns starting with `#` show `***ENCRYPTED***` in samples
