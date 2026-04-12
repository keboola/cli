# kbagent User Guide

Detailed guide for configuration, permissions, and advanced usage.
For quick start and feature overview, see the [README](../README.md).

## Configuration

Config lives at the platform default location (permissions `0600`):
- **macOS:** `~/Library/Application Support/keboola-agent-cli/config.json`
- **Linux:** `~/.config/keboola-agent-cli/config.json`

Tokens are always masked in output.

### Per-directory isolation

For separate environments (different clients, projects, etc.), create a local workspace:

```bash
kbagent init                  # creates .kbagent/ in current directory
kbagent init --from-global    # copies global projects into local config
```

Resolution order: `--config-dir` flag > `KBAGENT_CONFIG_DIR` env > `.kbagent/` in CWD/parents > global default.

### Health check

```bash
kbagent doctor       # verify setup, token validity, MCP server availability
kbagent doctor --fix # auto-fix common issues
```

## Auto-update

kbagent checks for updates on startup and upgrades itself automatically.

- After update, shows "What's new" with changes from the changelog
- Opt-out: `export KBAGENT_AUTO_UPDATE=false`
- Manual update: `kbagent update`
- View changelog: `kbagent changelog` or `kbagent changelog --limit 5`

## Permissions

Control which commands and MCP tools your AI agent can use -- like a firewall with allow/deny rules.

### Quick start -- read-only mode

```bash
# New workspace: read-only from the start
kbagent init --from-global --read-only

# Existing setup: switch to read-only
kbagent permissions set --mode allow --deny "cli:write" --deny "tool:write"
```

The agent can browse configs, list jobs, trace lineage, and read MCP tools -- but cannot create branches, delete workspaces, modify configs, or call write MCP tools. Any blocked command returns exit code 6 with a clear error message.

### Policy modes

| Mode | Meaning |
|------|---------|
| `--mode allow --deny "..."` | Everything allowed except denied patterns |
| `--mode deny --allow "..."` | Everything blocked except allowed patterns |

### Pattern examples

| Pattern | Matches |
|---------|---------|
| `cli:write` | All write/delete/admin CLI commands |
| `cli:read` | All read-only CLI commands |
| `tool:write` | All MCP write tools (create_\*, update_\*, delete_\*) |
| `tool:read` | All MCP read tools (get_\*, list_\*) |
| `branch.delete` | Exact command |
| `sync.*` | All sync subcommands |
| `tool:create_*` | MCP tools matching glob |

### Management commands

```bash
kbagent permissions list             # See all operations with risk categories
kbagent permissions show             # Show current policy
kbagent permissions check "branch.delete"  # Test if operation is allowed (exit 0 or 6)
kbagent permissions reset            # Remove restrictions (requires confirmation code)
```

### Layered security

`kbagent init --read-only` applies three independent protection layers:

| Layer | What it does | What it stops |
|-------|-------------|---------------|
| **kbagent policy** | Deny rules in `config.json` block write commands and MCP tools | Agent running `kbagent branch create`, `tool call create_config`, etc. |
| **Filesystem `chmod 0400`** | `config.json` owner-read-only | Agent editing the file directly |
| **`.claude/settings.json`** | Deny rules block Claude Code from touching the config | Claude Code specifically -- deny rules evaluated before any tool executes |

### Recommended setup for production

Run the AI agent as a separate OS user (or in a container/sandbox):

```bash
kbagent init --from-global --read-only   # creates config (0400) + .claude/settings.json
```

The agent can run `kbagent --json config list` etc. but cannot read, modify, or bypass the config.

### Claude Code settings.json

Claude Code loads settings from `.claude/settings.json` in the project root. The generated file blocks:

```json
{
  "permissions": {
    "deny": [
      "Read(.kbagent/config.json)",
      "Edit(.kbagent/config.json)",
      "Write(.kbagent/config.json)",
      "Bash(*.kbagent/config.json*)",
      "Bash(*chmod*.kbagent*)",
      "Bash(kbagent permissions set*)",
      "Bash(kbagent permissions reset*)",
      "Bash(*permissions set*)",
      "Bash(*permissions reset*)",
      "Bash(*--config-dir*)",
      "Bash(*KBAGENT_CONFIG_DIR*)"
    ]
  }
}
```

Commit it to git so the protection applies for everyone.

### Unlocking (human only)

```bash
chmod u+w .kbagent/config.json          # 1. restore write permission
kbagent permissions reset               # 2. type random confirmation code
# optionally: edit .claude/settings.json to remove deny rules
```

## Dev branches workflow

Create a branch, and every subsequent command auto-targets it:

```bash
kbagent branch create --project prod --name "refactor-pipeline"
# All commands now target this branch:
kbagent config list --project prod           # branch configs
kbagent tool call create_configuration ...   # creates on branch
kbagent sync pull                            # pulls branch state

# Done? Reset to main:
kbagent branch reset --project prod
```

Override with explicit `--branch ID` on any command.

## Sync & GitOps workflow

```bash
kbagent sync init --project prod             # initialize sync workspace
kbagent sync pull --with-samples             # download configs + CSV samples
# Edit YAML/SQL/Python files locally...
kbagent sync status                          # see what changed
kbagent sync diff                            # detailed diff
kbagent sync push --project prod             # push changes back
```

### Git branch to Keboola branch mapping

```bash
kbagent sync branch-link --project prod      # link current git branch to a Keboola dev branch
kbagent sync branch-status                   # show mapping
kbagent sync branch-unlink                   # remove mapping
```

## Storage Files workflow

Upload, download, tag, and manage files in Keboola Storage:

```bash
# Upload with tags
kbagent storage file-upload --project prod --file ./data.csv --tag report --permanent

# Download latest by tag
kbagent storage file-download --project prod --tag report

# Load uploaded file into a table
kbagent storage load-file --project prod --file-id 12345 --table-id in.c-data.users

# Export table to a Storage File
kbagent storage unload-table --project prod --table-id in.c-data.users --tag snapshot --download
```

See the full [Storage Files reference](../plugins/kbagent/skills/kbagent/references/storage-files-workflow.md) for more patterns.

## Encryption workflow

Encrypt secrets for component configurations:

```bash
kbagent encrypt values --project prod \
  --component-id keboola.ex-db-snowflake \
  --input '{"#password": "secret123", "#api_token": "tok_xxx"}'
```

Returns encrypted values ready for `sync push` or `tool call update_configuration`.
