"""Doctor command - comprehensive health check for CLI configuration and connectivity.

Runs four checks:
1. Config file existence and permissions (0600)
2. Config file valid JSON and parseable
3. Token verification for each project (API call with response time)
4. CLI version
"""

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
from ..errors import KeboolaApiError, mask_token
from ..models import AppConfig
from ..output import OutputFormatter
from ..services.project_service import ClientFactory, default_client_factory


def _get_formatter(ctx: typer.Context) -> OutputFormatter:
    """Retrieve the OutputFormatter from the Typer context."""
    return ctx.obj["formatter"]


def _get_config_store(ctx: typer.Context) -> ConfigStore:
    """Retrieve the ConfigStore from the Typer context."""
    return ctx.obj["config_store"]


def _check_config_file(config_store: ConfigStore) -> dict[str, Any]:
    """Check 1: Config file exists and has correct permissions (0600).

    Returns:
        Dict with check name, status (pass/fail/warn), and message.
    """
    config_path = config_store.config_path

    if not config_path.exists():
        return {
            "check": "config_file",
            "name": "Config file",
            "status": "warn",
            "message": f"Config file not found at {config_path}. Run 'kbagent project add' to create it.",
        }

    # Check permissions (Unix only)
    try:
        file_stat = os.stat(config_path)
        mode = stat.S_IMODE(file_stat.st_mode)
        if mode != 0o600:
            return {
                "check": "config_file",
                "name": "Config file",
                "status": "warn",
                "message": f"Config file exists at {config_path} but has permissions {oct(mode)} (expected 0o600).",
            }
    except OSError:
        # On platforms where permission checking is not reliable
        pass

    return {
        "check": "config_file",
        "name": "Config file",
        "status": "pass",
        "message": f"Config file exists at {config_path} with correct permissions.",
    }


def _check_config_valid(config_store: ConfigStore) -> tuple[dict[str, Any], AppConfig | None]:
    """Check 2: Config file is valid JSON and parseable.

    Returns:
        Tuple of (check result dict, parsed AppConfig or None on failure).
    """
    config_path = config_store.config_path

    if not config_path.exists():
        return {
            "check": "config_valid",
            "name": "Config parseable",
            "status": "skip",
            "message": "No config file to validate.",
        }, None

    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "check": "config_valid",
            "name": "Config parseable",
            "status": "fail",
            "message": f"Cannot read config file: {exc}",
        }, None

    try:
        json.loads(raw)
    except json.JSONDecodeError as exc:
        return {
            "check": "config_valid",
            "name": "Config parseable",
            "status": "fail",
            "message": f"Config file is not valid JSON: {exc}",
        }, None

    try:
        config = config_store.load()
    except Exception as exc:
        return {
            "check": "config_valid",
            "name": "Config parseable",
            "status": "fail",
            "message": f"Config file has invalid structure: {exc}",
        }, None

    project_count = len(config.projects)
    return {
        "check": "config_valid",
        "name": "Config parseable",
        "status": "pass",
        "message": f"Config file is valid JSON with {project_count} project(s).",
    }, config


def _check_connectivity(
    config: AppConfig | None,
    client_factory: ClientFactory,
) -> list[dict[str, Any]]:
    """Check 3: For each project, verify token via API call.

    Returns:
        List of check result dicts, one per project.
    """
    if config is None or not config.projects:
        return [{
            "check": "connectivity",
            "name": "Project connectivity",
            "status": "skip",
            "message": "No projects configured.",
        }]

    results = []
    for alias, project in config.projects.items():
        client = client_factory(project.stack_url, project.token)
        start_time = time.monotonic()
        try:
            token_info = client.verify_token()
            elapsed = time.monotonic() - start_time
            results.append({
                "check": "connectivity",
                "name": f"Project '{alias}'",
                "status": "pass",
                "message": (
                    f"Connected to {project.stack_url} "
                    f"(project: {token_info.project_name}, id: {token_info.project_id}) "
                    f"in {round(elapsed * 1000)}ms"
                ),
                "alias": alias,
                "response_time_ms": round(elapsed * 1000),
            })
        except KeboolaApiError as exc:
            elapsed = time.monotonic() - start_time
            results.append({
                "check": "connectivity",
                "name": f"Project '{alias}'",
                "status": "fail",
                "message": f"Failed: {exc.message}",
                "alias": alias,
                "error_code": exc.error_code,
                "response_time_ms": round(elapsed * 1000),
            })
        finally:
            client.close()

    return results


def _check_version() -> dict[str, Any]:
    """Check 4: CLI version information.

    Returns:
        Check result dict with the current CLI version.
    """
    return {
        "check": "version",
        "name": "CLI version",
        "status": "pass",
        "message": f"kbagent v{__version__}",
    }


def _format_doctor_human(console: Console, data: dict[str, Any]) -> None:
    """Render doctor check results as a Rich panel with colored status indicators."""
    checks = data.get("checks", [])

    lines = []
    for check in checks:
        status = check["status"]
        if status == "pass":
            icon = "[bold green]PASS[/bold green]"
        elif status == "fail":
            icon = "[bold red]FAIL[/bold red]"
        elif status == "warn":
            icon = "[bold yellow]WARN[/bold yellow]"
        else:
            icon = "[dim]SKIP[/dim]"

        lines.append(f"  {icon}  {check['name']}: {check['message']}")

    summary = data.get("summary", {})
    total = summary.get("total", 0)
    passed = summary.get("passed", 0)
    failed = summary.get("failed", 0)
    warnings = summary.get("warnings", 0)

    lines.append("")
    summary_parts = [f"{total} checks"]
    if passed:
        summary_parts.append(f"[green]{passed} passed[/green]")
    if failed:
        summary_parts.append(f"[red]{failed} failed[/red]")
    if warnings:
        summary_parts.append(f"[yellow]{warnings} warnings[/yellow]")
    lines.append(f"  Summary: {', '.join(summary_parts)}")

    panel = Panel("\n".join(lines), title="kbagent doctor", expand=False)
    console.print(panel)


def doctor_command(ctx: typer.Context) -> None:
    """Run health checks on CLI configuration and project connectivity."""
    formatter = _get_formatter(ctx)
    config_store = _get_config_store(ctx)

    # Determine client factory - use the default unless we're in a test context
    client_factory: ClientFactory = ctx.obj.get("client_factory", default_client_factory)

    all_checks: list[dict[str, Any]] = []

    # Check 1: Config file exists with correct permissions
    file_check = _check_config_file(config_store)
    all_checks.append(file_check)

    # Check 2: Config file is valid JSON and parseable
    valid_check, config = _check_config_valid(config_store)
    all_checks.append(valid_check)

    # Check 3: Project connectivity
    connectivity_checks = _check_connectivity(config, client_factory)
    all_checks.extend(connectivity_checks)

    # Check 4: CLI version
    version_check = _check_version()
    all_checks.append(version_check)

    # Build summary
    total = len(all_checks)
    passed = sum(1 for c in all_checks if c["status"] == "pass")
    failed = sum(1 for c in all_checks if c["status"] == "fail")
    warnings = sum(1 for c in all_checks if c["status"] == "warn")
    skipped = sum(1 for c in all_checks if c["status"] == "skip")

    result = {
        "checks": all_checks,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "warnings": warnings,
            "skipped": skipped,
            "healthy": failed == 0,
        },
    }

    formatter.output(result, _format_doctor_human)
