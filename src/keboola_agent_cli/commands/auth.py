"""Programmatic browser login -- `kbagent auth login|status|logout`.

Thin CLI layer for the `kbagent auth` command group: parses arguments, calls
:class:`AuthService`, formats output. No business logic belongs here.

Signs in a user-scoped Keboola session (PKCE authorization-code flow, or a
device-code flow for headless/remote machines) and stores it in `auth.json`.
Requires a human at a browser (or able to visit a URL and type a code) --
this is not something an AI agent can complete unattended. The resulting
session tokens are never printed or retrievable via the CLI; every result
below is built from a dataclass with no token field, so `--json` output is
safe by construction.
"""

from __future__ import annotations

from typing import Any, NoReturn

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..auth.models import DeviceAuthorization
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..services.auth_service import AuthService, AuthStatusResult, LoginResult, LogoutResult
from ._helpers import (
    check_cli_permission,
    get_formatter,
    get_service,
    map_error_to_exit_code,
)

auth_app = typer.Typer(
    help="Programmatic browser login (PKCE / device code) -- user-scoped sessions"
)

# `auth status` exits non-zero for these outcomes so scripts can branch on it
# without parsing --json (docs/programmatic-auth-login-plan.md section 4.5).
_STATUS_EXIT_3 = frozenset({"expired", "missing"})


@auth_app.callback(invoke_without_command=True)
def _auth_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "auth")


def _handle_errors(formatter: Any, exc: Exception) -> NoReturn:
    """Map a ConfigError / KeboolaApiError to a structured error + Exit."""
    if isinstance(exc, ConfigError):
        formatter.error(error_code=ErrorCode.CONFIG_ERROR, message=exc.message)
        raise typer.Exit(code=5) from None
    if isinstance(exc, KeboolaApiError):
        exit_code = map_error_to_exit_code(exc)
        formatter.error(error_code=exc.error_code, message=exc.message, retryable=exc.retryable)
        raise typer.Exit(code=exit_code) from None
    raise exc


# ── Human formatters ──────────────────────────────────────────────────


def _format_login_result(console: Console, result: LoginResult) -> None:
    """Render a completed login: session summary, accessible/registered projects."""
    lines = [
        f"[bold]Stack:[/bold] {result.stack_url}",
        f"[bold]Method:[/bold] {result.method}",
        f"[bold]Signed in as:[/bold] {result.user_name or result.user_email or '(unknown)'}",
        f"[bold]Session:[/bold] {result.session_id}",
        f"[bold]Access token expires:[/bold] {result.access_expires_at}",
    ]
    if result.fallback_reason:
        lines.append(f"[dim]Fell back to device login: {result.fallback_reason}[/dim]")
    if result.orphaned_session_id:
        lines.append(
            f"[bold yellow]Warning:[/bold yellow] previous session "
            f"{result.orphaned_session_id} could not be confirmed revoked -- "
            "`kbagent auth logout` will retry it."
        )
    console.print(Panel("\n".join(lines), title="Signed in to Keboola", expand=False))

    if result.accessible_projects:
        table = Table(title="Accessible projects")
        table.add_column("ID", justify="right")
        table.add_column("Name")
        table.add_column("Role")
        for project in result.accessible_projects:
            table.add_row(
                str(project.get("id", "")),
                str(project.get("name", "")),
                str(project.get("role", "")),
            )
        console.print(table)

    if result.registered_projects:
        table = Table(title="Registered project aliases")
        table.add_column("Alias")
        table.add_column("Project")
        table.add_column("Status")
        table.add_column("Note", style="dim")
        for registered in result.registered_projects:
            table.add_row(
                registered.alias,
                f"{registered.project_name} ({registered.project_id})",
                registered.status,
                registered.note,
            )
        console.print(table)

    for warning in result.warnings:
        console.print(f"[bold yellow]Warning:[/bold yellow] {warning}")


def _format_status_result(console: Console, result: AuthStatusResult) -> None:
    """Render the current session health for a stack."""
    status_style = {
        "live": "bold green",
        "refreshed": "bold green",
        "degraded": "bold yellow",
        "expired": "bold red",
        "missing": "bold red",
    }.get(result.status, "bold")
    lines = [
        f"[bold]Stack:[/bold] {result.stack_url}",
        f"[bold]Status:[/bold] [{status_style}]{result.status}[/{status_style}]",
    ]
    if result.session_id:
        lines.append(f"[bold]Session:[/bold] {result.session_id}")
    if result.user_name or result.user_email:
        lines.append(f"[bold]User:[/bold] {result.user_name or result.user_email}")
    if result.access_expires_at:
        lines.append(f"[bold]Access token expires:[/bold] {result.access_expires_at}")
    if result.refresh_expires_at:
        lines.append(f"[bold]Refresh token expires:[/bold] {result.refresh_expires_at}")
    if result.detail:
        lines.append(f"[dim]{result.detail}[/dim]")
    if result.orphaned_session_ids:
        lines.append(
            f"[bold yellow]Orphaned server sessions:[/bold yellow] "
            f"{', '.join(result.orphaned_session_ids)} (retried on `kbagent auth logout`)"
        )
    console.print(Panel("\n".join(lines), title="Keboola auth status", expand=False))

    if result.accessible_projects:
        table = Table(title="Accessible projects")
        table.add_column("ID", justify="right")
        table.add_column("Name")
        table.add_column("Role")
        for project in result.accessible_projects:
            table.add_row(
                str(project.get("id", "")),
                str(project.get("name", "")),
                str(project.get("role", "")),
            )
        console.print(table)


