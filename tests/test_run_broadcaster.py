"""Tests for RunBroadcaster: start-or-attach, replay dedupe, kill-on-empty.

The broadcaster's job is to fan out one in-flight agent run to N SSE
subscribers, with late-attach support (replay buffer) and kill-on-empty
semantics (UI-owned runs die when nobody is listening). The tricky parts:

- Two subscribers connecting at different times see the same totally-ordered
  event stream, with no duplicates at the replay/live boundary.
- Cancellation of the last subscriber cancels the underlying runner task.
- The persistent ``AgentRun`` record is written via ``store.append_run`` even
  on cancellation.

We mock ``stream_ai_agent_events`` with a scripted async iterator so we can
exactly control event timing.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.server.agents_store import AgentAction, AgentTask
from keboola_agent_cli.server.run_broadcaster import RunBroadcaster


class _FakeStore:
    """Minimal AgentStore stand-in: records what was appended/upserted."""

    def __init__(self) -> None:
        self.runs: list[Any] = []
        self.tasks: list[AgentTask] = []

    def append_run(self, run: Any) -> None:
        self.runs.append(run)

    def upsert_task(self, task: AgentTask) -> AgentTask:
        self.tasks.append(task)
        return task


def _make_task() -> AgentTask:
    return AgentTask(
        id="task-123",
        name="test",
        cron="0 0 * * *",
        enabled=True,
        action=AgentAction(
            type="ai_agent",
            params={"cli": "claude", "prompt": "hello"},
        ),
    )


async def _scripted_events(events: list[dict[str, Any]], delay: float = 0.01):
    """Yield ``events`` one at a time, sleeping briefly so subscribers can attach."""
    for evt in events:
        await asyncio.sleep(delay)
        yield evt


class TestSingleSubscriber:
    @pytest.mark.asyncio
    async def test_yields_all_events_in_order(self, monkeypatch) -> None:
        events = [
            {"event": "stdout", "data": {"type": "system", "subtype": "init"}},
            {"event": "stdout", "data": {"type": "assistant"}},
            {"event": "done", "data": {"status": "ok", "exit_code": 0}},
        ]
        monkeypatch.setattr(
            "keboola_agent_cli.server.run_broadcaster.stream_ai_agent_events",
            lambda _r, _p: _scripted_events(events),
        )
        bcast = RunBroadcaster()
        task = _make_task()
        store = _FakeStore()
        registry: Any = MagicMock()

        out: list[dict[str, Any]] = []
        async for evt in bcast.start_or_attach(task, registry, store):
            out.append(evt)

        # init + 3 scripted events = 3 (the broadcaster doesn't add init -- that
        # comes from the SSE endpoint layer). Subscriber sees stdout/stdout/done.
        assert [e["event"] for e in out] == ["stdout", "stdout", "done"]
        assert out[0]["data"]["type"] == "system"
        # Each event carries a monotonic seq for dedupe across replay/live.
        assert [e["seq"] for e in out] == [0, 1, 2]
        # Final AgentRun is persisted with status from `done` event.
        assert len(store.runs) == 1
        assert store.runs[0].status == "ok"


class TestLateAttach:
    @pytest.mark.asyncio
    async def test_second_subscriber_sees_replay_then_live(self, monkeypatch) -> None:
        # 4 events spread across ~80 ms, so a second subscriber attached
        # after ~25 ms gets at least 1 replay + the rest live.
        events = [
            {"event": "stdout", "data": {"i": 0}},
            {"event": "stdout", "data": {"i": 1}},
            {"event": "stdout", "data": {"i": 2}},
            {"event": "done", "data": {"status": "ok"}},
        ]
        monkeypatch.setattr(
            "keboola_agent_cli.server.run_broadcaster.stream_ai_agent_events",
            lambda _r, _p: _scripted_events(events, delay=0.02),
        )
        bcast = RunBroadcaster()
        task = _make_task()
        store = _FakeStore()
        registry: Any = MagicMock()

        a_out: list[dict[str, Any]] = []
        b_out: list[dict[str, Any]] = []

        async def consume_a() -> None:
            async for evt in bcast.start_or_attach(task, registry, store):
                a_out.append(evt)

        async def consume_b() -> None:
            # Attach mid-flight.
            await asyncio.sleep(0.025)
            async for evt in bcast.start_or_attach(task, registry, store):
                b_out.append(evt)

        await asyncio.gather(consume_a(), consume_b())

        # Both subscribers see the SAME ordered stream, with NO duplicate seqs.
        assert [e["seq"] for e in a_out] == [0, 1, 2, 3]
        assert [e["seq"] for e in b_out] == [0, 1, 2, 3]
        # Single persistent run record (one underlying runner task).
        assert len(store.runs) == 1
