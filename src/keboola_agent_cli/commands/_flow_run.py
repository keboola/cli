"""The ``kbagent flow run`` command: run a whole flow or a selection of its tasks (#725).

``--task-id`` (repeatable) and ``--from-phase-id`` select tasks; the Queue job
then carries ``onlyFlowTaskIds``, like the Keboola UI's "Run selected tasks".
A partial run does NOT evaluate transition conditions. Without a selector the
whole flow runs with the same Queue body as ``kbagent job run``.

Lives in a private module because ``commands/flow.py`` is over the commands
soft ceiling (CONTRIBUTING.md file-size budgets). Mounted flat onto
``flow_app`` via :func:`register`, so the permission key is ``flow.run`` and a
deny policy on it also blocks ``--dry-run`` (spec S7).
"""

from __future__ import annotations

from typing import Any

import typer
from rich.markup import escape

from ..constants import DEFAULT_JOB_RUN_TIMEOUT
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..services.flow_service import FlowRunOutcome
from ._helpers import (
    emit_api_error,
    finish_items,
    get_formatter,
    get_service,
    map_error_to_exit_code,
    split_multi,
)
from ._project_ref import PROJECT_HELP

RUN_HELP = """Run a conditional flow, or only some of its tasks.

Without --task-id / --from-phase-id the whole flow runs, exactly like
`kbagent job run --component-id keboola.flow`.

A PARTIAL run (any selector) sends onlyFlowTaskIds to the Queue API, like the
UI's "Run selected tasks". It does NOT evaluate transition conditions: every
selected task runs, phase after phase, also after a task failed; phases that
only route may run empty; the flow status is the status of the last phase and
can hide a failed task; unselected variable tasks do not run.

--from-phase-id P selects the enabled tasks of P and of every phase reached
by following DEFAULT transitions (the transition without a condition). It
does not follow conditional transitions; the phases it leaves out that way are
named in a CONDITIONAL_PHASES_SKIPPED warning. Disabled tasks there are
skipped and listed. --task-id adds tasks; the union runs in flow order.

--wait on a partial run reports every selected task's status (task_results,
failed_task_ids) and exits 1 when one did not succeed or left no result.
A --timeout never stops the job: it keeps running, the error names it.
"""


def _check_blank_selectors(
    formatter: Any, task_id: list[str] | None, from_phase_id: str | None
) -> None:
    """Refuse a blank selector before split_multi drops it (D2, R1): never a whole-flow run."""
    blank_task = task_id is not None and any(not value.strip() for value in task_id)
    blank_phase = from_phase_id is not None and not from_phase_id.strip()
    if blank_task or blank_phase:
        option = "--task-id" if blank_task else "--from-phase-id"
        formatter.error(
            message=f"{option} must not be empty or whitespace. Omit both --task-id and "
            "--from-phase-id to run the whole flow.",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)


def _render_selection(formatter: Any, payload: dict[str, Any]) -> None:
    """Human mode: what a run selects (membership, not execution order)."""
    selection = payload.get("selection") or {}
    kind = "Partial run" if payload.get("partial") else "Whole flow (conditions apply)"
    formatter.console.print(f"[bold]{kind}[/bold] of flow {escape(str(payload.get('flow_id')))}")
    for phase in selection.get("phases") or []:
        ids = ", ".join(phase.get("task_ids") or [])
        formatter.console.print(
            f"  phase {escape(str(phase.get('id')))} ({escape(str(phase.get('name') or ''))}): "
            f"{escape(ids)}"
        )
    skipped = selection.get("skipped_disabled_task_ids") or []
    if skipped:
        formatter.console.print(f"  [dim]disabled, skipped: {escape(', '.join(skipped))}[/dim]")
    if selection.get("default_path_cycle"):
        formatter.console.print("  [yellow]The default transitions form a cycle.[/yellow]")


def _render_job(formatter: Any, payload: dict[str, Any], alias: str) -> None:
    _render_selection(formatter, payload)
    job_id = payload.get("id", "?")
    formatter.console.print(f"[bold]Job {escape(str(job_id))}:[/bold] {payload.get('status')}")
    if payload.get("isFinished") is not True:
        formatter.console.print(
            f"  Follow it: kbagent job detail --project {alias} --job-id {job_id}"
        )


