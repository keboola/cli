# MCP Tools Workflow

MCP tools let you interact with Keboola components directly -- creating configs,
running components, fetching schemas -- through the keboola-mcp-server.

## Two types of tools

Every tool has a `multi_project` flag (visible in `tool list` output):

- **multi_project=true** (read tools): Runs across ALL connected projects in parallel.
  No `--project` needed. Examples: `get_configs`, `get_buckets`, `get_tables`.
- **multi_project=false** (write tools): Targets a single project. Requires `--project`
  (or a default project). Examples: `create_config`, `update_configuration`, `run_component`.

## Basic usage

```bash
# List all available MCP tools
kbagent --json tool list

# Read tool -- queries ALL projects automatically
kbagent --json tool call list_configs

# Read tool -- scope to one project
kbagent --json tool call list_configs --project prod

# Write tool -- must specify project
kbagent --json tool call create_config --project prod \
  --input '{"component_id": "keboola.ex-db-snowflake", "name": "My Extract"}'

# Get specific config details (note: configs takes a list of {component_id, configuration_id})
kbagent --json tool call get_configs \
  --input '{"configs": [{"component_id": "keboola.snowflake-transformation", "configuration_id": "12345"}]}'
```

## Discovering tool parameters

Each MCP tool has an `inputSchema` that defines accepted parameters. To see
the schema for a specific tool, use `tool list` with `--json`:

```bash
# List all tools with their input schemas
kbagent --json tool list | jq '.data.tools[] | select(.name == "get_configs") | .inputSchema'
```

**Important**: Only pass parameters defined in the tool's `inputSchema` via `--input`.
Unexpected parameters cause Pydantic validation errors on the MCP server side.

## Input format

Pass tool parameters as a JSON object via `--input`. Three formats are supported:

```bash
# Inline JSON
kbagent --json tool call update_configuration --project prod \
  --input '{"component_id": "keboola.snowflake-transformation", "configuration_id": "123", "configuration": {"parameters": {...}}}'

# From file (for large payloads that exceed shell argument limits)
kbagent --json tool call update_sql_transformation --project prod \
  --input @payload.json

# From stdin (pipe-friendly)
cat payload.json | kbagent --json tool call update_sql_transformation --project prod --input -
```

Input is validated against the tool's `inputSchema` before execution.

## Branch support

Use the CLI flag `--branch ID` to scope tool calls to a development branch:

```bash
# List tools available on a branch
kbagent --json tool list --project prod --branch 456

# Call a tool on a branch
kbagent --json tool call get_configs --project prod --branch 456 \
  --input '{"configs": [{"component_id": "keboola.snowflake-transformation", "configuration_id": "12345"}]}'
```

- `--branch` is a **CLI flag**, not a tool input parameter. Do NOT pass `branch_id`
  inside `--input` JSON -- it will be rejected as an unexpected keyword argument.
- `--branch` requires `--project` (forces single-project mode)
- If a branch is active via `kbagent branch use`, it is applied automatically --
  no need to pass `--branch` manually

## When to use MCP tools vs native commands

| Task | Use |
|------|-----|
| List configs/jobs across all projects | Native: `config list`, `job list` |
| Job history, error details | Native: `job list`, `job detail` |
| Workspace SQL debugging | Native: `workspace` commands |
| Sync configs to/from disk | Native: `sync` commands |
| Create/update configs with full parameters | MCP: `create_config`, `update_configuration` |
| Run a component | MCP: `run_component` |
| Get component input schema | MCP: `get_component` |
| Data preview / table samples | MCP: `retrieve_data` |
| Bucket/table metadata operations | MCP: `create_bucket`, `create_table` |

**Rule of thumb**: use native commands for cross-project overview and iteration;
use MCP tools when you need full control over component parameters or need
Keboola operations not covered by native commands.
