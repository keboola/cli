"""Storage API token commands -- scoped-token minting, revocation, rotation.

Thin CLI layer for the ``kbagent token`` command group: parses arguments, calls
:class:`TokenService`, formats output. No business logic belongs here.

These are Storage API operations authenticated with the per-project Storage
token kbagent already stores (``X-StorageApi-Token``) -- no manage token, no
extra prompt. The acting token must carry ``canManageTokens`` (the API rejects
the mint/rotate otherwise).

``token create`` mints a scoped token and prints its secret value **once** --
kbagent never persists it. Store it immediately; it cannot be retrieved again.
"""

from __future__ import annotations

from typing import Any, NoReturn

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._helpers import (
    check_cli_permission,
    get_formatter,
    get_service,
    map_error_to_exit_code,
)

token_app = typer.Typer(help="Storage API token management (scoped mint / revoke / rotate)")


@token_app.callback(invoke_without_command=True)
def _token_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "token")


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


def _format_created_token(console: Console, data: dict[str, Any]) -> None:
    """Render a freshly minted token -- the secret is shown once."""
    lines = [
        f"[bold]Token ID:[/bold] {data.get('id', '')}",
        f"[bold]Description:[/bold] {data.get('description', '')}",
        f"[bold]Expires:[/bold] {data.get('expires') or 'never'}",
    ]
    token = data.get("token", "")
    if token:
        lines.append("")
        lines.append(f"[bold yellow]Token (shown once):[/bold yellow] {token}")
        lines.append(
            "[dim]Store it now -- kbagent does not persist it and it cannot be "
            "retrieved again. Revoke with[/dim] "
            f"[bold]kbagent token delete --project {data.get('alias', '')} "
            f"--token-id {data.get('id', '')}[/bold]"
        )
    console.print(Panel("\n".join(lines), title="Scoped token created", expand=False))


def _format_token_list(console: Console, data: dict[str, Any]) -> None:
    """Render the project's tokens as a table -- never their secret values."""
    tokens = data.get("tokens") or []
    alias = data.get("alias", "")
    if not tokens:
        console.print(
            f"No tokens visible in project [cyan]{alias}[/cyan]. "
            "Mint one with [bold]kbagent token create[/bold]."
        )
        return
    table = Table(title=f"Storage API tokens -- {alias} ({len(tokens)})")
    table.add_column("ID", style="bold cyan")
    table.add_column("Description")
    table.add_column("Created", style="dim")
    table.add_column("Expires", style="dim")
    table.add_column("Master", justify="center")
    table.add_column("Created by", style="dim")
    for token in tokens:
        expires = token.get("expires")
        if not expires:
            expires_label = "never"
        elif token.get("isExpired"):
            expires_label = f"[red]{expires} (expired)[/red]"
        else:
            expires_label = str(expires)
        creator = token.get("creatorToken") or {}
        table.add_row(
            str(token.get("id", "")),
            str(token.get("description", "")),
            str(token.get("created") or ""),
            expires_label,
            "yes" if token.get("isMasterToken") else "",
            str(creator.get("description") or ""),
        )
    console.print(table)


def _format_deleted_token(console: Console, data: dict[str, Any]) -> None:
    """Render the outcome of a token delete."""
    console.print(
        f"[bold red]Revoked[/bold red] token [bold]{data.get('token_id', '')}[/bold] "
        f"(project {data.get('alias', '')}). It no longer authenticates."
    )


def _format_refreshed_token(console: Console, data: dict[str, Any]) -> None:
    """Render a rotated token -- the new secret is shown once, old is now invalid."""
    lines = [
        f"[bold]Token ID:[/bold] {data.get('id', '')}",
        f"[bold]Expires:[/bold] {data.get('expires') or 'never'}",
    ]
    token = data.get("token", "")
    if token:
        lines.append("")
        lines.append(f"[bold yellow]New token (shown once):[/bold yellow] {token}")
        lines.append(
            "[dim]The previous token value is now invalid -- update every place that used it.[/dim]"
        )
    console.print(Panel("\n".join(lines), title="Token rotated", expand=False))


