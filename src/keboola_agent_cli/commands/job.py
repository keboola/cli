"""Job commands - list history, show detail, and run jobs via Queue API.

Thin CLI layer: parses arguments, calls JobService, formats output.
No business logic belongs here.
"""

from enum import StrEnum

import typer
from rich.markup import escape

from ..config_store import ConfigStore
from ..constants import (
    DEFAULT_JOB_LIMIT,
    DEFAULT_JOB_MODE,
    DEFAULT_JOB_RUN_TIMEOUT,
    DEFAULT_JOB_SORT_BY,
    DEFAULT_JOB_SORT_ORDER,
    DEFAULT_LOG_TAIL_LINES,
    DEFAULT_POLL_STRATEGY,
    JOB_SORT_FIELDS,
    JOB_SORT_ORDERS,
    KILLABLE_JOB_STATUSES,
    MAX_JOB_LIMIT,
    MAX_LOG_TAIL_LINES,
    VALID_STATUSES,
)
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..output import OutputFormatter, format_job_detail, format_jobs_table
from ._helpers import (
    check_cli_permission,
    emit_project_warnings,
    get_formatter,
    get_service,
    map_error_to_exit_code,
    resolve_branch,
    validate_branch_requires_project,
)


class JobMode(StrEnum):
    """Queue API job mode."""

    run = "run"
    debug = "debug"


class PollStrategy(StrEnum):
    """Polling cadence for --wait."""

    exponential = "exponential"
    fixed = "fixed"


job_app = typer.Typer(help="Browse job history and run jobs")


@job_app.callback(invoke_without_command=True)
def _job_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "job")


