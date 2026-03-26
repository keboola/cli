"""Storage commands - buckets, tables, and direct access path resolution.

Provides direct Storage API access including sharing/linked bucket metadata
that is not available via MCP tools.
"""

import typer

from ..errors import ConfigError, KeboolaApiError
from ._helpers import emit_project_warnings, get_formatter, get_service, map_error_to_exit_code

storage_app = typer.Typer(help="Browse storage buckets and tables")


@storage_app.command("buckets")
def storage_buckets(
    ctx: typer.Context,
    project: list[str] | None = typer.Option(
        None,
        "--project",
        help="Project alias (can be repeated for multiple projects)",
    ),
) -> None:
    """List storage buckets with sharing/linked bucket information.

    Shows which buckets are linked from other projects, including the
    source project ID and name. This information is not available via
    MCP tools.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")

    try:
        result = service.list_buckets(aliases=project)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        from rich.table import Table

        buckets = result["buckets"]
        if not buckets:
            formatter.console.print("[dim]No buckets found.[/dim]")
            return

        # Group by project
        by_project: dict[str, list[dict]] = {}
        for b in buckets:
            alias = b["project_alias"]
            by_project.setdefault(alias, []).append(b)

        for alias, proj_buckets in by_project.items():
            table = Table(title=f"Buckets - {alias}")
            table.add_column("Bucket ID", style="bold cyan")
            table.add_column("Stage", style="dim")
            table.add_column("Rows", justify="right")
            table.add_column("Linked From", style="yellow")

            for b in proj_buckets:
                linked = ""
                if b["is_linked"]:
                    linked = f"{b['source_project_name']} (#{b['source_project_id']})"
                table.add_row(
                    b["id"],
                    b["stage"],
                    str(b["rows_count"]),
                    linked,
                )

            formatter.console.print(table)
            formatter.console.print()

        emit_project_warnings(formatter, result)


@storage_app.command("bucket-detail")
def storage_bucket_detail(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    bucket_id: str = typer.Option(
        ...,
        "--bucket-id",
        help="Bucket ID (e.g. in.c-db)",
    ),
) -> None:
    """Show detailed bucket info including Snowflake direct access paths.

    For linked/shared buckets, resolves the correct Snowflake database
    and schema from the source project. Each table includes a ready-to-use
    fully-qualified Snowflake path with proper quoting.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")

    try:
        result = service.get_bucket_detail(alias=project, bucket_id=bucket_id)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        formatter.console.print(f"[bold]Bucket:[/bold] {result['bucket_id']}")
        formatter.console.print(f"  Display name: {result['display_name']}")
        formatter.console.print(f"  Backend: {result['backend']}")

        if result["is_linked"]:
            formatter.console.print(
                f"  [yellow]Linked from:[/yellow] "
                f"{result['source_project_name']} (#{result['source_project_id']})"
            )
            formatter.console.print(f"  Source bucket: {result['source_bucket_id']}")

        formatter.console.print(f"  Snowflake DB: {result['snowflake_database']}")
        formatter.console.print(f"  Snowflake schema: {result['snowflake_schema']}")
        formatter.console.print(f"  Tables: {result['table_count']}")

        if result["tables"]:
            formatter.console.print()
            from rich.table import Table

            table = Table(title="Tables with Snowflake paths")
            table.add_column("Table", style="bold")
            table.add_column("Snowflake Path", style="green")
            table.add_column("Alias", style="dim")

            for t in result["tables"][:50]:  # limit display
                table.add_row(
                    t["name"],
                    t["snowflake_path"],
                    "yes" if t["is_alias"] else "",
                )

            formatter.console.print(table)

            if len(result["tables"]) > 50:
                formatter.console.print(
                    f"  ... and {len(result['tables']) - 50} more (use --json for full list)"
                )


@storage_app.command("tables")
def storage_tables(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    bucket_id: str | None = typer.Option(
        None,
        "--bucket-id",
        help="Filter tables by bucket ID",
    ),
) -> None:
    """List storage tables from a project."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")

    try:
        result = service.list_tables(alias=project, bucket_id=bucket_id)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        from rich.table import Table

        tables = result["tables"]
        if not tables:
            formatter.console.print("[dim]No tables found.[/dim]")
            return

        table = Table(title=f"Tables - {result['project_alias']}")
        table.add_column("Table ID", style="bold cyan")
        table.add_column("Rows", justify="right")
        table.add_column("Size", justify="right", style="dim")
        table.add_column("Last Import", style="dim")

        for t in tables:
            size_mb = t["data_size_bytes"] / (1024 * 1024) if t["data_size_bytes"] else 0
            last_import = t.get("last_import_date", "")
            if last_import and "T" in last_import:
                last_import = last_import.split("T")[0]
            table.add_row(
                t["id"],
                str(t["rows_count"]),
                f"{size_mb:.1f} MB",
                last_import,
            )

        formatter.console.print(table)
