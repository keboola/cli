"""Programmatic browser login -- `kbagent auth login|status|logout|register-projects`.

Thin CLI layer for the `kbagent auth` command group: parses arguments, calls
:class:`AuthService`, formats output. No business logic belongs here -- alias
computation, collision resolution, and the actual `config.json` write all
live in `AuthService` / `config_store.py`. The interactive picker itself
lives in `_auth_picker.py` (terminal I/O only, same reasoning).

Signs in a user-scoped Keboola session (PKCE authorization-code flow, or a
device-code flow for headless/remote machines) and stores it in `auth.json`.
Requires a human at a browser (or able to visit a URL and type a code) --
this is not something an AI agent can complete unattended. The resulting
session tokens are never printed or retrievable via the CLI; every result
below is built from a dataclass with no token field, so `--json` output is
safe by construction.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any, NoReturn

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..auth.models import DeviceAuthorization
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..output import OutputFormatter
from ..services.auth_service import (
    AuthService,
    AuthStatusResult,
    LoginResult,
    LogoutResult,
    ProjectSelection,
    RegisteredProject,
    RegisterProjectsResult,
)
from ._auth_picker import parse_alias_overrides, run_project_picker
from ._helpers import (
    check_cli_permission,
    get_formatter,
    get_service,
    map_error_to_exit_code,
)

auth_app = typer.Typer(
    help="Programmatic browser login (PKCE / device code) -- user-scoped sessions"
)

# Printed whenever accessible projects exist but were not registered this run
# (post-login hook declined/non-interactive, or the `register-projects`
# picker came back empty) -- the single discoverable next step (defect 1 from
# the bug report this feature fixes: --register-projects was undiscoverable).
_REGISTER_HINT = "Run 'kbagent auth register-projects' to register these projects as local aliases."

# `auth status` exits non-zero for these outcomes so scripts can branch on it
# without parsing --json (docs/programmatic-auth-login-plan.md section 4.5).
_STATUS_EXIT_3 = frozenset({"expired", "missing"})


def _is_stdout_tty() -> bool:
    """True when stdout is an interactive terminal (picker eligibility check)."""
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


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


def _render_registered_projects_table(
    console: Console, registered_projects: Sequence[RegisteredProject]
) -> None:
    """Render the shared "Registered project aliases" table.

    Shared by `auth login` (post `--register-projects` or the post-login
    hook) and `auth register-projects` so the two commands cannot drift into
    two different renderings of the same `RegisteredProject` shape.
    """
    if not registered_projects:
        return
    table = Table(title="Registered project aliases")
    table.add_column("Alias")
    table.add_column("Project")
    table.add_column("Status")
    table.add_column("Note", style="dim")
    for registered in registered_projects:
        table.add_row(
            registered.alias,
            f"{registered.project_name} ({registered.project_id})",
            registered.status,
            registered.note,
        )
    console.print(table)


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

    _render_registered_projects_table(console, result.registered_projects)

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


def _format_register_projects_result(console: Console, result: RegisterProjectsResult) -> None:
    """Render `auth register-projects` output: the shared table + warnings."""
    if not result.registered_projects:
        console.print("No projects registered.")
    else:
        _render_registered_projects_table(console, result.registered_projects)
    for warning in result.warnings:
        console.print(f"[bold yellow]Warning:[/bold yellow] {warning}")


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

    When `--register-projects` is NOT passed and the session can see at
    least one project, a post-login hook offers to run the interactive
    picker right away (TTY + human mode) or otherwise prints a one-line
    hint pointing at `auth register-projects` -- see `_run_post_login_hook`.
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

    if not register_projects and result.accessible_projects:
        _run_post_login_hook(formatter, service, result)


def _run_post_login_hook(
    formatter: OutputFormatter, service: AuthService, result: LoginResult
) -> None:
    """Offer to register accessible projects right after a successful login.

    Only called when `--register-projects` was NOT passed (that flag already
    registered everything inline) and there is at least one accessible
    project. Two output-shape rules drive the branching below:

    - `--json` output must stay a SINGLE valid JSON document -- the hint (and
      any warning) is printed to `err_console`, never mixed into stdout.
    - A human at a real terminal gets asked; anyone else (piped stdout, CI,
      an AI agent subprocess) only gets the one-line hint, since the picker
      needs a real TTY to be usable at all.

    Everything below runs inside this function's own try/except, which
    swallows the failure into a warning and never changes the process exit
    code: login has ALREADY succeeded and been reported by this point, so a
    declined confirm, an aborted picker (bad piped stdin), or a transient API
    error during registration must not make a successful login look like a
    failed command.
    """
    target_console = formatter.err_console if formatter.json_mode else formatter.console
    try:
        if formatter.json_mode or not _is_stdout_tty():
            target_console.print(_REGISTER_HINT)
            return

        if not typer.confirm("Register any of these projects as local aliases now?", default=True):
            formatter.console.print(_REGISTER_HINT)
            return

        candidates = service.candidates_from_projects(result.stack_url, result.accessible_projects)
        formatter.console.print(f"\n[bold]Accessible projects on {result.stack_url}[/bold]\n")
        selections = run_project_picker(formatter.console, candidates)
        if not selections:
            formatter.console.print("No projects selected.")
            return

        register_result = service.register_projects(stack=result.stack_url, selections=selections)
        _render_registered_projects_table(formatter.console, register_result.registered_projects)
        for warning in register_result.warnings:
            formatter.console.print(f"[bold yellow]Warning:[/bold yellow] {warning}")
    except (ConfigError, KeboolaApiError, typer.Abort) as exc:
        detail = getattr(exc, "message", "") or str(exc)
        suffix = f": {detail}" if detail else "."
        target_console.print(
            f"[bold yellow]Warning:[/bold yellow] Could not register projects{suffix}"
        )


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


@auth_app.command("register-projects")
def auth_register_projects(
    ctx: typer.Context,
    stack: str | None = typer.Option(
        None, "--stack", help="Stack URL or a registered project alias"
    ),
    all_projects: bool = typer.Option(
        False,
        "--all",
        help="Register every accessible project. Mutually exclusive with --project-id.",
    ),
    project_id: list[int] | None = typer.Option(
        None,
        "--project-id",
        help="Register only this project id (repeatable). Mutually exclusive with --all.",
    ),
    alias: list[str] | None = typer.Option(
        None,
        "--alias",
        help="Alias override as ID=ALIAS (repeatable). Applies in every mode, "
        "including as the prefilled default inside the interactive picker.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the picker's final confirmation prompt"
    ),
) -> None:
    """Register accessible projects from the current session as local aliases.

    This is the fix for the undiscoverable `auth login --register-projects`
    flag and its name-only aliases: run it any time after `auth login`
    (session must still be live) to pick which projects to register and
    under which alias, including one whose default (project-name-derived)
    alias collides with something else -- the picker lets you rename it
    instead of silently skipping.

    Exactly one selection method applies:

    - `--all`: every accessible project.
    - `--project-id` (repeatable): only those ids (an id the session cannot
      access surfaces as a ConfigError from the service, not a silent skip).
    - Neither: the interactive picker, but only when stdout is a TTY and
      `--json` was not passed -- a piped/non-interactive invocation with no
      selector is a usage error, not a hang.
    """
    formatter = get_formatter(ctx)
    service: AuthService = get_service(ctx, "auth_service")

    if all_projects and project_id:
        formatter.error(
            message="--all and --project-id are mutually exclusive.",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    try:
        alias_overrides = parse_alias_overrides(alias or [])
    except ConfigError as exc:
        _handle_errors(formatter, exc)

    selections: list[ProjectSelection]
    if all_projects:
        # Needs the live candidate set (that IS the selection); --project-id
        # below deliberately skips this call -- see its comment.
        try:
            candidates_result = service.list_project_candidates(stack=stack)
        except (ConfigError, KeboolaApiError) as exc:
            _handle_errors(formatter, exc)
        selections = [
            ProjectSelection(project_id=c.project_id, alias=alias_overrides.get(c.project_id, ""))
            for c in candidates_result.candidates
        ]
    elif project_id:
        # No upfront introspection here -- `register_projects` already does
        # its own (it must, to validate + apply), so calling
        # `list_project_candidates` first would introspect twice for no
        # benefit. An id this session cannot access is NOT filtered out
        # here either: the service raises ConfigError naming the offending
        # id, which is more useful than a silent skip.
        selections = [
            ProjectSelection(project_id=pid, alias=alias_overrides.get(pid, ""))
            for pid in project_id
        ]
    else:
        if formatter.json_mode or not _is_stdout_tty():
            _handle_errors(
                formatter,
                ConfigError(
                    "No selection given and no terminal available for the interactive "
                    "picker. Pass --all or --project-id (repeatable)."
                ),
            )
        try:
            candidates_result = service.list_project_candidates(stack=stack)
        except (ConfigError, KeboolaApiError) as exc:
            _handle_errors(formatter, exc)
        formatter.console.print(
            f"\n[bold]Accessible projects on {candidates_result.stack_url}[/bold]\n"
        )
        selections = run_project_picker(
            formatter.console,
            candidates_result.candidates,
            alias_overrides=alias_overrides,
            assume_yes=yes,
        )
        if not selections:
            formatter.console.print("No projects selected.")
            raise typer.Exit(code=0)

    try:
        result = service.register_projects(stack=stack, selections=selections)
    except (ConfigError, KeboolaApiError) as exc:
        _handle_errors(formatter, exc)
    formatter.output(result, _format_register_projects_result)
