"""Unit tests for AgentService.

Round-trip tests against a real :class:`AgentStore` on a tmp dir -- the
store is small enough (single JSON file + per-task JSONL) that mocking
adds more friction than value, and the on-disk format is part of the
public contract between CLI and ``kbagent serve``.

Runner side effects (subprocess spawn) are mocked: the tests assert on
the *service* boundary, not on what claude/codex actually emit.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError
from keboola_agent_cli.server.agents_store import AgentAction, AgentRun, Trigger
from keboola_agent_cli.services.agent_service import AgentService


@pytest.fixture
def agent_service(tmp_config_dir: Path) -> AgentService:
    """Real service backed by a temp config dir."""
    store = ConfigStore(config_dir=tmp_config_dir)
    return AgentService(config_store=store)


def _ai_action(prompt: str = "do something") -> AgentAction:
    return AgentAction(type="ai_agent", params={"cli": "claude", "prompt": prompt})


def _cli_action(*argv: str) -> AgentAction:
    return AgentAction(type="cli_command", params={"argv": list(argv) or ["version"]})


class TestCrud:
    def test_list_empty(self, agent_service: AgentService) -> None:
        assert agent_service.list_tasks() == []

    def test_create_round_trip(self, agent_service: AgentService) -> None:
        task = agent_service.create_task(name="Nightly", cron="0 3 * * *", action=_ai_action())
        assert task.name == "Nightly"
        assert task.next_run_at is not None
        again = agent_service.get_task(task.id)
        assert again.id == task.id
        assert again.action.type == "ai_agent"

    def test_create_manual_no_next_run(self, agent_service: AgentService) -> None:
        task = agent_service.create_task(
            name="Manual", manual=True, action=_cli_action("project", "list")
        )
        assert task.manual is True
        # Manual tasks don't advertise a next firing.
        assert task.next_run_at is None

    def test_create_invalid_cron(self, agent_service: AgentService) -> None:
        with pytest.raises(ConfigError, match="Invalid cron"):
            agent_service.create_task(name="Bad", cron="abc def", action=_cli_action())

    def test_get_missing(self, agent_service: AgentService) -> None:
        with pytest.raises(ConfigError, match="not found"):
            agent_service.get_task("nope-xxxxxx")

    def test_update_fields(self, agent_service: AgentService) -> None:
        task = agent_service.create_task(name="A", action=_cli_action())
        updated = agent_service.update_task(task.id, name="A-renamed", enabled=False, manual=True)
        assert updated.name == "A-renamed"
        assert updated.enabled is False
        assert updated.manual is True
        assert updated.next_run_at is None  # manual nulls it

    def test_update_recomputes_next_run_when_back_to_auto(
        self, agent_service: AgentService
    ) -> None:
        task = agent_service.create_task(
            name="A", manual=True, action=_cli_action(), cron="0 4 * * *"
        )
        re = agent_service.update_task(task.id, manual=False)
        assert re.next_run_at is not None

    def test_delete(self, agent_service: AgentService) -> None:
        task = agent_service.create_task(name="A", action=_cli_action())
        agent_service.delete_task(task.id)
        with pytest.raises(ConfigError):
            agent_service.get_task(task.id)

    def test_delete_missing(self, agent_service: AgentService) -> None:
        with pytest.raises(ConfigError, match="not found"):
            agent_service.delete_task("does-not-exist")


class TestTriggerValidation:
    def test_trigger_target_missing(self, agent_service: AgentService) -> None:
        with pytest.raises(ConfigError, match="not found"):
            agent_service.create_task(
                name="WithBadTrigger",
                action=_cli_action(),
                trigger=Trigger(on="success", task_id="nonexistent"),
            )

    def test_self_loop_rejected(self, agent_service: AgentService) -> None:
        task = agent_service.create_task(name="Loopy", action=_cli_action())
        with pytest.raises(ConfigError, match="self-loop"):
            agent_service.update_task(task.id, trigger=Trigger(on="success", task_id=task.id))

    def test_valid_chain(self, agent_service: AgentService) -> None:
        upstream = agent_service.create_task(name="Up", action=_cli_action())
        downstream = agent_service.create_task(
            name="Down",
            action=_cli_action(),
            trigger=Trigger(on="success", task_id=upstream.id),
        )
        assert downstream.trigger is not None
        assert downstream.trigger.task_id == upstream.id

    def test_clear_trigger(self, agent_service: AgentService) -> None:
        up = agent_service.create_task(name="Up", action=_cli_action())
        down = agent_service.create_task(
            name="Down",
            action=_cli_action(),
            trigger=Trigger(on="success", task_id=up.id),
        )
        cleared = agent_service.update_task(down.id, clear_trigger=True)
        assert cleared.trigger is None


class TestCronPreview:
    def test_preview_returns_strings(self, agent_service: AgentService) -> None:
        firings = agent_service.cron_preview("0 6 * * 1", count=3)
        assert len(firings) == 3
        # All ISO timestamps with timezone
        for ts in firings:
            assert "T" in ts and ("+" in ts or "Z" in ts)

    def test_preview_clamps_count(self, agent_service: AgentService) -> None:
        firings = agent_service.cron_preview("* * * * *", count=99)
        assert len(firings) == 20  # capped

    def test_preview_invalid(self, agent_service: AgentService) -> None:
        with pytest.raises(ConfigError, match="Invalid cron"):
            agent_service.cron_preview("not a cron")


class TestRuntimeInputMerge:
    """Spot-check the helper goes through unchanged via the service path.

    The full per-action merge is unit-tested in ``test_agents_store_events``-
    adjacent suites; here we assert the service calls into the helper and
    surfaces the merged params on the run.
    """

    def test_runtime_prompt_appended_to_ai_agent(self, agent_service: AgentService) -> None:
        from keboola_agent_cli.server.agents_store import merge_runtime_input

        task = agent_service.create_task(name="AI", action=_ai_action(prompt="base"))
        merged = merge_runtime_input(task, {"prompt": "extra"})
        assert "base" in merged.action.params["prompt"]
        assert "extra" in merged.action.params["prompt"]
        # Original task is not mutated.
        again = agent_service.get_task(task.id)
        assert again.action.params["prompt"] == "base"


class TestRunTaskMocked:
    """run_task delegates to run_task_once; we mock that boundary."""

    def test_run_task_persists_run(self, agent_service: AgentService) -> None:
        task = agent_service.create_task(name="A", action=_cli_action())

        async def _fake_run_task_once(t, _registry, store, **_kwargs) -> AgentRun:
            run = AgentRun(task_id=t.id, started_at="2026-01-01T00:00:00+00:00", status="ok")
            store.append_run(run)
            return run

        with patch(
            "keboola_agent_cli.services.agent_service.run_task_once",
            side_effect=_fake_run_task_once,
        ):
            result = asyncio.run(agent_service.run_task(task.id))
        assert result.status == "ok"
        runs = agent_service.list_runs(task.id)
        assert len(runs) == 1

    def test_run_task_with_runtime_prompt(self, agent_service: AgentService) -> None:
        task = agent_service.create_task(name="AI", action=_ai_action(prompt="base"))
        captured: dict = {}

        async def _capture(t, _registry, _store, **_kwargs) -> AgentRun:
            captured["params"] = dict(t.action.params)
            return AgentRun(task_id=t.id, started_at="2026-01-01T00:00:00+00:00", status="ok")

        with patch("keboola_agent_cli.services.agent_service.run_task_once", side_effect=_capture):
            asyncio.run(agent_service.run_task(task.id, runtime_input={"prompt": "now"}))
        assert "base" in captured["params"]["prompt"]
        assert "now" in captured["params"]["prompt"]


class TestStreamTestAction:
    """Streaming preview yields init + done envelope for non-ai action types."""

    def test_stream_cli_command_yields_init_then_done(self, agent_service: AgentService) -> None:
        async def _fake(t, _registry, _store, **_kwargs) -> AgentRun:
            return AgentRun(
                task_id=t.id,
                started_at="2026-01-01T00:00:00+00:00",
                status="ok",
                output={"argv": ["kbagent", "version"], "exit_code": 0},
            )

        events: list[dict] = []

        async def _drive() -> None:
            with patch("keboola_agent_cli.services.agent_service.run_task_once", side_effect=_fake):
                async for evt in agent_service.stream_test_action(
                    _cli_action("version"), name="test"
                ):
                    events.append(evt)

        asyncio.run(_drive())
        assert events[0]["event"] == "init"
        assert events[-1]["event"] == "done"
        assert events[-1]["data"]["status"] == "ok"


class TestRunHistoryReads:
    """list_runs / get_run / get_run_events surface the right NOT_FOUND errors."""

    def test_list_runs_for_unknown_task(self, agent_service: AgentService) -> None:
        with pytest.raises(ConfigError):
            agent_service.list_runs("unknown-id")

    def test_get_run_for_unknown_run(self, agent_service: AgentService) -> None:
        task = agent_service.create_task(name="A", action=_cli_action())
        with pytest.raises(ConfigError, match="Run 'bad-run-id' not found"):
            agent_service.get_run(task.id, "bad-run-id")

    def test_get_events_for_run_without_timeline(self, agent_service: AgentService) -> None:
        task = agent_service.create_task(name="A", action=_cli_action())
        run = AgentRun(task_id=task.id, started_at="2026-01-01T00:00:00+00:00", status="ok")
        # Append the run record but no events file -- mimics a pre-0.10 run.
        agent_service._store.append_run(run)
        with pytest.raises(ConfigError, match="No event timeline"):
            agent_service.get_run_events(task.id, run.run_id)
