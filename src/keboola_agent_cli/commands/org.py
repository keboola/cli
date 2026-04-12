"""Organization management commands - bulk project onboarding.

Thin CLI layer: parses arguments, calls OrgService, formats output.
No business logic belongs here.
"""

import typer
from rich.console import Console
from rich.table import Table

from ..constants import DEFAULT_TOKEN_DESCRIPTION, ENV_KBC_STORAGE_API_URL
from ..errors import KeboolaApiError
from ._helpers import (
    check_cli_permission,
    emit_hint,
    get_formatter,
    get_service,
    map_error_to_exit_code,
    resolve_manage_token,
    should_hint,
)

org_app = typer.Typer(help="Organization management")


@org_app.callback(invoke_without_command=True)
def _org_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "org")


def _parse_project_ids(value: str | None) -> list[int] | None:
    """Parse comma-separated project IDs string into a list of ints."""
    if not value:
        return None
    try:
        return [int(pid.strip()) for pid in value.split(",") if pid.strip()]
    except ValueError as exc:
        msg = f"Invalid project ID (must be integers): {exc}"
        raise typer.BadParameter(msg) from exc


def _format_setup_result(console: Console, data: dict) -> None:
    """Render org setup results as Rich tables with summary."""
    org_id = data.get("organization_id")
    stack_url = data.get("stack_url", "")
    dry_run = data.get("dry_run", False)
    projects_found = data.get("projects_found", 0)
    added = data.get("projects_added", [])
    refreshed = data.get("projects_refreshed", [])
    skipped = data.get("projects_skipped", [])
    failed = data.get("projects_failed", [])

    token_expires_in = data.get("token_expires_in")
    mode_label = "[bold yellow]DRY RUN[/bold yellow] " if dry_run else ""
    expiry_label = (
        f", token expiration: [bold]{token_expires_in}s[/bold]" if token_expires_in else ""
    )
    org_label = f"Organization [bold]{org_id}[/bold] on " if org_id else ""
    console.print(
        f"\n{mode_label}{org_label}{stack_url} -- {projects_found} project(s) found{expiry_label}\n"
    )

    # Added / would-add table
    if added:
        action_label = "Projects to Add" if dry_run else "Projects Added"
        table = Table(title=action_label)
        table.add_column("Alias", style="bold cyan")
        table.add_column("Project ID", justify="right")
        table.add_column("Project Name")
        if not dry_run:
            table.add_column("Token", style="dim")

        for p in added:
            if dry_run:
                table.add_row(p["alias"], str(p["project_id"]), p["project_name"])
            else:
                table.add_row(
                    p["alias"], str(p["project_id"]), p["project_name"], p.get("token", "")
                )

        console.print(table)
        console.print()

    # Refreshed tokens table
    if refreshed:
        action_label = "Tokens to Refresh" if dry_run else "Tokens Refreshed"
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

    # Skipped table
    if skipped:
        table = Table(title="Projects Skipped")
        table.add_column("Project ID", justify="right")
        table.add_column("Project Name")
        table.add_column("Reason", style="dim")

        for p in skipped:
            table.add_row(str(p["project_id"]), p["project_name"], p["reason"])

        console.print(table)
        console.print()

    # Failed table
    if failed:
        table = Table(title="Projects Failed")
        table.add_column("Project ID", justify="right")
        table.add_column("Project Name")
        table.add_column("Error", style="bold red")

        for p in failed:
            table.add_row(str(p["project_id"]), p["project_name"], p["error"])

        console.print(table)
        console.print()

    # Summary line
    summary_parts = []
    if added:
        verb = "to add" if dry_run else "added"
        summary_parts.append(f"[bold green]{len(added)}[/bold green] {verb}")
    if refreshed:
        verb = "to refresh" if dry_run else "refreshed"
        summary_parts.append(f"[bold cyan]{len(refreshed)}[/bold cyan] {verb}")
    if skipped:
        summary_parts.append(f"[dim]{len(skipped)} skipped[/dim]")
    if failed:
        summary_parts.append(f"[bold red]{len(failed)} failed[/bold red]")

    console.print("Summary: " + ", ".join(summary_parts) if summary_parts else "No changes.")


