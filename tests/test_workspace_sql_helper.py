"""Tests for the AI-driven SQL helper used by the workspace SQL editor.

Mirrors :mod:`tests.test_agent_prompt_helper` but for the workspace-side
helpers:

- ``build_sql_helper_meta_prompt`` / ``clean_sql_helper_response`` -- pure
  text helpers in :mod:`keboola_agent_cli.server.agent_runner`.
- ``POST /workspaces/sql/improve/stream`` -- the SSE endpoint that wires
  the helpers into the chosen AI CLI. We mock :func:`stream_ai_agent_events`
  so the test does not spawn a real ``claude`` / ``codex`` / ``gemini``
  subprocess and verify the endpoint forwards events and enriches the
  ``done`` payload with a cleaned ``sql`` field.
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
    build_sql_helper_meta_prompt,
    clean_sql_helper_response,
)

# ---------------------------------------------------------------------
# build_sql_helper_meta_prompt
# ---------------------------------------------------------------------


class TestBuildSqlHelperMetaPrompt:
    def test_includes_goal_verbatim(self) -> None:
        prompt = build_sql_helper_meta_prompt(
            goal="top 10 orders by revenue last 30 days",
            project="demo",
            backend="snowflake",
            schema="WORKSPACE_123",
        )
        assert "top 10 orders by revenue last 30 days" in prompt

    def test_includes_project_and_backend(self) -> None:
        prompt = build_sql_helper_meta_prompt(
            goal="g",
            project="padak",
            backend="bigquery",
            schema="my_dataset",
        )
        assert "padak" in prompt
        assert "bigquery" in prompt
        assert "my_dataset" in prompt

    def test_draft_block_present_when_draft_given(self) -> None:
        prompt = build_sql_helper_meta_prompt(
            goal="g",
            project="demo",
            backend="snowflake",
            schema="s",
            draft_sql="SELECT * FROM orders LIMIT 5",
        )
        assert "SELECT * FROM orders LIMIT 5" in prompt
        assert "refine this" in prompt

    def test_empty_draft_marked_explicitly(self) -> None:
        prompt = build_sql_helper_meta_prompt(
            goal="g",
            project="demo",
            backend="snowflake",
            schema="s",
            draft_sql="   ",
        )
        assert "empty -- write the query from scratch" in prompt

    def test_bucket_hint_lists_visible_buckets(self) -> None:
        prompt = build_sql_helper_meta_prompt(
            goal="g",
            project="demo",
            backend="snowflake",
            schema="s",
            bucket_ids=["in.c-orders", "in.c-customers"],
        )
        assert "in.c-orders" in prompt
        assert "in.c-customers" in prompt
        assert "VISIBLE BUCKETS" in prompt

    def test_bucket_hint_truncates_at_50(self) -> None:
        """Long bucket lists are truncated so the prompt stays tractable."""
        prompt = build_sql_helper_meta_prompt(
            goal="g",
            project="demo",
            backend="snowflake",
            schema="s",
            bucket_ids=[f"in.c-b{i}" for i in range(75)],
        )
        assert "and 25 more (truncated)" in prompt

    def test_bigquery_backend_hint_specifies_dataset_path(self) -> None:
        prompt = build_sql_helper_meta_prompt(
            goal="g",
            project="demo",
            backend="BigQuery",  # mixed case to test normalization
            schema="my_dataset",
        )
        assert "BACKEND HINT (BigQuery)" in prompt
        assert "`my_dataset.INFORMATION_SCHEMA.TABLES`" in prompt

    def test_snowflake_backend_hint_uses_current_schema(self) -> None:
        prompt = build_sql_helper_meta_prompt(
            goal="g",
            project="demo",
            backend="snowflake",
            schema="WORKSPACE_X",
        )
        assert "BACKEND HINT (Snowflake)" in prompt
        assert "CURRENT_SCHEMA()" in prompt

    def test_unknown_backend_gets_generic_hint(self) -> None:
        prompt = build_sql_helper_meta_prompt(
            goal="g",
            project="demo",
            backend="postgres",
            schema="s",
        )
        assert "BACKEND HINT (postgres)" in prompt
        # Generic hint mentions both Snowflake and BigQuery conventions.
        assert "current_schema()" in prompt

    def test_output_contract_present(self) -> None:
        """The OUTPUT CONTRACT block is the whole reason the helper works.

        Without it, claude wraps SQL in ```sql fences or starts with
        "Here's the SQL:" and the user's editor receives garbage. Pin the
        contract so accidental trimming surfaces as a test failure.
        """
        prompt = build_sql_helper_meta_prompt(
            goal="g",
            project="demo",
            backend="snowflake",
            schema="s",
        )
        assert "OUTPUT CONTRACT" in prompt
        assert "Output ONLY the SQL" in prompt
        assert "```sql" in prompt  # the contract names the forbidden fence form

    def test_discovery_block_references_kbagent_commands(self) -> None:
        """The meta-prompt anchors the AI in REAL kbagent commands for
        catalog discovery so it doesn't invent flag-less variants.
        """
        prompt = build_sql_helper_meta_prompt(
            goal="g",
            project="demo",
            backend="snowflake",
            schema="s",
        )
        assert "kbagent workspace query" in prompt
        assert "kbagent storage table-detail" in prompt

    def test_fix_mode_when_failed_error_set(self) -> None:
        """When the user clicks 'Send to AI for fix' the helper pivots from
        ``write SQL for goal`` to ``fix this failing SQL``: the prompt must
        carry both the failing query AND the warehouse error verbatim so
        the AI can correlate them with schema discovery output.
        """
        prompt = build_sql_helper_meta_prompt(
            goal="Fix this SQL",
            project="demo",
            backend="snowflake",
            schema="WORKSPACE_X",
            draft_sql='SELECT * FROM "in.c-shared"."t";',
            failed_error="SQL compilation error: Object 'IN.C-SHARED.T' does not exist",
        )
        assert "FAILED QUERY" in prompt
        assert "WAREHOUSE ERROR" in prompt
        assert "Object 'IN.C-SHARED.T' does not exist" in prompt
        # The draft must NOT be labelled "refine this draft" in fix mode -- that
        # framing pushes the AI to keep the broken query as a starting point.
        assert "refine this, don't throw it away" not in prompt

    def test_fix_mode_with_empty_draft(self) -> None:
        """Edge case: error came back but the editor was cleared between Run
        and 'Send to AI'. The prompt must still convey both error and the
        absence of a query, not silently fall back to 'write from scratch'.
        """
        prompt = build_sql_helper_meta_prompt(
            goal="Fix this SQL",
            project="demo",
            backend="snowflake",
            schema="s",
            draft_sql="",
            failed_error="Table not found",
        )
        assert "Table not found" in prompt
        assert "query body empty" in prompt

    def test_linked_bucket_warning_present(self) -> None:
        """Without explicit linked-bucket guidance the AI generates SQL like
        ``"in.c-shared"."table"`` for linked buckets, which silently fails
        with "table not found" at run time. The meta-prompt forces the AI
        to call ``bucket-detail`` and use the returned ``sql_path`` so
        cross-project paths come out correct (e.g. Snowflake's
        ``"KBC_USE4_340"."out.c-shared"."t"``).
        """
        prompt = build_sql_helper_meta_prompt(
            goal="g",
            project="demo",
            backend="snowflake",
            schema="s",
        )
        assert "LINKED BUCKETS" in prompt
        assert "kbagent storage bucket-detail" in prompt
        assert "sql_path" in prompt


