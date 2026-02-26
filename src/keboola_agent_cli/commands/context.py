"""Context command - provides comprehensive usage instructions for AI agents.

Outputs a curated text block that any AI agent (Claude, Codex, Gemini, etc.)
can consume to understand how to use kbagent effectively.
"""

import typer

from .. import __version__
from ..output import OutputFormatter

AGENT_CONTEXT = f"""\
# kbagent - Keboola Agent CLI v{__version__}

## What is kbagent?

kbagent is an AI-friendly CLI for managing Keboola projects. It allows you to:
- Connect to multiple Keboola projects across different stacks
- List and inspect configurations (extractors, writers, transformations, applications)
- Check connectivity and health of project connections
- Get structured JSON output suitable for programmatic consumption

## Quick Start

1. Add a project connection:
   kbagent project add --alias my-project --url https://connection.keboola.com --token YOUR_TOKEN

2. List connected projects:
   kbagent project list

3. List configurations:
   kbagent config list

## All Commands

### Project Management

  kbagent project add --alias NAME --url STACK_URL --token TOKEN
    Add a new Keboola project connection. The token is verified against the API.
    Example:
      kbagent --json project add --alias prod --url https://connection.keboola.com --token 901-xxxxx

  kbagent project list
    List all connected projects with their details (tokens are always masked).
    Example:
      kbagent --json project list

  kbagent project remove --alias NAME
    Remove a project connection.
    Example:
      kbagent --json project remove --alias prod

  kbagent project edit --alias NAME [--url NEW_URL] [--token NEW_TOKEN]
    Edit an existing project. If token changes, it is re-verified via API.
    Example:
      kbagent --json project edit --alias prod --url https://connection.north-europe.azure.keboola.com

  kbagent project status [--project NAME]
    Test connectivity to projects. Shows OK/ERROR with response time.
    Example:
      kbagent --json project status
      kbagent --json project status --project prod

### Configuration Browsing

  kbagent config list [--project NAME] [--component-type TYPE] [--component-id ID]
    List configurations from one, many, or all connected projects.
    --project can be repeated to query multiple projects.
    --component-type: extractor, writer, transformation, application
    --component-id: specific component (e.g. keboola.ex-db-snowflake)
    Examples:
      kbagent --json config list
      kbagent --json config list --project prod
      kbagent --json config list --project prod --project dev
      kbagent --json config list --component-type extractor
      kbagent --json config list --component-id keboola.ex-db-snowflake

  kbagent config detail --project NAME --component-id ID --config-id ID
    Show full detail of a specific configuration including parameters and rows.
    Example:
      kbagent --json config detail --project prod --component-id keboola.ex-db-snowflake --config-id 12345

### Utility Commands

  kbagent context
    Show this help text with usage instructions for AI agents.

  kbagent doctor
    Run health checks: config file, permissions, connectivity, CLI version.
    Example:
      kbagent --json doctor

## Global Flags

  --json / -j     Output in JSON format (always use this for programmatic parsing)
  --verbose / -v  Enable verbose output
  --no-color      Disable colored output (auto-disabled in non-TTY environments)

## Tips for AI Agents

1. ALWAYS use --json flag for reliable, parseable output:
     kbagent --json project list
     kbagent --json config list

2. JSON success response format:
     {{"status": "ok", "data": ...}}

3. JSON error response format:
     {{"status": "error", "error": {{"code": "ERROR_CODE", "message": "...", "retryable": true/false}}}}

4. Check the "retryable" field in errors - if true, you can retry the operation.

5. Tokens are always masked in output (e.g. 901-...pt0k) - this is expected behavior.

6. Common workflow - explore a project:
     kbagent --json project list                              # See all projects
     kbagent --json config list --project prod                # List all configs
     kbagent --json config list --project prod --component-type extractor  # Filter by type
     kbagent --json config detail --project prod --component-id keboola.ex-db-snowflake --config-id 12345

7. Common workflow - check health:
     kbagent --json doctor                                    # Full health check
     kbagent --json project status                            # Test all connections

8. Environment variables:
     KBC_TOKEN             - Default Storage API token
     KBC_STORAGE_API_URL   - Default stack URL

## Exit Codes

  0  Success
  1  General error
  2  Usage error (invalid arguments)
  3  Authentication error (invalid or expired token)
  4  Network error (timeout, unreachable server)
  5  Configuration error (corrupt config file, missing project alias)

When you receive a non-zero exit code, use --json to get structured error details.
"""


def _get_formatter(ctx: typer.Context) -> OutputFormatter:
    """Retrieve the OutputFormatter from the Typer context."""
    return ctx.obj["formatter"]


def context_command(ctx: typer.Context) -> None:
    """Show usage instructions for AI agents interacting with Keboola."""
    formatter = _get_formatter(ctx)

    if formatter.json_mode:
        # In JSON mode, output the context text as structured data
        data = {
            "version": __version__,
            "context": AGENT_CONTEXT,
        }
        formatter.output(data)
    else:
        formatter.console.print(AGENT_CONTEXT)