# ── Commands ──────────────────────────────────────────────────────────


@token_app.command("create")
def token_create(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", "-p", help="Project alias"),
    description: str = typer.Option(..., "--description", "-d", help="Human-readable token label"),
    bucket_write: list[str] | None = typer.Option(
        None, "--bucket-write", help="Bucket ID the token may WRITE (repeatable)"
    ),
    bucket_read: list[str] | None = typer.Option(
        None, "--bucket-read", help="Bucket ID the token may READ (repeatable)"
    ),
    component_access: list[str] | None = typer.Option(
        None, "--component-access", help="Component ID the token may run (repeatable)"
    ),
    can_read_all_file_uploads: bool = typer.Option(
        False,
        "--can-read-all-file-uploads",
        help="Allow reading files uploaded by OTHER tokens (default: only its own)",
    ),
    expires_in: int | None = typer.Option(
        None, "--expires-in", help="Lifetime in seconds (omit = never expires)"
    ),
) -> None:
    """Mint a scoped Storage API token (secret shown once).

    Grants only what you pass: bucket read/write, component access, expiry. A
    token with just --bucket-write on one bucket can upload Files and write that
    bucket, nothing else -- the Keboola single-bucket-write pattern. The acting
    project token must carry canManageTokens.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "token_service")
    try:
        result = service.create_scoped_token(
            alias=project,
            description=description,
            bucket_write=bucket_write,
            bucket_read=bucket_read,
            component_access=component_access,
            can_read_all_file_uploads=can_read_all_file_uploads,
            expires_in=expires_in,
        )
    except (ConfigError, KeboolaApiError) as exc:
        _handle_errors(formatter, exc)
    formatter.output(result, _format_created_token)


@token_app.command("list")
def token_list(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", "-p", help="Project alias"),
) -> None:
    """List the project's Storage API tokens (no secrets -- those are mint-only).

    Answers "what already exists" and hands you the token id that `token delete`
    and `token refresh` require, without a detour through the web UI. The acting
    project token must carry canManageTokens, same as `token create`.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "token_service")
    try:
        result = service.list_tokens(alias=project)
    except (ConfigError, KeboolaApiError) as exc:
        _handle_errors(formatter, exc)
    formatter.output(result, _format_token_list)


@token_app.command("delete")
def token_delete(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", "-p", help="Project alias"),
    token_id: str = typer.Option(..., "--token-id", help="ID of the token to revoke"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Revoke a Storage API token immediately (destructive; only non-master tokens)."""
    formatter = get_formatter(ctx)
    if (
        not formatter.json_mode
        and not yes
        and not typer.confirm(
            f"Revoke token '{token_id}' in project {project}? It stops authenticating at once."
        )
    ):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)
    service = get_service(ctx, "token_service")
    try:
        result = service.delete_token(alias=project, token_id=token_id)
    except (ConfigError, KeboolaApiError) as exc:
        _handle_errors(formatter, exc)
    formatter.output(result, _format_deleted_token)


@token_app.command("refresh")
def token_refresh(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", "-p", help="Project alias"),
    token_id: str = typer.Option(..., "--token-id", help="ID of the token to rotate"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Rotate a token: generate a new value and invalidate the old one (secret shown once)."""
    formatter = get_formatter(ctx)
    if (
        not formatter.json_mode
        and not yes
        and not typer.confirm(
            f"Rotate token '{token_id}' in project {project}? The current value becomes invalid."
        )
    ):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)
    service = get_service(ctx, "token_service")
    try:
        result = service.refresh_token(alias=project, token_id=token_id)
    except (ConfigError, KeboolaApiError) as exc:
        _handle_errors(formatter, exc)
    formatter.output(result, _format_refreshed_token)
