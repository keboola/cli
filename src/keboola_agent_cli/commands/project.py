"""Project management commands - add, list, remove, edit, status.

Thin CLI layer: parses arguments, calls ProjectService, formats output.
No business logic belongs here.
"""

import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from ..constants import (
    DEFAULT_STACK_URL,
    DEFAULT_TOKEN_DESCRIPTION,
    ENV_KBC_STORAGE_API_URL,
    ENV_KBC_TOKEN,
)
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._helpers import (
    check_cli_permission,
    emit_hint,
    get_formatter,
    get_service,
    map_error_to_exit_code,
    resolve_manage_token,
    should_hint,
)
from ._metadata_input import resolve_text_input

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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None


def _format_refresh_result(console: Console, data: dict) -> None:
    """Render token refresh results as Rich tables with summary."""
    dry_run = data.get("dry_run", False)
    mode_label = "[bold yellow]DRY RUN[/bold yellow] " if dry_run else ""
    console.print(f"\n{mode_label}Token Refresh\n")

    # Refreshed projects
    refreshed = data.get("projects_refreshed", [])
    if refreshed:
        action_label = "Projects to Refresh" if dry_run else "Projects Refreshed"
        table = Table(title=action_label)
        table.add_column("Alias", style="bold cyan")
        table.add_column("Project ID", justify="right")
        table.add_column("Project Name")
        if not dry_run:
            table.add_column("Token", style="dim")

        for p in refreshed:
            if dry_run:
                table.add_row(p["alias"], str(p["project_id"]), p["project_name"])
            else:
                table.add_row(
                    p["alias"], str(p["project_id"]), p["project_name"], p.get("token", "")
                )

        console.print(table)
        console.print()

    # Valid projects (tokens that were fine)
    valid = data.get("projects_valid", [])
    if valid:
        table = Table(title="Projects Valid")
        table.add_column("Alias", style="bold cyan")
        table.add_column("Project ID", justify="right")
        table.add_column("Project Name")

        for p in valid:
            table.add_row(p["alias"], str(p["project_id"]), p["project_name"])

        console.print(table)
        console.print()

    # Skipped projects
    skipped = data.get("projects_skipped", [])
    if skipped:
        table = Table(title="Projects Skipped")
        table.add_column("Alias", style="bold cyan")
        table.add_column("Reason", style="dim")

        for p in skipped:
            table.add_row(p["alias"], p["reason"])

        console.print(table)
        console.print()

    # Failed projects
    failed = data.get("projects_failed", [])
    if failed:
        table = Table(title="Projects Failed")
        table.add_column("Alias", style="bold cyan")
        table.add_column("Error", style="bold red")

        for p in failed:
            table.add_row(p["alias"], p["error"])

        console.print(table)
        console.print()

    # Summary line
    summary_parts = []
    if refreshed:
        verb = "to refresh" if dry_run else "refreshed"
        summary_parts.append(f"[bold green]{len(refreshed)}[/bold green] {verb}")
    if valid:
        summary_parts.append(f"[bold green]{len(valid)}[/bold green] valid")
    if skipped:
        summary_parts.append(f"[dim]{len(skipped)} skipped[/dim]")
    if failed:
        summary_parts.append(f"[bold red]{len(failed)} failed[/bold red]")

    console.print("Summary: " + ", ".join(summary_parts) if summary_parts else "No changes.")


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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None


