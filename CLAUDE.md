# CLAUDE.md - Development Context for Claude Code

> **Contributors**: Read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a PR.
> It covers coding style, security principles, Keboola API best practices,
> and the full checklist for adding new CLI commands.

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
make check          # lint + format-check + changelog-check + test (CI-like)
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
  json_utils.py         # Deep-merge, set_nested_value, compute_diff utilities
  http_base.py          # BaseHttpClient - shared HTTP foundation for both clients
  client.py             # LAYER 3: HTTP client (Storage API + Queue API)
  manage_client.py      # LAYER 3: HTTP client (Manage API, X-KBC-ManageApiToken)
  ai_client.py          # LAYER 3: HTTP client (AI Service API, component schemas)
  config_store.py       # JSON persistence for config.json (0600 permissions)
  models.py             # Pydantic models shared across layers
  output.py             # OutputFormatter: JSON vs Rich dual-mode output
  errors.py             # KeboolaApiError, ConfigError, mask_token()
  changelog.py          # Version changelog data + helpers (update on every release)
  auto_update.py        # Auto-update on startup + "What's new" display
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
    changelog.py        # LAYER 1: Changelog display
    context.py          # LAYER 1: Agent usage instructions
    doctor.py           # LAYER 1: Health check command
  hints/
    __init__.py         # HintRegistry + render_hint() public API
    models.py           # HintMode, ClientCall, ServiceCall, HintStep, CommandHint
    renderer.py         # ClientRenderer + ServiceRenderer (Python code generation)
    definitions/        # One file per command group (config.py, storage.py, job.py, ...)
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
    deep_lineage_service.py # LAYER 2: Column-level lineage analysis (SQL parsing, AI enrichment)
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
  test_json_utils.py       # Deep-merge and nested-path utility tests
  test_config_update.py    # Config update with configuration content (merge, set, dry-run)
  test_doctor_service.py   # Doctor service tests
  test_http_base.py        # BaseHttpClient tests
  test_helpers.py          # Command helpers tests
  test_ai_client.py        # AI Service client tests
  test_component_service.py # Component service tests
  test_component_cli.py    # Component CLI tests via CliRunner
  test_deep_lineage_service.py # Deep lineage service tests (column-level lineage, SQL parsing)
  test_e2e.py              # E2E tests against real API (make test-e2e)
  test_e2e_lineage_deep.py # E2E tests for deep lineage (build + show against real data)
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
- **Auto-expand**: tools like `get_tables` that require `bucket_ids` automatically
  resolve them by calling `get_buckets` first (configured in `AUTO_EXPAND_TOOLS` dict)
- Upfront parameter validation against tool's `inputSchema` before multi-project dispatch
- **Branch support**: `--branch ID` passes `KBC_BRANCH_ID` env var to MCP subprocess, forces single-project mode

## Versioning

**Single source of truth: `pyproject.toml`** (`version = "X.Y.Z"`).

- `src/keboola_agent_cli/__init__.py` reads the version at runtime via `importlib.metadata.version("keboola-agent-cli")`. **Never hardcode a version string in `__init__.py`.**
- `plugins/kbagent/.claude-plugin/plugin.json` must match. Run `make version-sync` (or `python scripts/sync_version.py`) to update it.
- The pre-commit hook and CI automatically check version consistency.

**When bumping the version**: edit `pyproject.toml`, add a changelog entry to `src/keboola_agent_cli/changelog.py`, then run `make version-sync`. Do not edit `__init__.py` or `plugin.json` manually. CI enforces changelog completeness via `make changelog-check`.

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

16. **E2E test coverage**: Every new CLI command MUST have a corresponding E2E test in `tests/test_e2e.py`. Run `make test-e2e` to verify. E2E tests require `E2E_API_TOKEN` and `E2E_URL` env vars and exercise the full CLI against a real Keboola project.

