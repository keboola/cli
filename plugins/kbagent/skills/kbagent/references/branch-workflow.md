# Branch Workflow -- Development Branches

Development branches let you make changes to Keboola configs without affecting production.
kbagent tracks the "active branch" per project so you don't have to pass `--branch` every time.

## Typical workflow

```bash
# 1. Create a branch (auto-activates it)
kbagent --json branch create --project ALIAS --name "fix-transform-x"
# Returns: branch_id, name, and confirms activation

# 2. All subsequent commands on this project auto-use the active branch
kbagent --json tool call get_configs --project ALIAS
kbagent --json tool call update_configuration --project ALIAS --input '{...}'
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
| `branch merge` | Get merge URL, reset to main |

## Key details

- **Async operations**: `branch create` and `branch delete` are async on the API. kbagent waits for completion (typically 1-3s). No need to poll.
- **Merge is manual**: `branch merge` returns a URL for the Keboola UI. It does NOT merge via API. This is intentional for safe review.
- **Active branch persistence**: stored in kbagent config. Survives between sessions.
- **MCP tools respect active branch**: tool calls automatically use the active branch without extra flags.
- **Workspaces respect active branch**: `workspace create` and `workspace delete` operate in the active branch context.
