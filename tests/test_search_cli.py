"""Tests for `kbagent search` command via CliRunner."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.project_service import ProjectService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

runner = CliRunner()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(config_dir: Path, projects: dict[str, dict] | None = None) -> ConfigStore:
    store = ConfigStore(config_dir=config_dir)
    if projects:
        for alias, info in projects.items():
            store.add_project(
                alias,
                ProjectConfig(
                    stack_url=info.get("stack_url", "https://connection.keboola.com"),
                    token=info.get("token", TEST_TOKEN),
                    project_name=info.get("project_name", alias),
                    project_id=info.get("project_id", 1234),
                ),
            )
    return store


def _make_service_result(
    results: list[dict] | None = None,
    errors: list[dict] | None = None,
    projects_searched: int = 1,
) -> dict:
    res = results or []
    return {
        "results": res,
        "errors": errors or [],
        "stats": {
            "projects_searched": projects_searched,
            "results_found": len(res),
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSearchCommandJson:
    """JSON output tests for `kbagent search`."""

    def test_basic_search_returns_ok(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _make_store(config_dir, {"prod": {"token": TEST_TOKEN}})

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SearchService") as MockSearchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            mock_svc = MagicMock()
            mock_svc.search.return_value = _make_service_result(
                results=[
                    {
                        "project_alias": "prod",
                        "type": "table",
                        "id": "in.c-main.orders",
                        "name": "orders",
                        "description": "",
                        "component_id": None,
                        "project_id": 1234,
                        "project_name": "Prod",
                    }
                ]
            )
            MockSearchService.return_value = mock_svc

            result = runner.invoke(app, ["--json", "search", "orders"], catch_exceptions=False)

        assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert "results" in data["data"]
        assert "errors" in data["data"]
        assert "stats" in data["data"]
        assert data["data"]["stats"]["results_found"] == 1

    def test_no_results_returns_ok_with_empty_list(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _make_store(config_dir, {"prod": {"token": TEST_TOKEN}})

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SearchService") as MockSearchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            mock_svc = MagicMock()
            mock_svc.search.return_value = _make_service_result()
            MockSearchService.return_value = mock_svc

            result = runner.invoke(
                app, ["--json", "search", "xyzzy_nonexistent"], catch_exceptions=False
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["data"]["results"] == []
        assert data["data"]["stats"]["results_found"] == 0

    def test_type_filter_passed_to_service(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _make_store(config_dir, {"prod": {"token": TEST_TOKEN}})

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SearchService") as MockSearchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            mock_svc = MagicMock()
            mock_svc.search.return_value = _make_service_result()
            MockSearchService.return_value = mock_svc

            result = runner.invoke(
                app,
                ["--json", "search", "revenue", "--type", "table", "--type", "bucket"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        call_kwargs = mock_svc.search.call_args
        item_types = call_kwargs.kwargs.get("item_types") or call_kwargs[1].get("item_types")
        assert "table" in item_types
        assert "bucket" in item_types

    def test_config_based_search_type(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _make_store(config_dir, {"prod": {"token": TEST_TOKEN}})

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SearchService") as MockSearchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            mock_svc = MagicMock()
            mock_svc.search.return_value = _make_service_result()
            MockSearchService.return_value = mock_svc

            result = runner.invoke(
                app,
                ["--json", "search", "WHERE", "--search-type", "config-based"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        call_kwargs = mock_svc.search.call_args
        search_type = call_kwargs.kwargs.get("search_type") or call_kwargs[1].get("search_type")
        assert search_type == "config-based"

    def test_project_filter_passed_to_service(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _make_store(config_dir, {"prod": {"token": TEST_TOKEN}})

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SearchService") as MockSearchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            mock_svc = MagicMock()
            mock_svc.search.return_value = _make_service_result()
            MockSearchService.return_value = mock_svc

            result = runner.invoke(
                app,
                ["--json", "search", "test", "--project", "prod"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        call_kwargs = mock_svc.search.call_args
        aliases = call_kwargs.kwargs.get("aliases") or call_kwargs[1].get("aliases")
        assert aliases == ["prod"]

    def test_limit_option_passed_to_service(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _make_store(config_dir, {"prod": {"token": TEST_TOKEN}})

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SearchService") as MockSearchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            mock_svc = MagicMock()
            mock_svc.search.return_value = _make_service_result()
            MockSearchService.return_value = mock_svc

            result = runner.invoke(
                app,
                ["--json", "search", "test", "--limit", "10"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        call_kwargs = mock_svc.search.call_args
        limit = call_kwargs.kwargs.get("limit") or call_kwargs[1].get("limit")
        assert limit == 10


class TestSearchCommandValidation:
    """Input validation tests."""

    def test_invalid_type_exits_with_code_2(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _make_store(config_dir, {"prod": {"token": TEST_TOKEN}})

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SearchService"),
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            result = runner.invoke(
                app,
                ["--json", "search", "test", "--type", "invalid-type"],
            )

        assert result.exit_code == 2

    def test_invalid_search_type_exits_with_code_2(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _make_store(config_dir, {"prod": {"token": TEST_TOKEN}})

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SearchService"),
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            result = runner.invoke(
                app,
                ["--json", "search", "test", "--search-type", "bad-mode"],
            )

        assert result.exit_code == 2

    def test_missing_query_shows_help(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _make_store(config_dir, {"prod": {"token": TEST_TOKEN}})

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SearchService"),
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            result = runner.invoke(app, ["search"])

        # Missing required argument should exit non-zero.
        assert result.exit_code != 0


class TestSearchCommandHuman:
    """Human-readable output tests."""

    def test_human_output_shows_table(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _make_store(config_dir, {"prod": {"token": TEST_TOKEN}})

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SearchService") as MockSearchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            mock_svc = MagicMock()
            mock_svc.search.return_value = _make_service_result(
                results=[
                    {
                        "project_alias": "prod",
                        "type": "table",
                        "id": "in.c-main.orders",
                        "name": "orders",
                        "description": "",
                        "component_id": None,
                    }
                ]
            )
            MockSearchService.return_value = mock_svc

            result = runner.invoke(app, ["search", "orders"], catch_exceptions=False)

        assert result.exit_code == 0
        # Should contain the result ID somewhere in human output.
        assert "orders" in result.output or "in.c-main" in result.output

    def test_human_output_no_results_message(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _make_store(config_dir, {"prod": {"token": TEST_TOKEN}})

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SearchService") as MockSearchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            mock_svc = MagicMock()
            mock_svc.search.return_value = _make_service_result()
            MockSearchService.return_value = mock_svc

            result = runner.invoke(app, ["search", "xyzzy_nonexistent"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "No results" in result.output


class TestSearchRegexFlag:
    """`--regex` flag wiring + config-based incompatibility."""

    def test_regex_flag_passed_to_service(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _make_store(config_dir, {"prod": {"token": TEST_TOKEN}})
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SearchService") as MockSearchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = MagicMock(spec=ProjectService)
            mock_svc = MockSearchService.return_value
            mock_svc.search.return_value = _make_service_result()

            result = runner.invoke(app, ["search", ".*orders.*", "--regex", "--project", "prod"])

        assert result.exit_code == 0
        _, kwargs = mock_svc.search.call_args
        assert kwargs["regex"] is True

    def test_regex_default_false(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _make_store(config_dir, {"prod": {"token": TEST_TOKEN}})
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SearchService") as MockSearchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = MagicMock(spec=ProjectService)
            mock_svc = MockSearchService.return_value
            mock_svc.search.return_value = _make_service_result()

            result = runner.invoke(app, ["search", "orders", "--project", "prod"])

        assert result.exit_code == 0
        _, kwargs = mock_svc.search.call_args
        assert kwargs["regex"] is False

    def test_regex_with_config_based_is_usage_error(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _make_store(config_dir, {"prod": {"token": TEST_TOKEN}})
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SearchService"),
        ):
            MockStore.return_value = store
            MockProjService.return_value = MagicMock(spec=ProjectService)

            result = runner.invoke(
                app,
                ["search", ".*", "--regex", "--search-type", "config-based", "--project", "prod"],
            )

        assert result.exit_code == 2


class TestSearchMatchedColumns:
    """Matched columns rendering (human table) + JSON passthrough."""

    def _table_result(self) -> dict:
        return _make_service_result(
            results=[
                {
                    "project_alias": "prod",
                    "type": "table",
                    "id": "in.c-main.orders",
                    "name": "orders",
                    "component_id": None,
                    "matched_columns": ["email", "name"],
                }
            ]
        )

    def test_matched_columns_rendered_in_table(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _make_store(config_dir, {"prod": {"token": TEST_TOKEN}})
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SearchService") as MockSearchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = MagicMock(spec=ProjectService)
            MockSearchService.return_value.search.return_value = self._table_result()

            result = runner.invoke(app, ["search", "email", "--project", "prod"])

        assert result.exit_code == 0
        assert "Matched columns" in result.stdout
        assert "email" in result.stdout

    def test_matched_columns_in_json(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _make_store(config_dir, {"prod": {"token": TEST_TOKEN}})
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SearchService") as MockSearchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = MagicMock(spec=ProjectService)
            MockSearchService.return_value.search.return_value = self._table_result()

            result = runner.invoke(app, ["--json", "search", "email", "--project", "prod"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["data"]["results"][0]["matched_columns"] == ["email", "name"]

    def test_matched_columns_column_hidden_when_no_matches(self, tmp_path: Path) -> None:
        """The 'Matched columns' column is omitted when no result matched via a column."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _make_store(config_dir, {"prod": {"token": TEST_TOKEN}})
        plain = _make_service_result(
            results=[
                {
                    "project_alias": "prod",
                    "type": "table",
                    "id": "in.c-main.orders",
                    "name": "orders",
                    "component_id": None,
                    "matched_columns": [],
                }
            ]
        )
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SearchService") as MockSearchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = MagicMock(spec=ProjectService)
            MockSearchService.return_value.search.return_value = plain

            result = runner.invoke(app, ["search", "orders", "--project", "prod"])

        assert result.exit_code == 0
        assert "Matched columns" not in result.stdout