# ---------------------------------------------------------------------
# clean_sql_helper_response
# ---------------------------------------------------------------------


class TestCleanSqlHelperResponse:
    def test_passthrough_clean_sql(self) -> None:
        body = (
            "-- top orders by revenue\nSELECT id, total FROM orders ORDER BY total DESC LIMIT 10;"
        )
        assert clean_sql_helper_response(body) == body

    def test_strips_sql_code_fence(self) -> None:
        raw = "```sql\nSELECT 1;\n```"
        assert clean_sql_helper_response(raw) == "SELECT 1;"

    def test_strips_plain_code_fence(self) -> None:
        raw = "```\nSELECT 1;\n```"
        assert clean_sql_helper_response(raw) == "SELECT 1;"

    def test_strips_here_is_the_sql_preamble(self) -> None:
        raw = "Here's the SQL:\n\nSELECT 1;"
        assert clean_sql_helper_response(raw) == "SELECT 1;"

    def test_strips_query_preamble(self) -> None:
        raw = "Query:\nSELECT 1;"
        assert clean_sql_helper_response(raw) == "SELECT 1;"

    def test_strips_surrounding_whitespace(self) -> None:
        assert clean_sql_helper_response("\n\n  SELECT 1;  \n\n") == "SELECT 1;"

    def test_empty_input_yields_empty(self) -> None:
        assert clean_sql_helper_response("") == ""

    def test_dedups_exact_double_body(self) -> None:
        """Same claude jsonl duplication quirk as the prompt helper."""
        body = "SELECT id FROM orders ORDER BY total DESC LIMIT 10;"
        raw = body + body
        assert clean_sql_helper_response(raw) == body

    def test_does_not_dedup_unrelated_halves(self) -> None:
        raw = "SELECT a FROM t1;\nSELECT b FROM t2;"
        # The two halves aren't equal -- the function must leave it alone.
        assert clean_sql_helper_response(raw) == raw


