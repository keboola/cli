"""Flow commands -- list, detail, new, update, delete, schedule, schedule-remove, schema.

Thin CLI layer: parses arguments, calls FlowService, formats output.
No business logic belongs here.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape
from rich.table import Table

from ..errors import ConfigError, KeboolaApiError
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

flow_app = typer.Typer(help="Manage flows (keboola.orchestrator + keboola.flow)")

_FLOW_COMPONENT_CHOICES = ["keboola.orchestrator", "keboola.flow"]

# YAML/JSON schema snippet shown by 'flow schema'
_FLOW_SCHEMA = """\
# kbagent flow schema -- keboola.flow configuration format
#
# Create with: kbagent flow new --project ALIAS --name "My Flow" [--file flow.yaml]
# Update with: kbagent flow update --project ALIAS --flow-id ID --file flow.yaml

name: "My Flow"
description: "Optional description"

phases:
  - id: 1
    name: "Phase 1 - Extract"
    dependsOn: []           # IDs of phases that must complete first
  - id: 2
    name: "Phase 2 - Transform"
    dependsOn: [1]

tasks:
  - id: 1
    name: "Extract Data"
    phase: 1               # phase.id this task belongs to
    componentId: "keboola.ex-http"
    configId: "123456789"
    continueOnFailure: false
    enabled: true
  - id: 2
    name: "Run Transformation"
    phase: 2
    componentId: "keboola.snowflake-transformation"
    configId: "987654321"
    continueOnFailure: false
    enabled: true

