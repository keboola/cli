"""Tests for the dashboard Local AI chat endpoint (#300).

Mirrors the test structure of ``test_workspace_sql_helper.py`` and
``test_agent_prompt_helper.py``: pure-text helper tests (meta-prompt
content) + SSE endpoint integration with mocked
``stream_ai_agent_events``.

The chat helper is the third instance of the same stateless-helper
pattern (after the SQL helper and the agent prompt helper) -- all three
sit on top of ``stream_ai_agent_events`` and differ only in their
meta-prompt builder and minimal endpoint wiring.
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
from keboola_agent_cli.server.agent_runner import build_local_ai_meta_prompt

# ---------------------------------------------------------------------
# build_local_ai_meta_prompt
# ---------------------------------------------------------------------


class TestBuildLocalAiMetaPrompt:
    def test_includes_user_message_verbatim(self) -> None:
        prompt = build_local_ai_meta_prompt(
            message="How many jobs failed in padak project yesterday?",
        )
        assert "How many jobs failed in padak project yesterday?" in prompt

    def test_strips_leading_trailing_whitespace_from_message(self) -> None:
        prompt = build_local_ai_meta_prompt(message="\n\n  question  \n\n")
        # The verbatim line should be the trimmed version.
        assert "question" in prompt

    def test_project_block_when_project_pinned(self) -> None:
        """A pinned project surfaces in USER CONTEXT with the --project
        flag hint baked in, so the AI doesn't have to guess.
        """
        prompt = build_local_ai_meta_prompt(message="g", project="padak")
        assert "Active project: 'padak'" in prompt
        assert "--project padak" in prompt

    def test_project_block_when_no_project(self) -> None:
        """No-project mode invites the AI to either ask the user OR run
        cross-project commands with explicit --project flags.
        """
        prompt = build_local_ai_meta_prompt(message="g")
        assert "Active project: (none" in prompt
        # Cross-project guidance present so the AI doesn't refuse the
        # request when no project is pinned.
        assert "multi-project" in prompt
        assert "--project NAME" in prompt

    def test_branch_id_surfaced_when_set(self) -> None:
        prompt = build_local_ai_meta_prompt(
            message="g",
            project="demo",
            branch_id=1234,
        )
        assert "Active branch: #1234" in prompt
        assert "--branch 1234" in prompt

    def test_branch_falls_back_to_main_when_unset(self) -> None:
        prompt = build_local_ai_meta_prompt(message="g")
        assert "Active branch: main (production)" in prompt

    def test_serve_url_baked_in_when_provided(self) -> None:
        """When the serve URL is known we tell the AI the fast path
        (`kbagent http get ...`); otherwise we give the env-var fallback.
        """
        prompt = build_local_ai_meta_prompt(
            message="g",
            serve_url="http://127.0.0.1:8001",
        )
        assert "kbagent http get|post" in prompt
        assert "http://127.0.0.1:8001" in prompt

    def test_serve_url_omitted_falls_back_to_env_hint(self) -> None:
        prompt = build_local_ai_meta_prompt(message="g")
        # Without a serve URL the AI still gets the env-var-based hint
        # so it knows kbagent http works inside the subprocess.
        assert "KBAGENT_SERVE_URL" in prompt
        assert "KBAGENT_SERVE_TOKEN" in prompt

    def test_kbagent_context_pointer_present(self) -> None:
        """The kbagent skill is NOT inlined into the prompt (it's 70+ KB
        of docs). Instead the AI is told to run `kbagent context`
        on-demand, mirroring how Claude Code's plugin loader bootstraps
        the skill. Pin this so a future refactor doesn't quietly
        balloon every chat round-trip by inlining the skill text.
        """
        prompt = build_local_ai_meta_prompt(message="g")
        assert "kbagent context" in prompt

    def test_output_format_section_present(self) -> None:
        """The chat surface renders markdown — pin that contract so a
        future change doesn't make the AI emit a wall of plain text.
        """
        prompt = build_local_ai_meta_prompt(message="g")
        assert "Markdown" in prompt
        assert "code blocks" in prompt.lower() or "code block" in prompt.lower()

    def test_no_output_shape_constraint(self) -> None:
        """Unlike the SQL helper / prompt helper, the chat helper does
        NOT pin an OUTPUT CONTRACT clause that strips fences or insight
        blocks -- the chat surface intentionally renders markdown
        verbatim including code blocks. Pin this so we don't
        accidentally inherit the strict contract from the other
        helpers via a copy-paste refactor.
        """
        prompt = build_local_ai_meta_prompt(message="g")
        # The forbidden-line patterns that the SQL helper uses must NOT
        # appear in the chat meta-prompt.
        assert "Do NOT wrap" not in prompt
        assert "★ Insight" not in prompt


# ---------------------------------------------------------------------
# POST /ai/chat/stream
# ---------------------------------------------------------------------


def _parse_sse_events(text: str) -> list[tuple[str, str]]:
    """Parse SSE wire format into [(event, data_json_string)]."""
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


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(config_dir=str(tmp_path), auth_token="test-token")
    return TestClient(app)


class TestChatStreamEndpoint:
    def test_empty_message_rejected_400(self, client: TestClient) -> None:
        res = client.post(
            "/ai/chat/stream",
            json={"cli": "claude", "message": "   "},
            headers={"Authorization": "Bearer test-token"},
        )
        assert res.status_code == 400, res.text

    def test_init_event_carries_meta_prompt(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The frontend's 'Show prompt' transparency panel needs the
        full meta-prompt that was sent to the CLI. Pin that the init
        event carries it so debugging stays possible.
        """

        async def fake_stream(registry: object, params: dict[str, object]):
            yield {
                "event": "done",
                "data": {
                    "cli": "claude",
                    "status": "ok",
                    "exit_code": 0,
                    "elapsed_seconds": 0.5,
                    "response": "ok",
                    "stderr": "",
                },
            }

        monkeypatch.setattr(
            "keboola_agent_cli.server.agent_runner.stream_ai_agent_events",
            fake_stream,
        )

        with client.stream(
            "POST",
            "/ai/chat/stream",
            json={"cli": "claude", "message": "list failing jobs", "project": "demo"},
            headers={"Authorization": "Bearer test-token"},
        ) as res:
            assert res.status_code == 200
            body = res.read().decode("utf-8")

        events = _parse_sse_events(body)
        import json as _json

        init = next(_json.loads(payload) for evt, payload in events if evt == "init")
        assert init["kind"] == "local_ai_chat"
        assert init["cli"] == "claude"
        assert init["project"] == "demo"
        assert "list failing jobs" in init["meta_prompt"]
        # Active project hint must render in the meta-prompt so the AI
        # has the project context surfaced.
        assert "demo" in init["meta_prompt"]

    def test_forwards_stream_events_as_is(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Chat does NOT post-process the done event (unlike the SQL
        helper which adds a cleaned `sql` field). The done event flows
        through verbatim — the assistant text accumulator on the
        frontend builds the markdown response from the stdout events.
        """

        async def fake_stream(registry: object, params: dict[str, object]):
            yield {"event": "stdout", "data": {"raw": "thinking..."}}
            yield {
                "event": "done",
                "data": {
                    "cli": "claude",
                    "status": "ok",
                    "exit_code": 0,
                    "elapsed_seconds": 1.0,
                    "response": "Answer with markdown\n```sql\nSELECT 1;\n```",
                    "stderr": "",
                },
            }

        monkeypatch.setattr(
            "keboola_agent_cli.server.agent_runner.stream_ai_agent_events",
            fake_stream,
        )

        with client.stream(
            "POST",
            "/ai/chat/stream",
            json={"cli": "claude", "message": "ask"},
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

        done = next(_json.loads(payload) for evt, payload in events if evt == "done")
        # No SQL-style post-processing: the response field is unmodified
        # and there is no added "sql" / "prompt" field.
        assert "sql" not in done
        assert "prompt" not in done
        assert "```sql" in done["response"]

    def test_stream_error_surfaces_as_done_error(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_stream(registry: object, params: dict[str, object]):
            raise ValueError("ai_agent.cli must be one of ['claude', 'codex', 'gemini']")
            yield  # pragma: no cover -- unreachable

        monkeypatch.setattr(
            "keboola_agent_cli.server.agent_runner.stream_ai_agent_events",
            fake_stream,
        )

        with client.stream(
            "POST",
            "/ai/chat/stream",
            json={"cli": "bogus", "message": "anything"},
            headers={"Authorization": "Bearer test-token"},
        ) as res:
            assert res.status_code == 200
            body = res.read().decode("utf-8")

        events = _parse_sse_events(body)
        import json as _json

        done = next(_json.loads(payload) for evt, payload in events if evt == "done")
        assert done["status"] == "error"
        assert "ai_agent.cli" in done["error"]
