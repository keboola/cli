"""Project management commands - add, list, remove, edit, status.

Thin CLI layer: parses arguments, calls ProjectService, formats output.
No business logic belongs here.
"""

import sys
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from ..constants import DEFAULT_STACK_URL, ENV_KBC_STORAGE_API_URL, ENV_KBC_TOKEN
from ..errors import ConfigError, KeboolaApiError
from ._helpers import check_cli_permission, get_formatter, get_service, map_error_to_exit_code

project_app = typer.Typer(help="Manage connected Keboola projects")


@project_app.callback(invoke_without_command=True)
def _project_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "project")


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
    table.add_column("Branch", justify="center")

    for p in projects:
        default_marker = "*" if p.get("is_default") else ""
        branch_id = p.get("active_branch_id")
        branch_display = str(branch_id) if branch_id is not None else "[dim]main[/dim]"
        table.add_row(
            p["alias"],
            p.get("project_name", ""),
            str(p.get("project_id", "")),
            p["stack_url"],
            p["token"],
            default_marker,
            branch_display,
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
    table.add_column("Branch", justify="center")

    for s in statuses:
        if s["status"] == "ok":
            status_str = "[bold green]OK[/bold green]"
        else:
            status_str = f"[bold red]ERROR[/bold red]: {s.get('error', 'Unknown')}"
        response_time = f"{s.get('response_time_ms', 0)}ms"
        branch_id = s.get("active_branch_id")
        branch_display = str(branch_id) if branch_id is not None else "[dim]main[/dim]"
        table.add_row(
            s["alias"],
            status_str,
            response_time,
            s.get("project_name", ""),
            s["stack_url"],
            branch_display,
        )

    console.print(table)


def _resolve_token(token: str | None) -> str:
    """Resolve the Storage API token, falling back to interactive prompt.

    Token resolution order (Typer handles steps 1-2 automatically via envvar):
    1. --token CLI argument
    2. KBC_TOKEN env var (handled by Typer's envvar parameter)
    3. Interactive prompt with hidden input (if TTY)
    4. Error if none available

    Args:
        token: Token from --token or KBC_TOKEN env var (resolved by Typer), or None.

    Returns:
        The Storage API token.

    Raises:
        typer.Exit: If no token can be resolved.
    """
    if token:
        return token

    is_tty = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
    if is_tty:
        return typer.prompt("Storage API token", hide_input=True)

    typer.echo(
        f"Error: No token available. Pass --token, set {ENV_KBC_TOKEN} env var, "
        "or run interactively.",
        err=True,
    )
    raise typer.Exit(code=2)


@project_app.command("add")
def project_add(
    ctx: typer.Context,
    alias: str = typer.Option(..., "--project", help="Human-friendly name for this project"),
    url: str = typer.Option(
        DEFAULT_STACK_URL,
        help="Keboola stack URL",
        envvar=ENV_KBC_STORAGE_API_URL,
    ),
    token: str | None = typer.Option(
        None,
        help="Storage API token (also via KBC_TOKEN env var)",
        envvar=ENV_KBC_TOKEN,
    ),
) -> None:
    """Add a new Keboola project connection.

    Token is read from --token, KBC_TOKEN env var, or prompted interactively.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "project_service")
    resolved_token = _resolve_token(token)

    try:
        result = service.add_project(alias=alias, stack_url=url, token=resolved_token)
        formatter.output(
            result,
            lambda c, d: c.print(
                f"[bold green]Success:[/bold green] Project [bold]{d['alias']}[/bold] added "
                f"(project: {d['project_name']}, id: {d['project_id']})"
            ),
        )
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None


@project_app.command("list")
def project_list(ctx: typer.Context) -> None:
    """List all connected Keboola projects."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "project_service")

    try:
        projects = service.list_projects()
        formatter.output(projects, _format_project_table)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None


@project_app.command("remove")
def project_remove(
    ctx: typer.Context,
    alias: str = typer.Option(..., "--project", help="Alias of the project to remove"),
) -> None:
    """Remove a Keboola project connection."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "project_service")

    try:
        result = service.remove_project(alias=alias)
        formatter.output(
            result, lambda c, d: c.print(f"[bold green]Success:[/bold green] {d['message']}")
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None


@project_app.command("edit")
def project_edit(
    ctx: typer.Context,
    alias: str = typer.Option(..., "--project", help="Alias of the project to edit"),
    url: str | None = typer.Option(None, help="New Keboola stack URL"),
    token: str | None = typer.Option(
        None,
        help="New Storage API token",
    ),
) -> None:
    """Edit an existing Keboola project connection.

    If --token is provided, the token is re-verified against the API.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "project_service")

    try:
        result = service.edit_project(alias=alias, stack_url=url, token=token)
        formatter.output(
            result,
            lambda c, d: c.print(
                f"[bold green]Success:[/bold green] Project [bold]{d['alias']}[/bold] updated."
            ),
        )
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None


@project_app.command("status")
def project_status(
    ctx: typer.Context,
    project: str | None = typer.Option(
        None, "--project", help="Check only this project (default: all)"
    ),
) -> None:
    """Test connectivity to connected Keboola projects."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "project_service")

    aliases = [project] if project else None

    try:
        statuses = service.get_status(aliases=aliases)
        formatter.output(statuses, _format_status_table)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None
