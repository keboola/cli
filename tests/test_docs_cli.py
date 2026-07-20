"""Tests for the `docs query` command and DocsService.

The docs_app is not yet wired into cli.py (central wiring happens
separately), so CLI tests mount docs_app on a minimal Typer root app
that provides the same ctx.obj contract (formatter + docs_service +
permission_engine) the real CLI callback builds.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import typer
from typer.testing import CliRunner

from keboola_agent_cli.commands.docs import docs_app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.output import OutputFormatter
from keboola_agent_cli.services.docs_service import DocsService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

runner = CliRunner()

ANSWER = {
    "query": "how do incremental loads work?",
    "text": "Incremental loading appends only changed rows to the table.",
    "source_urls": [
        "https://help.keboola.com/storage/tables/#incremental-loading",
        "https://help.keboola.com/components/",
    ],
}


def _build_app(mock_service: MagicMock, json_mode: bool) -> typer.Typer:
    """Build a minimal root app mounting docs_app with a stubbed ctx.obj."""
    app = typer.Typer()

    @app.callback()
    def _root(ctx: typer.Context) -> None:
        ctx.ensure_object(dict)
        ctx.obj["formatter"] = OutputFormatter(json_mode=json_mode, no_color=True)
        ctx.obj["docs_service"] = mock_service
        ctx.obj["permission_engine"] = None

    app.add_typer(docs_app, name="docs")
    return app


# ---------------------------------------------------------------------------
# docs query (CLI layer)
# ---------------------------------------------------------------------------


class TestDocsQueryCli:
    """Tests for `kbagent docs query` command."""

    def test_docs_query_json(self) -> None:
        """docs query --json returns {query, text, source_urls}."""
        mock_svc = MagicMock()
        mock_svc.ask_docs.return_value = dict(ANSWER)

        app = _build_app(mock_svc, json_mode=True)
        result = runner.invoke(app, ["docs", "query", "how do incremental loads work?"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["query"] == "how do incremental loads work?"
        assert output["data"]["text"].startswith("Incremental loading")
        assert output["data"]["source_urls"] == ANSWER["source_urls"]
        mock_svc.ask_docs.assert_called_once_with(
            alias=None,
            query="how do incremental loads work?",
        )

    def test_docs_query_passes_project(self) -> None:
        """docs query --project forwards the alias to the service."""
        mock_svc = MagicMock()
        mock_svc.ask_docs.return_value = dict(ANSWER)

        app = _build_app(mock_svc, json_mode=True)
        result = runner.invoke(
            app,
            ["docs", "query", "how do incremental loads work?", "--project", "prod"],
        )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        mock_svc.ask_docs.assert_called_once_with(
            alias="prod",
            query="how do incremental loads work?",
        )

    def test_docs_query_human(self) -> None:
        """docs query in human mode renders a panel with answer and sources."""
        mock_svc = MagicMock()
        mock_svc.ask_docs.return_value = dict(ANSWER)

        app = _build_app(mock_svc, json_mode=False)
        result = runner.invoke(app, ["docs", "query", "how do incremental loads work?"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "Keboola Docs" in result.output
        assert "Incremental loading appends" in result.output
        assert "Sources:" in result.output
        assert "help.keboola.com" in result.output

    def test_docs_query_human_empty_answer(self) -> None:
        """docs query in human mode handles an empty answer gracefully."""
        mock_svc = MagicMock()
        mock_svc.ask_docs.return_value = {"query": "q", "text": "", "source_urls": []}

        app = _build_app(mock_svc, json_mode=False)
        result = runner.invoke(app, ["docs", "query", "q"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "no answer text returned" in result.output

    def test_docs_query_api_error(self) -> None:
        """docs query surfaces KeboolaApiError as a structured error envelope."""
        mock_svc = MagicMock()
        mock_svc.ask_docs.side_effect = KeboolaApiError(
            message="AI service unavailable",
            status_code=503,
            error_code="RETRY_EXHAUSTED",
            retryable=True,
        )

        app = _build_app(mock_svc, json_mode=True)
        result = runner.invoke(app, ["docs", "query", "anything"])

        # RETRY_EXHAUSTED maps to exit code 4 (network/retryable)
        assert result.exit_code == 4
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "RETRY_EXHAUSTED"
        assert output["error"]["retryable"] is True

    def test_docs_query_config_error(self) -> None:
        """docs query maps ConfigError to exit code 5."""
        mock_svc = MagicMock()
        mock_svc.ask_docs.side_effect = ConfigError("No projects configured.")

        app = _build_app(mock_svc, json_mode=True)
        result = runner.invoke(app, ["docs", "query", "anything"])

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"

    def test_docs_query_missing_question(self) -> None:
        """docs query without the positional QUESTION is a usage error (exit 2)."""
        mock_svc = MagicMock()

        app = _build_app(mock_svc, json_mode=True)
        result = runner.invoke(app, ["docs", "query"])

        assert result.exit_code == 2
        mock_svc.ask_docs.assert_not_called()


# ---------------------------------------------------------------------------
# DocsService (service layer)
# ---------------------------------------------------------------------------


class TestDocsService:
    """Unit tests for DocsService.ask_docs alias resolution and normalization."""

    def _make_store(self, tmp_path: Path, aliases: list[str]) -> ConfigStore:
        store = ConfigStore(config_dir=tmp_path / "config")
        for i, alias in enumerate(aliases):
            store.add_project(
                alias,
                ProjectConfig(
                    stack_url=f"https://connection.{alias}.keboola.com",
                    token=TEST_TOKEN,
                    project_name=alias,
                    project_id=1000 + i,
                ),
            )
        return store

    def _make_service(
        self, store: ConfigStore, raw_answer: dict
    ) -> tuple[DocsService, MagicMock, MagicMock]:
        mock_client = MagicMock()
        mock_client.docs_question.return_value = raw_answer
        mock_factory = MagicMock(return_value=mock_client)
        service = DocsService(config_store=store, ai_client_factory=mock_factory)
        return service, mock_client, mock_factory

    def test_ask_docs_explicit_alias(self, tmp_path: Path) -> None:
        """Explicit alias resolves that project's stack URL and token."""
        store = self._make_store(tmp_path, ["prod", "dev"])
        raw = {"text": "Answer.", "sourceUrls": ["https://help.keboola.com/x"]}
        service, mock_client, mock_factory = self._make_service(store, raw)

        result = service.ask_docs(alias="dev", query="what is a bucket?")

        mock_factory.assert_called_once_with(
            "https://connection.dev.keboola.com",
            TEST_TOKEN,
        )
        mock_client.docs_question.assert_called_once_with("what is a bucket?")
        mock_client.close.assert_called_once()
        assert result == {
            "query": "what is a bucket?",
            "text": "Answer.",
            "source_urls": ["https://help.keboola.com/x"],
        }

    def test_ask_docs_default_alias_uses_first_project(self, tmp_path: Path) -> None:
        """alias=None falls back to the first configured project."""
        store = self._make_store(tmp_path, ["prod", "dev"])
        raw = {"text": "Answer.", "sourceUrls": []}
        service, _mock_client, mock_factory = self._make_service(store, raw)

        result = service.ask_docs(alias=None, query="q")

        mock_factory.assert_called_once_with(
            "https://connection.prod.keboola.com",
            TEST_TOKEN,
        )
        assert result["source_urls"] == []

    def test_ask_docs_unknown_alias_raises_config_error(self, tmp_path: Path) -> None:
        """Unknown alias raises ConfigError before any HTTP call."""
        store = self._make_store(tmp_path, ["prod"])
        service, mock_client, _mock_factory = self._make_service(store, {})

        with pytest.raises(ConfigError):
            service.ask_docs(alias="nope", query="q")
        mock_client.docs_question.assert_not_called()

    def test_ask_docs_no_projects_raises_config_error(self, tmp_path: Path) -> None:
        """No configured projects raises an actionable ConfigError."""
        store = self._make_store(tmp_path, [])
        service, mock_client, _mock_factory = self._make_service(store, {})

        with pytest.raises(ConfigError, match="No projects configured"):
            service.ask_docs(alias=None, query="q")
        mock_client.docs_question.assert_not_called()

    def test_ask_docs_normalizes_null_fields(self, tmp_path: Path) -> None:
        """Explicit nulls from the AI Service normalize to '' / []."""
        store = self._make_store(tmp_path, ["prod"])
        raw = {"text": None, "sourceUrls": None}
        service, _mock_client, _mock_factory = self._make_service(store, raw)

        result = service.ask_docs(alias="prod", query="q")

        assert result["text"] == ""
        assert result["source_urls"] == []

    def test_ask_docs_closes_client_on_error(self, tmp_path: Path) -> None:
        """The AI client is closed even when docs_question raises."""
        store = self._make_store(tmp_path, ["prod"])
        service, mock_client, _mock_factory = self._make_service(store, {})
        mock_client.docs_question.side_effect = KeboolaApiError(
            message="boom",
            status_code=500,
            error_code="RETRY_EXHAUSTED",
            retryable=True,
        )

        with pytest.raises(KeboolaApiError):
            service.ask_docs(alias="prod", query="q")
        mock_client.close.assert_called_once()
