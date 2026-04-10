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

storage_app = typer.Typer(help="Browse and manage storage buckets and tables")


@storage_app.callback(invoke_without_command=True)
def _storage_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "storage")


@storage_app.command("buckets")
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


@storage_app.command("create-bucket")
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


@storage_app.command("create-table")
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


@storage_app.command("upload-table")
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


@storage_app.command("delete-table")
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
        for f in result["failed"]:
            formatter.console.print(f"[bold red]Failed:[/bold red] {f['id']}: {f['error']}")

    if result["failed"]:
        raise typer.Exit(code=1)


@storage_app.command("delete-bucket")
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
        for f in result["failed"]:
            formatter.console.print(f"[bold red]Failed:[/bold red] {f['id']}: {f['error']}")

    if result["failed"]:
        raise typer.Exit(code=1)
