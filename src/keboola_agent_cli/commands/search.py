"""CLI commands for cross-project search.

Provides a single ``kbagent search QUERY`` command that searches item names or
configuration bodies across all (or selected) Keboola projects.

Supports two search modes:
- ``textual`` (default): fast name-based search via Storage API global-search.
- ``config-based``: slower full-body scan via ConfigService (searches JSON bodies).

Examples::

    kbagent search "customer_data"
    kbagent search "sales" --type table --project prod
    kbagent search "WHERE" --search-type config-based
    kbagent --json search "revenue" --type config --type flow
"""

from __future__ import annotations

from typing import Any

import typer
from rich.table import Table

from ..commands._helpers import (
    emit_project_warnings,
    get_formatter,
    get_service,
)
from ..errors import ConfigError, ErrorCode, KeboolaApiError

# Valid user-facing type values.
VALID_TYPES = ["table", "bucket", "config", "flow", "data-app", "transformation"]

# Valid search type values.
VALID_SEARCH_TYPES = ["textual", "config-based"]


def search_command(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Search query string."),
    project: list[str] | None = typer.Option(
        None,
        "--project",
        "-p",
        help="Project alias to search (repeatable; defaults to all projects).",
    ),
    item_type: list[str] | None = typer.Option(
        None,
        "--type",
        "-t",
        help=(
            f"Item type to restrict results. Repeatable. Valid values: {', '.join(VALID_TYPES)}."
        ),
    ),
    search_type: str = typer.Option(
        "textual",
        "--search-type",
        help=(
            "Search mode. ``textual`` (default) searches item names via the "
            "Storage API. ``config-based`` scans full configuration JSON bodies. "
            "Both modes match case-insensitively."
        ),
    ),
    limit: int = typer.Option(
        50,
        "--limit",
        "-l",
        min=1,
        max=100,
        help="Maximum number of results per project (textual search only, 1-100).",
    ),
    regex: bool = typer.Option(
        False,
        "--regex",
        "-r",
        help=(
            "Run the query as a regular expression (opt-in). Case-insensitive "
            "whole-term match against entity names only (not column names): "
            "'report' will NOT match 'monthly_report' -- use '.*report.*'. "
            "Textual search only."
        ),
    ),
    scope: list[str] | None = typer.Option(
        None,
        "--scope",
        help=(
            "Narrow a config-based hit to part of the configuration body. "
            "Dot notation written as it appears in the configuration, e.g. "
            "'parameters' or 'storage.input'. Repeatable (scopes are OR-ed). "
            "A configuration with no in-scope match drops out of the results. "
            "Config-based search only."
        ),
    ),
) -> None:
    """Search for items across one or more Keboola projects.

    In ``textual`` mode (default) the Storage API global-search endpoint is
    called, which matches item names efficiently. In ``config-based`` mode the
    full JSON body of every configuration is scanned for the query string.
    Both modes match case-insensitively; use ``kbagent config search`` when a
    case-sensitive body scan is what you want.

    Results from all queried projects are merged and printed together.
    One project failing does not stop others.

    Examples:

      kbagent search customer_data

      kbagent search sales --type table --project prod

      kbagent search "JOIN orders" --search-type config-based

      kbagent --json search revenue --type config --type flow
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "search_service")

    # Validate --type values.
    if item_type:
        for t in item_type:
            if t not in VALID_TYPES:
                formatter.error(
                    message=(f"Invalid item type '{t}'. Valid values: {', '.join(VALID_TYPES)}"),
                    error_code=ErrorCode.INVALID_ARGUMENT,
                )
                raise typer.Exit(code=2)

    # Validate --search-type.
    if search_type not in VALID_SEARCH_TYPES:
        formatter.error(
            message=(
                f"Invalid search type '{search_type}'. "
                f"Valid values: {', '.join(VALID_SEARCH_TYPES)}"
            ),
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    # Regex mode exists only on the global-search (textual) endpoint.
    if regex and search_type == "config-based":
        formatter.error(
            message="--regex is only supported with textual search (not --search-type config-based).",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    # Scopes point into a configuration body, which textual search never reads.
    if scope and search_type != "config-based":
        formatter.error(
            message="--scope is only supported with --search-type config-based.",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    try:
        result = service.search(
            query=query,
            aliases=project or None,
            item_types=item_type or None,
            search_type=search_type,
            limit=limit,
            regex=regex,
            scopes=scope or [],
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.API_ERROR)
        raise typer.Exit(code=1) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        _format_search_results(formatter.console, result, query, search_type, regex)
        emit_project_warnings(formatter, result)


# ── Human-readable output ──────────────────────────────────────────────────


def _format_search_results(
    console: Any, result: dict, query: str, search_type: str, regex: bool = False
) -> None:
    """Render search results as a Rich table with stats header."""
    stats = result.get("stats", {})
    projects_searched = stats.get("projects_searched", 0)
    results_found = stats.get("results_found", 0)
    errors = result.get("errors", [])

    mode_label = "config-based" if search_type == "config-based" else "textual"
    if regex:
        mode_label += ", regex"
    console.print(
        f'[bold]Search results[/bold] for [yellow]"{query}"[/yellow] '
        f"([dim]{mode_label}[/dim]) — "
        f"[cyan]{results_found}[/cyan] result(s) across "
        f"[cyan]{projects_searched}[/cyan] project(s)"
        + (f", [yellow]{len(errors)} error(s)[/yellow]" if errors else "")
    )

    rows = result.get("results", [])
    if not rows:
        console.print("[dim]No results found.[/dim]")
        return

    # Hide the column unless something actually matched via a column name.
    show_matched = any(row.get("matched_columns") for row in rows)

    table = Table(show_header=True, header_style="bold blue", show_lines=False)
    table.add_column("Project", style="cyan", no_wrap=True)
    table.add_column("Type", style="magenta", no_wrap=True)
    table.add_column("ID", style="green", overflow="fold")
    table.add_column("Name", overflow="fold")
    table.add_column("Component ID", style="dim", overflow="fold")
    if show_matched:
        table.add_column("Matched columns", style="yellow", overflow="fold")

    for row in rows:
        cells = [
            row.get("project_alias", ""),
            row.get("type", ""),
            row.get("id", ""),
            row.get("name", ""),
            row.get("component_id") or "",
        ]
        if show_matched:
            cells.append(", ".join(row.get("matched_columns") or []))
        table.add_row(*cells)

    console.print(table)
