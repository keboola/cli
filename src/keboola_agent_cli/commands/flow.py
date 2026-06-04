"""Flow commands -- list, detail, new, update, delete, schedule, schedule-remove, schema.

Thin CLI layer: parses arguments, calls FlowService, formats output.
No business logic belongs here.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.markup import escape
from rich.syntax import Syntax
from rich.table import Table

from ..errors import ConfigError, ErrorCode, KeboolaApiError
from ..services.flow_validation import find_unreachable_phases, validate_conditional_flow
from ._helpers import (
    check_cli_permission,
    get_formatter,
    get_service,
    map_error_to_exit_code,
    resolve_branch,
)

logger = logging.getLogger(__name__)

flow_app = typer.Typer(help="Manage conditional flows (keboola.flow)")

# YAML template shown by 'flow schema' (keboola.flow / Conditional Flow).
#
# IDs are STRINGS. goto is a phase id or null (= end the flow). A phase with
# conditional transitions must end with a default (condition-less) transition.
_FLOW_SCHEMA = """\
# kbagent flow schema -- keboola.flow (Conditional Flow) configuration format
#
# Create with: kbagent flow new --project ALIAS --name "My Flow" --file @flow.yaml
# Update with: kbagent flow update --project ALIAS --flow-id ID --file @flow.yaml
# Validate offline: kbagent flow validate --file @flow.yaml
# Full JSON schema: kbagent flow schema --full
#
# IDs are STRINGS. goto is a phase id or null (= end the flow).

phases:
  - id: "extract"
    name: "Extract"
    next:
      # Conditional transition: if any task in 'extract' failed, go to 'notify'.
      - id: "on-failure"
        goto: "notify"
        condition:
          type: operator
          operator: ANY_TASKS_IN_PHASE
          phase: "extract"
          operands: []
      # Default transition (NO condition) -- MUST be last.
      - id: "default"
        goto: "transform"
  - id: "transform"
    name: "Transform"
    retry:
      strategy: linear
      strategyParams:
        delaySeconds: 60
      retryOn: ["error"]
    next:
      - id: "done"
        goto: null
  - id: "notify"
    name: "Notify on failure"

