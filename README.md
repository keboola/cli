# kbagent -- Keboola Agent CLI

One CLI to manage all your Keboola projects. Designed to be driven by AI agents -- Claude Code, Codex, Gemini, Cursor -- but works great standalone too.

No more switching between the UI, old CLI, MCP server, and raw API calls. `kbagent` wraps everything into workflow-oriented commands where dev branches propagate automatically, multi-project operations run in parallel, and AI agents can be sandboxed safely.

![kbagent in action](docs/assets/demo-hero.gif)

## Install

```bash
uv tool install git+https://github.com/padak/keboola_agent_cli
```

Auto-updates on every launch. Run `kbagent changelog` to see what changed.

## For AI agents

This CLI is built AI-first. Every command outputs structured JSON (`--json`), errors include machine-readable codes, and the permission firewall enforces safety at the code level -- not via prompt instructions.

**Claude Code plugin** (agent learns all 80 commands + gets a specialist subagent for writes):

```
/plugin marketplace add padak/keboola_agent_cli
/plugin install kbagent@keboola-agent-cli
```

Then either let the `kbagent` skill auto-trigger from natural prompts, or delegate explicitly with `/keboola <task>` -- the slash command spawns a `kbagent:keboola-expert` subagent with fresh context, hard rules (fresh fetch, dry-run first, prefer CLI over REST/MCP, version gate), and a JSON verification payload. See [docs/TUTORIAL.md §6](docs/TUTORIAL.md#6-using-the-agent-and-slash-commands).

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

![30-second demo](docs/assets/demo-readme-main.gif)

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
| **Configurations** | List, search, inspect, scaffold, update, delete configs. Full-text search across all config bodies (incl. rows). Metadata CRUD + folder grouping. |
| **Jobs** | List, inspect, run with `--wait` polling (exponential curve), `--timeout` auto-kill, log tail on failure. Row-level execution for multi-row configs. |
| **Flows** | Create, update, delete orchestrator/flow configs with phase/task DAG validation. Attach cron schedules (timezone + enabled/disabled state). |
| **Storage** | Buckets, tables, files -- full CRUD. Upload CSV (auto-creates bucket+table). Download by file ID or by tag. Descriptions on buckets/tables/columns (batch-applicable from YAML). Native column types (`VARCHAR(40)`, `NUMBER(18,2)`, `TIMESTAMP_TZ`, `VARIANT`, ...) with per-column `--not-null` and `--default` flags; dev branches auto-materialize target buckets on first write. |
| **Dev branches** | Create a branch, activate it, and every command auto-targets it. Storage writes, MCP, sync -- everything follows. Storage reads default to production (safer). |
| **Sync & GitOps** | Pull configs as YAML, edit in IDE, push back. SQL/Python extracted as real files. Diff and status tracking. Adopt existing kbc Go CLI checkouts (`sync init --adopt-existing`). |
| **MCP tools** | Call `keboola-mcp-server` tools with auto-expand, multi-project fan-out, branch propagation, schema validation. |
| **Workspaces** | Create Snowflake/BQ workspace, load tables, run SQL. Create from transformation config for instant debugging. Orphan detection + garbage collection. |
| **Sharing** | Cross-project bucket sharing with org/project/user access control. Share, link, unlink. |
| **Data apps** | First-class lifecycle for Streamlit / Flask / Node deployments (`keboola.data-apps`). Create, deploy, start, stop, password, delete. Hides the redeploy contract and per-project KMS encryption of git PATs. |
| **Lineage** | Column-level dependency analysis across projects. SQL/Python parsing, AI-enhanced detection, interactive web browser, Mermaid/HTML/ER export. |
| **Kai (AI Assistant)** | Ask Keboola's built-in AI questions about your project. One-shot or chat sessions with full MCP context. |
| **Encryption** | Encrypt secrets (`#password`, `#api_token`) via Keboola Encryption API. Works with sync push and MCP. |
| **Permissions** | Firewall for AI agents: read-only, deny-writes, deny-destructive (session-only flags or persisted policy). Project pin + `KBAGENT_PROJECT` env override. Code-level enforcement, stable `ErrorCode` enum, not prompt tricks. |
| **Auto-update** | Self-updates on startup. "What's new" after each update. Full changelog via `kbagent changelog`. |

## Setup options

Three ways to register projects, depending on what you have.

**Single project** — you have a Storage API token from the UI:
```bash
kbagent project add --project prod --url https://connection.keboola.com --token YOUR_TOKEN
```

**Many projects by ID** — you have a Manage API or Personal Access Token + the project IDs:
```bash
KBC_MANAGE_API_TOKEN=your-manage-or-personal-token \
  kbagent org setup --project-ids 901,9621,10539 --url https://connection.keboola.com --yes
```

**Whole organization** — you are org admin:
```bash
KBC_MANAGE_API_TOKEN=your-org-admin-manage-token \
  kbagent org setup --org-id 123 --url https://connection.keboola.com --yes
```

Run `kbagent doctor` to verify setup (token validity, CLI version, MCP server, Claude Code plugin install).

> **Step-by-step guide with dry-runs, token descriptions, expiry, and
> global-vs-local config:** see [docs/TUTORIAL.md](docs/TUTORIAL.md).

## All commands

Full command reference with flags: [SKILL.md](plugins/kbagent/skills/kbagent/SKILL.md)

```
kbagent project     add | list | remove | edit | status | refresh | use | current
kbagent org         setup
kbagent component   list | detail
kbagent config      list | detail | search | update | set-default-bucket | rename | delete | new
                    metadata-list | get-metadata | set-metadata | delete-metadata | set-folder
                    variables-set | variables-get | variables-clear
kbagent job         list | detail | run | terminate
kbagent flow        list | detail | schema | new | update | delete | schedule | schedule-remove
kbagent storage     buckets | bucket-detail | create-bucket | delete-bucket
                    tables | table-detail | create-table | upload-table | download-table | delete-table | delete-column
                    describe-bucket | describe-table | describe-column | describe-batch
                    files | file-detail | file-upload | file-download | file-tag | file-delete
                    load-file | unload-table
kbagent sharing     list | share | unshare | link | unlink | edges
kbagent data-app    list | detail | create | deploy | start | stop | delete | password
kbagent lineage     build | show | info | server
kbagent branch      list | create | use | reset | delete | merge
                    metadata-list | metadata-get | metadata-set | metadata-delete
kbagent workspace   create | list | detail | delete | password | load | query | from-transformation | gc
kbagent tool        list | call
kbagent sync        init | pull | status | diff | push | branch-link | branch-unlink | branch-status
kbagent kai         ping | ask | chat | history
kbagent encrypt     values
kbagent permissions list | show | set | reset | check
kbagent             init | context | doctor | version | update | changelog

# Global flags: --json, --verbose, --no-color, --config-dir, --hint client|service
#               --deny-writes, --deny-destructive (session-only firewall)
```

## Documentation

| Guide | What it covers |
|-------|---------------|
| [Tutorial](docs/TUTORIAL.md) | End-to-end walkthrough: register projects (1, N, whole org), global vs local config, plugin install, using the specialist subagent and `/keboola` slash command. |
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
