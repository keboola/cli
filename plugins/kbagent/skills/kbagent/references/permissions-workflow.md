# Permissions Workflow -- AI Agent Sandboxing

Firewall-style allow/deny rules that control which CLI commands and MCP tools can be used.
Use this to restrict AI agents to read-only mode or block specific destructive operations.

## Setting up read-only mode

```bash
# Option A: New workspace with read-only from the start
kbagent init --from-global --read-only

# Option B: Existing setup -- set policy interactively (requires typing a confirmation code)
kbagent permissions set --mode allow --deny "cli:write" --deny "tool:write"
```

After this, the agent can:
- Browse configs, jobs, lineage, storage, components
- List and read MCP tools (get_*, list_*)
- Use `kbagent permissions check` to test what's allowed

The agent CANNOT:
- Create/delete branches, workspaces, configs
- Call write MCP tools (create_*, update_*, delete_*)
- Modify or remove the permission policy (requires confirmation code)

## Checking permissions before acting

```bash
# Before attempting a write operation, check if it's allowed
kbagent --json permissions check "branch.create"
# Exit 0 = allowed, exit 6 = denied

# List all operations with current status
kbagent --json permissions list
```

## Pattern reference

| Pattern | Matches |
|---------|---------|
| `cli:write` | All write, destructive, and admin CLI commands |
| `cli:read` | All read-only CLI commands |
| `tool:write` | All MCP write tools (create_*, update_*, delete_*, add_*, set_*, remove_*) |
| `tool:read` | All MCP read tools (get_*, list_*, search, find_*) |
| `branch.delete` | Exact command match |
| `sync.*` | All sync subcommands (glob) |
| `tool:create_*` | MCP tools matching glob pattern |

## Key details

- **Exit code 6** = operation blocked by permission policy
- **`permissions` commands always work** -- you can never lock yourself out of checking/listing
- **Changing or removing the policy requires interactive confirmation** (random code typed by human)
- **New commands not in the registry** are treated as write operations (fail-closed)
- Policy is stored in `config.json` alongside project configs