tasks:
  - id: "task-extract"
    name: "Run HTTP extractor"
    phase: "extract"
    enabled: true
    task:
      type: job
      componentId: "keboola.ex-http"
      configId: "123456789"
      mode: run
      retry:
        strategy: linear
        strategyParams:
          delaySeconds: 30
        retryOn: ["error"]
  - id: "task-transform"
    name: "Run transformation"
    phase: "transform"
    enabled: true
    task:
      type: job
      componentId: "keboola.snowflake-transformation"
      configId: "987654321"
      mode: run
  - id: "task-notify"
    name: "Email the team"
    phase: "notify"
    enabled: true
    task:
      type: notification
      title: "Flow failed"
      message: "The extract phase reported a failure."
      recipients:
        - channel: email
          address: "team@example.com"
  - id: "task-setvar"
    name: "Set a flow variable"
    phase: "extract"
    enabled: true
    task:
      type: variable
      name: "run_date"
      value: "2026-01-01"
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
    with_schedules: bool = typer.Option(
        False,
        "--with-schedules",
        help="Enrich each flow row with the cron schedules that target it "
        "(one extra API call per project, NOT per flow).",
    ),
) -> None:
    """List conditional flows (keboola.flow) across projects.

    Legacy keboola.orchestrator flows are NOT listed (orchestrator support was
    dropped in 0.57.0); a count of any that exist is shown as a warning.

    With ``--with-schedules`` each row includes a ``schedules`` list of
    ``{schedule_id, cron, timezone, enabled}`` entries. Flows without
    any schedule get ``schedules=[]``.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "flow_service")
    config_store = ctx.obj["config_store"]

    if branch is not None and (not project or len(project) != 1):
        formatter.error(
            message="--branch requires exactly one --project",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2)

    effective_branch: int | None = branch
    if branch is None and project and len(project) == 1:
        _, effective_branch = resolve_branch(config_store, formatter, project[0], None)

    try:
        result = service.list_flows(
            aliases=project,
            branch_id=effective_branch,
            with_schedules=with_schedules,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        _format_flows_table(formatter, result, with_schedules=with_schedules)


def _format_flows_table(
    formatter: Any, result: dict[str, Any], *, with_schedules: bool = False
) -> None:
    flows = result.get("flows", [])
    errors = result.get("errors", [])

    if not flows:
        formatter.console.print("[dim]No flows found.[/dim]")
    else:
        columns = ["Project", "Config ID", "Name", "Disabled"]
        if with_schedules:
            columns.append("Schedules")
        tbl = Table(
            *columns,
            show_header=True,
            header_style="bold cyan",
        )
        for f in flows:
            disabled = "[red]yes[/red]" if f.get("is_disabled") else "[dim]no[/dim]"
            row = [
                escape(f.get("project_alias", "")),
                escape(f.get("config_id", "")),
                escape(f.get("name", "")),
                disabled,
            ]
            if with_schedules:
                schedules = f.get("schedules") or []
                if not schedules:
                    row.append("[dim]-[/dim]")
                else:
                    lines = []
                    for s in schedules:
                        tag = "[green]on[/green]" if s.get("enabled") else "[yellow]off[/yellow]"
                        lines.append(
                            f"{escape(s.get('cron', ''))} ({escape(s.get('timezone', ''))}) {tag}"
                        )
                    row.append("\n".join(lines))
            tbl.add_row(*row)
        formatter.console.print(tbl)

    for err in errors:
        formatter.warning(
            f"Project '{err.get('project_alias', '?')}': {err.get('message', 'error')}"
        )

    legacy = result.get("legacy_orchestrator_count", 0)
    if legacy:
        formatter.warning(
            f"{legacy} legacy flow(s) are not shown "
            f"(Legacy Flows were dropped in 0.57.0; migrate them to Conditional Flows)."
        )


# ---------------------------------------------------------------------------
# flow detail
# ---------------------------------------------------------------------------


@flow_app.command("detail")
def flow_detail(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    flow_id: str = typer.Option(..., "--flow-id", help="Flow configuration ID"),
    branch: int | None = typer.Option(None, "--branch", help="Dev branch ID"),
) -> None:
    """Show detailed conditional-flow information including phases and tasks."""
    formatter = get_formatter(ctx)
    service = get_service(ctx, "flow_service")
    config_store = ctx.obj["config_store"]
    _, effective_branch = resolve_branch(config_store, formatter, project, branch)

    try:
        result = service.get_flow_detail(
            alias=project,
            config_id=flow_id,
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
    else:
        _format_flow_detail(formatter, result)


def _summarize_condition(condition: dict[str, Any] | None) -> str:
    """One-line human summary of a transition condition."""
    if not condition:
        return "default"
    ctype = condition.get("type")
    if ctype == "operator":
        op = condition.get("operator", "?")
        phase = condition.get("phase")
        return f"{op}({phase})" if phase else f"{op}(...)"
    if ctype == "function":
        return f"{condition.get('function', '?')}(...)"
    if ctype in ("const", "constant"):
        return f"const={condition.get('value')!r}"
    return str(ctype)


def _format_flow_detail(formatter: Any, result: dict[str, Any]) -> None:
    formatter.console.print(
        f"\n[bold]{escape(result.get('name', ''))}[/bold]"
        f"  [dim](keboola.flow / {escape(str(result.get('id', '')))})[/dim]"
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

    tasks_by_phase: dict[Any, list[dict[str, Any]]] = {}
    for task in tasks:
        tasks_by_phase.setdefault(str(task.get("phase")), []).append(task)

    type_colors = {"job": "green", "notification": "yellow", "variable": "magenta"}

    for phase in phases:
        pid = str(phase.get("id"))
        retry = " [dim](retry)[/dim]" if phase.get("retry") else ""
        formatter.console.print(
            f"\n  [cyan bold]Phase {escape(pid)}: {escape(phase.get('name', ''))}[/cyan bold]{retry}"
        )
        for transition in phase.get("next", []):
            goto = transition.get("goto")
            target = "END" if goto is None else str(goto)
            summary = _summarize_condition(transition.get("condition"))
            formatter.console.print(f"      [dim]→ {escape(target)} \\[{escape(summary)}][/dim]")
        for task in tasks_by_phase.get(pid, []):
            t_info = task.get("task") or {}
            ttype = t_info.get("type", "?")
            color = type_colors.get(ttype, "white")
            badge = f"[{color}]{escape(ttype)}[/{color}]"
            detail_str = ""
            if ttype == "job":
                detail_str = (
                    f" {escape(str(t_info.get('componentId', '')))}"
                    f"/{escape(str(t_info.get('configId', '')))}"
                )
            elif ttype == "variable":
                detail_str = f" {escape(str(t_info.get('name', '')))}"
            t_retry = " [dim](retry)[/dim]" if t_info.get("retry") else ""
            enabled = "" if task.get("enabled", True) else " [dim](disabled)[/dim]"
            formatter.console.print(
                f"    \\[{escape(str(task.get('id', '?')))}] {badge} "
                f"{escape(task.get('name', ''))}[dim]{detail_str}[/dim]{enabled}{t_retry}"
            )

    orphan_keys = set(tasks_by_phase.keys()) - {str(p.get("id")) for p in phases}
    for key in sorted(orphan_keys):
        formatter.console.print(f"\n  [yellow]Phase '{key}' (not in phases list)[/yellow]")
        for task in tasks_by_phase.get(key, []):
            formatter.console.print(f"    {escape(task.get('name', str(task)))}")


# ---------------------------------------------------------------------------
# flow schema
# ---------------------------------------------------------------------------


@flow_app.command("schema")
def flow_schema(
    ctx: typer.Context,
    full: bool = typer.Option(
        False,
        "--full",
        help="Dump the live JSON Schema fetched from the stack (requires --project).",
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Project alias -- required for --full (the schema is served by the stack).",
    ),
) -> None:
    """Print the conditional-flow YAML template, or --full for the live JSON Schema.

    The plain template is offline. ``--full`` fetches the real keboola.flow
    JSON Schema from the stack's component registry, so it needs ``--project``.
    """
    formatter = get_formatter(ctx)
    if full:
        if not project:
            formatter.error(
                message=(
                    "--full requires --project: the conditional-flow JSON Schema is "
                    "served by the stack's component registry, not bundled. "
                    "Run e.g. 'kbagent flow schema --full --project ALIAS'."
                ),
                error_code=ErrorCode.VALIDATION_ERROR,
            )
            raise typer.Exit(code=2)

        service = get_service(ctx, "flow_service")
        try:
            fetch = service.fetch_flow_schema(project)
        except ConfigError as exc:
            formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
            raise typer.Exit(code=5) from None
        except KeboolaApiError as exc:
            formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
            raise typer.Exit(code=map_error_to_exit_code(exc)) from None

        schema, reason = fetch.schema, fetch.reason
        if schema is None:
            formatter.error(
                message=f"Could not fetch the conditional-flow schema: {reason}",
                error_code=ErrorCode.NOT_FOUND,
            )
            raise typer.Exit(code=4)

        if formatter.json_mode:
            formatter.output({"format": "json-schema", "schema": schema})
        else:
            formatter.console.print(
                Syntax(json.dumps(schema, indent=2), "json", theme="monokai", line_numbers=False)
            )
        return

    if formatter.json_mode:
        formatter.output(
            {
                "format": "yaml",
                "description": "keboola.flow (Conditional Flow) configuration schema",
                "schema": _FLOW_SCHEMA,
            }
        )
    else:
        formatter.console.print(Syntax(_FLOW_SCHEMA, "yaml", theme="monokai", line_numbers=False))


# ---------------------------------------------------------------------------
# flow validate
# ---------------------------------------------------------------------------


@flow_app.command("validate")
def flow_validate(
    ctx: typer.Context,
    file: str = typer.Option(
        ...,
        "--file",
        help="YAML/JSON flow definition to validate (@file, -, or inline).",
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help=(
            "Project alias -- fetch the live JSON Schema from the stack for full "
            "structural + semantic validation. Without it, only semantic checks run."
        ),
    ),
) -> None:
    """Validate a conditional-flow definition (schema + semantic checks).

    With ``--project`` the live keboola.flow JSON Schema is fetched from the
    stack and structural validation runs alongside the semantic checks; a fetch
    failure degrades gracefully (semantic-only + a warning). Without
    ``--project`` only the semantic checks run and a note records that
    structural schema validation was skipped (no schema source).

    Exit 0 when valid (warnings still printed), exit 2 when there are errors.
    """
    formatter = get_formatter(ctx)

    try:
        flow_def = _load_flow_yaml(file)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        formatter.error(
            message=f"Cannot load flow definition: {exc}", error_code=ErrorCode.VALIDATION_ERROR
        )
        raise typer.Exit(code=2) from None

    phases = flow_def.get("phases", [])
    tasks = flow_def.get("tasks", [])

    schema: dict[str, Any] | None = None
    notes: list[str] = []
    if project:
        service = get_service(ctx, "flow_service")
        try:
            fetch = service.fetch_flow_schema(project)
        except ConfigError as exc:
            formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
            raise typer.Exit(code=5) from None
        schema = fetch.schema
        if schema is None:
            notes.append(f"structural schema validation skipped: {fetch.reason}")
    else:
        notes.append(
            "structural schema validation skipped: no schema source "
            "(pass --project ALIAS to fetch the live schema from the stack)"
        )

    errors = validate_conditional_flow(phases, tasks, schema)
    warnings = [
        f"Phase '{pid}' is unreachable from the entry phase"
        for pid in find_unreachable_phases(phases)
    ]
    valid = not errors

    if formatter.json_mode:
        formatter.output({"valid": valid, "errors": errors, "warnings": warnings, "notes": notes})
    else:
        for note in notes:
            formatter.console.print(f"[dim]note: {escape(note)}[/dim]")
        for w in warnings:
            formatter.warning(w)
        if valid:
            formatter.success("Flow definition is valid.")
        else:
            for e in errors:
                formatter.console.print(f"[red]✗[/red] {escape(e)}")
    if not valid:
        raise typer.Exit(code=2)


# ---------------------------------------------------------------------------
# flow new
# ---------------------------------------------------------------------------


def _load_flow_yaml(raw: str) -> dict[str, Any]:
    """Load flow definition from inline JSON, @file, or - (stdin)."""
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
    description: str = typer.Option("", "--description", help="Optional description"),
    file: str | None = typer.Option(
        None,
        "--file",
        help="YAML/JSON flow definition (@file, -, or inline). "
        "Run 'kbagent flow schema' to see the expected format.",
    ),
    branch: int | None = typer.Option(None, "--branch", help="Dev branch ID"),
) -> None:
    """Create a new conditional-flow (keboola.flow) configuration.

    \b
    Examples:
      # From a YAML file
      kbagent flow new --project prod --name "Daily ETL" --file @flow.yaml

      # Pipe from stdin
      cat flow.yaml | kbagent flow new --project prod --name "Daily ETL" --file -
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "flow_service")

    phases: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []

    if file:
        try:
            flow_def = _load_flow_yaml(file)
        except (OSError, yaml.YAMLError, ValueError) as exc:
            formatter.error(
                message=f"Cannot load flow definition: {exc}", error_code=ErrorCode.VALIDATION_ERROR
            )
            raise typer.Exit(code=2) from None
        phases = flow_def.get("phases", [])
        tasks = flow_def.get("tasks", [])

    try:
        result = service.create_flow(
            alias=project,
            name=name,
            description=description,
            phases=phases,
            tasks=tasks,
            branch_id=branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
            f"[keboola.flow/{escape(str(result.get('id', '')))}]{branch_info}"
        )
        for warning in result.get("warnings", []):
            formatter.warning(warning)


