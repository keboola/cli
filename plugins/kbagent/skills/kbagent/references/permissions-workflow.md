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

## Session firewall flags

Two top-level flags let an operator harden a single invocation WITHOUT editing
the persisted policy in `config.json`. They are session-only, additive, and
evaluated alongside any persisted policy.

```bash
# Wide net: blocks writes + destructive + admin
kbagent --deny-writes <command>

# Narrow net: blocks only data-destructive ops (pure writes still allowed)
kbagent --deny-destructive <command>

# Both (equivalent to --deny-writes here, since wide subsumes narrow)
kbagent --deny-writes --deny-destructive <command>
```

### `--deny-writes` (WIDE)

Appends `cli:write` and `tool:write` to the deny list. The `cli:write` pattern
intentionally spans the **write + destructive + admin** categories, so this one
flag blocks everything that mutates state -- config create/update/delete, branch
delete, project add/remove/edit, org setup, storage writes, sync push, and every
MCP write tool (`create_*`, `update_*`, `delete_*`, `add_*`, `set_*`, `remove_*`).

Use this when you want a strict read-only session without touching the persisted
policy. Blocked operations exit with code 6 (`PERMISSION_DENIED`).

### `--deny-destructive` (NARROW)

Appends `cli:destructive` and `tool:destructive` to the deny list. This pattern
matches **only** operations categorized as destructive (data destruction) --
`branch.delete`, `workspace.delete`, `config.delete`, `storage.delete-table`,
`storage.delete-bucket`, `storage.delete-column`, `job.terminate`, and MCP
`delete_*` / `remove_*` tools.

Pure-write operations (e.g. `storage create-bucket`, `config update`) and admin
operations (e.g. `project remove`, `org setup`) are **still allowed**. Use this
when an agent needs to create/modify resources but must not be able to destroy
existing data.

### REPL forwarding

When invoked as `kbagent --deny-writes repl` (or `--deny-destructive`), the
flags propagate into every subcommand run inside the REPL session, so each
inner invocation picks them up automatically. A duplicate-append guard prevents
the flag from being injected twice if a user also types it explicitly on a REPL
line.

### Relationship to persisted policy

The session flags merge **additively** with the persisted policy for the
duration of the invocation:

- The persisted `mode`, `allow` list, and existing `deny` entries are preserved
  unchanged. Only the flag-implied deny patterns are appended (deduped).
- Session flags **can only add more deny entries** -- they NEVER relax the
  persisted policy. Running `kbagent --deny-writes` against a policy that
  already denies everything does not re-open anything.
- The merged policy lives in memory for this process only. It is never written
  to `config.json`, so subsequent invocations without the flag revert to the
  persisted policy alone.
- `kbagent permissions list` and `kbagent permissions show` render the
  **effective** policy (persisted merged with session flags) so you can verify
  what is actually active right now. The `session_flags` field in the JSON
  output of `permissions show` surfaces which flags are in play.

For the complementary project-pin UX (`kbagent project use <alias>`, which
persists a default project so you can drop `--project` from subsequent
commands), see the project management section of the skill.

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

## Manage tokens and the firewall (since v0.27.1)

The firewall guards `OPERATION_REGISTRY` calls -- it does NOT
intercept env-var reads or raw HTTP. So a sandboxed agent with
`KBC_MANAGE_API_TOKEN` (or `KBC_MANAGE_TOKEN_<SUFFIX>`) in its env
can `curl -H "X-KBC-ManageApiToken: $KBC_MANAGE_API_TOKEN"
https://connection.keboola.com/manage/projects` and the firewall
stays blind. Manage tokens are org-scoped (broader blast radius
than per-project Storage tokens), so this leak matters.

`kbagent` ships three layers of opt-out:

```bash
# 1. Session-only flag (one invocation):
kbagent --no-env-manage-token data-app password --project foo --app-id 42
# resolve_manage_token refuses both env-var forms and falls through
# to TTY prompt (or exit 2 in non-interactive mode).

# 2. Persisted policy (survives across invocations):
kbagent permissions deny-manage-env       # type random confirmation code
# AppConfig.allow_env_manage_token = False
# Reverse with:
kbagent permissions allow-manage-env

# 3. Auto-set for AI sandboxes:
kbagent init --from-global --read-only
# In addition to the firewall, this sets
# allow_env_manage_token=False for the new workspace.
```

When the persisted policy denies env, the resolver behaves the same
as if `--no-env-manage-token` were passed on every invocation: env
vars are ignored; TTY prompt is the only path; non-interactive exits
2 with a message naming both env-var forms. The same random-code
confirmation prompt that gates `permissions set` and `reset` gates
`permissions {deny,allow}-manage-env` -- an AI agent cannot flip
the policy programmatically.

**Stack-aware resolver**: `resolve_manage_token()` also gained per-
stack env-var lookup in v0.27.1. The hostname-derived form
`KBC_MANAGE_TOKEN_<HOSTNAME_SUFFIX>` (e.g.
`KBC_MANAGE_TOKEN_EU_CENTRAL_1` for `connection.eu-central-1.keboola.com`)
is preferred when the caller can supply a `stack_url`. The legacy
single-var `KBC_MANAGE_API_TOKEN` is the fallback for callers that
cannot. Multi-stack `kbagent project refresh --all` resolves once
per distinct stack with internal caching, so prompts fire at most
once per stack. See `gotchas.md` ("Manage tokens are stack-scoped +
sit OUTSIDE the firewall") for the full table of stack-to-suffix
mappings.

## Key details

- **Exit code 6** = operation blocked by permission policy
- **`permissions` commands always work** -- you can never lock yourself out of checking/listing
- **Changing or removing the policy requires interactive confirmation** (random code typed by human)
- **New commands not in the registry** are treated as write operations (fail-closed)
- Policy is stored in `config.json` alongside project configs (mode 0600)
- **`permissions reset` does NOT touch `allow_env_manage_token`** -- the manage-env policy is intentionally a separate axis. Use `permissions allow-manage-env` to revert it
