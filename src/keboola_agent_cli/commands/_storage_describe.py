"""Description (metadata write) commands for the ``kbagent storage`` group.

``describe-bucket`` / ``describe-table`` / ``describe-column`` /
``describe-batch`` / ``describe-migrate`` -- thin CLI layer over
:class:`services.storage_service.StorageService`.

Lives in a private module because ``commands/storage.py`` is already past the
commands-file ceiling (CONTRIBUTING.md). The commands are mounted flat onto
``storage_app`` via :func:`register`, so permission keys stay in the
``storage.*`` namespace and ``kbagent storage --help`` lists them together in
the "Descriptions" panel.
"""

from pathlib import Path
from typing import Any

import typer
from rich.markup import escape

from ..config_store import ConfigStore
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._helpers import (
    get_formatter,
    get_service,
    map_error_to_exit_code,
    resolve_branch,
)

_DESCRIBE = "Descriptions"


def register(app: typer.Typer) -> None:
    """Mount the describe commands onto ``app`` (the ``storage`` Typer group)."""

    @app.command("describe-bucket", rich_help_panel=_DESCRIBE)
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
            formatter.console.print(f"  {escape(description[:120])}")

    @app.command("describe-table", rich_help_panel=_DESCRIBE)
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
            formatter.console.print(f"  {escape(description[:120])}")

    @app.command("describe-column", rich_help_panel=_DESCRIBE)
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

        Writes through the native table-definition endpoint -- the same one the
        Keboola UI uses -- so the text is visible in the UI, to the MCP server,
        and as the backend column comment (Snowflake COMMENT / BigQuery
        description).  The write also marks the descriptions as user-authored so
        the next component run's Output Mapping cannot overwrite them.

        Unknown column names are rejected before anything is written.  Legacy
        KBC.column.*.description metadata keys found on the same table (written by
        kbagent before 0.88.0) are migrated in the same write and removed; see
        'storage describe-migrate' to convert a whole bucket or project.

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
                formatter.console.print(f"  {name}: {escape(desc[:80])}")
            migrated = result.get("migrated") or {}
            if migrated:
                formatter.console.print(
                    f"  [dim]Migrated {len(migrated)} legacy column description(s): "
                    f"{', '.join(sorted(migrated))}[/dim]"
                )
            for item in result.get("skipped") or []:
                formatter.console.print(
                    f"  [yellow]Skipped[/yellow] {item['column']} ({item['reason']})"
                )

    @app.command("describe-batch", rich_help_panel=_DESCRIBE)
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

        All sections are optional (absent or empty sections are skipped).

        A malformed file -- a section that is not a mapping of ID to
        description, a null description, a non-mapping columns entry -- is
        rejected before any write (INVALID_ARGUMENT, exit 2), naming the
        offending key.  Once application starts, a per-item API failure does
        not abort the rest: those results are collected and reported, and the
        command exits 1 if any item failed.
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
                formatter.console.print(
                    f"  [red]✗[/red] {item['type']} {item['id']}: {item['error']}"
                )
            if errors:
                raise typer.Exit(code=1) from None

    @app.command("describe-migrate", rich_help_panel=_DESCRIBE)
    def storage_describe_migrate(
        ctx: typer.Context,
        project: str = typer.Option(
            ...,
            "--project",
            help="Project alias",
        ),
        table_id: list[str] | None = typer.Option(
            None,
            "--table-id",
            help="Migrate only this table (can be repeated). Excludes --bucket-id.",
        ),
        bucket_id: str | None = typer.Option(
            None,
            "--bucket-id",
            help="Migrate every table of this bucket. Excludes --table-id.",
        ),
        prune_orphans: bool = typer.Option(
            False,
            "--prune-orphans",
            help="Also delete legacy entries for columns that no longer exist",
        ),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="Show what would be migrated without writing",
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
        """Convert legacy KBC.column.* descriptions to the native definition endpoint.

        Before 0.88.0 kbagent stored column descriptions as flat
        KBC.column.{name}.description table-metadata keys, which nothing but
        kbagent itself ever read.  This command rewrites them through the native
        endpoint (visible in the Keboola UI, to the MCP server, and as backend
        column comments) and removes the flat keys afterwards.

        Scope defaults to the whole project; narrow it with --table-id (repeatable)
        or --bucket-id.  A column that already shows a different description keeps
        it (reported as a conflict), and a key whose column no longer exists is
        reported as an orphan -- deleted only with --prune-orphans.
        """
        formatter = get_formatter(ctx)
        service = get_service(ctx, "storage_service")
        config_store: ConfigStore = ctx.obj["config_store"]
        _, effective_branch = resolve_branch(config_store, formatter, project, branch)

        if table_id and bucket_id:
            formatter.error(
                message="--table-id and --bucket-id are mutually exclusive.",
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
            raise typer.Exit(code=2) from None

        def _migrate(scan_only: bool) -> dict[str, Any]:
            try:
                return service.describe_migrate(
                    alias=project,
                    table_ids=list(table_id) if table_id else None,
                    bucket_id=bucket_id,
                    prune_orphans=prune_orphans,
                    dry_run=scan_only,
                    branch_id=effective_branch,
                )
            except ValueError as exc:
                formatter.error(message=str(exc), error_code=ErrorCode.INVALID_ARGUMENT)
                raise typer.Exit(code=2) from None
            except ConfigError as exc:
                formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
                raise typer.Exit(code=5) from None
            except KeboolaApiError as exc:
                formatter.error(
                    message=exc.message, error_code=exc.error_code, retryable=exc.retryable
                )
                raise typer.Exit(code=map_error_to_exit_code(exc)) from None

        if not dry_run and not yes and not formatter.json_mode:
            # Scan first so the confirmation states what is actually at stake --
            # the repo's dry-run-then-confirm pattern for bulk write operations.
            scan = _migrate(True)
            column_count = sum(len(item["columns"]) for item in scan["migrated"])
            formatter.console.print(
                f"[bold]Scan:[/bold] {len(scan['migrated'])} table(s) with "
                f"{column_count} legacy column description(s) "
                f"of {scan['tables_scanned']} table(s) scanned."
            )
            if not typer.confirm(
                f"Migrate {len(scan['migrated'])} table(s) in project '{project}'?"
            ):
                formatter.console.print("Aborted.")
                raise typer.Exit(code=0)

        result = _migrate(dry_run)

        if formatter.json_mode:
            formatter.output(result)
        else:
            verb = "Would migrate" if result["dry_run"] else "Migrated"
            formatter.console.print(
                f"[bold green]{verb}:[/bold green] {len(result['migrated'])} table(s) of "
                f"{result['tables_scanned']} scanned"
            )
            for item in result["migrated"]:
                cols = ", ".join(sorted(item["columns"]))
                formatter.console.print(f"  [green]✓[/green] {item['table_id']}: {escape(cols)}")
            for item in result["skipped"]:
                formatter.console.print(
                    f"  [yellow]Skipped[/yellow] {item['table_id']}.{item['column']} ({item['reason']})"
                )
            for item in result["pruned_orphans"]:
                formatter.console.print(
                    f"  [dim]Pruned orphan {item['table_id']}.{item['column']}[/dim]"
                )
            for item in result["errors"]:
                formatter.console.print(f"  [red]✗[/red] {item['table_id']}: {item['error']}")

        if result["errors"]:
            raise typer.Exit(code=1) from None