# ---------------------------------------------------------------------------
# flow update
# ---------------------------------------------------------------------------


@flow_app.command("update")
def flow_update(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    flow_id: str = typer.Option(..., "--flow-id", help="Flow configuration ID"),
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
        except (OSError, yaml.YAMLError, ValueError) as exc:
            formatter.error(
                message=f"Cannot load flow definition: {exc}", error_code=ErrorCode.VALIDATION_ERROR
            )
            raise typer.Exit(code=2) from None
        phases = flow_def.get("phases")
        tasks = flow_def.get("tasks")

    if name is None and description is None and phases is None and tasks is None:
        formatter.error(
            message="At least one of --name, --description, or --file must be provided.",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2) from None

    try:
        result = service.update_flow(
            alias=project,
            config_id=flow_id,
            name=name,
            description=description,
            phases=phases,
            tasks=tasks,
            branch_id=branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
            f"[keboola.flow/{escape(flow_id)}]{branch_info}"
        )
        for warning in result.get("warnings", []):
            formatter.warning(warning)


# ---------------------------------------------------------------------------
# flow delete
# ---------------------------------------------------------------------------


@flow_app.command("delete")
def flow_delete(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    flow_id: str = typer.Option(..., "--flow-id", help="Flow configuration ID"),
    branch: int | None = typer.Option(None, "--branch", help="Dev branch ID"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be deleted without executing",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Delete a conditional-flow (keboola.flow) configuration.

    Note: associated keboola.scheduler configs are NOT automatically removed.
    Run 'flow schedule-remove' first if you want to clean up schedules.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "flow_service")

    if dry_run:
        result = {
            "would_delete": {
                "project_alias": project,
                "component_id": "keboola.flow",
                "config_id": flow_id,
                "branch_id": branch,
            },
        }
        if formatter.json_mode:
            formatter.output(result)
        else:
            formatter.console.print(
                f"[bold blue]Would delete:[/bold blue] flow keboola.flow/{escape(flow_id)}"
                + (f" (branch {branch})" if branch else "")
            )
        return

    if not yes and not formatter.json_mode:
        confirmed = typer.confirm(f"Delete flow keboola.flow/{flow_id}?")
        if not confirmed:
            formatter.console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(code=0)

    try:
        result = service.delete_flow(
            alias=project,
            config_id=flow_id,
            branch_id=branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None

    if formatter.json_mode:
        formatter.output(result)
    else:
        formatter.success(f"Deleted flow keboola.flow/{escape(flow_id)}")


# ---------------------------------------------------------------------------
# flow schedule
# ---------------------------------------------------------------------------


@flow_app.command("schedule")
def flow_schedule(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    flow_id: str = typer.Option(..., "--flow-id", help="Flow configuration ID"),
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
    formatter = get_formatter(ctx)
    service = get_service(ctx, "flow_service")

    try:
        result = service.set_flow_schedule(
            alias=project,
            config_id=flow_id,
            cron_tab=cron,
            timezone=timezone,
            enabled=enabled,
            schedule_name=schedule_name,
            branch_id=branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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


def _print_schedule_list(formatter: Any, schedules: list[dict[str, Any]]) -> None:
    """Print one line per schedule: state, cron, timezone, id."""
    for s in schedules:
        formatter.console.print(
            f"  [{escape(s.get('state', ''))}] {escape(s.get('cron_tab', ''))} "
            f"({escape(s.get('timezone', ''))})  ID={escape(s.get('schedule_id', ''))}"
        )


@flow_app.command("schedule-remove")
def flow_schedule_remove(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project", help="Project alias"),
    flow_id: str = typer.Option(..., "--flow-id", help="Flow configuration ID"),
    branch: int | None = typer.Option(None, "--branch", help="Dev branch ID"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="List the scheduler configs that would be removed without executing",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Remove all schedules bound to a flow (deletes keboola.scheduler configs).

    Idempotent: safe to run even if no schedules exist.
    """
    formatter = get_formatter(ctx)
    service = get_service(ctx, "flow_service")

    if dry_run:
        try:
            sched_result = service.list_flow_schedules(
                alias=project,
                config_id=flow_id,
                branch_id=branch,
            )
        except ConfigError as exc:
            formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
            raise typer.Exit(code=5) from None
        except KeboolaApiError as exc:
            formatter.error(message=exc.message, error_code=exc.error_code, retryable=exc.retryable)
            raise typer.Exit(code=map_error_to_exit_code(exc)) from None

        schedules = sched_result.get("schedules", [])
        payload = {
            "would_delete": {
                "project_alias": project,
                "component_id": "keboola.flow",
                "config_id": flow_id,
                "branch_id": branch,
                "schedules": schedules,
                "count": len(schedules),
            },
        }
        if formatter.json_mode:
            formatter.output(payload)
        else:
            if not schedules:
                formatter.console.print("[dim]No schedules found for this flow.[/dim]")
            else:
                formatter.console.print(
                    f"[bold blue]Would remove {len(schedules)} schedule(s) "
                    f"from flow[/bold blue] keboola.flow/{escape(flow_id)}:"
                )
                _print_schedule_list(formatter, schedules)
        return

    # Show existing schedules before confirming
    if not yes and not formatter.json_mode:
        try:
            sched_result = service.list_flow_schedules(
                alias=project,
                config_id=flow_id,
                branch_id=branch,
            )
            schedules = sched_result.get("schedules", [])
        except (ConfigError, KeboolaApiError):
            schedules = []

        if not schedules:
            formatter.console.print("[dim]No schedules found for this flow.[/dim]")
            raise typer.Exit(code=0)

        _print_schedule_list(formatter, schedules)
        confirmed = typer.confirm(f"Remove {len(schedules)} schedule(s) above?")
        if not confirmed:
            formatter.console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(code=0)

    try:
        result = service.remove_flow_schedule(
            alias=project,
            config_id=flow_id,
            branch_id=branch,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
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
