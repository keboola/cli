"""Data Streams commands -- OTLP/HTTP source provisioning and introspection.

Thin CLI layer for the ``kbagent stream`` command group: parses arguments,
calls :class:`StreamService`, formats output. No business logic belongs here.

Authentication uses the per-project Storage API token that kbagent already
stores (``X-StorageApi-Token``) -- no manage token, no extra prompt.

The OTLP endpoint embeds its secret in the URL path; ``stream detail`` /
``stream create-source`` mask it by default and only print it with ``--reveal``.
"""

from __future__ import annotations

from enum import StrEnum
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

stream_app = typer.Typer(help="Data Streams (OTLP) source management")


class SourceType(StrEnum):
    """Stream source types supported by the Stream API."""

    otlp = "otlp"
    http = "http"


@stream_app.callback(invoke_without_command=True)
def _stream_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "stream")


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


def _format_sources_table(console: Console, data: dict[str, Any]) -> None:
    """Render the sources list as a Rich table."""
    sources = data.get("sources") or []
    alias = data.get("alias", "")
    branch = data.get("branch_id", "")
    if not sources:
        console.print(
            f"No Data Streams sources in project [cyan]{alias}[/cyan] (branch {branch}). "
            "Create one with [bold]kbagent stream create-source[/bold]."
        )
        return
    table = Table(title=f"Data Streams sources -- {alias} (branch {branch})")
    table.add_column("Source ID", style="bold cyan")
    table.add_column("Name")
    table.add_column("Type", style="dim")
    table.add_column("Base Endpoint", style="dim", max_width=60)
    for src in sources:
        table.add_row(
            src.get("source_id", ""),
            src.get("name", ""),
            src.get("type", ""),
            src.get("base_endpoint", ""),
        )
    console.print(table)


def _format_detail(console: Console, data: dict[str, Any]) -> None:
    """Render one source's assembled detail as a Rich panel."""
    status = data.get("status")
    title = f"Stream source: {data.get('source_id', '')}"
    if status in ("created", "skipped"):
        verb = "Created" if status == "created" else "Already exists"
        title = f"{verb} -- {data.get('source_id', '')}"

    lines = [
        f"[bold]Name:[/bold] {data.get('name', '')}",
        f"[bold]Type:[/bold] {data.get('type', '')}",
        f"[bold]Branch:[/bold] {data.get('branch_id', '')}",
    ]
    if data.get("protocol"):
        lines.append(f"[bold]Protocol:[/bold] {data['protocol']}")
    lines.append(f"[bold]Endpoint:[/bold] {data.get('endpoint', '')}")
    if not data.get("secret_revealed") and data.get("endpoint"):
        lines.append("  [dim](secret masked -- pass --reveal to print the full URL)[/dim]")

    signals = data.get("signal_endpoints") or {}
    if signals:
        lines.append("\n[bold]Per-signal endpoints:[/bold]")
        for signal, url in signals.items():
            lines.append(f"  [cyan]{signal}[/cyan]: {url}")

    destination = data.get("destination") or {}
    tables = destination.get("tables") or {}
    if destination.get("bucket") or tables:
        lines.append("\n[bold]Destination:[/bold]")
        if destination.get("bucket"):
            lines.append(f"  bucket: {destination['bucket']}")
        for signal, table_id in tables.items():
            lines.append(f"  [cyan]{signal}[/cyan] -> {table_id}")

    conditions = data.get("import_conditions")
    if conditions:
        lines.append("\n[bold]Import conditions:[/bold]")
        for key, value in conditions.items():
            lines.append(f"  {key}: {value}")

    console.print(Panel("\n".join(lines), title=title, expand=False))


def _format_delete_result(console: Console, data: dict[str, Any]) -> None:
    """Render the outcome of a delete operation."""
    status = data.get("status", "")
    source_id = data.get("source_id", "")
    if status == "dry_run":
        console.print(
            f"[bold yellow]DRY RUN[/bold yellow] would delete source "
            f"[bold]{source_id}[/bold] (branch {data.get('branch_id', '')})."
        )
    elif status == "deleted":
        console.print(f"[bold red]Deleted[/bold red] source [bold]{source_id}[/bold].")