def _validate_log_tail_lines(formatter: OutputFormatter, log_tail_lines: int) -> None:
    """Reject an out-of-range --log-tail-lines with exit 2.

    Shared by ``job run`` and ``job detail`` so both surfaces accept exactly
    the same range and report a rejection the same way.
    """
    if log_tail_lines < 0 or log_tail_lines > MAX_LOG_TAIL_LINES:
        formatter.error(
            message=(
                f"--log-tail-lines must be between 0 and {MAX_LOG_TAIL_LINES}. "
                f"Got {log_tail_lines}."
            ),
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)


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
    offset: int = typer.Option(
        0,
        "--offset",
        help="Number of jobs to skip per project, for paging past --limit",
    ),
    sort_by: str = typer.Option(
        DEFAULT_JOB_SORT_BY,
        "--sort-by",
        help=f"Field to sort by: {', '.join(JOB_SORT_FIELDS)}",
    ),
    sort_order: str = typer.Option(
        DEFAULT_JOB_SORT_ORDER,
        "--sort-order",
        help=f"Sort direction: {', '.join(JOB_SORT_ORDERS)}",
    ),
) -> None:
    """List jobs from connected projects."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "job_service")

    # Validate status
    if status and status not in VALID_STATUSES:
        formatter.error(
            message=f"Invalid status '{status}'. Valid statuses: {', '.join(VALID_STATUSES)}",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    # Validate limit range
    if limit < 1 or limit > MAX_JOB_LIMIT:
        formatter.error(
            message=f"Invalid limit {limit}. Must be between 1 and {MAX_JOB_LIMIT}.",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    # Validate offset
    if offset < 0:
        formatter.error(
            message=f"Invalid --offset {offset}. Must be 0 or greater.",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    # Validate sort controls against the Queue API's accepted values
    if sort_by not in JOB_SORT_FIELDS:
        formatter.error(
            message=f"Invalid --sort-by '{sort_by}'. Valid fields: {', '.join(JOB_SORT_FIELDS)}",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    if sort_order not in JOB_SORT_ORDERS:
        formatter.error(
            message=f"Invalid --sort-order '{sort_order}'. Valid values: {', '.join(JOB_SORT_ORDERS)}",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    # Validate config_id requires component_id
    if config_id and not component_id:
        formatter.error(
            message="--config-id requires --component-id to be specified.",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    try:
        result = service.list_jobs(
            aliases=project,
            component_id=component_id,
            config_id=config_id,
            status=status,
            limit=limit,
            offset=offset,
            sort_by=sort_by,
            sort_order=sort_order,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
    log_tail_lines: int = typer.Option(
        0,
        "--log-tail-lines",
        help="Also fetch this many of the job's most recent events (0 = skip the extra call)",
    ),
) -> None:
    """Show detailed information about a specific job.

    Pass --log-tail-lines N to attach the job's last N events, which is how
    you inspect the logs of a job that already finished (`job run` only
    surfaces a tail for the run it started itself).
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "job_service")

    _validate_log_tail_lines(formatter, log_tail_lines)

    try:
        result = service.get_job_detail(
            alias=project,
            job_id=job_id,
            log_tail_lines=log_tail_lines,
        )
        formatter.output(result, format_job_detail)
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
    mode: JobMode = typer.Option(
        JobMode(DEFAULT_JOB_MODE),
        "--mode",
        help=(
            "Queue API job mode. 'run' (default) executes the component "
            "normally and writes to mapped output tables. 'debug' executes "
            "the component but redirects its output to a Storage File tagged "
            "'debug-<jobId>' instead of the destination buckets -- safe for "
            "dry-runs and for reproducing a failing run on production "
            "configuration without touching production data."
        ),
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
    poll_strategy: PollStrategy = typer.Option(
        PollStrategy(DEFAULT_POLL_STRATEGY),
        "--poll-strategy",
        help=(
            "Polling cadence used with --wait. 'exponential' (default) "
            "starts at 2s and relaxes toward 15s as a job runs long "
            "(2s x 30 -> 5s x 48 -> 15s). 'fixed' keeps a constant 1s "
            "interval (useful for tests or very short jobs)."
        ),
    ),
    log_tail_lines: int = typer.Option(
        DEFAULT_LOG_TAIL_LINES,
        "--log-tail-lines",
        help=(
            "On FAILED/WARNING/TERMINATED jobs, fetch the last N job events "
            f"(from Storage Events API) and surface them as 'logTail' in "
            f"JSON output or a panel in human mode. Only used with --wait. "
            f"0 disables (recommended for automation pipelines); max "
            f"{MAX_LOG_TAIL_LINES}."
        ),
    ),
    idempotency_key: str | None = typer.Option(
        None,
        "--idempotency-key",
        help=(
            "Client-supplied de-duplication token (issue #427). On replay with "
            "the same key, a prior still-running or non-failed job is returned "
            "instead of creating a duplicate -- safe for resumed/retried build "
            "steps. A prior FAILED run is re-run. Dedup is client-side (the "
            "Queue API has no idempotency key) and scoped to this machine's "
            "config-dir; reusing a key for a different component/config errors."
        ),
    ),
    force_rerun: bool = typer.Option(
        False,
        "--force-rerun",
        help="Ignore any stored --idempotency-key entry and always create a fresh job.",
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

    Queue polling uses an exponential curve by default (2s x 30 -> 5s x 48
    -> 15s, total 5min before the 15s tail). If --timeout expires, kbagent
    issues kill_job on the remote and exits 7 (JOB_TIMEOUT_TERMINATED) with
    the cancelled job + log tail attached. If the kill itself fails, exits
    4 (QUEUE_JOB_TIMEOUT, retryable) so scripts can tell "we killed it"
    from "local gave up, remote may still be running".
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "job_service")
    config_store: ConfigStore = ctx.obj["config_store"]

    if variable_values_id is not None:
        variable_values_id = variable_values_id.strip()
        if not variable_values_id:
            formatter.error(
                message=(
                    "--variable-values-id cannot be empty or whitespace. "
                    "Pass a row id, or omit the flag to auto-resolve the default row."
                ),
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
            raise typer.Exit(code=2)

    if variable_values_id and no_variables:
        formatter.error(
            message=(
                "--variable-values-id and --no-variables are mutually exclusive. "
                "Pass --variable-values-id to bind a specific values row, or "
                "--no-variables to skip resolution, but not both."
            ),
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    _validate_log_tail_lines(formatter, log_tail_lines)

    validate_branch_requires_project(formatter, branch, project)
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    if not formatter.json_mode:
        msg = f"Running [cyan]{component_id}[/cyan] / [cyan]{config_id}[/cyan]"
        if row_id:
            msg += f" (rows: {', '.join(row_id)})"
        if effective_branch is not None:
            msg += f" on branch [cyan]{effective_branch}[/cyan]"
        if mode != DEFAULT_JOB_MODE:
            msg += f" [bold yellow]mode={mode}[/bold yellow]"
        if wait:
            msg += f" [dim](waiting up to {timeout:.0f}s)[/dim]"
        msg += "..."
        formatter.console.print(msg)
        if no_variables:
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
            poll_strategy=poll_strategy,
            log_tail_lines=log_tail_lines,
            mode=mode,
            idempotency_key=idempotency_key,
            force_rerun=force_rerun,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(
            message=exc.message,
            error_code=exc.error_code,
            project=project,
            retryable=exc.retryable,
            details=exc.details or None,
        )
        if not formatter.json_mode:
            _render_log_tail(formatter, exc.details)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        if result.get("idempotent_replay"):
            formatter.console.print(
                "[dim]Idempotency key matched a prior run -- returning the existing "
                "job (no new job created).[/dim]"
            )
        resolved_id = result.get("resolvedVariableValuesId")
        if resolved_id:
            formatter.console.print(f"[dim]Bound variable values row: {escape(resolved_id)}[/dim]")
        job_id = result.get("id", "?")
        status = result.get("status", "unknown")
        if status in ("success", "terminated"):
            formatter.console.print(f"[bold green]Job {job_id}:[/bold green] {status}")
        elif status == "warning":
            # "error" is intentionally absent here: the service layer raises
            # QUEUE_JOB_FAILED for failed jobs, so this human-mode branch is
            # only reached for non-error terminal and transient states.
            formatter.console.print(f"[bold yellow]Job {job_id}:[/bold yellow] {status}")
            _render_log_tail(formatter, {"logTail": result.get("logTail") or []})
        else:
            formatter.console.print(f"[bold blue]Job {job_id}:[/bold blue] {status}")
            if not wait:
                formatter.console.print(
                    "  Use --wait to poll until completion, "
                    f"or: kbagent job detail --project {project} --job-id {job_id}"
                )


def _render_log_tail(formatter, details: dict | None) -> None:
    """Render a logTail attached to an error or result payload in human mode."""
    if not details:
        return
    tail = details.get("logTail") or []
    if not tail:
        return
    formatter.console.print("[bold]Log tail (last events):[/bold]")
    for event in tail:
        ts = event.get("created") or event.get("createdTime") or ""
        event_type = event.get("type") or event.get("event") or ""
        msg = event.get("message") or ""
        # Trim overly long event messages so the panel stays readable.
        if len(msg) > 400:
            msg = msg[:400] + "..."
        formatter.console.print(f"  [dim]{ts}[/dim] [{event_type}] {msg}")


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
    formatter = get_formatter(ctx)
    service = get_service(ctx, "job_service")
    config_store: ConfigStore = ctx.obj["config_store"]

    if bool(job_id) == bool(status):
        formatter.error(
            message="Provide either --job-id (one or more) or --status, but not both.",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    if status and status != "any" and status not in KILLABLE_JOB_STATUSES:
        formatter.error(
            message=(
                f"Invalid --status '{status}'. Use one of: "
                f"{', '.join(sorted(KILLABLE_JOB_STATUSES))} or 'any'."
            ),
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    if config_id and not component_id:
        formatter.error(
            message="--config-id requires --component-id.",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    if limit < 1 or limit > MAX_JOB_LIMIT:
        formatter.error(
            message=f"Invalid --limit {limit}. Must be between 1 and {MAX_JOB_LIMIT}.",
            error_code=ErrorCode.INVALID_ARGUMENT,
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
            formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
            formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
