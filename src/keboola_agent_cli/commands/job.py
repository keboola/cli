"""Job browsing commands - list job history from Queue API.

Thin CLI layer: parses arguments, calls JobService, formats output.
No business logic belongs here.
"""

import typer

from ..constants import DEFAULT_JOB_LIMIT, MAX_JOB_LIMIT
from ..errors import ConfigError, KeboolaApiError
from ..output import OutputFormatter, format_job_detail, format_jobs_table
from ..services.job_service import JobService

job_app = typer.Typer(help="Browse job history")

VALID_STATUSES = ["processing", "terminated", "cancelled", "success", "error"]


def _get_formatter(ctx: typer.Context) -> OutputFormatter:
    """Retrieve the OutputFormatter from the Typer context."""
    return ctx.obj["formatter"]


def _get_service(ctx: typer.Context) -> JobService:
    """Retrieve the JobService from the Typer context."""
    return ctx.obj["job_service"]


@job_app.command("list")
def job_list(
    ctx: typer.Context,
    project: list[str] | None = typer.Option(
        None,
        "--project",
        help="Project alias to query (can be repeated for multiple projects)",
    ),
    component_id: str | None = typer.Option(
        None,
        "--component-id",
        help="Filter by component ID (e.g. keboola.ex-db-snowflake)",
    ),
    config_id: str | None = typer.Option(
        None,
        "--config-id",
        help="Filter by configuration ID (requires --component-id)",
    ),
    status: str | None = typer.Option(
        None,
        "--status",
        help="Filter by job status: processing, terminated, cancelled, success, error",
    ),
    limit: int = typer.Option(
        DEFAULT_JOB_LIMIT,
        "--limit",
        help=f"Maximum number of jobs to return per project (1-{MAX_JOB_LIMIT})",
    ),
) -> None:
    """List jobs from connected projects."""
    formatter = _get_formatter(ctx)
    service = _get_service(ctx)

    # Validate status
    if status and status not in VALID_STATUSES:
        formatter.error(
            message=f"Invalid status '{status}'. Valid statuses: {', '.join(VALID_STATUSES)}",
            error_code="INVALID_ARGUMENT",
        )
        raise typer.Exit(code=2)

    # Validate limit range
    if limit < 1 or limit > MAX_JOB_LIMIT:
        formatter.error(
            message=f"Invalid limit {limit}. Must be between 1 and {MAX_JOB_LIMIT}.",
            error_code="INVALID_ARGUMENT",
        )
        raise typer.Exit(code=2)

    # Validate config_id requires component_id
    if config_id and not component_id:
        formatter.error(
            message="--config-id requires --component-id to be specified.",
            error_code="INVALID_ARGUMENT",
        )
        raise typer.Exit(code=2)

    try:
        result = service.list_jobs(
            aliases=project,
            component_id=component_id,
            config_id=config_id,
            status=status,
            limit=limit,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        format_jobs_table(formatter.console, result)

        for err in result.get("errors", []):
            formatter.warning(f"Project '{err['project_alias']}': {err['message']}")


@job_app.command("detail")
def job_detail(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    job_id: str = typer.Option(..., "--job-id", help="Job ID"),
) -> None:
    """Show detailed information about a specific job."""
    formatter = _get_formatter(ctx)
    service = _get_service(ctx)

    try:
        result = service.get_job_detail(alias=project, job_id=job_id)
        formatter.output(result, format_job_detail)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        if exc.error_code == "INVALID_TOKEN":
            exit_code = 3
        elif exc.error_code in ("TIMEOUT", "CONNECTION_ERROR", "RETRY_EXHAUSTED"):
            exit_code = 4
        else:
            exit_code = 1
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            project=project,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=exit_code) from None
