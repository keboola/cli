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
  __init__.py           # PUBLIC SDK ENTRYPOINT + __version__. Re-exports the importable
                        #   facade (Client, Files, FileEntry) and the typed result models
                        #   (JobResult, QueryResult, UploadTableResult, SyncPushResult,
                        #   ConfigDetailResult, CloneResult, JobIdempotencyStore). Everything
                        #   in __all__ is committed public API (semver). __version__ is read
                        #   at runtime via importlib.metadata; never hardcoded. See docs/sdk.md.
  lib.py                # In-process SDK facade: Client (query/run_job/config_detail/
                        #   upload_table + .files + .raw) over KeboolaClient. Stateless,
                        #   config-dir-free, token passed at construction (issue #415). See docs/sdk.md.
  result_models.py      # Typed pydantic return contracts for the SDK facade (extra="allow",
                        #   populate_by_name); the semver-stable shapes consumers type against (issue #428).
  py.typed              # PEP 561 marker -- ships in the wheel so downstream mypy/ty treat the SDK as typed.
  __main__.py           # python -m support
  cli.py                # Typer root app, global options, subcommand wiring
  constants.py          # Shared constants + dynamic APP_NAME resolution (retry params, timeouts, defaults)
  json_utils.py         # Deep-merge, set_nested_value, compute_diff utilities
  models.py             # Pydantic models shared across layers
  output.py             # OutputFormatter: JSON vs Rich dual-mode output
  errors.py             # KeboolaApiError, ConfigError, ErrorCode enum, mask_token()
  config_store.py       # JSON persistence for config.json (0600 permissions)
  permissions.py        # PermissionEngine (--deny-writes / --deny-destructive session firewall)
  changelog.py          # Version changelog data + helpers (update on every release)
  auto_update.py        # Auto-update on startup + "What's new" display

  # LAYER 3 -- HTTP clients (all inherit BaseHttpClient in http_base.py:
  #            shared 429/5xx retry + exponential backoff)
  http_base.py          # BaseHttpClient - shared retry/backoff + common HTTP infra
  client/               # Storage API + Queue API package (X-StorageApi-Token);
                        #   split by endpoint family (storage_tables/storage_files/configs/
                        #   queue/tokens/branches/stream/query/workspaces/misc + _core/_transfer),
                        #   composed into one KeboolaClient via mixins (#520)
  manage_client.py      # Manage API                   (X-KBC-ManageApiToken)
  ai_client.py          # AI Service API               (component schemas, Kai)
  data_science_client.py # Data Science API            (data apps)
  metastore_client.py   # Metastore API                (semantic layer)
  dev_portal_client.py  # Developer Portal API
  stream_client.py      # Stream / Data Streams API    (OTLP/HTTP sources)

  commands/             # LAYER 1 -- thin Typer commands, one file per group (32 modules):
                        #   project config job storage flow branch workspace data_app
                        #   sync semantic_layer agent stream feature dev_portal kai
                        #   sharing lineage schedule search org tool component encrypt
                        #   permissions serve http_client repl init doctor version
                        #   changelog context
                        # _helpers.py = formatter/service-factory/error-mapping; _*.py = private helpers
  services/             # LAYER 2 -- business logic, one <group>_service.py per command group
                        #   (DI: receives ConfigStore + client_factory; base.py = parallel infra;
                        #    member_service, variables_service, repo_validate_service,
                        #    mcp_service, mcp_transport, deep_lineage_service are extra non-1:1 services)
  server/               # FastAPI app behind `kbagent serve` (REST API + Web UI mount + SSE)
  sync/                 # GitOps sync engine (manifest v3, pull/push/diff, branch-linking)
  _ui_dist/             # bundled React SPA served by `kbagent serve --ui`

tests/                  # ~137 files; mirror the layers (one test_<module>.py per command/service)
  conftest.py           # Shared fixtures (tmp_config_dir, config_store, formatters)
  helpers.py            # Shared test utilities
  test_cli.py           # End-to-end CLI tests via typer.testing.CliRunner
  test_e2e*.py          # E2E tests against a real API (make test-e2e; needs E2E_API_TOKEN + E2E_URL)
  test_integration.py   # Integration tests (edge cases, linting)
```

## Architecture: 3-Layer Design

```
CLI Commands (commands/)  -->  Services (services/)  -->  API Client (client/, manage_client.py)
  Typer, output                 Business logic             HTTP, endpoints
