# Contributing to kbagent

Guidelines for anyone contributing to this project -- human or AI agent.
Read this **before** writing code. It will save review rounds.

## Coding Style

### Python conventions

- **Python 3.11+** -- use modern syntax (`str | None`, not `Optional[str]`)
- **Type hints** on all function signatures
- **f-strings** for string formatting (no `.format()` or `%`)
- **`pathlib.Path`** over `os.path` -- consistently used throughout the project
- **`httpx`** over `requests` for HTTP calls
- **PEP 8 naming**: `snake_case` for functions/variables, `PascalCase` for classes
- **Pydantic 2.x** (`BaseModel`) for all data models -- defined in `models.py`
- **Specific exception handling** -- never bare `except:`
- **`logging`** module for production logging, not `print()`
- Code is formatted with **ruff** -- run `make format` before committing

### 3-Layer architecture -- respect the boundaries

```
CLI Commands (commands/)  -->  Services (services/)  -->  API Client (client.py, manage_client.py)
  Typer, output                 Business logic             HTTP, endpoints
```

| Layer | What goes here | What does NOT go here |
|-------|---------------|----------------------|
| **Commands** (`commands/`) | Typer option parsing, `OutputFormatter` calls, error-to-exit-code mapping | Business logic, HTTP calls, data transformation |
| **Services** (`services/`) | Orchestration, validation, data normalization, parallel execution | Typer imports, output formatting, raw HTTP |
| **Clients** (`client.py`, etc.) | HTTP requests, URL construction, response parsing, retry logic | Business decisions, output formatting |

When adding a new feature, you will almost always touch all three layers.
If you find yourself importing `typer` in a service or calling `httpx` in a command, stop -- you're in the wrong layer.

### Thin commands, smart services

Commands are thin wrappers. All they do:
1. Parse Typer arguments
2. Call a service method
3. Format and output the result
4. Catch `KeboolaApiError` / `ConfigError` and map to exit codes

```python
# GOOD -- command is thin
@storage_app.command("create-bucket")
def storage_create_bucket(ctx, project, stage, name):
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    try:
        result = service.create_bucket(alias=project, stage=stage, name=name)
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    formatter.output(result) if formatter.json_mode else ...

# BAD -- business logic leaked into command
@storage_app.command("create-bucket")
def storage_create_bucket(ctx, project, stage, name):
    if stage not in ("in", "out"):  # This belongs in service!
        ...
    client = KeboolaClient(...)     # This belongs in service!
    client.create_bucket(...)       # Commands don't call clients!
```

### Validate at system boundaries

User input coming through CLI arguments is a **system boundary** -- validate it.
Internal code passing data between layers is trusted -- don't over-validate.

Validation belongs in the **service layer** (not commands, not clients):
- Enum values (stage: `in`/`out`, column types, status filters)
- Format constraints (table ID format, bucket ID format)
- File existence checks
- Business rule validation

For CLI options with a small fixed set of values, prefer Typer/Click's `Choice`:
```python
stage: str = typer.Option(..., click_type=click.Choice(["in", "out"]))
```

### Dual output -- every command must support both modes

- `--json` mode: structured JSON via `formatter.output(data)`
- Human mode: Rich-formatted tables/text via `formatter.console.print()`

Never print raw text that breaks JSON parsing. Always check `formatter.json_mode`.

### Error handling

- Commands catch `KeboolaApiError` and `ConfigError`, map to exit codes
- Exit codes: 0=success, 1=general, 2=usage, 3=auth, 4=network, 5=config, 6=permission denied
- Multi-project operations accumulate errors -- one project failing doesn't stop others
- Use `raise typer.Exit(code=N) from None` to suppress traceback in CLI

### Constants -- no magic numbers

All configuration values go in `constants.py` or a dedicated config:
- Timeouts, retry counts, polling intervals
- Valid enum values (column types, stages)
- API endpoint paths if reused