17. **Plugin & agent sync are mandatory, NOT CI-enforced.** When adding/removing/renaming any command -- and at every version bump -- follow `CONTRIBUTING.md` sections "Documentation changes (mandatory!)", "Plugin synchronization map", and "Releasing a new version". CI catches drift only in `SKILL.md` (decision table), `plugin.json` (version), and `changelog.py` (release entries). The following files are **silent-drift risks** that ship broken if you forget them:
    - `src/keboola_agent_cli/commands/context.py` (`AGENT_CONTEXT`)
    - `CLAUDE.md` `## All CLI Commands` section (this file)
    - `plugins/kbagent/agents/keboola-expert.md` -- **highest risk** (Rule 6 VERSION GATE, tool selection matrix, inline gotchas)
    - `plugins/kbagent/skills/kbagent/SKILL.md` -- description triggers and workflow links (the auto-generated table is CI-checked, the rest is not)
    - `plugins/kbagent/skills/kbagent/references/commands-reference.md`
    - `plugins/kbagent/skills/kbagent/references/gotchas.md` (every new gotcha **MUST** be tagged with `(since vX.Y.Z)`)
    - `plugins/kbagent/skills/kbagent/references/<topic>-workflow.md`

    Forgetting any of these does not fail tests or lint -- it ships an AI agent that quietly recommends commands that do not exist on the user's installed kbagent version, or refuses commands that do. Treat the change as **not done** until every applicable file has been updated.

## Claude Code Plugin (Marketplace)

This repo doubles as a Claude Code plugin marketplace. The plugin lives in `plugins/kbagent/` and exposes four AI surfaces: a CLI (`kbagent`), a skill (`kbagent`), a slash command (`/keboola`), and a specialist subagent (`keboola-expert`). All are namespaced under `kbagent:`.

**Update rules** -- see `CONTRIBUTING.md` > "Documentation changes (mandatory!)", "Plugin synchronization map", and "Releasing a new version" for the binding checklists. Coding convention #17 above is the short version. Do **not** maintain a parallel update list here -- it always drifts.

**Structure:**
```
.claude-plugin/marketplace.json                        # Repo-level marketplace definition
plugins/kbagent/
  .claude-plugin/
    plugin.json                                        # Plugin manifest (auto-synced from pyproject.toml)
    CLAUDE.md                                          # Operational guidance for Claude Code main agents
  agents/
    keboola-expert.md                                  # Specialist subagent system prompt (HIGHEST silent-drift risk)
  commands/
    keboola.md                                         # /keboola slash command
  skills/kbagent/
    SKILL.md                                           # Trigger rules + auto-generated decision table
    references/
      commands-reference.md                            # Per-command notes (hand-maintained)
      gotchas.md                                       # Response parsing + (since vX.Y.Z) behavior log
      <topic>-workflow.md                              # One file per workflow (workspace, branch, sync, ...)
```

`SKILL.md` instructs Claude to run `kbagent context` as its first step, which dynamically loads the full CLI documentation. That keeps command *signatures* in sync automatically -- but it does **not** save the agent's tool-selection matrix, the gotchas log, or the version-gate examples in `keboola-expert.md`. Those are static and must be updated by hand whenever the CLI changes.

## All CLI Commands

> **Hand-maintained, no CI freshness check.** When you add/remove/rename a
> command, you must edit this section AND the other silent-drift surfaces
> listed in convention #17 above. See `CONTRIBUTING.md` >
> "Plugin synchronization map" for the full list.

