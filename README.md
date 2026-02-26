# kbagent - Keboola Agent CLI

AI-friendly command-line interface for managing Keboola projects. Designed for
use by AI coding agents (Claude, Codex, Gemini) and human operators alike.

## Features

- **Multi-project management** -- connect to multiple Keboola stacks and projects
- **AI-optimized output** -- structured JSON output with `--json` flag for easy parsing
- **Configuration browsing** -- list and inspect configurations across projects
- **Health diagnostics** -- built-in `doctor` command to verify setup
- **Self-documenting** -- `context` command provides comprehensive usage instructions for AI agents

## Installation

### With uv (recommended)

```bash
uv tool install .
```

### With pip

```bash
pip install .
```

### Development install

```bash
uv pip install -e ".[dev]"
```

After installation, the `kbagent` command is available globally.

## Quick Start

### 1. Add a project

```bash
kbagent project add \
  --alias prod \
  --url https://connection.keboola.com \
  --token YOUR_STORAGE_API_TOKEN
```

### 2. List connected projects

```bash
kbagent project list
```

### 3. Check connectivity

```bash
kbagent project status
```

### 4. Browse configurations

```bash
kbagent config list --project prod
```

### 5. Run health check

```bash
kbagent doctor
```

## Commands

### Project Management

| Command | Description |
|---------|-------------|
| `kbagent project add --alias NAME --url URL --token TOKEN` | Add a new project connection |
| `kbagent project list` | List all connected projects |
| `kbagent project remove --alias NAME` | Remove a project connection |
| `kbagent project edit --alias NAME [--url URL] [--token TOKEN]` | Edit a project |
| `kbagent project status [--project NAME]` | Test connectivity |

### Configuration Browsing

| Command | Description |
|---------|-------------|
| `kbagent config list [--project NAME] [--component-type TYPE] [--component-id ID]` | List configurations |
| `kbagent config detail --project NAME --component-id ID --config-id ID` | Show configuration details |

### Diagnostics

| Command | Description |
|---------|-------------|
| `kbagent context` | Show AI agent usage instructions |
| `kbagent doctor` | Run health checks |

### Global Flags

| Flag | Short | Description |
|------|-------|-------------|
| `--json` | `-j` | Output structured JSON |
| `--verbose` | `-v` | Enable verbose output |
| `--no-color` | | Disable colored output |

## JSON Output

All commands support `--json` for structured output.

**Success:**

```json
{
  "status": "ok",
  "data": [ ... ]
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
| 2 | Usage error (bad arguments) |
| 3 | Authentication error (invalid/expired token) |
| 4 | Network error (timeout, unreachable server) |
| 5 | Configuration error (corrupt config, missing alias) |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `KBC_TOKEN` | Default Storage API token (fallback for `--token`) |
| `KBC_STORAGE_API_URL` | Default Keboola stack URL (fallback for `--url`) |

## Architecture

The project follows a 3-layer architecture:

```
CLI commands  -->  Services  -->  API Client
(commands/)       (services/)     (client.py)
```

- **Commands** -- thin Typer layer, parses arguments, formats output
- **Services** -- business logic, aggregation, validation
- **Client** -- HTTP communication with Keboola Storage API (retry, timeouts)

Configuration is stored at `~/.config/keboola-agent-cli/config.json` with
`0600` permissions. Tokens are always masked in output.

## Development

```bash
# Install in development mode
uv pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Run a specific test file
pytest tests/test_cli.py -v
```

## License

MIT
