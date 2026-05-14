"""Tests for the AI-driven prompt helper used by the React agent-task form.

Two surfaces under test:

- ``build_prompt_helper_meta_prompt`` / ``clean_prompt_helper_response`` --
  pure-text helpers in :mod:`keboola_agent_cli.server.agent_runner`. They are
  fast, deterministic, and worth pinning -- the OUTPUT CONTRACT in the
  meta-prompt and the preamble cleanup decide whether the React textarea
  receives a usable prompt body or markdown garbage.

- ``POST /agents/prompt/improve/stream`` -- the SSE endpoint that wires the
  helpers into the chosen AI CLI. We mock :func:`stream_ai_agent_events` so
  the test does NOT spawn a real ``claude`` / ``codex`` / ``gemini``
  subprocess and verify the endpoint forwards events and enriches the
  ``done`` payload with a cleaned ``prompt`` field.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

if importlib.util.find_spec("fastapi") is None:  # pragma: no cover
    pytest.skip(
        "FastAPI not installed; run `uv pip install -e '.[server]'`",
        allow_module_level=True,
    )

from fastapi.testclient import TestClient

from keboola_agent_cli.server import create_app
from keboola_agent_cli.server.agent_runner import (
    build_prompt_helper_meta_prompt,
    clean_prompt_helper_response,
)

# ---------------------------------------------------------------------
# build_prompt_helper_meta_prompt
# ---------------------------------------------------------------------


class TestBuildPromptHelperMetaPrompt:
    def test_includes_goal_text_verbatim(self) -> None:
        prompt = build_prompt_helper_meta_prompt(
            goal="Each morning summarize errored jobs in padak",
        )
        assert "Each morning summarize errored jobs in padak" in prompt

    def test_includes_draft_when_provided(self) -> None:
        prompt = build_prompt_helper_meta_prompt(
            goal="goal x",
            draft="Use kbagent to list jobs",
        )
        assert "Use kbagent to list jobs" in prompt
        assert "preserve any concrete details" in prompt

    def test_marks_draft_as_empty_when_blank(self) -> None:
        prompt = build_prompt_helper_meta_prompt(goal="goal x", draft="   ")
        assert "(empty -- write the prompt from scratch.)" in prompt

    def test_project_pinned_message(self) -> None:
        prompt = build_prompt_helper_meta_prompt(goal="g", project="padak")
        assert "pinned project 'padak'" in prompt

    def test_no_project_falls_back_to_asking(self) -> None:
        prompt = build_prompt_helper_meta_prompt(goal="g", project=None)
        assert "No project is pinned" in prompt

    def test_output_contract_is_present(self) -> None:
        """The whole point of the meta-prompt is the OUTPUT CONTRACT.

        Without these instructions, the AI emits "Here is the prompt:\\n\\n..."
        and the React textarea gets a preamble. The OUTPUT CONTRACT block
        forbids it -- if it's accidentally trimmed, this test catches the
        regression before users see it.
        """
        prompt = build_prompt_helper_meta_prompt(goal="g")
        assert "OUTPUT CONTRACT" in prompt
        assert "Output ONLY the rewritten prompt body" in prompt
        assert "Do not say" in prompt and "Here is the prompt" in prompt

    def test_kbagent_commands_in_examples(self) -> None:
        """The meta-prompt anchors the AI in REAL kbagent commands.

        Without concrete examples, models invent commands like
        ``kbagent jobs --filter=error`` (no such flag). Pinning a few
        canonical examples in the meta-prompt keeps suggestions grounded.
        """
        prompt = build_prompt_helper_meta_prompt(goal="g")
        assert "kbagent job list" in prompt
        assert "kbagent http get" in prompt


# ---------------------------------------------------------------------
# clean_prompt_helper_response
# ---------------------------------------------------------------------


class TestCleanPromptHelperResponse:
    def test_passthrough_clean_body(self) -> None:
        body = "Use kbagent to summarize jobs.\nThen output a markdown table."
        assert clean_prompt_helper_response(body) == body

    def test_strips_leading_and_trailing_whitespace(self) -> None:
        assert clean_prompt_helper_response("\n\n  body  \n\n") == "body"

    def test_strips_plain_code_fence(self) -> None:
        raw = "```\nUse kbagent to list jobs\n```"
        assert clean_prompt_helper_response(raw) == "Use kbagent to list jobs"

    def test_strips_typed_code_fence(self) -> None:
        raw = "```text\nUse kbagent to list jobs\n```"
        assert clean_prompt_helper_response(raw) == "Use kbagent to list jobs"

    def test_strips_here_is_the_prompt_preamble(self) -> None:
        raw = "Here is the prompt:\n\nUse kbagent to list jobs"
        assert clean_prompt_helper_response(raw) == "Use kbagent to list jobs"

    def test_strips_rewritten_prompt_preamble(self) -> None:
        raw = "Rewritten prompt:\nUse kbagent to list jobs"
        assert clean_prompt_helper_response(raw) == "Use kbagent to list jobs"

    def test_empty_input_yields_empty(self) -> None:
        assert clean_prompt_helper_response("") == ""

    def test_only_preamble_yields_empty(self) -> None:
        assert clean_prompt_helper_response("Here is the prompt:") == ""

    def test_dedups_exact_double_body(self) -> None:
        """Claude stream-json emits the same body twice (assistant turn +
        final result event), and ``stream_ai_agent_events`` concatenates
        both into ``response``. For a single non-tool turn the result is
        an exactly-doubled string. Collapsing it here keeps the textarea
        clean for the user without poking at the shared streaming helper.
        """
        body = "Use kbagent to list jobs.\nReport top 3 root causes."
        raw = body + body
        assert clean_prompt_helper_response(raw) == body

    def test_does_not_dedup_unrelated_halves(self) -> None:
        raw = "first half\nsecond half"
        # The two halves aren't equal -- the function must leave it alone.
        assert clean_prompt_helper_response(raw) == raw


# ---------------------------------------------------------------------
# POST /agents/prompt/improve/stream
# ---------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(config_dir=str(tmp_path), auth_token="test-token")
    return TestClient(app)


def _parse_sse_events(text: str) -> list[tuple[str, str]]:
    """Parse the SSE wire-format body into [(event, data_json_string)].

    SSE messages are delimited by blank lines; within a message we collect
    `event:` and `data:` lines. Comments (starting with `:`) are skipped.
    The endpoint we test emits one `data:` line per event, so we don't need
    to handle multi-line data concatenation here.
    """
    out: list[tuple[str, str]] = []
    event = "message"
    data = ""
    for line in text.splitlines():
        if line == "":
            if data:
                out.append((event, data))
                event = "message"
                data = ""
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data = line[5:].lstrip()
    if data:
        out.append((event, data))
    return out


class TestImprovePromptStreamEndpoint:
    def test_empty_goal_rejected_400(self, client: TestClient) -> None:
        res = client.post(
            "/agents/prompt/improve/stream",
            json={"cli": "claude", "goal": "   ", "draft": ""},
            headers={"Authorization": "Bearer test-token"},
        )
        assert res.status_code == 400, res.text

    def test_streams_events_and_enriches_done(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Happy path: the endpoint forwards mocked events and the final
        ``done`` payload carries a cleaned ``prompt`` field plus the raw
        response, both surfaced to the frontend via SSE.

        We patch ``stream_ai_agent_events`` at the import site used by the
        router (the local-import inside ``improve_prompt_stream`` resolves
        to ``keboola_agent_cli.server.routers.agents`` even though it does
        ``from ..agent_runner import ...``). Patching the module-level name
        in :mod:`agent_runner` works because :func:`improve_prompt_stream`
        imports those helpers locally on each call.
        """

        async def fake_stream(
            registry: object,
            params: dict[str, object],
        ):
            yield {"event": "stdout", "data": {"raw": "thinking..."}}
            yield {
                "event": "done",
                "data": {
                    "cli": "claude",
                    "status": "ok",
                    "exit_code": 0,
                    "elapsed_seconds": 1.23,
                    "response": "Here is the prompt:\n\nUse kbagent to summarize jobs.",
                    "stderr": "",
                },
            }

        monkeypatch.setattr(
            "keboola_agent_cli.server.routers.agents.stream_ai_agent_events",
            fake_stream,
        )

        with client.stream(
            "POST",
            "/agents/prompt/improve/stream",
            json={
                "cli": "claude",
                "goal": "Summarize errored jobs each morning",
                "draft": "",
                "project": "padak",
            },
            headers={"Authorization": "Bearer test-token"},
        ) as res:
            assert res.status_code == 200
            body = res.read().decode("utf-8")

        events = _parse_sse_events(body)
        names = [e for e, _ in events]
        assert "init" in names
        assert "stdout" in names
        assert "done" in names

        import json as _json

        done_data = next(_json.loads(payload) for evt, payload in events if evt == "done")
        # The "Here is the prompt:" preamble must be stripped before reaching the UI.
        assert done_data["prompt"] == "Use kbagent to summarize jobs."
        # raw_response preserved for debugging.
        assert "Here is the prompt:" in done_data["raw_response"]
        assert done_data["status"] == "ok"

    def test_stream_error_surfaces_as_done_error(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ValueError from ``stream_ai_agent_events`` (bad CLI, empty
        prompt, ...) must surface as a final ``done`` event with status
        ``error`` so the React side can render it without hanging on
        an unterminated stream.
        """

        async def fake_stream(
            registry: object,
            params: dict[str, object],
        ):
            raise ValueError("ai_agent.cli must be one of ['claude', 'codex', 'gemini']")
            yield  # pragma: no cover -- unreachable, kept so this is an async generator

        monkeypatch.setattr(
            "keboola_agent_cli.server.routers.agents.stream_ai_agent_events",
            fake_stream,
        )

        with client.stream(
            "POST",
            "/agents/prompt/improve/stream",
            json={
                "cli": "bogus",
                "goal": "anything",
                "draft": "",
            },
            headers={"Authorization": "Bearer test-token"},
        ) as res:
            assert res.status_code == 200
            body = res.read().decode("utf-8")

        events = _parse_sse_events(body)
        import json as _json

        done = next(_json.loads(payload) for evt, payload in events if evt == "done")
        assert done["status"] == "error"
        assert "claude" in done["error"]
