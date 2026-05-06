"""Project management commands - add, list, remove, edit, status.

Thin CLI layer: parses arguments, calls ProjectService, formats output.
No business logic belongs here.
"""

import sys
from pathlib import Path
from typing import Any

import click
import typer
from rich.console import Console
from rich.table import Table

from ..constants import (
    DEFAULT_INVITE_WORKERS,
    DEFAULT_STACK_URL,
    DEFAULT_TOKEN_DESCRIPTION,
    ENV_KBC_STORAGE_API_URL,
    ENV_KBC_TOKEN,
    PROJECT_ROLES,
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
    Requires a Manage API token: interactive hidden prompt by default
    (since 0.28.0); pass top-level --allow-env-manage-token to read
    KBC_MANAGE_API_TOKEN from env (CI/CD).

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

    manage_token = resolve_manage_token(allow_env=ctx.obj["allow_env_manage_token"])

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


# ── Project pin (default project) ─────────────────────────────────────


@project_app.command("use")
def project_use(
    ctx: typer.Context,
    alias: str = typer.Argument(..., help="Project alias to pin as default"),
) -> None:
    """Pin <alias> as the default project for subsequent commands.

    The pin persists in config.json. ``KBAGENT_PROJECT`` overrides it for a
    single invocation; an explicit ``--project`` flag overrides both.
    """
    # No --hint: local-only ConfigStore mutation; no client or service call to render.
    formatter = get_formatter(ctx)
    service = get_service(ctx, "project_service")

    try:
        result = service.use_project(alias=alias)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    def _human(c: Console, d: dict[str, Any]) -> None:
        previous = d.get("previous")
        if previous and previous != d["alias"]:
            c.print(
                f"[bold green]Pinned:[/bold green] default project is now "
                f"[bold]{d['alias']}[/bold] (was [dim]{previous}[/dim])"
            )
        else:
            c.print(
                f"[bold green]Pinned:[/bold green] default project is [bold]{d['alias']}[/bold]"
            )
        env_override = d.get("env_override")
        if env_override and env_override != d["alias"]:
            c.print(
                f"[yellow]Note:[/yellow] KBAGENT_PROJECT='{env_override}' is set "
                "and overrides this pin for the current shell."
            )

    formatter.output(result, _human)


@project_app.command("current")
def project_current(ctx: typer.Context) -> None:
    """Show the effective default project.

    Reports whether the value comes from the ``KBAGENT_PROJECT`` env var
    (``env``) or the persisted pin (``pin``). Prints nothing but a hint if
    neither is set.
    """
    # No --hint: local-only ConfigStore read; no client or service call to render.
    formatter = get_formatter(ctx)
    service = get_service(ctx, "project_service")

    result = service.current_project()

    def _human(c: Console, d: dict[str, Any]) -> None:
        alias = d.get("alias")
        source = d.get("source")
        if alias is None:
            c.print(
                "[dim](no default project set)[/dim] -- pass --project, set "
                "KBAGENT_PROJECT, or run 'kbagent project use <alias>'"
            )
            return
        if source == "env":
            c.print(f"[bold cyan]{alias}[/bold cyan]  [dim](source: KBAGENT_PROJECT env var)[/dim]")
            if d.get("env_points_to_configured_project") is False:
                c.print(
                    f"[yellow]Warning:[/yellow] '{alias}' is NOT in your "
                    "configured projects. Commands that use this pin will fail."
                )
            pinned = d.get("pinned")
            if pinned:
                c.print(f"[dim]  (pinned in config: {pinned}, overridden)[/dim]")
        else:
            c.print(f"[bold cyan]{alias}[/bold cyan]  [dim](source: pinned default)[/dim]")

    formatter.output(result, _human)


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


# ── Project members & invitations (since v0.26.1) ─────────────────────


def _format_invite_result(console: Console, data: dict[str, Any]) -> None:
    """Single-shot invite result."""
    status = data.get("status", "")
    if status == "ok":
        console.print(
            f"[bold green]Invited[/bold green] {data['email']} to "
            f"[cyan]{data['alias']}[/cyan] as [yellow]{data['role']}[/yellow] "
            f"(invitation_id={data.get('invitation_id')})."
        )
    elif status == "noop":
        console.print(
            f"[yellow]No-op[/yellow]: {data['email']} on [cyan]{data['alias']}[/cyan] "
            f"-- {data.get('note', '')}."
        )
    elif status == "dry_run":
        console.print(
            f"[dim]Would invite[/dim] {data['email']} to [cyan]{data['alias']}[/cyan] "
            f"as [yellow]{data['role']}[/yellow]."
        )
    else:
        console.print(f"[bold red]Unexpected status[/bold red]: {data!r}")


def _format_bulk_invite_result(console: Console, data: dict[str, Any]) -> None:
    """Render the bulk-invite summary table."""
    console.print(
        f"\n[bold]Bulk invite:[/bold] total={data['total']} "
        f"succeeded={data['succeeded']} noop={data['noop']} failed={data['failed']}"
        + (" [dim](dry-run)[/dim]" if data.get("dry_run") else "")
    )
    rows = data.get("rows") or []
    if not rows:
        return
    table = Table(title="Per-row results")
    table.add_column("Status", style="bold")
    table.add_column("Email")
    table.add_column("Project")
    table.add_column("Role")
    table.add_column("Note")
    status_style = {"ok": "green", "noop": "yellow", "failed": "red"}
    for row in rows:
        status = row.get("status", "")
        style = status_style.get(status, "white")
        table.add_row(
            f"[{style}]{status}[/{style}]",
            row.get("email", ""),
            row.get("project", ""),
            row.get("role", ""),
            row.get("note", ""),
        )
    console.print(table)


def _format_member_list(console: Console, data: dict[str, Any]) -> None:
    members = data.get("members") or []
    table = Table(title=f"Members of {data.get('alias')} (project_id={data.get('project_id')})")
    table.add_column("ID", justify="right", style="dim")
    table.add_column("Email")
    table.add_column("Role", style="yellow")
    table.add_column("Status")
    table.add_column("MFA", justify="center")
    for m in members:
        table.add_row(
            str(m.get("id", "")),
            m.get("email", ""),
            m.get("role", ""),
            m.get("status", ""),
            "yes" if m.get("mfa_enabled") else "no",
        )
    console.print(table)
    pending = data.get("pending_invitations")
    if pending:
        ptable = Table(title="Pending invitations")
        ptable.add_column("ID", justify="right", style="dim")
        ptable.add_column("Email")
        ptable.add_column("Role", style="yellow")
        ptable.add_column("Reason")
        for p in pending:
            ptable.add_row(
                str(p.get("id", "")),
                p.get("user", {}).get("email", ""),
                p.get("role", ""),
                p.get("reason", ""),
            )
        console.print(ptable)


def _format_invitation_list(console: Console, data: dict[str, Any]) -> None:
    invitations = data.get("invitations") or []
    if not invitations:
        console.print(f"No pending invitations for [cyan]{data.get('alias')}[/cyan].")
        return
    table = Table(
        title=f"Pending invitations for {data.get('alias')} (project_id={data.get('project_id')})"
    )
    table.add_column("ID", justify="right", style="dim")
    table.add_column("Email")
    table.add_column("Role", style="yellow")
    table.add_column("Reason")
    for inv in invitations:
        table.add_row(
            str(inv.get("id", "")),
            inv.get("user", {}).get("email", ""),
            inv.get("role", ""),
            inv.get("reason", ""),
        )
    console.print(table)


@project_app.command("invite")
def project_invite(
    ctx: typer.Context,
    project: str | None = typer.Option(
        None, "--project", "-p", help="Project alias to invite the user to (single-shot mode)"
    ),
    email: str | None = typer.Option(
        None, "--email", "-e", help="Email address of the user to invite"
    ),
    role: str | None = typer.Option(
        None,
        "--role",
        "-r",
        click_type=click.Choice(list(PROJECT_ROLES)),
        help="Role to grant: " + " | ".join(PROJECT_ROLES),
    ),
    reason: str | None = typer.Option(
        None, "--reason", help="Optional human-readable reason attached to the invitation"
    ),
    from_csv: Path | None = typer.Option(
        None,
        "--from-csv",
        help="CSV file with columns email, project (alias or numeric ID), role[, reason]",
    ),
    default_role: str | None = typer.Option(
        None,
        "--default-role",
        click_type=click.Choice(list(PROJECT_ROLES)),
        help="Role to apply when a CSV row has no role column",
    ),
    workers: int = typer.Option(
        DEFAULT_INVITE_WORKERS,
        "--workers",
        min=1,
        max=32,
        help="Parallel workers for --from-csv (default 8)",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without sending invitations"),
) -> None:
    """Invite a user (or many users via CSV) to one or more projects.

    \b
    Single-shot:
        kbagent project invite --project prod --email a@b.com --role admin

    \b
    Bulk (one row per email; CSV header required):
        kbagent project invite --from-csv participants.csv --default-role guest
    """
    formatter = get_formatter(ctx)

    if from_csv and (project or email):
        formatter.error(
            message="--from-csv is mutually exclusive with --project / --email",
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2)
    if not from_csv and not (project and email and role):
        formatter.error(
            message="Provide --project, --email, and --role for single-shot invite "
            "(or use --from-csv for bulk).",
            error_code=ErrorCode.USAGE_ERROR,
        )
        raise typer.Exit(code=2)

    if should_hint(ctx):
        if from_csv:
            formatter.error(
                message=(
                    "--hint is not available for `project invite --from-csv`. "
                    "Use --hint client/service on a single-shot invite "
                    "(--project + --email + --role) instead, or open the "
                    "MemberService source for the bulk pattern."
                ),
                error_code=ErrorCode.USAGE_ERROR,
            )
            raise typer.Exit(code=2)
        emit_hint(
            ctx,
            "project.invite",
            project=project,
            project_id="<resolved-from-config>",
            email=email,
            role=role,
            reason=reason or "",
        )
        return

    manage_token = resolve_manage_token(allow_env=ctx.obj["allow_env_manage_token"])
    service = get_service(ctx, "member_service")

    try:
        if from_csv:
            result = service.invite_bulk(
                manage_token=manage_token,
                csv_path=from_csv,
                default_role=default_role,
                workers=workers,
                dry_run=dry_run,
            )
            payload = result.model_dump()
            formatter.output(payload, _format_bulk_invite_result)
            return

        result = service.invite(
            manage_token=manage_token,
            alias=project,
            email=email,
            role=role,
            reason=reason,
            dry_run=dry_run,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except ValueError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.VALIDATION_ERROR)
        raise typer.Exit(code=2) from None
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None

    formatter.output(result, _format_invite_result)


@project_app.command("member-list")
def project_member_list(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", "-p", help="Project alias to list members for"),
    include_pending: bool = typer.Option(
        False, "--include-pending", help="Also list pending (unaccepted) invitations"
    ),
) -> None:
    """List active members of a project (and optionally pending invitations)."""
    formatter = get_formatter(ctx)

    if should_hint(ctx):
        emit_hint(
            ctx,
            "project.member-list",
            project=project,
            project_id="<resolved-from-config>",
            include_pending=str(include_pending),
        )
        return

    manage_token = resolve_manage_token(allow_env=ctx.obj["allow_env_manage_token"])
    service = get_service(ctx, "member_service")
    try:
        result = service.list_members(
            manage_token=manage_token,
            alias=project,
            include_pending=include_pending,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=exit_code) from None

    formatter.output(result, _format_member_list)


@project_app.command("invitation-list")
def project_invitation_list(
    ctx: typer.Context,
    project: str = typer.Option(
        ..., "--project", "-p", help="Project alias to list pending invitations for"
    ),
) -> None:
    """List pending project invitations."""
    formatter = get_formatter(ctx)

    if should_hint(ctx):
        emit_hint(
            ctx,
            "project.invitation-list",
            project=project,
            project_id="<resolved-from-config>",
        )
        return

    manage_token = resolve_manage_token(allow_env=ctx.obj["allow_env_manage_token"])
    service = get_service(ctx, "member_service")
    try:
        result = service.list_invitations(manage_token=manage_token, alias=project)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=exit_code) from None

    formatter.output(result, _format_invitation_list)


@project_app.command("invitation-cancel")
def project_invitation_cancel(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", "-p", help="Project alias"),
    email: str = typer.Option(
        ...,
        "--email",
        "-e",
        help="Invitee's email address (used to look up the invitation if --invitation-id is omitted)",
    ),
    invitation_id: int | None = typer.Option(
        None,
        "--invitation-id",
        help="Numeric invitation ID; bypass the email lookup",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Cancel a pending invitation."""
    formatter = get_formatter(ctx)

    if should_hint(ctx):
        emit_hint(
            ctx,
            "project.invitation-cancel",
            project=project,
            project_id="<resolved-from-config>",
            email=email,
            invitation_id=str(invitation_id) if invitation_id is not None else "None",
        )
        return

    if (
        not formatter.json_mode
        and not yes
        and not typer.confirm(f"Cancel pending invitation for {email} on {project}?")
    ):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)

    manage_token = resolve_manage_token(allow_env=ctx.obj["allow_env_manage_token"])
    service = get_service(ctx, "member_service")
    try:
        result = service.cancel_invitation(
            manage_token=manage_token,
            alias=project,
            email=email,
            invitation_id=invitation_id,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=exit_code) from None

    formatter.output(
        result,
        lambda c, d: c.print(
            f"[bold green]Cancelled[/bold green] invitation_id={d.get('invitation_id')} "
            f"for {d.get('email')} on [cyan]{d.get('alias')}[/cyan]."
        ),
    )


@project_app.command("member-remove")
def project_member_remove(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", "-p", help="Project alias"),
    email: str = typer.Option(..., "--email", "-e", help="Email of the member to remove"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Remove an active member from a project (destructive)."""
    formatter = get_formatter(ctx)

    if should_hint(ctx):
        emit_hint(
            ctx,
            "project.member-remove",
            project=project,
            project_id="<resolved-from-config>",
            user_id="<resolved-from-email>",
            email=email,
        )
        return

    if (
        not formatter.json_mode
        and not yes
        and not typer.confirm(f"Remove member {email} from project {project}? This is destructive.")
    ):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)

    manage_token = resolve_manage_token(allow_env=ctx.obj["allow_env_manage_token"])
    service = get_service(ctx, "member_service")
    try:
        result = service.remove_member(
            manage_token=manage_token,
            alias=project,
            email=email,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=exit_code) from None

    formatter.output(
        result,
        lambda c, d: c.print(
            f"[bold red]Removed[/bold red] {d.get('email')} (user_id={d.get('user_id')}) "
            f"from [cyan]{d.get('alias')}[/cyan]."
        ),
    )


@project_app.command("member-set-role")
def project_member_set_role(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", "-p", help="Project alias"),
    email: str = typer.Option(..., "--email", "-e", help="Email of the member to update"),
    role: str = typer.Option(
        ...,
        "--role",
        "-r",
        click_type=click.Choice(list(PROJECT_ROLES)),
        help="New role: " + " | ".join(PROJECT_ROLES),
    ),
) -> None:
    """Change an existing member's role (PATCH)."""
    formatter = get_formatter(ctx)

    if should_hint(ctx):
        emit_hint(
            ctx,
            "project.member-set-role",
            project=project,
            project_id="<resolved-from-config>",
            user_id="<resolved-from-email>",
            email=email,
            role=role,
        )
        return

    manage_token = resolve_manage_token(allow_env=ctx.obj["allow_env_manage_token"])
    service = get_service(ctx, "member_service")
    try:
        result = service.set_member_role(
            manage_token=manage_token,
            alias=project,
            email=email,
            role=role,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except ValueError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.VALIDATION_ERROR)
        raise typer.Exit(code=2) from None
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=exit_code) from None

    formatter.output(
        result,
        lambda c, d: c.print(
            f"[bold green]Updated[/bold green] {d.get('email')} role on "
            f"[cyan]{d.get('alias')}[/cyan] -> [yellow]{d.get('role')}[/yellow]."
        ),
    )
