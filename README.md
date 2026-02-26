# Keboola Agent CLI (`kbagent`)

AI-friendly CLI for managing Keboola projects. Designed for use by AI coding agents (Claude Code, Codex, Gemini) and human developers alike.

## Features

- **Multi-project management**: Connect to multiple Keboola projects across different stacks (AWS, Azure, GCP)
- **Configuration browsing**: List and inspect extractors, writers, transformations, and applications
- **Structured JSON output**: Every command supports `--json` for reliable programmatic parsing
- **Health checks**: Built-in `doctor` command to verify setup and connectivity
- **Agent context**: `kbagent context` provides comprehensive usage instructions for AI agents
- **Secure token handling**: Tokens are stored with 0600 permissions and always masked in output

## Installation

```bash
# Install with uv (recommended)
uv tool install .

# Or install in development mode
uv pip install -e ".[dev]"
```

After installation, the `kbagent` command is available globally.

## Quick Start

```bash
# 1. Add a Keboola project
kbagent project add --alias prod --url https://connection.keboola.com --token YOUR_TOKEN

# 2. Verify the connection
kbagent project status

# 3. List configurations
kbagent config list

# 4. Get structured JSON output (recommended for scripts and agents)
kbagent --json config list
```

## Commands

### Project Management

```bash
# Add a new project connection (token is verified against API)
kbagent project add --alias NAME --url STACK_URL --token TOKEN

# List all connected projects
kbagent project list

# Remove a project connection
kbagent project remove --alias NAME

# Edit an existing project (re-verifies token if changed)
kbagent project edit --alias NAME [--url NEW_URL] [--token NEW_TOKEN]

# Check connectivity to all projects (or a specific one)
kbagent project status
kbagent project status --project NAME
```

### Configuration Browsing

```bash
# List all configurations from all projects
kbagent config list

# Filter by project (can be repeated)
kbagent config list --project prod
kbagent config list --project prod --project dev

# Filter by component type
kbagent config list --component-type extractor

# Filter by specific component
kbagent config list --component-id keboola.ex-db-snowflake

# Get full detail of a specific configuration
kbagent config detail --project prod --component-id keboola.ex-db-snowflake --config-id 12345
```

### Utility Commands

```bash
# Show usage instructions for AI agents
kbagent context

# Run health checks (config, permissions, connectivity, version)
kbagent doctor
kbagent --json doctor
```

### Global Flags

| Flag | Short | Description |
|------|-------|-------------|
| `--json` | `-j` | Output in JSON format for programmatic consumption |
| `--verbose` | `-v` | Enable verbose output |
| `--no-color` | | Disable colored Rich formatting |

Non-TTY environments automatically disable Rich formatting.

## JSON Output Format

All commands with `--json` return a consistent structure.

**Success:**
```json
{
  "status": "ok",
  "data": { ... }
}
```

**Error:**
```json
{
  "status": "error",
  "error": {
    "code": "INVALID_TOKEN",
    "message": "Token is invalid or expired",
    "project": "prod",
    "retryable": false
  }
}
```

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error |
| 2 | Usage error (invalid arguments) |
| 3 | Authentication error (invalid/expired token) |
| 4 | Network error (timeout, unreachable server) |
| 5 | Configuration error (corrupt config, unknown alias) |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `KBC_TOKEN` | Default Storage API token (used by `project add`) |
| `KBC_STORAGE_API_URL` | Default stack URL (used by `project add`) |

## Configuration

Configuration is stored at `~/.config/keboola-agent-cli/config.json` with file permissions `0600` to protect stored tokens.

```json
{
  "version": 1,
  "default_project": "prod",
  "projects": {
    "prod": {
      "stack_url": "https://connection.keboola.com",
      "token": "901-...",
      "project_name": "My Project",
      "project_id": 1234
    }
  }
}
```

## Architecture

The project follows a 3-layer architecture:

```
CLI Commands (commands/)  -->  Services (services/)  -->  API Client (client.py)
```

- **Commands layer**: Thin Typer wrappers that parse arguments, call services, and format output
- **Services layer**: Business logic, project resolution, multi-project aggregation
- **Client layer**: HTTP communication with Keboola API, retry logic, error mapping

## Development

```bash
# Install in development mode
uv pip install -e ".[dev]"

# Run tests
uv run pytest tests/ -v

# Run the CLI
uv run kbagent --help
```

## Supported Keboola Stacks

- AWS: `https://connection.keboola.com`
- Azure (North Europe): `https://connection.north-europe.azure.keboola.com`
- GCP (Europe West): `https://connection.europe-west3.gcp.keboola.com`

## License

MIT
