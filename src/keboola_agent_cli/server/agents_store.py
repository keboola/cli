"""Persistent store for scheduled agent tasks.

Tasks are JSON, kept in ``<config_dir>/agents.json`` with 0600 permissions.
Histor each task's runs lives in ``<config_dir>/agent_runs/<task_id>.jsonl``
(one JSON object per line, append-only).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# "mcp_tool" is a tombstone -- see REMOVED_ACTION_TYPES
ActionType = Literal["mcp_tool", "cli_command", "ai_agent"]

#: Action types removed in 0.85.0. Kept in ``ActionType`` on purpose: load_tasks()
#: skips entries that fail validation and save_tasks() rewrites the file from the
#: loaded list, so dropping the literal would silently delete the user's task from
#: disk on the next unrelated write. Round-trip must survive; execution must not.
REMOVED_ACTION_TYPES: frozenset[str] = frozenset({"mcp_tool"})
REMOVED_IN_VERSION: str = "0.85.0"
REMOVED_ACTION_MESSAGE = (
    f"agent action type 'mcp_tool' was REMOVED in kbagent v{REMOVED_IN_VERSION} "
    "(epic #390). This task no longer runs. Recreate it with --type cli_command "
    "using the native kbagent command -- see docs/mcp-migration.md for the "
    "tool->command map."
)


def annotate_removed_action(task: dict) -> dict:
    """Add an additive ``deprecation`` key to a task using a removed action type.

    Additive and only on affected tasks, so every existing consumer sees a
    byte-identical payload. Mutates and returns the same dict.
    """
    if (task.get("action") or {}).get("type") in REMOVED_ACTION_TYPES:
        task["deprecation"] = REMOVED_ACTION_MESSAGE
    return task


class AgentAction(BaseModel):
    """What the task does when triggered.

    - ``cli_command``: spawn ``kbagent <argv>`` and capture stdout
        params: { argv: ["job", "list", "--project", "padak", "--status", "error"], timeout: 300 }
    - ``ai_agent``:    spawn an AI CLI (claude/codex/gemini) with a prompt
        params: {
          cli: "claude" | "codex" | "gemini",
          prompt: "Check overnight job logs and summarize errors",
          extra_args: ["--print", ...] (optional CLI-specific flags),
          timeout: 600,
        }
    """

    type: ActionType
    params: dict[str, Any] = Field(default_factory=dict)


class Trigger(BaseModel):
    """Chain configuration: after an upstream task completes, optionally
    run a downstream task with the upstream's run context attached.

    The downstream's subprocess inherits ``KBAGENT_UPSTREAM_TASK_ID`` and
    ``KBAGENT_UPSTREAM_RUN_ID`` env vars; ``ai_agent`` action types
    additionally get a one-line prompt prefix pointing at the upstream
    run JSON they can pull via the kbagent HTTP API.
    """

    on: Literal["success", "error", "always"] = "success"
    task_id: str


class AgentTask(BaseModel):
    """A scheduled agent task.

    Default: cron-driven by ``cron`` expression. With ``manual=True`` the
    scheduler skips the task entirely; it only runs when triggered through
    ``POST /agents/{id}/run`` or as a downstream of another task's
    ``trigger``. ``cron`` is preserved on manual tasks so the operator can
    flip back to scheduled mode without losing the schedule.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str
    description: str = ""
    cron: str = "0 * * * *"  # default every hour, top of the hour
    manual: bool = False
    enabled: bool = True
    action: AgentAction
    trigger: Trigger | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_run_at: str | None = None
    next_run_at: str | None = None


class AgentRun(BaseModel):
    """One execution of a task.

    The ``output`` field carries the final terminal payload (response text,
    exit code, stderr) so old clients keep working. Newer fields:

    - ``summary``: aggregated metrics produced by ``pricing.build_run_summary``
      (model, token totals, cost breakdown, per-tool call counts). Always
      present for ai_agent runs after v0.10.x; ``None`` for cli_command
      runs (no per-step structure to summarize).
    - ``events_path``: relative path under the store's run directory pointing
      at ``<task_id>/<run_id>.jsonl`` -- the full event timeline as it was
      streamed live. Lets the detail drawer "replay" a finished run with the
      same per-step UI shown during the live run.
    """

    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    task_id: str
    started_at: str
    ended_at: str | None = None
    status: Literal["running", "ok", "error"] = "running"
    output: dict[str, Any] | None = None
    error: str | None = None
    summary: dict[str, Any] | None = None
    events_path: str | None = None


