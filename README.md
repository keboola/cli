# kbagent -- Keboola Agent CLI

One CLI to manage all your Keboola projects. Bring your own AI agent -- Claude Code, Codex, Gemini -- and turn it into a Keboola power user that handles complex tasks for you: auditing projects, debugging failed jobs, optimizing transformations, tracing data lineage across dozens of projects.

kbagent is the bridge between your AI and Keboola. It respects real engineering workflows -- dev branches, git sync, pull requests -- so the agent works the way your team already works.

```bash
# Install
uv tool install git+https://github.com/padak/keboola_agent_cli
```

### Quick start -- pick your path

**Org admin** (registers all projects in one shot):
```bash
KBC_MANAGE_API_TOKEN=your-manage-token \
  kbagent org setup --org-id 123 --url https://connection.keboola.com --yes
```

**Project member** (no org-admin rights needed -- uses a Personal Access Token):
```bash
KBC_MANAGE_API_TOKEN=your-personal-access-token \
  kbagent org setup --project-ids 123,456 --url https://connection.keboola.com --yes
```
PAT is available in Keboola UI under *Account Settings > Personal Access Tokens*.

Both variants auto-create Storage API tokens with minimal scope and register the projects in kbagent.

<details>
<summary>Manual single-project setup (advanced)</summary>

If you already have a Storage API token and want to add one project manually:

```bash
kbagent project add --project prod --url https://connection.keboola.com --token YOUR_TOKEN
```

</details>

## For AI agents (Claude Code, Codex, Gemini)

Install the Claude Code plugin and the agent learns all commands automatically:

```bash
/plugin marketplace add padak/keboola_agent_cli
/plugin install kbagent@keboola-agent-cli
```

Or just tell any agent to run `kbagent context` -- it prints a complete reference.

### What you can ask

> "Give me a full inventory of all Keboola projects -- configs, jobs, components, data volumes. Deliver it as an HTML report for our CDO."

> "Map how data flows between projects via bucket sharing. Draw a Mermaid diagram and save it as an interactive HTML page."

> "Find the last failed job in project X, figure out why it crashed, spin up a workspace with the input data, and fix the SQL."

> "Review jobs from the past 7 days across all projects. Find the slowest transformations and the ones that fail most often. Give me an optimization report."

> "Compare the SQL transformation config between production and the dev branch -- show me what changed in the code and which input tables were added or removed."

> "Create a new Snowflake transformation that joins 'orders' and 'customers' from in.c-crm, builds a revenue-per-customer summary, and push it to a dev branch."

## What you get

| Capability | Example |
|-----------|---------|
| **Query all projects at once** | `kbagent config list` returns configs from every connected project in parallel |
| **Search across everything** | `kbagent config search --query "snowflake" -i` finds matches in all config bodies |
| **Trace data flow** | `kbagent lineage show` maps cross-project data dependencies via bucket sharing |
| **Debug SQL without running jobs** | `kbagent workspace create` + `workspace query --sql "SELECT ..."` |
| **Manage dev branches** | `kbagent branch create` / `branch merge` with persistent active branch state |
| **Call MCP tools** | `kbagent tool call get_tables --input '{"bucket_ids":["in.c-main"]}'` across all projects |
| **Sync configs as files** | `kbagent sync pull --with-samples` downloads configs, jobs, storage metadata, CSV samples |
| **Create new configs** | `kbagent config new --component-id keboola.ex-db-snowflake` scaffolds from schema + examples |
| **Structured JSON output** | Every command supports `--json` for reliable programmatic parsing |

Every read command runs across all connected projects in parallel. Write commands target a specific project with `--project`.

## All commands

```
kbagent project   add | list | remove | edit | status
kbagent org       setup
kbagent component list | detail
kbagent config    list | detail | search | update | delete | new
kbagent job       list | detail
kbagent storage   buckets | bucket-detail | tables | create-bucket | create-table | upload-table | delete-table | delete-bucket
kbagent sharing   list | share | unshare | link | unlink
kbagent lineage   show
kbagent branch    list | create | use | reset | delete | merge
kbagent workspace create | list | detail | delete | password | load | query | from-transformation
kbagent tool      list | call
kbagent sync      init | pull | status | diff | push | branch-link | branch-unlink | branch-status
kbagent permissions list | show | set | reset | check
kbagent           init | context | doctor | version | update
```