```

- API changes: modify only the relevant LAYER 3 client (`client/` package, `manage_client.py`, ...)
- Business logic changes: modify only `services/`
- UI changes: modify only `commands/`

### HTTP Clients

Seven clients, all inheriting `BaseHttpClient` (`http_base.py`) which provides shared retry/backoff logic (429/5xx, exponential backoff, 3 retries) and common HTTP infrastructure:

- **KeboolaClient** (`client/` package): Storage API + Queue API, auth via `X-StorageApi-Token`
- **ManageClient** (`manage_client.py`): Manage API, auth via `X-KBC-ManageApiToken`
- **AiServiceClient** (`ai_client.py`): AI Service API (component schemas, Kai), URL derived as `ai.{stack_suffix}`
- **DataScienceClient** (`data_science_client.py`): Data Science API (data apps)
- **MetastoreClient** (`metastore_client.py`): Metastore API (semantic layer)
- **DeveloperPortalClient** (`dev_portal_client.py`): Developer Portal API (component publishing)
- **StreamClient** (`stream_client.py`): Stream / Data Streams API (OTLP/HTTP sources)

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

- `src/keboola_agent_cli/__init__.py` reads the version at runtime via `importlib.metadata.version(APP_NAME)`. `APP_NAME` is resolved **dynamically** in `constants.py` (`_resolve_app_name()`: prefers the current distribution `keboola-cli`, falls back to the legacy `keboola-agent-cli` so the #424 migration-bridge wheel keeps working) -- it is **not** a fixed literal. **Never hardcode a version string in `__init__.py`.**
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

> **0. (BINDING) Follow [CONTRIBUTING.md](CONTRIBUTING.md) in full.** Every code change -- human or AI agent -- must satisfy the rules in `CONTRIBUTING.md`. Specifically, the "Code Quality Patterns" section is non-negotiable: dataclasses (not bare tuples) for multi-value returns; categorical arguments before variable ones; `ErrorCode` enum (never raw strings); file-size budgets (measured in CODE LINES -- docstrings and comments are free; `make loc-check`); context managers over lambdas; named functions over assigned anonymous functions; `ty` clean for new code. The `.claude/settings.json` post-edit hooks run `ruff check --fix`, `ruff format`, and `ty check` after every edit -- when an AI agent edits a file in this repo, those checks fire automatically and any failure must be addressed before continuing. If a rule conflicts with an existing pattern in legacy code, **fix it in the PR you are touching** or open a follow-up issue; do not propagate the pattern.

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

18. **Session-sentinel guards are CI-enforced.** `make check-sentinel-guards`
    (in `make check`) rejects three kinds of drift: a `config.json` credential
    write that is not sentinel-aware, a `BaseHttpClient` subclass that neither
    declares `SESSION_AUTH_FEATURE` nor is recorded as bearer-capable, and a
    `require_static_token` guard missing from `SESSION_UNSUPPORTED_FEATURES`
    (`services/_auth_registration.py`) -- the tuple `auth login` /
    `auth register-projects` disclose and every doc surface defers to. Run
    `python scripts/check_sentinel_guards.py --list` to see the inventory.

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
# Global options: --json, --verbose, --no-color, --config-dir, --deny-writes, --deny-destructive, --allow-env-manage-token
# Headless / token-only (0.50.0+): export KBAGENT_PROJECT_FROM_ENV=1 + KBC_TOKEN + KBC_STORAGE_API_URL to synthesize an in-memory `__env__` project (no `project add`, no config.json on disk; token never persisted). Use `--project __env__`. Same env setup also powers `kbagent serve`.

kbagent auth login [--stack URL|alias] [--device-code] [--register-projects]
kbagent auth login-password --email EMAIL (--password PASSWORD | --password-stdin) [--totp-secret SECRET] [--stack URL|alias] [--register-projects]
kbagent auth status [--stack URL|alias]
kbagent auth logout [--stack URL|alias] [--remove-projects] [--yes]
kbagent auth register-projects [--stack URL|alias] [--all] [--project-id ID ...] [--alias ID=ALIAS ...] [--yes]
# auth login-password (0.84.0+): the deliberate unattended exception to auth login's "needs a human at
#   a browser" rule -- email + password (+ TOTP if the account has MFA) grant, no browser, safe to run
#   from a CI secret-backed workflow step. Prefer --password-stdin (or KBC_LOGIN_PASSWORD) over
#   --password -- a value on the command line lands in shell history and process listings;
#   --password/--password-stdin are mutually exclusive (ConfigError if both given).
#   --email/--password/--totp-secret also read from
#   KBC_LOGIN_EMAIL/KBC_LOGIN_PASSWORD/KBC_LOGIN_TOTP_SECRET env vars (mirroring KBC_TOKEN's convention),
#   so a workflow can set them once in a step's env: block. --totp-secret is the base32 TOTP SEED (not
#   a 6-digit code) -- kbagent computes the current code itself (auth/totp.py, stdlib-only RFC 6238),
#   so no human ever types a live code. Only the TOTP factor is resolvable this way; a WebAuthn/passkey-
#   only account gets AUTH_MFA_INVALID and must use `auth login` (needs a browser) instead. Stores the
#   session in auth.json exactly like `auth login` does -- same auth-mode, same "session" column in
#   `project list`, same downstream command support. Storing an account's password (and TOTP seed) as
#   CI secrets is a bigger blast radius than a single scoped project token: use a dedicated,
#   least-privileged service account, never a real human's own credentials. New error code:
#   AUTH_MFA_INVALID.
# auth (since 0.80.0): browser-based login -- PKCE authorization-code by default (falls back to the
#   RFC 8628 device flow ONLY on a pre-exchange failure: no loopback browser, callback timeout, or an
#   SSH/container/WSL heuristic; --device-code forces it). REQUIRES A HUMAN AT A BROWSER -- never attempt
#   from an unattended AI agent task; use `auth login-password` or a static Storage token for
#   CI/headless instead. Issues a
#   USER-scoped "programmatic session" (kbc_at_* access token + kbc_rt_* refresh token) stored in
#   auth.json (0600), a sibling of config.json -- config.json's schema and CURRENT_CONFIG_VERSION are
#   unchanged. --register-projects writes each accessible project into config.json with the sentinel
#   token `kbc-session://{project_id}`. v1 scope is the Storage + Manage paths: the CLI commands and
#   `serve` both reach them, because `serve` delegates to the same already-guarded services (see
#   server/dependencies.py). Everything outside those paths fails fast on a sentinel-token project
#   (AUTH_NOT_SUPPORTED_ON_STACK) naming the static-token fallback; the authoritative list is
#   SESSION_UNSUPPORTED_FEATURES in services/_auth_registration.py, shipped to callers as
#   `session_unsupported_features` in --json -- do not re-derive it by hand. `dev-portal` is NOT on it
#   (it authenticates with its own identity, never a project token). Over `serve`, a session that
#   expires at runtime answers HTTP 401 with
#   error_code SESSION_EXPIRED -- a browser login only completes on the host. Serving session projects
#   means whoever holds KBAGENT_SERVE_TOKEN acts as the signed-in USER; see docs/web-server.md.
#   New error codes: AUTH_NOT_SUPPORTED_ON_STACK, AUTH_FLOW_TIMEOUT, AUTH_FLOW_DENIED, AUTH_FLOW_EXPIRED,
#   AUTH_BROWSER_UNAVAILABLE, AUTH_STATE_MISMATCH, SESSION_EXPIRED, SESSION_NOT_FOUND.
# `auth register-projects` (0.80.0+): fixes the usability gap where nothing was registered unless
#   --register-projects was passed at login, and where the alias offered was a slug of the project
#   NAME (never the numeric id, so `--project 9840` never resolves). Lists every project the session
#   can access with a collision-free suggested alias, then lets the caller pick which to register.
#   --all selects every candidate; --project-id ID (repeatable) selects specific ones (unknown id ->
#   ConfigError); omitting both runs an interactive arrow-key + spacebar checkbox picker (every
#   not-yet-registered project preselected, [a] toggles all, [enter] accepts) followed by a single
#   "Edit aliases?" confirm (default No) that opens the old per-project alias prompt only if opted
#   into -- each row already shows its suggested alias. On a piped stdin or a terminal without real
#   interactive capabilities, it falls back to the original numbers/ranges/'all'/'none' typed prompt.
#   In a non-TTY or --json context with neither --all nor --project-id, it fails fast telling the
#   caller to pass --all or --project-id. --alias ID=ALIAS (repeatable) overrides the
#   suggested alias in every mode. --yes skips only the picker's final confirmation. Never overwrites
#   an existing alias: same project+stack already registered -> status "exists" (no-op); alias taken by
#   something else -> status "skipped" with a rename hint. `auth login` (no --register-projects) also
#   offers this same picker interactively right after a successful login when stdout is a TTY and
#   --json was not used; otherwise it just prints the `auth register-projects` hint. Same picker fix
#   applies retroactively to `auth login --register-projects` (now suffixes on an alias collision
#   instead of silently skipping the second project). See docs/programmatic-auth-login-plan.md
#   section 4.5 for the full design.

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
kbagent config update --project NAME --component-id ID --config-id ID [--name N] [--description D] [--configuration JSON|@file|-] [--configuration-file PATH] [--set PATH=VALUE ...] [--merge] [--change-description TEXT] [--dry-run] [--branch ID] [--allow-plaintext-on-encrypt-failure]
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
kbagent config row-create --project NAME --component-id ID --config-id ID --name ROW_NAME [--description D] [--configuration JSON|@file|-] [--is-disabled] [--branch ID] [--allow-plaintext-on-encrypt-failure]
kbagent config row-update --project NAME --component-id ID --config-id ID --row-id ID [--name N] [--description D] [--configuration JSON|@file|-] [--change-description TEXT] [--is-disabled | --is-enabled] [--branch ID] [--allow-plaintext-on-encrypt-failure]
kbagent config row-delete --project NAME --component-id ID --config-id ID --row-id ID [--branch ID] [--yes]
kbagent config oauth-url --project NAME --component-id ID --config-id ID [--redirect-url URL]
kbagent config state-get --project NAME --component-id ID --config-id ID [--row-id ID] [--branch ID]
kbagent config state-set --project NAME --component-id ID --config-id ID [--row-id ID] --state JSON|@file|- [--branch ID] [--dry-run] [--yes]
# state-get/state-set (0.84.2+, #593): read/write a config's runtime state via the dedicated
#   PUT .../state endpoint -- closes the gap where `config update --set 'state...'` looked
#   successful but silently wrote to configuration.state.* instead (runtime state untouched).
#   Since 0.84.2, `config update --set` / `config row-update --set` REJECT (exit 2) any path
#   whose first segment is a top-level API field (state, rows, name, description, id, version,
#   currentVersion, changeDescription, created, creatorToken, isDeleted, isDisabled) -- use
#   state-set / --name / --description / row-update --is-disabled instead, or --configuration
#   for a genuine configuration.<prefix> key.