class TestSearchScopeFlag:
    """`kbagent search --scope` reaches SearchService and rejects bad combos."""

    def _invoke(self, tmp_path: Path, args: list[str]):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _make_store(config_dir, {"prod": {"token": TEST_TOKEN}})
        mock_svc = MagicMock()
        mock_svc.search.return_value = _make_service_result()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.SearchService") as MockSearchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockSearchService.return_value = mock_svc
            result = runner.invoke(app, ["--json", "search", *args])
        return result, mock_svc

    def test_repeated_scope_is_forwarded(self, tmp_path: Path) -> None:
        result, mock_svc = self._invoke(
            tmp_path,
            [
                "orders",
                "--search-type",
                "config-based",
                "--scope",
                "storage.input",
                "--scope",
                "storage.output",
            ],
        )

        assert result.exit_code == 0, result.output
        assert mock_svc.search.call_args.kwargs["scopes"] == ["storage.input", "storage.output"]

    def test_no_scope_passes_empty_list(self, tmp_path: Path) -> None:
        result, mock_svc = self._invoke(tmp_path, ["orders"])

        assert result.exit_code == 0, result.output
        assert mock_svc.search.call_args.kwargs["scopes"] == []

    def test_scope_with_textual_search_exits_2(self, tmp_path: Path) -> None:
        result, mock_svc = self._invoke(tmp_path, ["orders", "--scope", "parameters"])

        assert result.exit_code == 2, result.output
        assert json.loads(result.output)["error"]["code"] == "INVALID_ARGUMENT"
        mock_svc.search.assert_not_called()
