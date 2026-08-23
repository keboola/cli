"""``kbagent config restore`` + ``config trash-list`` -- the undo side of delete.

``config delete`` is a soft delete into the Storage trash; these two commands
make that reversible from the CLI. Lives in a private module because
``commands/config.py`` is over its size ceiling (``make loc-check``); mounted
onto ``config_app`` via :func:`register`, so the permission keys stay
``config.restore`` / ``config.trash-list``.
"""

from __future__ import annotations

import typer
from rich.markup import escape

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._helpers import get_formatter, get_service, map_error_to_exit_code


def register(app: typer.Typer) -> None:
    """Mount restore + trash-list onto ``app`` (the ``config`` Typer group)."""

    @app.command("restore", rich_help_panel="Lifecycle")
    def config_restore(
        ctx: typer.Context,
        project: str = typer.Option(..., "--project", help="Project alias"),
        component_id: str = typer.Option(
            ..., "--component-id", help="Component ID (e.g. keboola.snowflake-transformation)"
        ),
        config_id: str = typer.Option(..., "--config-id", help="Trashed configuration ID"),
        branch: int | None = typer.Option(
            None, "--branch", help="Restore in a specific dev branch ID (defaults to active branch)"
        ),
    ) -> None:
        """Restore a configuration from the trash (undo of 'config delete').

        Only a trashed configuration can be restored; find candidates with
        'config trash-list'. Restoring brings back the configuration with its
        versions, rows and metadata.
        """
        formatter = get_formatter(ctx)
        service = get_service(ctx, "config_service")
        try:
            result = service.restore_config(
                alias=project,
                component_id=component_id,
                config_id=config_id,
                branch_id=branch,
            )
        except ConfigError as exc:
            formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
            raise typer.Exit(code=5) from None
        except KeboolaApiError as exc:
            formatter.error(message=exc.message, error_code=exc.error_code)
            raise typer.Exit(code=map_error_to_exit_code(exc)) from None

        if formatter.json_mode:
            formatter.output(result)
        else:
            formatter.success(
                f"Restored config {result['component_id']}/{result['config_id']} "
                f"('{result.get('name')}', version {result.get('version')}) "
                f"in project '{result['project_alias']}'"
            )

    @app.command("trash-list", rich_help_panel="Lifecycle")
    def config_trash_list(
        ctx: typer.Context,
        project: str = typer.Option(..., "--project", help="Project alias"),
        component_id: str | None = typer.Option(
            None, "--component-id", help="Limit to one component's trashed configurations"
        ),
        branch: int | None = typer.Option(
            None, "--branch", help="List a specific dev branch's trash (defaults to active branch)"
        ),
    ) -> None:
        """List configurations in the trash (restorable via 'config restore')."""
        formatter = get_formatter(ctx)
        service = get_service(ctx, "config_service")
        try:
            result = service.list_config_trash(
                alias=project,
                component_id=component_id,
                branch_id=branch,
            )
        except ConfigError as exc:
            formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
            raise typer.Exit(code=5) from None
        except KeboolaApiError as exc:
            formatter.error(message=exc.message, error_code=exc.error_code)
            raise typer.Exit(code=map_error_to_exit_code(exc)) from None

        if formatter.json_mode:
            formatter.output(result)
            return
        entries = result["trash"]
        if not entries:
            formatter.console.print("Trash is empty.")
            return
        from rich.table import Table

        table = Table(title=f"Trashed configurations -- {escape(project)} ({len(entries)})")
        table.add_column("Component", style="dim")
        table.add_column("Config ID", style="bold cyan")
        table.add_column("Name")
        table.add_column("Version", justify="right", style="dim")
        table.add_column("Deleted at", style="dim")
        for e in entries:
            table.add_row(
                str(e.get("component_id") or ""),
                str(e.get("config_id") or ""),
                escape(str(e.get("name") or "")),
                str(e.get("version") or ""),
                str(e.get("deleted_at") or ""),
            )
        formatter.console.print(table)
        formatter.console.print(
            "[dim]Restore with: kbagent config restore --project "
            f"{escape(project)} --component-id <component> --config-id <id>[/dim]"
        )
