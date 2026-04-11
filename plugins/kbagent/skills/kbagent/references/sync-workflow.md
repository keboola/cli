# Sync Workflow -- GitOps for Keboola Configurations

Sync lets you manage Keboola configurations as local files with full git integration.

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
