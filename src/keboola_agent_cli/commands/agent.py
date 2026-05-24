"""Agent task commands -- CLI parity for the `/agents` REST surface.

Mirrors what ``kbagent serve --ui`` exposes: CRUD over scheduled tasks,
ad-hoc runs (blocking + streaming), run history, cron preview, and an
AI-assisted prompt helper. Reads/writes the same ``agents.json`` the
server scheduler uses, so a CLI-created task fires on cron as soon as
``kbagent serve`` is running.

Thin layer: each command parses arguments, calls AgentService, formats
output. No business logic lives here. Async service methods are bridged
through ``asyncio.run`` at the command boundary so Typer stays sync.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import typer

from ..errors import ConfigError, ErrorCode
from ..output import OutputFormatter
from ..server.agents_store import AgentAction, Trigger
from ..services.agent_service import AgentService
from ._helpers import check_cli_permission, get_formatter, get_service

agent_app = typer.Typer(help="Scheduled agent tasks (cron / manual / chained)")


@agent_app.callback(invoke_without_command=True)
def _agent_permission_check(ctx: typer.Context) -> None:
    check_cli_permission(ctx, "agent")


# ── Shared parsing helpers ────────────────────────────────────────────


def _read_payload(value: str, formatter: OutputFormatter) -> str:
    """Resolve --input style strings: inline JSON, ``@file``, or ``-`` (stdin)."""
    if value == "-":
        return sys.stdin.read()
    if value.startswith("@"):
        path = Path(value[1:])
        if not path.is_file():
            formatter.error(
                message=f"Input file not found: {path}",
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
            raise typer.Exit(code=2) from None
        return path.read_text(encoding="utf-8")
    return value


def _parse_json(value: str, formatter: OutputFormatter, *, label: str) -> dict[str, Any]:
    """Decode a JSON string into a dict, exiting cleanly on bad JSON."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        formatter.error(
            message=f"Invalid JSON in {label}: {exc}",
            error_code=ErrorCode.INVALID_FORMAT,
        )
        raise typer.Exit(code=2) from None
    if not isinstance(parsed, dict):
        formatter.error(
            message=f"{label} must be a JSON object",
            error_code=ErrorCode.INVALID_FORMAT,
        )
        raise typer.Exit(code=2) from None
    return parsed


def _action_from_flags(
    formatter: OutputFormatter,
    *,
    action_type: str | None,
    from_file: str | None,
    cli: str | None,
    prompt: str | None,
    extra_arg: list[str],
    argv: list[str],
    tool: str | None,
    mcp_project: str | None,
    mcp_branch: int | None,
    input_payload: str | None,
    timeout: int | None,
) -> AgentAction:
    """Build an AgentAction from CLI flags or ``--from-file PATH|-``.

    ``--from-file`` wins outright -- the file is expected to contain the
    full ``{type, params}`` envelope, mirroring the REST POST body. The
    convenience flags are for the common "single ai_agent" / "single
    cli_command" / "single mcp_tool" cases where typing a JSON file would
    be overkill.
    """
    if from_file is not None:
        raw = _read_payload(from_file, formatter)
        payload = _parse_json(raw, formatter, label="--from-file")
        try:
            return AgentAction.model_validate(payload)
        except Exception as exc:
            formatter.error(
                message=f"Invalid action payload: {exc}",
                error_code=ErrorCode.VALIDATION_ERROR,
            )
            raise typer.Exit(code=2) from None

    if action_type is None:
        formatter.error(
            message="--type is required (one of: ai_agent, cli_command, mcp_tool) "
            "or pass --from-file PATH|- with a full {type, params} JSON.",
            error_code=ErrorCode.MISSING_PARAMETER,
        )
        raise typer.Exit(code=2) from None

    if action_type == "ai_agent":
        if not cli or not prompt:
            formatter.error(
                message="ai_agent action requires --cli {claude|codex|gemini} and --prompt TEXT.",
                error_code=ErrorCode.MISSING_PARAMETER,
            )
            raise typer.Exit(code=2) from None
        params: dict[str, Any] = {"cli": cli, "prompt": prompt}
        if extra_arg:
            params["extra_args"] = list(extra_arg)
        if timeout is not None:
            params["timeout"] = timeout
        return AgentAction(type="ai_agent", params=params)

    if action_type == "cli_command":
        if not argv:
            formatter.error(
                message="cli_command action requires --argv ARG (repeatable) -- e.g. "
                "--argv job --argv list --argv --project=padak.",
                error_code=ErrorCode.MISSING_PARAMETER,
            )
            raise typer.Exit(code=2) from None
        params: dict[str, Any] = {"argv": list(argv)}
        if timeout is not None:
            params["timeout"] = timeout
        return AgentAction(type="cli_command", params=params)

    if action_type == "mcp_tool":
        if not tool:
            formatter.error(
                message="mcp_tool action requires --tool NAME.",
                error_code=ErrorCode.MISSING_PARAMETER,
            )
            raise typer.Exit(code=2) from None
        params: dict[str, Any] = {"tool": tool}
        if mcp_project:
            params["project"] = mcp_project
        if mcp_branch is not None:
            params["branch_id"] = mcp_branch
        if input_payload:
            raw = _read_payload(input_payload, formatter)
            params["input"] = _parse_json(raw, formatter, label="--input")
        return AgentAction(type="mcp_tool", params=params)

    formatter.error(
        message=f"Unknown --type {action_type!r}; expected ai_agent|cli_command|mcp_tool.",
        error_code=ErrorCode.INVALID_ARGUMENT,
    )
    raise typer.Exit(code=2) from None