@org_app.command("setup")
def org_setup(
    ctx: typer.Context,
    org_id: int | None = typer.Option(
        None,
        "--org-id",
        help="Organization ID (requires org-admin manage token)",
    ),
    project_ids_raw: str | None = typer.Option(
        None,
        "--project-ids",
        help="Comma-separated project IDs (works with Personal Access Token)",
    ),
    url: str = typer.Option(
        ...,
        "--url",
        envvar=ENV_KBC_STORAGE_API_URL,
        help="Keboola stack URL (e.g. https://connection.keboola.com)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview what would happen without making changes",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt",
    ),
    token_description: str = typer.Option(
        DEFAULT_TOKEN_DESCRIPTION,
        "--token-description",
        help="Description prefix for created Storage API tokens",
    ),
    token_expires_in: int | None = typer.Option(
        None,
        "--token-expires-in",
        min=1,
        help="Token lifetime in seconds (e.g. 3600 for 1 hour). If not set, tokens never expire.",
    ),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help="Refresh tokens for already-registered projects with invalid tokens",
    ),
) -> None:
    """Set up projects and register them in the kbagent config.

    Two modes:

    \b
    1. Org admin:    --org-id 123          (lists all projects in the org)
    2. Project member: --project-ids 1,2,3 (fetches specific projects)

    Creates Storage API tokens and registers projects. Safe to re-run --
    already registered projects are skipped.

    The token is read from KBC_MANAGE_API_TOKEN env var or prompted
    interactively (never passed as a CLI argument for security).
    """
    if should_hint(ctx):
        emit_hint(ctx, "org.setup", org_id=org_id, url=url, dry_run=dry_run)
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "org_service")

    # Validate: need at least one of --org-id or --project-ids
    project_ids = _parse_project_ids(project_ids_raw)
    if not org_id and not project_ids:
        formatter.error(
            message="Provide --org-id (org admin) or --project-ids (project member)",
            error_code="usage_error",
        )
        raise typer.Exit(code=2)

    manage_token = resolve_manage_token()

    # Build kwargs shared by preview and real call
    setup_kwargs: dict = {
        "stack_url": url,
        "manage_token": manage_token,
        "org_id": org_id,
        "token_description": token_description,
        "token_expires_in": token_expires_in,
        "project_ids": project_ids,
    }

    # Interactive safety: show preview first, then confirm
    interactive = not formatter.json_mode and not yes and not dry_run
    if interactive:
        try:
            preview = service.setup_organization(**setup_kwargs, dry_run=True)
        except KeboolaApiError as exc:
            _handle_api_error(formatter, exc)
            return

        _format_setup_result(formatter.console, preview)

        would_add = len(preview.get("projects_added", []))
        would_skip = len(preview.get("projects_skipped", []))
        if would_add == 0 and not (refresh and would_skip > 0):
            formatter.console.print("\nNo new projects to add.")
            return

        if would_add > 0 and not typer.confirm(f"\nProceed to add {would_add} project(s)?"):
            formatter.console.print("Aborted.")
            raise typer.Exit(code=0)

    # Execute the actual setup
    try:
        result = service.setup_organization(**setup_kwargs, dry_run=dry_run)
    except KeboolaApiError as exc:
        _handle_api_error(formatter, exc)
        return

    # Refresh tokens for skipped (already-registered) projects if requested
    if refresh and result.get("projects_skipped"):
        config = ctx.obj["config_store"].load()
        skipped_ids = {p["project_id"] for p in result["projects_skipped"]}
        skipped_aliases = [
            alias for alias, proj in config.projects.items() if proj.project_id in skipped_ids
        ]
        if skipped_aliases:
            try:
                refresh_result = service.refresh_tokens(
                    manage_token=manage_token,
                    aliases=skipped_aliases,
                    token_description=token_description,
                    token_expires_in=token_expires_in,
                    dry_run=dry_run,
                )
                result["projects_refreshed"] = refresh_result.get("projects_refreshed", [])
            except KeboolaApiError as exc:
                _handle_api_error(formatter, exc)
                return

    formatter.output(result, _format_setup_result)


def _handle_api_error(formatter, exc: KeboolaApiError) -> None:
    """Handle a KeboolaApiError by outputting it and raising Exit."""
    exit_code = map_error_to_exit_code(exc)
    formatter.error(
        message=exc.message,
        error_code=exc.error_code,
        retryable=exc.retryable,
    )
    raise typer.Exit(code=exit_code)
