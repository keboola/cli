# kbagent -- Keboola Agent CLI

One CLI to manage all your Keboola projects. Designed to be driven by AI agents -- Claude Code, Codex, Gemini, Cursor -- but works great standalone too.

No more switching between the UI, old CLI, MCP server, and raw API calls. `kbagent` wraps everything into workflow-oriented commands where dev branches propagate automatically, multi-project operations run in parallel, and AI agents can be sandboxed safely.

## Install

```bash
uv tool install git+https://github.com/padak/keboola_agent_cli
```

Auto-updates on every launch. Run `kbagent changelog` to see what changed.

## For AI agents

This CLI is built AI-first. Every command outputs structured JSON (`--json`), errors include machine-readable codes, and the permission firewall enforces safety at the code level -- not via prompt instructions.

**Claude Code plugin** (agent learns all 74 commands automatically):

```
/plugin marketplace add padak/keboola_agent_cli
/plugin install kbagent@keboola-agent-cli
```

**Any other agent** -- just tell it to run `kbagent context` and it gets the full command reference.

**What you can ask your agent:**

> "Give me a full inventory of all Keboola projects -- configs, jobs, components, data volumes."

> "Find the last failed job in project X, figure out why it crashed, spin up a workspace with the input data, and fix the SQL."

> "Compare the SQL transformation between production and the dev branch."

> "Create a new Snowflake transformation that joins orders and customers, push it to a dev branch."

### Sandboxing

```bash
kbagent init --from-global --read-only
```

Three protection layers (kbagent policy + filesystem chmod + Claude Code deny rules) prevent the agent from writing, deleting, or bypassing restrictions. See [Permissions Guide](docs/guide.md#permissions) for details.

## 30-second demo

```bash
# Connect a project (Storage API token from Keboola UI)
kbagent project add --project prod \
  --url https://connection.keboola.com --token YOUR_TOKEN

# Search across ALL projects for a table reference
kbagent config search --query "customer_id"

# Run a job and wait for it to finish
kbagent job run --project prod --component-id keboola.ex-db-snowflake \
  --config-id 456 --wait

# Debug a failing SQL transformation with real data (no full job needed)
kbagent workspace from-transformation --project prod \
  --component-id keboola.snowflake-transformation --config-id 789
kbagent workspace query --project prod --workspace-id WS_ID \
  --sql "SELECT * FROM users LIMIT 10"
```

## What it does

| Area | What you get |
|------|-------------|
| **Multi-project** | All read commands query every connected project in parallel. One command, all projects. |
| **Configurations** | List, search, inspect, scaffold, update, delete configs. Full-text search across all config bodies. |
| **Jobs** | List, inspect, run with `--wait` polling and `--timeout`. Row-level execution for multi-row configs. |
| **Storage** | Buckets, tables, files -- full CRUD. Upload CSV (auto-creates bucket+table). Download by file ID or by tag. |
| **Dev branches** | Create a branch, activate it, and every command auto-targets it. Storage, MCP, sync -- everything follows. |
| **Sync & GitOps** | Pull configs as YAML, edit in IDE, push back. SQL/Python extracted as real files. Diff and status tracking. |
| **MCP tools** | Call `keboola-mcp-server` tools with auto-expand, multi-project fan-out, branch propagation, schema validation. |
| **Workspaces** | Create Snowflake/BQ workspace, load tables, run SQL. Create from transformation config for instant debugging. |
| **Sharing & lineage** | Cross-project data lineage via bucket sharing. Share/link/unlink with org/project/user access control. |
| **Encryption** | Encrypt secrets (`#password`, `#api_token`) via Keboola Encryption API. Works with sync push and MCP. |
| **Permissions** | Firewall for AI agents: read-only, deny-writes, deny-destructive. Code-level enforcement, not prompt tricks. |
| **Auto-update** | Self-updates on startup. "What's new" after each update. Full changelog via `kbagent changelog`. |

## Setup options

**Org admin** (registers all projects at once):
```bash
KBC_MANAGE_API_TOKEN=your-manage-token \
  kbagent org setup --org-id 123 --url https://connection.keboola.com --yes
```

**Project member** (Personal Access Token, no org-admin rights needed):
```bash
KBC_MANAGE_API_TOKEN=your-personal-access-token \
  kbagent org setup --project-ids 123,456 --url https://connection.keboola.com --yes
```

**Single project** (manual):
```bash
kbagent project add --project prod --url https://connection.keboola.com --token YOUR_TOKEN
```

Run `kbagent doctor` to verify your setup.

## All commands

Full command reference with flags: [SKILL.md](plugins/kbagent/skills/kbagent/SKILL.md)

```
kbagent project     add | list | remove | edit | status | refresh
kbagent org         setup
kbagent component   list | detail
kbagent config      list | detail | search | update | delete | new
kbagent job         list | detail | run
kbagent storage     buckets | bucket-detail | create-bucket | delete-bucket
                    tables | table-detail | create-table | upload-table | download-table | delete-table
                    files | file-detail | file-upload | file-download | file-tag | file-delete
                    load-file | unload-table
kbagent sharing     list | share | unshare | link | unlink
kbagent lineage     show
kbagent branch      list | create | use | reset | delete | merge
kbagent workspace   create | list | detail | delete | password | load | query | from-transformation
kbagent tool        list | call
kbagent sync        init | pull | status | diff | push | branch-link | branch-unlink | branch-status
kbagent encrypt     values
kbagent permissions list | show | set | reset | check
kbagent             init | context | doctor | version | update | changelog
```

## Documentation

| Guide | What it covers |
|-------|---------------|
| [User Guide](docs/guide.md) | Configuration, permissions, per-directory isolation, workflows |
| [Contributing](CONTRIBUTING.md) | Architecture, coding style, adding commands, testing checklist |

## Development

Read [CONTRIBUTING.md](CONTRIBUTING.md) first -- it covers the 3-layer architecture, coding conventions, security principles, and the full checklist for adding new commands.

```bash
git clone https://github.com/padak/keboola_agent_cli.git && cd keboola_agent_cli
make install   # uv pip install -e ".[dev]"
make check     # lint + format + test
make hooks     # install pre-commit hook
```

## License

MIT
