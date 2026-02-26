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
  __init__.py           # __version__ = "0.1.0"
  __main__.py           # python -m support
  cli.py                # Typer root app, global options, subcommand wiring
  client.py             # LAYER 3: HTTP client (only module talking to Keboola API)
  config_store.py       # JSON persistence for config.json (0600 permissions)
  models.py             # Pydantic models shared across layers
  output.py             # OutputFormatter: JSON vs Rich dual-mode output
  errors.py             # KeboolaApiError, ConfigError, mask_token()
  commands/
    project.py          # LAYER 1: CLI commands for project management
    config.py           # LAYER 1: CLI commands for config browsing
    context.py          # LAYER 1: Agent usage instructions
    doctor.py           # LAYER 1: Health check command
  services/
    project_service.py  # LAYER 2: Business logic for projects
    config_service.py   # LAYER 2: Business logic for configurations

tests/
  conftest.py           # Shared fixtures (tmp_config_dir, config_store, formatters)
  test_cli.py           # End-to-end CLI tests via CliRunner
  test_client.py        # API client tests with mocked HTTP
  test_config_store.py  # Config persistence tests
  test_errors.py        # mask_token() tests
  test_models.py        # Pydantic model tests
  test_output.py        # OutputFormatter tests
  test_services.py      # Business logic tests
```

## Architecture: 3-Layer Design

```
CLI Commands (commands/)  -->  Services (services/)  -->  API Client (client.py)
  Typer, output                 Business logic             HTTP, endpoints
```

- API changes: modify only `client.py`
- Business logic changes: modify only `services/`
- UI changes: modify only `commands/`

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

10. **Dependencies**: typer, rich, httpx, pydantic, platformdirs. Dev: pytest, pytest-httpx.
