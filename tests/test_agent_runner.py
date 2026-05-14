"""Tests for agent_runner subprocess context: env injection + prompt wrapping.

The two surfaces under test:

- ``_build_subprocess_env`` -- composes the env dict for AI / CLI subprocesses
  spawned by the scheduler. Must overlay KBAGENT_CONFIG_DIR /
  KBAGENT_SERVE_URL / KBAGENT_SERVE_TOKEN onto a copy of ``os.environ``,
  never mutate the parent's env.
- ``_run_cli`` / ``_run_ai_agent`` -- both must pass the composed env to
  ``asyncio.create_subprocess_exec`` and ``_run_ai_agent`` must prepend the
  runtime-context preamble to the user's prompt.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from keboola_agent_cli.constants import (
    ENV_CONFIG_DIR,
    ENV_KBAGENT_SERVE_TOKEN,
    ENV_KBAGENT_SERVE_URL,
    ENV_KBAGENT_UPSTREAM_RUN_ID,
    ENV_KBAGENT_UPSTREAM_STATUS,
    ENV_KBAGENT_UPSTREAM_TASK_ID,
)
from keboola_agent_cli.server.agent_runner import (
    _AI_AGENT_PROMPT_PREFIX,
    _build_subprocess_env,
    _run_ai_agent,
    _run_cli,
    _trigger_should_fire,
    _upstream_prompt_prefix,
)
from keboola_agent_cli.server.agents_store import AgentAction, AgentRun, AgentTask


def _make_registry(
    config_dir: Path,
    *,
    serve_url: str | None = "http://127.0.0.1:8001",
    serve_token: str | None = "test-bearer-token",
) -> Any:
    """Synth a minimal registry shaped like ``ServiceRegistry``."""
    config_store = MagicMock()
    config_store.config_dir = config_dir
    registry = MagicMock()
    registry.config_store = config_store
    registry.serve_url = serve_url
    registry.serve_token = serve_token
    return registry


class TestBuildSubprocessEnv:
    def test_overlays_three_keys_on_os_environ(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path / "kbagent-config")
        env = _build_subprocess_env(registry)
        assert env[ENV_CONFIG_DIR] == str(tmp_path / "kbagent-config")
        assert env[ENV_KBAGENT_SERVE_URL] == "http://127.0.0.1:8001"
        assert env[ENV_KBAGENT_SERVE_TOKEN] == "test-bearer-token"
        # Sanity: PATH from os.environ is preserved (subprocess needs it
        # to find `kbagent` / `claude` on the system).
        if "PATH" in os.environ:
            assert env["PATH"] == os.environ["PATH"]

    def test_returns_fresh_dict_parent_env_untouched(self, tmp_path: Path) -> None:
        sentinel = "KBAGENT_TEST_SENTINEL_DO_NOT_CLOBBER"
        os.environ.pop(sentinel, None)
        registry = _make_registry(tmp_path)
        env = _build_subprocess_env(registry)
        env[sentinel] = "1"
        assert sentinel not in os.environ

    def test_skips_url_and_token_when_registry_lacks_them(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path, serve_url=None, serve_token=None)
        env = _build_subprocess_env(registry)
        assert env[ENV_CONFIG_DIR] == str(tmp_path)
        # No URL/token => keys must be ABSENT (not empty-string), so any
        # consumer can distinguish "running standalone" from "running under serve".
        assert ENV_KBAGENT_SERVE_URL not in env or env[ENV_KBAGENT_SERVE_URL] == os.environ.get(
            ENV_KBAGENT_SERVE_URL, ""
        )

    def test_handles_missing_config_store(self) -> None:
        # Registry that crashes on .config_store should still produce a usable env
        # (graceful degrade -- subprocess just won't get config_dir override).
        registry = MagicMock(spec=[])  # no attrs
        env = _build_subprocess_env(registry)
        assert ENV_CONFIG_DIR not in env or env[ENV_CONFIG_DIR] == os.environ.get(
            ENV_CONFIG_DIR, ""
        )


class TestRunCliEnvPropagation:
    @pytest.mark.asyncio
    async def test_passes_composed_env_to_subprocess(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"out", b""))
        mock_proc.returncode = 0
        with patch(
            "keboola_agent_cli.server.agent_runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=mock_proc),
        ) as mock_spawn:
            await _run_cli(registry, {"argv": ["doctor"]})
        kwargs = mock_spawn.call_args.kwargs
        # The cardinal bug-fix: env MUST NOT be None, and MUST contain
        # KBAGENT_CONFIG_DIR aligned with the serve.
        assert kwargs["env"] is not None
        assert kwargs["env"][ENV_CONFIG_DIR] == str(tmp_path)


class TestStreamAiAgentEvents:
    """Coverage for the JSONL streaming generator that drives /agents/test/stream.

    Uses an inline shell-style stand-in: we monkey-patch
    ``asyncio.create_subprocess_exec`` to return a `_FakeProc` with
    pre-baked stdout / stderr StreamReaders. Avoids spawning the real
    claude CLI in CI (would need an API key and is non-deterministic).
    """

    @pytest.mark.asyncio
    async def test_parses_jsonl_lines_into_stdout_events(self, tmp_path: Path) -> None:
        from keboola_agent_cli.server.agent_runner import stream_ai_agent_events

        scripted_stdout = (
            b'{"type":"system","subtype":"init","model":"claude-opus"}\n'
            b'{"type":"assistant","message":{"content":[{"type":"text","text":"Hi"}]}}\n'
            b'{"type":"result","subtype":"success","result":"All done"}\n'
        )
        scripted_stderr = b"some warning\n"

        registry = _make_registry(tmp_path)
        with patch(
            "keboola_agent_cli.server.agent_runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_FakeProc(scripted_stdout, scripted_stderr)),
        ):
            events = []
            async for evt in stream_ai_agent_events(
                registry,
                {"cli": "claude", "prompt": "test"},
            ):
                events.append(evt)

        # Expected event sequence:
        # 1. init (one-shot)
        # 2. 3x stdout (parsed JSONL)
        # 3. 1x stderr
        # 4. done (one-shot)
        kinds = [e["event"] for e in events]
        assert kinds[0] == "init"
        assert kinds.count("stdout") == 3
        assert kinds.count("stderr") == 1
        assert kinds[-1] == "done"

        # The 3 stdout events should have parsed JSON payloads, not raw lines.
        stdout_evts = [e for e in events if e["event"] == "stdout"]
        assert stdout_evts[0]["data"]["type"] == "system"
        assert stdout_evts[1]["data"]["type"] == "assistant"
        assert stdout_evts[2]["data"]["type"] == "result"

        # Done event should accumulate assistant text + result text into
        # response_text so the UI can show it without re-walking the stream.
        done = events[-1]["data"]
        assert "Hi" in done["response"]
        assert "All done" in done["response"]
        assert done["status"] == "ok"
        # exit_code from _FakeProc.returncode = 0
        assert done["exit_code"] == 0
        # stderr captured intact
        assert "some warning" in done["stderr"]

    @pytest.mark.asyncio
    async def test_malformed_jsonl_line_falls_back_to_raw(self, tmp_path: Path) -> None:
        from keboola_agent_cli.server.agent_runner import stream_ai_agent_events

        # First line: valid JSON. Second line: garbage. Third line: valid.
        scripted_stdout = (
            b'{"type":"system","subtype":"init"}\n'
            b"this is not json\n"
            b'{"type":"result","result":"ok"}\n'
        )
        registry = _make_registry(tmp_path)
        with patch(
            "keboola_agent_cli.server.agent_runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_FakeProc(scripted_stdout, b"")),
        ):
            events = []
            async for evt in stream_ai_agent_events(
                registry,
                {"cli": "claude", "prompt": "test"},
            ):
                events.append(evt)

        stdout_evts = [e for e in events if e["event"] == "stdout"]
        assert stdout_evts[0]["data"]["type"] == "system"
        # Malformed line carries `raw` instead of typed shape -- the UI
        # uses presence of `type` to switch on rendering, so this routes
        # to the fallback raw-JSON row.
        assert "raw" in stdout_evts[1]["data"]
        assert stdout_evts[2]["data"]["type"] == "result"


class _FakeProc:
    """Stand-in for ``asyncio.subprocess.Process`` used by tests.

    Only the three pieces the generator touches are mocked:
    - ``stdout`` / ``stderr`` as ``StreamReader``s pre-fed with scripted bytes.
    - ``wait()`` returns 0 (success).
    - ``returncode`` is None until ``wait()`` is called, then 0.
    - ``kill()`` is a no-op (we don't test the timeout branch here).
    """

    def __init__(self, stdout_bytes: bytes, stderr_bytes: bytes) -> None:
        loop = asyncio.get_event_loop()
        self.stdout = asyncio.StreamReader(loop=loop)
        self.stderr = asyncio.StreamReader(loop=loop)
        self.stdout.feed_data(stdout_bytes)
        self.stdout.feed_eof()
        self.stderr.feed_data(stderr_bytes)
        self.stderr.feed_eof()
        self.returncode: int | None = None

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def kill(self) -> None:
        pass


class TestRunAiAgentPromptWrapping:
    @pytest.mark.asyncio
    async def test_prepends_runtime_context_to_prompt(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"ai-response", b""))
        mock_proc.returncode = 0
        with patch(
            "keboola_agent_cli.server.agent_runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=mock_proc),
        ) as mock_spawn:
            await _run_ai_agent(
                registry,
                {"cli": "claude", "prompt": "List jobs.", "extra_args": []},
            )
        argv_passed = mock_spawn.call_args.args
        # claude recipe is: ["claude", "-p", prompt, ...extra]
        assert argv_passed[0] == "claude"
        assert argv_passed[1] == "-p"
        wrapped = argv_passed[2]
        assert wrapped.startswith(_AI_AGENT_PROMPT_PREFIX[:40])
        assert wrapped.endswith("List jobs.")
        # Env propagation check (same as _run_cli).
        kwargs = mock_spawn.call_args.kwargs
        assert kwargs["env"][ENV_KBAGENT_SERVE_URL] == "http://127.0.0.1:8001"
        assert kwargs["env"][ENV_KBAGENT_SERVE_TOKEN] == "test-bearer-token"


# ---------------------------------------------------------------------------
# Manual + trigger-chain tests
# ---------------------------------------------------------------------------


class TestTriggerPolicy:
    """``_trigger_should_fire`` matches the three on-filter modes correctly."""

    @pytest.mark.parametrize(
        "trigger_on,status,expected",
        [
            ("success", "ok", True),
            ("success", "error", False),
            ("error", "error", True),
            ("error", "ok", False),
            ("always", "ok", True),
            ("always", "error", True),
            # Unknown values fail closed — we don't blindly fire.
            ("nonsense", "ok", False),
        ],
    )
    def test_should_fire(self, trigger_on: str, status: str, expected: bool) -> None:
        assert _trigger_should_fire(trigger_on, status) is expected


class TestUpstreamEnvAndPrompt:
    """Upstream chain context propagates to subprocess env + AI prompt."""

    def test_env_includes_upstream_keys_when_chained(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        upstream_task = AgentTask(
            id="UPSTREAM-ID",
            name="upstream",
            action=AgentAction(type="ai_agent", params={"cli": "claude", "prompt": "x"}),
        )
        upstream_run = AgentRun(
            task_id=upstream_task.id, started_at="2026-01-01T00:00:00+00:00", status="ok"
        )
        env = _build_subprocess_env(
            registry, upstream_run=upstream_run, upstream_task=upstream_task
        )
        assert env[ENV_KBAGENT_UPSTREAM_TASK_ID] == "UPSTREAM-ID"
        assert env[ENV_KBAGENT_UPSTREAM_RUN_ID] == upstream_run.run_id
        assert env[ENV_KBAGENT_UPSTREAM_STATUS] == "ok"

    def test_env_omits_upstream_keys_when_not_chained(self, tmp_path: Path) -> None:
        env = _build_subprocess_env(_make_registry(tmp_path))
        assert ENV_KBAGENT_UPSTREAM_TASK_ID not in env
        assert ENV_KBAGENT_UPSTREAM_RUN_ID not in env
        assert ENV_KBAGENT_UPSTREAM_STATUS not in env

    def test_prompt_prefix_empty_without_upstream(self) -> None:
        assert _upstream_prompt_prefix(None, None) == ""

    def test_prompt_prefix_references_upstream_run(self) -> None:
        task = AgentTask(
            id="A",
            name="upstream task",
            action=AgentAction(type="ai_agent", params={"cli": "claude", "prompt": "x"}),
        )
        run = AgentRun(task_id="A", started_at="2026-01-01T00:00:00+00:00", status="ok")
        prefix = _upstream_prompt_prefix(run, task)
        assert "upstream task" in prefix
        # AI must know how to fetch the upstream output via HTTP.
        assert f"/agents/A/runs/{run.run_id}" in prefix