class AgentStore:
    """File-based agent task + run history persistence."""

    def __init__(self, config_dir: Path) -> None:
        self._config_dir = config_dir
        self._agents_path = config_dir / "agents.json"
        self._runs_dir = config_dir / "agent_runs"

    def _ensure_dirs(self) -> None:
        self._config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._runs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def load_tasks(self) -> list[AgentTask]:
        if not self._agents_path.exists():
            return []
        try:
            raw = json.loads(self._agents_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("agents.json invalid; ignoring (%s)", exc)
            return []
        if not isinstance(raw, list):
            return []
        result: list[AgentTask] = []
        for item in raw:
            try:
                result.append(AgentTask.model_validate(item))
            except Exception as exc:
                logger.warning("Skipping invalid agent entry: %s", exc)
        return result

    def save_tasks(self, tasks: list[AgentTask]) -> None:
        self._ensure_dirs()
        payload = [t.model_dump(mode="json") for t in tasks]
        tmp = self._agents_path.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(str(tmp), str(self._agents_path))

    def upsert_task(self, task: AgentTask) -> AgentTask:
        tasks = self.load_tasks()
        idx = next((i for i, t in enumerate(tasks) if t.id == task.id), None)
        if idx is None:
            tasks.append(task)
        else:
            tasks[idx] = task
        self.save_tasks(tasks)
        return task

    def delete_task(self, task_id: str) -> bool:
        tasks = self.load_tasks()
        new = [t for t in tasks if t.id != task_id]
        if len(new) == len(tasks):
            return False
        self.save_tasks(new)
        return True

    def get_task(self, task_id: str) -> AgentTask | None:
        return next((t for t in self.load_tasks() if t.id == task_id), None)

    # ---- run history ---------------------------------------------------

    def append_run(self, run: AgentRun) -> None:
        self._ensure_dirs()
        path = self._runs_dir / f"{run.task_id}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(run.model_dump(mode="json"), ensure_ascii=False) + "\n")

    def list_runs(self, task_id: str, limit: int = 50) -> list[AgentRun]:
        path = self._runs_dir / f"{task_id}.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        runs: list[AgentRun] = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                runs.append(AgentRun.model_validate_json(line))
            except Exception:
                continue
            if len(runs) >= limit:
                break
        return runs

    # ---- per-run event timeline (since v0.10.x) ------------------------
    # Events are stored separately from the run summary to keep the
    # row-level <task_id>.jsonl small (one line per run for fast listing)
    # while letting the detail view fetch the full timeline on demand.

    def _events_path(self, task_id: str, run_id: str) -> Path:
        """Filesystem path for a run's full event timeline JSONL."""
        return self._runs_dir / task_id / f"{run_id}.jsonl"

    def append_events(self, task_id: str, run_id: str, events: list[dict[str, Any]]) -> str:
        """Persist the full event stream for one run, return the relative path.

        File layout: ``<runs_dir>/<task_id>/<run_id>.jsonl`` with one JSON
        object per line (the same shape that ``stream_ai_agent_events``
        emits, plus the ``seq`` tag from ``RunBroadcaster``).

        Empty event lists still create an empty file so callers can later
        distinguish "no timeline persisted" (path is None) from "agent
        produced nothing" (file exists, zero lines).
        """
        self._ensure_dirs()
        path = self._events_path(task_id, run_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # 0600: events can leak Storage tokens (an AI agent's own tool calls
        # and a cli_command's output both dump project context); same security
        # posture as the rest of config_dir.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            for evt in events:
                line = json.dumps(evt, ensure_ascii=False) + "\n"
                os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        # Return the relative path for storage on the AgentRun record.
        # Resolving back to absolute is the store's job, not the caller's.
        return f"{task_id}/{run_id}.jsonl"

    def load_events(self, task_id: str, run_id: str) -> list[dict[str, Any]] | None:
        """Load a run's event timeline, or ``None`` if no timeline was saved."""
        path = self._events_path(task_id, run_id)
        if not path.exists():
            return None
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                # One bad line should not poison the whole replay; the
                # frontend just sees a slightly shorter timeline.
                logger.warning("Skipping malformed event line in %s", path)
                continue
        return events

    def get_run(self, task_id: str, run_id: str) -> AgentRun | None:
        """Find a single run record by run_id (linear scan, OK for <1000 runs)."""
        for run in self.list_runs(task_id, limit=10_000):
            if run.run_id == run_id:
                return run
        return None


# ── Boundary helpers (shared between REST router + CLI service) ─────
# Kept here (next to the types they operate on) so REST endpoints and the
# CLI service stay in lock-step. Both import these directly. Raise
# ValueError; callers translate to their preferred error envelope.


def validate_trigger(
    store: AgentStore, trigger: Trigger | None, *, owner_task_id: str | None = None
) -> None:
    """Reject obviously broken trigger configs at the API/CLI boundary.

    - downstream task_id must exist
    - no self-loop (task triggering itself)

    Deeper cycle detection (A->B->A) is left to runtime safety; the value of
    a deep check here is low compared to the implementation cost.

    Raises:
        ValueError: with a human-readable message when the trigger is invalid.
    """
    if trigger is None:
        return
    if owner_task_id is not None and trigger.task_id == owner_task_id:
        raise ValueError("Trigger target cannot be the task itself (would self-loop).")
    if store.get_task(trigger.task_id) is None:
        raise ValueError(f"Trigger target task '{trigger.task_id}' not found.")


def merge_runtime_input(task: AgentTask, runtime_input: dict[str, Any] | None) -> AgentTask:
    """Return a shallow-copied task with runtime_input merged into its action.

    The original task is NOT mutated; we copy because the scheduler-side
    ``run_task_once`` persists ``task.last_run_at`` / ``next_run_at`` on the
    real stored task, not on the merged ghost.

    Per-action-type semantics:
    - ``ai_agent``: ``runtime_input.prompt`` (string) is appended to the
      persisted prompt as a labeled section so the AI sees both the
      operator's static instructions and the runtime ask.
    - ``cli_command``: ``runtime_input.argv`` (list of strings) is appended
      to the persisted argv list.
    """
    if not runtime_input:
        return task
    merged_params = dict(task.action.params)
    if task.action.type == "ai_agent":
        extra = runtime_input.get("prompt")
        if isinstance(extra, str) and extra.strip():
            base_prompt = str(merged_params.get("prompt", ""))
            merged_params["prompt"] = (
                f"{base_prompt}\n\n[Operator's runtime input for this run]\n{extra.strip()}"
            )
    elif task.action.type == "cli_command":
        extra_argv = runtime_input.get("argv")
        if isinstance(extra_argv, list) and extra_argv:
            base_argv = list(merged_params.get("argv") or [])
            merged_params["argv"] = [*base_argv, *(str(a) for a in extra_argv)]
    merged_action = AgentAction(type=task.action.type, params=merged_params)
    return task.model_copy(update={"action": merged_action})
