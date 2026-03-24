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
- Generate a visual explorer dashboard with catalog, orchestrations, and lineage
- Get structured JSON output suitable for programmatic consumption

## Quick Start

### Option A: Add a single project

  kbagent --json project add --alias my-project --url https://connection.keboola.com --token YOUR_TOKEN

### Option B: Bulk-onboard all projects from an organization

  KBC_MANAGE_API_TOKEN=your-manage-token kbagent --json org setup --org-id 123 --url https://connection.keboola.com --yes

Then explore:

  kbagent --json project list
  kbagent --json config list

## All Commands

### Project Management

  kbagent project add --alias NAME --url STACK_URL --token TOKEN
    Add a new Keboola project connection. The token is verified against the API.
    Token can be passed via --token, KBC_TOKEN env var, or interactive prompt.
    Default URL: https://connection.keboola.com (or KBC_STORAGE_API_URL env var).
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
    Edit an existing project connection. If token changes, it is re-verified via API.
    Examples:
      kbagent --json project edit --alias prod --url https://connection.north-europe.azure.keboola.com
      kbagent --json project edit --alias prod --token new-901-xxxxx

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

  kbagent config update --project NAME --component-id ID --config-id ID [--name NEW_NAME] [--description NEW_DESC] [--branch BRANCH_ID]
    Update a configuration's name and/or description.
    If a dev branch is active, update targets that branch. Use --branch to override.
    At least one of --name or --description must be provided.
    Examples:
      kbagent --json config update --project prod --component-id keboola.python-transformation-v2 --config-id 12345 --name "New Name"
      kbagent --json config update --project prod --component-id keboola.ex-http --config-id 67890 --name "Renamed" --description "Updated desc" --branch 456

  kbagent config delete --project NAME --component-id ID --config-id ID [--branch BRANCH_ID]
    Delete a configuration. If a dev branch is active, deletion targets that branch.
    Use --branch to override. Deleting in a branch marks the config as removed
    without affecting Main.
    Example:
      kbagent --json config delete --project prod --component-id keboola.python-transformation-v2 --config-id 12345
      kbagent --json config delete --project prod --component-id keboola.ex-http --config-id 67890 --branch 456

  kbagent config search --query PATTERN [--project NAME] [--component-type TYPE] [--component-id ID] [--ignore-case] [--regex]
    Search through configuration bodies (names, descriptions, parameters, rows)
    for a string or regex pattern. Reports matching configs and WHERE in the JSON
    tree the match was found. Runs across all connected projects in parallel.
    Default: case-sensitive substring match.
    -i / --ignore-case: case-insensitive matching.
    -r / --regex: interpret query as a regular expression.
    --project can be repeated to limit to specific projects.
    Examples:
      kbagent --json config search --query "password"
      kbagent --json config search --query "marketing" --ignore-case
      kbagent --json config search --query "marketing" -i --project prod
      kbagent --json config search --query "\\d{{3}}-\\d+" --regex
      kbagent --json config search --query "(password|secret|heslo)" --regex --ignore-case

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

### Explorer Dashboard

  kbagent explorer [--project NAME] [--output-dir DIR] [--job-limit N] [--tiers FILE] [--no-open]
    Generate catalog and orchestration data files for the KBC Explorer dashboard,
    then open the dashboard in a browser.
    Collects configs, jobs, lineage, and flow details across all connected projects.
    --project can be repeated to limit to specific projects.
    --output-dir: directory for output files (default: kbc-explorer/)
    --job-limit: max jobs per project for statistics (default: 500)
    --tiers: YAML file mapping project aliases to tiers (L0/L1/L2)
    --no-open: generate files without opening the browser
    Examples:
      kbagent --json explorer --no-open
      kbagent explorer --project prod --project dev --no-open
      kbagent explorer --tiers tiers.yaml

  kbagent explorer init-tiers [--output FILE]
    Generate a tiers.yaml template from registered projects.
    Auto-detects tier from alias naming convention (-l0-, -l1-, -l2-).
    Projects that cannot be classified are marked with TODO comments.
    --output / -o: output path (default: tiers.yaml)
    Example:
      kbagent explorer init-tiers
      kbagent explorer init-tiers -o my-tiers.yaml

    The generated YAML has this structure:
      description: "Project catalog"
      tiers:
        L0:
          name: "Data Sources / Extraction"
          description: "Raw data extraction from external systems"
        L1:
          name: "Processing / Transformation"
          description: "Data processing, transformation, and modeling"
        L2:
          name: "Output / Delivery"
          description: "Final data products, dashboards, and data sharing"
      projects:
        my-l0-extract: L0
        my-l1-transform: L1
        unclassified-project: L0  # TODO: assign correct tier

