"""Job commands - list history, show detail, and run jobs via Queue API.

Thin CLI layer: parses arguments, calls JobService, formats output.
No business logic belongs here.
"""

import typer

from ..config_store import ConfigStore
from ..constants import (
    DEFAULT_JOB_LIMIT,
    DEFAULT_JOB_RUN_TIMEOUT,
    KILLABLE_JOB_STATUSES,
    MAX_JOB_LIMIT,
    VALID_STATUSES,
)
from ..errors import ConfigError, KeboolaApiError
from ..output import format_job_detail, format_jobs_table
from ._helpers import (
    check_cli_permission,
    emit_hint,
    emit_project_warnings,
    get_formatter,
    get_service,
    map_error_to_exit_code,
    resolve_branch,
    should_hint,
    validate_branch_requires_project,
)

job_app = typer.Typer(help="Browse job history and run jobs")


@job_app.callback(invoke_without_command=True)
def _job_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "job")


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
    if should_hint(ctx):
        emit_hint(
            ctx,
            "job.list",
            project=project,
            component_id=component_id,
            config_id=config_id,
            status=status,
            limit=limit,
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "job_service")

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
        emit_project_warnings(formatter, result)


@job_app.command("detail")
def job_detail(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    job_id: str = typer.Option(..., "--job-id", help="Job ID"),
) -> None:
    """Show detailed information about a specific job."""
    if should_hint(ctx):
        emit_hint(ctx, "job.detail", project=project, job_id=job_id)
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "job_service")

    try:
        result = service.get_job_detail(alias=project, job_id=job_id)
        formatter.output(result, format_job_detail)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
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