```python
# BAD
time.sleep(2)
if retries > 3:

# GOOD
from .constants import POLL_INTERVAL, MAX_RETRIES
time.sleep(POLL_INTERVAL)
if retries > MAX_RETRIES:
```

## Keboola API Best Practices

### Reference implementation

The official Keboola CLI is written in Go: https://github.com/keboola/keboola-as-code

Before implementing any Keboola Storage API integration, **check how the official CLI does it**.
It is the authoritative source for correct API usage patterns -- endpoint selection,
async vs. sync behavior, polling strategies, and error handling. If our implementation
diverges from theirs, we need a documented reason why.

### Prefer async API endpoints over sync shortcuts

Many Storage API operations offer both sync and async variants. Sync endpoints are
simpler but have lower limits (e.g., file size caps, timeouts). Always use the async
variant for production code unless there is a specific reason not to.

Use `_wait_for_storage_job()` from `client.py` for polling -- it already handles
intervals, backoff, timeout, and error extraction.

### Graceful resource creation (UX principle)

When the user's intent is clear (e.g., "upload data to this table"), don't force them
to manually create every prerequisite. If a bucket or table doesn't exist yet and can
be inferred from context, create it automatically and log what you did. The official
KBC CLI follows this pattern -- see `EnsureBucketExists()` in their codebase.

## Security Principles

### Permission engine -- register every new operation

Every CLI command must be registered in `OPERATION_REGISTRY` in `src/keboola_agent_cli/permissions.py`.
This maps operations to risk categories:

| Category | Examples | Risk |
|----------|----------|------|
| `read` | list, detail, status, search | No side effects |
| `write` | create, update, upload, load | Creates or modifies data |
| `destructive` | delete, force-delete | Destroys data |
| `admin` | org setup, project add/remove | Infrastructure-level |

**If you add a new command and forget to register it, the permission engine silently allows it** -- even when the user has set a restrictive policy. This is a security gap. Treat unregistered operations as a bug.

Pattern: `"{subapp}.{command}": "{category}"`, e.g.:
```python
"storage.create-bucket": "write",
"storage.create-table": "write",
"storage.upload-table": "write",
```

### Token security

- Tokens are **never** printed in full -- use `mask_token()` from `errors.py`
- Manage tokens: never persisted, never in CLI args, never logged
- Master tokens: via env var only (`KBC_MASTER_TOKEN` / `KBC_MASTER_TOKEN_{ALIAS}`)
- Never commit secrets to git -- not in code, not in docs, not in test fixtures

### Input validation for API-bound data

Validate user-supplied values before sending them to Keboola API:
- Column types against known base types: `STRING`, `INTEGER`, `NUMERIC`, `FLOAT`, `BOOLEAN`, `DATE`, `TIMESTAMP`
- Bucket stages: `in`, `out`
- File existence before upload attempts
- Table/bucket ID format where reasonable

Fail fast with clear error messages rather than letting the API return opaque errors.

## Checklist: Adding a New CLI Command

When adding a new command (e.g., `kbagent storage create-foo`), you must update **all** of these:

### Code changes

- [ ] **Client method** in `client.py` (or `manage_client.py`) -- HTTP layer
- [ ] **Service method** in `services/` -- business logic, validation, orchestration
- [ ] **Command function** in `commands/` -- Typer options, formatter, error handling
- [ ] **`--hint` support** -- every command must support `--hint client` and `--hint service` code generation:
  - [ ] **Hint definition** in `hints/definitions/` -- register a `CommandHint` with `ClientCall` + `ServiceCall` (see existing files for pattern)
  - [ ] **Hint short-circuit** in the command function -- add `if should_hint(ctx): emit_hint(...)` **before** the service call
  - [ ] **Verify** both modes produce valid Python: `kbagent --hint client <command> ...` and `kbagent --hint service <command> ...`
- [ ] **Permission registration** in `permissions.py` (`OPERATION_REGISTRY` dict)
- [ ] **Service wiring** in `cli.py` if adding a new service class

### Documentation changes (mandatory!)

