"""Agent task service - business logic for `kbagent agent` CLI parity.

Wraps :class:`~keboola_agent_cli.server.agents_store.AgentStore` and the
in-process scheduler runner so CLI commands can perform the same CRUD +
run + history operations that the REST router (`/agents`) exposes when
``kbagent serve`` is running. The on-disk format is identical -- a task
created via ``kbagent agent create`` will fire on the cron schedule as
soon as ``kbagent serve`` starts and reads ``agents.json``.

Why a separate "service" rather than calling ``AgentStore`` directly from
CLI commands?

1. Encapsulate the boundary helpers (trigger validation, runtime_input
   merging, next-run computation) so commands stay thin.
2. Build a *minimal* runtime registry on demand for ``run_task`` /
   ``stream_run`` -- ``run_task_once`` and ``stream_ai_agent_events`` only
   need ``config_store`` (for env propagation). The full FastAPI
   ``ServiceRegistry`` (with all 25+ services) is overkill for the CLI path.
3. Translate :class:`ValueError` from the boundary helpers into
   :class:`ConfigError` so the CLI exit-code mapper handles them uniformly.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from croniter import croniter

from ..config_store import ConfigStore
from ..constants import AI_PROMPT_HELPER_TIMEOUT
from ..errors import ConfigError
from ..server.agent_runner import (
    build_prompt_helper_meta_prompt,
    clean_prompt_helper_response,
    compute_next_run,
    run_task_once,
    stream_ai_agent_events,
)
from ..server.agents_store import (
    AgentAction,
    AgentRun,
    AgentStore,
    AgentTask,
    Trigger,
    merge_runtime_input,
    validate_trigger,
)

logger = logging.getLogger(__name__)


@dataclass
class CliAgentRegistry:
    """Minimal registry passed to ``run_task_once`` / ``stream_ai_agent_events``.

    The runner duck-types its ``registry`` argument: it only reads
    ``config_store`` (for ``_build_subprocess_env``) and the optional
    ``serve_url`` / ``serve_token`` (None means "no live serve to call back into"; the
    spawned subprocess just falls through to local CLI mode).

    Mirrors the shape of :class:`~keboola_agent_cli.server.dependencies.ServiceRegistry`
    closely enough that the runner cannot tell them apart -- which lets
    the CLI reuse the exact same execution code path the cron scheduler
    uses inside ``kbagent serve``.
    """

    config_store: ConfigStore
    serve_url: str | None = None
    serve_token: str | None = None


class _NullStore:
    """In-memory drop-in for :class:`AgentStore` used by ``test_action``.

    The runner calls ``append_run`` (persist the AgentRun) and
    ``upsert_task`` (refresh last_run_at). For an ad-hoc preview we want
    *neither* to touch disk, so both are no-ops. ``append_events`` is also
    declared so the ai_agent code path can persist its timeline without
    crashing -- we throw the bytes away.

    ``get_task`` returns ``None`` because trigger fan-out is suppressed for
    preview runs (a transient task has no persisted downstream chain).
    """

    def append_run(self, _run: AgentRun) -> None:
        return None

    def upsert_task(self, task: AgentTask) -> AgentTask:
        return task

    def append_events(self, _task_id: str, _run_id: str, _events: list[dict[str, Any]]) -> str:
        return ""

    def get_task(self, _task_id: str) -> AgentTask | None:
        return None


class AgentService:
    """CLI-side business logic for scheduled agent tasks.

    Reads/writes ``<config_dir>/agents.json`` via :class:`AgentStore` and
    spawns task actions via the same runner used by the cron loop inside
    ``kbagent serve``. The two paths share the on-disk format, so a CLI
    create + ``serve`` boot picks up the new task on the next tick.
    """

    def __init__(self, config_store: ConfigStore) -> None:
        self._config_store = config_store
        # Resolve the actual config directory (not the value passed by tests)
        # so CLI + serve always agree on the agents.json location.
        config_dir = config_store.config_path.parent
        self._store = AgentStore(config_dir=config_dir)

    # ── CRUD ───────────────────────────────────────────────────────────

    def list_tasks(self) -> list[AgentTask]:
        return self._store.load_tasks()

    def get_task(self, task_id: str) -> AgentTask:
        task = self._store.get_task(task_id)
        if task is None:
            raise ConfigError(f"Task '{task_id}' not found.")
        return task

    def create_task(
        self,
        *,
        name: str,
        action: AgentAction,
        description: str = "",
        cron: str = "0 * * * *",
        manual: bool = False,
        enabled: bool = True,
        trigger: Trigger | None = None,
    ) -> AgentTask:
        """Persist a new task. Validates cron + trigger before writing."""
        # Cron is validated by computing the next firing; a bad expression
        # would emit a warning and return None inside ``compute_next_run``.
        # We turn that into a hard error so the operator notices.
        if not manual:
            try:
                croniter(cron, datetime.now(UTC))
            except Exception as exc:
                raise ConfigError(f"Invalid cron expression {cron!r}: {exc}") from exc
        try:
            validate_trigger(self._store, trigger)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        task = AgentTask(
            name=name,
            description=description,
            cron=cron,
            manual=manual,
            enabled=enabled,
            action=action,
            trigger=trigger,
            next_run_at=None if manual else compute_next_run(cron),
        )
        return self._store.upsert_task(task)

    def update_task(
        self,
        task_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        cron: str | None = None,
        manual: bool | None = None,
        enabled: bool | None = None,
        action: AgentAction | None = None,
        trigger: Trigger | None = None,
        clear_trigger: bool = False,
    ) -> AgentTask:
        """Patch fields. ``clear_trigger`` is required to set trigger to None.

        Pydantic's "field absent vs explicit null" can't be distinguished by
        the CLI surface, so callers pass an explicit boolean instead.
        """
        task = self.get_task(task_id)
        if name is not None:
            task.name = name
        if description is not None:
            task.description = description
        if cron is not None:
            try:
                croniter(cron, datetime.now(UTC))
            except Exception as exc:
                raise ConfigError(f"Invalid cron expression {cron!r}: {exc}") from exc
            task.cron = cron
            task.next_run_at = compute_next_run(cron)
        if manual is not None:
            task.manual = manual
            task.next_run_at = None if manual else compute_next_run(task.cron)
        if enabled is not None:
            task.enabled = enabled
        if action is not None:
            task.action = action
        if clear_trigger:
            task.trigger = None
        elif trigger is not None:
            try:
                validate_trigger(self._store, trigger, owner_task_id=task.id)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
            task.trigger = trigger
        return self._store.upsert_task(task)

    def delete_task(self, task_id: str) -> None:
        if not self._store.delete_task(task_id):
            raise ConfigError(f"Task '{task_id}' not found.")

    # ── Run history ────────────────────────────────────────────────────

    def list_runs(self, task_id: str, *, limit: int = 50) -> list[AgentRun]:
        # Validate the task exists so callers don't get a silent empty list
        # for a typo'd task_id.
        self.get_task(task_id)
        return self._store.list_runs(task_id, limit=limit)

    def get_run(self, task_id: str, run_id: str) -> AgentRun:
        self.get_task(task_id)
        run = self._store.get_run(task_id, run_id)
        if run is None:
            raise ConfigError(f"Run '{run_id}' not found for task '{task_id}'.")
        return run

    def get_run_events(self, task_id: str, run_id: str) -> list[dict[str, Any]]:
        self.get_task(task_id)
        events = self._store.load_events(task_id, run_id)
        if events is None:
            raise ConfigError(
                f"No event timeline persisted for run '{run_id}' "
                "(only ai_agent runs from v0.10+ carry one)."
            )
        return events

    # ── Execution (blocking) ───────────────────────────────────────────

    async def run_task(
        self, task_id: str, *, runtime_input: dict[str, Any] | None = None
    ) -> AgentRun:
        """Trigger a task immediately, blocking until it finishes.

        Mirrors POST /agents/{id}/run. Persists a new AgentRun on disk,
        updates last_run_at + next_run_at, fans out chained triggers.
        """
        task = self.get_task(task_id)
        task_for_run = merge_runtime_input(task, runtime_input)
        registry = self._build_registry()
        return await run_task_once(task_for_run, registry, self._store)

    async def test_action(self, action: AgentAction, *, name: str = "[preview]") -> AgentRun:
        """Run an action ad-hoc without persisting -- the /agents/test endpoint."""
        transient = AgentTask(name=name, enabled=False, action=action)
        registry = self._build_registry()
        # ty: _NullStore is a no-op AgentStore stand-in -- test_action runs ad-hoc and
        # never persists, so run_task_once's store argument is intentionally unused here.
        return await run_task_once(transient, registry, _NullStore())  # ty: ignore[invalid-argument-type]

    # ── Execution (streaming) ──────────────────────────────────────────
    # The two streaming methods return AsyncIterator[dict] mirroring the
    # SSE event shape (``{"event": ..., "data": ...}``) so CLI commands
    # can render each event verbatim without re-parsing.

    async def stream_run(
        self, task_id: str, *, runtime_input: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream a real (persisted) run with init + interleaved events + done.

        For non-ai_agent action types the runner has no incremental events,
        so we emit a single ``done`` event with the final run record.
        """
        task = self.get_task(task_id)
        task_for_run = merge_runtime_input(task, runtime_input)
        registry = self._build_registry()
        yield {
            "event": "init",
            "data": {
                "task_id": task_for_run.id,
                "name": task_for_run.name,
                "action_type": task_for_run.action.type,
            },
        }
        if task_for_run.action.type == "ai_agent":
            # Drive the runner manually so we can yield each event live AND
            # persist the run at the end. This duplicates run_task_once's
            # bookkeeping deliberately -- run_task_once is sync-completion
            # (returns the AgentRun); we need an async generator instead.
            async for evt in self._stream_persisted_ai_run(task_for_run):
                yield evt
        else:
            run = await run_task_once(task_for_run, registry, self._store)
            yield {"event": "done", "data": run.model_dump(mode="json")}

    async def stream_test_action(
        self, action: AgentAction, *, name: str = "[preview]"
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream a preview run -- mirrors POST /agents/test/stream.

        The non-streaming action type (cli_command) emits a single
        ``done`` event with the run dict, matching the server's behavior.
        """
        yield {
            "event": "init",
            "data": {"name": name, "action_type": action.type, "cron": "*"},
        }
        if action.type == "ai_agent":
            registry = self._build_registry()
            try:
                async for evt in stream_ai_agent_events(registry, action.params):
                    yield evt
            except ValueError as exc:
                yield {"event": "done", "data": {"status": "error", "error": str(exc)}}
        else:
            run = await self.test_action(action, name=name)
            yield {"event": "done", "data": run.model_dump(mode="json")}

    async def _stream_persisted_ai_run(self, task: AgentTask) -> AsyncIterator[dict[str, Any]]:
        """Walk stream_ai_agent_events, persist the run on done.

        Re-implements the persistence side of ``run_task_once`` for the
        streaming case. Kept in sync with the server's behaviour: same
        events_path/summary/last_run_at side effects, same chain fan-out.
        """
        from datetime import UTC, datetime

        from ..server.pricing import build_run_summary

        registry = self._build_registry()
        started = datetime.now(UTC).replace(microsecond=0).isoformat()
        run = AgentRun(task_id=task.id, started_at=started)
        captured: list[dict[str, Any]] = []
        done_payload: dict[str, Any] | None = None
        try:
            async for evt in stream_ai_agent_events(registry, task.action.params):
                captured.append(evt)
                if evt["event"] == "done":
                    done_payload = evt["data"]
                yield evt
        except ValueError as exc:
            done_payload = {"status": "error", "error": str(exc)}
            yield {"event": "done", "data": done_payload}
        finally:
            ended = datetime.now(UTC).replace(microsecond=0).isoformat()
            run.ended_at = ended
            if done_payload is None:
                run.status = "error"
                run.error = "ai_agent stream ended without a done event"
            else:
                run.status = done_payload.get("status", "ok")
                run.output = done_payload
                if done_payload.get("error"):
                    run.error = done_payload["error"]
            if captured:
                try:
                    run.summary = build_run_summary(captured)
                    run.events_path = self._store.append_events(task.id, run.run_id, captured)
                except Exception:
                    logger.exception(
                        "Failed to persist event timeline for %s/%s", task.id, run.run_id
                    )
            self._store.append_run(run)
            persisted = self._store.get_task(task.id)
            if persisted is not None:
                persisted.last_run_at = run.started_at
                persisted.next_run_at = (
                    None if persisted.manual else compute_next_run(persisted.cron)
                )
                self._store.upsert_task(persisted)

    # ── Utilities ──────────────────────────────────────────────────────

    def cron_preview(self, cron: str, *, count: int = 5) -> list[str]:
        """Return ISO timestamps of the next N cron firings.

        Capped at 20 to match the REST endpoint and to keep CLI output
        readable on smaller terminals.
        """
        bounded = max(1, min(count, 20))
        try:
            it = croniter(cron, datetime.now(UTC))
        except Exception as exc:
            raise ConfigError(f"Invalid cron expression {cron!r}: {exc}") from exc
        firings: list[str] = []
        for _ in range(bounded):
            dt = it.get_next(datetime)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            firings.append(dt.isoformat())
        return firings

    async def improve_prompt(
        self,
        *,
        cli: str,
        goal: str,
        draft: str = "",
        project: str | None = None,
        extra_args: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream a polished prompt back to the caller -- one-to-one with the SSE route.

        Yields the same event shapes (``init`` / ``stdout`` / ``stderr`` /
        ``done``) the test-stream endpoint emits. The final ``done`` event
        is enriched with a ``prompt`` field carrying the cleaned response.
        """
        goal_clean = goal.strip()
        if not goal_clean:
            raise ConfigError("goal must not be empty")
        meta_prompt = build_prompt_helper_meta_prompt(goal=goal_clean, draft=draft, project=project)
        params: dict[str, Any] = {
            "cli": cli,
            "prompt": meta_prompt,
            "extra_args": extra_args or [],
            "timeout": AI_PROMPT_HELPER_TIMEOUT,
        }
        registry = self._build_registry()
        yield {
            "event": "init",
            "data": {"kind": "prompt_helper", "cli": cli, "goal_preview": goal_clean[:200]},
        }
        try:
            async for evt in stream_ai_agent_events(registry, params):
                if evt["event"] == "done":
                    raw = str(evt["data"].get("response") or "")
                    yield {
                        "event": "done",
                        "data": {
                            **evt["data"],
                            "prompt": clean_prompt_helper_response(raw),
                            "raw_response": raw,
                        },
                    }
                else:
                    yield evt
        except ValueError as exc:
            yield {"event": "done", "data": {"status": "error", "error": str(exc)}}

    # ── Internals ──────────────────────────────────────────────────────

    def _build_registry(self) -> CliAgentRegistry:
        """Construct the minimal registry passed to the runner.

        ``serve_url`` / ``serve_token`` are intentionally ``None`` in the
        CLI path: there is no live serve instance to expose to spawned
        subprocesses. Children fall back to local CLI mode against
        ``KBAGENT_CONFIG_DIR``.
        """
        return CliAgentRegistry(config_store=self._config_store)