def _format_logout_result(console: Console, result: LogoutResult) -> None:
    """Render a logout outcome, surfacing an uncertain remote revoke distinctly."""
    if result.remote_revoked:
        console.print(
            f"[bold green]Signed out[/bold green] of {result.stack_url} "
            f"(session {result.session_id})."
        )
    else:
        console.print(
            f"[bold yellow]Local credentials cleared[/bold yellow] for {result.stack_url}, "
            f"but the server session {result.session_id} may still be active."
        )
    if result.detail and not result.remote_revoked:
        console.print(f"[dim]{result.detail}[/dim]")
    if result.orphans_revoked:
        console.print(
            f"[dim]Also revoked orphaned session(s): {', '.join(result.orphans_revoked)}[/dim]"
        )
    if result.orphans_remaining:
        console.print(
            f"[bold yellow]Could not confirm revocation of orphaned session(s):[/bold yellow] "
            f"{', '.join(result.orphans_remaining)}"
        )
    if result.removed_projects:
        console.print(f"[dim]Removed project alias(es): {', '.join(result.removed_projects)}[/dim]")


# ── Commands ──────────────────────────────────────────────────────────


@auth_app.command("login")
def auth_login(
    ctx: typer.Context,
    stack: str | None = typer.Option(
        None, "--stack", help="Stack URL or a registered project alias to log into"
    ),
    device_code: bool = typer.Option(
        False,
        "--device-code",
        help="Force the device-authorization flow (skip the browser loopback)",
    ),
    register_projects: bool = typer.Option(
        False,
        "--register-projects",
        help="Register every project this session can access under a local alias",
    ),
) -> None:
    """Sign in to a Keboola stack via browser login (PKCE) or device code.

    Requires a human at a browser -- an AI agent must not attempt this
    headlessly. The verification URL and code are always printed (to stderr
    in --json mode); session tokens themselves are never printed.
    """
    formatter = get_formatter(ctx)
    service: AuthService = get_service(ctx, "auth_service")
    target_console = formatter.err_console if formatter.json_mode else formatter.console

    def _on_prompt(authorization: DeviceAuthorization) -> None:
        lines = [
            "Open this URL and enter the code to finish signing in:",
            "",
            f"[bold]{authorization.verification_uri}[/bold]",
            "",
            f"Code: [bold yellow]{authorization.user_code}[/bold yellow]",
        ]
        target_console.print(
            Panel("\n".join(lines), title="Keboola CLI device login", expand=False)
        )

    def _on_notice(message: str) -> None:
        target_console.print(f"[dim]{message}[/dim]")

    try:
        result = service.login(
            stack=stack,
            device_code=device_code,
            register_projects=register_projects,
            on_device_prompt=_on_prompt,
            on_notice=_on_notice,
        )
    except (ConfigError, KeboolaApiError) as exc:
        _handle_errors(formatter, exc)
    formatter.output(result, _format_login_result)


@auth_app.command("status")
def auth_status(
    ctx: typer.Context,
    stack: str | None = typer.Option(
        None, "--stack", help="Stack URL or a registered project alias to inspect"
    ),
) -> None:
    """Show the programmatic-auth session health for a stack.

    Exits 0 for a live/refreshed/degraded (offline) session, 3 when the
    session is expired or missing.
    """
    formatter = get_formatter(ctx)
    service: AuthService = get_service(ctx, "auth_service")
    try:
        result = service.status(stack=stack)
    except (ConfigError, KeboolaApiError) as exc:
        _handle_errors(formatter, exc)
    formatter.output(result, _format_status_result)
    if result.status in _STATUS_EXIT_3:
        raise typer.Exit(code=3)


@auth_app.command("logout")
def auth_logout(
    ctx: typer.Context,
    stack: str | None = typer.Option(
        None, "--stack", help="Stack URL or a registered project alias to log out of"
    ),
    remove_projects: bool = typer.Option(
        False,
        "--remove-projects",
        help="Also remove local project aliases registered from this session",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Revoke and clear the local programmatic-auth session for a stack.

    Local credentials are always cleared, even when the remote revoke call
    fails or is uncertain -- that outcome is reported distinctly rather than
    presented as a full success.
    """
    formatter = get_formatter(ctx)
    if (
        not formatter.json_mode
        and not yes
        and not typer.confirm(
            "Log out of the current Keboola session? Local credentials will be cleared."
        )
    ):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)
    service: AuthService = get_service(ctx, "auth_service")
    try:
        result = service.logout(stack=stack, remove_projects=remove_projects)
    except (ConfigError, KeboolaApiError) as exc:
        _handle_errors(formatter, exc)
    formatter.output(result, _format_logout_result)
