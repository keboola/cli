"""Project management commands - add, list, remove, edit, status.

Thin CLI layer: parses arguments, calls ProjectService, formats output.
No business logic belongs here.
"""

from typing import Any, Optional

import typer
from rich.console import Console
from rich.table import Table

from ..errors import ConfigError, KeboolaApiError
from ..output import OutputFormatter
from ..services.project_service import ProjectService

project_app = typer.Typer(help="Manage connected Keboola projects")


def _get_formatter(ctx: typer.Context) -> OutputFormatter:
    """Retrieve the OutputFormatter from the Typer context."""
    return ctx.obj["formatter"]


def _get_service(ctx: typer.Context) -> ProjectService:
    """Retrieve the ProjectService from the Typer context."""
    return ctx.obj["project_service"]


def _format_project_table(console: Console, projects: list[dict[str, Any]]) -> None:
    """Render a Rich table of projects for human output."""
    if not projects:
        console.print("No projects configured. Use [bold]kbagent project add[/bold] to add one.")
        return

    table = Table(title="Connected Projects")
    table.add_column("Alias", style="bold cyan")
    table.add_column("Project Name")
    table.add_column("Project ID", justify="right")
    table.add_column("Stack URL")
    table.add_column("Token", style="dim")
    table.add_column("Default", justify="center")

    for p in projects:
        default_marker = "*" if p.get("is_default") else ""
        table.add_row(
            p["alias"],
            p.get("project_name", ""),
            str(p.get("project_id", "")),
            p["stack_url"],
            p["token"],
            default_marker,
        )

    console.print(table)


def _format_status_table(console: Console, statuses: list[dict[str, Any]]) -> None:
    """Render a Rich table of project connectivity statuses."""
    if not statuses:
        console.print("No projects configured.")
        return

    table = Table(title="Project Status")
    table.add_column("Alias", style="bold cyan")
    table.add_column("Status")
    table.add_column("Response Time", justify="right")
    table.add_column("Project Name")
    table.add_column("Stack URL")

    for s in statuses:
        if s["status"] == "ok":
            status_str = "[bold green]OK[/bold green]"
        else:
            status_str = f"[bold red]ERROR[/bold red]: {s.get('error', 'Unknown')}"
        response_time = f"{s.get('response_time_ms', 0)}ms"
        table.add_row(
            s["alias"],
            status_str,
            response_time,
            s.get("project_name", ""),
            s["stack_url"],
        )

    console.print(table)


@project_app.command("add")
def project_add(
    ctx: typer.Context,
    alias: str = typer.Option(..., help="Human-friendly name for this project"),
    url: str = typer.Option(
        "https://connection.keboola.com",
        help="Keboola stack URL",
        envvar="KBC_STORAGE_API_URL",
    ),
    token: str = typer.Option(
        ...,
        help="Storage API token",
        envvar="KBC_TOKEN",
    ),
) -> None:
    """Add a new Keboola project connection."""
    formatter = _get_formatter(ctx)
    service = _get_service(ctx)

    try:
        result = service.add_project(alias=alias, stack_url=url, token=token)
        formatter.output(result, lambda c, d: c.print(
            f"[bold green]Success:[/bold green] Project [bold]{d['alias']}[/bold] added "
            f"(project: {d['project_name']}, id: {d['project_id']})"
        ))
    except KeboolaApiError as exc:
        exit_code = 3 if exc.error_code == "INVALID_TOKEN" else 4
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5)


@project_app.command("list")
def project_list(ctx: typer.Context) -> None:
    """List all connected Keboola projects."""
    formatter = _get_formatter(ctx)
    service = _get_service(ctx)

    try:
        projects = service.list_projects()
        formatter.output(projects, _format_project_table)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5)


@project_app.command("remove")
def project_remove(
    ctx: typer.Context,
    alias: str = typer.Option(..., help="Alias of the project to remove"),
) -> None:
    """Remove a Keboola project connection."""
    formatter = _get_formatter(ctx)
    service = _get_service(ctx)

    try:
        result = service.remove_project(alias=alias)
        formatter.output(result, lambda c, d: c.print(
            f"[bold green]Success:[/bold green] {d['message']}"
        ))
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5)


@project_app.command("edit")
def project_edit(
    ctx: typer.Context,
    alias: str = typer.Option(..., help="Alias of the project to edit"),
    url: Optional[str] = typer.Option(None, help="New Keboola stack URL"),
    token: Optional[str] = typer.Option(None, help="New Storage API token"),
) -> None:
    """Edit an existing Keboola project connection."""
    formatter = _get_formatter(ctx)
    service = _get_service(ctx)

    try:
        result = service.edit_project(alias=alias, stack_url=url, token=token)
        formatter.output(result, lambda c, d: c.print(
            f"[bold green]Success:[/bold green] Project [bold]{d['alias']}[/bold] updated."
        ))
    except KeboolaApiError as exc:
        exit_code = 3 if exc.error_code == "INVALID_TOKEN" else 4
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5)


@project_app.command("status")
def project_status(
    ctx: typer.Context,
    project: Optional[str] = typer.Option(None, "--project", help="Check only this project (default: all)"),
) -> None:
    """Test connectivity to connected Keboola projects."""
    formatter = _get_formatter(ctx)
    service = _get_service(ctx)

    aliases = [project] if project else None

    try:
        statuses = service.get_status(aliases=aliases)
        formatter.output(statuses, _format_status_table)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5)
    except KeboolaApiError as exc:
        exit_code = 3 if exc.error_code == "INVALID_TOKEN" else 4
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code)