# ---------------------------------------------------------------------
# POST /workspaces/sql/improve/stream
# ---------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(config_dir=str(tmp_path), auth_token="test-token")
    return TestClient(app)


def _parse_sse_events(text: str) -> list[tuple[str, str]]:
    """Parse the SSE wire-format body into [(event, data_json_string)]."""
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


class TestImproveSqlStreamEndpoint:
    def test_empty_goal_rejected_400(self, client: TestClient) -> None:
        res = client.post(
            "/workspaces/sql/improve/stream",
            json={
                "cli": "claude",
                "goal": "   ",
                "project": "demo",
                "backend": "snowflake",
                "schema_name": "WORKSPACE_X",
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert res.status_code == 400, res.text

    def test_streams_events_and_enriches_done_with_sql(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Happy path: mocked AI stream events get forwarded, and the final
        ``done`` payload carries a cleaned ``sql`` field plus the raw
        response for debugging.
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
                    "elapsed_seconds": 1.5,
                    "response": "```sql\nSELECT id FROM orders LIMIT 10;\n```",
                    "stderr": "",
                },
            }

        # The endpoint imports stream_ai_agent_events locally inside the
        # handler; patching the module-level name in agent_runner works
        # because Python re-resolves it on each call.
        monkeypatch.setattr(
            "keboola_agent_cli.server.agent_runner.stream_ai_agent_events",
            fake_stream,
        )

        with client.stream(
            "POST",
            "/workspaces/sql/improve/stream",
            json={
                "cli": "claude",
                "goal": "top 10 orders by id",
                "project": "demo",
                "backend": "snowflake",
                "schema_name": "WORKSPACE_X",
                "bucket_ids": ["in.c-orders"],
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
        # ```sql fence must be stripped before the SQL reaches the UI.
        assert done_data["sql"] == "SELECT id FROM orders LIMIT 10;"
        # raw_response preserved for debugging.
        assert "```sql" in done_data["raw_response"]
        assert done_data["status"] == "ok"

    def test_stream_error_surfaces_as_done_error(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ValueError from ``stream_ai_agent_events`` must surface as a
        final ``done`` event with status ``error`` so the React side can
        render it without hanging on an unterminated stream.
        """

        async def fake_stream(
            registry: object,
            params: dict[str, object],
        ):
            raise ValueError("ai_agent.cli must be one of ['claude', 'codex', 'gemini']")
            yield  # pragma: no cover -- async generator marker

        monkeypatch.setattr(
            "keboola_agent_cli.server.agent_runner.stream_ai_agent_events",
            fake_stream,
        )

        with client.stream(
            "POST",
            "/workspaces/sql/improve/stream",
            json={
                "cli": "bogus",
                "goal": "anything",
                "project": "demo",
                "backend": "snowflake",
                "schema_name": "s",
            },
            headers={"Authorization": "Bearer test-token"},
        ) as res:
            assert res.status_code == 200
            body = res.read().decode("utf-8")

        events = _parse_sse_events(body)
        import json as _json

        done_data = next(_json.loads(payload) for evt, payload in events if evt == "done")
        assert done_data["status"] == "error"
        assert "ai_agent.cli" in done_data["error"]
