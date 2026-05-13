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
import contextlib
import json
import logging
import os
import sys
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from croniter import croniter

from ..constants import (
    ENV_CONFIG_DIR,
    ENV_KBAGENT_SERVE_TOKEN,
    ENV_KBAGENT_SERVE_URL,
)
from .agents_store import AgentRun, AgentStore, AgentTask

logger = logging.getLogger(__name__)


def _build_subprocess_env(registry: Any) -> dict[str, str]:
    """Compose the env for an AI / CLI subprocess spawned by the scheduler.

    Inherits the parent's environment and overlays three keys so the child
    process points back at *this* serve instead of falling back to the
    global ``~/.config/keboola-agent-cli/config.json`` (which is almost
    always a different set of project tokens than what the operator
    configured for the running serve):

    - ``KBAGENT_CONFIG_DIR`` aligns any spawned ``kbagent <cmd>`` with the
      serve's config -- same projects, same storage tokens, same active
      branches. Read by ``resolve_config_dir`` in ``config_store.py``.
    - ``KBAGENT_SERVE_URL`` + ``KBAGENT_SERVE_TOKEN`` let the child use
      ``kbagent http get/post/patch/delete`` to call the live HTTP API
      directly, bypassing local config entirely. Useful for AI agents
      that prefer one stateless HTTP hop over forking ``kbagent`` CLIs.

    Returns a fresh dict (callers can mutate without affecting parent env).
    """
    env = dict(os.environ)
    config_store = getattr(registry, "config_store", None)
    if config_store is not None:
        env[ENV_CONFIG_DIR] = str(config_store.config_dir)
    serve_url = getattr(registry, "serve_url", None)
    if serve_url:
        env[ENV_KBAGENT_SERVE_URL] = serve_url
    serve_token = getattr(registry, "serve_token", None)
    if serve_token:
        env[ENV_KBAGENT_SERVE_TOKEN] = serve_token
    return env


# Instruction injected at the head of every ai_agent prompt. Tells the AI
# CLI it is running inside ``kbagent serve`` and how to call the live API
# instead of forking a stale ``kbagent`` CLI subprocess. Kept short so the
# user's actual prompt remains the dominant signal.
_AI_AGENT_PROMPT_PREFIX = """\
[kbagent serve runtime context]
You are running inside a `kbagent serve` instance. Two ways to query Keboola:

1) Preferred: HTTP API of *this* serve.
   - URL    in env var KBAGENT_SERVE_URL
   - Bearer in env var KBAGENT_SERVE_TOKEN
   - Browse the OpenAPI: `kbagent http get /openapi.json`
   - Example: `kbagent http get /projects`, `kbagent http get /configs?project=padak`

2) Fallback: local CLI. KBAGENT_CONFIG_DIR is set so any `kbagent <cmd>` you
   run reads the SAME config the serve uses (no stale tokens). Refresh /
   manage-token operations still need a human at a terminal -- do not try
   to obtain manage tokens yourself.

End of runtime context. The user's task follows:

"""


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


async def _run_cli(registry: Any, params: dict[str, Any]) -> dict[str, Any]:
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
        env=_build_subprocess_env(registry),
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


