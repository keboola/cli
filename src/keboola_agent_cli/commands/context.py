"""Context command - provides comprehensive usage instructions for AI agents."""

import typer

from .. import __version__
from ..output import OutputFormatter

CONTEXT_TEXT = f"""\
# kbagent - Keboola Agent CLI v{__version__}

You are interacting with `kbagent`, an AI-friendly command-line interface for
managing Keboola projects. This tool is designed to be used by AI agents
(Claude, Codex, Gemini, and others) as well as human operators.

## Key Principle

Always use the `--json` flag when calling kbagent programmatically. This
ensures structured, machine-parseable output that is easy to process.

---

## Available Commands

### Project Management

```bash
# List all connected projects
kbagent --json project list

# Add a new project connection (verifies token via API)
kbagent --json project add --alias my-project --url https://connection.keboola.com --token YOUR_TOKEN

# Remove a project connection
kbagent --json project remove --alias my-project

# Edit a project (update URL, token, or both)
kbagent --json project edit --alias my-project --url https://new.stack.url
kbagent --json project edit --alias my-project --token NEW_TOKEN

# Check connectivity status of all projects
kbagent --json project status

# Check connectivity of a specific project
kbagent --json project status --project my-project
```

### Configuration Browsing

```bash
# List all configurations across all projects
kbagent --json config list

# List configurations from a specific project
kbagent --json config list --project my-project

# List configurations from multiple projects
kbagent --json config list --project proj-a --project proj-b

# Filter by component type (extractor, writer, transformation, application)
kbagent --json config list --component-type extractor

# Filter by specific component ID
kbagent --json config list --component-id keboola.ex-db-snowflake

# Get full detail of a specific configuration
kbagent --json config detail --project my-project --component-id keboola.ex-db-snowflake --config-id 12345
```

### Diagnostics

```bash
# Run health checks (config file, connectivity, version)
kbagent --json doctor

# Show these instructions
kbagent context
```

---

## Global Flags

| Flag          | Short | Description                                    |
|---------------|-------|------------------------------------------------|
| `--json`      | `-j`  | Output structured JSON (recommended for agents) |
| `--verbose`   | `-v`  | Enable verbose output                          |
| `--no-color`  |       | Disable colored/Rich output                    |

---

## JSON Output Format

### Success Response

```json
{{
  "status": "ok",
  "data": [ ... ]
}}
```

### Error Response

```json
{{
  "status": "error",
  "error": {{
    "code": "INVALID_TOKEN",
    "message": "Token is invalid or expired",
    "project": "my-project",
    "retryable": false
  }}
}}
```

---

## Exit Codes

| Code | Meaning                                          |
|------|--------------------------------------------------|
| 0    | Success                                          |
| 1    | General error                                    |
| 2    | Usage error (bad arguments, missing flags)       |
| 3    | Authentication error (invalid/expired token)     |
| 4    | Network error (timeout, unreachable server)      |
| 5    | Configuration error (corrupted config file, missing alias) |

---

## Common Workflows

### 1. Set up a new project

```bash
kbagent --json project add --alias prod --url https://connection.keboola.com --token 901-xxxxx
```

Parse the response to confirm the project was added:
```bash
kbagent --json project list
```

### 2. Explore configurations

```bash
# Get all extractors
kbagent --json config list --component-type extractor

# Get details of a specific config
kbagent --json config detail --project prod --component-id keboola.ex-db-snowflake --config-id 12345
```

### 3. Verify everything is working

```bash
kbagent --json doctor
```

### 4. Multi-project operations

```bash
# Compare configurations across environments
kbagent --json config list --project prod
kbagent --json config list --project staging
```

---

## Tips for AI Agents

1. **Always use `--json`**: Raw JSON is easier to parse than Rich-formatted tables.
2. **Check exit codes**: Non-zero exit codes indicate errors. Use the exit code to determine the type of failure.
3. **Parse the `status` field**: Every JSON response has `"status": "ok"` or `"status": "error"`.
4. **Tokens are masked**: Token values in output are always masked (e.g., `901-...pt0k`). Never attempt to extract full tokens from output.
5. **Error responses include `retryable`**: If `retryable` is `true`, you can safely retry the operation.
6. **Use `kbagent doctor`** to verify the setup before performing operations.
7. **Project aliases are case-sensitive**: Use consistent casing when referring to projects.

---

## Environment Variables

| Variable              | Description                          |
|-----------------------|--------------------------------------|
| `KBC_TOKEN`           | Default Storage API token            |
| `KBC_STORAGE_API_URL` | Default Keboola stack URL            |

These can be used as fallbacks when `--token` or `--url` flags are not provided
to `project add`.
"""


def _get_formatter(ctx: typer.Context) -> OutputFormatter:
    """Retrieve the OutputFormatter from the Typer context."""
    return ctx.obj["formatter"]


def context_command(ctx: typer.Context) -> None:
    """Show usage instructions for AI agents interacting with Keboola."""
    formatter = _get_formatter(ctx)

    def _human_output(console, data: str) -> None:  # type: ignore[no-untyped-def]
        from rich.markdown import Markdown

        console.print(Markdown(data))

    formatter.output(CONTEXT_TEXT, _human_output)
