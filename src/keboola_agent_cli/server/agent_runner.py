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
    ENV_KBAGENT_UPSTREAM_RUN_ID,
    ENV_KBAGENT_UPSTREAM_STATUS,
    ENV_KBAGENT_UPSTREAM_TASK_ID,
)
from .agents_store import AgentRun, AgentStore, AgentTask

logger = logging.getLogger(__name__)


def _build_subprocess_env(
    registry: Any,
    *,
    upstream_run: AgentRun | None = None,
    upstream_task: AgentTask | None = None,
) -> dict[str, str]:
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

    When the task was triggered as a downstream of another task's
    ``trigger`` chain, three more keys are added so the subprocess can
    discover its upstream context:

    - ``KBAGENT_UPSTREAM_TASK_ID``
    - ``KBAGENT_UPSTREAM_RUN_ID``
    - ``KBAGENT_UPSTREAM_STATUS`` (``ok`` or ``error``)

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
    if upstream_task is not None and upstream_run is not None:
        env[ENV_KBAGENT_UPSTREAM_TASK_ID] = upstream_task.id
        env[ENV_KBAGENT_UPSTREAM_RUN_ID] = upstream_run.run_id
        env[ENV_KBAGENT_UPSTREAM_STATUS] = upstream_run.status
    return env


def _upstream_prompt_prefix(upstream_run: AgentRun | None, upstream_task: AgentTask | None) -> str:
    """Compose a short prompt prefix announcing the upstream chain context.

    Empty when no upstream — kept out-of-line so the regular prefix stays
    the dominant signal for cron-driven runs.
    """
    if upstream_run is None or upstream_task is None:
        return ""
    return (
        "[Upstream chain context]\n"
        f"You were triggered after the upstream task '{upstream_task.name}' "
        f"(id={upstream_task.id}) completed with status '{upstream_run.status}'.\n"
        f"Read the full upstream output: "
        f"`kbagent http get /agents/{upstream_task.id}/runs/{upstream_run.run_id}`\n"
        "Env vars KBAGENT_UPSTREAM_TASK_ID + KBAGENT_UPSTREAM_RUN_ID carry the same values.\n\n"
    )


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


def build_prompt_helper_meta_prompt(
    *,
    goal: str,
    draft: str = "",
    project: str | None = None,
) -> str:
    """Compose the meta-prompt sent to the AI CLI by the prompt-helper.

    The helper's job is to take a user's plain-English goal (and an optional
    half-baked draft) and produce a polished prompt that another scheduled
    AI agent will execute. The output is consumed verbatim, so the meta-
    prompt is engineered to make the AI emit ONLY the final prompt body --
    no preamble, no code fences, no commentary.

    The meta-prompt is deliberately specific about kbagent CLI commands so
    the AI recommends real commands the scheduled agent can actually invoke
    instead of inventing API calls.
    """
    project_hint = (
        f"The user has pinned project '{project}'. Reference it explicitly in the prompt."
        if project
        else "No project is pinned in this serve; if the goal needs one, the prompt should ask for it."
    )
    draft_block = (
        f"USER'S CURRENT DRAFT (preserve any concrete details from here):\n{draft.strip()}"
        if draft.strip()
        else "USER'S CURRENT DRAFT: (empty -- write the prompt from scratch.)"
    )
    return f"""\
You are a senior prompt engineer. Rewrite the user's request into a polished
single-shot prompt for an AI agent that will run unattended on a CRON schedule
inside `kbagent serve`. The scheduled agent has access to the kbagent CLI and
the `kbagent http` family of commands; it can query Keboola Connection
projects via the serve's REST API (env vars KBAGENT_SERVE_URL +
KBAGENT_SERVE_TOKEN) or fall back to local CLI calls (KBAGENT_CONFIG_DIR is
pre-set).

USER'S GOAL (plain English):
{goal.strip()}

{draft_block}

PROJECT CONTEXT: {project_hint}

REQUIREMENTS for the rewritten prompt:
- Imperative voice, second person ("Use ...", "Then summarize ...").
- Concrete: name the kbagent commands the agent should run. Examples of
  real commands: `kbagent job list --project NAME --status error --limit 10`,
  `kbagent config search --query 'snowflake'`, `kbagent doctor`,
  `kbagent http get /projects`, `kbagent storage tables --project NAME`.
- Bound the scope: time window, project alias, max results, expected output
  format (markdown table, JSON, top-3 list, ...).
- Be self-contained: the agent has no chat history. Restate the goal in
  the prompt body.
- 80 to 250 words. No preamble, no headings, no code fences.

OUTPUT CONTRACT (critical):
- Output ONLY the rewritten prompt body. Plain text.
- Do not say "Here is the prompt:" or wrap the result in ``` fences.
- Do not include any text before or after the prompt body.
"""


