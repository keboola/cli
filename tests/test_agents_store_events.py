"""Tests for AgentStore event-timeline persistence (append_events / load_events).

These cover the new per-run JSONL files added in v0.10.x to enable
"replay" of finished agent runs in the detail drawer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from keboola_agent_cli.server.agents_store import (
    AgentAction,
    AgentRun,
    AgentStore,
    AgentTask,
)


@pytest.fixture
def store(tmp_path: Path) -> AgentStore:
    return AgentStore(config_dir=tmp_path / "kbagent")


def _sample_events() -> list[dict]:
    return [
        {
            "event": "stdout",
            "data": {"type": "system", "subtype": "init", "model": "claude-opus-4-7"},
            "seq": 0,
        },
        {
            "event": "stdout",
            "data": {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                    ]
                },
            },
            "seq": 1,
        },
        {
            "event": "stdout",
            "data": {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "is_error": False, "content": "file1\nfile2\n"},
                    ]
                },
            },
            "seq": 2,
        },
        {"event": "done", "data": {"status": "ok", "exit_code": 0}, "seq": 3},
    ]


class TestAppendAndLoadEvents:
    def test_round_trip_preserves_order_and_shape(self, store: AgentStore) -> None:
        events = _sample_events()
        rel = store.append_events("task-abc", "run-xyz", events)
        assert rel == "task-abc/run-xyz.jsonl"
        loaded = store.load_events("task-abc", "run-xyz")
        assert loaded == events

    def test_load_returns_none_when_no_file(self, store: AgentStore) -> None:
        assert store.load_events("nonexistent", "run-xyz") is None

    def test_empty_event_list_creates_empty_file(self, store: AgentStore) -> None:
        rel = store.append_events("task-abc", "run-empty", [])
        assert rel == "task-abc/run-empty.jsonl"
        loaded = store.load_events("task-abc", "run-empty")
        # Empty file != missing file: returns [] not None.
        assert loaded == []

    def test_overwrites_existing_file(self, store: AgentStore) -> None:
        # Same run_id: second write replaces first (idempotent re-run).
        store.append_events("t1", "r1", [{"event": "a", "data": {}, "seq": 0}])
        store.append_events("t1", "r1", [{"event": "b", "data": {}, "seq": 0}])
        loaded = store.load_events("t1", "r1")
        assert loaded == [{"event": "b", "data": {}, "seq": 0}]

    def test_skips_malformed_lines_on_load(self, store: AgentStore, tmp_path: Path) -> None:
        # Hand-craft a file with one valid + one bad line.
        path = tmp_path / "kbagent" / "agent_runs" / "t" / "r.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(
            '{"event": "ok", "data": {}, "seq": 0}\n'
            "not-json\n"
            '{"event": "ok2", "data": {}, "seq": 1}\n',
            encoding="utf-8",
        )
        loaded = store.load_events("t", "r")
        assert loaded is not None
        assert len(loaded) == 2
        assert loaded[0]["event"] == "ok"
        assert loaded[1]["event"] == "ok2"

    def test_file_permissions_are_0600(self, store: AgentStore, tmp_path: Path) -> None:
        store.append_events("t", "r", [{"event": "x", "data": {}, "seq": 0}])
        path = tmp_path / "kbagent" / "agent_runs" / "t" / "r.jsonl"
        # Octal mode of the file. 0o600 = -rw-------
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_per_task_subdirectory_isolation(self, store: AgentStore) -> None:
        # Two tasks, same run_id digit -> distinct files in different subdirs.
        store.append_events(
            "task-a", "shared-id", [{"event": "x", "data": {"task": "a"}, "seq": 0}]
        )
        store.append_events(
            "task-b", "shared-id", [{"event": "x", "data": {"task": "b"}, "seq": 0}]
        )
        a = store.load_events("task-a", "shared-id")
        b = store.load_events("task-b", "shared-id")
        assert a is not None and b is not None
        assert a[0]["data"]["task"] == "a"
        assert b[0]["data"]["task"] == "b"


class TestGetRun:
    def test_finds_run_by_id(self, store: AgentStore) -> None:
        run = AgentRun(task_id="t1", started_at="2026-01-01T00:00:00Z", status="ok")
        store.append_run(run)
        found = store.get_run("t1", run.run_id)
        assert found is not None
        assert found.run_id == run.run_id

    def test_returns_none_for_missing(self, store: AgentStore) -> None:
        assert store.get_run("nope", "nope") is None

    def test_returns_none_for_wrong_task(self, store: AgentStore) -> None:
        run = AgentRun(task_id="t1", started_at="2026-01-01T00:00:00Z", status="ok")
        store.append_run(run)
        # Same run_id but wrong task_id -> file not even consulted.
        assert store.get_run("other", run.run_id) is None


class TestAgentRunNewFields:
    def test_summary_and_events_path_are_optional(self) -> None:
        run = AgentRun(task_id="t", started_at="2026-01-01T00:00:00Z")
        assert run.summary is None
        assert run.events_path is None

    def test_round_trip_through_jsonl(self, store: AgentStore) -> None:
        run = AgentRun(
            task_id="t",
            started_at="2026-01-01T00:00:00Z",
            status="ok",
            summary={
                "model": "claude-opus-4-7",
                "tokens": {"total": 1500, "input": 1000, "output": 500},
                "cost_usd": {"total": 0.0525, "source": "claude_result"},
            },
            events_path="t/run-1.jsonl",
        )
        store.append_run(run)
        loaded_runs = store.list_runs("t")
        assert len(loaded_runs) == 1
        loaded = loaded_runs[0]
        assert loaded.summary is not None
        assert loaded.summary["model"] == "claude-opus-4-7"
        assert loaded.summary["cost_usd"]["total"] == 0.0525
        assert loaded.events_path == "t/run-1.jsonl"


def test_task_action_construction() -> None:
    """Sanity: AgentAction shape unchanged so existing API contract holds."""
    task = AgentTask(
        name="x",
        cron="0 * * * *",
        action=AgentAction(type="ai_agent", params={"cli": "claude", "prompt": "hi"}),
    )
    assert task.action.type == "ai_agent"
