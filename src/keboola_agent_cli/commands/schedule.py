"""Schedule discovery and audit commands.

Thin CLI layer over :class:`ScheduleService`. Three subcommands:

- ``schedule list`` — fleet-wide listing of ``keboola.scheduler`` configs.
- ``schedule detail`` — single-schedule detail plus parent metadata.
- ``schedule find`` — audit filters (cron-window, not-run-since).

All three are read-only and safe under ``--deny-writes``.
"""

from __future__ import annotations

import logging
from typing import Any

import typer
from rich.markup import escape
from rich.table import Table

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ._helpers import (
    check_cli_permission,
    emit_hint,
    get_formatter,
    get_service,
    map_error_to_exit_code,
    resolve_branch,
    should_hint,
)

logger = logging.getLogger(__name__)

schedule_app = typer.Typer(
    help="Discover and audit cron schedules across projects (keboola.scheduler)"
)


@schedule_app.callback(invoke_without_command=True)
def _schedule_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "schedule")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _enabled_badge(enabled: bool) -> str:
    return "[green]yes[/green]" if enabled else "[yellow]no[/yellow]"


def _format_schedule_table(
    formatter: Any,
    schedules: list[dict[str, Any]],
    *,
    extra_columns: list[str] | None = None,
) -> None:
    """Render a schedule list as a Rich table.

    ``extra_columns`` optionally appends columns like ``last_run_at`` that
    only exist on the ``find`` output, keeping the base list view compact.
    """
    extra_columns = extra_columns or []

    columns = [
        "Project",
        "Schedule",
        "Parent",
        "Component",
        "Cron",
        "TZ",
        "Enabled",
    ]
    columns.extend(extra_columns)

    tbl = Table(*columns, show_header=True, header_style="bold cyan")
    for s in schedules:
        row = [
            escape(s.get("project_alias", "")),
            escape(s.get("schedule_name", "") or s.get("schedule_id", "")),
            escape(s.get("parent_name", "") or s.get("parent_config_id", "")),
            escape(s.get("parent_component_id", "")),
            escape(s.get("cron", "")),
            escape(s.get("timezone", "")),
            _enabled_badge(bool(s.get("enabled", False))),
        ]
        for col in extra_columns:
            if col == "Last run":
                row.append(escape(s.get("last_run_at") or "never"))
            elif col == "In window":
                row.append(_enabled_badge(bool(s.get("matches_cron_window", True))))
            else:
                row.append("")
        tbl.add_row(*row)
    formatter.console.print(tbl)


def _emit_errors(formatter: Any, errors: list[dict[str, Any]]) -> None:
    for err in errors:
        formatter.warning(
            f"Project '{err.get('project_alias', '?')}': {err.get('message', 'error')}"
        )


# ---------------------------------------------------------------------------
# schedule list
# ---------------------------------------------------------------------------


