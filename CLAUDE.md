# CLAUDE.md - Project Development Context

This file provides context for AI coding assistants (Claude Code, etc.) working
on the `kbagent` (Keboola Agent CLI) project.

## Quick Start

### Build and install (editable mode)

```bash
uv pip install -e ".[dev]"
```

### Run the CLI

```bash
kbagent --help
kbagent project list
kbagent --json project list
```

### Run tests

```bash
pytest tests/ -v
```

Or with uv:

```bash
uv run pytest tests/ -v
```

## Project Structure

```
src/keboola_agent_cli/
    __init__.py              # Package init, exports __version__
    __main__.py              # python -m support
    cli.py                   # Typer root app, global options, subcommand registration
    client.py                # HTTP client for Keboola Storage API (retry, timeouts)
    config_store.py          # JSON config persistence (~/.config/keboola-agent-cli/config.json)
    errors.py                # KeboolaApiError, ConfigError, mask_token()
    models.py                # Pydantic models: AppConfig, ProjectConfig, TokenVerifyResponse, etc.
    output.py                # OutputFormatter - dual mode (JSON for agents, Rich for humans)
    commands/
        __init__.py
        project.py           # project add/list/remove/edit/status commands
        config.py            # config list/detail commands
        context.py           # Agent usage instructions
        doctor.py            # Health check command
    services/
        __init__.py
        project_service.py   # Business logic for project management
        config_service.py    # Business logic for config listing (Phase 3)
tests/
    conftest.py              # Shared fixtures (tmp dirs, formatters)
    test_cli.py              # End-to-end CLI tests via CliRunner
    test_client.py           # API client tests (mocked HTTP)
    test_config_store.py     # Config persistence tests
    test_errors.py           # Error handling and token masking tests
    test_models.py           # Pydantic model serialization tests
    test_output.py           # Output formatter tests
    test_services.py         # Service layer business logic tests
```

## Architecture (3-Layer)

```
CLI commands  -->  Services (business logic)  -->  API Client (HTTP)
(Typer, output)    (aggregation, resolving)        (endpoints, requests)
```

- **API changes** --> only modify `client.py`
- **Business logic changes** --> only modify `services/`
- **UI/output changes** --> only modify `commands/`

## Coding Conventions

### Commands (`commands/`)

- Thin layer: parse arguments with Typer, call service, format output.
- No business logic in commands.
- Use `_get_formatter(ctx)` and `_get_service(ctx)` helpers to pull from Typer context.
- All commands handle `KeboolaApiError` and `ConfigError` with proper exit codes.

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error |
| 2 | Usage error (bad arguments) |
| 3 | Authentication error (invalid token) |
| 4 | Network error (timeout, unreachable) |
| 5 | Configuration error (bad config file, missing alias) |

### Services (`services/`)

- Accept `ConfigStore` and `client_factory` via dependency injection.
- `client_factory` is `Callable[[str, str], KeboolaClient]` for easy mocking.
- Return plain dicts (not Pydantic models) so the CLI layer can format freely.

### Models (`models.py`)

- All data contracts are Pydantic v2 models.
- `AppConfig` is the top-level config file schema (versioned).
- `ProjectConfig` stores per-project connection details.
- `SuccessResponse` and `ErrorResponse` define the JSON output envelope.

### Output (`output.py`)

- `OutputFormatter` supports dual mode: `--json` for agents, Rich for humans.
- JSON mode writes to stdout via `SuccessResponse` / `ErrorResponse`.
- Human mode uses `rich.console.Console` with optional color disable.

### Error Handling (`errors.py`)

- `KeboolaApiError`: HTTP/API failures with `error_code`, `status_code`, `retryable`.
- `ConfigError`: Configuration file issues.
- `mask_token()`: Always mask tokens in output (`901-...pt0k`).

### Testing

- Use `pytest` with `typer.testing.CliRunner` for CLI tests.
- Mock `ConfigStore` and `ProjectService` via `unittest.mock.patch`.
- Use `tmp_path` fixture for isolated config directories.
- All API calls in tests must be mocked (no real HTTP).

### Dependencies

- **Typer** (with `rich` extra) for CLI framework
- **Rich** for formatted terminal output
- **httpx** for HTTP client
- **Pydantic v2** for data validation and serialization
- **platformdirs** for cross-platform config paths