# Markdown artifacts the AI sometimes emits despite the OUTPUT CONTRACT.
# Stripped post-hoc so the textarea is filled with a clean prompt body.
_PROMPT_RESPONSE_PREAMBLES = (
    "here is the prompt:",
    "here's the prompt:",
    "here is a prompt:",
    "here's a prompt:",
    "rewritten prompt:",
    "prompt:",
)

# Same idea for the SQL helper; the AI is told to emit only SQL but routinely
# starts with "Here's the SQL:" or wraps the body in ```sql fences.
_SQL_RESPONSE_PREAMBLES = (
    "here is the sql:",
    "here's the sql:",
    "here is a sql:",
    "here's a sql:",
    "here is the query:",
    "here's the query:",
    "sql:",
    "query:",
)


def build_sql_helper_meta_prompt(
    *,
    goal: str,
    project: str,
    backend: str,
    schema: str,
    draft_sql: str = "",
    bucket_ids: list[str] | None = None,
    serve_url: str | None = None,
) -> str:
    """Compose the meta-prompt sent to the AI CLI by the workspace SQL helper.

    The AI is asked to produce a single polished SQL statement (or a small
    statement batch) that runs against the user's Keboola workspace. It is
    explicitly instructed to discover table / column shape via
    INFORMATION_SCHEMA using the kbagent CLI before guessing column names.

    Backend-specific hints are folded in so claude doesn't have to "know" the
    quirks: BigQuery's backticked dataset paths and per-dataset
    INFORMATION_SCHEMA, Snowflake's CURRENT_SCHEMA() default, etc. The
    bucket list (when supplied) gives the AI a starting catalog without
    burning a tool call.
    """
    goal_clean = goal.strip()
    draft_block = (
        f"USER'S CURRENT DRAFT (refine this, don't throw it away):\n{draft_sql.strip()}"
        if draft_sql.strip()
        else "USER'S CURRENT DRAFT: (empty -- write the query from scratch.)"
    )
    bucket_block = (
        "VISIBLE BUCKETS (already loaded in the editor sidebar):\n"
        + "\n".join(f"  - {b}" for b in bucket_ids[:50])
        if bucket_ids
        else "VISIBLE BUCKETS: (none preloaded -- discover via INFORMATION_SCHEMA.)"
    )
    if len(bucket_ids or []) > 50:
        bucket_block += f"\n  ... and {len(bucket_ids or []) - 50} more (truncated)"

    backend_hint = _sql_helper_backend_hint(backend, schema)
    serve_hint = (
        f"SERVE CONTEXT: kbagent serve is reachable at {serve_url}; the AI agent\n"
        "shell has KBAGENT_SERVE_URL + KBAGENT_SERVE_TOKEN env vars pre-set, so\n"
        "`kbagent http get /...` is the fastest discovery path."
        if serve_url
        else "SERVE CONTEXT: assume `kbagent` CLI is available on PATH."
    )

    return f"""\
You are a senior data engineer writing SQL for a Keboola workspace. Your
output will be pasted into the workspace SQL editor verbatim and executed
through the Keboola Query Service against project '{project}'. The Query
Service runs SELECT only -- it rejects SHOW / DESCRIBE / DDL / DML.

WORKSPACE CONTEXT:
- Project alias: {project}
- Backend: {backend}
- Default schema: {schema}

USER'S GOAL (plain English):
{goal_clean}

{draft_block}

{bucket_block}

{backend_hint}

DISCOVERY (do this BEFORE guessing column names):
- Use `kbagent workspace query --project {project} --workspace-id <id> --sql '...'`
  with an INFORMATION_SCHEMA query to confirm table + column names exist.
- Alternative: `kbagent storage table-detail --project {project} --table-id <id>`
  returns the full column list for a Storage table without spinning up a query.
{serve_hint}

REQUIREMENTS for the returned SQL:
- Match the user's goal precisely; do not invent columns.
- Be a single SELECT statement (or a tiny CTE batch) -- nothing destructive.
- Qualify tables explicitly when joining across buckets so the result is
  unambiguous after the workspace is reused.
- Add a brief 1-line `-- comment` at the top describing what the query
  returns (purpose + key filters), but no other prose.

OUTPUT CONTRACT (critical):
- Output ONLY the SQL. Plain text.
- Do NOT wrap the SQL in ```sql fences.
- Do NOT prefix with "Here's the SQL:" / "Rewritten query:" / similar.
- Do NOT append commentary after the SQL.
"""