kbagent search QUERY [--project NAME] [--type table|bucket|config|flow|data-app|transformation] [--search-type textual|config-based] [--regex] [--limit N]
# --regex (0.67.0+): opt-in regex mode (mode=regex). Case-insensitive whole-term match on ENTITY NAMES
#   only ('report' != 'monthly_report'; use '.*report.*'). Textual only -- error with --search-type
#   config-based. Regex does NOT match column names, so matched_columns is always empty under --regex.
#   In textual mode, table results matched via a column name carry matched_columns (JSON) / a
#   "Matched columns" table column.
# BOTH modes match case-insensitively (config-based was case-sensitive until #569, which made
#   "is this referenced anywhere" miss a table spelled DCFAmount in one config and DCFAMOUNT in
#   another). `kbagent config search --query` stays case-sensitive by default (it has --ignore-case).

kbagent job list [--project NAME] [--component-id ID] [--status STATUS] [--limit N]
kbagent job detail --project NAME --job-id ID
kbagent job run --project NAME --component-id ID --config-id ID [--row-id ID ...] [--wait] [--timeout N] [--branch ID] [--mode run|debug] [--variable-values-id ID] [--no-variables] [--poll-strategy exponential|fixed] [--log-tail-lines N] [--idempotency-key KEY] [--force-rerun]
kbagent job terminate --project NAME (--job-id ID [--job-id ID ...] | --status any|created|waiting|processing [--component-id ID] [--config-id ID] [--branch ID] [--limit N]) [--dry-run] [--yes]

