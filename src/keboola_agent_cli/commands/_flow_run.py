"""The ``kbagent flow run`` command (issue #725).

Running a flow used to mean ``job run --component-id keboola.flow``, which has
no phase or task awareness: every run starts at phase 1 and re-executes the
whole graph. Re-testing one downstream phase of a 16-phase production flow
therefore meant re-running ~50 unrelated tasks, or running the components
individually and bypassing the flow engine entirely.

The Queue API has supported a task allowlist all along -- ``onlyFlowTaskIds``
on ``POST /jobs`` (keboola/job-queue ``apps/public-api/docs/swagger.yaml``) --
and mirrors it back on every job detail. kbagent simply never sent it. This
command sends it, and resolves a *phase* into the task ids for callers who
think in phases rather than in task ids.

WHAT A SELECTED RUN IS AND IS NOT (keboola/job-queue-daemon
``docs/flow-documentation.md``): the daemon runs only the selected tasks and
**ignores every configured condition** -- the phase graph is linearized into a
single sequential chain of synthetic unconditional ``selected-run``
transitions. Phases kept only to route to a selected task run empty. A failing
task does not stop the chain. So this is "re-run this part of the flow", NOT
"rehearse the flow's ``next[].condition`` logic": there is no API that does the
latter without a full run, and pretending otherwise would be worse than the
gap it fills. The command says so in ``--dry-run`` and in JSON
(``conditions_evaluated: false``).

Lives in a private module because ``commands/flow.py`` is at the 800-code-line
commands soft ceiling (CONTRIBUTING.md file-size budgets). Mounted flat onto
``flow_app`` via :func:`register`, so the permission key stays ``flow.run``.
"""

from __future__ import annotations

from typing import Any

import typer
from rich.markup import escape
from rich.table import Table

from ..constants import (
    DEFAULT_JOB_RUN_TIMEOUT,
    DEFAULT_LOG_TAIL_LINES,
    DEFAULT_POLL_STRATEGY,
    MAX_LOG_TAIL_LINES,
)
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._helpers import (
    get_formatter,
    get_service,
    map_error_to_exit_code,
    resolve_branch,
)

CONDITIONS_NOTE = (
    "A selected run IGNORES the flow's phase conditions: the graph is "
    "linearized into an unconditional chain, and a failing task does not stop "
    "it. Use it to re-run part of a flow, not to verify next[].condition logic."
)


def _format_selection(formatter: Any, selection: dict[str, Any]) -> None:
    """Human-mode preview of what a partial run resolved to."""
    task_ids = selection.get("task_ids") or []
    formatter.console.print(
        f"\n[bold]Partial run of flow {escape(str(selection.get('config_id', '')))}[/bold]"
        f"  [dim]({escape(str(selection.get('project_alias', '')))})[/dim]"
    )
    if selection.get("from_phase"):
        formatter.console.print(
            f"[dim]From phase: {escape(str(selection['from_phase']))} "
            f"(plus every phase reachable from it)[/dim]"
        )

    table = Table(title=f"Selected tasks ({len(task_ids)})")
    table.add_column("Task ID")
    table.add_column("Name")
    table.add_column("Phase")
    phase_names = {p["id"]: p.get("name", "") for p in selection.get("selected_phases") or []}
    for task in selection.get("selected_tasks") or []:
        phase_id = str(task.get("phase", ""))
        phase_label = phase_id
        if phase_names.get(phase_id):
            phase_label = f"{phase_id} ({phase_names[phase_id]})"
        table.add_row(
            escape(str(task.get("id", ""))),
            escape(str(task.get("name", ""))),
            escape(phase_label),
        )
    formatter.console.print(table)

    skipped = selection.get("skipped_disabled_task_ids") or []
    if skipped:
        formatter.console.print(
            f"[dim]Skipped {len(skipped)} disabled task(s): {escape(', '.join(skipped))}[/dim]"
        )
    formatter.console.print(f"\n[yellow]{escape(CONDITIONS_NOTE)}[/yellow]")


def _print_run_result(
    formatter: Any,
    result: dict[str, Any],
    project: str,
    selection: dict[str, Any] | None,
    wait: bool,
) -> None:
    job_id = result.get("id", "?")
    status = result.get("status", "unknown")
    if selection:
        formatter.console.print(
            f"[dim]Partial run: {len(selection['task_ids'])} task(s) selected. "
            f"{CONDITIONS_NOTE}[/dim]"
        )
    if status == "success":
        formatter.console.print(f"[bold green]Flow job {job_id}:[/bold green] {status}")
    elif status in ("warning", "terminated"):
        formatter.console.print(f"[bold yellow]Flow job {job_id}:[/bold yellow] {status}")
    else:
        formatter.console.print(f"[bold blue]Flow job {job_id}:[/bold blue] {status}")
        if not wait:
            formatter.console.print(
                "  Use --wait to poll until completion, "
                f"or: kbagent job detail --project {project} --job-id {job_id}"
            )


