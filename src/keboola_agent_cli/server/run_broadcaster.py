"""Live broadcast of in-flight agent runs (UI "Run live" + re-attach).

Design rationale
================

The scheduler-driven flow (cron loop -> ``run_task_once``) is fire-and-forget:
the run completes whether or not anyone is watching. For the UI button "Run
live" we want something stricter:

- **Single source of truth per task**: if Alice clicks Run in tab A and Bob
  opens the same task in tab B, both must see the SAME run, not duplicate
  spawns. The broadcaster keys runs by ``task_id``.
- **Late attach**: Bob may connect 30s after Alice. He must see events
  Alice already saw (replay) before being placed in the live tail.
- **Kill-on-empty**: when every UI subscriber disconnects, the run is
  cancelled. UI-initiated runs are user-owned; they should not become
  zombies if the operator closes their browser. (The scheduler keeps its
  own independent dispatch path -- those runs survive UI disconnect.)

The implementation is one ``_ActiveRun`` per in-flight task, each holding:
- ``events``: replay buffer of every event already produced (with monotonic
  ``seq`` field for dedupe across the replay/live boundary).
- ``subscribers``: set of asyncio.Queue, one per attached client.
- ``runner_task``: the asyncio.Task that consumes
  ``stream_ai_agent_events`` and broadcasts each event.

Concurrency
===========

All access goes through one asyncio.Lock guarding the registry dict. Per-
task state is then accessed without further locking because the runner task
and the subscriber coroutines all live on the same event loop -- adds/removes
to ``subscribers`` happen between awaits.

The dedupe at the replay/live boundary is the only subtle point. Each event
carries ``seq`` (assigned by the broadcaster, not by the underlying agent
generator). On subscribe:

1. We add our queue to ``subscribers`` (so we get every event pushed AFTER
   this point).
2. We snapshot ``list(events)`` -- the events appended BEFORE the queue was
   added.
3. We yield the snapshot first (replay), tracking ``last_seq`` as we go.
4. Then we read the queue. If an event's ``seq`` is <= ``last_seq`` (i.e.,
   it was already in the snapshot), we skip it. Otherwise yield it.

This makes the boundary race-free without locks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from ..constants import HTTP_DEFAULT_TIMEOUT
from .agent_runner import (
    _now_utc,
    compute_next_run,
    stream_ai_agent_events,
)
from .agents_store import AgentRun, AgentStore, AgentTask

logger = logging.getLogger(__name__)


# Sentinel pushed into a subscriber queue when the run has finished.
# A bare ``None`` would also work; using a named object makes intent
# explicit at consumer call sites.
_DONE_SENTINEL: Any = object()


class _ActiveRun:
    """One in-flight run for a single task_id.

    Lives until ``runner_task`` completes AND ``subscribers`` is empty.
    The broadcaster removes it from the registry when both conditions hold.
    """

    def __init__(
        self,
        task: AgentTask,
        registry: Any,
        store: AgentStore,
    ) -> None:
        self.task = task
        self.registry = registry
        self.store = store
        self.events: list[dict[str, Any]] = []
        self.subscribers: set[asyncio.Queue[Any]] = set()
        self.runner_task: asyncio.Task[None] | None = None
        self.done = False
        self._next_seq = 0
        # Final run record (for callers that want to inspect the result
        # without walking the event stream again).
        self.final_run: AgentRun | None = None

    def _assign_seq(self, evt: dict[str, Any]) -> dict[str, Any]:
        """Tag event with a monotonic seq used for replay/live dedupe."""
        out = dict(evt)
        out["seq"] = self._next_seq
        self._next_seq += 1
        return out

    async def _run(self) -> None:
        """Drive the underlying agent generator + fan out + persist."""
        started_at = _now_utc().isoformat()
        agent_run = AgentRun(task_id=self.task.id, started_at=started_at)
        try:
            if self.task.action.type != "ai_agent":
                # Only ai_agent has structured streaming today. For
                # cli_command / mcp_tool we fall back to a single
                # "done" event so the UI gets one consolidated payload.
                # (Splitting these into a streaming generator is a
                # follow-up; their output is one-shot text/JSON anyway.)
                from .agent_runner import _run_cli, _run_mcp_tool

                if self.task.action.type == "cli_command":
                    output = await _run_cli(self.registry, self.task.action.params)
                else:
                    output = await _run_mcp_tool(self.registry, self.task.action.params)
                await self._publish(
                    {
                        "event": "done",
                        "data": {
                            "status": "ok",
                            **output,
                        },
                    }
                )
                agent_run.status = "ok"
                agent_run.output = output if isinstance(output, dict) else {"value": output}
                return
            params = dict(self.task.action.params)
            params.setdefault("timeout", HTTP_DEFAULT_TIMEOUT * 10)  # ai agents can be long
            async for evt in stream_ai_agent_events(self.registry, params):
                await self._publish(evt)
                if evt["event"] == "done":
                    d = evt["data"]
                    agent_run.status = d.get("status", "ok")
                    agent_run.output = d
                    agent_run.ended_at = d.get("ended_at")
                    if d.get("error"):
                        agent_run.error = d["error"]
        except asyncio.CancelledError:
            agent_run.status = "error"
            agent_run.error = "cancelled (last subscriber disconnected)"
            agent_run.ended_at = _now_utc().isoformat()
            await self._publish(
                {
                    "event": "done",
                    "data": {
                        "status": "error",
                        "error": agent_run.error,
                    },
                }
            )
            raise
        except Exception as exc:
            logger.exception("RunBroadcaster: run failed for task %s", self.task.id)
            agent_run.status = "error"
            agent_run.error = str(exc)
            agent_run.ended_at = _now_utc().isoformat()
            await self._publish(
                {
                    "event": "done",
                    "data": {"status": "error", "error": str(exc)},
                }
            )
        finally:
            if not agent_run.ended_at:
                agent_run.ended_at = _now_utc().isoformat()
            self.done = True
            self.final_run = agent_run
            # Persist the run record + bump last/next on the task.
            with contextlib.suppress(Exception):
                self.store.append_run(agent_run)
                self.task.last_run_at = agent_run.started_at
                self.task.next_run_at = compute_next_run(self.task.cron)
                self.store.upsert_task(self.task)
            # Wake every subscriber so their generator can exit.
            for q in list(self.subscribers):
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(_DONE_SENTINEL)

    async def _publish(self, evt: dict[str, Any]) -> None:
        """Tag with seq, append to replay buffer, push to live subscribers."""
        tagged = self._assign_seq(evt)
        self.events.append(tagged)
        for q in list(self.subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(tagged)

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        """Replay buffered events, then tail live until the run completes.

        Removes itself from ``subscribers`` on exit so the broadcaster can
        garbage-collect the active run + kill the runner if nobody is left.
        """
        q: asyncio.Queue[Any] = asyncio.Queue()
        # Add q BEFORE snapshotting events so we don't lose any event that
        # arrives in the gap. Duplicates from that window are filtered out
        # by the seq-dedupe step below.
        self.subscribers.add(q)
        last_seq = -1
        try:
            for evt in list(self.events):
                yield evt
                last_seq = evt["seq"]
            if self.done:
                return
            while True:
                item = await q.get()
                if item is _DONE_SENTINEL:
                    return
                seq = item.get("seq", -1)
                if seq > last_seq:
                    yield item
                    last_seq = seq
        finally:
            self.subscribers.discard(q)


class RunBroadcaster:
    """Singleton-on-app.state coordinator for UI-driven streaming runs."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._active: dict[str, _ActiveRun] = {}
        # RUF006: hold strong references to fire-and-forget reap tasks so the
        # event loop's WeakSet doesn't GC them mid-flight. The set is pruned
        # via done-callbacks below.
        self._reap_tasks: set[asyncio.Task[None]] = set()

    async def start_or_attach(
        self,
        task: AgentTask,
        registry: Any,
        store: AgentStore,
    ) -> AsyncIterator[dict[str, Any]]:
        """If a run is in flight for ``task.id``, attach. Otherwise start one.

        On final subscriber disconnect (no more queues in ``subscribers``),
        the runner is cancelled -- kill-on-empty for UI-owned runs.
        """
        async with self._lock:
            active = self._active.get(task.id)
            if active is None:
                active = _ActiveRun(task=task, registry=registry, store=store)
                self._active[task.id] = active
                active.runner_task = asyncio.create_task(
                    active._run(), name=f"run-broadcast-{task.id}"
                )
                # Reap the registry entry once the run AND every subscriber
                # are gone. The cleanup task awaits the runner; we then poll
                # for empty subscribers (events drained) before unregistering.
                reap = asyncio.create_task(self._reap_when_done(task.id))
                self._reap_tasks.add(reap)
                reap.add_done_callback(self._reap_tasks.discard)

        try:
            async for evt in active.subscribe():
                yield evt
        finally:
            # If we were the last subscriber AND the runner hasn't finished
            # yet, kill it. (Manual UI runs are user-owned -- closing every
            # tab should not leave a zombie claude subprocess churning
            # tokens.)
            if (
                not active.done
                and not active.subscribers
                and active.runner_task is not None
                and not active.runner_task.done()
            ):
                active.runner_task.cancel()

    async def _reap_when_done(self, task_id: str) -> None:
        active = self._active.get(task_id)
        if active is None or active.runner_task is None:
            return
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await active.runner_task
        # Allow late subscribers to still replay -- but only briefly. Cron
        # runs aside, an unsubscribed completed run should not linger past
        # ~30s of idle (keeps memory bounded under burst of test runs).
        for _ in range(30):
            await asyncio.sleep(1)
            if not active.subscribers:
                break
        async with self._lock:
            if self._active.get(task_id) is active:
                self._active.pop(task_id, None)

    def is_running(self, task_id: str) -> bool:
        """For UI to decide whether the Play button should show 'attach' label."""
        active = self._active.get(task_id)
        return active is not None and not active.done


def install_broadcaster(app: Any) -> RunBroadcaster:
    """Attach a singleton ``RunBroadcaster`` to ``app.state.run_broadcaster``."""
    if not hasattr(app.state, "run_broadcaster"):
        app.state.run_broadcaster = RunBroadcaster()
    return app.state.run_broadcaster  # type: ignore[no-any-return]