kbagent storage buckets [--project NAME] [--branch ID]
kbagent storage bucket-detail --project NAME --bucket-id ID [--branch ID]
kbagent storage tables [--project NAME ...] [--bucket-id ID] [--branch ID]
kbagent storage table-detail --project NAME --table-id ID [--branch ID]
kbagent storage create-bucket --project NAME --stage STAGE --name NAME [--description D] [--backend B] [--branch ID]
kbagent storage create-table --project NAME --bucket-id ID --name NAME [--column COL:TYPE[(length)] ...] [--primary-key COL] [--not-null COL ...] [--default NAME=VALUE ...] [--source-table-id ID] [--source-branch-id N] [--time-partitioning-type DAY|HOUR|MONTH|YEAR] [--time-partitioning-field COL] [--time-partitioning-expiration-ms MS] [--range-partitioning-field COL --range-partitioning-start S --range-partitioning-end E --range-partitioning-interval I] [--clustering-field COL ...] [--branch ID] [--if-not-exists]
# --column XOR --source-table-id (0.66.0+, BigQuery only): --source-table-id copies an existing table's data into the requested partition/clustering layout (schema derived from source) -> swap into place with swap-tables. Partition/clustering flags work in both modes (BigQuery only); time vs range partitioning are mutually exclusive. A non-BigQuery project fails fast (pre-flight backend check).
kbagent storage upload-table --project NAME --table-id ID --file PATH [--incremental] [--branch ID]
kbagent storage download-table --project NAME --table-id ID [--output FILE] [--columns COL ...] [--limit N] [--where-column COL --where-value VAL ... [--where-operator eq|neq]] [--changed-since WHEN] [--changed-until WHEN] [--branch ID]
kbagent storage add-column --project NAME --table-id ID --column COL:TYPE[(length)] [--not-null] [--default VALUE] [--branch ID]
kbagent storage delete-table --project NAME --table-id ID [--table-id ...] [--force] [--dry-run] [--yes] [--branch ID]
kbagent storage truncate-table --project NAME --table-id ID [--table-id ...] [--dry-run] [--yes] [--branch ID]
kbagent storage delete-column --project NAME --table-id ID --column COL [--column ...] [--force] [--dry-run] [--yes] [--branch ID]
kbagent storage delete-bucket --project NAME --bucket-id ID [--bucket-id ...] [--force] [--dry-run] [--yes] [--branch ID]
kbagent storage swap-tables --project NAME --table-id ID --target-table-id ID --branch ID [--dry-run] [--yes]
kbagent storage clone-table --project NAME --table-id ID --branch ID [--dry-run]
kbagent storage snapshot-create --project NAME --table-id ID [--description D] [--branch ID]
kbagent storage snapshots --project NAME --table-id ID [--limit N] [--branch ID]
kbagent storage snapshot-detail --project NAME --snapshot-id ID
kbagent storage snapshot-delete --project NAME --snapshot-id ID [--snapshot-id ...] [--dry-run] [--yes]
kbagent storage table-from-snapshot --project NAME --snapshot-id ID --bucket-id ID --name NAME [--branch ID] [--dry-run]
# Snapshots (0.75.0+, #512): point-in-time table backup (data+columns+PK) and restore as a NEW table.
#   table-from-snapshot goes through the classic tables-async endpoint (NOT tables-definition), --name is
#   REQUIRED (API rejects empty), no overwrite -- restore under a new name, then swap/delete yourself.
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
kbagent stream delete SOURCE_ID --project NAME [--branch ID] [--dry-run] [--yes|--force]

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