Run `kbagent <command> --help` for flags and examples.

## Configuration

Config lives at the platform default location (permissions `0600`):
- **macOS:** `~/Library/Application Support/keboola-agent-cli/config.json`
- **Linux:** `~/.config/keboola-agent-cli/config.json`

Tokens are always masked in output.

For per-directory isolation (e.g. separate clients), run `kbagent init` to create a local `.kbagent/` workspace. Resolution: `--config-dir` flag > `KBAGENT_CONFIG_DIR` env > `.kbagent/` in CWD/parents > global default.

Run `kbagent doctor` to verify your setup.

## Permissions (AI agent sandboxing)

Control which commands and MCP tools your AI agent can use -- like a firewall with allow/deny rules.

**Quick start -- read-only mode:**

```bash
# New workspace: read-only from the start
kbagent init --from-global --read-only

# Existing setup: switch to read-only
kbagent permissions set --mode allow --deny "cli:write" --deny "tool:write"
```

The agent can browse configs, list jobs, trace lineage, and read MCP tools -- but cannot create branches, delete workspaces, modify configs, or call write MCP tools. Any blocked command returns exit code 6 with a clear error message.

**How it works:**

| Mode | Meaning |
|------|---------|
| `--mode allow --deny "..."` | Everything allowed except denied patterns |
| `--mode deny --allow "..."` | Everything blocked except allowed patterns |

**Pattern examples:**

| Pattern | Matches |
|---------|---------|
| `cli:write` | All write/delete/admin CLI commands |
| `cli:read` | All read-only CLI commands |
| `tool:write` | All MCP write tools (create_\*, update_\*, delete_\*) |
| `tool:read` | All MCP read tools (get_\*, list_\*) |
| `branch.delete` | Exact command |
| `sync.*` | All sync subcommands |
| `tool:create_*` | MCP tools matching glob |

**Management commands:**

```bash
kbagent permissions list             # See all operations with risk categories
kbagent permissions show             # Show current policy
kbagent permissions check "branch.delete"  # Test if operation is allowed (exit 0 or 6)
kbagent permissions reset            # Remove restrictions (requires confirmation code)
```

### Layered security

`kbagent init --read-only` applies three independent protection layers. Each stops a different bypass vector:

| Layer | What it does | What it stops |
|-------|-------------|---------------|
| **kbagent policy** | Deny rules in `config.json` block write commands and MCP tools | Agent running `kbagent branch create`, `tool call create_config`, etc. |
| **Filesystem `chmod 0400`** | `config.json` owner-read-only | Agent editing the file directly; ideally, run the agent as a different OS user so it can't even read the config |
| **`.claude/settings.json`** | Deny rules block Claude Code from touching the config, running `chmod`, `permissions set/reset`, or using `--config-dir` | Claude Code specifically -- deny rules are evaluated before any tool executes |

**Recommended setup for production:**

Run the AI agent as a separate OS user (or in a container/sandbox). The human sets up the workspace:
```bash
kbagent init --from-global --read-only   # creates config (0400) + .claude/settings.json
```
The agent can run `kbagent --json config list` etc. but cannot read, modify, or bypass the config.

**How `.claude/settings.json` works:**

Claude Code loads settings from `.claude/settings.json` in the project root. Deny rules always win over allow rules. The generated file blocks:

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

Settings precedence (highest wins): Managed policy > CLI flags > `.claude/settings.local.json` > **`.claude/settings.json`** > `~/.claude/settings.json`. Commit it to git so the protection applies for everyone.

**To unlock (human only):**

```bash
chmod u+w .kbagent/config.json          # 1. restore write permission
kbagent permissions reset               # 2. type random confirmation code
# optionally: edit .claude/settings.json to remove deny rules
```

## Development

```bash
git clone https://github.com/padak/keboola_agent_cli.git && cd keboola_agent_cli
make install   # uv pip install -e ".[dev]"
make check     # lint + format + test
make hooks     # install pre-commit hook
```

## License

MIT