```
# Global options: --json, --verbose, --no-color, --config-dir, --hint client|service, --deny-writes, --deny-destructive

kbagent project add --project NAME --url URL --token TOKEN
kbagent project list
kbagent project remove --project NAME
kbagent project edit --project NAME [--url URL] [--token TOKEN]
kbagent project status [--project NAME]
kbagent project refresh --project ALIAS [--dry-run] [--force] [--yes] [--token-description DESC] [--token-expires-in N]
kbagent project refresh --all [--dry-run] [--force] [--yes] [--token-description DESC] [--token-expires-in N]
kbagent project description-get --project NAME
kbagent project description-set --project NAME [--text STR | --file PATH | --stdin]
kbagent project use ALIAS
kbagent project current
kbagent project info --project NAME

kbagent config list [--project NAME] [--component-type TYPE] [--component-id ID] [--branch ID] [--include-rows]
kbagent config detail --project NAME [--project NAME ...] --component-id ID [--config-id ID] [--branch ID] [--with-state]
kbagent config search --query PATTERN [--project NAME] [--component-type TYPE] [--ignore-case] [--regex] [--branch ID]
kbagent config update --project NAME --component-id ID --config-id ID [--name N] [--description D] [--configuration JSON|@file|-] [--configuration-file PATH] [--set PATH=VALUE ...] [--merge] [--dry-run] [--branch ID]
kbagent config set-default-bucket --project NAME --component-id ID --config-id ID (--bucket BUCKET_ID | --clear) [--dry-run] [--branch ID]
kbagent config rename --project NAME --component-id ID --config-id ID --name "New Name" [--branch ID] [--directory DIR]
kbagent config variables-set --project NAME --component-id ID --config-id ID --var KEY=VALUE [--var ...] [--replace] [--variables-id ID] [--values-id ID] [--branch ID] [--dry-run]
kbagent config variables-get --project NAME --component-id ID --config-id ID [--branch ID]
kbagent config variables-clear --project NAME --component-id ID --config-id ID [--branch ID] [--yes]
kbagent config metadata-list --project NAME --component-id ID --config-id ID [--branch ID]
kbagent config get-metadata --project NAME --component-id ID --config-id ID --key KEY [--branch ID]
kbagent config set-metadata --project NAME --component-id ID --config-id ID --key KEY --value VALUE [--branch ID]
kbagent config delete-metadata --project NAME --component-id ID --config-id ID --metadata-id ID [--branch ID] [--yes]
kbagent config set-folder --project NAME --component-id ID --config-id ID --name FOLDER [--branch ID]
kbagent config row-create --project NAME --component-id ID --config-id ID --name ROW_NAME [--description D] [--configuration JSON|@file|-] [--is-disabled] [--branch ID]
kbagent config row-update --project NAME --component-id ID --config-id ID --row-id ID [--name N] [--description D] [--configuration JSON|@file|-] [--is-disabled | --is-enabled] [--branch ID]
kbagent config row-delete --project NAME --component-id ID --config-id ID --row-id ID [--branch ID] [--yes]
kbagent config oauth-url --project NAME --component-id ID --config-id ID [--redirect-url URL]

kbagent search QUERY [--project NAME] [--type table|bucket|config|flow|data-app|transformation] [--search-type textual|config-based] [--limit N]

kbagent job list [--project NAME] [--component-id ID] [--status STATUS] [--limit N]
kbagent job detail --project NAME --job-id ID
kbagent job run --project NAME --component-id ID --config-id ID [--row-id ID ...] [--wait] [--timeout N] [--branch ID] [--variable-values-id ID] [--no-variables] [--poll-strategy exponential|fixed] [--log-tail-lines N]
kbagent job terminate --project NAME (--job-id ID [--job-id ID ...] | --status any|created|waiting|processing [--component-id ID] [--config-id ID] [--branch ID] [--limit N]) [--dry-run] [--yes]

kbagent storage buckets [--project NAME] [--branch ID]
kbagent storage bucket-detail --project NAME --bucket-id ID [--branch ID]
kbagent storage tables [--project NAME ...] [--bucket-id ID] [--branch ID]
kbagent storage table-detail --project NAME --table-id ID [--branch ID]
kbagent storage create-bucket --project NAME --stage STAGE --name NAME [--description D] [--backend B] [--branch ID]
kbagent storage create-table --project NAME --bucket-id ID --name NAME --column COL:TYPE[(length)] [...] [--primary-key COL] [--not-null COL ...] [--default NAME=VALUE ...] [--branch ID]
kbagent storage upload-table --project NAME --table-id ID --file PATH [--incremental] [--branch ID]
kbagent storage download-table --project NAME --table-id ID [--output FILE] [--columns COL ...] [--limit N] [--branch ID]
kbagent storage delete-table --project NAME --table-id ID [--table-id ...] [--force] [--dry-run] [--yes] [--branch ID]
kbagent storage delete-column --project NAME --table-id ID --column COL [--column ...] [--force] [--dry-run] [--yes] [--branch ID]
kbagent storage delete-bucket --project NAME --bucket-id ID [--bucket-id ...] [--force] [--dry-run] [--yes] [--branch ID]
kbagent storage swap-tables --project NAME --table-id ID --target-table-id ID --branch ID [--dry-run] [--yes]
kbagent storage describe-bucket --project NAME --bucket-id ID [--text STR | --file PATH | --stdin] [--branch ID]
kbagent storage describe-table --project NAME --table-id ID [--text STR | --file PATH | --stdin] [--branch ID]
kbagent storage describe-column --project NAME --table-id ID --column NAME=DESC [--column ...] [--branch ID]
kbagent storage describe-batch --project NAME --from-file YAML [--branch ID]
kbagent storage files --project NAME [--tag TAG ...] [--limit N] [--offset N] [--query Q] [--branch ID]
kbagent storage file-upload --project NAME --file PATH [--name NAME] [--tag TAG ...] [--permanent] [--branch ID]
kbagent storage file-download --project NAME [--file-id ID | --tag TAG ...] [--output FILE]
kbagent storage file-detail --project NAME --file-id ID
kbagent storage file-delete --project NAME --file-id ID [--file-id ...] [--dry-run] [--yes]
kbagent storage file-tag --project NAME --file-id ID [--add TAG ...] [--remove TAG ...]
kbagent storage load-file --project NAME --file-id ID --table-id ID [--incremental] [--delimiter D] [--enclosure E] [--branch ID]
kbagent storage unload-table --project NAME --table-id ID [--columns COL ...] [--limit N] [--tag TAG ...] [--download] [--output FILE|DIR] [--file-type csv|parquet] [--branch ID]

kbagent lineage build --directory PATH --output PATH [--ai] [--refresh]
kbagent lineage show --load PATH [--upstream NODE] [--downstream NODE] [--column COL] [--columns] [--project ALIAS] [--depth N] [--format text|mermaid|html|er]
kbagent lineage info --load PATH
kbagent lineage server --load PATH [--port N] [--host HOST]

kbagent sharing list [--project NAME]
kbagent sharing share --project ALIAS --bucket-id ID --type TYPE [--target-project-ids IDs] [--target-users EMAILS]
kbagent sharing unshare --project ALIAS --bucket-id ID
kbagent sharing link --project ALIAS --source-project-id ID --bucket-id ID [--name NAME]
kbagent sharing unlink --project ALIAS --bucket-id ID
kbagent sharing edges [--project NAME]

kbagent org setup --org-id ID --url URL [--dry-run] [--yes] [--token-description PREFIX] [--refresh]
kbagent org setup --project-ids 1,2,3 --url URL [--dry-run] [--yes] [--token-description PREFIX] [--refresh]

kbagent tool list [--project NAME] [--branch ID]
kbagent tool call TOOL_NAME [--project NAME] [--input JSON|@file|-] [--branch ID]

kbagent branch list [--project NAME]
kbagent branch create --project ALIAS --name "..." [--description "..."]
kbagent branch use --project ALIAS --branch ID
kbagent branch reset --project ALIAS
kbagent branch delete --project ALIAS --branch ID
kbagent branch merge --project ALIAS [--branch ID]
kbagent branch metadata-list --project NAME [--branch ID|default]
kbagent branch metadata-get --project NAME --key KEY [--branch ID|default]
kbagent branch metadata-set --project NAME --key KEY [--text STR | --file PATH | --stdin] [--branch ID|default]
kbagent branch metadata-delete --project NAME --metadata-id ID [--branch ID|default]

kbagent workspace create --project ALIAS [--name NAME] [--backend TYPE] [--ui] [--read-only/--no-read-only]
kbagent workspace list [--project NAME ...] [--orphaned]
kbagent workspace detail --project ALIAS --workspace-id ID
kbagent workspace delete --project ALIAS --workspace-id ID
kbagent workspace password --project ALIAS --workspace-id ID
kbagent workspace load --project ALIAS --workspace-id ID --tables TABLE_ID [--tables ...] [--preserve]
kbagent workspace query --project ALIAS --workspace-id ID --sql "SELECT ..." [--transactional]
kbagent workspace query --project ALIAS --workspace-id ID --file query.sql
kbagent workspace gc [--project NAME ...] [--dry-run] [--yes]
kbagent workspace from-transformation --project ALIAS --component-id ID --config-id ID [--row-id ID]

kbagent data-app list [--project NAME ...] [--branch ID]
kbagent data-app detail --project NAME --app-id ID [--branch ID]
kbagent data-app create --project ALIAS --name NAME --slug SLUG --git-repo URL [--description STR | --description-file PATH] [--git-branch main] [--git-public/--no-git-public] [--git-username USER] [--git-pat-env VAR | --git-pat-file PATH | --git-pat-encrypted KBC::Project...] [--auth password|public] [--size tiny|small|medium|large] [--auto-suspend SECONDS] [--type python-js|python|streamlit|r|...] [--branch ID] [--no-deploy] [--wait] [--timeout SECONDS] [--keep-on-failure] [--dry-run]
kbagent data-app deploy --project NAME --app-id ID [--config-version N] [--wait] [--timeout SECONDS] [--branch ID]
kbagent data-app start --project NAME --app-id ID [--wait] [--timeout SECONDS]
kbagent data-app stop --project NAME --app-id ID [--wait] [--timeout SECONDS]
kbagent data-app delete --project NAME --app-id ID [--yes]
kbagent data-app password --project NAME --app-id ID

kbagent component list [--project NAME] [--type TYPE] [--query QUERY]
kbagent component detail --component-id ID [--project NAME]
kbagent config new --component-id ID [--name NAME] [--project NAME] [--output-dir DIR]

kbagent encrypt values --project ALIAS --component-id ID --input JSON|@file|- [--output-file PATH]

kbagent kai ping [--project NAME]
kbagent kai ask --message "question" [--project NAME]
kbagent kai chat --message "msg" [--chat-id ID] [--project NAME]
kbagent kai history [--project NAME] [--limit N]

kbagent flow list [--project NAME] [--branch ID] [--with-schedules]
kbagent flow detail --project NAME --flow-id ID [--component-id keboola.orchestrator|keboola.flow] [--branch ID]
kbagent flow schema
kbagent flow new --project NAME --name NAME [--component-id keboola.orchestrator|keboola.flow] [--description D] [--file @path.yaml|-|JSON] [--branch ID]
kbagent flow update --project NAME --flow-id ID [--component-id ID] [--name N] [--description D] [--file @path.yaml|-|JSON] [--branch ID]
kbagent flow delete --project NAME --flow-id ID [--component-id ID] [--branch ID] [--yes]
kbagent flow schedule --project NAME --flow-id ID --cron "0 6 * * *" [--component-id ID] [--timezone TZ] [--disabled] [--branch ID]
kbagent flow schedule-remove --project NAME --flow-id ID [--component-id ID] [--branch ID] [--yes]

kbagent schedule list [--project NAME ...] [--enabled-only] [--branch ID]
kbagent schedule detail --project NAME --schedule-id ID [--branch ID]
kbagent schedule find [--cron-window START-END] [--not-run-since DAYS] [--project NAME ...] [--branch ID]

kbagent context
kbagent init [--from-global]
kbagent doctor [--fix]
kbagent version
kbagent update
kbagent changelog [--limit N]
```
