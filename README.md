# Keboola Agent CLI (`kbagent`)

A CLI for managing multiple Keboola projects from one place. Built for AI coding agents (Claude Code, Codex, Gemini) and human operators who need to work across many projects at once.

## Why does this exist?

Keboola's web UI and standard API clients work great for a single project. But when you manage 30+ projects across an organization, you need a way to:

- **Query all projects at once** -- list configurations, jobs, or data lineage across the entire organization in one command
- **Give AI agents access to Keboola** -- every command supports `--json` output that agents can parse reliably
- **Onboard an entire organization** -- register all projects from a Keboola org with a single `org setup` command
- **Trace data flow** -- see how data moves between projects via bucket sharing (lineage)
- **Use MCP tools** -- call keboola-mcp-server tools across all projects in parallel
- **Manage dev branches** -- create, switch, delete branches with persistent active branch state
- **Debug SQL interactively** -- create workspaces, load tables, run SQL queries without full job runs

See [real-world use cases](docs/use-cases.md) for end-to-end examples with AI agents.

## What it can do

**Setup & Info**

| Command | What it does |
|---------|-------------|
| `init` | Initialize a local `.kbagent/` workspace for per-directory project isolation |
| `doctor` | Health check -- verifies config, permissions, connectivity, MCP server availability |
| `version` | Show kbagent version and check for dependency updates |
| `context` | Print comprehensive usage instructions for AI agents |

**Project Management**

| Command | What it does |
|---------|-------------|
| `project` | Add, remove, edit, list, and check status of connected Keboola projects |
| `org setup` | Bulk-onboard all projects from a Keboola organization (uses Manage API) |

**Browse & Inspect**

| Command | What it does |
|---------|-------------|
| `config` | List, detail, search, update, delete configurations; generate new config scaffolds |
| `component` | Discover and search Keboola components (AI-powered search, schema inspection) |
| `job` | Browse job history -- list and inspect jobs from the Queue API |
| `storage` | Browse buckets and tables with Snowflake path resolution for shared buckets |
| `lineage` | Analyze cross-project data flow via bucket sharing |

**Development**

| Command | What it does |
|---------|-------------|
| `branch` | Full branch lifecycle -- create, switch, reset, delete dev branches; get merge URL |
| `workspace` | Create workspaces, load tables, run SQL queries -- iterative SQL debugging |
| `tool` | List and call MCP tools from keboola-mcp-server (read tools run in parallel) |
| `sync` | Sync project configurations to local filesystem; push with auto-encryption of secrets |

Every command supports `--json` for structured output and Rich formatting for human-readable output.

Run `kbagent --help` or `kbagent <command> --help` for details on any command.

## Installation

```bash
# Install from GitHub (recommended)
uv tool install git+https://github.com/padak/keboola_agent_cli

# Update to latest version
uv tool install --reinstall git+https://github.com/padak/keboola_agent_cli

# Run without installing (one-off use)
uvx --from git+https://github.com/padak/keboola_agent_cli kbagent --help
```

For development:

```bash
# Clone and install in editable mode
git clone https://github.com/padak/keboola_agent_cli.git
cd keboola_agent_cli
uv pip install -e ".[dev]"
```

After `uv tool install`, `kbagent` is available globally in your shell.

## Configuration and credentials

### Where is everything stored?

All configuration lives in a single file (permissions: `0600`):

- **macOS:** `~/Library/Application Support/keboola-agent-cli/config.json`
- **Linux:** `~/.config/keboola-agent-cli/config.json`

This file contains **Storage API tokens** for each connected project. Tokens are always masked in CLI output (e.g. `901-...pt0k`).

### Workspace isolation (per-client directories)

By default kbagent uses a single global config. For teams working with multiple clients, you can create **per-directory workspaces** so each client folder has its own isolated set of Keboola projects.

```bash
# Create a workspace for ACME client
mkdir -p ~/clients/acme && cd ~/clients/acme
kbagent init                    # creates .kbagent/config.json
kbagent project add --project acme-prod --url https://connection.keboola.com --token ...

# Create a workspace for BigCorp client
mkdir -p ~/clients/bigcorp && cd ~/clients/bigcorp
kbagent init                    # separate .kbagent/config.json
kbagent project add --project bigcorp-main --url https://connection.eu-central-1.keboola.com --token ...
```