async def _run_ai_agent(registry: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch an ai_agent action via an AI CLI (claude / codex / gemini).

    Spawns the chosen CLI once with the prompt, captures stdout (the AI
    response), exits. Use this for "summarize my error jobs at midnight"
    style autonomous agents -- the assistant can use its own tools (web
    search, MCP, file ops) to satisfy the prompt.

    The user's prompt is wrapped with a small runtime-context preamble
    (KBAGENT_SERVE_URL / KBAGENT_SERVE_TOKEN / KBAGENT_CONFIG_DIR env vars
    plus a `kbagent http` usage hint) so the AI knows it can talk to *this*
    serve over HTTP and that any `kbagent` CLI calls will see the serve's
    config -- not the global one.
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

    wrapped_prompt = _AI_AGENT_PROMPT_PREFIX + prompt
    argv = _AI_CLI_RECIPES[cli_name](wrapped_prompt, extra_args)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        env=_build_subprocess_env(registry),
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


# Per-CLI streaming recipes. Only claude supports a structured JSONL stream
# today (`--output-format=stream-json --verbose`); codex and gemini fall back
# to the unstructured plain-text path. The recipe builder returns argv, and
# whether the resulting subprocess emits JSONL (so the consumer knows whether
# to JSON-parse each line or treat it as raw text).
_AI_CLI_STREAM_RECIPES: dict[str, Any] = {
    # `--verbose` is REQUIRED by claude for `--output-format=stream-json`;
    # without it claude refuses and prints an error to stderr. Note: stream-json
    # is line-buffered by claude itself, so we don't need to disable Python
    # / Node stdout buffering on our side.
    "claude": lambda prompt, extra: (
        [
            "claude",
            "-p",
            prompt,
            "--output-format=stream-json",
            "--verbose",
            *extra,
        ],
        True,  # jsonl
    ),
    "codex": lambda prompt, extra: (["codex", "exec", *extra, prompt], False),
    "gemini": lambda prompt, extra: (["gemini", "-p", prompt, *extra], False),
}


async def stream_ai_agent_events(
    registry: Any,
    params: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    """Spawn an AI CLI and yield events as they are emitted, live.

    Each yielded value is a dict shaped like ``{"event": <name>, "data": ...}``,
    drop-in for an SSE serializer:

    - ``init``  -- one-shot at the start. Contains ``cli``, ``argv``,
      ``jsonl`` flag, ``started_at``, ``prompt_preview``.
    - ``stdout`` -- one per line of stdout. For ``cli == "claude"`` the
      ``data`` is the parsed JSONL object (raw line preserved under
      ``data.raw``). For codex/gemini ``data`` is just the raw line.
    - ``stderr`` -- one per line of stderr (each AI CLI writes its
      progress notes there too; rarely empty).
    - ``done`` -- one-shot at the end. Contains ``exit_code``,
      ``elapsed_seconds``, ``ended_at``, ``status``, plus the accumulated
      ``response_text`` (text content joined across all ``assistant``
      turn events -- so callers don't have to re-walk the stream).

    Cancellation: the caller can stop iterating; the subprocess is killed
    via try/finally. Timeout-induced kills emit a final ``done`` with
    ``status="error"`` and ``error="timeout"``.
    """
    cli_name = str(params.get("cli", "")).lower()
    if cli_name not in _AI_CLI_STREAM_RECIPES:
        raise ValueError(
            f"ai_agent.cli must be one of {sorted(_AI_CLI_STREAM_RECIPES)}, got {cli_name!r}"
        )
    prompt = params.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("ai_agent action requires a non-empty 'prompt'")
    extra_args = params.get("extra_args") or []
    if not isinstance(extra_args, list):
        raise ValueError("ai_agent.extra_args must be a list of strings")
    extra_args = [str(a) for a in extra_args]
    timeout = float(params.get("timeout", 600.0))

    wrapped_prompt = _AI_AGENT_PROMPT_PREFIX + prompt
    argv, jsonl = _AI_CLI_STREAM_RECIPES[cli_name](wrapped_prompt, extra_args)
    started_monotonic = time.monotonic()
    started_at = _now_utc().isoformat()

    yield {
        "event": "init",
        "data": {
            "cli": cli_name,
            "argv": argv,
            "jsonl": jsonl,
            "started_at": started_at,
            "prompt_preview": prompt[:200],
        },
    }

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        env=_build_subprocess_env(registry),
    )

    # Walk stdout + stderr concurrently. Each side gets its own consumer
    # coroutine that pushes items into a shared queue; the generator
    # awaits the queue and yields events as they arrive. This is the
    # idiomatic asyncio fan-in -- avoids the trap of `async for line in
    # proc.stdout` blocking stderr until EOF (which would defeat the
    # whole point of "show me what's happening live").
    queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()
    response_chunks: list[str] = []
    stderr_chunks: list[str] = []

    async def _consume(stream: asyncio.StreamReader, kind: str) -> None:
        while True:
            raw = await stream.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if kind == "stdout" and jsonl:
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    await queue.put(("stdout", {"raw": line}))
                    continue
                # Best-effort extract of assistant text so the final
                # `done` event carries a `response_text` field. claude's
                # stream-json shapes assistant turns as
                # ``{"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}``
                # and the final result as ``{"type":"result","result":"..."}``.
                if isinstance(parsed, dict):
                    if parsed.get("type") == "assistant":
                        content = parsed.get("message", {}).get("content", [])
                        for block in content if isinstance(content, list) else []:
                            if (
                                isinstance(block, dict)
                                and block.get("type") == "text"
                                and isinstance(block.get("text"), str)
                            ):
                                response_chunks.append(block["text"])
                    elif parsed.get("type") == "result" and isinstance(parsed.get("result"), str):
                        response_chunks.append(parsed["result"])
                await queue.put(
                    ("stdout", parsed if isinstance(parsed, dict) else {"value": parsed})
                )
            else:
                if kind == "stderr":
                    stderr_chunks.append(line)
                await queue.put((kind, {"raw": line}))

    assert proc.stdout is not None
    assert proc.stderr is not None
    stdout_task = asyncio.create_task(_consume(proc.stdout, "stdout"))
    stderr_task = asyncio.create_task(_consume(proc.stderr, "stderr"))

    async def _wait_and_signal() -> None:
        await asyncio.gather(stdout_task, stderr_task)
        await proc.wait()
        await queue.put(None)

    wait_task = asyncio.create_task(_wait_and_signal())

    deadline = started_monotonic + timeout
    timed_out = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=remaining)
            except TimeoutError:
                timed_out = True
                break
            if item is None:
                break
            kind, payload = item
            yield {"event": kind, "data": payload}
    finally:
        if timed_out and proc.returncode is None:
            proc.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=2.0)
        for t in (stdout_task, stderr_task, wait_task):
            if not t.done():
                t.cancel()
        # Drain any remaining queue items already produced before kill.
        while not queue.empty():
            item = queue.get_nowait()
            if item is None:
                continue
            kind, payload = item
            yield {"event": kind, "data": payload}

    elapsed = round(time.monotonic() - started_monotonic, 2)
    status = "error" if (timed_out or proc.returncode not in (0, None)) else "ok"
    final: dict[str, Any] = {
        "cli": cli_name,
        "argv": argv,
        "exit_code": proc.returncode,
        "elapsed_seconds": elapsed,
        "ended_at": _now_utc().isoformat(),
        "status": status,
        "response": "".join(response_chunks),
        "stderr": "\n".join(stderr_chunks),
    }
    if timed_out:
        final["error"] = f"AI CLI '{cli_name}' timed out after {timeout}s"
    yield {"event": "done", "data": final}


