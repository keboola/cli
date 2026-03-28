# CLAUDE.md - Development Context for Claude Code

## Build and Run

```bash
# Install in development mode (editable)
uv pip install -e ".[dev]"

# Or install dependencies only
uv sync

# Run the CLI
kbagent --help
uv run kbagent --help

# Run a specific command
kbagent --json project list
```

A `Makefile` provides shortcuts for common tasks. Run `make help` to see all targets, or use:

```bash
make install        # install in dev mode
make test           # run all tests
make lint           # run ruff linter
make format         # format code
make check          # lint + format-check + test (CI-like)
make hooks          # install pre-commit hook
make clean          # remove caches and build artifacts
```

## Testing

```bash
# Run all tests
uv run pytest tests/ -v

# Run a specific test file
uv run pytest tests/test_cli.py -v

# Run a specific test class or method
uv run pytest tests/test_cli.py::TestProjectAdd -v
uv run pytest tests/test_cli.py::TestProjectAdd::test_project_add_success_json -v
```

## Project Structure

```
src/keboola_agent_cli/
  __init__.py           # __version__ = "0.5.0"
  __main__.py           # python -m support
  cli.py                # Typer root app, global options, subcommand wiring
  constants.py          # Shared constants (retry params, timeouts, defaults)
  http_base.py          # BaseHttpClient - shared HTTP foundation for both clients
  client.py             # LAYER 3: HTTP client (Storage API + Queue API)
  manage_client.py      # LAYER 3: HTTP client (Manage API, X-KBC-ManageApiToken)
  ai_client.py          # LAYER 3: HTTP client (AI Service API, component schemas)
  config_store.py       # JSON persistence for config.json (0600 permissions)
  models.py             # Pydantic models shared across layers
  output.py             # OutputFormatter: JSON vs Rich dual-mode output
  errors.py             # KeboolaApiError, ConfigError, mask_token()
  commands/
    _helpers.py         # Shared command-layer helpers (formatter, service factory, error mapping)
    project.py          # LAYER 1: CLI commands for project management
    config.py           # LAYER 1: CLI commands for config browsing
    job.py              # LAYER 1: CLI commands for job history (Queue API)
    lineage.py          # LAYER 1: CLI commands for cross-project data lineage
    org.py              # LAYER 1: CLI commands for organization bulk onboarding
    tool.py             # LAYER 1: CLI commands for MCP tool list/call (supports --branch)
    branch.py           # LAYER 1: CLI commands for branch lifecycle (list/create/use/reset/delete/merge)
    workspace.py        # LAYER 1: CLI commands for workspace lifecycle (create/list/delete/query)
    component.py        # LAYER 1: CLI commands for component discovery and scaffold
    context.py          # LAYER 1: Agent usage instructions
    doctor.py           # LAYER 1: Health check command
  services/
    base.py             # LAYER 2: BaseService - shared parallel execution infrastructure
    project_service.py  # LAYER 2: Business logic for projects
    config_service.py   # LAYER 2: Business logic for configurations
    job_service.py      # LAYER 2: Business logic for job history
    lineage_service.py  # LAYER 2: Cross-project lineage via bucket sharing
    org_service.py      # LAYER 2: Organization setup orchestration
    mcp_service.py      # LAYER 2: MCP tool integration (keboola-mcp-server wrapper)
    branch_service.py   # LAYER 2: Branch lifecycle (create/use/reset/delete/merge, async job polling)
    workspace_service.py # LAYER 2: Workspace lifecycle (CRUD, table load, SQL query via Query Service)
    component_service.py # LAYER 2: Component discovery, schema fetch, scaffold generation
    doctor_service.py   # LAYER 2: Health check business logic

tests/
  conftest.py           # Shared fixtures (tmp_config_dir, config_store, formatters)
  helpers.py            # Shared test utilities
  test_cli.py           # End-to-end CLI tests via CliRunner
  test_client.py        # API client tests with mocked HTTP
  test_manage_client.py # Manage API client tests with mocked HTTP
  test_config_store.py  # Config persistence tests
  test_errors.py        # mask_token() tests
  test_models.py        # Pydantic model tests
  test_output.py        # OutputFormatter tests
  test_services.py      # Business logic tests (project, config, parallel)
  test_base_service.py     # BaseService unit tests (resolve, workers, parallel)
  test_lineage_service.py  # Lineage service tests
  test_mcp_service.py      # MCP service tests (incl. branch_id propagation)
  test_branch_service.py   # Branch service tests (lifecycle, multi-project, errors)
  test_org_service.py      # Org service tests (slugify, setup, idempotency)
  test_workspace_service.py # Workspace service tests (CRUD, query, from-transformation)
  test_workspace_cli.py    # Workspace CLI tests via CliRunner
  test_doctor_service.py   # Doctor service tests
  test_http_base.py        # BaseHttpClient tests
  test_helpers.py          # Command helpers tests
  test_ai_client.py        # AI Service client tests
  test_component_service.py # Component service tests
  test_component_cli.py    # Component CLI tests via CliRunner
  test_integration.py      # Integration tests (edge cases, linting)
```