def _sql_helper_backend_hint(backend: str, schema: str) -> str:
    """Emit backend-specific INFORMATION_SCHEMA recipes for the meta-prompt.

    Keboola Workspaces run on three backends; each has different table-catalog
    surface area, so the meta-prompt embeds the exact INFORMATION_SCHEMA query
    the AI should run for discovery. Without this hint claude routinely
    invents Snowflake-style queries when the workspace is BigQuery.
    """
    backend_lc = (backend or "").lower()
    if backend_lc == "bigquery":
        return (
            "BACKEND HINT (BigQuery):\n"
            f"- Workspace schema is the dataset `{schema}`.\n"
            f"- Backtick-quote dataset + table names: `\\`{schema}\\`.\\`<table>\\``.\n"
            f"- Discovery: SELECT table_name FROM `{schema}.INFORMATION_SCHEMA.TABLES`;\n"
            f"- Columns: SELECT column_name, data_type FROM "
            f"`{schema}.INFORMATION_SCHEMA.COLUMNS` WHERE table_name='<table>';"
        )
    if backend_lc == "snowflake":
        return (
            "BACKEND HINT (Snowflake):\n"
            f"- Workspace default schema is `{schema}`. Identifiers are case-sensitive\n"
            f'  when quoted; Keboola Storage tables are quoted ("my-table").\n'
            f"- Discovery: SELECT TABLE_NAME, ROW_COUNT FROM INFORMATION_SCHEMA.TABLES\n"
            f"  WHERE TABLE_SCHEMA = CURRENT_SCHEMA();\n"
            f"- Columns: SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS\n"
            f"  WHERE TABLE_SCHEMA = CURRENT_SCHEMA() AND TABLE_NAME = '<table>';"
        )
    # Unknown / future backend: stay generic so the AI still has a starting point.
    return (
        f"BACKEND HINT ({backend or 'unknown'}):\n"
        f"- Workspace default schema is `{schema}`.\n"
        "- Discovery: query INFORMATION_SCHEMA.TABLES / COLUMNS following the\n"
        "  backend's conventions (Snowflake = CURRENT_SCHEMA(), BigQuery = dataset\n"
        "  path, Postgres = current_schema())."
    )


