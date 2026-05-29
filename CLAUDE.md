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

### Beta / pre-release versions (since 0.43.3)

Beta and release-candidate versions follow **PEP 440**: `0.44.0b1`, `0.44.0rc1`, ... -- **not** the SemVer `-beta.1` form (hatchling + uv require PEP 440 syntax in `pyproject.toml`). Three independent gates keep stable users safe from accidentally landing on a beta:

1. **PEP 440 pre-release suffix.** pip / uv default to **skipping** pre-releases unless told otherwise (`--pre` for pip, `--prerelease=allow` for uv).
2. **GitHub Release `prerelease: true` flag.** The auto-update startup hook calls `/releases/latest`, which GitHub defines as "the most recent non-prerelease, non-draft release". Marking the release `--prerelease` makes it invisible to the auto-update path.
3. **Tag-pinned install URL.** When `--beta` opts into a pre-release, the install command appends `@v<version>` to the git+ source URL so uv pulls the **exact commit** the tag points to. Without this, uv would resolve the default branch (`main`) and -- if the beta lives on a feature branch -- silently install the stale main HEAD even though the version fetcher advertised the beta tag.

**Author workflow (release a beta from a feature branch):**

```bash
# 1. Bump pyproject.toml to PEP 440 pre-release form on the feature branch
#    version = "0.44.0b1"
make version-sync                          # propagates to plugin.json / marketplace.json

# 2. Add a changelog entry under that key in src/keboola_agent_cli/changelog.py
# 3. Commit + push to PR (NOT to main -- main stays on the stable channel)
git push origin feat/my-feature

# 4. Tag the PR head SHA + push tag
git tag v0.44.0b1 && git push origin v0.44.0b1

# 5. KEY STEP: create the GitHub Release WITH --prerelease pointing at the tag
gh release create v0.44.0b1 --prerelease \
    --title "v0.44.0 — Beta 1" \
    --notes-file release-notes-0.44.0b1.md
```

When the beta cooks long enough, merge the PR (stable squash) and ship `0.44.0` from main with a normal release **without** `--prerelease` -- auto-update picks it up on next startup.

**User opt-in (consume betas):**

- One-shot: `kbagent update --beta` -- resolver opts into pre-releases for this invocation only.
- Per-shell: `export KBAGENT_INCLUDE_PRERELEASE=1` -- every `kbagent update` / `kbagent version` in that shell treats betas as installable.
- **No persistent setting.** Each beta install is an active choice; never a forgotten "I once typed --beta six months ago" foot-gun.

The startup auto-update hook is **never** affected by `--beta` / env opt-in -- it always uses `/releases/latest` (stable channel). Beta installs only come from explicit `kbagent update --beta`.

Full author checklist: see `CONTRIBUTING.md` > "Releasing a beta (pre-release) version".

## Coding Conventions

> **0. (BINDING) Follow [CONTRIBUTING.md](CONTRIBUTING.md) in full.** Every code change -- human or AI agent -- must satisfy the rules in `CONTRIBUTING.md`. Specifically, the "Code Quality Patterns" section is non-negotiable: dataclasses (not bare tuples) for multi-value returns; categorical arguments before variable ones; `ErrorCode` enum (never raw strings); file-size budgets; context managers over lambdas; named functions over assigned anonymous functions; `ty` clean for new code. The `.claude/settings.json` post-edit hooks run `ruff check --fix`, `ruff format`, and `ty check` after every edit -- when an AI agent edits a file in this repo, those checks fire automatically and any failure must be addressed before continuing. If a rule conflicts with an existing pattern in legacy code, **fix it in the PR you are touching** or open a follow-up issue; do not propagate the pattern.

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

12. **Manage token security**: never persisted, never passed as CLI argument, never logged. Default-deny since 0.29.0: only via interactive hidden prompt; the `KBC_MANAGE_API_TOKEN` env var is **ignored** unless the top-level `--allow-env-manage-token` flag is passed. Default-deny closes the AI-exfiltration risk where any subprocess (including the AI agent itself) inherits the manage token via env. CI/CD callers must opt in explicitly.

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
    - `plugins/kbagent/skills/kbagent/references/<topic>-workflow.md` (e.g. `semantic-layer-workflow.md`, `workspace-workflow.md`, `sync-workflow.md`)

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
# Global options: --json, --verbose, --no-color, --config-dir, --hint client|service (deprecated, use kbagent serve REST API), --deny-writes, --deny-destructive, --allow-env-manage-token
# Headless / token-only (0.50.0+): export KBAGENT_PROJECT_FROM_ENV=1 + KBC_TOKEN + KBC_STORAGE_API_URL to synthesize an in-memory `__env__` project (no `project add`, no config.json on disk; token never persisted). Use `--project __env__`. Same env setup also powers `kbagent serve`.

