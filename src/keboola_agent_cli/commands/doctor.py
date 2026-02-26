"""Doctor command - comprehensive health check for CLI configuration and connectivity."""

import json
import os
import stat
import time
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .. import __version__
from ..client import KeboolaClient
from ..config_store import ConfigStore
from ..errors import ConfigError, KeboolaApiError, mask_token
from ..models import AppConfig
from ..output import OutputFormatter


def _get_formatter(ctx: typer.Context) -> OutputFormatter:
    """Retrieve the OutputFormatter from the Typer context."""
    return ctx.obj["formatter"]


def _get_config_store(ctx: typer.Context) -> ConfigStore:
    """Retrieve the ConfigStore from the Typer context."""
    return ctx.obj["config_store"]


def _check_config_exists(config_store: ConfigStore) -> dict[str, Any]:
    """Check 1: Config file exists and has correct permissions (0600)."""
    config_path = config_store.config_path
    result: dict[str, Any] = {
        "check": "config_exists",
        "name": "Config file exists",
        "path": str(config_path),
    }

    if not config_path.exists():
        result["status"] = "warn"
        result["message"] = f"Config file not found at {config_path}. Run 'kbagent project add' to create it."
        return result

    # Check permissions (Unix only)
    try:
        file_stat = os.stat(config_path)
        mode = stat.S_IMODE(file_stat.st_mode)
        result["permissions"] = oct(mode)
        if mode == 0o600:
            result["status"] = "ok"
            result["message"] = f"Config file exists at {config_path} with correct permissions (0600)."
        else:
            result["status"] = "warn"
            result["message"] = (
                f"Config file exists at {config_path} but has permissions {oct(mode)} "
                f"(expected 0o600). Run: chmod 600 {config_path}"
            )
    except OSError as exc:
        result["status"] = "error"
        result["message"] = f"Cannot check file permissions: {exc}"

    return result


def _check_config_valid(config_store: ConfigStore) -> tuple[dict[str, Any], AppConfig | None]:
    """Check 2: Config file is valid JSON and parseable.

    Returns the check result and the loaded AppConfig (or None on failure).
    """
    result: dict[str, Any] = {
        "check": "config_valid",
        "name": "Config file is valid",
    }

    config_path = config_store.config_path
    if not config_path.exists():
        result["status"] = "skip"
        result["message"] = "Config file does not exist, skipping validation."
        return result, None

    try:
        config = config_store.load()
        num_projects = len(config.projects)
        result["status"] = "ok"
        result["message"] = f"Config file is valid JSON with {num_projects} project(s) configured."
        result["version"] = config.version
        result["project_count"] = num_projects
        return result, config
    except ConfigError as exc:
        result["status"] = "error"
        result["message"] = f"Config file is invalid: {exc.message}"
        return result, None
    except json.JSONDecodeError as exc:
        result["status"] = "error"
        result["message"] = f"Config file contains invalid JSON: {exc}"
        return result, None


def _check_project_connectivity(
    alias: str,
    stack_url: str,
    token: str,
    client_factory: Any = None,
) -> dict[str, Any]:
    """Check 3: Verify a project's token via API call with response time."""
    result: dict[str, Any] = {
        "check": "project_connectivity",
        "name": f"Project '{alias}' connectivity",
        "alias": alias,
        "stack_url": stack_url,
        "token": mask_token(token),
    }

    if client_factory is not None:
        client = client_factory(stack_url, token)
    else:
        client = KeboolaClient(stack_url=stack_url, token=token)

    start = time.monotonic()
    try:
        token_info = client.verify_token()
        elapsed = time.monotonic() - start
        result["status"] = "ok"
        result["response_time_ms"] = round(elapsed * 1000)
        result["project_name"] = token_info.project_name
        result["project_id"] = token_info.project_id
        result["message"] = (
            f"Connected to '{token_info.project_name}' (ID: {token_info.project_id}) "
            f"in {result['response_time_ms']}ms."
        )
    except KeboolaApiError as exc:
        elapsed = time.monotonic() - start
        result["status"] = "error"
        result["response_time_ms"] = round(elapsed * 1000)
        result["error_code"] = exc.error_code
        result["message"] = f"Connection failed: {exc.message}"
    finally:
        client.close()

    return result


def _check_cli_version() -> dict[str, Any]:
    """Check 4: CLI version information."""
    return {
        "check": "cli_version",
        "name": "CLI version",
        "status": "ok",
        "version": __version__,
        "message": f"kbagent version {__version__}",
    }


def _render_human_output(console: Console, checks: list[dict[str, Any]]) -> None:
    """Render doctor results as a Rich panel with colored status indicators."""
    table = Table(show_header=True, header_style="bold", expand=True)
    table.add_column("Check", style="bold")
    table.add_column("Status", justify="center", width=8)
    table.add_column("Details")

    status_icons = {
        "ok": "[bold green]PASS[/bold green]",
        "warn": "[bold yellow]WARN[/bold yellow]",
        "error": "[bold red]FAIL[/bold red]",
        "skip": "[dim]SKIP[/dim]",
    }

    for check in checks:
        status = check.get("status", "error")
        icon = status_icons.get(status, "[bold red]FAIL[/bold red]")
        table.add_row(
            check.get("name", check.get("check", "Unknown")),
            icon,
            check.get("message", ""),
        )

    panel = Panel(table, title="kbagent Doctor", border_style="blue")
    console.print(panel)


def doctor_command(ctx: typer.Context) -> None:
    """Run health checks on CLI configuration and project connectivity."""
    formatter = _get_formatter(ctx)
    config_store = _get_config_store(ctx)

    # Optionally accept a client_factory from context (for testing)
    client_factory = ctx.obj.get("client_factory")

    checks: list[dict[str, Any]] = []

    # Check 1: Config file existence and permissions
    checks.append(_check_config_exists(config_store))

    # Check 2: Config file validity
    validity_result, config = _check_config_valid(config_store)
    checks.append(validity_result)

    # Check 3: Project connectivity (for each configured project)
    if config and config.projects:
        for alias, project in config.projects.items():
            checks.append(
                _check_project_connectivity(
                    alias=alias,
                    stack_url=project.stack_url,
                    token=project.token,
                    client_factory=client_factory,
                )
            )

    # Check 4: CLI version
    checks.append(_check_cli_version())

    formatter.output(checks, _render_human_output)