def clean_sql_helper_response(text: str) -> str:
    """Strip code fences, preambles, and claude jsonl duplication from SQL output.

    Mirrors :func:`clean_prompt_helper_response` step-for-step but uses the
    SQL-specific preamble list. Two distinct cleaners (instead of a unified
    one with a knob) makes the call sites self-documenting and lets future
    SQL/prompt divergence land without entangling.
    """
    text = text.strip()
    # Step 1: collapse "AB" where A == B (claude jsonl duplication).
    if text and len(text) % 2 == 0:
        half = len(text) // 2
        if text[:half] == text[half:]:
            text = text[:half].rstrip()
    # Step 2: strip a single set of leading/trailing code fences. Accept
    # ```sql or ``` -- the AI uses both interchangeably.
    if text.startswith("```"):
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    # Step 3: strip a preamble like "Here's the SQL:\n\n..." on the first line.
    lines = text.split("\n", 1)
    first = lines[0].strip().lower()
    if any(first == p or first.startswith(p) for p in _SQL_RESPONSE_PREAMBLES):
        text = lines[1].strip() if len(lines) > 1 else ""
    return text.strip()


def clean_prompt_helper_response(text: str) -> str:
    """Trim surrounding code fences, preambles, and dedup the response.

    Three independent cleanups, applied in order:

    1. **Deduplication.** ``stream_ai_agent_events`` accumulates both
       claude's incremental ``assistant`` turns AND the final ``result``
       event into ``response``. Claude often emits the same body in both
       (assistant streams it; result repeats the whole thing). For a
       prompt-helper task -- a single non-tool turn -- the result is
       effectively duplicated. If the string is exactly two equal halves,
       collapse to one.
    2. **Code-fence strip** (``` / ```text / ```md).
    3. **Preamble strip** (``Here is the prompt:`` / ``Rewritten prompt:`` ...).
    """
    text = text.strip()
    # Step 1: collapse "AB" where A == B (claude jsonl duplication).
    if text and len(text) % 2 == 0:
        half = len(text) // 2
        if text[:half] == text[half:]:
            text = text[:half].rstrip()
    # Step 2: strip a single set of leading/trailing code fences.
    if text.startswith("```"):
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    # Step 3: strip a preamble like "Here is the prompt:\n\n..." on the first line.
    lines = text.split("\n", 1)
    first = lines[0].strip().lower()
    if any(first == p or first.startswith(p) for p in _PROMPT_RESPONSE_PREAMBLES):
        text = lines[1].strip() if len(lines) > 1 else ""
    return text.strip()


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


async def _run_cli(
    registry: Any,
    params: dict[str, Any],
    *,
    upstream_run: AgentRun | None = None,
    upstream_task: AgentTask | None = None,
) -> dict[str, Any]:
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
        env=_build_subprocess_env(registry, upstream_run=upstream_run, upstream_task=upstream_task),
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


async def _run_ai_agent(
    registry: Any,
    params: dict[str, Any],
    *,
    upstream_run: AgentRun | None = None,
    upstream_task: AgentTask | None = None,
) -> dict[str, Any]:
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

    wrapped_prompt = (
        _upstream_prompt_prefix(upstream_run, upstream_task) + _AI_AGENT_PROMPT_PREFIX + prompt
    )
    argv = _AI_CLI_RECIPES[cli_name](wrapped_prompt, extra_args)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        env=_build_subprocess_env(registry, upstream_run=upstream_run, upstream_task=upstream_task),
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
    *,
    upstream_run: AgentRun | None = None,
    upstream_task: AgentTask | None = None,
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

    wrapped_prompt = (
        _upstream_prompt_prefix(upstream_run, upstream_task) + _AI_AGENT_PROMPT_PREFIX + prompt
    )
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
        env=_build_subprocess_env(registry, upstream_run=upstream_run, upstream_task=upstream_task),
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


def _trigger_should_fire(trigger_on: str, run_status: str) -> bool:
    """Match a Trigger.on filter against the upstream run status.

    Pulled out so tests can assert the policy in isolation; also makes
    the fan-out site at the bottom of ``run_task_once`` legible.
    """
    if trigger_on == "always":
        return True
    if trigger_on == "success" and run_status == "ok":
        return True
    return trigger_on == "error" and run_status == "error"