Now when you `cd ~/clients/acme` and run `kbagent project list`, you only see ACME's projects. Each client directory can have its own `CLAUDE.md` with project-specific instructions.

**Config resolution order** (first match wins):
1. `--config-dir` CLI flag
2. `KBAGENT_CONFIG_DIR` environment variable
3. `.kbagent/config.json` in current or parent directory (walks up, like git)
4. `~/.config/keboola-agent-cli/config.json` (global default)

Run `kbagent doctor` to see which config is active. Use `kbagent init --from-global` to copy existing projects to a local workspace.

The config file structure:

```json
{
  "version": 1,
  "default_project": "prod",
  "max_parallel_workers": 10,
  "projects": {
    "prod": {
      "stack_url": "https://connection.keboola.com",
      "token": "901-xxxxx-xxxxxxx",
      "project_name": "Production",
      "project_id": 1234
    }
  }
}
```

### Manage API token is never stored

The `org setup` command requires a Manage API token to list organization projects and create Storage API tokens. This token is **never persisted** to disk -- it is either read from the `KBC_MANAGE_API_TOKEN` environment variable or prompted interactively (hidden input). It is never passed as a CLI argument and never logged.

### Parallel execution

All multi-project read operations (`config list`, `job list`, `project status`, `lineage show`, `tool call` for read tools) run in parallel. The concurrency is configurable:

**HTTP operations** (config, job, lineage, project status):

| Priority | Method | Example |
|----------|--------|---------|
| 1 (highest) | Environment variable | `KBAGENT_MAX_PARALLEL_WORKERS=20 kbagent lineage show` |
| 2 | Config file | `"max_parallel_workers": 20` in config.json |
| 3 (default) | Built-in default | `10` |

**MCP tool calls** (`tool call`, `tool list`):

By default, all MCP sessions run fully in parallel (one subprocess per project). If you hit OS resource limits on machines with many projects (50+), throttle with:

```bash
KBAGENT_MCP_MAX_SESSIONS=10 kbagent tool call get_buckets
```

## Development branches

Work on Keboola dev branches without passing `--branch` to every command:

```bash
# Create a branch -- auto-activates it for the project
kbagent branch create --project prod --name "fix-transform-x"

# All subsequent tool calls use the active branch automatically
kbagent tool call get_configs --project prod
kbagent tool call update_sql_transformation --project prod --input '{...}'

# When done, get the merge URL (opens KBC UI for safe review)
kbagent branch merge --project prod
```

The active branch is stored per-project in `config.json` and displayed in `project list` and `branch list` output. Use `branch reset` to switch back to main, or `branch delete` to remove a branch (auto-resets if it was active).

## Workspaces (SQL debugging)

Debug a failing SQL transformation without running full jobs:

```bash
# Option A: Create workspace from an existing transformation (auto-loads input tables)
kbagent --json workspace from-transformation --project prod \
  --component-id keboola.snowflake-transformation --config-id 22777254

# Option B: Create a standalone workspace and load tables manually
kbagent workspace create --project prod --name "debug-ws"
kbagent workspace load --project prod --workspace-id WS_ID \
  --tables in.c-main.users --tables in.c-main.orders

# Run SQL queries iteratively (no need to run full jobs!)
kbagent --json workspace query --project prod --workspace-id WS_ID \
  --sql "SELECT * FROM users LIMIT 10"

# Or run SQL from a file
kbagent --json workspace query --project prod --workspace-id WS_ID --file fix.sql

# Clean up when done (workspaces also expire automatically)
kbagent workspace delete --project prod --workspace-id WS_ID
```

Two create modes:
- **Default (headless):** fast ~1s via Storage API, not visible in Keboola UI
- **`--ui` flag:** ~15s via Queue job, visible in Keboola UI Workspaces tab

```bash
# Visible in Keboola UI
kbagent workspace create --project prod --name "debug-ws" --ui
```

## Creating new configurations

Generate boilerplate config files for any Keboola component using AI-powered schema and example discovery:

```bash
# Find the right component
kbagent component list --query "snowflake extractor" --project prod

# Inspect component schema and documentation
kbagent --json component detail --component-id keboola.ex-db-snowflake --project prod

# Generate scaffold files (auto-detects kbc project structure)
kbagent config new --component-id keboola.ex-db-snowflake --project prod --name "My Import" --output-dir .
# Creates: main/extractor/keboola.ex-db-snowflake/my-import/_config.yml

# Edit the generated config -- fill in credentials, adjust parameters
# Secret fields (#password) start as <YOUR_SECRET> placeholder

# Push to Keboola (secrets auto-encrypted, config_id auto-assigned)
kbagent sync push --project prod
```

For transformations, the scaffold also generates code files:
- SQL transformations: `_config.yml` + `transform.sql`
- Python transformations: `_config.yml` + `transform.py` + `pyproject.toml`
- Custom Python apps: `_config.yml` + `code.py` + `pyproject.toml`
- Flows/orchestrators: `_config.yml` with phases, tasks, and schedule skeleton

## Sync pull (project snapshot)

Download complete project snapshots to your local filesystem -- configurations, storage metadata, job history, and data samples:

```bash
# Pull all projects (includes configs, storage metadata, per-config jobs)
kbagent sync pull --all-projects

# Include CSV data samples from tables
kbagent sync pull --all-projects --with-samples

# Customize
kbagent sync pull --project prod --job-limit 10       # more job history per config
kbagent sync pull --project prod --no-storage --no-jobs # configs only (faster)
```

What gets pulled:

| Path | Content |
|------|---------|
| `main/*/_config.yml` | Configuration parameters (YAML) |
| `main/*/_description.md` | Human descriptions |
| `main/*/_jobs.jsonl` | Recent jobs per config (status, timing, errors) |
| `main/*/transform.sql` | SQL code for Snowflake transformations |
| `storage/buckets.json` | All buckets with metadata |
| `storage/tables/{bucket}/{table}.json` | Table schemas, columns, row counts, sizes |
| `storage/samples/...` | CSV data previews (opt-in: `--with-samples`) |

Tables with >30 columns export only the first 30 (Storage API sync export limit). Encrypted columns are masked as `***ENCRYPTED***` in samples.

## JSON output format

All commands with `--json` return a consistent structure:

```json
{"status": "ok", "data": { ... }}
{"status": "error", "error": {"code": "INVALID_TOKEN", "message": "...", "retryable": false}}
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error |
| 2 | Usage error (invalid arguments) |
| 3 | Authentication error (invalid/expired token) |
| 4 | Network error (timeout, unreachable) |
| 5 | Configuration error |

## Supported Keboola stacks

Works with any Keboola stack -- AWS, Azure, GCP. Examples:

- `https://connection.keboola.com` (US, AWS)
- `https://connection.north-europe.azure.keboola.com` (Azure)
- `https://connection.europe-west3.gcp.keboola.com` (GCP)
- `https://connection.us-east4.gcp.keboola.com` (GCP)

## Claude Code plugin

This repo doubles as a [Claude Code](https://docs.anthropic.com/en/docs/claude-code) plugin marketplace. Install the `kbagent` skill to teach Claude how to use the CLI effectively:

```bash
claude install-plugin https://github.com/padak/keboola_agent_cli
```

Once installed, Claude automatically recognizes Keboola-related tasks and knows how to use `kbagent` commands, including workspace SQL debugging, dev branch workflows, and multi-project operations. The skill loads `kbagent context` dynamically so documentation stays in sync with the installed CLI version.

## Development

```bash
uv pip install -e ".[dev]"
uv run pytest tests/ -v
uv run kbagent --help
```

A `Makefile` provides shortcuts for common development tasks:

```bash
make help           # show all available targets
make install        # install in dev mode
make test           # run all tests
make test-unit      # run unit tests only
make lint           # run ruff linter
make format         # format code with ruff
make hooks          # install pre-commit hook (lint + format)
make check          # run lint + format-check + test (CI-like)
make clean          # remove caches and build artifacts
```

## Contributors

- [Jordan Burger](https://github.com/jordanrburger)
- [František Řehoř](https://github.com/frantisekrehor)
- [Vojta Tůma](https://github.com/yustme)

## License

MIT