kbagent project add --project NAME --url URL --token TOKEN
kbagent project list
kbagent project remove --project NAME
kbagent project edit --project NAME [--url URL] [--token TOKEN] [--new-alias NEW]
kbagent project status [--project NAME]
kbagent project refresh --project ALIAS [--dry-run] [--force] [--yes] [--token-description DESC] [--token-expires-in N]
kbagent project refresh --all [--dry-run] [--force] [--yes] [--token-description DESC] [--token-expires-in N]
kbagent project description-get --project NAME
kbagent project description-set --project NAME [--text STR | --file PATH | --stdin]
kbagent project use ALIAS
kbagent project current
kbagent project info --project NAME
kbagent project invite --project ALIAS --email EMAIL --role admin|guest|readOnly|share [--reason TEXT] [--dry-run]
kbagent project invite --from-csv FILE [--default-role ROLE] [--workers N] [--dry-run]
kbagent project member-list --project ALIAS [--include-pending]
kbagent project invitation-list --project ALIAS
kbagent project invitation-cancel --project ALIAS --email EMAIL [--invitation-id ID] [--yes]
kbagent project member-remove --project ALIAS --email EMAIL [--yes]
kbagent project member-set-role --project ALIAS --email EMAIL --role admin|guest|readOnly|share

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
kbagent job run --project NAME --component-id ID --config-id ID [--row-id ID ...] [--wait] [--timeout N] [--branch ID] [--mode run|debug] [--variable-values-id ID] [--no-variables] [--poll-strategy exponential|fixed] [--log-tail-lines N]
kbagent job terminate --project NAME (--job-id ID [--job-id ID ...] | --status any|created|waiting|processing [--component-id ID] [--config-id ID] [--branch ID] [--limit N]) [--dry-run] [--yes]

kbagent storage buckets [--project NAME] [--branch ID]
kbagent storage bucket-detail --project NAME --bucket-id ID [--branch ID]
kbagent storage tables [--project NAME ...] [--bucket-id ID] [--branch ID]
kbagent storage table-detail --project NAME --table-id ID [--branch ID]
kbagent storage create-bucket --project NAME --stage STAGE --name NAME [--description D] [--backend B] [--branch ID]
kbagent storage create-table --project NAME --bucket-id ID --name NAME --column COL:TYPE[(length)] [...] [--primary-key COL] [--not-null COL ...] [--default NAME=VALUE ...] [--branch ID] [--if-not-exists]
kbagent storage upload-table --project NAME --table-id ID --file PATH [--incremental] [--branch ID]
kbagent storage download-table --project NAME --table-id ID [--output FILE] [--columns COL ...] [--limit N] [--branch ID]
kbagent storage delete-table --project NAME --table-id ID [--table-id ...] [--force] [--dry-run] [--yes] [--branch ID]
kbagent storage truncate-table --project NAME --table-id ID [--table-id ...] [--dry-run] [--yes] [--branch ID]
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

# stream: Data Streams (OpenTelemetry/OTLP). Storage token from config (no manage token).
# Control plane = stream.<region> (derived from connection.<region>); the OTLP ingest URL
# (stream-in.<region>/otlp/<projectId>/<sourceName>/<secret>) is returned in source.otlp.url
# with the secret in the path -- MASKED by default, --reveal to print it. create-source --type otlp
# auto-provisions the logs/metrics/traces sinks (bucket in.c-otlp-<source>) so data lands; --no-sinks opts out.
kbagent stream list --project NAME [--branch ID]
kbagent stream create-source --project NAME --name NAME [--type otlp|http] [--branch ID] [--if-not-exists] [--no-sinks] [--reveal]
kbagent stream detail [SOURCE_ID | --name NAME] --project NAME [--branch ID] [--reveal]
kbagent stream delete SOURCE_ID --project NAME [--branch ID] [--dry-run] [--yes] [--force]

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

# feature: requires a super-admin Manage API token (inline hidden prompt; never persisted; --allow-env-manage-token for CI). --project resolves the stack URL (+ project_id for project ops) from config.
kbagent feature list --project ALIAS
kbagent feature project-show --project ALIAS
kbagent feature project-add --project ALIAS --feature NAME [--dry-run] [--yes]
kbagent feature project-remove --project ALIAS --feature NAME [--dry-run] [--yes]
kbagent feature user-show --project ALIAS --email EMAIL
kbagent feature user-add --project ALIAS --email EMAIL --feature NAME [--dry-run] [--yes]
kbagent feature user-remove --project ALIAS --email EMAIL --feature NAME [--dry-run] [--yes]

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
kbagent workspace list [--project NAME ...] [--orphaned] [--branch ID] [--qs-compatible]
kbagent workspace detail --project ALIAS --workspace-id ID [--branch ID]
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
kbagent data-app logs --project NAME --app-id ID [--lines N] [--since ISO8601]
kbagent data-app secrets-set --project ALIAS --app-id ID --secret '#KEY=VALUE' [--secret ...] [--secrets-file PATH] [--branch ID] [--allow-plaintext-on-encrypt-failure] [--dry-run] [--no-hint-next]
kbagent data-app secrets-list --project ALIAS --app-id ID [--branch ID] [--show-fingerprint]
kbagent data-app secrets-get --project ALIAS --app-id ID --key 'KEY' [--branch ID]   # '#' optional; plain values return their value, encrypted return metadata only
kbagent data-app secrets-remove --project ALIAS --app-id ID --key 'KEY' [--key ...] [--branch ID] [--yes] [--dry-run]   # '#' optional
kbagent data-app validate-repo --git-repo URL [--git-branch BRANCH] [--git-public/--no-git-public] [--git-pat-env VAR | --git-pat-file PATH] [--type python-js] [--strict]