async def run_task_once(
    task: AgentTask,
    registry: Any,
    store: AgentStore,
    *,
    upstream_run: AgentRun | None = None,
    upstream_task: AgentTask | None = None,
) -> AgentRun:
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

    When ``upstream_run`` + ``upstream_task`` are supplied (the run was
    triggered as a chained downstream), the subprocess receives extra
    ``KBAGENT_UPSTREAM_*`` env vars and the ai_agent prompt is prefixed
    with a hint explaining where to fetch the upstream output.

    After persist, if ``task.trigger`` is set and its ``on`` filter
    matches this run's status, the downstream task runs synchronously
    with this run threaded through as its upstream context. The chain
    is awaited because each downstream's persist depends on the
    upstream's persist already being on disk.
    """
    started = _now_utc()
    run = AgentRun(task_id=task.id, started_at=started.isoformat())
    captured_events: list[dict[str, Any]] = []
    try:
        if task.action.type == "mcp_tool":
            # mcp_tool runs in-process via McpService, no env vars to
            # propagate. The upstream payload, when relevant, can still
            # be read from store by a follow-up ai_agent task.
            output = await _run_mcp_tool(registry, task.action.params)
            run.status = "ok"
            run.output = output if isinstance(output, dict) else {"value": output}
        elif task.action.type == "cli_command":
            output = await _run_cli(
                registry,
                task.action.params,
                upstream_run=upstream_run,
                upstream_task=upstream_task,
            )
            run.status = "ok"
            run.output = output if isinstance(output, dict) else {"value": output}
        elif task.action.type == "ai_agent":
            # Stream events so we can persist the full timeline. The final
            # ``done`` event carries the same payload the legacy
            # _run_ai_agent built, so callers reading ``run.output`` see
            # an identical shape.
            done_payload: dict[str, Any] | None = None
            async for evt in stream_ai_agent_events(
                registry,
                task.action.params,
                upstream_run=upstream_run,
                upstream_task=upstream_task,
            ):
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
        # Update last_run / next_run on the PERSISTED task. We refetch from
        # the store rather than mutating the in-memory `task` because
        # callers (router /run with runtime_input) may pass a model-copy
        # of the original with merged action params — upserting that ghost
        # would clobber the saved action on disk. Refetch is cheap (single
        # JSON file read) and keeps the persisted record clean.
        persisted = store.get_task(task.id)
        if persisted is not None:
            persisted.last_run_at = run.started_at
            persisted.next_run_at = None if persisted.manual else compute_next_run(persisted.cron)
            store.upsert_task(persisted)

    # Fan-out a chained downstream AFTER persist, so any HTTP-read the
    # downstream does (`kbagent http get /agents/<id>/runs/<run_id>`)
    # sees the upstream output already on disk. Disabled downstreams
    # are skipped silently — disabling is the operator's "off switch"
    # for the chain.
    if task.trigger and _trigger_should_fire(task.trigger.on, run.status):
        downstream = store.get_task(task.trigger.task_id)
        if downstream is None:
            logger.warning(
                "Chain target task %s (from %s) not found; skipping fan-out",
                task.trigger.task_id,
                task.id,
            )
        elif not downstream.enabled:
            logger.info(
                "Chain target %s disabled; skipping fan-out from %s",
                downstream.id,
                task.id,
            )
        else:
            logger.info(
                "Chain: %s -> %s (on=%s, status=%s)",
                task.name,
                downstream.name,
                task.trigger.on,
                run.status,
            )
            try:
                await run_task_once(
                    downstream,
                    registry,
                    store,
                    upstream_run=run,
                    upstream_task=task,
                )
            except Exception:
                # Swallow downstream errors so the upstream's run record
                # stays "ok". The downstream's own run record captures
                # its failure; we don't want a bad downstream to retro-
                # flip the upstream's status.
                logger.exception(
                    "Chain downstream %s failed (upstream %s already persisted)",
                    downstream.id,
                    task.id,
                )

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
                if task.manual:
                    # Manual tasks only run via POST /agents/{id}/run or as a
                    # chained downstream — cron is preserved on the record
                    # but the scheduler ignores it.
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