def _finish(formatter: Any, outcome: FlowRunOutcome, alias: str) -> None:
    payload = outcome.payload
    if payload.get("dry_run"):
        if formatter.json_mode:
            formatter.output(payload)
        else:
            _render_selection(formatter, payload)
            count = len((payload.get("selection") or {}).get("task_ids") or [])
            formatter.console.print(f"[bold green]Success:[/bold green] {count} task(s) would run.")
        return
    if not (outcome.waited and payload.get("partial")):
        if formatter.json_mode:
            formatter.output(payload)
        else:
            _render_job(formatter, payload, alias)
        return

    def render() -> None:
        _render_job(formatter, payload, alias)
        for result in payload.get("task_results") or []:
            if result["task_id"] in outcome.failed_task_ids:
                formatter.item_error(f"task {result['task_id']}", str(result["status"]))

    failed = len(outcome.failed_task_ids)
    finish_items(
        formatter,
        payload,
        failed=failed,
        total=outcome.selected_count,
        noun="selected task(s)",
        summary="" if failed else f"{outcome.selected_count} selected task(s) succeeded.",
        render=render,
    )


def register(app: typer.Typer) -> None:
    """Mount the ``run`` command onto ``app`` (the ``flow`` Typer group)."""

    @app.command("run", help=RUN_HELP)
    def flow_run(
        ctx: typer.Context,
        project: str = typer.Option(None, "--project", "-p", help=PROJECT_HELP),
        flow_id: str = typer.Option(..., "--flow-id", help="Flow configuration ID"),
        task_id: list[str] | None = typer.Option(
            None,
            "--task-id",
            help="Flow task ID to run (repeat for several). Makes the run partial.",
        ),
        from_phase_id: str | None = typer.Option(
            None,
            "--from-phase-id",
            help="Run the enabled tasks of this phase and of the phases reached by "
            "following DEFAULT transitions. Makes the run partial.",
        ),
        variable_values_id: str | None = typer.Option(
            None,
            "--variable-values-id",
            help="Explicit keboola.variables values-row ID to bind. Mutually exclusive "
            "with --no-variables.",
        ),
        no_variables: bool = typer.Option(
            False,
            "--no-variables",
            help="Skip variable-values resolution. Mutually exclusive with --variable-values-id.",
        ),
        branch: int | None = typer.Option(
            None, "--branch", help="Dev branch ID (overrides the active branch; 0 = production)"
        ),
        wait: bool = typer.Option(False, "--wait", help="Wait for the flow job to finish."),
        timeout: float = typer.Option(
            DEFAULT_JOB_RUN_TIMEOUT,
            "--timeout",
            help=f"Max seconds to wait with --wait (default {DEFAULT_JOB_RUN_TIMEOUT:g}). "
            "The job keeps running after a timeout.",
        ),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="Read the flow and show the selection; create no job.",
        ),
    ) -> None:
        formatter = get_formatter(ctx)
        service = get_service(ctx, "flow_service")
        _check_blank_selectors(formatter, task_id, from_phase_id)
        task_ids = split_multi(task_id, label="--task-id") if task_id else None

        try:
            outcome = service.run_flow(
                alias=project,
                flow_id=flow_id,
                task_ids=task_ids,
                from_phase_id=from_phase_id,
                branch_id=branch,
                variable_values_id=variable_values_id,
                no_variables=no_variables,
                wait=wait,
                timeout=timeout,
                dry_run=dry_run,
            )
        except ConfigError as exc:
            formatter.error(
                message=exc.message,
                error_code=getattr(exc, "error_code", ErrorCode.CONFIG_ERROR),
            )
            raise typer.Exit(code=5) from None
        except KeboolaApiError as exc:
            emit_api_error(formatter, exc, project=project)
            raise typer.Exit(code=map_error_to_exit_code(exc)) from None

        # O3: every warning goes in before the one output document.
        for warning in outcome.warnings:
            formatter.warning(escape(warning.message), code=warning.code, **warning.extra)
        _finish(formatter, outcome, project)
