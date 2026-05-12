"""Execution backend for scheduled agent tasks + the cron scheduler loop.

Two action types are supported today:

- ``mcp_tool``:    call a keboola-mcp-server tool via :class:`McpService`.
- ``cli_command``: spawn ``kbagent <argv>`` as a subprocess and capture stdout.

The scheduler is a single asyncio loop attached to the FastAPI lifespan
(``serve.create_app()``); it ticks once a minute, checks every enabled
task's cron expression against ``datetime.now(UTC)`` (truncated to the
minute), and dispatches due tasks via :func:`run_task_once`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from croniter import croniter

from .agents_store import AgentRun, AgentStore, AgentTask

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def compute_next_run(cron: str, after: datetime | None = None) -> str | None:
    """Return ISO timestamp of the next cron firing after ``after`` (default now)."""
    try:
        base = after or _now_utc()
        nxt = croniter(cron, base).get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=UTC)
        return nxt.isoformat()
    except Exception as exc:
        logger.warning("Invalid cron %r: %s", cron, exc)
        return None


def is_due(cron: str, last_run: datetime | None, now: datetime) -> bool:
    """Has this task's cron crossed since ``last_run`` (or since 1 minute ago)?

    The scheduler ticks every minute; we look at the previous firing and
    say "due" iff that firing is strictly after the last successful run.
    """
    try:
        prev_iter = croniter(cron, now)
        prev = prev_iter.get_prev(datetime)
        if prev.tzinfo is None:
            prev = prev.replace(tzinfo=UTC)
    except Exception:
        return False
    if last_run is None:
        return True
    return prev > last_run


async def _run_mcp_tool(registry: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch an mcp_tool action via the McpService."""
    tool = params.get("tool")
    if not tool:
        raise ValueError("mcp_tool action requires a 'tool' name in params")
    project = params.get("project")
    branch_id = params.get("branch_id")
    tool_input = params.get("input") or {}
    return await asyncio.to_thread(
        registry.mcp.validate_and_call_tool,
        tool_name=str(tool),
        tool_input=tool_input,
        alias=project,
        branch_id=branch_id,
    )


async def _run_cli(params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a cli_command action via subprocess."""
    argv_param = params.get("argv")
    if not isinstance(argv_param, list) or not argv_param:
        raise ValueError("cli_command action requires non-empty 'argv' list")
    argv = [str(a) for a in argv_param]
    if argv[0] != "kbagent":
        argv = ["kbagent", *argv]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=None,
    )
    timeout = float(params.get("timeout", 300.0))
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        raise RuntimeError(f"CLI command timed out after {timeout}s") from None
    return {
        "argv": argv,
        "exit_code": proc.returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }


# Per-CLI launcher recipes for "single prompt, no interaction" mode.
_AI_CLI_RECIPES: dict[str, Any] = {
    # Anthropic Claude Code: -p PROMPT runs in headless / non-interactive mode.
    "claude": lambda prompt, extra: ["claude", "-p", prompt, *extra],
    # OpenAI Codex CLI: `codex exec PROMPT` runs once and exits.
    "codex": lambda prompt, extra: ["codex", "exec", *extra, prompt],
    # Google Gemini CLI: `gemini -p PROMPT` for non-interactive single prompt.
    "gemini": lambda prompt, extra: ["gemini", "-p", prompt, *extra],
}


async def _run_ai_agent(params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch an ai_agent action via an AI CLI (claude / codex / gemini).

    Spawns the chosen CLI once with the prompt, captures stdout (the AI
    response), exits. Use this for "summarize my error jobs at midnight"
    style autonomous agents -- the assistant can use its own tools (web
    search, MCP, file ops) to satisfy the prompt.
    """
    cli_name = str(params.get("cli", "")).lower()
    if cli_name not in _AI_CLI_RECIPES:
        raise ValueError(f"ai_agent.cli must be one of {sorted(_AI_CLI_RECIPES)}, got {cli_name!r}")
    prompt = params.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("ai_agent action requires a non-empty 'prompt'")
    extra_args = params.get("extra_args") or []
    if not isinstance(extra_args, list):
        raise ValueError("ai_agent.extra_args must be a list of strings")
    extra_args = [str(a) for a in extra_args]
    timeout = float(params.get("timeout", 600.0))

    argv = _AI_CLI_RECIPES[cli_name](prompt, extra_args)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        env=None,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        raise RuntimeError(f"AI CLI '{cli_name}' timed out after {timeout}s") from None
    return {
        "cli": cli_name,
        "argv": argv,
        "prompt_preview": prompt[:200],
        "exit_code": proc.returncode,
        "response": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }


async def run_task_once(task: AgentTask, registry: Any, store: AgentStore) -> AgentRun:
    """Execute one task and append a run record."""
    started = _now_utc()
    run = AgentRun(task_id=task.id, started_at=started.isoformat())
    try:
        if task.action.type == "mcp_tool":
            output = await _run_mcp_tool(registry, task.action.params)
        elif task.action.type == "cli_command":
            output = await _run_cli(task.action.params)
        elif task.action.type == "ai_agent":
            output = await _run_ai_agent(task.action.params)
        else:
            raise ValueError(f"Unknown action type: {task.action.type}")
        run.status = "ok"
        run.output = output if isinstance(output, dict) else {"value": output}
    except Exception as exc:
        logger.exception("Agent task %s failed", task.id)
        run.status = "error"
        run.error = str(exc)
    finally:
        run.ended_at = _now_utc().isoformat()
        store.append_run(run)
        # Update last_run / next_run on the task itself.
        task.last_run_at = run.started_at
        task.next_run_at = compute_next_run(task.cron)
        store.upsert_task(task)
    return run


async def scheduler_loop(store: AgentStore, registry: Any, *, tick_seconds: int = 60) -> None:
    """Run forever: every tick, dispatch due tasks."""
    logger.info("Agent scheduler started (tick=%ss)", tick_seconds)
    # Hold strong references to in-flight task coroutines so they don't get
    # GC'd mid-flight (RUF006). We discard them via a done-callback.
    in_flight: set[asyncio.Task[None]] = set()
    while True:
        try:
            now = _now_utc()
            for task in store.load_tasks():
                if not task.enabled:
                    continue
                last = datetime.fromisoformat(task.last_run_at) if task.last_run_at else None
                if not is_due(task.cron, last, now):
                    continue
                logger.info("Dispatching agent task: %s (%s)", task.name, task.id)
                fut = asyncio.create_task(_safe_run(task, registry, store))
                in_flight.add(fut)
                fut.add_done_callback(in_flight.discard)
        except Exception as exc:
            logger.exception("Scheduler tick error: %s", exc)
        try:
            await asyncio.sleep(tick_seconds)
        except asyncio.CancelledError:
            logger.info("Agent scheduler stopping")
            return


async def _safe_run(task: AgentTask, registry: Any, store: AgentStore) -> None:
    try:
        await run_task_once(task, registry, store)
    except Exception:
        logger.exception("Background task execution failed for %s", task.id)


def stdin_isatty() -> bool:
    return bool(getattr(sys.stdin, "isatty", lambda: False)())