kbagent billing credits [--project ALIAS ...]
# billing credits (0.84.2+, issue #594 secondary ask): read-only PAYG credit balance, fanned out
#   across all registered projects in parallel by default (--project repeatable narrows). A project
#   without the `pay-as-you-go` owner.features flag never touches the billing host (NXDOMAIN on some
#   non-PAYG stacks) -- it gets a per-project error_code PAYG_NOT_AVAILABLE instead; per-project
#   failures degrade individually, the run never aborts. Rows report the API's native unit (credits)
#   AND derived minutes (1 credit = 60 min, matching the Keboola UI). Purchase history / Stripe
#   invoice IDs are NOT available here -- that data lives on connection.{stack}
#   /pay-as-you-go/billing/*, which does not accept a Storage token (issue #594 primary ask, open).

# feature: requires a super-admin Manage API token (inline hidden prompt; never persisted; --allow-env-manage-token for CI). --project resolves the stack URL (+ project_id for project ops) from config.
kbagent feature list --project ALIAS
kbagent feature project-show --project ALIAS
kbagent feature project-add --project ALIAS --feature NAME [--dry-run] [--yes]
kbagent feature project-remove --project ALIAS --feature NAME [--dry-run] [--yes]
kbagent feature user-show --project ALIAS --email EMAIL
kbagent feature user-add --project ALIAS --email EMAIL --feature NAME [--dry-run] [--yes]
kbagent feature user-remove --project ALIAS --email EMAIL --feature NAME [--dry-run] [--yes]

# token: scoped Storage tokens (Keboola single-bucket-write pattern; acting token needs canManageTokens; secret shown once).
kbagent token create --project NAME --description DESC [--bucket-write BUCKET ...] [--bucket-read BUCKET ...] [--component-access ID ...] [--can-read-all-file-uploads] [--expires-in N]
kbagent token delete --project NAME --token-id ID [--yes]
kbagent token refresh --project NAME --token-id ID [--yes]
# SDK (importable Client(url,token)) now exposes create_scoped_token / delete_token / refresh_token /
# create_stream_source / get_stream_source / list_stream_sources / delete_stream_source: dicts on .raw,
# typed ScopedTokenResult / StreamSourceResult on the facade. See docs/sdk.md.

# permissions: session write/destructive firewall. The top-level --deny-writes / --deny-destructive
# flags are the one-shot form; `permissions set` persists a policy (mode allow|deny + allow/deny patterns
# like cli:write, cli:destructive, tool:write). The agent guards rails against mistakes; not a sandbox.
kbagent permissions list [--category read|write|destructive|admin]
kbagent permissions show
kbagent permissions set --mode allow|deny [--allow PATTERN ...] [--deny PATTERN ...]
kbagent permissions reset
kbagent permissions check OPERATION

kbagent tool list [--project NAME] [--branch ID]
kbagent tool call TOOL_NAME [--project NAME] [--input JSON|@file|-] [--branch ID]
# tool group DEPRECATED (0.74.0+, epic #390): every catalog tool has a native command -- tool list
#   prints a cli_equivalent column, tool call warns with the exact replacement (stderr; --json adds
#   an additive "deprecation" key). Parity map = src/keboola_agent_cli/mcp_parity.py; weekly
#   mcp-parity-canary workflow (make parity-check) diffs it against upstream TOOLS.md. The group
#   (and `agent --type mcp_tool`) is REMOVED in v0.85.0, scheduled for the end of August 2026
#   -- epic #390 phase 3. `agent --type mcp_tool` tasks persist in agents.json, so they need
#   migrating to `--type cli_command` before that release or they fail on their next cron tick.

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
kbagent workspace query --project ALIAS --workspace-id ID --sql "SELECT ..." [--transactional] [--full] [--limit N]
kbagent workspace query --project ALIAS --workspace-id ID --file query.sql
# query: default reads results inline via Query Service `GET .../results` (fast, JSON columns+rows),
#   capped at --limit rows (default 500); pass --full for the complete CSV export (slower, uncapped).
#   Each statement carries structured columns+rows + a synthesized csv_data (back-compat) since 0.59.0.
kbagent workspace gc [--project NAME ...] [--dry-run] [--yes]
kbagent workspace from-transformation --project ALIAS --component-id ID --config-id ID [--row-id ID]

