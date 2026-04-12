"""Storage commands - buckets, tables, and direct access path resolution.

Provides direct Storage API access including sharing/linked bucket metadata
that is not available via MCP tools.
"""

from pathlib import Path

import typer

from ..config_store import ConfigStore
from ..errors import ConfigError, KeboolaApiError
from ._helpers import (
    check_cli_permission,
    emit_project_warnings,
    get_formatter,
    get_service,
    map_error_to_exit_code,
    resolve_branch,
)

storage_app = typer.Typer(help="Browse and manage storage buckets, tables, and files")

# Rich help panel names for grouping in --help output
_BUCKETS = "Buckets"
_TABLES = "Tables"
_FILES = "Files"


@storage_app.callback(invoke_without_command=True)
def _storage_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "storage")


@storage_app.command("buckets", rich_help_panel=_BUCKETS)
def storage_buckets(
    ctx: typer.Context,
    project: list[str] | None = typer.Option(
        None,
        "--project",
        help="Project alias (can be repeated for multiple projects)",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID (defaults to active branch if set via 'branch use')",
    ),
) -> None:
    """List storage buckets with sharing/linked bucket information.

    Shows which buckets are linked from other projects, including the
    source project ID and name. This information is not available via
    MCP tools.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]

    # --branch requires exactly one --project
    if branch is not None and (not project or len(project) != 1):
        formatter.error(
            message="--branch requires exactly one --project (branch ID is per-project)",
            error_code="INVALID_ARGUMENT",
        )
        raise typer.Exit(code=2)

    # Resolve active branch for single-project queries
    effective_branch: int | None = branch
    if branch is None and project and len(project) == 1:
        _, effective_branch = resolve_branch(config_store, formatter, project[0], None)

    try:
        result = service.list_buckets(aliases=project, branch_id=effective_branch)
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


@storage_app.command("bucket-detail", rich_help_panel=_BUCKETS)
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
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID (defaults to active branch if set via 'branch use')",
    ),
) -> None:
    """Show detailed bucket info including Snowflake direct access paths.

    For linked/shared buckets, resolves the correct Snowflake database
    and schema from the source project. Each table includes a ready-to-use
    fully-qualified Snowflake path with proper quoting.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    try:
        result = service.get_bucket_detail(
            alias=project,
            bucket_id=bucket_id,
            branch_id=effective_branch,
        )
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


