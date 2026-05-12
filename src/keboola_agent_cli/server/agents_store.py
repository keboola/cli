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

ActionType = Literal["mcp_tool", "cli_command", "ai_agent"]


class AgentAction(BaseModel):
    """What the task does when triggered.

    - ``mcp_tool``:    call a keboola-mcp-server tool
        params: { tool: "get_jobs", project: "padak", input: {...} }
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


class AgentTask(BaseModel):
    """A scheduled agent task (cron-driven)."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str
    description: str = ""
    cron: str = "0 * * * *"  # default every hour, top of the hour
    enabled: bool = True
    action: AgentAction
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_run_at: str | None = None
    next_run_at: str | None = None


class AgentRun(BaseModel):
    """One execution of a task."""

    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    task_id: str
    started_at: str
    ended_at: str | None = None
    status: Literal["running", "ok", "error"] = "running"
    output: dict[str, Any] | None = None
    error: str | None = None


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