kbagent data-app list [--project NAME ...] [--branch ID]
kbagent data-app detail --project NAME --app-id ID [--branch ID]
kbagent data-app create --project ALIAS --name NAME --slug SLUG (--git-repo URL | --use-managed-git-repo) [--description STR | --description-file PATH] [--git-branch main] [--git-public/--no-git-public] [--git-username USER] [--git-pat-env VAR | --git-pat-file PATH | --git-pat-encrypted KBC::Project...] [--auth password|public] [--size tiny|small|medium|large] [--auto-suspend SECONDS] [--type python-js|python|streamlit|r|...] [--branch ID] [--no-deploy] [--wait] [--timeout SECONDS] [--keep-on-failure] [--dry-run]
# Exactly one git source is required: --git-repo URL (external) OR --use-managed-git-repo (0.65.0+).
# --use-managed-git-repo provisions an EMPTY Keboola-hosted repo (POST useManagedGitRepo:true), writes
#   NO parameters.dataApp.git block, and forces --no-deploy (nothing to run yet). Mutually exclusive with
#   --git-repo and all --git-*/PAT auth flags. Full flow to a RUNNING managed-repo app (0.65.0+):
#   (1) create --use-managed-git-repo -> (2) `git-credentials-create --type http_token --permissions
#   readWrite` + `git push` code to the managed repo URL (from `git-repo`) -> (3) `data-app deploy`.
#   The platform injects the clone credentials at deploy time, so no credential wiring is needed.
#   deploy pins the LATEST configVersion when a git block is present and omits it for a PURE managed
#   repo (deploys from managedGitRepoId). Use `data-app runs` to debug a deploy that reverts to
#   stopped (setup-phase failures produce no container logs).
kbagent data-app deploy --project NAME --app-id ID [--config-version N] [--wait] [--timeout SECONDS] [--branch ID]
kbagent data-app start --project NAME --app-id ID [--wait] [--timeout SECONDS]
kbagent data-app stop --project NAME --app-id ID [--wait] [--timeout SECONDS]
kbagent data-app delete --project NAME --app-id ID [--yes]
kbagent data-app password --project NAME --app-id ID
kbagent data-app logs --project NAME --app-id ID [--lines N] [--since ISO8601]
kbagent data-app runs --project NAME --app-id ID [--limit N]
kbagent data-app secrets-set --project ALIAS --app-id ID --secret '#KEY=VALUE' [--secret ...] [--secrets-file PATH] [--branch ID] [--allow-plaintext-on-encrypt-failure] [--dry-run] [--no-hint-next]
kbagent data-app secrets-list --project ALIAS --app-id ID [--branch ID] [--show-fingerprint]
kbagent data-app secrets-get --project ALIAS --app-id ID --key 'KEY' [--branch ID]   # '#' optional; plain values return their value, encrypted return metadata only
kbagent data-app secrets-remove --project ALIAS --app-id ID --key 'KEY' [--key ...] [--branch ID] [--yes] [--dry-run]   # '#' optional
kbagent data-app validate-repo --git-repo URL [--git-branch BRANCH] [--git-public/--no-git-public] [--git-pat-env VAR | --git-pat-file PATH] [--type python-js] [--strict]
kbagent data-app git-repo --project NAME --app-id ID
kbagent data-app git-credentials --project NAME --app-id ID
kbagent data-app git-credentials-create --project NAME --app-id ID --type ssh_key|http_token --permissions readOnly|readWrite [--public-key KEY | --public-key-file PATH] [--name LABEL] [--yes]
# git-repo introspects the deployed-from git repo (sandboxes-service /apps/{id}/git-repo); it returns 409 "no Git repository configured" until the app has been DEPLOYED at least once (git config syncs Storage->DS record at deploy). git-credentials* manage credentials for a MANAGED repo only; apps from `data-app create --git-repo` are external => git-credentials-create returns 409. http_token mints a ONE-TIME secret (shown once); credentials endpoints need an admin storage token.

kbagent component list [--project NAME] [--type TYPE] [--query QUERY]
kbagent component detail --component-id ID [--project NAME]
kbagent component sync-action ACTION_NAME --component-id ID --project ALIAS (--config-id ID [--row-id ID] | --config-data JSON|@file|-) [--branch ID] [--timeout N]
# sync-action (0.73.0+): POST sync-actions.{stack}/actions; ACTION_NAME freeform (component-defined,
#   e.g. testConnection/getTables); --row-id shallow-merges row over root at TOP level only (row
#   parameters/storage keys replace root wholesale, MCP parity -- NOT deep merge); --config-data
#   sends explicit configData verbatim (skips fetch); branchId omitted from body for production.
kbagent config examples --component-id ID [--project NAME] [--row]
kbagent config new --component-id ID [--name NAME] [--project NAME] [--output-dir DIR] [--push --no-files --description D --configuration JSON|@file|- --configuration-file PATH --no-validate --branch ID --dry-run --allow-plaintext-on-encrypt-failure]

