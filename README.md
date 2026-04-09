# kbagent -- Keboola Agent CLI

One CLI to manage all your Keboola projects. Bring your own AI agent -- Claude Code, Codex, Gemini -- and turn it into a Keboola power user that handles complex tasks for you: auditing projects, debugging failed jobs, optimizing transformations, tracing data lineage across dozens of projects.

kbagent is the bridge between your AI and Keboola. It respects real engineering workflows -- dev branches, git sync, pull requests -- so the agent works the way your team already works.

```bash
# Install
uv tool install git+https://github.com/padak/keboola_agent_cli

# Connect a project
kbagent project add --project prod --url https://connection.keboola.com --token YOUR_TOKEN

# Or onboard an entire organization at once
KBC_MANAGE_API_TOKEN=xxx kbagent org setup --org-id 123 --url https://connection.keboola.com --yes
```

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
kbagent storage   buckets | bucket-detail | tables
kbagent sharing   list | share | unshare | link | unlink
kbagent lineage   show
kbagent branch    list | create | use | reset | delete | merge
kbagent workspace create | list | detail | delete | password | load | query | from-transformation
kbagent tool      list | call
kbagent sync      init | pull | status | diff | push | branch-link | branch-unlink | branch-status
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

## Development

```bash
git clone https://github.com/padak/keboola_agent_cli.git && cd keboola_agent_cli
make install   # uv pip install -e ".[dev]"
make check     # lint + format + test
make hooks     # install pre-commit hook
```

## License

MIT
