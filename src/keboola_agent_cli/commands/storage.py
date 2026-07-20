"""Storage commands - buckets, tables, and direct access path resolution.

Provides direct Storage API access including sharing/linked bucket metadata
that is not available via MCP tools.
"""

from pathlib import Path
from typing import Any

import typer
from rich.markup import escape

from ..config_store import ConfigStore
from ..errors import ConfigError, ErrorCode, KeboolaApiError
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

# Surfaced in human mode whenever a branch-aware write completes against a
# project lacking the `storage-branches` feature. The transformation runner
# on such projects ignores buckets created via /v2/storage/branch/<id>/buckets
# and rewrites destinations to `out.c-<branch_id>-*` in the default branch
# at job time -- so the bucket the user just created here is reachable only
# from the branch view (and via direct Snowflake) but will NOT receive
# transformation output. JSON mode surfaces the same signal as the
# `legacy_branch_storage: true` field on the response.
_LEGACY_BRANCH_STORAGE_WARNING: str = (
    "  [yellow]Warning:[/yellow] this project uses legacy fake-branch storage "
    "(no `storage-branches` feature). The transformation runner will create "
    "a separate `out.c-<branch_id>-...` bucket on its own at job time; the "
    "bucket created here is reachable from the branch view and direct "
    "Snowflake queries, but transformations will not write into it."
)


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

    Branch handling: this read command uses the production endpoint by
    default, even when a dev branch is active via `branch use`. The
    Storage API branch-scoped endpoint only returns locally-modified
    buckets, so a fresh dev branch lists nothing. Pass --branch to query
    a dev branch explicitly.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]

    # --branch requires exactly one --project
    if branch is not None and (not project or len(project) != 1):
        formatter.error(
            message="--branch requires exactly one --project (branch ID is per-project)",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    # Resolve active branch for single-project queries.
    # Storage read commands ignore the implicit active dev branch: the
    # Storage API branch-scoped endpoint returns only locally-modified
    # buckets, which for a freshly created dev branch is an empty set.
    # Explicit --branch still wins.
    effective_branch: int | None = branch
    if branch is None and project and len(project) == 1:
        _, effective_branch = resolve_branch(
            config_store, formatter, project[0], None, ignore_active_branch=True
        )

    try:
        result = service.list_buckets(aliases=project, branch_id=effective_branch)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
                    linked = f"{escape(b['source_project_name'])} (#{b['source_project_id']})"
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
    """Show detailed bucket info including backend-native direct access paths.

    For linked/shared buckets, resolves the correct database/dataset and
    schema from the source project. Each table includes a ready-to-use
    fully-qualified path with dialect-correct quoting:

    - Snowflake -> ``"DATABASE"."schema"."table"`` (double quotes)
    - BigQuery  -> ``\\`project\\`.\\`dataset\\`.\\`table\\``` (backticks);
      ``project`` is omitted when the API does not expose it.

    Backend-agnostic ``sql_dialect`` and per-table ``sql_path`` keys are
    always present in JSON output.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]
    # Read command: ignore implicit active dev branch (empty listing trap).
    _, effective_branch = resolve_branch(
        config_store, formatter, project, branch, ignore_active_branch=True
    )

    try:
        result = service.get_bucket_detail(
            alias=project,
            bucket_id=bucket_id,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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

        dialect = result.get("sql_dialect", "snowflake")
        if dialect == "bigquery":
            bq_project = result.get("bigquery_project", "")
            if bq_project:
                formatter.console.print(f"  BigQuery project: {bq_project}")
            else:
                formatter.console.print(
                    "  BigQuery project: [dim](not exposed by Storage API "
                    "-- supply your GCP project for full FQN)[/dim]"
                )
            formatter.console.print(f"  BigQuery dataset: {result.get('bigquery_dataset', '')}")
        else:
            formatter.console.print(f"  Snowflake DB: {result['snowflake_database']}")
            formatter.console.print(f"  Snowflake schema: {result['snowflake_schema']}")
        formatter.console.print(f"  Tables: {result['table_count']}")

        if result["tables"]:
            formatter.console.print()
            from rich.table import Table

            path_col = "BigQuery Path" if dialect == "bigquery" else "Snowflake Path"
            table = Table(title=f"Tables with {dialect} paths")
            table.add_column("Table", style="bold")
            table.add_column(path_col, style="green")
            table.add_column("Alias", style="dim")

            for t in result["tables"][:50]:  # limit display
                table.add_row(
                    t["name"],
                    t.get("sql_path", ""),
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
    project: list[str] | None = typer.Option(
        None,
        "--project",
        help="Project alias (can be repeated for multiple projects). "
        "Omit to query all connected projects in parallel.",
    ),
    bucket_id: str | None = typer.Option(
        None,
        "--bucket-id",
        help="Filter tables by bucket ID (applied independently per project)",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID (defaults to active branch if set via 'branch use')",
    ),
) -> None:
    """List storage tables from one or more projects.

    Queries all connected projects in parallel by default, matching the
    behaviour of ``storage buckets``, ``config list``, ``job list``, and other
    read commands. Each row in the output is tagged with ``project_alias``
    so results from multiple projects can be distinguished.

    Branch handling: this read command uses the production endpoint by
    default, even when a dev branch is active via `branch use`. The
    Storage API branch-scoped endpoint only returns tables that were
    locally modified in the dev branch, so a fresh dev branch lists
    nothing. Pass --branch to query a dev branch explicitly.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]

    # --branch requires exactly one --project (branch ID is per-project).
    # Mirrors the validation used by `storage buckets` and `config list`.
    if branch is not None and (not project or len(project) != 1):
        formatter.error(
            message="--branch requires exactly one --project (branch ID is per-project)",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    # Resolve active branch only for single-project queries; multi-project
    # listing intentionally skips active-branch resolution because branches
    # are per-project state. Read commands use ignore_active_branch=True:
    # Storage API branch endpoint only returns locally modified tables, so
    # auto-scoping to the active branch traps users into an empty listing.
    effective_branch: int | None = branch
    if branch is None and project and len(project) == 1:
        _, effective_branch = resolve_branch(
            config_store, formatter, project[0], None, ignore_active_branch=True
        )

    try:
        result = service.list_tables(
            aliases=project,
            bucket_id=bucket_id,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
            emit_project_warnings(formatter, result)
            return

        # Group by project so multi-project output stays readable.
        by_project: dict[str, list[dict]] = {}
        for t in tables:
            alias = t["project_alias"]
            by_project.setdefault(alias, []).append(t)

        for alias, proj_tables in by_project.items():
            table = Table(title=f"Tables - {alias}")
            table.add_column("Table ID", style="bold cyan")
            table.add_column("Rows", justify="right")
            table.add_column("Size", justify="right", style="dim")
            table.add_column("Last Import", style="dim")

            for t in proj_tables:
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
            formatter.console.print()

        emit_project_warnings(formatter, result)


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
    # Read command: ignore implicit active dev branch (empty listing trap).
    _, effective_branch = resolve_branch(
        config_store, formatter, project, branch, ignore_active_branch=True
    )

    try:
        result = service.get_table_detail(
            alias=project,
            table_id=table_id,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
        formatter.error(message=str(exc), error_code=ErrorCode.INVALID_ARGUMENT)
        raise typer.Exit(code=2) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
        if result.get("legacy_branch_storage"):
            formatter.console.print(_LEGACY_BRANCH_STORAGE_WARNING)


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
    column: list[str] | None = typer.Option(
        None,
        "--column",
        help=(
            "Column as 'name:TYPE' or 'name:TYPE(length)'. Repeatable. Base types: "
            "STRING, INTEGER, NUMERIC, FLOAT, BOOLEAN, DATE, TIMESTAMP. Native types "
            "are passed through to the Storage API (e.g. 'pk:VARCHAR(40)', "
            "'amount:NUMERIC(18,2)', 'ts:TIMESTAMP_TZ', 'meta:VARIANT'). Required "
            "unless --source-table-id is given; the two are mutually exclusive."
        ),
    ),
    primary_key: list[str] | None = typer.Option(
        None,
        "--primary-key",
        help="Primary key column name. Can be repeated.",
    ),
    not_null: list[str] | None = typer.Option(
        None,
        "--not-null",
        help="Column name to mark NOT NULL. Can be repeated. Must match a --column name.",
    ),
    default: list[str] | None = typer.Option(
        None,
        "--default",
        help=(
            "Column default as 'name=value'. Can be repeated. Boolean values must be "
            "lowercase ('true'/'false') per Keboola API validation."
        ),
    ),
    source_table_id: str | None = typer.Option(
        None,
        "--source-table-id",
        help=(
            "Create the table by copying an existing table's data into the "
            "requested partition/clustering layout (BigQuery only). The column "
            "definition is derived from the source, so --column must not be used. "
            "Pair with 'swap-tables' to repartition a populated table in place."
        ),
    ),
    source_branch_id: int | None = typer.Option(
        None,
        "--source-branch-id",
        help="Branch ID the source table is resolved in (defaults to the request branch).",
    ),
    time_partitioning_type: str | None = typer.Option(
        None,
        "--time-partitioning-type",
        help="BigQuery time partitioning type, e.g. DAY, HOUR, MONTH, YEAR.",
    ),
    time_partitioning_field: str | None = typer.Option(
        None,
        "--time-partitioning-field",
        help="Column used for time partitioning (defaults to ingestion time if omitted).",
    ),
    time_partitioning_expiration_ms: str | None = typer.Option(
        None,
        "--time-partitioning-expiration-ms",
        help="Milliseconds to keep storage for a partition (BigQuery).",
    ),
    range_partitioning_field: str | None = typer.Option(
        None,
        "--range-partitioning-field",
        help="Column used for integer-range partitioning (BigQuery).",
    ),
    range_partitioning_start: str | None = typer.Option(
        None,
        "--range-partitioning-start",
        help="Start of the range partitioning, inclusive (required with other range flags).",
    ),
    range_partitioning_end: str | None = typer.Option(
        None,
        "--range-partitioning-end",
        help="End of the range partitioning, exclusive (required with other range flags).",
    ),
    range_partitioning_interval: str | None = typer.Option(
        None,
        "--range-partitioning-interval",
        help="Width of each range interval (required with other range flags).",
    ),
    clustering_field: list[str] | None = typer.Option(
        None,
        "--clustering-field",
        help="Column used for clustering (BigQuery). Repeatable.",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID (defaults to active branch if set via 'branch use')",
    ),
    if_not_exists: bool = typer.Option(
        False,
        "--if-not-exists",
        help=(
            "Treat a duplicate-display-name failure as a successful no-op "
            "when the table already exists at the expected id. Safe for "
            "parallel workers (FIIA scaffold pattern). A different table "
            "with the same display name still surfaces the original error."
        ),
    ),
) -> None:
    """Create a new storage table with typed columns.

    Base types (`STRING`, `INTEGER`, `NUMERIC`, `FLOAT`, `BOOLEAN`, `DATE`,
    `TIMESTAMP`) plus any native backend type (`VARCHAR(n)`, `NUMBER(p,s)`,
    `TIMESTAMP_TZ`, `VARIANT`, etc.) are accepted. Type/length validation
    is delegated to the Keboola Storage API, which has precise per-backend
    rules and returns actionable errors.

    When `--branch` targets a dev branch and the bucket has not been
    materialized there yet, kbagent auto-creates it (mirrors the official
    Go CLI's `EnsureBucketExists`). The response's `auto_created_bucket`
    flag reports whether this happened.

    Examples:
        kbagent storage create-table --project p --bucket-id in.c-b --name t \\
            --column id:INTEGER --column name:STRING --primary-key id

        kbagent storage create-table --project p --bucket-id in.c-b --name sales \\
            --column pk:VARCHAR(40) --column amount:NUMERIC(18,2) \\
            --column ts:TIMESTAMP_TZ --column is_paid:BOOLEAN \\
            --primary-key pk --not-null pk --not-null amount \\
            --default amount=0 --default is_paid=false

        # BigQuery: repartition a populated table by copying it into a new
        # layout, then swap it into place (no --column -- schema derives from
        # the source):
        kbagent storage create-table --project p --bucket-id in.c-main \\
            --name events_repart --source-table-id in.c-main.events \\
            --time-partitioning-type DAY --time-partitioning-field created_at \\
            --clustering-field tenant_id --primary-key id --branch 123
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
            not_null_columns=not_null,
            defaults=default,
            if_not_exists=if_not_exists,
            source_table_id=source_table_id,
            source_branch_id=source_branch_id,
            time_partitioning_type=time_partitioning_type,
            time_partitioning_field=time_partitioning_field,
            time_partitioning_expiration_ms=time_partitioning_expiration_ms,
            range_partitioning_field=range_partitioning_field,
            range_partitioning_start=range_partitioning_start,
            range_partitioning_end=range_partitioning_end,
            range_partitioning_interval=range_partitioning_interval,
            clustering_fields=clustering_field,
        )
    except ValueError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.INVALID_ARGUMENT)
        raise typer.Exit(code=2) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        if result.get("action") == "skipped":
            formatter.console.print(
                f"[bold yellow]Skipped[/bold yellow] (already exists): {result['table_id']}"
            )
            reason = result.get("skip_reason")
            if reason:
                formatter.console.print(f"  [dim]{reason}[/dim]")
            if result.get("schema_drift"):
                formatter.console.print(
                    "  [yellow]Warning:[/yellow] the existing table's schema differs "
                    "from the requested definition. The fields below show the ACTUAL "
                    "existing schema; your requested schema was not applied."
                )
            if result.get("primary_key"):
                formatter.console.print(f"  Primary key: {', '.join(result['primary_key'])}")
            if result.get("columns"):
                formatter.console.print(f"  Columns: {', '.join(result['columns'])}")
        else:
            formatter.console.print(f"[bold green]Created table:[/bold green] {result['table_id']}")
            if result.get("auto_created_bucket"):
                formatter.console.print(
                    f"  [yellow]Note:[/yellow] bucket {result['bucket_id']} was "
                    f"auto-materialized in this branch."
                )
            if result.get("source_table_id"):
                formatter.console.print(f"  Copied from: {result['source_table_id']}")
            if result["primary_key"]:
                formatter.console.print(f"  Primary key: {', '.join(result['primary_key'])}")
            if result.get("columns"):
                formatter.console.print(f"  Columns: {', '.join(result['columns'])}")
            time_partitioning = result.get("time_partitioning")
            if time_partitioning:
                field = time_partitioning.get("field")
                suffix = f" on {field}" if field else " (ingestion time)"
                formatter.console.print(f"  Time partitioning: {time_partitioning['type']}{suffix}")
            range_partitioning = result.get("range_partitioning")
            if range_partitioning:
                bounds = range_partitioning.get("range") or {}
                bounds_suffix = ""
                if bounds:
                    bounds_suffix = (
                        f" [{bounds.get('start')}, {bounds.get('end')})"
                        f" step {bounds.get('interval')}"
                    )
                formatter.console.print(
                    f"  Range partitioning: {range_partitioning['field']}{bounds_suffix}"
                )
            clustering = result.get("clustering")
            if clustering:
                formatter.console.print(f"  Clustering: {', '.join(clustering['fields'])}")
            if result.get("legacy_branch_storage"):
                formatter.console.print(_LEGACY_BRANCH_STORAGE_WARNING)


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
        formatter.error(message=f"File not found: {file}", error_code=ErrorCode.FILE_NOT_FOUND)
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
        formatter.error(message=str(exc), error_code=ErrorCode.INVALID_ARGUMENT)
        raise typer.Exit(code=2) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
        help=(
            "Output path. Default mode: file path (e.g. table.csv). "
            "With --keep-slices: directory path (default ./{project}/{table_id}.csv/)."
        ),
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
    keep_slices: bool = typer.Option(
        False,
        "--keep-slices",
        help=(
            "Save each slice as its own file under --output (treated as a "
            "directory). Avoids the concat pass, matches the parquet download "
            "layout, and is the analytical-workflow-friendly option for DuckDB, "
            "polars, Spark. A _columns.csv sidecar holds the column order."
        ),
    ),
    where_column: str | None = typer.Option(
        None,
        "--where-column",
        help="Export only rows where this column matches --where-value(s).",
    ),
    where_operator: str = typer.Option(
        "eq",
        "--where-operator",
        help="Filter operator: 'eq' (default) or 'neq'.",
    ),
    where_value: list[str] | None = typer.Option(
        None,
        "--where-value",
        help="Value(s) for --where-column (repeat for multiple: matched as OR).",
    ),
    changed_since: str | None = typer.Option(
        None,
        "--changed-since",
        help="Only rows imported since this time (unix ts or strtotime, e.g. '-2 days').",
    ),
    changed_until: str | None = typer.Option(
        None,
        "--changed-until",
        help="Only rows imported up to this time (unix ts or strtotime).",
    ),
) -> None:
    """Export a storage table to a local CSV file.

    Downloads table data via the async export API. Handles gzip
    decompression transparently. Use --columns to select specific
    columns and --limit to cap row count.

    Use --keep-slices to write the individual slices into a directory
    instead of concatenating them into a single file.
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
            keep_slices=keep_slices,
            where_column=where_column,
            where_operator=where_operator,
            where_values=where_value,
            changed_since=changed_since,
            changed_until=changed_until,
        )
    except ValueError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.INVALID_ARGUMENT)
        raise typer.Exit(code=2) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        size_mb = result["file_size_bytes"] / (1024 * 1024)
        suffix = (
            f", {result['slice_count']} slices"
            if result.get("keep_slices") and result.get("slice_count")
            else ""
        )
        formatter.console.print(
            f"[bold green]Exported:[/bold green] {result['table_id']} -> {result['output_path']} "
            f"({size_mb:.2f} MB{suffix})"
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
    force: bool = typer.Option(
        False,
        "--force",
        help="Force-delete tables that have aliases in other projects (cascade).",
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

    Use --force to cascade-delete tables that have aliases linked
    into other projects (shared buckets). Without --force, the API
    rejects deletion of aliased tables.
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
            formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
            raise typer.Exit(code=5) from None

        if formatter.json_mode:
            formatter.output(result)
        else:
            for tid in result.get("would_delete", []):
                formatter.console.print(f"[bold blue]Would delete:[/bold blue] {tid}")
        return

    confirm_msg = f"Delete {len(table_id)} table(s) from project '{project}'?"
    if force:
        confirm_msg = (
            f"FORCE-delete {len(table_id)} table(s) from project '{project}'?"
            " This will also delete all aliases in downstream projects."
        )
    if not yes and not formatter.json_mode and not typer.confirm(confirm_msg):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)

    try:
        result = service.delete_tables(
            alias=project,
            table_ids=table_id,
            force=force,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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


@storage_app.command("truncate-table", rich_help_panel=_TABLES)
def storage_truncate_table(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    table_id: list[str] = typer.Option(
        ...,
        "--table-id",
        help="Table ID to truncate (e.g. 'in.c-bucket.table'). Can be repeated.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be truncated without executing",
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
    """Truncate (delete all rows from) one or more storage tables.

    Preserves the table definition: columns, types, primary key,
    descriptions, sharing edges, and dependents are unaffected -- only
    rows are removed. Idempotent (truncating an empty table is a no-op).

    The Storage API truncate endpoint is asynchronous: it returns a
    queued storage job which the client polls to completion before
    surfacing the result. Both production and dev branches behave the
    same way; the only difference is wall-clock latency (sub-second
    on production, longer on busy dev branches).

    Use this when re-seeding a table without losing the schema contract.
    To destroy the table itself, use ``storage delete-table``.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    if dry_run:
        try:
            result = service.truncate_tables(
                alias=project,
                table_ids=table_id,
                dry_run=True,
                branch_id=effective_branch,
            )
        except ConfigError as exc:
            formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
            raise typer.Exit(code=5) from None

        if formatter.json_mode:
            formatter.output(result)
        else:
            for entry in result.get("would_truncate", []):
                formatter.console.print(
                    f"[bold blue]Would truncate:[/bold blue] {entry['table_id']} "
                    f"(rows_before={entry['rows_before']})"
                )
        return

    confirm_msg = (
        f"Truncate {len(table_id)} table(s) in project '{project}'? "
        "All rows will be deleted; schema and dependents are preserved."
    )
    if not yes and not formatter.json_mode and not typer.confirm(confirm_msg):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)

    try:
        result = service.truncate_tables(
            alias=project,
            table_ids=table_id,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        for entry in result["truncated"]:
            formatter.console.print(
                f"[bold green]Truncated:[/bold green] {entry['table_id']} "
                f"({entry['rows_before']} -> 0 rows)"
            )
        for f_item in result["failed"]:
            formatter.console.print(
                f"[bold red]Failed:[/bold red] {f_item['id']}: {f_item['error']}"
            )

    if result["failed"]:
        raise typer.Exit(code=1)


@storage_app.command("add-column", rich_help_panel=_TABLES)
def storage_add_column(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    table_id: str = typer.Option(
        ...,
        "--table-id",
        help="Table ID to add the column to (e.g. 'in.c-bucket.table')",
    ),
    column: str = typer.Option(
        ...,
        "--column",
        help=(
            "Column spec: 'name', 'name:TYPE', or 'name:TYPE(length)' "
            "(e.g. 'status:VARCHAR(20)', 'amount:NUMBER(18,2)')."
        ),
    ),
    not_null: bool = typer.Option(
        False,
        "--not-null",
        help="Make the new column NOT NULL (needs an empty table or a --default).",
    ),
    default: str | None = typer.Option(
        None,
        "--default",
        help="Default value for the new column.",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID (defaults to active branch if set via 'branch use')",
    ),
) -> None:
    """Add a single column to an existing table (synchronous, typed).

    Mirrors ``create-table --column``: ``name:TYPE(length)`` creates a typed
    column; a bare ``name`` adds an untyped STRING column. The Storage
    add-column endpoint is synchronous -- there is no job to wait on.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    try:
        result = service.add_column(
            alias=project,
            table_id=table_id,
            column=column,
            not_null=not_null,
            default=default,
            branch_id=effective_branch,
        )
    except ValueError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.INVALID_ARGUMENT)
        raise typer.Exit(code=2) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        col_type = result["definition"].get("type", "STRING")
        formatter.console.print(
            f"[bold green]Added column:[/bold green] {result['column']} "
            f"({col_type}) to {result['table_id']}"
        )


@storage_app.command("delete-column", rich_help_panel=_TABLES)
def storage_delete_column(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    table_id: str = typer.Option(
        ...,
        "--table-id",
        help="Table ID containing the column(s) (e.g. 'in.c-bucket.table')",
    ),
    column: list[str] = typer.Option(
        ...,
        "--column",
        help="Column name to delete. Can be repeated.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Force delete even if column is referenced by table aliases",
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
    """Delete one or more columns from a storage table.

    Supports batch deletion with multiple --column flags.
    Use --force when a column is referenced by table aliases.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    if dry_run:
        try:
            result = service.delete_columns(
                alias=project,
                table_id=table_id,
                columns=column,
                dry_run=True,
                branch_id=effective_branch,
            )
        except ConfigError as exc:
            formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
            raise typer.Exit(code=5) from None

        if formatter.json_mode:
            formatter.output(result)
        else:
            for col in result.get("would_delete", []):
                formatter.console.print(
                    f"[bold blue]Would delete:[/bold blue] {col} from {table_id}"
                )
        return

    if (
        not yes
        and not formatter.json_mode
        and not typer.confirm(
            f"Delete {len(column)} column(s) from table '{table_id}' in project '{project}'?"
        )
    ):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)

    try:
        result = service.delete_columns(
            alias=project,
            table_id=table_id,
            columns=column,
            force=force,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        for col in result["deleted"]:
            formatter.console.print(f"[bold green]Deleted:[/bold green] {col} from {table_id}")
        for f_item in result["failed"]:
            formatter.console.print(
                f"[bold red]Failed:[/bold red] {f_item['column']}: {f_item['error']}"
            )

    if result["failed"]:
        raise typer.Exit(code=1)


@storage_app.command("swap-tables", rich_help_panel=_TABLES)
def storage_swap_tables(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    table_id: str = typer.Option(
        ...,
        "--table-id",
        help="First table ID (e.g. 'in.c-bucket.table')",
    ),
    target_table_id: str = typer.Option(
        ...,
        "--target-table-id",
        help="Second table ID to swap with the first",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help=(
            "Branch ID. Required; defaults to the active branch set via "
            "'kbagent branch use'. Any branch works, including the "
            "default/production branch -- a default-branch swap is how a "
            "typed rebuild is applied to production."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be swapped without executing",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt",
    ),
) -> None:
    """Swap two storage tables (any branch, including the default/production branch).

    Both tables exchange physical positions. Aliases are NOT transferred --
    they keep pointing at the same physical position and therefore expose
    the OTHER table's data after the swap. Use this to promote a typed
    rebuild ("data_change_log" with proper column types) into the original
    name ("data") without touching downstream config references.

    \b
    branch_id is mandatory (the swap is always branch-scoped): the command
    resolves the active branch from 'kbagent branch use' if --branch is
    omitted, and exits 5 before any HTTP call if no branch is set in either
    place. Any branch works, INCLUDING the default/production branch -- a
    default-branch swap is how a typed rebuild is applied to prod, since a
    dev-branch merge does not carry storage schema.

    \b
    Example:
      kbagent branch use --project P --branch 1234
      kbagent storage swap-tables --project P \\
        --table-id in.c-foo.data --target-table-id in.c-foo.data_change_log
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    if dry_run:
        try:
            result = service.swap_tables(
                alias=project,
                table_id=table_id,
                target_table_id=target_table_id,
                branch_id=effective_branch,
                dry_run=True,
            )
        except ConfigError as exc:
            formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
            raise typer.Exit(code=5) from None

        if formatter.json_mode:
            formatter.output(result)
        else:
            formatter.console.print(
                f"[bold blue]Would swap (branch {result['branch_id']}):[/bold blue] "
                f"{result['table_id']} <-> {result['target_table_id']}"
            )
        return

    confirm_msg = (
        f"Swap '{table_id}' <-> '{target_table_id}' in project '{project}' "
        f"on branch {effective_branch}? Aliases will continue to point at the "
        "same physical position (i.e. they will expose the OTHER table's data "
        "after the swap)."
    )
    if not yes and not formatter.json_mode and not typer.confirm(confirm_msg):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)

    try:
        result = service.swap_tables(
            alias=project,
            table_id=table_id,
            target_table_id=target_table_id,
            branch_id=effective_branch,
            dry_run=False,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            project=project,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        formatter.console.print(
            f"[bold green]Swapped:[/bold green] {result['table_id']} <-> "
            f"{result['target_table_id']} (branch {result['branch_id']})"
        )


@storage_app.command("clone-table", rich_help_panel=_TABLES)
def storage_clone_table(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    table_id: str = typer.Option(
        ...,
        "--table-id",
        help="Table ID to pull into the branch (e.g. 'in.c-bucket.table')",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help=(
            "Target dev branch ID. Required; defaults to the active branch "
            "set via 'kbagent branch use'. The pull is one-way: default -> branch."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be pulled without executing",
    ),
) -> None:
    """Clone (pull) a production table into a development branch.

    On storage-branches projects a dev branch reads production tables
    transparently until the first write. To mutate a table's schema in the
    branch -- e.g. 'swap-tables' or dropping a column -- you first need a
    branch-local copy of the production table; without it the Storage API
    reports the bucket as "not found" in the branch. This materializes that
    copy from the default branch (one-way: default -> branch).

    \b
    Example:
      kbagent branch use --project P --branch 1234
      kbagent storage clone-table --project P --table-id in.c-foo.data
      kbagent storage swap-tables --project P \\
        --table-id in.c-foo.data --target-table-id in.c-foo.data_typed
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    try:
        result = service.clone_table(
            alias=project,
            table_id=table_id,
            branch_id=effective_branch,
            dry_run=dry_run,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        exit_code = map_error_to_exit_code(exc)
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            project=project,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None

    if dry_run:
        if formatter.json_mode:
            formatter.output(result)
        else:
            formatter.console.print(
                f"[bold blue]Would clone (branch {result['branch_id']}):[/bold blue] "
                f"{result['table_id']} (default -> branch)"
            )
        return

    if formatter.json_mode:
        formatter.output(result)
    else:
        formatter.console.print(
            f"[bold green]Cloned:[/bold green] {result['table_id']} "
            f"into branch {result['branch_id']}"
        )


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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
# Describe (metadata write) commands
# ------------------------------------------------------------------

_DESCRIBE = "Descriptions"


@storage_app.command("describe-bucket", rich_help_panel=_DESCRIBE)
def storage_describe_bucket(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    bucket_id: str = typer.Option(
        ...,
        "--bucket-id",
        help="Bucket ID (e.g. 'in.c-my-bucket')",
    ),
    text: str | None = typer.Option(
        None,
        "--text",
        help="Description text (inline)",
    ),
    file: Path | None = typer.Option(
        None,
        "--file",
        help="Path to a file containing the description",
    ),
    stdin: bool = typer.Option(
        False,
        "--stdin",
        help="Read description from standard input",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID (defaults to active branch if set via 'branch use')",
    ),
) -> None:
    """Set the description on a storage bucket.

    Stores the description as KBC.description in bucket metadata (upsert).
    Provide the text via --text, --file, or --stdin (exactly one required).
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    from ._metadata_input import resolve_text_input

    try:
        description = resolve_text_input(text=text, file=file, stdin=stdin)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.INVALID_ARGUMENT)
        raise typer.Exit(code=2) from None

    try:
        result = service.describe_bucket(
            alias=project,
            bucket_id=bucket_id,
            description=description,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        formatter.console.print(f"[bold green]Description set:[/bold green] {bucket_id}")
        formatter.console.print(f"  {description[:120]}")


@storage_app.command("describe-table", rich_help_panel=_DESCRIBE)
def storage_describe_table(
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
    text: str | None = typer.Option(
        None,
        "--text",
        help="Description text (inline)",
    ),
    file: Path | None = typer.Option(
        None,
        "--file",
        help="Path to a file containing the description",
    ),
    stdin: bool = typer.Option(
        False,
        "--stdin",
        help="Read description from standard input",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID (defaults to active branch if set via 'branch use')",
    ),
) -> None:
    """Set the description on a storage table.

    Stores the description as KBC.description in table metadata (upsert).
    Provide the text via --text, --file, or --stdin (exactly one required).
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    from ._metadata_input import resolve_text_input

    try:
        description = resolve_text_input(text=text, file=file, stdin=stdin)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.INVALID_ARGUMENT)
        raise typer.Exit(code=2) from None

    try:
        result = service.describe_table(
            alias=project,
            table_id=table_id,
            description=description,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        formatter.console.print(f"[bold green]Description set:[/bold green] {table_id}")
        formatter.console.print(f"  {description[:120]}")


@storage_app.command("describe-column", rich_help_panel=_DESCRIBE)
def storage_describe_column(
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
    column: list[str] = typer.Option(
        ...,
        "--column",
        help="Column description as 'NAME=DESCRIPTION' (can be repeated)",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID (defaults to active branch if set via 'branch use')",
    ),
) -> None:
    """Set descriptions on one or more columns of a storage table.

    Descriptions are stored as KBC.column.{name}.description keys in table
    metadata (upsert).  Keboola Storage does not expose a user-writable
    column-level metadata endpoint; this convention lets you annotate columns
    and read them back via 'storage table-detail'.

    Example:

        kbagent storage describe-column \\
            --project myproj \\
            --table-id in.c-bucket.orders \\
            --column order_id="Unique order identifier" \\
            --column total="Order total in USD"
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    parsed: dict[str, str] = {}
    for entry in column:
        if "=" not in entry:
            formatter.error(
                message=f"--column must be NAME=DESCRIPTION, got: {entry!r}",
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
            raise typer.Exit(code=2) from None
        name, _, desc = entry.partition("=")
        name = name.strip()
        if not name:
            formatter.error(
                message=f"Column name cannot be empty in: {entry!r}",
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
            raise typer.Exit(code=2) from None
        parsed[name] = desc

    try:
        result = service.describe_columns(
            alias=project,
            table_id=table_id,
            columns=parsed,
            branch_id=effective_branch,
        )
    except ValueError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.INVALID_ARGUMENT)
        raise typer.Exit(code=2) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        formatter.console.print(
            f"[bold green]Column descriptions set:[/bold green] {table_id} "
            f"({len(parsed)} column(s))"
        )
        for name, desc in parsed.items():
            formatter.console.print(f"  {name}: {desc[:80]}")


@storage_app.command("describe-batch", rich_help_panel=_DESCRIBE)
def storage_describe_batch(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    from_file: Path = typer.Option(
        ...,
        "--from-file",
        help="Path to a YAML file with bucket/table/column descriptions",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID (defaults to active branch if set via 'branch use')",
    ),
) -> None:
    """Apply descriptions to buckets, tables, and columns from a YAML file.

    YAML schema:

        buckets:
          in.c-my-bucket: "Bucket description"

        tables:
          in.c-my-bucket.my-table: "Table description"

        columns:
          in.c-my-bucket.my-table:
            col1: "Column 1 description"
            col2: "Column 2 description"

    All sections are optional.  A failure in one item does not abort the
    rest -- all results are collected and reported.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    config_store: ConfigStore = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    # In human mode, show a live progress indicator so that large batches
    # (100+ items) do not look frozen. JSON mode must remain silent on stderr
    # so structured output is the only thing on stdout.
    progress_cm: Any = None
    progress_task: Any = None
    progress_callback = None
    if not formatter.json_mode:
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
            TimeElapsedColumn,
        )

        progress_cm = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("•"),
            TimeElapsedColumn(),
            console=formatter.console,
            transient=True,
        )

        def _on_item(obj_type: str, obj_id: str, current: int, total: int) -> None:
            # Guard against progress_task/progress_cm not being ready yet.
            if progress_task is None or progress_cm is None:
                return
            # total is known up-front (passed the first time), but re-setting
            # is a no-op after the first call.
            progress_cm.update(
                progress_task,
                total=total,
                completed=max(current - 1, 0),
                description=f"Describing {obj_type} {obj_id}",
            )

        progress_callback = _on_item

    try:
        if progress_cm is not None:
            progress_cm.start()
            progress_task = progress_cm.add_task("Applying descriptions...", total=None)
        result = service.describe_batch(
            alias=project,
            from_file=from_file,
            branch_id=effective_branch,
            progress_callback=progress_callback,
        )
        if progress_cm is not None and progress_task is not None:
            # Mark the task complete so the final render shows N / N.
            progress_cm.update(
                progress_task,
                completed=result["applied_count"] + result["error_count"],
            )
    except ValueError as exc:
        formatter.error(message=str(exc), error_code=ErrorCode.INVALID_ARGUMENT)
        raise typer.Exit(code=2) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    finally:
        if progress_cm is not None:
            # .stop() is idempotent; safe for both happy and error paths.
            progress_cm.stop()

    if formatter.json_mode:
        formatter.output(result)
    else:
        applied = result["applied_count"]
        errors = result["error_count"]
        formatter.console.print(
            f"[bold green]Batch complete:[/bold green] {applied} applied, {errors} error(s)"
        )
        for item in result["applied"]:
            obj_type = item["type"]
            obj_id = item["id"]
            if obj_type == "columns":
                n = len(item.get("columns", {}))
                formatter.console.print(f"  [green]✓[/green] {obj_type} {obj_id} ({n} cols)")
            else:
                formatter.console.print(f"  [green]✓[/green] {obj_type} {obj_id}")
        for item in result["errors"]:
            formatter.console.print(f"  [red]✗[/red] {item['type']} {item['id']}: {item['error']}")
        if errors:
            raise typer.Exit(code=1) from None


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
    # Read command: ignore implicit active dev branch (empty listing trap).
    _, effective_branch = resolve_branch(
        config_store, formatter, project, branch, ignore_active_branch=True
    )

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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
        formatter.error(message=f"File not found: {file}", error_code=ErrorCode.FILE_NOT_FOUND)
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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
            error_code=ErrorCode.INVALID_ARGUMENT,
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
        formatter.error(message=str(exc), error_code=ErrorCode.INVALID_ARGUMENT)
        raise typer.Exit(code=2) from None
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
            error_code=ErrorCode.INVALID_ARGUMENT,
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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
    file_type: str = typer.Option(
        "csv",
        "--file-type",
        help="Output format: 'csv' (default) or 'parquet'. Parquet output is "
        "always sliced; with --download each slice is saved as its own file "
        "under --output (treated as a directory).",
    ),
    keep_slices: bool = typer.Option(
        False,
        "--keep-slices",
        help=(
            "CSV-only with --download: write each slice as its own file under "
            "--output (treated as a directory) instead of concatenating into a "
            "single CSV. Mirrors the parquet download layout. Ignored for "
            "parquet (always sliced) and for non-sliced exports."
        ),
    ),
) -> None:
    """Export a table to a Storage File.

    Creates a file in Storage that can be downloaded or consumed by other
    components. Use --tag to tag the output file and --download to also
    save it locally. Use --file-type parquet to export as Parquet (sliced;
    --download produces a directory with per-slice .parquet files and a
    _manifest.json sidecar).

    Default parquet download layout: ./{project}/{table_id}.parquet/
    Override with --output DIR to choose a different location.
    """
    if file_type not in ("csv", "parquet"):
        formatter = get_formatter(ctx)
        formatter.error(
            message=f"--file-type must be 'csv' or 'parquet', got {file_type!r}",
            error_code=ErrorCode.VALIDATION_ERROR,
        )
        raise typer.Exit(code=2) from None
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
            file_type=file_type,
            keep_slices=keep_slices,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
            f"file ID {result['file_id']} ({size_str}, {result.get('file_type', 'csv')})"
        )
        if tags_str:
            formatter.console.print(f"  Tags: {tags_str}")
        if result.get("downloaded"):
            dl_size = _format_file_size(result.get("downloaded_bytes"))
            slice_count = result.get("slice_count")
            suffix = f", {slice_count} slices" if slice_count else ""
            formatter.console.print(f"  Downloaded to: {result['output_path']} ({dl_size}{suffix})")
