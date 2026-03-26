"""Organization management commands - bulk project onboarding.

Thin CLI layer: parses arguments, calls OrgService, formats output.
No business logic belongs here.
"""

import os
import sys

import typer
from rich.console import Console
from rich.table import Table

from ..constants import DEFAULT_TOKEN_DESCRIPTION, ENV_KBC_MANAGE_API_TOKEN, ENV_KBC_STORAGE_API_URL
from ..errors import KeboolaApiError
from ._helpers import get_formatter, get_service, map_error_to_exit_code

org_app = typer.Typer(help="Organization management")


def _resolve_manage_token() -> str:
    """Resolve the manage token from env var or interactive prompt.

    Token resolution order:
    1. KBC_MANAGE_API_TOKEN env var (for CI/CD)
    2. Interactive prompt with hidden input (if TTY)
    3. Error if neither available

    Returns:
        The manage API token.

    Raises:
        typer.Exit: If no token can be resolved.
    """
    env_token = os.environ.get(ENV_KBC_MANAGE_API_TOKEN)
    if env_token:
        return env_token

    is_tty = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
    if is_tty:
        return typer.prompt("Manage API token", hide_input=True)

    typer.echo(
        f"Error: No manage token available. Set {ENV_KBC_MANAGE_API_TOKEN} env var "
        "or run interactively.",
        err=True,
    )
    raise typer.Exit(code=2)


def _format_setup_result(console: Console, data: dict) -> None:
    """Render org setup results as Rich tables with summary."""
    org_id = data.get("organization_id", "")
    stack_url = data.get("stack_url", "")
    dry_run = data.get("dry_run", False)
    projects_found = data.get("projects_found", 0)
    added = data.get("projects_added", [])
    skipped = data.get("projects_skipped", [])
    failed = data.get("projects_failed", [])

    token_expires_in = data.get("token_expires_in")
    mode_label = "[bold yellow]DRY RUN[/bold yellow] " if dry_run else ""
    expiry_label = (
        f", token expiration: [bold]{token_expires_in}s[/bold]" if token_expires_in else ""
    )
    console.print(
        f"\n{mode_label}Organization [bold]{org_id}[/bold] on {stack_url} "
        f"-- {projects_found} project(s) found{expiry_label}\n"
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
    if skipped:
        summary_parts.append(f"[dim]{len(skipped)} skipped[/dim]")
    if failed:
        summary_parts.append(f"[bold red]{len(failed)} failed[/bold red]")

    console.print("Summary: " + ", ".join(summary_parts) if summary_parts else "No changes.")


@org_app.command("setup")
def org_setup(
    ctx: typer.Context,
    org_id: int = typer.Option(..., "--org-id", help="Organization ID"),
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
) -> None:
    """Set up all projects from a Keboola organization.

    Lists all projects in the org, creates Storage API tokens, and registers
    them in the kbagent config. Safe to re-run -- already registered projects
    are skipped.

    The manage token is read from KBC_MANAGE_API_TOKEN env var or prompted
    interactively (never passed as a CLI argument for security).
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "org_service")

    manage_token = _resolve_manage_token()

    # Interactive safety: show preview first, then confirm
    interactive = not formatter.json_mode and not yes and not dry_run
    if interactive:
        # Do a dry-run preview first
        try:
            preview = service.setup_organization(
                stack_url=url,
                manage_token=manage_token,
                org_id=org_id,
                token_description=token_description,
                dry_run=True,
                token_expires_in=token_expires_in,
            )
        except KeboolaApiError as exc:
            _handle_api_error(formatter, exc)
            return

        _format_setup_result(formatter.console, preview)

        # Count projects that would be added
        would_add = len(preview.get("projects_added", []))
        if would_add == 0:
            formatter.console.print("\nNo new projects to add.")
            return

        if not typer.confirm(f"\nProceed to add {would_add} project(s)?"):
            formatter.console.print("Aborted.")
            raise typer.Exit(code=0)

    # Execute the actual setup
    try:
        result = service.setup_organization(
            stack_url=url,
            manage_token=manage_token,
            org_id=org_id,
            token_description=token_description,
            dry_run=dry_run,
            token_expires_in=token_expires_in,
        )
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