kbagent component list [--project NAME] [--type TYPE] [--query QUERY]
kbagent component detail --component-id ID [--project NAME]
kbagent config new --component-id ID [--name NAME] [--project NAME] [--output-dir DIR] [--push --no-files --description D --configuration JSON|@file|- --configuration-file PATH --no-validate --branch ID --dry-run]

kbagent dev-portal identity add --alias A --username U [--password P | --password-stdin]
                                [--role-hint vendor|admin] [--vendor V] [--portal-url URL]
kbagent dev-portal identity list
kbagent dev-portal identity remove --alias A
kbagent dev-portal identity edit --alias A [--username U] [--password P|--password-stdin]
                                 [--role-hint H] [--vendor V] [--new-alias N]
kbagent dev-portal identity use ALIAS
kbagent dev-portal identity current
kbagent dev-portal identity verify [--identity A]

kbagent dev-portal list --vendor V [--identity A]
kbagent dev-portal get --app VENDOR.APP_ID [--identity A]

kbagent dev-portal create --vendor V --data FILE [--identity A] [--dry-run]
kbagent dev-portal patch --app VENDOR.APP_ID (--data FILE | --property KEY (--value V | --value-file F))
                         [--identity A] [--dry-run]
kbagent dev-portal upload-icon --app VENDOR.APP_ID --file PATH [--identity A] [--dry-run]
kbagent dev-portal publish --app VENDOR.APP_ID [--identity A] [--dry-run]
kbagent dev-portal deprecate --app VENDOR.APP_ID [--identity A] [--dry-run]
# All writes require an interactive random-code TTY confirm; no --yes / no env bypass.

kbagent encrypt values --project ALIAS --component-id ID --input JSON|@file|- [--output-file PATH]

kbagent semantic-layer model list --project P
kbagent semantic-layer model create --project P --name N [--description D] [--sql-dialect Snowflake]
kbagent semantic-layer model delete --project P --model M [--yes]
kbagent semantic-layer show --project P [--model M] [--type dataset|metric|relationship|constraint|glossary]
kbagent semantic-layer search-context --project P [--pattern G ...] [--type model|dataset|metric|relationship|constraint|glossary|all] [--limit N]
kbagent semantic-layer get-context --project P --context-id ID
kbagent semantic-layer validate --project P [--model M] [--deep]
kbagent semantic-layer export --project P [--model M] [--output PATH]
kbagent semantic-layer diff (--project-a A | --file-a PATH) (--project-b B | --file-b PATH) [--model-a M] [--model-b M]
kbagent semantic-layer add metric --project P [--model M] --name N --sql SQL --dataset TABLE_ID [--description D] [--yes]
kbagent semantic-layer add dataset --project P [--model M] --name N --table-id TABLE_ID [--description D] [--grain G] [--primary-key COL ...] [--deep-fields]
kbagent semantic-layer add relationship --project P [--model M] --name N --from TABLE_ID --to TABLE_ID --on EXPR [--type left|inner]
kbagent semantic-layer add constraint --project P [--model M] --name N --constraint-type inequality|equality|range|composition|exclusion|temporal|conditional --rule "EXPR" --metrics M1,M2 [--severity error|warning|info]
kbagent semantic-layer add glossary --project P [--model M] --term TERM [--definition D]
kbagent semantic-layer edit metric --project P [--model M] --name N [--new-name N2] [--new-sql SQL] [--new-dataset TABLE_ID] [--new-description D] [--yes]
kbagent semantic-layer edit dataset --project P [--model M] --name N [--new-name N2] [--new-description D] [--new-grain G]
kbagent semantic-layer edit constraint --project P [--model M] --name N [--new-name N2] [--new-rule "EXPR"] [--new-constraint-type T] [--new-severity error|warning|info] [--new-metrics M1,M2]
kbagent semantic-layer edit relationship --project P [--model M] --name N [--new-name N2] [--new-from TABLE_ID] [--new-to TABLE_ID] [--new-on EXPR] [--new-type left|inner]
kbagent semantic-layer edit glossary --project P [--model M] --term TERM [--new-term TERM2] [--new-definition D] [--yes]
kbagent semantic-layer remove metric --project P [--model M] --name N [--yes]
kbagent semantic-layer remove dataset --project P [--model M] --name N [--yes]
kbagent semantic-layer remove constraint --project P [--model M] --name N [--yes]
kbagent semantic-layer remove relationship --project P [--model M] --name N [--yes]
kbagent semantic-layer remove glossary --project P [--model M] --term TERM [--yes]
kbagent semantic-layer import --project P --file PATH [--model M] [--types T,T,...] [--dry-run] [--yes] [--overwrite]
kbagent semantic-layer promote --from-project A --to-project B [--from-model M] [--to-model M] [--types T,T,...] [--dry-run] [--yes]
kbagent semantic-layer build --project P [--model M] --tables T,T,... [--name N] [--dry-run] [--keep-on-failure] [--output PATH]
kbagent semantic-layer token --encrypt --project P --component-id C
# Alias: `kbagent sl ...` (hidden) is equivalent to `kbagent semantic-layer ...`.