@project_app.command("refresh")
def project_refresh(
    ctx: typer.Context,
    project: str | None = typer.Option(
        None, "--project", "-p", help="Refresh token for a specific project"
    ),
    all_projects: bool = typer.Option(
        False, "--all", help="Refresh all projects with invalid tokens"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview what would be refreshed without making changes"
    ),
    force: bool = typer.Option(False, "--force", help="Refresh even if token is valid"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    token_description: str = typer.Option(
        DEFAULT_TOKEN_DESCRIPTION,
        "--token-description",
        help="Description prefix for created Storage API tokens",
    ),
    token_expires_in: int | None = typer.Option(
        None,
        "--token-expires-in",
        min=1,
        help="Token lifetime in seconds. If not set, tokens never expire.",
    ),
) -> None:
    """Refresh expired or invalid Storage API tokens.

    Creates new tokens via the Manage API and updates the local config.
    Requires a Manage API token (via KBC_MANAGE_API_TOKEN env var or interactive prompt).

    \b
    Examples:
        kbagent project refresh --project prod
        kbagent project refresh --all
        kbagent project refresh --all --dry-run
        kbagent project refresh --all --force
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "org_service")

    # Validate: must have --project or --all, not both, not neither
    if project and all_projects:
        formatter.error(
            message="Provide --project or --all, not both",
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2)
    if not project and not all_projects:
        formatter.error(
            message="Provide --project or --all",
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2)

    manage_token = resolve_manage_token()

    aliases = [project] if project else None

    # Build kwargs shared by preview and real call
    refresh_kwargs: dict = {
        "manage_token": manage_token,
        "aliases": aliases,
        "token_description": token_description,
        "token_expires_in": token_expires_in,
        "force": force,
    }

    # Interactive safety: show preview first, then confirm
    interactive = not formatter.json_mode and not yes and not dry_run
    if interactive:
        try:
            preview = service.refresh_tokens(**refresh_kwargs, dry_run=True)
        except KeboolaApiError as exc:
            exit_code = map_error_to_exit_code(exc)
            formatter.error(
                message=exc.message,
                error_code=exc.error_code,
                retryable=exc.retryable,
            )
            raise typer.Exit(code=exit_code) from None

        _format_refresh_result(formatter.console, preview)

        would_refresh = len(preview.get("projects_refreshed", []))
        if would_refresh == 0:
            formatter.console.print("\nAll tokens are valid.")
            return

        if not typer.confirm(f"\nProceed to refresh {would_refresh} token(s)?"):
            formatter.console.print("Aborted.")
            raise typer.Exit(code=0)

    # Execute the actual refresh
    try:
        result = service.refresh_tokens(**refresh_kwargs, dry_run=dry_run)
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None

    formatter.output(result, _format_refresh_result)


# ── Project description (dashboard KBC.projectDescription) ────────────


@project_app.command("description-get")
def project_description_get(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias to query",
    ),
) -> None:
    """Get the Keboola dashboard project description.

    Reads the ``KBC.projectDescription`` metadata value from the default
    branch - this is what the Keboola UI shows on the project dashboard.
    Returns an empty string if no description has been set.
    """
    if should_hint(ctx):
        emit_hint(ctx, "project.description-get", project=project)
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "branch_service")

    try:
        result = service.get_project_description(alias=project)
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=exit_code) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    formatter.output(
        result,
        lambda c, d: c.print(d["description"] or "[dim](no description set)[/dim]"),
    )


@project_app.command("description-set")
def project_description_set(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias to update",
    ),
    text: str | None = typer.Option(None, "--text", help="Inline description string"),
    file: Path | None = typer.Option(
        None,
        "--file",
        help="Read description from a UTF-8 markdown file",
    ),
    stdin: bool = typer.Option(
        False,
        "--stdin",
        help="Read description from standard input",
    ),
) -> None:
    """Set the Keboola dashboard project description (markdown).

    Writes to ``KBC.projectDescription`` on the default branch. Provide the
    content via exactly one of --text, --file, or --stdin.
    """
    formatter = get_formatter(ctx)

    try:
        description = resolve_text_input(text=text, file=file, stdin=stdin)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.INVALID_ARGUMENT)
        raise typer.Exit(code=2) from None

    if should_hint(ctx):
        emit_hint(
            ctx,
            "project.description-set",
            project=project,
            description=description,
        )
        return
    service = get_service(ctx, "branch_service")

    try:
        result = service.set_project_description(alias=project, description=description)
        formatter.output(
            result,
            lambda c, d: c.print(f"[bold green]Success:[/bold green] {d['message']}"),
        )
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=exit_code) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