# sync: GitOps -- configs as local files. init/pull/push/diff are filesystem-local (no serve REST surface).
kbagent sync init --project ALIAS [--directory DIR] [--git-branching] [--adopt-existing]
kbagent sync pull --project ALIAS [--all-projects] [--force] [--theirs] [--dry-run] [--with-samples] [--no-storage] [--no-jobs] [--job-limit N] [--branch ID]
# `sync pull --force` is conflict-aware (since 0.53.0): locally-modified config whose remote is UNCHANGED is preserved (delta stays pushable, never silently re-stamped); a true merge conflict (local AND remote both changed since last pull) aborts (exit 1, SYNC_CONFLICT, --json lists details.conflicts); local-untouched + remote-changed takes remote.
# `sync pull --theirs` (0.72.0+) is the supported reconcile path for a drifted tree: remote wins everywhere -- overwrites locally-modified configs/rows, restores deleted/missing files, resolves conflicts by taking remote (no abort). Since 0.72.0 plain pull also re-materializes a tracked config whose local dir was deleted (manifest<->disk invariant), so delete-dir-then-pull refetches; config-level isDisabled round-trips as sparse `is_disabled: true` in _config.yml (pull/diff/push).
kbagent sync status [--directory DIR]
kbagent sync diff --project ALIAS [--all-projects] [--directory DIR] [--branch ID]
kbagent sync push --project ALIAS [--all-projects] [--dry-run] [--force] [--allow-plaintext-on-encrypt-failure] [--branch ID] [--no-name-drift-warnings]
kbagent sync clone --source DIR --target ALIAS --target-dir DIR [--bucket-map FILE] [--variable-values FILE] [--instance-rename FILE] [--dry-run] [--branch ID]
# `sync clone` (0.63.0+) copies a reference synced tree into a fresh target project + parameterizes it: applies bucket_map / variable_values / instance_rename overrides (JSON/YAML files), then pushes so every config CREATEs fresh -- keboola.flow task configIds and transformation variable links are remapped reference->ULID by push Phase C/D. Idempotent: re-run with an existing --target-dir reports no_changes. Fails fast if the target already contains the reference's configs (clone needs a fresh target).
kbagent sync branch-link --project ALIAS (--branch-id ID | --branch-name NAME) [--directory DIR]
kbagent sync branch-unlink [--directory DIR]
kbagent sync branch-status [--directory DIR]

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
# Since v0.51.1: --role-hint is validated (vendor/admin) and load-bearing -- admin identities route
# `patch` to PATCH /admin/apps/{app} (permissive schema). Vendor + admin-only field => fail-fast preflight.
# --password-stdin works on TTY (hidden prompt) AND on a pipe (reads to EOF).

kbagent encrypt values --project ALIAS --component-id ID --input JSON|@file|- [--output-file PATH]

kbagent semantic-layer model list --project P
kbagent semantic-layer model create --project P --name N [--description D] [--sql-dialect Snowflake]
kbagent semantic-layer model delete --project P --model M [--yes]
kbagent semantic-layer show --project P [--model M] [--type dataset|metric|relationship|constraint|glossary]
kbagent semantic-layer search-context --project P [--pattern G ...] [--type model|dataset|metric|relationship|constraint|glossary|all] [--limit N]
kbagent semantic-layer schema --project P (--type model|dataset|metric|relationship|constraint|glossary[,TYPE...] | --all)
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
kbagent semantic-layer reference-data list --project P [--model M]
kbagent semantic-layer reference-data get --project P (--id ID | --dimension D)
kbagent semantic-layer reference-data set --project P [--model M] --dimension D --members-file PATH [--dataset-id T] [--description X]
kbagent semantic-layer reference-data delete --project P --id ID [--yes]
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

kbagent transformation create --project NAME --name NAME (--sql 'SELECT ...' | --sql-file PATH) [--created-table NAME ...] [--component-id ID] [--description D] [--branch ID] [--dry-run]
kbagent transformation show --project NAME --config-id ID [--component-id ID] [--branch ID]
kbagent transformation edit --project NAME --config-id ID --change-description TEXT (--op JSON ... | --op-file ops.json) [--storage JSON|@file|-] [--component-id ID] [--branch ID] [--dry-run]
# transformation (0.73.0+): native SQL-transformation editing (port of MCP create/update_sql_transformation, #396).
#   create: component derived from the project default_backend (snowflake|bigquery; other backends need
#   --component-id); SQL split one-statement-per-script[] element; single block "Blocks"/code "Code";
#   each --created-table T maps to out.c-<cleaned-name>.<T>. show: synthetic positional ids b{i}/b{i}.c{j};
#   when --component-id omitted, all known SQL transformation components are tried. edit: 9 ops
#   (add/remove/rename block+code, set_code, add_script, str_replace) applied sequentially against
#   batch-start ids -- ALWAYS `transformation show` first, ids renumber after structural ops;
#   --storage REPLACES configuration.storage wholesale; --dry-run previews without PUT.

