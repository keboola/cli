"""Storage import jobs in the ``kbagent storage`` group (issue #834).

- ``storage job-detail`` -- report (and optionally wait for) one Storage job,
  typically the import a ``upload-table --no-wait`` or a timed-out upload
  left running server-side.
- The ``--wait/--no-wait`` + ``--timeout`` options and the human-mode
  rendering ``upload-table`` and ``load-file`` share, so the two commands
  cannot drift apart.

Lives in a private module because ``commands/storage.py`` is past the
commands-file size ceiling (CONTRIBUTING.md). Mounted flat onto
``storage_app`` via :func:`register`, so the permission key stays
``storage.job-detail``.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer
from rich.markup import escape

from ..constants import IMPORT_JOB_MAX_WAIT
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..output import OutputFormatter
from ._helpers import get_formatter, get_service, map_error_to_exit_code

_JOBS = "Jobs"

ImportWaitOption = Annotated[
    bool,
    typer.Option(
        "--wait/--no-wait",
        help=(
            "Wait for the Storage import job to finish (default). --no-wait returns as "
            "soon as the job is queued and prints its job ID; follow it with "
            "`storage job-detail --wait`. The upload of the file itself is always awaited."
        ),
    ),
]

ImportTimeoutOption = Annotated[
    float | None,
    typer.Option(
        "--timeout",
        help=(
            f"Seconds to wait for the import job (default: {IMPORT_JOB_MAX_WAIT:g}). "
            "On timeout the import keeps running server-side; the error names its job ID."
        ),
    ),
]


def print_import_outcome(formatter: OutputFormatter, project: str, result: dict[str, Any]) -> None:
    """Human-mode lines for an import result: rows, or how to follow the job."""
    if result.get("imported_rows") is not None:
        formatter.console.print(f"  Rows imported: {result['imported_rows']}")
    elif result.get("job_status") not in (None, "success"):
        job_id = result.get("job_id")
        formatter.console.print(
            f"  Import job [cyan]{job_id}[/cyan] queued (status {result['job_status']}). "
            f"Follow it with: kbagent storage job-detail --project {project} "
            f"--job-id {job_id} --wait"
        )
    for warning in result.get("warnings") or []:
        formatter.console.print(f"  [yellow]Warning:[/yellow] {warning}")


def _render_job(formatter: OutputFormatter, job: dict[str, Any]) -> None:
    """Rich table of one ``storage_job_detail`` result."""
    from rich.table import Table

    table = Table(title=f"Storage job {job['job_id']}", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    error = job.get("error") or {}
    rows = [
        ("Status", job.get("status")),
        ("Operation", job.get("operation_name")),
        ("Table", job.get("table_id")),
        ("File", job.get("file_id")),
        ("Rows imported", job.get("imported_rows")),
        ("Created", job.get("created_time")),
        ("Started", job.get("start_time")),
        ("Ended", job.get("end_time")),
        ("Error", error.get("message")),
    ]
    for label, value in rows:
        if value is not None:
            table.add_row(label, escape(str(value)))
    formatter.console.print(table)
    for warning in job.get("warnings") or []:
        formatter.console.print(f"  [yellow]Warning:[/yellow] {escape(str(warning))}")


def register(app: typer.Typer) -> None:
    """Mount ``job-detail`` onto ``app`` (the ``storage`` Typer group)."""

    @app.command("job-detail", rich_help_panel=_JOBS)
    def storage_job_detail(
        ctx: typer.Context,
        project: str = typer.Option(..., "--project", help="Project alias"),
        job_id: int = typer.Option(
            ..., "--job-id", help="Storage job ID (e.g. from `upload-table --no-wait`)"
        ),
        wait: bool = typer.Option(
            False, "--wait", help="Poll until the job finishes (success or error)"
        ),
        timeout: float | None = typer.Option(
            None,
            "--timeout",
            help=f"Seconds to wait; requires --wait (default: {IMPORT_JOB_MAX_WAIT:g})",
        ),
    ) -> None:
        """Show a Storage job -- status, table, rows imported, timing, error.

        Follows an import that `upload-table --no-wait` queued or that outlived
        the upload's own wait. Storage job IDs are project-wide, so no branch
        is needed. Exit code 0 while the job is queued, running or succeeded;
        1 when it failed (the job details stay in the --json error `details`);
        4 when --wait runs out of time (the job keeps running -- run it again).
        """
        formatter = get_formatter(ctx)
        service = get_service(ctx, "storage_service")
        try:
            result = service.storage_job_detail(
                alias=project, job_id=job_id, wait=wait, timeout=timeout
            )
        except ValueError as exc:
            formatter.error(message=str(exc), error_code=ErrorCode.INVALID_ARGUMENT)
            raise typer.Exit(code=2) from None
        except ConfigError as exc:
            formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
            raise typer.Exit(code=5) from None
        except KeboolaApiError as exc:
            formatter.error(
                message=exc.message,
                error_code=exc.error_code,
                retryable=exc.retryable,
                details=exc.details,
            )
            raise typer.Exit(code=map_error_to_exit_code(exc)) from None

        if result["status"] == "error":
            if not formatter.json_mode:
                _render_job(formatter, result)
            message = (result["error"] or {}).get("message") or "Storage job failed"
            formatter.error(
                message=f"Storage job {job_id} failed: {message}",
                error_code=ErrorCode.STORAGE_JOB_FAILED,
                details=result,
            )
            raise typer.Exit(code=1)

        if formatter.json_mode:
            formatter.output(result)
        else:
            _render_job(formatter, result)