## Architecture: 3-Layer Design

```
CLI Commands (commands/)  -->  Services (services/)  -->  API Client (client.py, manage_client.py)
  Typer, output                 Business logic             HTTP, endpoints
```

- API changes: modify only `client.py` or `manage_client.py`
- Business logic changes: modify only `services/`
- UI changes: modify only `commands/`

### Three HTTP Clients

- **KeboolaClient** (`client.py`): Storage API + Queue API, auth via `X-StorageApi-Token`
- **ManageClient** (`manage_client.py`): Manage API, auth via `X-KBC-ManageApiToken`

- **AiServiceClient** (`ai_client.py`): AI Service API, auth via `X-StorageApi-Token`, URL derived as `ai.{stack_suffix}`

All three inherit from `BaseHttpClient` (`http_base.py`) which provides shared retry/backoff logic (429/5xx, exponential backoff, 3 retries) and common HTTP infrastructure.

### MCP Integration

`McpService` wraps `keboola-mcp-server` as a subprocess via MCP SDK (`mcp` package).
- Read tools run across ALL projects in parallel (one MCP session per project)
- Write tools target a single project (default or `--project`)
- **Auto-expand**: tools like `list_tables` that require `bucket_id` automatically
  resolve it by calling `list_buckets` first (configured in `AUTO_EXPAND_TOOLS` dict)
- Upfront parameter validation against tool's `inputSchema` before multi-project dispatch
- **Branch support**: `--branch ID` passes `KBC_BRANCH_ID` env var to MCP subprocess, forces single-project mode

## Coding Conventions

1. **Typer commands** are thin - they parse arguments, call a service, and format output. No business logic in commands.

2. **Services** receive `ConfigStore` and a `client_factory` callable via dependency injection. This enables easy testing with mocks.

3. **All data models** use Pydantic 2.x (`BaseModel`). Models are defined in `models.py` and shared across layers.

4. **Dual output**: every command supports `--json` for structured output and Rich formatting for human-readable output. Use `OutputFormatter.output(data, human_formatter)`.

5. **Error handling**: commands catch `KeboolaApiError` and `ConfigError`, map them to the appropriate exit code, and output structured errors in JSON mode.

6. **Exit codes**: 0=success, 1=general error, 2=usage error, 3=auth error, 4=network error, 5=config error.

7. **Token masking**: tokens are never printed in full. Use `mask_token()` from `errors.py`.

8. **Config file**: stored at `~/.config/keboola-agent-cli/config.json` with `0600` permissions. Managed by `ConfigStore`.

9. **Tests**: use `typer.testing.CliRunner` for CLI tests, `unittest.mock` for mocking services and clients, `pytest` fixtures from `conftest.py`.

10. **Dependencies**: typer, rich, httpx, pydantic, platformdirs, mcp, jsonschema, pyyaml. Dev: pytest, pytest-httpx, pytest-asyncio, ruff.

11. **Error accumulation**: multi-project operations collect per-project errors without stopping. One project failing doesn't block others (see `lineage_service.py`, `org_service.py`).

12. **Manage token security**: never persisted, never passed as CLI argument, never logged. Only via `KBC_MANAGE_API_TOKEN` env var or interactive hidden prompt.

13. **Idempotency**: `org setup` skips already-registered projects by matching `project_id`. Safe to re-run.

14. **Protected main branch**: direct pushes to `main` are blocked. Always create a feature branch, commit there, push, create a PR via `gh pr create`, merge via `gh pr merge`, then switch back to main and pull.

15. **Pre-commit checks are mandatory.** Before every `git commit`, run `ruff check` and `ruff format --check` on changed files. A pre-commit hook (`scripts/pre-commit`, install via `make hooks`) does this automatically. **Never commit without passing lint + format.** If using sub-agents that write code, always run `make check` (or at minimum `ruff check src/ tests/ && ruff format . --check`) before committing their output.

