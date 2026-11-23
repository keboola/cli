"""``kbagent storage clustering-set``: in-place BigQuery clustering change.

Lives in a private module because ``commands/storage.py`` is past the
commands-file size ceiling (CONTRIBUTING.md). Mounted flat onto
``storage_app`` via :func:`register`, so the permission key stays
``storage.clustering-set``.
"""

from typing import Any

import typer
from rich.markup import escape

from ..config_store import ConfigStore
from ..constants import STORAGE_JOB_MAX_WAIT
from ..effective_branch import resolve_branch
from ..errors import ErrorCode
from ..output import OutputFormatter
from ..services._storage_jobs import validate_wait_timeout
from ._helpers import get_formatter, get_service, handle_service_call
from ._project_ref import PROJECT_HELP

_TABLES = "Tables"


def _fields(fields: list[str] | None) -> str:
    return ", ".join(escape(field) for field in fields) if fields else "none"


def _render(formatter: OutputFormatter, result: dict[str, Any]) -> None:
    table = escape(result["table_id"])
    before = _fields(result["clustering_before"])
    clustering = result["clustering"]
    if result.get("dry_run"):
        if not result["would_change"]:
            formatter.success(f"No change: clustering of {table} is already {before}.")
        elif clustering is None:
            formatter.console.print(f"Would remove clustering of {table} (now: {before})")
        else:
            formatter.console.print(
                f"Would set clustering of {table}: {_fields(clustering)} (now: {before})"
            )
        return
    if result["status"] == "unchanged":
        formatter.success(f"No change: clustering of {table} is already {before}.")
        return
    job = result["job_id"]
    if clustering is None:
        formatter.success(f"Clustering removed: {table} (job {job})")
    else:
        formatter.success(f"Clustering set: {table} -> {_fields(clustering)} (job {job})")
    formatter.console.print(
        "[dim]BigQuery clusters data written after the change; "
        "existing rows keep their order until rewritten.[/dim]"
    )


def register(app: typer.Typer) -> None:
    """Mount ``clustering-set`` onto ``app`` (the ``storage`` Typer group)."""

    @app.command("clustering-set", rich_help_panel=_TABLES)
    def storage_clustering_set(
        ctx: typer.Context,
        project: str = typer.Option(None, "--project", "-p", help=PROJECT_HELP),
        table_id: str = typer.Option(
            ...,
            "--table-id",
            help="Table ID (e.g. 'in.c-bucket.table')",
        ),
        clustering_fields: list[str] | None = typer.Option(
            None,
            "--clustering-field",
            help=(
                "Column to cluster by, in order (BigQuery, 1 to 4). Repeatable. "
                "Replaces the current clustering."
            ),
        ),
        clear: bool = typer.Option(
            False,
            "--clear",
            help="Remove the clustering. Mutually exclusive with --clustering-field.",
        ),
        branch: int | None = typer.Option(
            None,
            "--branch",
            help="Dev branch ID (defaults to active branch; 0 is production)",
        ),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="Show the current and the requested clustering without writing",
        ),
        timeout: float = typer.Option(
            STORAGE_JOB_MAX_WAIT,
            "--timeout",
            help=(
                f"Seconds to wait for the storage job (default {STORAGE_JOB_MAX_WAIT:g}). "
                "On timeout the job KEEPS RUNNING server-side -- kbagent stops watching, "
                "it does not cancel."
            ),
        ),
    ) -> None:
        """Set or remove the clustering of a BigQuery table in place (waits for the job).

        No copy and no swap: Storage changes the clustering of the existing
        table. Typed BigQuery tables only. BigQuery applies the new clustering
        to data written after the change; existing rows keep their order until
        they are rewritten (for example by a full load). Verify with
        'storage table-detail'. The result reports status 'updated', or
        'unchanged' when the table already has the requested clustering.

        \b
        Examples:
          kbagent storage clustering-set --project P --table-id in.c-b.t \\
            --clustering-field country --clustering-field city
          kbagent storage clustering-set --project P --table-id in.c-b.t --clear
          kbagent storage clustering-set --project P --table-id in.c-b.t \\
            --clustering-field country --dry-run
        """
        formatter = get_formatter(ctx)
        service = get_service(ctx, "storage_service")
        config_store: ConfigStore = ctx.obj["config_store"]
        effective_branch = resolve_branch(config_store, project, branch)

        try:
            validate_wait_timeout(timeout)
        except ValueError as exc:
            formatter.error(message=str(exc), error_code=ErrorCode.INVALID_ARGUMENT)
            raise typer.Exit(code=2) from None

        result = handle_service_call(
            ctx,
            service.set_table_clustering,
            alias=project,
            table_id=table_id,
            fields=clustering_fields,
            clear=clear,
            branch_id=effective_branch,
            dry_run=dry_run,
            timeout=timeout,
        )
        if formatter.json_mode:
            formatter.output(result)
        else:
            _render(formatter, result)
