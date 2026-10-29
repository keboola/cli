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

# 3. When done, merge the branch (project feature branches-merge-requests;
#    without it, merge in the Keboola UI, then run `branch reset`)
kbagent --json merge-request create --project ALIAS --title "Fix transform X"
kbagent --json merge-request merge --project ALIAS --merge-request-id 42
# See merge-request-workflow.md before you automate a merge
```

## Branch commands reference

| Command | What it does |
|---------|-------------|
| `branch list` | List all branches (marks active one) |
| `branch create --name "..."` | Create + auto-activate |
| `branch use --branch ID` | Switch to existing branch |
| `branch reset` | Switch back to main/production |
| `branch delete --branch ID` | Delete branch (resets if it was active) |
| `branch merge` | Removed in 1.0.0rc3 (deprecated since 0.94.0). It only printed the merge URL of the Keboola UI and reset to main. Use `merge-request` -- see [merge-request-workflow.md](merge-request-workflow.md) -- or the Keboola UI |

## Key details

- **Async operations**: `branch create` and `branch delete` are async on the API. kbagent waits for completion (typically 1-3s). No need to poll.
- **Merge from the CLI needs the merge-request group** *(since 0.94.0)*: `branch merge` only returned a URL for the Keboola UI, and 1.0.0 removes it *(since 1.0.0rc3)*. On a project with the `branches-merge-requests` feature, `kbagent merge-request create` + `merge-request merge` merge via the API with review and conflict resolution -- see [merge-request-workflow.md](merge-request-workflow.md).
- **Active branch persistence**: stored in kbagent config. Survives between sessions.
- **See which branch a command used** (since 0.96.1, #766): every command that picks a branch prints `Target: project 'P', branch ID 'NAME' (from 'kbagent branch use')` on stderr, and `--json` output carries `targets` (`branch_source`: `explicit`, `active_branch`, `git_mapping`, `manifest`, `merge_request` or `production`). `--dry-run` reports the same target as the real run. Check it before a write.
- **Config commands respect active branch**: `config list`, `config detail`, and `config search` auto-scope to the active branch. Use `--branch ID` to override.
- **Workspaces respect active branch**: `workspace create`, `workspace list`, `workspace detail` and `workspace delete` operate in the active branch context.
- **Sync respects active branch**: `sync pull` writes dev branch configs into a separate directory (e.g. `fix-etl/` instead of `main/`). `sync diff` and `sync push` also auto-scope to the active branch. See [sync-workflow.md](sync-workflow.md) for details.
- **`branch reset` alone does not re-target the sync manifest** *(since v0.89.0)*: a dev-branch `sync pull` re-points every `manifest.configurations` entry at that branch, so after resetting to production the `main/` tree is an orphan the manifest no longer tracks. A production `sync diff` / `sync push` reports those configs under `orphaned` and excludes them (they are never pushed as new configs — issue #649); run `kbagent sync pull --project ALIAS` to re-target the manifest to production first. Configs that exist only on the dev branch are promoted with a merge of the dev branch (`merge-request`, or the Keboola UI), never by pushing them to production.

## Headless `__env__` project (CI)

The `KBAGENT_PROJECT_FROM_ENV` project `__env__` cannot store an active branch: pass `--branch <id>` on every command. Up to 1.0.0rc4, `branch create --project __env__` exits 5 although the branch was created -- look it up with `kbagent --json branch list --project __env__` before a retry, or you get a second branch -- and `branch use` / `branch reset` are refused (fixed *(since vNEXT)* by #940). See [gitops-cicd-workflow.md](gitops-cicd-workflow.md) pitfall 2.6.