async def run_task_once(task: AgentTask, registry: Any, store: AgentStore) -> AgentRun:
    """Execute one task and append a run record.

    For ai_agent runs we now drive the streaming generator and capture
    every emitted event so the persisted run carries:
    - the full timeline (saved to ``agent_runs/<task_id>/<run_id>.jsonl``)
    - a precomputed summary (model, tokens, cost, tool calls)

    This unifies cron-driven runs with UI-driven runs (RunBroadcaster):
    both produce the same persisted shape, and the detail drawer can
    replay either using the same /events endpoint. cli_command and
    mcp_tool runs still use the one-shot path; their structured output
    fits in the ``output`` field directly.
    """
    started = _now_utc()
    run = AgentRun(task_id=task.id, started_at=started.isoformat())
    captured_events: list[dict[str, Any]] = []
    try:
        if task.action.type == "mcp_tool":
            output = await _run_mcp_tool(registry, task.action.params)
            run.status = "ok"
            run.output = output if isinstance(output, dict) else {"value": output}
        elif task.action.type == "cli_command":
            output = await _run_cli(registry, task.action.params)
            run.status = "ok"
            run.output = output if isinstance(output, dict) else {"value": output}
        elif task.action.type == "ai_agent":
            # Stream events so we can persist the full timeline. The final
            # ``done`` event carries the same payload the legacy
            # _run_ai_agent built, so callers reading ``run.output`` see
            # an identical shape.
            done_payload: dict[str, Any] | None = None
            async for evt in stream_ai_agent_events(registry, task.action.params):
                captured_events.append(evt)
                if evt["event"] == "done":
                    done_payload = evt["data"]
            if done_payload is None:
                # Stream ended without a done frame -- treat as error so
                # the UI flags it; the captured events still get persisted
                # so an operator can see what claude was up to.
                run.status = "error"
                run.error = "ai_agent stream ended without a done event"
            else:
                run.status = done_payload.get("status", "ok")
                run.output = done_payload
                if done_payload.get("error"):
                    run.error = done_payload["error"]
        else:
            raise ValueError(f"Unknown action type: {task.action.type}")
    except Exception as exc:
        logger.exception("Agent task %s failed", task.id)
        run.status = "error"
        run.error = str(exc)
    finally:
        run.ended_at = _now_utc().isoformat()
        # Persist the timeline + compute summary BEFORE appending the run
        # row so events_path/summary land on the same JSONL line.
        from .pricing import build_run_summary

        try:
            if task.action.type == "ai_agent" and captured_events:
                run.summary = build_run_summary(captured_events)
                run.events_path = store.append_events(task.id, run.run_id, captured_events)
        except Exception:
            logger.exception("Failed to persist event timeline for %s/%s", task.id, run.run_id)
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