kbagent http get PATH [--timeout SECONDS]
kbagent http post PATH [--body JSON|@file|-] [--timeout SECONDS]
kbagent http patch PATH [--body JSON|@file|-] [--timeout SECONDS]
kbagent http delete PATH [--timeout SECONDS]
# `http` talks to the running `kbagent serve`. Requires KBAGENT_SERVE_URL +
# KBAGENT_SERVE_TOKEN env vars (auto-injected into AI-agent / cli_command
# subprocesses by the scheduler). Use this from inside a scheduled agent
# task instead of forking another `kbagent` CLI process tree.

kbagent agent list
kbagent agent show TASK_ID
kbagent agent create --name NAME [--description D] [--cron CRON] [--manual] [--enabled/--disabled] (--type ai_agent --cli CLI --prompt P [--extra-arg ARG ...] [--timeout SECONDS] | --type cli_command --argv ARG [--argv ARG ...] [--timeout SECONDS] | --type mcp_tool --tool TOOL [--mcp-project ALIAS] [--mcp-branch ID] [--input JSON|@file|-] [--timeout SECONDS] | --from-file PATH|@path|-) [--trigger-task-id ID --trigger-on success|error|always]
kbagent agent update TASK_ID [--name N] [--description D] [--cron C] [--enabled/--disabled] [--manual/--auto] [--clear-trigger] [--trigger-task-id ID --trigger-on success|error|always]
kbagent agent delete TASK_ID [--yes]
kbagent agent run TASK_ID [--stream] [--runtime-prompt TEXT | --runtime-input JSON|@file|-]
kbagent agent runs TASK_ID [--limit N]
kbagent agent run-detail TASK_ID RUN_ID
kbagent agent run-events TASK_ID RUN_ID
kbagent agent test (--type ai_agent --cli CLI --prompt P | --type cli_command --argv ARG ... | --type mcp_tool --tool T ... | --from-file PATH) [--name N] [--stream] [--timeout SECONDS]
kbagent agent cron-preview --cron "0 6 * * 1" [--count N]
kbagent agent prompt-improve --goal "..." [--draft "..."] [--cli claude|codex|gemini] [--project ALIAS] [--extra-arg X ...] [--stream/--no-stream]
# `agent` reads/writes <config_dir>/agents.json directly (offline-first, no
# serve required for CRUD + ad-hoc run). The cron loop that fires scheduled
# tasks still requires `kbagent serve` running. Three action flavours
# (ai_agent / cli_command / mcp_tool) mirror the /agents REST surface
# byte-for-byte. Every subcommand taking TASK_ID / RUN_ID accepts it
# positionally OR via flag (--id / --task-id; --run-id for run-detail /
# run-events) -- the flag form matches the rest of the CLI (--job-id, ...).

kbagent kai ping [--project NAME]
kbagent kai preflight [--project NAME]
kbagent kai ask --message "question" [--project NAME]
kbagent kai chat --message "msg" [--chat-id ID] [--project NAME]
kbagent kai chat-detail --chat-id ID [--project NAME]
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
kbagent version [--beta]
kbagent update [--beta]
# `--beta` (or env `KBAGENT_INCLUDE_PRERELEASE=1`) opts into pre-release versions
# (PEP 440 betas/rc, e.g. 0.43.0b1). Default (no flag) is stable-only -- auto-update
# startup hook never silently lands on a beta.
kbagent changelog [--limit N]
kbagent serve [--host HOST] [--port PORT] [--ui] [--ui-dist PATH] [--reload] [--log-level LVL] [--cors-origin ORIGIN] [--config-dir DIR]
```