@job_app.command("run")
def job_run(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    component_id: str = typer.Option(
        ...,
        "--component-id",
        help="Component ID (e.g. keboola.snowflake-transformation)",
    ),
    config_id: str = typer.Option(
        ...,
        "--config-id",
        help="Configuration ID",
    ),
    row_id: list[str] | None = typer.Option(
        None,
        "--row-id",
        help="Config row ID(s) to run (repeat for multiple; omit to run entire config)",
    ),
    wait: bool = typer.Option(
        False,
        "--wait",
        help="Wait for job to finish (poll until terminal state)",
    ),
    timeout: float = typer.Option(
        DEFAULT_JOB_RUN_TIMEOUT,
        "--timeout",
        help="Max seconds to wait when --wait is set",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID (overrides active branch)",
    ),
    variable_values_id: str | None = typer.Option(
        None,
        "--variable-values-id",
        help=(
            "Explicit keboola.variables values-row ID to bind. Use when the "
            "linked variables config has multiple rows and auto-resolution "
            "(first row) picks the wrong one. Mutually exclusive with "
            "--no-variables."
        ),
    ),
    no_variables: bool = typer.Option(
        False,
        "--no-variables",
        help=(
            "Skip variable-values resolution entirely. Use for components "
            "that do not support variables, or when intentionally running "
            "against empty bindings. Mutually exclusive with "
            "--variable-values-id."
        ),
    ),
) -> None:
    """Run a job for a component configuration.

    Creates a Queue API job and optionally waits for completion.
    Use --row-id to run specific configuration rows.

    When a dev branch is active (via 'branch use'), the job automatically
    runs on that branch. Use --branch to override.

    When the config has linked variables (configuration.variables_id),
    kbagent auto-resolves a variableValuesId so the job binds to the
    deployed values row. Override with --variable-values-id or skip
    with --no-variables.
    """
    if should_hint(ctx):
        emit_hint(
            ctx,
            "job.run",
            project=project,
            component_id=component_id,
            config_id=config_id,
            row_id=row_id,
            wait=wait,
            timeout=timeout,
            branch=branch,
            variable_values_id=variable_values_id,
            no_variables=no_variables,
        )
        return
    formatter = get_formatter(ctx)
    service = get_service(ctx, "job_service")
    config_store: ConfigStore = ctx.obj["config_store"]

    if variable_values_id is not None and not variable_values_id.strip():
        formatter.error(
            message=(
                "--variable-values-id cannot be empty or whitespace. "
                "Pass a row id, or omit the flag to auto-resolve the default row."
            ),
            error_code="INVALID_ARGUMENT",
        )
        raise typer.Exit(code=2)

    if variable_values_id and no_variables:
        formatter.error(
            message=(
                "--variable-values-id and --no-variables are mutually exclusive. "
                "Pass --variable-values-id to bind a specific values row, or "
                "--no-variables to skip resolution, but not both."
            ),
            error_code="INVALID_ARGUMENT",
        )
        raise typer.Exit(code=2)

    validate_branch_requires_project(formatter, branch, project)
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    if not formatter.json_mode:
        msg = f"Running [cyan]{component_id}[/cyan] / [cyan]{config_id}[/cyan]"
        if row_id:
            msg += f" (rows: {', '.join(row_id)})"
        if effective_branch is not None:
            msg += f" on branch [cyan]{effective_branch}[/cyan]"
        if wait:
            msg += f" [dim](waiting up to {timeout:.0f}s)[/dim]"
        msg += "..."
        formatter.console.print(msg)
        if variable_values_id:
            from rich.markup import escape

            formatter.console.print(
                f"[dim]Using variable values row: {escape(variable_values_id)}[/dim]"
            )
        elif no_variables:
            formatter.console.print("[dim]Skipping variable-values resolution.[/dim]")

    try:
        result = service.run_job(
            alias=project,
            component_id=component_id,
            config_id=config_id,
            config_row_ids=row_id,
            wait=wait,
            timeout=timeout,
            branch_id=effective_branch,
            variable_values_id=variable_values_id,
            no_variables=no_variables,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            project=project,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        job_id = result.get("id", "?")
        status = result.get("status", "unknown")
        if status in ("success", "terminated"):
            formatter.console.print(f"[bold green]Job {job_id}:[/bold green] {status}")
        elif status == "error":
            error_msg = ""
            job_result = result.get("result", {})
            if isinstance(job_result, dict):
                error_msg = job_result.get("message", "")
            formatter.console.print(f"[bold red]Job {job_id}:[/bold red] {status}")
            if error_msg:
                formatter.console.print(f"  Error: {error_msg}")
            raise typer.Exit(code=1)
        else:
            formatter.console.print(f"[bold blue]Job {job_id}:[/bold blue] {status}")
            if not wait:
                formatter.console.print(
                    "  Use --wait to poll until completion, "
                    f"or: kbagent job detail --project {project} --job-id {job_id}"
                )


@job_app.command("terminate")
def job_terminate(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project alias",
    ),
    job_id: list[str] | None = typer.Option(
        None,
        "--job-id",
        help="Job ID to terminate. Can be repeated. Mutually exclusive with --status.",
    ),
    status: str | None = typer.Option(
        None,
        "--status",
        help=(
            "Bulk-terminate jobs matching status. "
            f"Single killable: {', '.join(sorted(KILLABLE_JOB_STATUSES))}. "
            "Use 'any' to match all killable states at once (typical for runaway cleanup). "
            "Recommend scoping with --component-id / --config-id / --branch."
        ),
    ),
    component_id: str | None = typer.Option(
        None,
        "--component-id",
        help="Filter bulk terminate by component ID",
    ),
    config_id: str | None = typer.Option(
        None,
        "--config-id",
        help="Filter bulk terminate by configuration ID (requires --component-id)",
    ),
    limit: int = typer.Option(
        DEFAULT_JOB_LIMIT,
        "--limit",
        help=f"Max jobs to consider when using --status (1-{MAX_JOB_LIMIT})",
    ),
    branch: int | None = typer.Option(
        None,
        "--branch",
        help="Dev branch ID (filters jobs client-side; defaults to active branch if set via 'branch use')",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be terminated without executing",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt",
    ),
) -> None:
    """Terminate one or more Queue API jobs (use to stop runaway or stuck jobs).

    Two modes:

    - Single/multiple by ID: --job-id ID [--job-id ID ...]
    - Bulk by filter: --status processing [--component-id ID] [--config-id ID] [--branch ID]

    Queue API kill is asynchronous: the job's desiredStatus becomes 'terminating'
    and the actual status transitions to 'cancelled' (if waiting) or 'terminated'
    (if processing) within a few seconds. Poll with 'kbagent job detail' to
    observe the terminal state.

    Jobs already in a terminal state (success/error/terminated/cancelled) are
    counted as 'already_finished' — safe to re-run this command idempotently
    for cleanup purposes.
    """
    if should_hint(ctx):
        emit_hint(
            ctx,
            "job.terminate",
            project=project,
            job_id=job_id,
            status=status,
            component_id=component_id,
            config_id=config_id,
            limit=limit,
            branch=branch,
            dry_run=dry_run,
        )
        return

    formatter = get_formatter(ctx)
    service = get_service(ctx, "job_service")
    config_store: ConfigStore = ctx.obj["config_store"]

    if bool(job_id) == bool(status):
        formatter.error(
            message="Provide either --job-id (one or more) or --status, but not both.",
            error_code="INVALID_ARGUMENT",
        )
        raise typer.Exit(code=2)

    if status and status != "any" and status not in KILLABLE_JOB_STATUSES:
        formatter.error(
            message=(
                f"Invalid --status '{status}'. Use one of: "
                f"{', '.join(sorted(KILLABLE_JOB_STATUSES))} or 'any'."
            ),
            error_code="INVALID_ARGUMENT",
        )
        raise typer.Exit(code=2)

    if config_id and not component_id:
        formatter.error(
            message="--config-id requires --component-id.",
            error_code="INVALID_ARGUMENT",
        )
        raise typer.Exit(code=2)

    if limit < 1 or limit > MAX_JOB_LIMIT:
        formatter.error(
            message=f"Invalid --limit {limit}. Must be between 1 and {MAX_JOB_LIMIT}.",
            error_code="INVALID_ARGUMENT",
        )
        raise typer.Exit(code=2)

    validate_branch_requires_project(formatter, branch, project)
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    # Resolve job IDs
    resolved_ids: list[str]
    filter_context: dict[str, object | None] | None = None
    if job_id:
        resolved_ids = list(job_id)
    else:
        # "any" means: list without status filter, then keep only killable states client-side
        list_status = None if status == "any" else status
        try:
            matched = service.resolve_job_ids_by_filter(
                alias=project,
                status=list_status,
                component_id=component_id,
                config_id=config_id,
                branch_id=effective_branch,
                limit=limit,
            )
            if status == "any":
                matched = service.filter_killable(matched)
        except ConfigError as exc:
            formatter.error(message=exc.message, error_code="CONFIG_ERROR")
            raise typer.Exit(code=5) from None
        except KeboolaApiError as exc:
            formatter.error(
                message=exc.message,
                error_code=exc.error_code,
                project=project,
                retryable=exc.retryable,
            )
            raise typer.Exit(code=map_error_to_exit_code(exc)) from None

        resolved_ids = [str(j.get("id")) for j in matched if j.get("id")]
        filter_context = {
            "status": status,
            "component_id": component_id,
            "config_id": config_id,
            "branch_id": effective_branch,
            "matched_count": len(resolved_ids),
        }

    if not resolved_ids:
        empty_result = {
            "killed": [],
            "already_finished": [],
            "not_found": [],
            "failed": [],
            "dry_run": dry_run,
            "project_alias": project,
            "filter": filter_context,
        }
        if formatter.json_mode:
            formatter.output(empty_result)
        else:
            formatter.console.print("[bold blue]No jobs matched.[/bold blue]")
        return

    # Dry-run: report without killing
    if dry_run:
        try:
            result = service.terminate_jobs(
                alias=project,
                job_ids=resolved_ids,
                dry_run=True,
            )
        except ConfigError as exc:
            formatter.error(message=exc.message, error_code="CONFIG_ERROR")
            raise typer.Exit(code=5) from None

        if filter_context is not None:
            result["filter"] = filter_context
        if formatter.json_mode:
            formatter.output(result)
        else:
            formatter.console.print(
                f"[bold blue]Would terminate {len(resolved_ids)} job(s):[/bold blue]"
            )
            for jid in resolved_ids:
                formatter.console.print(f"  - {jid}")
        return

    # Confirmation prompt (interactive only)
    confirm_msg = f"Terminate {len(resolved_ids)} job(s) in project '{project}'?"
    if not yes and not formatter.json_mode and not typer.confirm(confirm_msg):
        formatter.console.print("Aborted.")
        raise typer.Exit(code=0)

    try:
        result = service.terminate_jobs(
            alias=project,
            job_ids=resolved_ids,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            project=project,
            retryable=exc.retryable,
        )
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if filter_context is not None:
        result["filter"] = filter_context

    if formatter.json_mode:
        formatter.output(result)
    else:
        for entry in result["killed"]:
            formatter.console.print(
                f"[bold green]Killed:[/bold green] {entry['id']} "
                f"(status={entry.get('status')}, desiredStatus={entry.get('desiredStatus')})"
            )
        for entry in result["already_finished"]:
            formatter.console.print(
                f"[yellow]Already finished:[/yellow] {entry['id']} ({entry.get('reason')})"
            )
        for jid in result["not_found"]:
            formatter.console.print(f"[bold red]Not found:[/bold red] {jid}")
        for f_item in result["failed"]:
            formatter.console.print(
                f"[bold red]Failed:[/bold red] {f_item['id']}: {f_item['error']}"
            )

    if result["failed"]:
        raise typer.Exit(code=1)