@schedule_app.command("list")
def schedule_list(
    ctx: typer.Context,
    project: list[str] | None = typer.Option(
        None,
        "--project",
        help="Project alias (repeatable; omit for all registered projects)",
    ),
    enabled_only: bool = typer.Option(
        False,
        "--enabled-only",
        help="Only show schedules whose state is 'enabled'",
    ),
    branch: int | None = typer.Option(
        None, "--branch", help="Dev branch ID (requires single --project)"
    ),
) -> None:
    """List cron schedules (keboola.scheduler configs) across projects.

    Each row shows: project alias, schedule ID + name, parent component ID
    and config name, cron expression, timezone, and enabled state.
    """
    if should_hint(ctx):
        emit_hint(
            ctx,
            "schedule.list",
            project=project,
            enabled_only=enabled_only,
            branch=branch,
        )

    formatter = get_formatter(ctx)
    service = get_service(ctx, "schedule_service")
    config_store = ctx.obj["config_store"]

    if branch is not None and (not project or len(project) != 1):
        formatter.error(
            message="--branch requires exactly one --project",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2) from None

    effective_branch: int | None = branch
    if branch is None and project and len(project) == 1:
        _, effective_branch = resolve_branch(config_store, formatter, project[0], None)

    try:
        result = service.list_schedules(
            aliases=project,
            enabled_only=enabled_only,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
        return

    schedules = result.get("schedules", [])
    if not schedules:
        formatter.console.print("[dim]No schedules found.[/dim]")
    else:
        _format_schedule_table(formatter, schedules)
    _emit_errors(formatter, result.get("errors", []))


# ---------------------------------------------------------------------------
# schedule detail
# ---------------------------------------------------------------------------


@schedule_app.command("detail")
def schedule_detail(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    schedule_id: str = typer.Option(
        ..., "--schedule-id", help="keboola.scheduler configuration ID"
    ),
    branch: int | None = typer.Option(None, "--branch", help="Dev branch ID"),
) -> None:
    """Show full detail for a single cron schedule.

    Returns the cron expression, timezone, enabled state, and the parent
    configuration the schedule targets (component_id + config_id + name).
    """
    if should_hint(ctx):
        emit_hint(
            ctx,
            "schedule.detail",
            project=project,
            schedule_id=schedule_id,
            branch=branch,
        )

    formatter = get_formatter(ctx)
    service = get_service(ctx, "schedule_service")
    config_store = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    try:
        result = service.get_schedule_detail(
            alias=project,
            schedule_id=schedule_id,
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
        return

    formatter.console.print(
        f"\n[bold]{escape(result.get('schedule_name', ''))}[/bold] "
        f"[dim](ID: {escape(result.get('schedule_id', ''))})[/dim]"
    )
    formatter.console.print(f"  Project:     {escape(result.get('project_alias', ''))}")
    if result.get("branch_id") is not None:
        formatter.console.print(f"  Branch:      {result['branch_id']}")
    formatter.console.print(
        f"  Parent:      {escape(result.get('parent_name', '') or '(unknown)')} "
        f"[dim]{escape(result.get('parent_component_id', ''))}/"
        f"{escape(result.get('parent_config_id', ''))}[/dim]"
    )
    formatter.console.print(f"  Cron:        {escape(result.get('cron', ''))}")
    formatter.console.print(f"  Timezone:    {escape(result.get('timezone', ''))}")
    formatter.console.print(f"  Enabled:     {_enabled_badge(bool(result.get('enabled', False)))}")
    if result.get("created"):
        formatter.console.print(f"  Created:     {escape(result['created'])}")


# ---------------------------------------------------------------------------
# schedule find
# ---------------------------------------------------------------------------


@schedule_app.command("find")
def schedule_find(
    ctx: typer.Context,
    project: list[str] | None = typer.Option(
        None,
        "--project",
        help="Project alias (repeatable; omit for all projects)",
    ),
    cron_window: str | None = typer.Option(
        None,
        "--cron-window",
        help="Only include schedules firing entirely inside an hour window, "
        "e.g. '02:00-04:00'. Hour-field approximation -- see gotchas.md.",
    ),
    not_run_since: int | None = typer.Option(
        None,
        "--not-run-since",
        help="Only include schedules whose parent config has not produced "
        "a job in the last N days (or never ran).",
    ),
    branch: int | None = typer.Option(
        None, "--branch", help="Dev branch ID (requires single --project)"
    ),
) -> None:
    """Audit schedules by cron window or job-freshness.

    \b
    Examples:
      # Schedules that fire between 02:00 and 04:00 across every project
      kbagent --json schedule find --cron-window "02:00-04:00"

      # Schedules whose parent hasn't run in the last 90 days
      kbagent --json schedule find --not-run-since 90

      # Both filters combined (AND)
      kbagent --json schedule find --cron-window "00:00-05:00" --not-run-since 30

      # No filters -> same rows as 'schedule list' plus last_run_at
      kbagent --json schedule find
    """
    if should_hint(ctx):
        emit_hint(
            ctx,
            "schedule.find",
            project=project,
            cron_window=cron_window,
            not_run_since=not_run_since,
            branch=branch,
        )

    formatter = get_formatter(ctx)
    service = get_service(ctx, "schedule_service")
    config_store = ctx.obj["config_store"]

    if branch is not None and (not project or len(project) != 1):
        formatter.error(
            message="--branch requires exactly one --project",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2) from None

    effective_branch: int | None = branch
    if branch is None and project and len(project) == 1:
        _, effective_branch = resolve_branch(config_store, formatter, project[0], None)

    try:
        result = service.find_schedules(
            aliases=project,
            cron_window=cron_window,
            not_run_since_days=not_run_since,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
        return

    schedules = result.get("schedules", [])
    if not schedules:
        formatter.console.print("[dim]No schedules match the filters.[/dim]")
    else:
        extras: list[str] = []
        if cron_window is not None:
            extras.append("In window")
        if not_run_since is not None:
            extras.append("Last run")
        _format_schedule_table(formatter, schedules, extra_columns=extras)
    _emit_errors(formatter, result.get("errors", []))
