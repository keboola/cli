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
    _resolve_ai_extra_args,
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

    def test_strip_admin_tokens_removes_manage_and_master(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # GHSA-wm54-r2hh-cxm9: an AI-agent child must not inherit the super-admin
        # (manage) or master tokens. Per-alias master tokens are stripped by prefix.
        monkeypatch.setenv("KBC_MANAGE_API_TOKEN", "manage-secret")
        monkeypatch.setenv("KBC_MASTER_TOKEN", "master-secret")
        monkeypatch.setenv("KBC_MASTER_TOKEN_PROD", "master-prod-secret")
        monkeypatch.setenv("KBC_TOKEN", "901-55555-storage")
        registry = _make_registry(tmp_path)

        env = _build_subprocess_env(registry, strip_admin_tokens=True)

        assert "KBC_MANAGE_API_TOKEN" not in env
        assert "KBC_MASTER_TOKEN" not in env
        assert "KBC_MASTER_TOKEN_PROD" not in env
        # The per-project storage token is intentionally retained so the child
        # can still run headless `--project __env__` reads.
        assert env["KBC_TOKEN"] == "901-55555-storage"

    def test_default_keeps_all_tokens_for_cli_command(self, tmp_path: Path, monkeypatch) -> None:
        # cli_command IS kbagent and legitimately needs these (e.g. a scheduled
        # `project refresh` / `sharing` task), so the default must NOT strip.
        monkeypatch.setenv("KBC_MANAGE_API_TOKEN", "manage-secret")
        monkeypatch.setenv("KBC_MASTER_TOKEN", "master-secret")
        registry = _make_registry(tmp_path)

        env = _build_subprocess_env(registry)

        assert env["KBC_MANAGE_API_TOKEN"] == "manage-secret"
        assert env["KBC_MASTER_TOKEN"] == "master-secret"


class TestAiAgentTokenIsolation:
    """GHSA-wm54-r2hh-cxm9: the spawned ai_agent child is isolated from the
    manage/master tokens; the cli_command child keeps them."""

    @pytest.mark.asyncio
    async def test_ai_agent_subprocess_strips_admin_tokens(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("KBC_MANAGE_API_TOKEN", "manage-secret")
        monkeypatch.setenv("KBC_MASTER_TOKEN", "master-secret")
        monkeypatch.setenv("KBC_TOKEN", "901-55555-storage")
        registry = _make_registry(tmp_path)
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"out", b""))
        mock_proc.returncode = 0
        with patch(
            "keboola_agent_cli.server.agent_runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=mock_proc),
        ) as mock_spawn:
            await _run_ai_agent(registry, {"cli": "claude", "prompt": "summarize my jobs"})
        env = mock_spawn.call_args.kwargs["env"]
        assert "KBC_MANAGE_API_TOKEN" not in env
        assert "KBC_MASTER_TOKEN" not in env
        # Storage token + serve callback are retained so the agent can still work.
        assert env["KBC_TOKEN"] == "901-55555-storage"
        assert env[ENV_KBAGENT_SERVE_TOKEN] == "test-bearer-token"

    @pytest.mark.asyncio
    async def test_cli_command_subprocess_keeps_admin_tokens(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("KBC_MANAGE_API_TOKEN", "manage-secret")
        registry = _make_registry(tmp_path)
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"out", b""))
        mock_proc.returncode = 0
        with patch(
            "keboola_agent_cli.server.agent_runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=mock_proc),
        ) as mock_spawn:
            await _run_cli(registry, {"argv": ["project", "refresh", "--all"]})
        env = mock_spawn.call_args.kwargs["env"]
        # cli_command keeps the manage token so scheduled admin tasks still work.
        assert env["KBC_MANAGE_API_TOKEN"] == "manage-secret"


