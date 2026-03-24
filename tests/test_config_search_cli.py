"""Tests for `kbagent config search` command via CliRunner."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.project_service import ProjectService

TEST_TOKEN = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"

runner = CliRunner()


# ---------------------------------------------------------------------------
# Sample data - configs with body content for search to match against
# ---------------------------------------------------------------------------

SAMPLE_COMPONENTS_WITH_BODY = [
    {
        "id": "keboola.ex-db-snowflake",
        "name": "Snowflake Extractor",
        "type": "extractor",
        "configurations": [
            {
                "id": "101",
                "name": "Production Load",
                "description": "Loads production data from Snowflake",
                "configuration": {
                    "parameters": {
                        "db": {"host": "prod.snowflakecomputing.com", "database": "PROD_DB"},
                    }
                },
                "rows": [],
            },
            {
                "id": "102",
                "name": "Dev Load",
                "description": "Loads dev data",
                "configuration": {
                    "parameters": {
                        "db": {"host": "dev.snowflakecomputing.com", "database": "DEV_DB"},
                    }
                },
                "rows": [],
            },
        ],
    },
    {
        "id": "keboola.wr-db-snowflake",
        "name": "Snowflake Writer",
        "type": "writer",
        "configurations": [
            {
                "id": "201",
                "name": "Write to DWH",
                "description": "Writes to data warehouse",
                "configuration": {
                    "parameters": {"db": {"host": "dwh.snowflakecomputing.com"}},
                },
                "rows": [],
            },
        ],
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_config_store(config_dir: Path, projects: dict[str, dict] | None = None) -> ConfigStore:
    """Set up a ConfigStore with given projects for testing."""
    store = ConfigStore(config_dir=config_dir)
    if projects:
        for alias, info in projects.items():
            store.add_project(
                alias,
                ProjectConfig(
                    stack_url=info.get("stack_url", "https://connection.keboola.com"),
                    token=info["token"],
                    project_name=info.get("project_name", alias),
                    project_id=info.get("project_id", 1234),
                ),
            )
    return store


def _make_list_components_client(components: list[dict]) -> MagicMock:
    """Create a mock KeboolaClient with list_components returning given data."""
    mock_client = MagicMock()
    mock_client.list_components.return_value = components
    return mock_client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConfigSearch:
    """Tests for `kbagent config search` command."""

    def test_config_search_json_output(self, tmp_path: Path) -> None:
        """config search --json returns structured JSON with matches, errors, stats."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_components_client(SAMPLE_COMPONENTS_WITH_BODY)
        store = _setup_config_store(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(
                app,
                ["--json", "config", "search", "--query", "snowflakecomputing"],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"

        data = output["data"]
        assert "matches" in data
        assert "errors" in data
        assert "stats" in data

        # All 3 configs contain "snowflakecomputing" in their parameters
        assert data["stats"]["matches_found"] == 3
        assert data["stats"]["projects_searched"] == 1
        assert data["stats"]["configs_searched"] == 3

        # Verify match structure
        first_match = data["matches"][0]
        assert first_match["project_alias"] == "prod"
        assert "component_id" in first_match
        assert "config_id" in first_match
        assert "config_name" in first_match
        assert "match_locations" in first_match
        assert "match_count" in first_match
        assert first_match["match_count"] > 0

        assert data["errors"] == []

    def test_config_search_no_matches_json(self, tmp_path: Path) -> None:
        """config search --json returns empty result with stats when nothing matches."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_components_client(SAMPLE_COMPONENTS_WITH_BODY)
        store = _setup_config_store(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(
                app,
                ["--json", "config", "search", "--query", "nonexistent_term_xyz_999"],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"

        data = output["data"]
        assert data["matches"] == []
        assert data["stats"]["matches_found"] == 0
        assert data["stats"]["projects_searched"] == 1
        assert data["stats"]["configs_searched"] == 3
        assert data["errors"] == []

    def test_config_search_invalid_regex(self, tmp_path: Path) -> None:
        """config search --regex with bad pattern exits with code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_store(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(
                config_store=store,
                client_factory=lambda url, token: MagicMock(),
            )

            result = runner.invoke(
                app,
                ["--json", "config", "search", "--query", "[invalid(", "--regex"],
            )

        assert result.exit_code == 2, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_ARGUMENT"
        assert "regex" in output["error"]["message"].lower()

    def test_config_search_invalid_component_type(self, tmp_path: Path) -> None:
        """config search --component-type with invalid value exits with code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_store(
            config_dir,
            {"prod": {"token": TEST_TOKEN}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(
                config_store=store,
                client_factory=lambda url, token: MagicMock(),
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "search",
                    "--query",
                    "test",
                    "--component-type",
                    "bogus_type",
                ],
            )

        assert result.exit_code == 2, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_ARGUMENT"
        assert "bogus_type" in output["error"]["message"]

    def test_config_search_unknown_project(self, tmp_path: Path) -> None:
        """config search --project with unknown alias exits with code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        # Store has NO projects registered
        store = _setup_config_store(config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(
                config_store=store,
                client_factory=lambda url, token: MagicMock(),
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "search",
                    "--query",
                    "test",
                    "--project",
                    "nonexistent",
                ],
            )

        assert result.exit_code == 5, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"