# ── Commands ──────────────────────────────────────────────────────────


@stream_app.command("list")
def stream_list(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", "-p", help="Project alias"),
    branch: str | None = typer.Option(
        None, "--branch", help="Branch ref (default: the project's default branch)"
    ),
) -> None:
    """List Data Streams sources in a project."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "stream_service")
    try:
        result = service.list_sources(alias=project, branch_id=branch)
    except (ConfigError, KeboolaApiError) as exc:
        _handle_errors(formatter, exc)
    formatter.output(result, _format_sources_table)


@stream_app.command("create-source")
def stream_create_source(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", "-p", help="Project alias"),
    name: str = typer.Option(..., "--name", "-n", help="Human-readable source name"),
    source_type: SourceType = typer.Option(
        SourceType.otlp, "--type", help="Source type (otlp | http)"
    ),
    branch: str | None = typer.Option(
        None, "--branch", help="Branch ref (default branch if unset)"
    ),
    if_not_exists: bool = typer.Option(
        False, "--if-not-exists", help="Return the existing source instead of failing if it exists"
    ),
    no_sinks: bool = typer.Option(
        False,
        "--no-sinks",
        help="Skip auto-creating the logs/metrics/traces sinks for an OTLP source",
    ),
    reveal: bool = typer.Option(False, "--reveal", help="Print the full endpoint incl. secret"),
) -> None:
    """Create an OTLP (or HTTP) source and return its endpoint.

    For an OTLP source the three standard sinks (logs/metrics/traces) are
    auto-created so data lands in Storage (bucket in.c-otlp-<source>); pass
    --no-sinks to create a bare source without them.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "stream_service")
    try:
        result = service.create_source(
            alias=project,
            name=name,
            source_type=source_type.value,
            branch_id=branch,
            if_not_exists=if_not_exists,
            reveal=reveal,
            provision_sinks=not no_sinks,
        )
    except (ConfigError, KeboolaApiError) as exc:
        _handle_errors(formatter, exc)
    formatter.output(result, _format_detail)


@stream_app.command("detail")
def stream_detail(
    ctx: typer.Context,
    source_id: str | None = typer.Argument(None, help="Source id (or use --name)"),
    project: str = typer.Option(..., "--project", "-p", help="Project alias"),
    name: str | None = typer.Option(None, "--name", "-n", help="Look up the source by name"),
    branch: str | None = typer.Option(
        None, "--branch", help="Branch ref (default branch if unset)"
    ),
    reveal: bool = typer.Option(False, "--reveal", help="Print the full endpoint incl. secret"),
) -> None:
    """Show a source's endpoints, protocol, and destination tables."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "stream_service")
    try:
        result = service.get_source_detail(
            alias=project,
            source_id=source_id,
            name=name,
            branch_id=branch,
            reveal=reveal,
        )
    except (ConfigError, KeboolaApiError) as exc:
        _handle_errors(formatter, exc)
    formatter.output(result, _format_detail)


@stream_app.command("delete")
def stream_delete(
    ctx: typer.Context,
    source_id: str = typer.Argument(..., help="Source id to delete"),
    project: str = typer.Option(..., "--project", "-p", help="Project alias"),
    branch: str | None = typer.Option(
        None, "--branch", help="Branch ref (default branch if unset)"
    ),
    force: bool = typer.Option(False, "--force", help="Alias for --yes (skip confirmation)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without deleting"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Delete a Data Streams source (destructive)."""
    formatter = get_formatter(ctx)
    if (
        not dry_run
        and not formatter.json_mode
        and not (yes or force)
        and not typer.confirm(
            f"Delete source '{source_id}' from project {project}? This is destructive."
        )
    ):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)
    service = get_service(ctx, "stream_service")
    try:
        result = service.delete_source(
            alias=project, source_id=source_id, branch_id=branch, dry_run=dry_run
        )
    except (ConfigError, KeboolaApiError) as exc:
        _handle_errors(formatter, exc)
    formatter.output(result, _format_delete_result)
