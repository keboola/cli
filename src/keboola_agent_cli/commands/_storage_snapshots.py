"""Table snapshot commands for the ``kbagent storage`` group (issue #512).

Thin CLI layer over :class:`services.snapshot_service.SnapshotService`:
``snapshots`` (list), ``snapshot-create``, ``snapshot-detail``,
``snapshot-delete``, and ``table-from-snapshot`` (create a NEW table from an
existing snapshot -- the core ask of #512).

Lives in a private module because ``commands/storage.py`` is already past the
1,200-LOC commands-file ceiling (CONTRIBUTING.md). The commands are mounted
flat onto ``storage_app`` via :func:`register`, so permission keys stay in the
``storage.*`` namespace and ``kbagent storage --help`` lists them alongside
the other table commands.
"""

from __future__ import annotations

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

_SNAPSHOTS = "Snapshots"


def _handle_errors(formatter: Any, exc: Exception) -> None:
    """Map ConfigError / KeboolaApiError to a structured error + Exit."""
    if isinstance(exc, ConfigError):
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    if isinstance(exc, KeboolaApiError):
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    raise exc


def _snapshot_created(snapshot: dict[str, Any]) -> str:
    """Best-effort creation timestamp from a raw API snapshot dict."""
    return str(snapshot.get("created") or snapshot.get("createdTime") or "")