def _trigger_from_flags(
    trigger_task_id: str | None,
    trigger_on: str,
) -> Trigger | None:
    """Build a Trigger from --trigger-task-id / --trigger-on flags."""
    if not trigger_task_id:
        return None
    # ty: trigger_on is a plain str here; Trigger.on is a Literal[success|error|always].
    # The value is constrained to that set by the CLI/REST boundary before reaching here.
    return Trigger(on=trigger_on, task_id=trigger_task_id)  # ty: ignore[invalid-argument-type]


def _resolve_id(
    formatter: OutputFormatter,
    positional: str | None,
    option: str | None,
    *,
    label: str,
    flag: str,
) -> str:
    """Resolve an ID passed either positionally or via a named flag.

    Every agent subcommand accepts its task/run ID both ways: positionally
    (``agent show TASK_ID``) for terse interactive use, and via a named flag
    (``--id`` / ``--task-id`` / ``--run-id``) for consistency with the rest of
    the CLI, which identifies entities by flag everywhere else (``--job-id``,
    ``--config-id``, ``--app-id``, ...). Exactly one must be supplied; passing
    both with conflicting values is a usage error rather than a silent pick.
    """
    if positional is not None and option is not None and positional != option:
        formatter.error(
            message=f"{label} given both positionally ({positional!r}) and via {flag} "
            f"({option!r}) -- pass it only one way.",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        raise typer.Exit(code=2) from None
    resolved = positional if positional is not None else option
    if not resolved:
        formatter.error(
            message=f"{label} is required: pass it positionally or via {flag}.",
            error_code=ErrorCode.MISSING_PARAMETER,
        )
        raise typer.Exit(code=2) from None
    return resolved


# ── Output renderers ──────────────────────────────────────────────────


def _render_tasks_table(console: Any, data: dict[str, Any]) -> None:
    """Plain-text table of tasks: id / name / cron / state."""
    tasks = data.get("tasks") or []
    if not tasks:
        console.print("[dim]No agent tasks registered.[/dim]")
        return
    from rich.table import Table

    table = Table(title="Agent Tasks", show_lines=False)
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Schedule")
    table.add_column("Type")
    table.add_column("State")
    table.add_column("Last run", style="dim")
    table.add_column("Next run", style="dim")
    for task in tasks:
        state_bits = []
        if not task.get("enabled", True):
            state_bits.append("[yellow]disabled[/yellow]")
        if task.get("manual"):
            state_bits.append("[blue]manual[/blue]")
        elif task.get("enabled", True):
            state_bits.append("[green]enabled[/green]")
        state = " ".join(state_bits) or "-"
        action = task.get("action") or {}
        table.add_row(
            str(task.get("id", "")),
            task.get("name", ""),
            "" if task.get("manual") else task.get("cron", "") or "-",
            action.get("type", "?"),
            state,
            task.get("last_run_at") or "-",
            task.get("next_run_at") or "-",
        )
    console.print(table)


def _render_task_detail(console: Any, task: dict[str, Any]) -> None:
    """Pretty single-task panel with the action payload pretty-printed."""
    from rich.panel import Panel
    from rich.syntax import Syntax

    header_lines = [
        f"[cyan]{task.get('id', '')}[/cyan]  [bold]{task.get('name', '')}[/bold]",
    ]
    if task.get("description"):
        header_lines.append(task["description"])
    state_bits = []
    if not task.get("enabled", True):
        state_bits.append("[yellow]disabled[/yellow]")
    if task.get("manual"):
        state_bits.append("[blue]manual[/blue]")
    else:
        state_bits.append(f"cron=[green]{task.get('cron', '')}[/green]")
    header_lines.append("State: " + " ".join(state_bits))
    if task.get("last_run_at"):
        header_lines.append(f"Last run: [dim]{task['last_run_at']}[/dim]")
    if task.get("next_run_at"):
        header_lines.append(f"Next run: [dim]{task['next_run_at']}[/dim]")
    if task.get("trigger"):
        trig = task["trigger"]
        header_lines.append(
            f"Trigger: on=[magenta]{trig.get('on')}[/magenta] -> {trig.get('task_id')}"
        )
    action_json = json.dumps(task.get("action") or {}, indent=2, ensure_ascii=False)
    console.print(Panel("\n".join(header_lines), title="Task", border_style="blue"))
    console.print(Syntax(action_json, "json", theme="ansi_dark", word_wrap=True))


def _render_created_task(console: Any, task: dict[str, Any]) -> None:
    """Confirmation line + full detail panel after `agent create`."""
    console.print(f"[bold green]Created[/bold green] task [cyan]{task['id']}[/cyan]")
    _render_task_detail(console, task)


def _render_updated_task(console: Any, task: dict[str, Any]) -> None:
    """Confirmation line + full detail panel after `agent update`."""
    console.print(f"[bold green]Updated[/bold green] task [cyan]{task['id']}[/cyan]")
    _render_task_detail(console, task)


def _render_deleted_task(console: Any, data: dict[str, Any]) -> None:
    """Confirmation line after `agent delete`."""
    console.print(f"[bold green]Deleted[/bold green] task [cyan]{data['id']}[/cyan]")


def _render_run_result(console: Any, run: dict[str, Any]) -> None:
    """One-line run status + timing after `agent run`."""
    status = run.get("status")
    status_styled = f"[green]{status}[/green]" if status == "ok" else f"[red]{status}[/red]"
    console.print(f"[bold]Run[/bold] [cyan]{run['run_id']}[/cyan] status={status_styled}")
    console.print(f"started: [dim]{run['started_at']}[/dim]")
    console.print(f"ended:   [dim]{run.get('ended_at') or '-'}[/dim]")
    if run.get("error"):
        console.print(f"[red]error:[/red] {run['error']}")


def _render_test_result(console: Any, run: dict[str, Any]) -> None:
    """Ad-hoc `agent test` preview: status + optional output / error."""
    console.print(f"[bold]Preview[/bold] status={run.get('status')}")
    if run.get("output"):
        console.print(json.dumps(run["output"], indent=2, ensure_ascii=False))
    if run.get("error"):
        console.print(f"[red]error:[/red] {run['error']}")


def _render_improved_prompt(console: Any, data: dict[str, Any]) -> None:
    """`agent prompt-improve` result: header + the cleaned prompt body."""
    console.print(f"[bold]Cleaned prompt[/bold] (status={data.get('status', '?')}):")
    console.print(data.get("prompt") or "[dim]<empty>[/dim]")


def _render_runs_table(console: Any, data: dict[str, Any]) -> None:
    runs = data.get("runs") or []
    if not runs:
        console.print("[dim]No run history.[/dim]")
        return
    from rich.table import Table

    table = Table(title="Run history", show_lines=False)
    table.add_column("Run ID", style="cyan")
    table.add_column("Started", style="dim")
    table.add_column("Ended", style="dim")
    table.add_column("Status")
    table.add_column("Summary")
    for run in runs:
        status = run.get("status", "?")
        status_styled = (
            f"[green]{status}[/green]"
            if status == "ok"
            else f"[red]{status}[/red]"
            if status == "error"
            else f"[yellow]{status}[/yellow]"
        )
        summary = ""
        if run.get("error"):
            summary = f"[red]{run['error'][:80]}[/red]"
        elif run.get("summary"):
            s = run["summary"]
            bits = []
            if s.get("model"):
                bits.append(str(s["model"]))
            if s.get("tokens"):
                bits.append(f"tokens={s['tokens']}")
            if s.get("cost_usd") is not None:
                bits.append(f"${s['cost_usd']:.4f}")
            summary = " ".join(bits)
        table.add_row(
            run.get("run_id", ""),
            run.get("started_at") or "",
            run.get("ended_at") or "-",
            status_styled,
            summary,
        )
    console.print(table)


def _render_stream_event(formatter: OutputFormatter, evt: dict[str, Any]) -> None:
    """Single event renderer for ``--stream`` mode.

    JSON: one NDJSON line per event (the same shape SSE consumers see).
    Human: short labeled line per event; the ``done`` event gets a
    multi-line summary with exit code, elapsed time, response preview.
    """
    if formatter.json_mode:
        sys.stdout.write(json.dumps(evt, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return
    event = evt.get("event", "?")
    data = evt.get("data") or {}
    console = formatter.console
    if event == "init":
        info_bits = ["[bold blue]Init[/bold blue]"]
        for key in ("name", "task_id", "action_type", "cli"):
            if data.get(key):
                info_bits.append(f"{key}=[cyan]{data[key]}[/cyan]")
        console.print(" ".join(info_bits))
        return
    if event == "stdout":
        # claude JSONL: try to extract the most useful field
        if isinstance(data, dict) and "type" in data and "raw" not in data:
            kind = data.get("type")
            if kind == "assistant":
                content = data.get("message", {}).get("content") or []
                texts = [
                    c.get("text", "")
                    for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                ]
                if texts:
                    console.print(f"[bold]assistant:[/bold] {' '.join(texts)}")
                else:
                    tool_uses = [
                        c.get("name")
                        for c in content
                        if isinstance(c, dict) and c.get("type") == "tool_use"
                    ]
                    if tool_uses:
                        console.print(
                            f"[magenta]tool_use:[/magenta] {', '.join(filter(None, tool_uses))}"
                        )
                    else:
                        console.print("[dim]event=assistant (no text)[/dim]")
            elif kind == "result":
                # final result line carries the summary text
                pass
            elif kind:
                console.print(f"[dim]event={kind}[/dim]")
            return
        # codex/gemini raw lines or unparseable json
        raw = data.get("raw") if isinstance(data, dict) else None
        if raw:
            console.print(raw)
        return
    if event == "stderr":
        raw = data.get("raw") if isinstance(data, dict) else str(data)
        console.print(f"[dim red]{raw}[/dim red]")
        return
    if event == "done":
        status = data.get("status", "?")
        status_styled = (
            "[green]ok[/green]"
            if status == "ok"
            else "[red]error[/red]"
            if status == "error"
            else f"[yellow]{status}[/yellow]"
        )
        bits = [f"[bold]Done[/bold] status={status_styled}"]
        if data.get("exit_code") is not None:
            bits.append(f"exit_code={data['exit_code']}")
        if data.get("elapsed_seconds") is not None:
            bits.append(f"elapsed={data['elapsed_seconds']}s")
        console.print(" ".join(bits))
        if data.get("error"):
            console.print(f"[red]error:[/red] {data['error']}")
        if data.get("response"):
            preview = str(data["response"])[:500]
            console.print(f"[dim]response preview:[/dim] {preview}")
        return
    # Unknown event type; dump as-is
    console.print(f"[dim]{event}:[/dim] {json.dumps(data, ensure_ascii=False)[:200]}")


def _stream_to_stdout(formatter: OutputFormatter, agen: AsyncIterator[dict[str, Any]]) -> None:
    """Drive an async event generator from sync code and render each event."""

    async def _drive() -> None:
        async for evt in agen:
            _render_stream_event(formatter, evt)

    try:
        asyncio.run(_drive())
    except KeyboardInterrupt:
        # The async generator's finally block kills any spawned subprocess.
        # Print a short marker so the user knows the partial output is theirs.
        if not formatter.json_mode:
            formatter.console.print("[yellow]Interrupted by user.[/yellow]")
        raise typer.Exit(code=130) from None


# ── Commands ───────────────────────────────────────────────────────────


@agent_app.command("list")
def agent_list(ctx: typer.Context) -> None:
    """List all registered agent tasks."""
    formatter = get_formatter(ctx)
    service: AgentService = get_service(ctx, "agent_service")
    try:
        tasks = service.list_tasks()
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    payload = {"tasks": [t.model_dump(mode="json") for t in tasks]}
    formatter.output(payload, _render_tasks_table)


@agent_app.command("show")
def agent_show(
    ctx: typer.Context,
    task_id: str | None = typer.Argument(None, help="Task ID (12-char hex). Or use --id."),
    task_id_opt: str | None = typer.Option(
        None, "--id", "--task-id", help="Task ID (alias for the positional argument)."
    ),
) -> None:
    """Show one task's full configuration."""
    formatter = get_formatter(ctx)
    service: AgentService = get_service(ctx, "agent_service")
    task_id = _resolve_id(formatter, task_id, task_id_opt, label="Task ID", flag="--id/--task-id")
    try:
        task = service.get_task(task_id)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.NOT_FOUND)
        raise typer.Exit(code=1) from None
    formatter.output(task.model_dump(mode="json"), _render_task_detail)


@agent_app.command("create")
def agent_create(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", help="Human-readable task name"),
    description: str = typer.Option("", "--description", help="Free-form description"),
    cron: str = typer.Option("0 * * * *", "--cron", help="Cron expression (UTC)"),
    manual: bool = typer.Option(
        False,
        "--manual",
        help="Skip cron firing -- only run when triggered manually or as downstream.",
    ),
    enabled: bool = typer.Option(True, "--enabled/--disabled", help="Initial enabled state."),
    action_type: str | None = typer.Option(
        None,
        "--type",
        help="Action type when not using --from-file: ai_agent|cli_command|mcp_tool",
    ),
    from_file: str | None = typer.Option(
        None,
        "--from-file",
        help='Full action JSON ({"type": "...", "params": {...}}). PATH, @path, or - for stdin.',
    ),
    cli: str | None = typer.Option(None, "--cli", help="ai_agent: claude|codex|gemini"),
    prompt: str | None = typer.Option(None, "--prompt", help="ai_agent: prompt body"),
    extra_arg: list[str] = typer.Option(
        [],
        "--extra-arg",
        help="ai_agent: extra CLI arg (repeatable). Forwarded to claude/codex/gemini.",
    ),
    argv: list[str] = typer.Option(
        [],
        "--argv",
        help="cli_command: argv element (repeatable). 'kbagent' prefix is auto-added.",
    ),
    tool: str | None = typer.Option(None, "--tool", help="mcp_tool: tool name (e.g. get_jobs)"),
    mcp_project: str | None = typer.Option(
        None, "--mcp-project", help="mcp_tool: project alias to dispatch into."
    ),
    mcp_branch: int | None = typer.Option(
        None, "--mcp-branch", help="mcp_tool: branch ID (optional)."
    ),
    input_payload: str | None = typer.Option(
        None,
        "--input",
        help="mcp_tool: JSON input. Inline, @path, or -.",
    ),
    timeout: int | None = typer.Option(None, "--timeout", help="Action timeout in seconds."),
    trigger_task_id: str | None = typer.Option(
        None, "--trigger-task-id", help="Chain: ID of downstream task to fire after this one."
    ),
    trigger_on: str = typer.Option(
        "success",
        "--trigger-on",
        help="Chain filter: success|error|always.",
    ),
) -> None:
    """Register a new scheduled task.

    Two ways to specify the action:
    1. ``--from-file PATH|-`` with the full {type, params} JSON envelope.
    2. ``--type TYPE`` plus the type-specific flags (ai_agent: --cli/--prompt,
       cli_command: --argv ..., mcp_tool: --tool/--input/--mcp-project).
    """
    formatter = get_formatter(ctx)
    service: AgentService = get_service(ctx, "agent_service")
    action = _action_from_flags(
        formatter,
        action_type=action_type,
        from_file=from_file,
        cli=cli,
        prompt=prompt,
        extra_arg=list(extra_arg),
        argv=list(argv),
        tool=tool,
        mcp_project=mcp_project,
        mcp_branch=mcp_branch,
        input_payload=input_payload,
        timeout=timeout,
    )
    trigger = _trigger_from_flags(trigger_task_id, trigger_on)
    try:
        task = service.create_task(
            name=name,
            action=action,
            description=description,
            cron=cron,
            manual=manual,
            enabled=enabled,
            trigger=trigger,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    formatter.output(task.model_dump(mode="json"), _render_created_task)


@agent_app.command("update")
def agent_update(
    ctx: typer.Context,
    task_id: str | None = typer.Argument(None, help="Task ID to update. Or use --id."),
    task_id_opt: str | None = typer.Option(
        None, "--id", "--task-id", help="Task ID (alias for the positional argument)."
    ),
    name: str | None = typer.Option(None, "--name"),
    description: str | None = typer.Option(None, "--description"),
    cron: str | None = typer.Option(None, "--cron"),
    enabled: bool | None = typer.Option(
        None, "--enabled/--disabled", help="Toggle scheduler firing."
    ),
    manual: bool | None = typer.Option(
        None,
        "--manual/--auto",
        help="--manual disables cron loop; --auto re-enables it (and recomputes next_run_at).",
    ),
    clear_trigger: bool = typer.Option(
        False, "--clear-trigger", help="Remove any chained downstream trigger."
    ),
    trigger_task_id: str | None = typer.Option(
        None, "--trigger-task-id", help="Set/replace the downstream chain target."
    ),
    trigger_on: str = typer.Option(
        "success", "--trigger-on", help="Chain filter when --trigger-task-id is set."
    ),
) -> None:
    """Patch one or more fields on a task. Omitted flags leave the field unchanged."""
    formatter = get_formatter(ctx)
    service: AgentService = get_service(ctx, "agent_service")
    task_id = _resolve_id(formatter, task_id, task_id_opt, label="Task ID", flag="--id/--task-id")
    trigger = _trigger_from_flags(trigger_task_id, trigger_on)
    try:
        task = service.update_task(
            task_id,
            name=name,
            description=description,
            cron=cron,
            manual=manual,
            enabled=enabled,
            trigger=trigger,
            clear_trigger=clear_trigger,
        )
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    formatter.output(task.model_dump(mode="json"), _render_updated_task)


@agent_app.command("delete")
def agent_delete(
    ctx: typer.Context,
    task_id: str | None = typer.Argument(None, help="Task ID to delete. Or use --id."),
    task_id_opt: str | None = typer.Option(
        None, "--id", "--task-id", help="Task ID (alias for the positional argument)."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Remove a task. Run history on disk is preserved.

    Disabled tasks should be deleted with this command (not just disabled),
    when they will never run again -- a long history of disabled tasks
    clutters the UI / list output.
    """
    formatter = get_formatter(ctx)
    service: AgentService = get_service(ctx, "agent_service")
    task_id = _resolve_id(formatter, task_id, task_id_opt, label="Task ID", flag="--id/--task-id")
    if not yes and not formatter.json_mode:
        confirm = typer.confirm(f"Delete task '{task_id}'?")
        if not confirm:
            formatter.console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(code=0)
    try:
        service.delete_task(task_id)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.NOT_FOUND)
        raise typer.Exit(code=1) from None
    formatter.output({"status": "deleted", "id": task_id}, _render_deleted_task)


@agent_app.command("run")
def agent_run(
    ctx: typer.Context,
    task_id: str | None = typer.Argument(None, help="Task ID to run. Or use --id."),
    task_id_opt: str | None = typer.Option(
        None, "--id", "--task-id", help="Task ID (alias for the positional argument)."
    ),
    stream: bool = typer.Option(
        False,
        "--stream",
        help="Stream events live (each event on its own line / NDJSON in --json mode).",
    ),
    runtime_prompt: str | None = typer.Option(
        None,
        "--runtime-prompt",
        help="ai_agent: ad-hoc text appended to the persisted prompt for this run only.",
    ),
    runtime_input: str | None = typer.Option(
        None,
        "--runtime-input",
        help="JSON input merged into the action params for this run only. Inline, @path, or -.",
    ),
) -> None:
    """Trigger a task immediately (does not wait for the next cron firing).

    By default blocks until the run finishes and prints the AgentRun
    record. Use ``--stream`` to render live events as they arrive (one
    line per event in human mode, NDJSON in --json mode).

    ``--runtime-prompt`` is a shortcut for ai_agent tasks (most common
    use case for manual tasks). For full control over the runtime merge,
    pass ``--runtime-input '{"key": "value"}'``.
    """
    formatter = get_formatter(ctx)
    service: AgentService = get_service(ctx, "agent_service")
    task_id = _resolve_id(formatter, task_id, task_id_opt, label="Task ID", flag="--id/--task-id")
    runtime: dict[str, Any] | None = None
    if runtime_input:
        raw = _read_payload(runtime_input, formatter)
        runtime = _parse_json(raw, formatter, label="--runtime-input")
    if runtime_prompt:
        runtime = dict(runtime or {})
        runtime["prompt"] = runtime_prompt
    try:
        if stream:
            _stream_to_stdout(formatter, service.stream_run(task_id, runtime_input=runtime))
            return
        run = asyncio.run(service.run_task(task_id, runtime_input=runtime))
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.NOT_FOUND)
        raise typer.Exit(code=1) from None
    formatter.output(run.model_dump(mode="json"), _render_run_result)


@agent_app.command("runs")
def agent_runs(
    ctx: typer.Context,
    task_id: str | None = typer.Argument(None, help="Task ID whose history to show. Or use --id."),
    task_id_opt: str | None = typer.Option(
        None, "--id", "--task-id", help="Task ID (alias for the positional argument)."
    ),
    limit: int = typer.Option(50, "--limit", help="Max rows to return."),
) -> None:
    """Show the run history of a task (most recent first)."""
    formatter = get_formatter(ctx)
    service: AgentService = get_service(ctx, "agent_service")
    task_id = _resolve_id(formatter, task_id, task_id_opt, label="Task ID", flag="--id/--task-id")
    try:
        runs = service.list_runs(task_id, limit=limit)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.NOT_FOUND)
        raise typer.Exit(code=1) from None
    payload = {"runs": [r.model_dump(mode="json") for r in runs]}
    formatter.output(payload, _render_runs_table)


@agent_app.command("run-detail")
def agent_run_detail(
    ctx: typer.Context,
    task_id: str | None = typer.Argument(None, help="Task ID. Or use --id/--task-id."),
    run_id: str | None = typer.Argument(None, help="Run ID (12-char hex). Or use --run-id."),
    task_id_opt: str | None = typer.Option(
        None, "--id", "--task-id", help="Task ID (alias for the positional argument)."
    ),
    run_id_opt: str | None = typer.Option(
        None, "--run-id", help="Run ID (alias for the positional argument)."
    ),
) -> None:
    """Show a single AgentRun record (status, summary, output, error)."""
    formatter = get_formatter(ctx)
    service: AgentService = get_service(ctx, "agent_service")
    task_id = _resolve_id(formatter, task_id, task_id_opt, label="Task ID", flag="--id/--task-id")
    run_id = _resolve_id(formatter, run_id, run_id_opt, label="Run ID", flag="--run-id")
    try:
        run = service.get_run(task_id, run_id)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.NOT_FOUND)
        raise typer.Exit(code=1) from None

    def _render(c: Any, d: dict[str, Any]) -> None:
        from rich.syntax import Syntax

        c.print(f"[bold]Run[/bold] [cyan]{d['run_id']}[/cyan]  status={d.get('status', '?')}")
        c.print(f"task_id: {d.get('task_id')}")
        c.print(f"started: [dim]{d.get('started_at')}[/dim]")
        c.print(f"ended:   [dim]{d.get('ended_at') or '-'}[/dim]")
        if d.get("error"):
            c.print(f"[red]error:[/red] {d['error']}")
        if d.get("summary"):
            c.print(Syntax(json.dumps(d["summary"], indent=2), "json", theme="ansi_dark"))
        if d.get("output"):
            c.print("[bold]output:[/bold]")
            c.print(Syntax(json.dumps(d["output"], indent=2), "json", theme="ansi_dark"))

    formatter.output(run.model_dump(mode="json"), _render)


@agent_app.command("run-events")
def agent_run_events(
    ctx: typer.Context,
    task_id: str | None = typer.Argument(None, help="Task ID. Or use --id/--task-id."),
    run_id: str | None = typer.Argument(None, help="Run ID. Or use --run-id."),
    task_id_opt: str | None = typer.Option(
        None, "--id", "--task-id", help="Task ID (alias for the positional argument)."
    ),
    run_id_opt: str | None = typer.Option(
        None, "--run-id", help="Run ID (alias for the positional argument)."
    ),
) -> None:
    """Replay the persisted event timeline of an ai_agent run (line-by-line)."""
    formatter = get_formatter(ctx)
    service: AgentService = get_service(ctx, "agent_service")
    task_id = _resolve_id(formatter, task_id, task_id_opt, label="Task ID", flag="--id/--task-id")
    run_id = _resolve_id(formatter, run_id, run_id_opt, label="Run ID", flag="--run-id")
    try:
        events = service.get_run_events(task_id, run_id)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.NOT_FOUND)
        raise typer.Exit(code=1) from None
    if formatter.json_mode:
        formatter.output({"events": events, "count": len(events)})
        return
    for evt in events:
        _render_stream_event(formatter, evt)


@agent_app.command("test")
def agent_test(
    ctx: typer.Context,
    name: str = typer.Option("[preview]", "--name", help="Name shown in event init payload."),
    stream: bool = typer.Option(
        False, "--stream", help="Stream events live instead of returning the final run record."
    ),
    action_type: str | None = typer.Option(None, "--type", help="ai_agent|cli_command|mcp_tool"),
    from_file: str | None = typer.Option(None, "--from-file", help="Action JSON (or @path / -)."),
    cli: str | None = typer.Option(None, "--cli"),
    prompt: str | None = typer.Option(None, "--prompt"),
    extra_arg: list[str] = typer.Option([], "--extra-arg"),
    argv: list[str] = typer.Option([], "--argv"),
    tool: str | None = typer.Option(None, "--tool"),
    mcp_project: str | None = typer.Option(None, "--mcp-project"),
    mcp_branch: int | None = typer.Option(None, "--mcp-branch"),
    input_payload: str | None = typer.Option(None, "--input"),
    timeout: int | None = typer.Option(None, "--timeout"),
) -> None:
    """Execute an action ad-hoc (no persistence, no scheduling).

    Exact dispatch logic as the cron scheduler -- useful for sanity-checking
    a prompt / tool / cli_command before saving a task.
    """
    formatter = get_formatter(ctx)
    service: AgentService = get_service(ctx, "agent_service")
    action = _action_from_flags(
        formatter,
        action_type=action_type,
        from_file=from_file,
        cli=cli,
        prompt=prompt,
        extra_arg=list(extra_arg),
        argv=list(argv),
        tool=tool,
        mcp_project=mcp_project,
        mcp_branch=mcp_branch,
        input_payload=input_payload,
        timeout=timeout,
    )
    if stream:
        _stream_to_stdout(formatter, service.stream_test_action(action, name=name))
        return
    run = asyncio.run(service.test_action(action, name=name))
    formatter.output(run.model_dump(mode="json"), _render_test_result)


@agent_app.command("cron-preview")
def agent_cron_preview(
    ctx: typer.Context,
    cron: str = typer.Option(..., "--cron", help="Cron expression to evaluate."),
    count: int = typer.Option(5, "--count", help="How many firings to return (1-20)."),
) -> None:
    """Show the next N firings of a cron expression.

    Useful when authoring a task: paste the cron, eyeball the next few
    times, then save. Works offline (no network calls).
    """
    formatter = get_formatter(ctx)
    service: AgentService = get_service(ctx, "agent_service")
    try:
        firings = service.cron_preview(cron, count=count)
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.VALIDATION_ERROR)
        raise typer.Exit(code=2) from None

    def _render(c: Any, d: dict[str, Any]) -> None:
        c.print(f"[bold]Cron[/bold] [cyan]{d['cron']}[/cyan]")
        for ts in d["firings"]:
            c.print(f"  - [dim]{ts}[/dim]")

    formatter.output({"cron": cron, "firings": firings}, _render)


@agent_app.command("prompt-improve")
def agent_prompt_improve(
    ctx: typer.Context,
    goal: str = typer.Option(..., "--goal", help="Plain-English goal to polish."),
    draft: str = typer.Option("", "--draft", help="Optional half-baked prompt to refine."),
    cli: str = typer.Option("claude", "--cli", help="AI CLI to invoke: claude|codex|gemini."),
    project: str | None = typer.Option(None, "--project", help="Pinned project alias hint."),
    extra_arg: list[str] = typer.Option([], "--extra-arg", help="Extra args for the AI CLI."),
    stream: bool = typer.Option(
        True, "--stream/--no-stream", help="Stream events as they arrive (default: on)."
    ),
) -> None:
    """Polish a plain-English goal into an unattended-agent-ready prompt.

    Spawns the chosen AI CLI exactly the way an ai_agent run does, with a
    meta-prompt that asks for a single polished prompt body. The final
    ``done`` event carries the cleaned prompt under ``data.prompt``.
    """
    formatter = get_formatter(ctx)
    service: AgentService = get_service(ctx, "agent_service")

    async def _drive_no_stream() -> dict[str, Any] | None:
        last_done: dict[str, Any] | None = None
        async for evt in service.improve_prompt(
            cli=cli, goal=goal, draft=draft, project=project, extra_args=list(extra_arg)
        ):
            if evt.get("event") == "done":
                last_done = evt.get("data")
        return last_done

    try:
        if stream:
            _stream_to_stdout(
                formatter,
                service.improve_prompt(
                    cli=cli,
                    goal=goal,
                    draft=draft,
                    project=project,
                    extra_args=list(extra_arg),
                ),
            )
            return
        final = asyncio.run(_drive_no_stream())
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.VALIDATION_ERROR)
        raise typer.Exit(code=2) from None
    if final is None:
        formatter.error(
            message="Prompt helper produced no output.",
            error_code=ErrorCode.UNKNOWN_ERROR,
        )
        raise typer.Exit(code=1) from None
    formatter.output(final, _render_improved_prompt)
