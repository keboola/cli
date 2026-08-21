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

from collections.abc import Callable
from functools import partial
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


def _expires_cell(token: dict[str, Any]) -> str:
    expires = token.get("expires")
    if not expires:
        return "never"
    if token.get("isExpired"):
        return f"[red]{expires} (expired)[/red]"
    return str(expires)


def _last_used_cell(token: dict[str, Any]) -> str:
    """Render the derived recency, keeping its three states visually apart.

    "never used" and "unknown" both have an empty ``lastUsed``, but they mean
    opposite things for a revocation decision -- one is proof, the other is the
    absence of it -- so they must never render as the same blank cell.
    """
    status = token.get("lastUsedStatus")
    if status == "used":
        return str(token.get("lastUsed") or "")
    if status == "never":
        return "[yellow]never used[/yellow]"
    if status == "error":
        return "[red]lookup failed[/red]"
    return "[dim]unknown (older than event retention)[/dim]"


# Column name -> (header, cell renderer, Rich column kwargs). The name is what
# `--columns` accepts; keep it lowercase and stable, it is a user-facing token.
_COLUMNS: dict[str, tuple[str, Callable[[dict[str, Any]], str], dict[str, Any]]] = {
    "id": ("ID", lambda t: str(t.get("id", "")), {"style": "bold cyan"}),
    "description": ("Description", lambda t: str(t.get("description", "")), {}),
    "created": ("Created", lambda t: str(t.get("created") or ""), {"style": "dim"}),
    "refreshed": ("Refreshed", lambda t: str(t.get("refreshed") or ""), {"style": "dim"}),
    "expires": ("Expires", _expires_cell, {"style": "dim"}),
    "master": ("Master", lambda t: "yes" if t.get("isMasterToken") else "", {"justify": "center"}),
    "created_by": (
        "Created by",
        lambda t: str((t.get("creatorToken") or {}).get("description") or ""),
        {"style": "dim"},
    ),
    "last_used": ("Last used", _last_used_cell, {}),
    "last_used_event": (
        "Last event",
        lambda t: str(t.get("lastUsedEvent") or ""),
        {"style": "dim"},
    ),
}

_DEFAULT_COLUMNS: tuple[str, ...] = (
    "id",
    "description",
    "created",
    "refreshed",
    "expires",
    "master",
    "created_by",
)


def _resolve_columns(selected: list[str] | None, with_last_used: bool) -> list[str]:
    """Pick the table's columns: explicit selection, else the default set.

    ``--with-last-used`` appends the derived columns to the DEFAULT set only.
    An explicit ``--columns`` is taken literally -- someone who names their
    columns means those columns, and silently appending to their choice would
    make the flag unpredictable.
    """
    if selected:
        return list(selected)
    if with_last_used:
        return [*_DEFAULT_COLUMNS, "last_used", "last_used_event"]
    return list(_DEFAULT_COLUMNS)


def _format_token_list(console: Console, data: dict[str, Any], columns: list[str]) -> None:
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
    for name in columns:
        header, _renderer, column_kwargs = _COLUMNS[name]
        table.add_column(header, **column_kwargs)
    for token in tokens:
        table.add_row(*(_COLUMNS[name][1](token) for name in columns))
    console.print(table)
    for error in data.get("errors") or []:
        console.print(
            f"[red]Last-used lookup failed[/red] for token "
            f"[bold]{error.get('token_id', '')}[/bold]: {error.get('message', '')}"
        )


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
    with_last_used: bool = typer.Option(
        False,
        "--with-last-used",
        help="Derive each token's last activity (one extra API call PER TOKEN) and sort dormant-first",
    ),
    columns: list[str] | None = typer.Option(
        None,
        "--columns",
        help=(
            "Table columns to show, in order (repeat for multiple). "
            f"Available: {', '.join(_COLUMNS)}. Human output only -- --json is unaffected."
        ),
    ),
) -> None:
    """List the project's Storage API tokens (no secrets -- those are mint-only).

    Answers "what already exists" and hands you the token id that `token delete`
    and `token refresh` require, without a detour through the web UI. The acting
    project token must carry canManageTokens, same as `token create`.

    --with-last-used answers the follow-up question -- which of them are still
    in use -- by deriving each token's most recent activity from its event
    feed, and orders the table so reading order is cleanup order. It is opt-in
    because it costs one extra request per token. Two caveats it cannot work
    around: activity inside a development branch is invisible to that feed, and
    events are only retained ~6 months, so a token older than that with no
    activity reports "unknown" rather than claiming it was never used.
    """
    formatter = get_formatter(ctx)
    unknown = [name for name in columns or [] if name not in _COLUMNS]
    if unknown:
        formatter.error(
            message=(
                f"Unknown --columns value(s): {', '.join(unknown)}. "
                f"Available: {', '.join(_COLUMNS)}"
            ),
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)
    service = get_service(ctx, "token_service")
    try:
        result = service.list_tokens(alias=project, with_last_used=with_last_used)
    except (ConfigError, KeboolaApiError) as exc:
        _handle_errors(formatter, exc)
    selected = _resolve_columns(columns, with_last_used)
    formatter.output(result, partial(_format_token_list, columns=selected))


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
