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

## Common restriction recipes

### Block all writes (read-only agent)
```bash
kbagent permissions set --mode allow --deny "cli:write" --deny "tool:write"
```

### Block all MCP tool calls (CLI only, no MCP)
```bash
kbagent permissions set --mode allow --deny "tool.call" --deny "tool:*"
```
Blocks both the `tool call` CLI command and any MCP tool execution via the service layer.

### Block config deletion
```bash
kbagent permissions set --mode allow --deny "config.delete" --deny "tool:delete_configuration"
```
Blocks both the CLI command and the MCP tool. The agent can still create and update configs.

### Block storage operations
```bash
kbagent permissions set --mode allow --deny "storage.*"
```
Blocks `storage buckets`, `storage bucket-detail`, and `storage tables`. Note: MCP tools like `get_buckets` are separate -- add `--deny "tool:*bucket*"` to block those too.

### Block workspace SQL queries
```bash
kbagent permissions set --mode allow --deny "workspace.query"
```
The agent can still create/list/delete workspaces and load tables, but cannot execute SQL. To block all workspace writes:
```bash
kbagent permissions set --mode allow --deny "workspace.create" --deny "workspace.delete" --deny "workspace.load" --deny "workspace.query" --deny "workspace.from-transformation"
```

### Block sync push (prevent pushing changes to Keboola)
```bash
kbagent permissions set --mode allow --deny "sync.push"
```
The agent can still pull configs and view diffs, but cannot push changes back. Note: `sync.push` pushes to whatever branch is active (main or dev). There is no way to block "push to main only" -- use `branch.create` + `branch.use` workflow to ensure the agent works on a dev branch, then block `branch.reset` to prevent switching back to main.

### Block destructive operations only (allow creates/updates)
```bash
kbagent permissions set --mode allow --deny "cli:destructive" --deny "tool:destructive"
```
Blocks `branch.delete`, `workspace.delete`, `config.delete` and MCP tools like `delete_*`, `remove_*`. The agent can still create and modify resources.

### Allow only specific commands (strict allowlist)
```bash
kbagent permissions set --mode deny \
  --allow "project.list" --allow "project.status" \
  --allow "config.list" --allow "config.detail" --allow "config.search" \
  --allow "job.list" --allow "job.detail" \
  --allow "tool.list" --allow "tool:read"
```
Everything else is blocked. This is the most restrictive approach.

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

## Defense in depth (`--read-only`)

`kbagent init --read-only` applies three layers of protection:

| Layer | What it does | Who it stops |
|-------|-------------|-------------|
| kbagent policy | Blocks write CLI commands and MCP tools | Any agent using kbagent |
| Filesystem `chmod 0400` | config.json owner-read-only | Other users/processes; run agent as separate OS user for full isolation |
| `.claude/settings.json` | Deny rules block: Read/Edit/Write config, any Bash mentioning config path, chmod, --config-dir, KBAGENT_CONFIG_DIR, permissions set/reset | Claude Code specifically |

For production: run the agent as a separate OS user so it cannot even read config.json (tokens stay invisible to the agent process).

To unlock (human only):
```bash
chmod u+w .kbagent/config.json          # restore write permission
kbagent permissions reset               # type confirmation code
# optionally remove .claude/settings.json deny rules
```

## Key details

- **Exit code 6** = operation blocked by permission policy
- **`permissions` commands always work** -- you can never lock yourself out of checking/listing
- **Changing or removing the policy requires interactive confirmation** (random code typed by human)
- **New commands not in the registry** are treated as write operations (fail-closed)
- Policy is stored in `config.json` alongside project configs