@storage_app.command("tables", rich_help_panel=_TABLES)
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
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID (defaults to active branch if set via 'branch use')",
    ),
) -> None:
    """List storage tables from a project."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    try:
        result = service.list_tables(
            alias=project,
            bucket_id=bucket_id,
            branch_id=effective_branch,
        )
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


@storage_app.command("table-detail", rich_help_panel=_TABLES)
def storage_table_detail(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    table_id: str = typer.Option(
        ...,
        "--table-id",
        help="Table ID (e.g. 'in.c-my-bucket.my-table')",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID (defaults to active branch if set via 'branch use')",
    ),
) -> None:
    """Show detailed table info including columns and types."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    try:
        result = service.get_table_detail(
            alias=project,
            table_id=table_id,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        formatter.console.print(f"[bold]Table:[/bold] {result['table_id']}")
        formatter.console.print(f"  Name: {result['display_name'] or result['name']}")
        formatter.console.print(f"  Bucket: {result['bucket_id']}")
        formatter.console.print(f"  Rows: {result['rows_count']:,}")
        size_mb = result["data_size_bytes"] / (1024 * 1024)
        formatter.console.print(f"  Size: {size_mb:.2f} MB")
        if result["primary_key"]:
            formatter.console.print(f"  Primary key: {', '.join(result['primary_key'])}")
        if result["last_import_date"]:
            formatter.console.print(f"  Last import: {result['last_import_date']}")

        if result["column_details"]:
            formatter.console.print()
            from rich.table import Table

            table = Table(title="Columns")
            table.add_column("Name", style="bold cyan")
            table.add_column("Type", style="dim")
            table.add_column("Nullable", style="dim")

            for col in result["column_details"]:
                table.add_row(
                    col["name"],
                    col.get("type", ""),
                    "yes" if col.get("nullable") else "",
                )

            formatter.console.print(table)


@storage_app.command("create-bucket", rich_help_panel=_BUCKETS)
def storage_create_bucket(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    stage: str = typer.Option(
        ...,
        "--stage",
        help="Bucket stage: 'in' or 'out'",
    ),
    name: str = typer.Option(
        ...,
        "--name",
        help="Bucket name slug (e.g. 'my-bucket')",
    ),
    description: str | None = typer.Option(
        None,
        "--description",
        help="Optional bucket description",
    ),
    backend: str | None = typer.Option(
        None,
        "--backend",
        help="Optional backend type (e.g. 'snowflake', 'bigquery')",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID (defaults to active branch if set via 'branch use')",
    ),
) -> None:
    """Create a new storage bucket."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    try:
        result = service.create_bucket(
            alias=project,
            stage=stage,
            name=name,
            description=description,
            backend=backend,
            branch_id=effective_branch,
        )
    except ValueError as exc:
        formatter.error(message=str(exc), error_code="INVALID_ARGUMENT")
        raise typer.Exit(code=2) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        formatter.console.print(f"[bold green]Created bucket:[/bold green] {result['id']}")
        formatter.console.print(f"  Stage: {result['stage']}")
        formatter.console.print(f"  Backend: {result['backend']}")
        if result["description"]:
            formatter.console.print(f"  Description: {result['description']}")


@storage_app.command("create-table", rich_help_panel=_TABLES)
def storage_create_table(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    bucket_id: str = typer.Option(
        ...,
        "--bucket-id",
        help="Target bucket ID (e.g. 'in.c-my-bucket')",
    ),
    name: str = typer.Option(
        ...,
        "--name",
        help="Table name",
    ),
    column: list[str] = typer.Option(
        ...,
        "--column",
        help="Column definition as 'name:TYPE' (e.g. 'id:INTEGER'). Can be repeated. Types: STRING, INTEGER, NUMERIC, FLOAT, BOOLEAN, DATE, TIMESTAMP",
    ),
    primary_key: list[str] | None = typer.Option(
        None,
        "--primary-key",
        help="Primary key column name. Can be repeated.",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID (defaults to active branch if set via 'branch use')",
    ),
) -> None:
    """Create a new storage table with typed columns.

    Column types: STRING, INTEGER, NUMERIC, FLOAT, BOOLEAN, DATE, TIMESTAMP.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    try:
        result = service.create_table(
            alias=project,
            bucket_id=bucket_id,
            name=name,
            columns=column,
            primary_key=primary_key,
            branch_id=effective_branch,
        )
    except ValueError as exc:
        formatter.error(message=str(exc), error_code="INVALID_ARGUMENT")
        raise typer.Exit(code=2) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        formatter.console.print(f"[bold green]Created table:[/bold green] {result['table_id']}")
        if result["primary_key"]:
            formatter.console.print(f"  Primary key: {', '.join(result['primary_key'])}")
        formatter.console.print(f"  Columns: {', '.join(result['columns'])}")


@storage_app.command("upload-table", rich_help_panel=_TABLES)
def storage_upload_table(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    table_id: str = typer.Option(
        ...,
        "--table-id",
        help="Target table ID (e.g. 'in.c-my-bucket.my-table')",
    ),
    file: str = typer.Option(
        ...,
        "--file",
        help="Path to the CSV file to upload",
    ),
    incremental: bool = typer.Option(
        False,
        "--incremental",
        help="Append rows instead of full load (default: full load)",
    ),
    delimiter: str = typer.Option(
        ",",
        "--delimiter",
        help="CSV column delimiter (default: ',')",
    ),
    enclosure: str = typer.Option(
        '"',
        "--enclosure",
        help="CSV value enclosure character (default: '\"')'",
    ),
    auto_create: bool = typer.Option(
        True,
        "--auto-create/--no-auto-create",
        help="Auto-create bucket and table if they don't exist (default: on). "
        "Columns are inferred as STRING from the CSV header row.",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID (defaults to active branch if set via 'branch use')",
    ),
) -> None:
    """Upload a CSV file into a storage table.

    Auto-creates the bucket and table if they don't exist (columns inferred as
    STRING from the CSV header). Use --no-auto-create to require the table to
    already exist.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    p = Path(file)
    if not p.is_file():
        formatter.error(message=f"File not found: {file}", error_code="FILE_NOT_FOUND")
        raise typer.Exit(code=2) from None

    if not formatter.json_mode:
        size_mb = p.stat().st_size / (1024 * 1024)
        formatter.console.print(
            f"Uploading [bold]{p.name}[/bold] ({size_mb:.2f} MB) to [cyan]{table_id}[/cyan]..."
        )

    try:
        result = service.upload_table(
            alias=project,
            table_id=table_id,
            file_path=file,
            incremental=incremental,
            delimiter=delimiter,
            enclosure=enclosure,
            auto_create=auto_create,
            branch_id=effective_branch,
        )
    except ValueError as exc:
        formatter.error(message=str(exc), error_code="INVALID_ARGUMENT")
        raise typer.Exit(code=2) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        parts = result["table_id"].split(".")
        bucket_id = ".".join(parts[:2]) if len(parts) == 3 else ""
        if result.get("auto_created_bucket") and bucket_id:
            formatter.console.print(f"[dim]Created bucket: {bucket_id}[/dim]")
        if result.get("auto_created_table"):
            formatter.console.print(f"[dim]Created table: {result['table_id']}[/dim]")
        load_type = "incremental" if result["incremental"] else "full"
        size_mb = result.get("file_size_bytes", 0) / (1024 * 1024)
        formatter.console.print(
            f"[bold green]Uploaded:[/bold green] {result['table_id']} "
            f"({load_type} load, {size_mb:.2f} MB)"
        )
        if result["imported_rows"] is not None:
            formatter.console.print(f"  Rows imported: {result['imported_rows']}")
        if result["warnings"]:
            for w in result["warnings"]:
                formatter.console.print(f"  [yellow]Warning:[/yellow] {w}")


@storage_app.command("download-table", rich_help_panel=_TABLES)
def storage_download_table(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    table_id: str = typer.Option(
        ...,
        "--table-id",
        help="Table ID to export (e.g. 'in.c-my-bucket.my-table')",
    ),
    output: str | None = typer.Option(
        None,
        "--output",
        help="Output file path (default: {table_name}.csv)",
    ),
    columns: list[str] | None = typer.Option(
        None,
        "--columns",
        help="Column names to export (repeat for multiple: --columns col1 --columns col2)",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        help="Max number of rows to export",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID (defaults to active branch if set via 'branch use')",
    ),
) -> None:
    """Export a storage table to a local CSV file.

    Downloads table data via the async export API. Handles gzip
    decompression transparently. Use --columns to select specific
    columns and --limit to cap row count.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    if not formatter.json_mode:
        msg = f"Exporting [cyan]{table_id}[/cyan]"
        if columns:
            msg += f" (columns: {', '.join(columns)})"
        if limit:
            msg += f" (limit: {limit})"
        msg += "..."
        formatter.console.print(msg)

    try:
        result = service.download_table(
            alias=project,
            table_id=table_id,
            output_path=output,
            columns=columns,
            limit=limit,
            branch_id=effective_branch,
        )
    except ValueError as exc:
        formatter.error(message=str(exc), error_code="INVALID_ARGUMENT")
        raise typer.Exit(code=2) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        size_mb = result["file_size_bytes"] / (1024 * 1024)
        formatter.console.print(
            f"[bold green]Exported:[/bold green] {result['table_id']} -> {result['output_path']} "
            f"({size_mb:.2f} MB)"
        )


@storage_app.command("delete-table", rich_help_panel=_TABLES)
def storage_delete_table(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    table_id: list[str] = typer.Option(
        ...,
        "--table-id",
        help="Table ID to delete (e.g. 'in.c-bucket.table'). Can be repeated.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be deleted without executing",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID (defaults to active branch if set via 'branch use')",
    ),
) -> None:
    """Delete one or more storage tables.

    Supports batch deletion with multiple --table-id flags.
    All deletes are async and wait for completion.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    if dry_run:
        try:
            result = service.delete_tables(
                alias=project,
                table_ids=table_id,
                dry_run=True,
                branch_id=effective_branch,
            )
        except ConfigError as exc:
            formatter.error(message=exc.message, error_code="CONFIG_ERROR")
            raise typer.Exit(code=5) from None

        if formatter.json_mode:
            formatter.output(result)
        else:
            for tid in result.get("would_delete", []):
                formatter.console.print(f"[bold blue]Would delete:[/bold blue] {tid}")
        return

    if (
        not yes
        and not formatter.json_mode
        and not typer.confirm(f"Delete {len(table_id)} table(s) from project '{project}'?")
    ):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)

    try:
        result = service.delete_tables(
            alias=project,
            table_ids=table_id,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        for tid in result["deleted"]:
            formatter.console.print(f"[bold green]Deleted:[/bold green] {tid}")
        for f_item in result["failed"]:
            formatter.console.print(
                f"[bold red]Failed:[/bold red] {f_item['id']}: {f_item['error']}"
            )

    if result["failed"]:
        raise typer.Exit(code=1)


@storage_app.command("delete-bucket", rich_help_panel=_BUCKETS)
def storage_delete_bucket(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    bucket_id: list[str] = typer.Option(
        ...,
        "--bucket-id",
        help="Bucket ID to delete (e.g. 'in.c-my-bucket'). Can be repeated.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Force delete even if bucket contains tables (cascade)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be deleted without executing",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID (defaults to active branch if set via 'branch use')",
    ),
) -> None:
    """Delete one or more storage buckets.

    Without --force, fails if a bucket contains tables.
    With --force, cascade-deletes all tables in the bucket.
    Linked and shared buckets are protected (use sharing unlink/unshare).
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    try:
        result = service.delete_buckets(
            alias=project,
            bucket_ids=bucket_id,
            force=force,
            dry_run=dry_run,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        if dry_run:
            for bid in result.get("would_delete", []):
                force_hint = " [force]" if force else ""
                formatter.console.print(f"[bold blue]Would delete:[/bold blue] {bid}{force_hint}")
        else:
            for bid in result["deleted"]:
                formatter.console.print(f"[bold green]Deleted:[/bold green] {bid}")
        for f_item in result["failed"]:
            formatter.console.print(
                f"[bold red]Failed:[/bold red] {f_item['id']}: {f_item['error']}"
            )

    if result["failed"]:
        raise typer.Exit(code=1)


# ------------------------------------------------------------------
# File operations
# ------------------------------------------------------------------


def _format_file_size(size_bytes: int | None) -> str:
    """Format file size in human-readable form."""
    if size_bytes is None:
        return "unknown"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


@storage_app.command("files", rich_help_panel=_FILES)
def storage_file_list(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    tag: list[str] | None = typer.Option(
        None,
        "--tag",
        help="Filter by tag (repeat for AND logic: --tag a --tag b)",
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        help="Max number of files to return",
    ),
    offset: int = typer.Option(
        0,
        "--offset",
        help="Pagination offset",
    ),
    query: str | None = typer.Option(
        None,
        "--query",
        "-q",
        help="Full-text search on file name",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID (defaults to active branch if set via 'branch use')",
    ),
) -> None:
    """List Storage Files with optional tag filtering.

    Lists files from the project's Storage Files API. Use --tag to filter
    by tags (AND logic - all specified tags must match).
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    try:
        result = service.list_files(
            alias=project,
            limit=limit,
            offset=offset,
            tags=tag,
            query=query,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        files = result["files"]
        if not files:
            formatter.console.print("[dim]No files found.[/dim]")
            return

        from rich.table import Table

        table = Table(title=f"Storage Files ({result['count']} files)")
        table.add_column("ID", style="cyan")
        table.add_column("Name")
        table.add_column("Size", justify="right")
        table.add_column("Tags")
        table.add_column("Permanent")
        table.add_column("Created")

        for f in files:
            tags_str = ", ".join(f.get("tags", []))
            permanent = "yes" if f.get("isPermanent") else ""
            created = f.get("created", "")[:19] if f.get("created") else ""
            table.add_row(
                str(f.get("id", "")),
                f.get("name", ""),
                _format_file_size(f.get("sizeBytes")),
                tags_str,
                permanent,
                created,
            )

        formatter.console.print(table)


@storage_app.command("file-detail", rich_help_panel=_FILES)
def storage_file_info(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    file_id: int = typer.Option(
        ...,
        "--file-id",
        help="Storage file ID",
    ),
) -> None:
    """Show Storage File metadata (without downloading)."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")

    try:
        result = service.get_file_info(alias=project, file_id=file_id)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        formatter.console.print(f"[bold]File ID:[/bold] {result.get('id')}")
        formatter.console.print(f"[bold]Name:[/bold] {result.get('name')}")
        formatter.console.print(f"[bold]Size:[/bold] {_format_file_size(result.get('sizeBytes'))}")
        formatter.console.print(f"[bold]Created:[/bold] {result.get('created', '')}")
        formatter.console.print(f"[bold]Sliced:[/bold] {'yes' if result.get('isSliced') else 'no'}")
        formatter.console.print(
            f"[bold]Permanent:[/bold] {'yes' if result.get('isPermanent') else 'no'}"
        )
        tags_str = ", ".join(result.get("tags", []))
        formatter.console.print(f"[bold]Tags:[/bold] {tags_str or '(none)'}")
        creator = result.get("creatorToken", {})
        if isinstance(creator, dict):
            formatter.console.print(
                f"[bold]Creator:[/bold] {creator.get('description', 'unknown')}"
            )


@storage_app.command("file-upload", rich_help_panel=_FILES)
def storage_file_upload(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    file: str = typer.Option(
        ...,
        "--file",
        help="Path to the file to upload",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        help="Custom file name (default: local filename)",
    ),
    tag: list[str] | None = typer.Option(
        None,
        "--tag",
        help="Tag to assign (repeat for multiple: --tag a --tag b)",
    ),
    permanent: bool = typer.Option(
        False,
        "--permanent",
        help="Make file permanent (not auto-deleted after 15 days)",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID (defaults to active branch if set via 'branch use')",
    ),
) -> None:
    """Upload a local file to Storage Files.

    Uploads any file (CSV, JSON, ZIP, etc.) to Keboola Storage Files.
    Use --tag to assign tags and --permanent to prevent auto-deletion.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    p = Path(file)
    if not p.is_file():
        formatter.error(message=f"File not found: {file}", error_code="FILE_NOT_FOUND")
        raise typer.Exit(code=2) from None

    if not formatter.json_mode:
        size_str = _format_file_size(p.stat().st_size)
        formatter.console.print(f"Uploading [bold]{p.name}[/bold] ({size_str})...")

    try:
        result = service.upload_file(
            alias=project,
            file_path=file,
            name=name,
            tags=tag,
            is_permanent=permanent,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        size_str = _format_file_size(result.get("file_size_bytes"))
        tags_str = ", ".join(result.get("tags", []))
        formatter.console.print(
            f"[bold green]Uploaded:[/bold green] file ID {result['id']} "
            f"({result.get('name', '')}), {size_str}"
        )
        if tags_str:
            formatter.console.print(f"  Tags: {tags_str}")
        if result.get("isPermanent"):
            formatter.console.print("  Permanent: yes")


@storage_app.command("file-download", rich_help_panel=_FILES)
def storage_file_download(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    file_id: int | None = typer.Option(
        None,
        "--file-id",
        help="Storage file ID to download",
    ),
    tag: list[str] | None = typer.Option(
        None,
        "--tag",
        help="Download latest file matching tags (repeat for AND: --tag a --tag b)",
    ),
    output: str | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Output file path (default: original filename)",
    ),
) -> None:
    """Download a Storage File to local disk.

    Download by file ID (--file-id) or by tags (--tag, downloads the latest
    matching file). Handles both sliced and non-sliced files transparently.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")

    if not file_id and not tag:
        formatter.error(
            message="Either --file-id or --tag must be provided",
            error_code="INVALID_ARGUMENT",
        )
        raise typer.Exit(code=2) from None

    if not formatter.json_mode:
        if file_id:
            formatter.console.print(f"Downloading file ID [cyan]{file_id}[/cyan]...")
        else:
            formatter.console.print(f"Downloading latest file with tags: {', '.join(tag or [])}...")

    try:
        result = service.download_file(
            alias=project,
            file_id=file_id,
            tags=tag,
            output_path=output,
        )
    except ValueError as exc:
        formatter.error(message=str(exc), error_code="INVALID_ARGUMENT")
        raise typer.Exit(code=2) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        size_str = _format_file_size(result["file_size_bytes"])
        formatter.console.print(
            f"[bold green]Downloaded:[/bold green] {result['file_name']} "
            f"-> {result['output_path']} ({size_str})"
        )


@storage_app.command("file-tag", rich_help_panel=_FILES)
def storage_file_tag(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    file_id: int = typer.Option(
        ...,
        "--file-id",
        help="Storage file ID",
    ),
    add: list[str] | None = typer.Option(
        None,
        "--add",
        help="Tag to add (repeat for multiple: --add a --add b)",
    ),
    remove: list[str] | None = typer.Option(
        None,
        "--remove",
        help="Tag to remove (repeat for multiple: --remove a --remove b)",
    ),
) -> None:
    """Add and/or remove tags on a Storage File.

    Use --add and --remove to modify tags in a single operation.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")

    if not add and not remove:
        formatter.error(
            message="At least one of --add or --remove must be provided",
            error_code="INVALID_ARGUMENT",
        )
        raise typer.Exit(code=2) from None

    try:
        result = service.tag_file(
            alias=project,
            file_id=file_id,
            add_tags=add,
            remove_tags=remove,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        for tag_name in result["added"]:
            formatter.console.print(f"[bold green]Added tag:[/bold green] {tag_name}")
        for tag_name in result["removed"]:
            formatter.console.print(f"[bold yellow]Removed tag:[/bold yellow] {tag_name}")
        for err in result["errors"]:
            formatter.console.print(
                f"[bold red]Failed:[/bold red] {err['action']} tag '{err['tag']}': {err['error']}"
            )

    if result["errors"]:
        raise typer.Exit(code=1)


@storage_app.command("file-delete", rich_help_panel=_FILES)
def storage_file_delete(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    file_id: list[int] = typer.Option(
        ...,
        "--file-id",
        help="Storage file ID to delete (repeat for multiple)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be deleted without executing",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt",
    ),
) -> None:
    """Delete one or more Storage Files."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")

    try:
        result = service.delete_files(
            alias=project,
            file_ids=file_id,
            dry_run=dry_run,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        if dry_run:
            for fid in result.get("would_delete", []):
                formatter.console.print(f"[bold blue]Would delete:[/bold blue] file ID {fid}")
        else:
            for fid in result["deleted"]:
                formatter.console.print(f"[bold green]Deleted:[/bold green] file ID {fid}")
        for f_err in result["failed"]:
            formatter.console.print(
                f"[bold red]Failed:[/bold red] file ID {f_err['id']}: {f_err['error']}"
            )

    if result["failed"]:
        raise typer.Exit(code=1)


@storage_app.command("load-file", rich_help_panel=_FILES)
def storage_load_file(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    file_id: int = typer.Option(
        ...,
        "--file-id",
        help="Storage file ID to load into a table",
    ),
    table_id: str = typer.Option(
        ...,
        "--table-id",
        help="Target table ID (e.g. 'in.c-my-bucket.my-table')",
    ),
    incremental: bool = typer.Option(
        False,
        "--incremental",
        help="Append rows instead of full load",
    ),
    delimiter: str = typer.Option(
        ",",
        "--delimiter",
        help="CSV column delimiter",
    ),
    enclosure: str = typer.Option(
        '"',
        "--enclosure",
        help="CSV value enclosure character",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID (defaults to active branch if set via 'branch use')",
    ),
) -> None:
    """Load a Storage File into a table.

    Imports an already-uploaded file (from file-upload or component output)
    into a storage table. Use --incremental to append rows.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    if not formatter.json_mode:
        formatter.console.print(
            f"Loading file ID [cyan]{file_id}[/cyan] into [cyan]{table_id}[/cyan]..."
        )

    try:
        result = service.load_file_to_table(
            alias=project,
            file_id=file_id,
            table_id=table_id,
            incremental=incremental,
            delimiter=delimiter,
            enclosure=enclosure,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        load_type = "incremental" if result["incremental"] else "full"
        formatter.console.print(
            f"[bold green]Loaded:[/bold green] file {result['file_id']} -> "
            f"{result['table_id']} ({load_type} load)"
        )
        if result["imported_rows"] is not None:
            formatter.console.print(f"  Rows imported: {result['imported_rows']}")
        for w in result.get("warnings", []):
            formatter.console.print(f"  [yellow]Warning:[/yellow] {w}")


@storage_app.command("unload-table", rich_help_panel=_FILES)
def storage_unload_table(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    table_id: str = typer.Option(
        ...,
        "--table-id",
        help="Table ID to export (e.g. 'in.c-my-bucket.my-table')",
    ),
    columns: list[str] | None = typer.Option(
        None,
        "--columns",
        help="Column names to export (repeat for multiple)",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        help="Max number of rows to export",
    ),
    tag: list[str] | None = typer.Option(
        None,
        "--tag",
        help="Tag to apply to the exported file (repeat for multiple)",
    ),
    download: bool = typer.Option(
        False,
        "--download",
        help="Also download the exported file locally",
    ),
    output: str | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Output file path (only with --download)",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID (defaults to active branch if set via 'branch use')",
    ),
) -> None:
    """Export a table to a Storage File.

    Creates a file in Storage that can be downloaded or consumed by other
    components. Use --tag to tag the output file and --download to also
    save it locally.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    if not formatter.json_mode:
        msg = f"Exporting [cyan]{table_id}[/cyan] to Storage File"
        if download:
            msg += " (with download)"
        msg += "..."
        formatter.console.print(msg)

    try:
        result = service.unload_table_to_file(
            alias=project,
            table_id=table_id,
            columns=columns,
            limit=limit,
            tags=tag,
            download=download,
            output_path=output,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        size_str = _format_file_size(result.get("file_size_bytes"))
        tags_str = ", ".join(result.get("tags", []))
        formatter.console.print(
            f"[bold green]Exported:[/bold green] {result['table_id']} -> "
            f"file ID {result['file_id']} ({size_str})"
        )
        if tags_str:
            formatter.console.print(f"  Tags: {tags_str}")
        if result.get("downloaded"):
            dl_size = _format_file_size(result.get("downloaded_bytes"))
            formatter.console.print(f"  Downloaded to: {result['output_path']} ({dl_size})")