class TestAiExtraArgsGate:
    """GHSA-777j-6p95-qv3m: ai_agent extra_args reach the AI CLI only when the
    serve operator opts in via KBAGENT_ALLOW_AI_EXTRA_ARGS."""

    def test_extra_args_dropped_without_optin(self, monkeypatch) -> None:
        monkeypatch.delenv("KBAGENT_ALLOW_AI_EXTRA_ARGS", raising=False)
        out = _resolve_ai_extra_args({"extra_args": ["--dangerously-skip-permissions"]})
        assert out == []

    def test_extra_args_honored_with_optin(self, monkeypatch) -> None:
        monkeypatch.setenv("KBAGENT_ALLOW_AI_EXTRA_ARGS", "1")
        out = _resolve_ai_extra_args({"extra_args": ["--model", "opus"]})
        assert out == ["--model", "opus"]

    def test_empty_extra_args_returns_empty(self, monkeypatch) -> None:
        monkeypatch.setenv("KBAGENT_ALLOW_AI_EXTRA_ARGS", "1")
        assert _resolve_ai_extra_args({}) == []

    def test_non_list_extra_args_raises(self) -> None:
        with pytest.raises(ValueError, match="must be a list"):
            _resolve_ai_extra_args({"extra_args": "--oops"})

    @pytest.mark.asyncio
    async def test_run_ai_agent_drops_extra_args_without_optin(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("KBAGENT_ALLOW_AI_EXTRA_ARGS", raising=False)
        registry = _make_registry(tmp_path)
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"out", b""))
        mock_proc.returncode = 0
        with patch(
            "keboola_agent_cli.server.agent_runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=mock_proc),
        ) as mock_spawn:
            await _run_ai_agent(
                registry,
                {
                    "cli": "claude",
                    "prompt": "go",
                    "extra_args": ["--dangerously-skip-permissions"],
                },
            )
        argv = list(mock_spawn.call_args.args)
        assert "--dangerously-skip-permissions" not in argv

    @pytest.mark.asyncio
    async def test_run_ai_agent_honors_extra_args_with_optin(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("KBAGENT_ALLOW_AI_EXTRA_ARGS", "1")
        registry = _make_registry(tmp_path)
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"out", b""))
        mock_proc.returncode = 0
        with patch(
            "keboola_agent_cli.server.agent_runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=mock_proc),
        ) as mock_spawn:
            await _run_ai_agent(
                registry,
                {"cli": "claude", "prompt": "go", "extra_args": ["--model", "opus"]},
            )
        argv = list(mock_spawn.call_args.args)
        assert "--model" in argv and "opus" in argv


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

    @pytest.mark.asyncio
    async def test_streaming_strips_admin_tokens_and_gates_extra_args(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # NB-3: the streaming path (production-dominant: serve REST, run
        # broadcaster, Web UI, local `agent test`) must enforce BOTH fixes,
        # not just the one-shot `_run_ai_agent` path.
        from keboola_agent_cli.server.agent_runner import stream_ai_agent_events

        monkeypatch.setenv("KBC_MANAGE_API_TOKEN", "manage-secret")
        monkeypatch.setenv("KBC_MASTER_TOKEN", "master-secret")
        monkeypatch.setenv("KBC_TOKEN", "901-55555-storage")
        monkeypatch.delenv("KBAGENT_ALLOW_AI_EXTRA_ARGS", raising=False)
        registry = _make_registry(tmp_path)
        spawn = AsyncMock(return_value=_FakeProc(b'{"type":"result","result":"ok"}\n', b""))
        with patch(
            "keboola_agent_cli.server.agent_runner.asyncio.create_subprocess_exec",
            new=spawn,
        ):
            async for _ in stream_ai_agent_events(
                registry,
                {
                    "cli": "claude",
                    "prompt": "test",
                    "extra_args": ["--dangerously-skip-permissions"],
                },
            ):
                pass
        env = spawn.call_args.kwargs["env"]
        argv = list(spawn.call_args.args)
        # M1: admin tokens stripped from the streaming child too; storage kept.
        assert "KBC_MANAGE_API_TOKEN" not in env
        assert "KBC_MASTER_TOKEN" not in env
        assert env["KBC_TOKEN"] == "901-55555-storage"
        # M3: the rail-disabling flag is dropped without the opt-in env.
        assert "--dangerously-skip-permissions" not in argv


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


# ---------------------------------------------------------------------------
# Codex headless invocation guard + non-timeout error surfacing
# ---------------------------------------------------------------------------


class TestCodexHeadlessRecipe:
    """Codex CLI 0.131+ refuses to run in any directory the user has not
    interactively "trusted". A subprocess never sees that dialog, so the
    `--skip-git-repo-check` flag is the only way to keep `codex exec`
    usable from `kbagent serve` / scheduled agent runs. Without it codex
    exits 1 with "Not inside a trusted directory" before reading the
    prompt — and the failure surfaces as a generic "AI chat failed" in
    the dashboard. Pin the flag in BOTH recipe tables so a future refactor
    cannot silently drop it.
    """

    def test_one_shot_recipe_includes_skip_git_repo_check(self) -> None:
        from keboola_agent_cli.server.agent_runner import _AI_CLI_RECIPES

        argv = _AI_CLI_RECIPES["codex"]("hi", [])
        assert argv[0] == "codex"
        assert argv[1] == "exec"
        assert "--skip-git-repo-check" in argv
        # The prompt must remain the LAST positional argument, otherwise
        # codex would interpret following tokens as part of the prompt.
        assert argv[-1] == "hi"

    def test_stream_recipe_includes_skip_git_repo_check(self) -> None:
        from keboola_agent_cli.server.agent_runner import _AI_CLI_STREAM_RECIPES

        argv, jsonl = _AI_CLI_STREAM_RECIPES["codex"]("hi", [])
        assert argv[0] == "codex"
        assert argv[1] == "exec"
        assert "--skip-git-repo-check" in argv
        assert argv[-1] == "hi"
        # codex does not support a structured JSONL stream today.
        assert jsonl is False

    def test_codex_extra_args_land_before_prompt(self) -> None:
        """User-supplied extra_args must not push the prompt out of its
        terminal position (otherwise codex would treat the prompt as a
        subcommand or eat extra_args as part of it).
        """
        from keboola_agent_cli.server.agent_runner import (
            _AI_CLI_RECIPES,
            _AI_CLI_STREAM_RECIPES,
        )

        argv = _AI_CLI_RECIPES["codex"]("THE PROMPT", ["--sandbox", "read-only"])
        assert argv[-3:] == ["--sandbox", "read-only", "THE PROMPT"]

        argv2, _ = _AI_CLI_STREAM_RECIPES["codex"]("THE PROMPT", ["--sandbox", "read-only"])
        assert argv2[-3:] == ["--sandbox", "read-only", "THE PROMPT"]


class _FailingProc:
    """Stand-in for asyncio.subprocess.Process that exits non-zero.

    Mirrors _FakeProc above but lets the test choose ``returncode`` so we
    can exercise the non-timeout error path of ``stream_ai_agent_events``.
    """

    def __init__(self, stdout_bytes: bytes, stderr_bytes: bytes, exit_code: int) -> None:
        loop = asyncio.get_event_loop()
        self.stdout = asyncio.StreamReader(loop=loop)
        self.stderr = asyncio.StreamReader(loop=loop)
        self.stdout.feed_data(stdout_bytes)
        self.stdout.feed_eof()
        self.stderr.feed_data(stderr_bytes)
        self.stderr.feed_eof()
        self._exit_code = exit_code
        self.returncode: int | None = None

    async def wait(self) -> int:
        self.returncode = self._exit_code
        return self._exit_code

    def kill(self) -> None:
        pass


class TestStreamErrorSurfacing:
    """When a CLI exits with non-zero rc (not a timeout), the done event
    must carry an ``error`` field so the UI does not fall back to a
    generic "AI chat failed" placeholder. The stderr tail is the most
    useful diagnostic (codex trust check, claude auth, network blip).
    """

    @pytest.mark.asyncio
    async def test_non_timeout_failure_populates_error_with_stderr_tail(
        self, tmp_path: Path
    ) -> None:
        from keboola_agent_cli.server.agent_runner import stream_ai_agent_events

        registry = _make_registry(tmp_path)
        scripted_stderr = (
            b"Reading additional input from stdin...\n"
            b"Not inside a trusted directory and "
            b"--skip-git-repo-check was not specified.\n"
        )
        proc = _FailingProc(b"", scripted_stderr, exit_code=1)
        with patch(
            "keboola_agent_cli.server.agent_runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            events = []
            async for evt in stream_ai_agent_events(registry, {"cli": "codex", "prompt": "test"}):
                events.append(evt)

        done = events[-1]
        assert done["event"] == "done"
        data = done["data"]
        assert data["status"] == "error"
        assert data["exit_code"] == 1
        # The error field is what the React side reads; it MUST be present
        # so the placeholder fallback ("AI chat failed") never wins.
        assert "error" in data
        assert "exited with code 1" in data["error"]
        # Stderr tail surfaces the root cause verbatim.
        assert "trusted directory" in data["error"]

    @pytest.mark.asyncio
    async def test_successful_run_does_not_set_error_field(self, tmp_path: Path) -> None:
        from keboola_agent_cli.server.agent_runner import stream_ai_agent_events

        registry = _make_registry(tmp_path)
        # Reuse the existing _FakeProc (exit 0) — but feed claude-shaped
        # JSONL so the generator's stream_ai_agent_events accumulator
        # produces a sensible response.
        scripted = b'{"type":"result","result":"ok"}\n'
        proc = _FakeProc(scripted, b"")
        with patch(
            "keboola_agent_cli.server.agent_runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            events = []
            async for evt in stream_ai_agent_events(registry, {"cli": "claude", "prompt": "ok"}):
                events.append(evt)

        done = events[-1]["data"]
        assert done["status"] == "ok"
        # Success path leaves the error field absent — UI checks for status
        # before reading error, but absence keeps the wire shape clean.
        assert "error" not in done
