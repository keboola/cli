# Branch Workflow -- Development Branches

Development branches let you make changes to Keboola configs without affecting production.
kbagent tracks the "active branch" per project so you don't have to pass `--branch` every time.

## Typical workflow

```bash
# 1. Create a branch (auto-activates it)
kbagent --json branch create --project ALIAS --name "fix-transform-x"
# Returns: branch_id, name, and confirms activation

# 2. All subsequent commands on this project auto-use the active branch
kbagent --json config list --project ALIAS
kbagent --json config update --project ALIAS --component-id C --config-id ID --set '...'
kbagent --json workspace create --project ALIAS --name "branch-debug"

# 3. When done, get the merge URL (does NOT auto-merge!)
kbagent --json branch merge --project ALIAS
# Returns: Keboola UI URL for review and merge
# Auto-resets active branch back to main
```

## Branch commands reference

| Command | What it does |
|---------|-------------|
| `branch list` | List all branches (marks active one) |
| `branch create --name "..."` | Create + auto-activate |
| `branch use --branch ID` | Switch to existing branch |
| `branch reset` | Switch back to main/production |
| `branch delete --branch ID` | Delete branch (resets if it was active) |
| `branch merge` | DEPRECATED (since vNEXT): get merge URL, reset to main. On a project with `branches-merge-requests` use `merge-request` -- see [merge-request-workflow.md](merge-request-workflow.md) |

## Key details

- **Async operations**: `branch create` and `branch delete` are async on the API. kbagent waits for completion (typically 1-3s). No need to poll.
- **Merge from the CLI needs the merge-request group** *(since vNEXT)*: `branch merge` only returns a URL for the Keboola UI (and is deprecated). On a project with the `branches-merge-requests` feature, `kbagent merge-request create` + `merge-request merge` merge via the API with review and conflict resolution -- see [merge-request-workflow.md](merge-request-workflow.md).
- **Active branch persistence**: stored in kbagent config. Survives between sessions.
- **Config commands respect active branch**: `config list`, `config detail`, and `config search` auto-scope to the active branch. Use `--branch ID` to override.
- **Workspaces respect active branch**: `workspace create` and `workspace delete` operate in the active branch context.
- **Sync respects active branch**: `sync pull` writes dev branch configs into a separate directory (e.g. `fix-etl/` instead of `main/`). `sync diff` and `sync push` also auto-scope to the active branch. See [sync-workflow.md](sync-workflow.md) for details.
- **`branch reset` alone does not re-target the sync manifest** *(since v0.89.0)*: a dev-branch `sync pull` re-points every `manifest.configurations` entry at that branch, so after resetting to production the `main/` tree is an orphan the manifest no longer tracks. A production `sync diff` / `sync push` reports those configs under `orphaned` and excludes them (they are never pushed as new configs — issue #649); run `kbagent sync pull --project ALIAS` to re-target the manifest to production first. Configs that exist only on the dev branch are promoted with `branch merge`, never by pushing them to production.
