"""Context command - provides comprehensive usage instructions for AI agents.

Outputs a curated text block that any AI agent (Claude, Codex, Gemini, etc.)
can consume to understand how to use kbagent effectively.
"""

import typer

from .. import __version__
from ._helpers import get_formatter

AGENT_CONTEXT = f"""\
# kbagent - Keboola Agent CLI v{__version__}

## What is kbagent?

kbagent is an AI-friendly CLI for managing Keboola projects. It allows you to:
- Connect to multiple Keboola projects across different stacks
- List and inspect configurations (extractors, writers, transformations, applications)
- Browse job history (running, succeeded, failed jobs)
- Check connectivity and health of project connections
- Analyze cross-project data lineage via bucket sharing
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

### Job History

  kbagent job list [--project NAME] [--component-id ID] [--config-id ID] [--status STATUS] [--limit N]
    List jobs from the Queue API across one, many, or all connected projects.
    --project can be repeated to query multiple projects.
    --status: processing, terminated, cancelled, success, error
    --limit: 1-500 (default 50)
    --config-id requires --component-id
    Examples:
      kbagent --json job list
      kbagent --json job list --project prod
      kbagent --json job list --project prod --project dev
      kbagent --json job list --status error
      kbagent --json job list --component-id keboola.ex-db-snowflake --limit 10
      kbagent --json job list --component-id keboola.ex-db-snowflake --config-id 12345

  kbagent job detail --project NAME --job-id ID
    Show full detail of a specific job including result message and timing.
    Example:
      kbagent --json job detail --project prod --job-id 148512262

### Data Lineage

  kbagent lineage [--project NAME]
    Analyze cross-project data lineage via bucket sharing.
    Shows which projects share buckets to other projects -- essential
    for understanding multi-project data architectures.
    --project can be repeated to query specific projects.
    Examples:
      kbagent --json lineage
      kbagent --json lineage show
      kbagent --json lineage show --project prod
      kbagent --json lineage show --project prod --project dev

### Organization Management

  kbagent org setup --org-id ID --url URL [--dry-run] [--yes] [--token-description PREFIX]
    Bulk-onboard all projects from a Keboola organization.
    Lists all projects via Manage API, creates Storage API tokens, and registers them.
    Safe to re-run -- already registered projects are skipped.

    The manage token is read from KBC_MANAGE_API_TOKEN env var or prompted
    interactively (never passed as CLI argument for security).

    Options:
      --org-id         Required. Organization ID.
      --url            Required. Keboola stack URL (or KBC_STORAGE_API_URL env var).
      --dry-run        Preview what would happen without making changes.
      --yes / -y       Skip confirmation prompt.
      --token-description  Description prefix for created tokens (default: kbagent-cli).

    Examples:
      kbagent org setup --org-id 123 --url https://connection.keboola.com --dry-run
      kbagent --json org setup --org-id 123 --url https://connection.keboola.com
      KBC_MANAGE_API_TOKEN=xxx kbagent --json org setup --org-id 123 --url https://connection.keboola.com --yes

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

### MCP Tools (Multi-Project)

  kbagent tool list [--project NAME]
    List all available MCP tools from keboola-mcp-server.
    Each tool is annotated with multi_project flag:
    - multi_project=true: Read tool, runs across ALL connected projects in parallel
    - multi_project=false: Write tool, targets a single project
    Example:
      kbagent --json tool list

  kbagent tool call TOOL_NAME [--project NAME] [--input JSON]
    Call an MCP tool. Read tools automatically query all projects.
    Write tools use --project or the default project.
    Examples:
      kbagent --json tool call list_configs
      kbagent --json tool call list_configs --project prod
      kbagent --json tool call get_config --input '{{"configuration_id": "12345"}}'
      kbagent --json tool call create_config --project prod --input '{{"component_id": "keboola.ex-db-snowflake", "name": "My Config"}}'

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
     kbagent --json job list --project prod --limit 10        # Recent jobs
     kbagent --json job list --project prod --status error    # Failed jobs

7. Common workflow - check health:
     kbagent --json doctor                                    # Full health check
     kbagent --json project status                            # Test all connections

8. Common workflow - explore data lineage:
     kbagent --json lineage                                    # Cross-project data flow
     kbagent --json lineage show --project prod                # Lineage for one project
     kbagent --json lineage show --project prod --project dev  # Lineage for specific projects

9. MCP tools workflow - interact with Keboola via MCP server:
     kbagent --json tool list                                    # See all available tools
     kbagent --json tool call list_configs                       # List configs from ALL projects
     kbagent --json tool call list_configs --project prod        # List configs from one project
     kbagent --json tool call create_config --project prod --input '{{...}}'  # Write to one project

10. Environment variables:
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


def context_command(ctx: typer.Context) -> None:
    """Show usage instructions for AI agents interacting with Keboola."""
    formatter = get_formatter(ctx)

    if formatter.json_mode:
        # In JSON mode, output the context text as structured data
        data = {
            "version": __version__,
            "context": AGENT_CONTEXT,
        }
        formatter.output(data)
    else:
        formatter.console.print(AGENT_CONTEXT)