def register(app: typer.Typer) -> None:
    """Mount the ``run`` command onto ``app`` (the ``flow`` Typer group)."""

    @app.command("run")
    def flow_run(
        ctx: typer.Context,
        project: str = typer.Option(..., "--project", help="Project alias"),
        flow_id: str = typer.Option(..., "--flow-id", help="Flow configuration ID"),
        from_phase: str | None = typer.Option(
            None,
            "--from-phase",
            help=(
                "Run this phase and every phase reachable from it via "
                "next[].goto. Disabled tasks in scope are skipped. Mutually "
                "exclusive with --only-task."
            ),
        ),
        only_task: list[str] | None = typer.Option(
            None,
            "--only-task",
            help=(
                "Explicit task ID to run (repeat for several). An unknown or "
                "disabled id is an error, not a silent skip. Mutually "
                "exclusive with --from-phase."
            ),
        ),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="Resolve and print the task selection without creating a job.",
        ),
        wait: bool = typer.Option(
            False, "--wait", help="Wait for the flow job to finish (poll until terminal state)"
        ),
        timeout: float = typer.Option(
            DEFAULT_JOB_RUN_TIMEOUT, "--timeout", help="Max seconds to wait when --wait is set"
        ),
        branch: int | None = typer.Option(
            None, "--branch", help="Dev branch ID (overrides active branch)"
        ),
        poll_strategy: str = typer.Option(
            DEFAULT_POLL_STRATEGY,
            "--poll-strategy",
            help="Polling cadence used with --wait: 'exponential' (default) or 'fixed'.",
        ),
        log_tail_lines: int = typer.Option(
            DEFAULT_LOG_TAIL_LINES,
            "--log-tail-lines",
            help=(
                "On a non-success terminal state, surface the last N job events. "
                f"Only used with --wait. 0 disables; max {MAX_LOG_TAIL_LINES}."
            ),
        ),
    ) -> None:
        """Run a flow, optionally only from a given phase onward.

        With neither --from-phase nor --only-task this is the ordinary full
        flow run (identical to `job run --component-id keboola.flow`), with
        conditions evaluated as configured.

        With either selector the Queue API runs ONLY the selected tasks and
        IGNORES the flow's phase conditions -- the graph is linearized into an
        unconditional chain, so a failed task does not halt the run and the
        flow's final status is that of the last phase in the chain. Preview a
        selection with --dry-run before committing to it.
        """
        formatter = get_formatter(ctx)
        config_store = ctx.obj["config_store"]

        if from_phase and only_task:
            formatter.error(
                message=(
                    "--from-phase and --only-task are mutually exclusive. Pass a "
                    "phase to expand into its downstream tasks, or an explicit "
                    "task-id allowlist, but not both."
                ),
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
            raise typer.Exit(code=2)
        if dry_run and not (from_phase or only_task):
            formatter.error(
                message=(
                    "--dry-run needs --from-phase or --only-task: with no selector "
                    "there is nothing to resolve, and a full flow run has no preview."
                ),
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
            raise typer.Exit(code=2)
        if log_tail_lines < 0 or log_tail_lines > MAX_LOG_TAIL_LINES:
            formatter.error(
                message=(
                    f"--log-tail-lines must be between 0 and {MAX_LOG_TAIL_LINES}. "
                    f"Got {log_tail_lines}."
                ),
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
            raise typer.Exit(code=2)

        _, effective_branch = resolve_branch(config_store, formatter, project, branch)

        selection: dict[str, Any] | None = None
        try:
            if from_phase or only_task:
                selection = get_service(ctx, "flow_service").resolve_flow_task_ids(
                    alias=project,
                    config_id=flow_id,
                    from_phase=from_phase,
                    only_task_ids=list(only_task) if only_task else None,
                    branch_id=effective_branch,
                )
                if dry_run:
                    if formatter.json_mode:
                        formatter.output({**selection, "dry_run": True})
                    else:
                        _format_selection(formatter, selection)
                    return

            result = get_service(ctx, "job_service").run_job(
                alias=project,
                component_id="keboola.flow",
                config_id=flow_id,
                wait=wait,
                timeout=timeout,
                branch_id=effective_branch,
                poll_strategy=poll_strategy,
                log_tail_lines=log_tail_lines,
                only_flow_task_ids=selection["task_ids"] if selection else None,
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
            # A rejected selector is a usage error whichever layer caught it:
            # the two checks above exit 2, and so must the service-side ones
            # (unknown phase, unknown/disabled task id, empty selection). The
            # generic mapping sends INVALID_ARGUMENT to exit 1, which would
            # make "you typed a bad phase id" indistinguishable from "the run
            # failed".
            if exc.error_code == ErrorCode.INVALID_ARGUMENT:
                raise typer.Exit(code=2) from None
            raise typer.Exit(code=map_error_to_exit_code(exc)) from None

        if selection:
            # Echo the selection onto the job payload so a caller that only
            # keeps the run's JSON can still tell WHICH tasks it ran -- the
            # Queue API mirrors the ids back, but not their names or phases.
            result["flow_task_selection"] = selection
        if formatter.json_mode:
            formatter.output(result)
        else:
            _print_run_result(formatter, result, project, selection, wait=wait)