kbagent docs query "QUESTION" [--project NAME]
# (0.73.0+) Documentation Q&A via the AI Service (server-side RAG). Unlike kai ask it does NOT
#   see project data; works with any token. --json emits {query, text, source_urls}.

kbagent flow list [--project NAME] [--branch ID] [--with-schedules]
kbagent flow detail --project NAME --flow-id ID [--branch ID]
kbagent flow schema [--full [--project NAME]]
kbagent flow examples [--component-id keboola.flow|keboola.orchestrator]
kbagent flow validate --file @flow.yaml|- [--project NAME]
kbagent flow new --project NAME --name NAME [--description D] [--file @path.yaml|-|JSON] [--branch ID]
kbagent flow update --project NAME --flow-id ID [--name N] [--description D] [--file @path.yaml|-|JSON] [--branch ID]
kbagent flow delete --project NAME --flow-id ID [--branch ID] [--yes]
kbagent flow schedule --project NAME --flow-id ID --cron "0 6 * * *" [--timezone TZ] [--disabled] [--branch ID]
kbagent flow schedule-remove --project NAME --flow-id ID [--branch ID] [--yes]
# Flows are conditional flows (keboola.flow). keboola.orchestrator is NOT supported (dropped 0.57.0).
# IDs are strings; phases use next[].goto + conditions; tasks are typed (job/notification/variable).
# flow new/update validate against the live CF schema fetched from the stack (AI Service
#   configurationSchema for keboola.flow; NOT bundled) -> INVALID_FLOW_DEFINITION on failure.
#   Schema-fetch failure (network/empty) does NOT block the write: structural check skipped,
#   semantic checks still run, a "structural schema validation skipped" warning is surfaced.
# flow validate: with --project fetches the live schema (full validation; fetch failure ->
#   semantic-only + note); without --project runs semantic-only + a note. flow schema --full:
#   with --project fetches the live schema (source=live); without --project serves the bundled
#   authoritative snapshot (source=bundled, 0.73.0+). Plain flow schema is the offline YAML template.
# flow examples (0.73.0+): bundled example flow configs (vendored from keboola-mcp-server), offline.
#   Default keboola.flow; keboola.orchestrator serves legacy examples informational-only (kbagent
#   cannot create/edit orchestrator flows). --json emits the bare list of configs.
# flow schedule (0.66.1+) also activates the config on the Scheduler Service so the cron fires;
#   activation failure keeps the config written, sets activated=false + warning, exit stays 0.
#   flow schedule-remove deregisters from the service before deleting each config.
# Execute a flow with: kbagent job run --project NAME --component-id keboola.flow --config-id ID

kbagent schedule list [--project NAME ...] [--enabled-only] [--branch ID]
kbagent schedule detail --project NAME --schedule-id ID [--branch ID]
kbagent schedule find [--cron-window START-END] [--not-run-since DAYS] [--project NAME ...] [--branch ID]

kbagent context
kbagent init [--from-global] [--project ALIAS ...]
# `--project ALIAS` (repeatable) copies only the named project(s) from the global config and implies --from-global.
kbagent doctor [--fix]
# `doctor` includes an `mcp_tool_tasks` check (0.81.0+): warns about scheduled agent tasks
# still using `--type mcp_tool` (removed in v0.85.0); they run unattended and get no warning
# at removal. `agent list` marks those rows and adds a per-task `deprecation` key in --json.
kbagent version [--beta]
kbagent update [--beta]
# `--beta` (or env `KBAGENT_INCLUDE_PRERELEASE=1`) opts into pre-release versions
# (PEP 440 betas/rc, e.g. 0.43.0b1). Default (no flag) is stable-only -- auto-update
# startup hook never silently lands on a beta.
# Since 0.60.0 install + self-update prefer a prebuilt wheel Release asset (fast, no
# source build; falls back to git+ when absent). Env `KBAGENT_UPDATE_TIMEOUT` (integer
# seconds, default 300) raises the self-update subprocess timeout for the slow git+
# fallback on WSL. Bootstrap install: `curl -LsSf .../main/install.sh | sh`.
# Since 0.79.0 a STANDALONE (PyInstaller) binary (choco/winget/brew/apt/dnf/zip) refuses
# the kbagent self-update stage and reports that channel's own command instead -- a uv/pip
# reinstall would install a SECOND, unrelated kbagent that shadows it on PATH. `version
# --json` gains additive `install_channel` + `upgrade_hint`; `upgrade_command` is empty for
# a hand-unpacked archive. The keboola-mcp-server stage still runs (separate distribution).
# Since 0.76.2 self-update completes discovery first, updates MCP before the terminal
# exact-version full kbagent reinstall, then immediately re-executes; failures print a
# copy-paste recovery command.
kbagent changelog [--limit N] [--full]
# Default shows a one-line summary (first sentence) per version; --full / -v expands every note.
kbagent serve [--host HOST] [--port PORT] [--ui] [--ui-dist PATH] [--reload] [--log-level LVL] [--cors-origin ORIGIN] [--config-dir DIR]
```