def register(app: typer.Typer) -> None:
    """Mount the snapshot commands onto ``app`` (the ``storage`` Typer group)."""

    @app.command("snapshots", rich_help_panel=_SNAPSHOTS)
    def storage_snapshots(
        ctx: typer.Context,
        project: str = typer.Option(..., "--project", help="Project alias"),
        table_id: str = typer.Option(
            ..., "--table-id", help="Table ID (e.g. 'in.c-my-bucket.my-table')"
        ),
        limit: int | None = typer.Option(None, "--limit", help="Max snapshots to return"),
        branch: int | None = typer.Option(
            None, "--branch", help="Dev branch ID (production endpoint by default)"
        ),
    ) -> None:
        """List snapshots of a table.

        Each snapshot captures the table's data, columns, and primary key at
        a point in time. Use the snapshot ID with `storage table-from-snapshot`
        to restore it as a new table.
        """
        formatter = get_formatter(ctx)
        service = get_service(ctx, "snapshot_service")
        config_store: ConfigStore = ctx.obj["config_store"]
        # Read command: ignore implicit active dev branch (empty listing trap).
        _, effective_branch = resolve_branch(
            config_store, formatter, project, branch, ignore_active_branch=True
        )

        try:
            result = service.list_snapshots(
                alias=project,
                table_id=table_id,
                limit=limit,
                branch_id=effective_branch,
            )
        except (ConfigError, KeboolaApiError) as exc:
            _handle_errors(formatter, exc)

        if formatter.json_mode:
            formatter.output(result)
            return

        from rich.table import Table

        rich_table = Table(title=f"Snapshots - {result['table_id']} ({result['count']})")
        rich_table.add_column("ID", style="cyan")
        rich_table.add_column("Created")
        rich_table.add_column("Description")
        for snap in result["snapshots"]:
            rich_table.add_row(
                str(snap.get("id", "")),
                _snapshot_created(snap),
                escape(str(snap.get("description") or "")),
            )
        formatter.console.print(rich_table)
        if result["count"]:
            formatter.console.print(
                "[dim]Restore one as a new table with:[/dim] "
                f"kbagent storage table-from-snapshot --project {result['project_alias']} "
                "--snapshot-id <ID> --bucket-id <BUCKET> --name <NEW_TABLE>"
            )

    @app.command("snapshot-create", rich_help_panel=_SNAPSHOTS)
    def storage_snapshot_create(
        ctx: typer.Context,
        project: str = typer.Option(..., "--project", help="Project alias"),
        table_id: str = typer.Option(
            ..., "--table-id", help="Table ID to snapshot (e.g. 'in.c-my-bucket.my-table')"
        ),
        description: str | None = typer.Option(
            None, "--description", help="Human-readable snapshot description"
        ),
        branch: int | None = typer.Option(
            None,
            "--branch",
            help="Dev branch ID (defaults to active branch if set via 'branch use')",
        ),
    ) -> None:
        """Create a snapshot of a table (data + columns + primary key).

        The snapshot is a point-in-time backup stored by the platform. Restore
        it later as a NEW table with `storage table-from-snapshot`.
        """
        formatter = get_formatter(ctx)
        service = get_service(ctx, "snapshot_service")
        config_store: ConfigStore = ctx.obj["config_store"]
        _, effective_branch = resolve_branch(config_store, formatter, project, branch)

        try:
            result = service.create_snapshot(
                alias=project,
                table_id=table_id,
                description=description,
                branch_id=effective_branch,
            )
        except (ConfigError, KeboolaApiError) as exc:
            _handle_errors(formatter, exc)

        if formatter.json_mode:
            formatter.output(result)
            return
        formatter.console.print(
            f"[bold green]Snapshot created:[/bold green] ID {result['snapshot_id']} "
            f"(table {result['table_id']})"
        )
        formatter.console.print(
            "[dim]Restore as a new table with:[/dim] "
            f"kbagent storage table-from-snapshot --project {result['project_alias']} "
            f"--snapshot-id {result['snapshot_id']} --bucket-id <BUCKET> --name <NEW_TABLE>"
        )

    @app.command("snapshot-detail", rich_help_panel=_SNAPSHOTS)
    def storage_snapshot_detail(
        ctx: typer.Context,
        project: str = typer.Option(..., "--project", help="Project alias"),
        snapshot_id: str = typer.Option(..., "--snapshot-id", help="Snapshot ID"),
    ) -> None:
        """Show one snapshot's detail (source table, creation time, description)."""
        formatter = get_formatter(ctx)
        service = get_service(ctx, "snapshot_service")

        try:
            result = service.get_snapshot(alias=project, snapshot_id=snapshot_id)
        except (ConfigError, KeboolaApiError) as exc:
            _handle_errors(formatter, exc)

        if formatter.json_mode:
            formatter.output(result)
            return
        snap = result["snapshot"]
        source_table = snap.get("table") or {}
        formatter.console.print(f"[bold]Snapshot:[/bold] {snap.get('id', '')}")
        formatter.console.print(f"  Created: {_snapshot_created(snap)}")
        formatter.console.print(f"  Description: {escape(str(snap.get('description') or ''))}")
        if source_table:
            formatter.console.print(f"  Source table: {source_table.get('id', '')}")

    @app.command("snapshot-delete", rich_help_panel=_SNAPSHOTS)
    def storage_snapshot_delete(
        ctx: typer.Context,
        project: str = typer.Option(..., "--project", help="Project alias"),
        snapshot_id: list[str] = typer.Option(
            ..., "--snapshot-id", help="Snapshot ID to delete (repeat for multiple)"
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Show what would be deleted without executing"
        ),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    ) -> None:
        """Delete one or more table snapshots (the source tables are untouched)."""
        formatter = get_formatter(ctx)
        service = get_service(ctx, "snapshot_service")

        if (
            not dry_run
            and not yes
            and not formatter.json_mode
            and not typer.confirm(
                f"Delete {len(snapshot_id)} snapshot(s) in project '{project}'? "
                "Restoring from them will no longer be possible."
            )
        ):
            formatter.console.print("Aborted.")
            raise typer.Exit(code=0)

        try:
            result = service.delete_snapshots(
                alias=project,
                snapshot_ids=snapshot_id,
                dry_run=dry_run,
            )
        except (ConfigError, KeboolaApiError) as exc:
            _handle_errors(formatter, exc)

        if formatter.json_mode:
            formatter.output(result)
        else:
            if dry_run:
                for sid in result.get("would_delete", []):
                    formatter.console.print(f"[bold blue]Would delete:[/bold blue] snapshot {sid}")
            else:
                for sid in result["deleted"]:
                    formatter.console.print(f"[bold green]Deleted:[/bold green] snapshot {sid}")
            for s_err in result["failed"]:
                formatter.console.print(
                    f"[bold red]Failed:[/bold red] snapshot {s_err['id']}: {s_err['error']}"
                )

        if result["failed"]:
            raise typer.Exit(code=1)

    @app.command("table-from-snapshot", rich_help_panel=_SNAPSHOTS)
    def storage_table_from_snapshot(
        ctx: typer.Context,
        project: str = typer.Option(..., "--project", help="Project alias"),
        snapshot_id: str = typer.Option(..., "--snapshot-id", help="Source snapshot ID"),
        bucket_id: str = typer.Option(
            ..., "--bucket-id", help="Destination bucket ID (e.g. 'in.c-my-bucket')"
        ),
        name: str = typer.Option(
            ..., "--name", help="Name for the new table (required by the API)"
        ),
        branch: int | None = typer.Option(
            None,
            "--branch",
            help="Dev branch ID (defaults to active branch if set via 'branch use')",
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Show what would be created without executing"
        ),
    ) -> None:
        """Create a NEW table from an existing snapshot (snapshot restore).

        Restores the snapshot's data, columns, and primary key into a fresh
        table in --bucket-id. The destination bucket must exist; a table with
        the target name must not (the API rejects overwrites). Find snapshot
        IDs with `storage snapshots --table-id ...`.
        """
        formatter = get_formatter(ctx)
        service = get_service(ctx, "snapshot_service")
        config_store: ConfigStore = ctx.obj["config_store"]
        _, effective_branch = resolve_branch(config_store, formatter, project, branch)

        try:
            result = service.create_table_from_snapshot(
                alias=project,
                bucket_id=bucket_id,
                snapshot_id=snapshot_id,
                name=name,
                branch_id=effective_branch,
                dry_run=dry_run,
            )
        except (ConfigError, KeboolaApiError) as exc:
            _handle_errors(formatter, exc)

        if formatter.json_mode:
            formatter.output(result)
            return
        if result["dry_run"]:
            formatter.console.print(
                f"[bold blue]Would create:[/bold blue] table "
                f"'{result['name']}' in bucket "
                f"{result['bucket_id']} from snapshot {result['snapshot_id']}"
            )
            return
        formatter.console.print(
            f"[bold green]Table created:[/bold green] {result['table_id']} "
            f"(from snapshot {result['snapshot_id']})"
        )