- [ ] **`kbagent context`** -- update `AGENT_CONTEXT` string in `commands/context.py` (this is the primary reference for AI agents; if it's missing there, AI agents won't know the command exists)
- [ ] **SKILL.md** -- run `make skill-gen` to regenerate the decision table (CI has a freshness check that will fail if the generated output doesn't match). **Do not edit SKILL.md by hand** -- the table is auto-generated from CLI command metadata
- [ ] **CLAUDE.md** -- add command signature to the `## All CLI Commands` section
- [ ] **Plugin references** -- update `plugins/kbagent/skills/kbagent/references/`:
  - [ ] **`commands-reference.md`** -- add the new command to the appropriate section (this is a hand-maintained file, NOT auto-generated)
  - [ ] **New reference file** -- if the command introduces a new workflow or topic area (e.g. a new subcommand group), create a dedicated `<topic>-workflow.md` in the references directory. Existing examples: `workspace-workflow.md`, `branch-workflow.md`, `sync-workflow.md`, `storage-files-workflow.md`
  - [ ] **`gotchas.md`** -- if the command has non-obvious behavior, response format quirks, or common mistakes, document them here
- [ ] **`--help` text** -- Typer docstring and option help strings should be clear and complete

### Tests (mandatory!)

- [ ] **Service-layer tests** -- mock the client, test business logic, edge cases, error propagation
- [ ] **CLI-layer tests** -- use `CliRunner`, test JSON output, error exit codes
- [ ] **E2E tests** -- add a test in `tests/test_e2e.py` that exercises the command against a real Keboola project (requires `E2E_API_TOKEN` + `E2E_URL`). Run `make test-e2e` to verify. Every CLI command must have E2E coverage
- [ ] **Run `make check`** before committing (lint + format + full test suite)

### UX considerations

- [ ] Human-mode output is informative (sizes, counts, progress indicators)
- [ ] JSON-mode output includes all relevant fields for programmatic consumption
- [ ] Error messages are actionable ("Bucket not found" not just "404")
- [ ] Destructive operations have `--dry-run` and `--yes` flags
- [ ] Write operations log what they did (created X, uploaded Y rows)

## Commit & PR Conventions

- **No `Co-Authored-By`** lines in commit messages
- **No AI attribution footers** in PR descriptions
- **Conventional commits**: `feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`
- **One logical change per commit** -- don't mix unrelated fixes
- **Pre-commit hook must pass** -- `ruff check` + `ruff format --check`. Install via `make hooks`
- **Never skip hooks** (`--no-verify`) -- fix the lint issue instead
- **Protected main branch** -- always work on a feature branch, create PR, merge via GitHub

For reference on commit style: https://github.com/padak/claude-code-kit/blob/main/CLAUDE.md

## Testing Guidelines

- Use `typer.testing.CliRunner` for CLI tests
- Use `unittest.mock.MagicMock` for mocking services and clients
- Use `pytest` fixtures from `conftest.py` (`tmp_config_dir`, `config_store`, etc.)
- Test both success and error paths
- Test JSON output parsing (`json.loads(result.output)["data"]`)
- Verify `client.close()` is called (via `mock_client.close.assert_called_once()`)
- Test edge cases: missing project alias, API errors, invalid input
- Match test file naming: `test_{feature}.py` or `test_{feature}_cli.py`

## Running CI Locally

```bash
make check          # Full CI: lint + format check + all tests
make lint           # Just ruff linter
make format         # Auto-format code
make test           # Just tests
make skill-gen      # Regenerate SKILL.md from CLI command metadata
```

Always run `make check` before pushing. The PR won't pass CI if lint or tests fail.

**SKILL.md freshness check**: CI verifies that `plugins/kbagent/skills/kbagent/SKILL.md` matches the auto-generated output from `make skill-gen`. If you added, removed, or renamed any CLI command, run `make skill-gen` and commit the result. Manual edits to the decision table will be overwritten and will cause CI to fail.