# Notes:
#  - dependsOn: IDs form a directed acyclic graph (kbagent validates this)
#  - configId values must be string IDs of existing configs in the project
#  - For keboola.orchestrator (legacy), phases are referenced by name (string),
#    not ID (integer); use keboola.flow for new flows
"""


@flow_app.callback(invoke_without_command=True)
def _flow_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "flow")


# ---------------------------------------------------------------------------
# flow list
# ---------------------------------------------------------------------------


@flow_app.command("list")
def flow_list(
    ctx: typer.Context,
    project: list[str] | None = typer.Option(
        None,
        "--project",
        help="Project alias (repeatable for multiple projects; omit for all)",
    ),
    branch: int | None = typer.Option(
        None, "--branch", help="Dev branch ID (per-project; requires single --project)"
    ),
) -> None:
    """List all flows (keboola.orchestrator + keboola.flow) across projects."""
    if should_hint(ctx):
        emit_hint(ctx, "flow.list", project=project, branch=branch)

    formatter = get_formatter(ctx)
    service = get_service(ctx, "flow_service")
    config_store = ctx.obj["config_store"]

    if branch is not None and (not project or len(project) != 1):
        formatter.error(
            message="--branch requires exactly one --project",
            error_code="INVALID_ARGUMENT",
        )
        raise typer.Exit(code=2)

    effective_branch: int | None = branch
    if branch is None and project and len(project) == 1:
        _, effective_branch = resolve_branch(config_store, formatter, project[0], None)

    try:
        result = service.list_flows(aliases=project, branch_id=effective_branch)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        _format_flows_table(formatter, result)


def _format_flows_table(formatter: Any, result: dict[str, Any]) -> None:
    flows = result.get("flows", [])
    errors = result.get("errors", [])

    if not flows:
        formatter.console.print("[dim]No flows found.[/dim]")
    else:
        tbl = Table(
            "Project",
            "Component",
            "Config ID",
            "Name",
            "Disabled",
            show_header=True,
            header_style="bold cyan",
        )
        for f in flows:
            disabled = "[red]yes[/red]" if f.get("is_disabled") else "[dim]no[/dim]"
            tbl.add_row(
                escape(f.get("project_alias", "")),
                escape(f.get("component_id", "")),
                escape(f.get("config_id", "")),
                escape(f.get("name", "")),
                disabled,
            )
        formatter.console.print(tbl)

    for err in errors:
        formatter.warning(
            f"Project '{err.get('project_alias', '?')}': {err.get('message', 'error')}"
        )


# ---------------------------------------------------------------------------
# flow detail
# ---------------------------------------------------------------------------


@flow_app.command("detail")
def flow_detail(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    flow_id: str = typer.Option(..., "--flow-id", help="Flow configuration ID"),
    component_id: str = typer.Option(
        "keboola.orchestrator",
        "--component-id",
        help="Flow component ID (default: keboola.orchestrator). "
        "Use --component-id keboola.flow for flows listed with component_id=keboola.flow.",
    ),
    branch: int | None = typer.Option(None, "--branch", help="Dev branch ID"),
) -> None:
    """Show detailed flow information including phases and tasks."""
    if should_hint(ctx):
        emit_hint(
            ctx,
            "flow.detail",
            project=project,
            flow_id=flow_id,
            component_id=component_id,
            branch=branch,
        )

    formatter = get_formatter(ctx)
    service = get_service(ctx, "flow_service")
    config_store = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    try:
        result = service.get_flow_detail(
            alias=project,
            component_id=component_id,
            config_id=flow_id,
            branch_id=effective_branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        _format_flow_detail(formatter, result)


def _format_flow_detail(formatter: Any, result: dict[str, Any]) -> None:
    formatter.console.print(
        f"\n[bold]{escape(result.get('name', ''))}[/bold]"
        f"  [dim]({escape(result.get('component_id', ''))} / {escape(str(result.get('id', '')))})[/dim]"
    )
    if result.get("description"):
        formatter.console.print(f"[dim]{escape(result['description'])}[/dim]")
    if result.get("branch_id"):
        formatter.console.print(f"[dim]Branch: {result['branch_id']}[/dim]")

    phases = result.get("phases", [])
    tasks = result.get("tasks", [])

    if not phases and not tasks:
        formatter.console.print("\n[dim]No phases or tasks defined.[/dim]")
        return

    formatter.console.print(
        f"\n[bold]Phases[/bold] ({len(phases)})  [bold]Tasks[/bold] ({len(tasks)})"
    )

    # Group tasks by phase
    tasks_by_phase: dict[Any, list[dict[str, Any]]] = {}
    for task in tasks:
        phase_key = task.get("phase")
        tasks_by_phase.setdefault(phase_key, []).append(task)

    for phase in phases:
        pid = phase.get("id")
        deps = phase.get("dependsOn", [])
        dep_str = f" ← {deps}" if deps else ""
        formatter.console.print(
            f"\n  [cyan bold]Phase {escape(str(pid))}: {escape(phase.get('name', ''))}[/cyan bold]"
            f"[dim]{escape(dep_str)}[/dim]"
        )
        for task in tasks_by_phase.get(pid, []):
            t_info = task.get("task") or {}
            comp = t_info.get("componentId", task.get("componentId", ""))
            cfg = t_info.get("configId", task.get("configId", ""))
            enabled = "" if task.get("enabled", True) else " [dim](disabled)[/dim]"
            formatter.console.print(
                f"    [{escape(str(task.get('id', '?')))}] {escape(task.get('name', ''))}"
                f"  [dim]{escape(comp)}/{escape(str(cfg))}[/dim]{enabled}"
            )

    # Orphan tasks (phase not found in phases list)
    orphan_phase_keys = set(tasks_by_phase.keys()) - {p.get("id") for p in phases}
    for key in sorted(str(k) for k in orphan_phase_keys):
        formatter.console.print(f"\n  [yellow]Phase '{key}' (not in phases list)[/yellow]")
        for task in tasks_by_phase.get(key, []):
            formatter.console.print(f"    {escape(task.get('name', str(task)))}")


# ---------------------------------------------------------------------------
# flow schema
# ---------------------------------------------------------------------------


@flow_app.command("schema")
def flow_schema(ctx: typer.Context) -> None:
    """Print the YAML format expected by 'flow new' and 'flow update'."""
    formatter = get_formatter(ctx)
    if formatter.json_mode:
        formatter.output(
            {
                "format": "yaml",
                "description": "keboola.flow configuration schema",
                "schema": _FLOW_SCHEMA,
            }
        )
    else:
        from rich.syntax import Syntax

        formatter.console.print(Syntax(_FLOW_SCHEMA, "yaml", theme="monokai", line_numbers=False))


# ---------------------------------------------------------------------------
# flow new
# ---------------------------------------------------------------------------


def _load_flow_yaml(raw: str) -> dict[str, Any]:
    """Load flow definition from inline JSON, @file, or - (stdin)."""
    import yaml

    if raw == "-":
        content = sys.stdin.read()
    elif raw.startswith("@"):
        file_path = Path(raw[1:])
        if not file_path.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")
        content = file_path.read_text(encoding="utf-8")
    else:
        content = raw

    # Try YAML first (superset of JSON)
    parsed = yaml.safe_load(content)
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError(
            f"Flow definition must be a YAML/JSON object (mapping), got {type(parsed).__name__}"
        )
    return parsed


@flow_app.command("new")
def flow_new(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    name: str = typer.Option(..., "--name", help="Flow name"),
    component_id: str = typer.Option(
        "keboola.flow",
        "--component-id",
        help="Component ID (default: keboola.flow)",
    ),
    description: str = typer.Option("", "--description", help="Optional description"),
    file: str | None = typer.Option(
        None,
        "--file",
        help="YAML/JSON flow definition (@file, -, or inline). "
        "Run 'kbagent flow schema' to see the expected format.",
    ),
    branch: int | None = typer.Option(None, "--branch", help="Dev branch ID"),
) -> None:
    """Create a new flow configuration.

    \b
    Examples:
      # Empty skeleton
      kbagent flow new --project prod --name "Daily ETL"

      # From a YAML file
      kbagent flow new --project prod --name "Daily ETL" --file @flow.yaml

      # Pipe from stdin
      cat flow.yaml | kbagent flow new --project prod --name "Daily ETL" --file -
    """
    if should_hint(ctx):
        emit_hint(
            ctx,
            "flow.new",
            project=project,
            name=name,
            component_id=component_id,
            branch=branch,
        )

    formatter = get_formatter(ctx)
    service = get_service(ctx, "flow_service")

    phases: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []

    if file:
        try:
            flow_def = _load_flow_yaml(file)
        except (FileNotFoundError, Exception) as exc:
            formatter.error(
                message=f"Cannot load flow definition: {exc}", error_code="VALIDATION_ERROR"
            )
            raise typer.Exit(code=2) from None
        phases = flow_def.get("phases", [])
        tasks = flow_def.get("tasks", [])

    try:
        result = service.create_flow(
            alias=project,
            component_id=component_id,
            name=name,
            description=description,
            phases=phases,
            tasks=tasks,
            branch_id=branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        branch_info = f" (branch {result.get('branch_id')})" if result.get("branch_id") else ""
        formatter.success(
            f"Created flow '{escape(result.get('name', name))}' "
            f"[{escape(component_id)}/{escape(str(result.get('id', '')))}]{branch_info}"
        )


# ---------------------------------------------------------------------------
# flow update
# ---------------------------------------------------------------------------


@flow_app.command("update")
def flow_update(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    flow_id: str = typer.Option(..., "--flow-id", help="Flow configuration ID"),
    component_id: str = typer.Option(
        "keboola.orchestrator",
        "--component-id",
        help="Flow component ID (default: keboola.orchestrator)",
    ),
    name: str | None = typer.Option(None, "--name", help="New flow name"),
    description: str | None = typer.Option(None, "--description", help="New description"),
    file: str | None = typer.Option(
        None,
        "--file",
        help="YAML/JSON flow definition to replace phases + tasks (@file, -, or inline)",
    ),
    branch: int | None = typer.Option(None, "--branch", help="Dev branch ID"),
) -> None:
    """Update a flow's name, description, or phases/tasks.

    \b
    Examples:
      # Rename only
      kbagent flow update --project prod --flow-id 123 --name "New Name"

      # Replace phases + tasks from file
      kbagent flow update --project prod --flow-id 123 --file @flow.yaml
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "flow_service")

    phases: list[dict[str, Any]] | None = None
    tasks: list[dict[str, Any]] | None = None

    if file:
        try:
            flow_def = _load_flow_yaml(file)
        except (FileNotFoundError, Exception) as exc:
            formatter.error(
                message=f"Cannot load flow definition: {exc}", error_code="VALIDATION_ERROR"
            )
            raise typer.Exit(code=2) from None
        phases = flow_def.get("phases")
        tasks = flow_def.get("tasks")

    if name is None and description is None and phases is None and tasks is None:
        formatter.error(
            message="At least one of --name, --description, or --file must be provided.",
            error_code="INVALID_ARGUMENT",
        )
        raise typer.Exit(code=2) from None

    try:
        result = service.update_flow(
            alias=project,
            component_id=component_id,
            config_id=flow_id,
            name=name,
            description=description,
            phases=phases,
            tasks=tasks,
            branch_id=branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        branch_info = f" (branch {result.get('branch_id')})" if result.get("branch_id") else ""
        formatter.success(
            f"Updated flow '{escape(result.get('name', flow_id))}' "
            f"[{escape(component_id)}/{escape(flow_id)}]{branch_info}"
        )


# ---------------------------------------------------------------------------
# flow delete
# ---------------------------------------------------------------------------


@flow_app.command("delete")
def flow_delete(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    flow_id: str = typer.Option(..., "--flow-id", help="Flow configuration ID"),
    component_id: str = typer.Option(
        "keboola.orchestrator",
        "--component-id",
        help="Flow component ID (default: keboola.orchestrator)",
    ),
    branch: int | None = typer.Option(None, "--branch", help="Dev branch ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Delete a flow configuration.

    Note: associated keboola.scheduler configs are NOT automatically removed.
    Run 'flow schedule-remove' first if you want to clean up schedules.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "flow_service")

    if not yes and not formatter.json_mode:
        confirmed = typer.confirm(f"Delete flow {component_id}/{flow_id}?")
        if not confirmed:
            formatter.console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(code=0)

    try:
        result = service.delete_flow(
            alias=project,
            component_id=component_id,
            config_id=flow_id,
            branch_id=branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        formatter.success(f"Deleted flow {escape(component_id)}/{escape(flow_id)}")


# ---------------------------------------------------------------------------
# flow schedule
# ---------------------------------------------------------------------------


@flow_app.command("schedule")
def flow_schedule(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    flow_id: str = typer.Option(..., "--flow-id", help="Flow configuration ID"),
    component_id: str = typer.Option(
        "keboola.orchestrator",
        "--component-id",
        help="Flow component ID (default: keboola.orchestrator)",
    ),
    cron: str = typer.Option(..., "--cron", help="Cron expression (e.g. '0 6 * * *')"),
    timezone: str = typer.Option("UTC", "--timezone", help="IANA timezone (default: UTC)"),
    enabled: bool = typer.Option(True, "--enabled/--disabled", help="Enable the schedule"),
    schedule_name: str | None = typer.Option(
        None, "--name", help="Name for the scheduler config (auto-generated if omitted)"
    ),
    branch: int | None = typer.Option(None, "--branch", help="Dev branch ID"),
) -> None:
    """Bind a cron schedule to a flow (upsert: creates or updates).

    If no schedule exists for this flow a new keboola.scheduler config is
    created. If one already exists it is updated in-place — calling this
    command a second time will not create duplicates.

    \b
    Examples:
      # Run daily at 6am UTC
      kbagent flow schedule --project prod --flow-id 123 --cron "0 6 * * *"

      # Run hourly, disabled by default
      kbagent flow schedule --project prod --flow-id 123 --cron "0 * * * *" --disabled
    """
    if should_hint(ctx):
        emit_hint(
            ctx,
            "flow.schedule",
            project=project,
            flow_id=flow_id,
            component_id=component_id,
            cron=cron,
            branch=branch,
        )

    formatter = get_formatter(ctx)
    service = get_service(ctx, "flow_service")

    try:
        result = service.set_flow_schedule(
            alias=project,
            component_id=component_id,
            config_id=flow_id,
            cron_tab=cron,
            timezone=timezone,
            enabled=enabled,
            schedule_name=schedule_name,
            branch_id=branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        state_label = "[green]enabled[/green]" if enabled else "[yellow]disabled[/yellow]"
        action = result.get("status", "created")
        formatter.success(f"Schedule {action}: {escape(cron)} ({escape(timezone)}) — {state_label}")
        formatter.console.print(
            f"  Scheduler config: {escape(result.get('schedule_name', ''))} "
            f"[dim](ID: {escape(result.get('schedule_id', ''))})[/dim]"
        )


# ---------------------------------------------------------------------------
# flow schedule-remove
# ---------------------------------------------------------------------------


@flow_app.command("schedule-remove")
def flow_schedule_remove(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    flow_id: str = typer.Option(..., "--flow-id", help="Flow configuration ID"),
    component_id: str = typer.Option(
        "keboola.orchestrator",
        "--component-id",
        help="Flow component ID (default: keboola.orchestrator)",
    ),
    branch: int | None = typer.Option(None, "--branch", help="Dev branch ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Remove all schedules bound to a flow (deletes keboola.scheduler configs).

    Idempotent: safe to run even if no schedules exist.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "flow_service")

    # Show existing schedules before confirming
    if not yes and not formatter.json_mode:
        try:
            sched_result = service.list_flow_schedules(
                alias=project,
                component_id=component_id,
                config_id=flow_id,
                branch_id=branch,
            )
            schedules = sched_result.get("schedules", [])
        except Exception:
            schedules = []

        if not schedules:
            formatter.console.print("[dim]No schedules found for this flow.[/dim]")
            raise typer.Exit(code=0)

        for s in schedules:
            formatter.console.print(
                f"  [{escape(s.get('state', ''))}] {escape(s.get('cron_tab', ''))} "
                f"({escape(s.get('timezone', ''))})  ID={escape(s.get('schedule_id', ''))}"
            )
        confirmed = typer.confirm(f"Remove {len(schedules)} schedule(s) above?")
        if not confirmed:
            formatter.console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(code=0)

    try:
        result = service.remove_flow_schedule(
            alias=project,
            component_id=component_id,
            config_id=flow_id,
            branch_id=branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code="CONFIG_ERROR")
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        count = result.get("deleted_count", 0)
        if count == 0:
            formatter.console.print("[dim]No schedules found — nothing removed.[/dim]")
        else:
            formatter.success(f"Removed {count} schedule(s) from flow {escape(flow_id)}")