## Claude Code Plugin (Marketplace)

This repo doubles as a Claude Code plugin marketplace. The plugin lives in `plugins/kbagent/` and contains a skill that teaches Claude how to use kbagent.

**When to update the plugin:**
- Adding/removing/renaming CLI commands → update `plugins/kbagent/skills/kbagent/SKILL.md` (decision table, workflows)
- Changing response format or adding gotchas → update `plugins/kbagent/skills/kbagent/references/gotchas.md`
- Changing workspace or branch behavior → update the respective `references/*.md` file
- Bumping CLI version → also bump `plugins/kbagent/.claude-plugin/plugin.json` version

**Structure:**
```
.claude-plugin/marketplace.json                        # Repo-level marketplace definition
plugins/kbagent/
  .claude-plugin/plugin.json                           # Plugin manifest (keep version in sync!)
  skills/kbagent/
    SKILL.md                                           # Lean: trigger rules + decision table
    references/
      workspace-workflow.md                            # SQL debugging step-by-step
      branch-workflow.md                               # Dev branch lifecycle
      gotchas.md                                       # Response parsing, common pitfalls
```

Note: `SKILL.md` instructs Claude to run `kbagent context` as its first step, which dynamically loads the full CLI documentation. This means command details stay in sync automatically. The plugin files only need updating when workflows, gotchas, or the skill's triggering description change.

## All CLI Commands

```
# Global options: --json, --verbose, --no-color, --config-dir

kbagent project add --project NAME --url URL --token TOKEN
kbagent project list
kbagent project remove --project NAME
kbagent project edit --project NAME [--url URL] [--token TOKEN]
kbagent project status [--project NAME]

kbagent config list [--project NAME] [--component-type TYPE] [--component-id ID]
kbagent config detail --project NAME --component-id ID --config-id ID
kbagent config search --query PATTERN [--project NAME] [--component-type TYPE] [--ignore-case] [--regex]

kbagent job list [--project NAME] [--component-id ID] [--status STATUS] [--limit N]
kbagent job detail --project NAME --job-id ID

kbagent lineage show [--project NAME]   # also works as just: kbagent lineage

kbagent sharing list [--project NAME]
kbagent sharing share --project ALIAS --bucket-id ID --type TYPE [--target-project-ids IDs] [--target-users EMAILS]
kbagent sharing unshare --project ALIAS --bucket-id ID
kbagent sharing link --project ALIAS --source-project-id ID --bucket-id ID [--name NAME]
kbagent sharing unlink --project ALIAS --bucket-id ID

kbagent org setup --org-id ID --url URL [--dry-run] [--yes] [--token-description PREFIX]
kbagent org setup --project-ids 1,2,3 --url URL [--dry-run] [--yes] [--token-description PREFIX]

kbagent tool list [--project NAME] [--branch ID]
kbagent tool call TOOL_NAME [--project NAME] [--input JSON|@file|-] [--branch ID]

kbagent branch list [--project NAME]
kbagent branch create --project ALIAS --name "..." [--description "..."]
kbagent branch use --project ALIAS --branch ID
kbagent branch reset --project ALIAS
kbagent branch delete --project ALIAS --branch ID
kbagent branch merge --project ALIAS [--branch ID]

kbagent workspace create --project ALIAS [--name NAME] [--backend TYPE] [--ui] [--read-only/--no-read-only]
kbagent workspace list [--project NAME]
kbagent workspace detail --project ALIAS --workspace-id ID
kbagent workspace delete --project ALIAS --workspace-id ID
kbagent workspace password --project ALIAS --workspace-id ID
kbagent workspace load --project ALIAS --workspace-id ID --tables TABLE_ID [--tables ...] [--preserve]
kbagent workspace query --project ALIAS --workspace-id ID --sql "SELECT ..." [--transactional]
kbagent workspace query --project ALIAS --workspace-id ID --file query.sql
kbagent workspace from-transformation --project ALIAS --component-id ID --config-id ID [--row-id ID]

kbagent component list [--project NAME] [--type TYPE] [--query QUERY]
kbagent component detail --component-id ID [--project NAME]
kbagent config new --component-id ID [--name NAME] [--project NAME] [--output-dir DIR]

kbagent context
kbagent init [--from-global]
kbagent doctor [--fix]
kbagent version
kbagent update
```