### LLM Export (Project Context for AI)

  kbagent llm export --project ALIAS [--with-samples] [--sample-limit N] [--max-samples N]
    Export project to "Twin Format" -- an AI-optimized directory of JSON files
    containing table schemas, transformation SQL code, internal lineage graph,
    job statistics, and component configurations.
    Output is written to ./${{ALIAS}}/ directory (created automatically).
    Requires the kbc CLI binary (brew install keboola-cli).

    After export, start by reading ./${{ALIAS}}/ai/AGENT_INSTRUCTIONS.md -- it explains
    the directory structure, how to interpret each file, and recommended workflows.

    The twin format includes:
    - ai/AGENT_INSTRUCTIONS.md -- how to read and use the exported data
    - buckets/*/metadata.json -- table schemas, columns, row counts
    - transformations/*/metadata.json -- SQL/Python code, input/output tables
    - indices/graph.jsonl -- internal lineage (table->transformation->table)
    - jobs/index.json -- job execution stats
    - components/*/ -- extractor/writer configurations

    Use --with-samples to include actual data samples (CSV) from tables.

    Examples:
      kbagent llm export --project prod
      kbagent llm export --project prod --with-samples --sample-limit 50

### Version Information

  kbagent version
    Show kbagent version and check for updates of dependencies (kbc, keboola-mcp-server).
    Example:
      kbagent --json version

### Development Branches

  kbagent branch list [--project NAME]
    List development branches from connected projects.
    --project can be repeated to query multiple projects.
    Each branch shows: ID, name, whether it is the default branch, active marker, and creation date.
    Examples:
      kbagent --json branch list
      kbagent --json branch list --project prod
      kbagent --json branch list --project prod --project dev

  kbagent branch create --project ALIAS --name "branch-name" [--description "..."]
    Create a new development branch and auto-activate it.
    Branch creation is an async operation on the Keboola API -- the CLI waits
    for the job to complete (typically 1-3 seconds) before returning.
    The created branch becomes the active branch for the project, so subsequent
    tool calls will automatically use it (no need to pass --branch every time).
    Example:
      kbagent --json branch create --project prod --name "fix-transform-x"

  kbagent branch use --project ALIAS --branch ID
    Set an existing development branch as active.
    Validates the branch exists via the API before activating it.
    Example:
      kbagent --json branch use --project prod --branch 456

  kbagent branch reset --project ALIAS
    Reset the active branch back to main/production.
    Subsequent tool calls will operate on the main branch.
    Example:
      kbagent --json branch reset --project prod

  kbagent branch delete --project ALIAS --branch ID
    Delete a development branch via API (async operation, CLI waits for completion).
    If the deleted branch was active, it is automatically reset to main.
    Example:
      kbagent --json branch delete --project prod --branch 456

  kbagent branch merge --project ALIAS [--branch ID]
    Get the KBC UI merge URL for a development branch.
    Does NOT merge via API -- generates the URL for safe review and merge in the UI.
    If --branch is not set, uses the active branch from config.
    After displaying the URL, resets the active branch to main.
    Example:
      kbagent --json branch merge --project prod

### Workspaces (SQL Debugging)

  kbagent workspace create --project ALIAS [--name NAME] [--backend snowflake] [--ui] [--read-only/--no-read-only]
    Create a temporary workspace for SQL debugging. Two modes:
    - Default (headless): fast (~1s) via Storage API. Password returned immediately.
    - --ui flag: slower (~15s) via Queue job. Workspace visible in Keboola UI Workspaces tab.
    Both modes return connection credentials including password.
    --name sets a human-readable name (default: kbagent-ALIAS).
    Examples:
      kbagent --json workspace create --project prod --name "debug-transform"
      kbagent --json workspace create --project prod --name "debug-ui" --ui

  kbagent workspace list [--project NAME]
    List workspaces from connected projects.
    --project can be repeated to query multiple projects.
    Example:
      kbagent --json workspace list --project prod

  kbagent workspace detail --project ALIAS --workspace-id ID
    Show workspace connection details (password NOT included).
    Example:
      kbagent --json workspace detail --project prod --workspace-id 12345

  kbagent workspace delete --project ALIAS --workspace-id ID
    Delete a workspace and its associated sandbox config.
    Workspaces also expire automatically server-side.
    Example:
      kbagent --json workspace delete --project prod --workspace-id 12345

  kbagent workspace password --project ALIAS --workspace-id ID
    Reset workspace password and return the new one.
    Example:
      kbagent --json workspace password --project prod --workspace-id 12345

  kbagent workspace load --project ALIAS --workspace-id ID --tables TABLE_ID [--tables TABLE_ID2 ...]
    Load tables from storage into a workspace. Waits for async load to complete.
    Table IDs use Keboola format: in.c-bucket.table-name
    Example:
      kbagent --json workspace load --project prod --workspace-id 12345 --tables in.c-main.users --tables in.c-main.orders

  kbagent workspace query --project ALIAS --workspace-id ID --sql "SELECT ..." [--transactional]
  kbagent workspace query --project ALIAS --workspace-id ID --file query.sql [--transactional]
    Execute SQL in a workspace via Query Service. Provide SQL via --sql or --file.
    Polls until complete and returns results as CSV.
    Uses the Storage API token for auth -- no Snowflake credentials needed.
    Example:
      kbagent --json workspace query --project prod --workspace-id 12345 --sql "SELECT * FROM users LIMIT 10"

  kbagent workspace from-transformation --project ALIAS --component-id ID --config-id ID [--row-id ID] [--backend snowflake]
    Create a workspace from an existing transformation config.
    Reads the transformation, creates a config-tied workspace, and loads all input tables.
    Returns credentials ready for SQL debugging.
    Example:
      kbagent --json workspace from-transformation --project prod --component-id keboola.snowflake-transformation --config-id 22777254

### Project Sync (GitOps Workflow)

  kbagent sync init --project ALIAS [--directory DIR] [--git-branching]
    Initialize a sync working directory for a Keboola project.
    Creates .keboola/manifest.json with project metadata and naming conventions.
    Use --git-branching to enable git-to-Keboola branch mapping.
    Example:
      mkdir my-project && cd my-project
      kbagent --json sync init --project prod
      kbagent --json sync init --project prod --git-branching

  kbagent sync pull --project ALIAS [--directory DIR] [--force]
    Download all configurations from Keboola to local files.
    Creates a dev-friendly directory structure with _config.yml files.
    SQL transformations are extracted into transform.sql with block markers.
    Python code is extracted into transform.py/code.py + pyproject.toml.
    Example:
      kbagent --json sync pull --project prod

  kbagent sync status [--directory DIR]
    Show which local configs have been modified, added, or deleted since last pull.
    Uses SHA256 hash comparison for reliable change detection.
    Example:
      kbagent --json sync status

  kbagent sync diff --project ALIAS [--directory DIR]
    Show detailed diff between local files and remote Keboola state.
    Compares config content (ignoring encrypted value nonces).
    Example:
      kbagent --json sync diff --project prod

  kbagent sync push --project ALIAS [--directory DIR] [--dry-run] [--force]
    Push local changes to Keboola. Creates new configs, updates modified,
    deletes removed (with --force). New configs get IDs from API automatically.
    --dry-run shows what would change without applying.
    Example:
      kbagent --json sync push --project prod --dry-run
      kbagent --json sync push --project prod

  kbagent sync branch-link --project ALIAS [--branch-id ID] [--branch-name NAME]
    Link current git branch to a Keboola development branch.
    Auto-creates the Keboola branch if it doesn't exist with the same name.
    Requires --git-branching mode enabled via sync init.
    Example:
      git checkout -b feature/new-etl
      kbagent --json sync branch-link --project prod

  kbagent sync branch-unlink [--directory DIR]
    Remove the branch mapping for the current git branch.
    Does NOT delete the Keboola branch itself.

  kbagent sync branch-status [--directory DIR]
    Show the current git branch mapping status.

### Utility Commands

  kbagent init [--from-global]
    Initialize a local .kbagent/ workspace in the current directory.
    Creates .kbagent/config.json for per-directory project isolation.
    Use --from-global to copy existing projects from global config.
    Automatically adds .kbagent/ to .gitignore.
    Example:
      kbagent init
      kbagent init --from-global

  kbagent context
    Show this help text with usage instructions for AI agents.

  kbagent doctor [--fix]
    Run health checks: config file, permissions, connectivity, CLI version, MCP server.
    --fix: Auto-fix issues (installs MCP server binary for faster tool call startup).
    Examples:
      kbagent --json doctor
      kbagent doctor --fix

## Global Flags

  --json / -j     Output in JSON format (always use this for programmatic parsing)
  --verbose / -v  Enable verbose output
  --no-color      Disable colored output (auto-disabled in non-TTY environments)
  --config-dir    Override config directory path (bypasses all auto-detection)

### MCP Tools (Multi-Project)

  kbagent tool list [--project NAME] [--branch ID]
    List all available MCP tools from keboola-mcp-server.
    Use --branch to scope to a development branch (requires --project).
    Each tool is annotated with multi_project flag:
    - multi_project=true: Read tool, runs across ALL connected projects in parallel
    - multi_project=false: Write tool, targets a single project
    Example:
      kbagent --json tool list

  kbagent tool call TOOL_NAME [--project NAME] [--input JSON] [--branch ID]
    Call an MCP tool. Read tools automatically query all projects.
    Write tools use --project or the default project.
    Use --branch to scope the call to a development branch (requires --project).
    Examples:
      kbagent --json tool call list_configs
      kbagent --json tool call list_configs --project prod
      kbagent --json tool call list_configs --project prod --branch 123
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

7. Common workflow - check health and optimize:
     kbagent --json doctor                                    # Full health check
     kbagent doctor --fix                                     # Auto-install MCP server binary (faster tool calls)
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

10. Environment variables (alternative to CLI arguments):
     KBC_TOKEN             - Storage API token (fallback for --token in project add/edit)
     KBC_STORAGE_API_URL   - Default stack URL (fallback for --url in project add, org setup)
     KBC_MANAGE_API_TOKEN  - Manage API token (for org setup)
     KBAGENT_CONFIG_DIR    - Override config directory path (same as --config-dir flag)

11. Branch workflow -- develop on a branch without passing --branch every time:
     kbagent --json branch create --project prod --name "fix-transform-x"
       # ^ creates the branch AND sets it as "active" for the project
       #   all subsequent tool calls on this project auto-use this branch
     kbagent --json tool call list_configs --project prod     # auto-uses active branch
     kbagent --json tool call update_sql_transformation --project prod --input '{{...}}'  # auto-uses active branch
     kbagent --json branch merge --project prod
       # ^ does NOT merge! Returns a URL to Keboola UI where you review and merge manually.
       #   After displaying the URL, resets the active branch back to main.

12. Branch create and delete are async operations on the Keboola API.
    The CLI handles this transparently -- it waits for the async job to complete
    before returning (typically 1-3 seconds). You do NOT need to poll or retry.
    If the job takes too long (>60s), the CLI returns an error.

13. Project context for AI -- get a full offline snapshot of a project:
     kbagent llm export --project prod
     # Creates ./prod/ directory with Twin Format JSON files
     # FIRST read ./prod/ai/AGENT_INSTRUCTIONS.md -- it explains the structure
     # and how to interpret each file (schemas, transformations, lineage, etc.)
     # This is much faster than querying each piece via MCP tool calls.

14. Workspace isolation -- use per-directory config for client separation:
     kbagent init                                            # Create local .kbagent/ workspace
     kbagent --json project add --alias prod --url ...       # Adds to LOCAL config
     kbagent --json doctor                                   # Shows "Using local config at ..."

    Config resolution order: --config-dir flag > KBAGENT_CONFIG_DIR env var
    > .kbagent/ in CWD or parent dirs > ~/.config/keboola-agent-cli/ (global)

15. Workspace workflow -- debug a failing SQL transformation iteratively:
     # Step 1: Create workspace from the transformation (loads input tables automatically)
     kbagent --json workspace from-transformation --project prod --component-id keboola.snowflake-transformation --config-id 22777254
       # ^ returns workspace_id, credentials, and loaded tables

     # Step 2: Run the SQL to reproduce the error
     kbagent --json workspace query --project prod --workspace-id WS_ID --sql "SELECT ..."

     # Step 3: Iterate on fixes (no need to run full jobs!)
     kbagent --json workspace query --project prod --workspace-id WS_ID --sql "SELECT fixed_query ..."

     # Step 4: Once the fix works, update the transformation config
     kbagent --json tool call update_configuration --project prod --input '{{"component_id": "...", "configuration_id": "...", ...}}'

     # Step 5: Clean up (optional -- workspaces expire automatically)
     kbagent --json workspace delete --project prod --workspace-id WS_ID

     Alternative: create a standalone workspace (not from transformation):
     kbagent --json workspace create --project prod --name "debug-ws"
       # ^ headless mode, fast (~1s), not visible in Keboola UI
     kbagent --json workspace create --project prod --name "debug-ws" --ui
       # ^ UI mode, slower (~15s), visible in Keboola UI Workspaces tab
     kbagent --json workspace load --project prod --workspace-id WS_ID --tables in.c-bucket.my-table
     kbagent --json workspace query --project prod --workspace-id WS_ID --sql "SELECT * FROM \"my-table\" LIMIT 10"

16. Setting up projects -- two approaches:

    a) Single project (you have a Storage API token):
       kbagent --json project add --alias my-proj --url https://connection.keboola.com --token 901-xxxxx

    b) Entire organization (you have a Manage API token):
       KBC_MANAGE_API_TOKEN=xxx kbagent --json org setup --org-id 123 --url https://connection.keboola.com --yes
       This creates Storage API tokens for ALL projects in the org and registers them automatically.

17. Sync workflow -- manage configs as local files with GitOps:
     # Step 1: Initialize and pull
     mkdir my-project && cd my-project
     kbagent --json sync init --project prod
     kbagent --json sync pull --project prod

     # Step 2: Edit configs locally
     # SQL is in transform.sql, Python in code.py, config in _config.yml
     # Edit with any IDE, get git diffs, code review, etc.

     # Step 3: Review and push
     kbagent --json sync status                      # local changes
     kbagent --json sync diff --project prod         # vs remote
     kbagent --json sync push --project prod --dry-run  # preview
     kbagent --json sync push --project prod         # apply

     # Git-branching mode (maps git branches to Keboola dev branches):
     kbagent --json sync init --project prod --git-branching
     git checkout -b feature/new-etl
     kbagent --json sync branch-link --project prod  # creates Keboola dev branch
     kbagent --json sync pull --project prod
     # ... edit, push, then merge via PR + Keboola UI

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
