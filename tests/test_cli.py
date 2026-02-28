"""Tests for CLI commands via CliRunner - project, config, context, doctor commands."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig, TokenVerifyResponse
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.job_service import JobService
from keboola_agent_cli.services.lineage_service import LineageService
from keboola_agent_cli.services.org_service import OrgService
from keboola_agent_cli.services.project_service import ProjectService

from helpers import make_mock_client

TEST_TOKEN = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"

runner = CliRunner()


class TestProjectAdd:
    """Tests for `kbagent project add` command."""

    def test_project_add_success_json(self, tmp_path: Path) -> None:
        """project add with --json outputs structured success response."""
        mock_client = make_mock_client(project_name="Prod Project", project_id=5678)
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--alias",
                    "prod",
                    "--url",
                    "https://connection.keboola.com",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["alias"] == "prod"
        assert output["data"]["project_name"] == "Prod Project"
        assert output["data"]["project_id"] == 5678
        # Token should be masked
        assert "10493007" not in output["data"]["token"]

    def test_project_add_success_human(self, tmp_path: Path) -> None:
        """project add in human mode outputs success message."""
        mock_client = make_mock_client()
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            result = runner.invoke(
                app,
                [
                    "project",
                    "add",
                    "--alias",
                    "test",
                    "--url",
                    "https://connection.keboola.com",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "test" in result.output
        assert "Success" in result.output or "Test Project" in result.output

    def test_project_add_invalid_token_exit_code_3(self, tmp_path: Path) -> None:
        """project add with invalid token returns exit code 3."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        fail_client = MagicMock()
        fail_client.verify_token.side_effect = KeboolaApiError(
            message="Invalid token",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": "invalid-token-abcdefgh"}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: fail_client,
            )
            MockService.return_value = service_instance

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--alias",
                    "bad",
                ],
            )

        assert result.exit_code == 3
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_TOKEN"

    def test_project_add_timeout_exit_code_4(self, tmp_path: Path) -> None:
        """project add with network timeout returns exit code 4."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        timeout_client = MagicMock()
        timeout_client.verify_token.side_effect = KeboolaApiError(
            message="Request timed out",
            status_code=0,
            error_code="TIMEOUT",
            retryable=True,
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: timeout_client,
            )
            MockService.return_value = service_instance

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--alias",
                    "timeout",
                ],
            )

        assert result.exit_code == 4


class TestProjectList:
    """Tests for `kbagent project list` command."""

    def test_project_list_json_empty(self, tmp_path: Path) -> None:
        """project list --json with no projects returns empty data."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance
            MockService.return_value = ProjectService(config_store=store_instance)

            result = runner.invoke(app, ["--json", "project", "list"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"] == []

    def test_project_list_json_with_projects(self, tmp_path: Path) -> None:
        """project list --json returns project data with masked tokens."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        mock_client = make_mock_client()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            # Add a project first
            runner.invoke(
                app,
                [
                    "project",
                    "add",
                    "--alias",
                    "test",
                    "--url",
                    "https://connection.keboola.com",
                ],
            )

            result = runner.invoke(app, ["--json", "project", "list"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert len(output["data"]) == 1
        assert output["data"][0]["alias"] == "test"
        # Token must be masked
        assert output["data"][0]["token"] != TEST_TOKEN

    def test_project_list_human_mode(self, tmp_path: Path) -> None:
        """project list in human mode outputs a Rich table."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        mock_client = make_mock_client(project_name="My Production")

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            runner.invoke(
                app,
                [
                    "project",
                    "add",
                    "--alias",
                    "prod",
                    "--url",
                    "https://connection.keboola.com",
                ],
            )

            result = runner.invoke(app, ["project", "list"])

        assert result.exit_code == 0
        assert "prod" in result.output
        assert "Connected Projects" in result.output

    def test_project_list_human_empty(self, tmp_path: Path) -> None:
        """project list in human mode with no projects shows helpful message."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance
            MockService.return_value = ProjectService(config_store=store_instance)

            result = runner.invoke(app, ["project", "list"])

        assert result.exit_code == 0
        assert "No projects configured" in result.output


class TestProjectRemove:
    """Tests for `kbagent project remove` command."""

    def test_project_remove_success_json(self, tmp_path: Path) -> None:
        """project remove --json returns structured success."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        mock_client = make_mock_client()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            runner.invoke(
                app,
                [
                    "project",
                    "add",
                    "--alias",
                    "test",
                ],
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "remove",
                    "--alias",
                    "test",
                ],
            )

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["alias"] == "test"

    def test_project_remove_nonexistent_exit_code_5(self, tmp_path: Path) -> None:
        """project remove with nonexistent alias returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance
            MockService.return_value = ProjectService(config_store=store_instance)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "remove",
                    "--alias",
                    "nonexistent",
                ],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"


class TestProjectStatus:
    """Tests for `kbagent project status` command."""

    def test_project_status_json(self, tmp_path: Path) -> None:
        """project status --json returns connectivity info."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        mock_client = make_mock_client(project_name="Prod", project_id=123)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            runner.invoke(
                app,
                [
                    "project",
                    "add",
                    "--alias",
                    "prod",
                ],
            )

            result = runner.invoke(app, ["--json", "project", "status"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert len(output["data"]) == 1
        assert output["data"][0]["alias"] == "prod"
        assert output["data"][0]["status"] == "ok"
        assert "response_time_ms" in output["data"][0]

    def test_project_status_human(self, tmp_path: Path) -> None:
        """project status in human mode shows status table."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        mock_client = make_mock_client()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            runner.invoke(
                app,
                [
                    "project",
                    "add",
                    "--alias",
                    "test",
                ],
            )

            result = runner.invoke(app, ["project", "status"])

        assert result.exit_code == 0
        assert "Project Status" in result.output


class TestProjectEdit:
    """Tests for `kbagent project edit` command."""

    def test_project_edit_url_json(self, tmp_path: Path) -> None:
        """project edit --url with --json updates URL and returns result."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        mock_client = make_mock_client()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            runner.invoke(
                app,
                [
                    "project",
                    "add",
                    "--alias",
                    "test",
                    "--url",
                    "https://old.keboola.com",
                ],
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "edit",
                    "--alias",
                    "test",
                    "--url",
                    "https://new.keboola.com",
                ],
            )

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["stack_url"] == "https://new.keboola.com"

    def test_project_edit_config_error_exit_code_5(self, tmp_path: Path) -> None:
        """project edit with no changes returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        mock_client = make_mock_client()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            runner.invoke(
                app,
                [
                    "project",
                    "add",
                    "--alias",
                    "test",
                ],
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "edit",
                    "--alias",
                    "test",
                ],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"


# ---------------------------------------------------------------------------
# Helpers for config command tests
# ---------------------------------------------------------------------------

SAMPLE_COMPONENTS = [
    {
        "id": "keboola.ex-db-snowflake",
        "name": "Snowflake Extractor",
        "type": "extractor",
        "configurations": [
            {
                "id": "101",
                "name": "Production Load",
                "description": "Loads production data",
            },
            {
                "id": "102",
                "name": "Dev Load",
                "description": "Loads dev data",
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
            },
        ],
    },
]

SAMPLE_COMPONENTS_2 = [
    {
        "id": "keboola.python-transformation-v2",
        "name": "Python Transformation",
        "type": "transformation",
        "configurations": [
            {
                "id": "301",
                "name": "Aggregate Data",
                "description": "Aggregation script",
            },
        ],
    },
]


def _make_list_components_client(components: list[dict]) -> MagicMock:
    """Create a mock KeboolaClient with list_components returning given data."""
    mock_client = MagicMock()
    mock_client.list_components.return_value = components
    return mock_client


def _setup_config_test(config_dir: Path, projects: dict[str, dict] | None = None):
    """Set up a ConfigStore with given projects for testing config commands.

    Args:
        config_dir: Directory for config files.
        projects: Dict mapping alias to dict with 'token' and optional 'stack_url'.

    Returns:
        Configured ConfigStore instance.
    """
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


class TestConfigList:
    """Tests for `kbagent config list` command."""

    def test_config_list_json_output(self, tmp_path: Path) -> None:
        """config list --json returns structured JSON with configs from all projects."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_components_client(SAMPLE_COMPONENTS)
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
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

            result = runner.invoke(app, ["--json", "config", "list"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        configs = output["data"]["configs"]
        assert len(configs) == 3
        assert configs[0]["project_alias"] == "prod"
        assert configs[0]["component_id"] == "keboola.ex-db-snowflake"
        assert configs[0]["config_name"] == "Production Load"

    def test_config_list_human_output(self, tmp_path: Path) -> None:
        """config list in human mode shows Rich table grouped by project."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_components_client(SAMPLE_COMPONENTS)
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
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

            result = runner.invoke(app, ["config", "list"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        # Should show project-grouped table
        assert "prod" in result.output
        assert "Configurations" in result.output
        assert "Production Load" in result.output
        # Rich may truncate long component IDs, so check for prefix
        assert "keboola.ex-db-" in result.output

    def test_config_list_project_filter(self, tmp_path: Path) -> None:
        """config list --project X returns configs only from that project."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        prod_client = _make_list_components_client(SAMPLE_COMPONENTS)
        dev_client = _make_list_components_client(SAMPLE_COMPONENTS_2)

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
                "dev": {"token": "532-abcdef-ghijklmnopqrst"},
            },
        )

        def factory(url, token):
            if "901" in token:
                return prod_client
            return dev_client

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=factory,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "list",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 0
        output = json.loads(result.output)
        configs = output["data"]["configs"]
        assert len(configs) == 3
        assert all(c["project_alias"] == "prod" for c in configs)

    def test_config_list_multiple_projects(self, tmp_path: Path) -> None:
        """config list --project X --project Y returns configs from both."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        prod_client = _make_list_components_client(SAMPLE_COMPONENTS)
        dev_client = _make_list_components_client(SAMPLE_COMPONENTS_2)

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
                "dev": {"token": "532-abcdef-ghijklmnopqrst"},
            },
        )

        def factory(url, token):
            if "901" in token:
                return prod_client
            return dev_client

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=factory,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "list",
                    "--project",
                    "prod",
                    "--project",
                    "dev",
                ],
            )

        assert result.exit_code == 0
        output = json.loads(result.output)
        configs = output["data"]["configs"]
        assert len(configs) == 4  # 3 from prod + 1 from dev
        aliases = {c["project_alias"] for c in configs}
        assert aliases == {"prod", "dev"}

    def test_config_list_type_filter(self, tmp_path: Path) -> None:
        """config list --component-type extractor filters by type."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        # Client returns only extractors when type filter is applied
        extractor_only = [SAMPLE_COMPONENTS[0]]
        mock_client = _make_list_components_client(extractor_only)

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
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
                [
                    "--json",
                    "config",
                    "list",
                    "--component-type",
                    "extractor",
                ],
            )

        assert result.exit_code == 0
        output = json.loads(result.output)
        configs = output["data"]["configs"]
        assert len(configs) == 2
        assert all(c["component_type"] == "extractor" for c in configs)

    def test_config_list_component_id_filter(self, tmp_path: Path) -> None:
        """config list --component-id X filters by specific component."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_components_client(SAMPLE_COMPONENTS)
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
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
                [
                    "--json",
                    "config",
                    "list",
                    "--component-id",
                    "keboola.wr-db-snowflake",
                ],
            )

        assert result.exit_code == 0
        output = json.loads(result.output)
        configs = output["data"]["configs"]
        assert len(configs) == 1
        assert configs[0]["component_id"] == "keboola.wr-db-snowflake"

    def test_config_list_unknown_alias_exit_code_5(self, tmp_path: Path) -> None:
        """config list --project unknown returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "list",
                    "--project",
                    "nonexistent",
                ],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"
        assert "not found" in output["error"]["message"]

    def test_config_list_partial_failure_json(self, tmp_path: Path) -> None:
        """config list shows errors for failed projects while returning others."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        good_client = _make_list_components_client(SAMPLE_COMPONENTS)
        bad_client = MagicMock()
        bad_client.list_components.side_effect = KeboolaApiError(
            message="Token expired",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        store = _setup_config_test(
            config_dir,
            {
                "good": {"token": "901-good-abcdefghijklmnop"},
                "bad": {"token": "532-bad-abcdefghijklmnopq"},
            },
        )

        def factory(url, token):
            if "good" in token:
                return good_client
            return bad_client

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=factory,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(app, ["--json", "config", "list"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"

        configs = output["data"]["configs"]
        errors = output["data"]["errors"]

        assert len(configs) == 3
        assert all(c["project_alias"] == "good" for c in configs)

        assert len(errors) == 1
        assert errors[0]["project_alias"] == "bad"
        assert errors[0]["error_code"] == "INVALID_TOKEN"

    def test_config_list_partial_failure_human(self, tmp_path: Path) -> None:
        """config list in human mode shows warnings for failed projects."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        good_client = _make_list_components_client(SAMPLE_COMPONENTS)
        bad_client = MagicMock()
        bad_client.list_components.side_effect = KeboolaApiError(
            message="Token expired",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        store = _setup_config_test(
            config_dir,
            {
                "good": {"token": "901-good-abcdefghijklmnop"},
                "bad": {"token": "532-bad-abcdefghijklmnopq"},
            },
        )

        def factory(url, token):
            if "good" in token:
                return good_client
            return bad_client

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=factory,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(app, ["config", "list"])

        assert result.exit_code == 0
        # Should show configs from good project
        assert "Configurations" in result.output
        assert "Production Load" in result.output
        # Should show warning about bad project
        assert "bad" in result.output
        assert "Token expired" in result.output

    def test_config_list_empty_json(self, tmp_path: Path) -> None:
        """config list --json with no configs returns empty data."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_components_client([])
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
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

            result = runner.invoke(app, ["--json", "config", "list"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["configs"] == []
        assert output["data"]["errors"] == []

    def test_config_list_invalid_component_type_exit_code_2(self, tmp_path: Path) -> None:
        """config list with invalid --component-type returns exit code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "list",
                    "--component-type",
                    "invalid-type",
                ],
            )

        assert result.exit_code == 2
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "INVALID_ARGUMENT" in output["error"]["code"]


class TestConfigDetail:
    """Tests for `kbagent config detail` command."""

    def test_config_detail_json_output(self, tmp_path: Path) -> None:
        """config detail --json returns full config detail."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        detail_response = {
            "id": "101",
            "name": "Production Load",
            "description": "Loads production data",
            "componentId": "keboola.ex-db-snowflake",
            "configuration": {"parameters": {"db": "prod"}},
            "rows": [],
        }

        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = detail_response

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
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
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "101",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["id"] == "101"
        assert output["data"]["name"] == "Production Load"
        assert output["data"]["project_alias"] == "prod"
        assert output["data"]["configuration"] == {"parameters": {"db": "prod"}}

    def test_config_detail_human_output(self, tmp_path: Path) -> None:
        """config detail in human mode shows a Rich panel with details."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        detail_response = {
            "id": "101",
            "name": "Production Load",
            "description": "Loads production data",
            "componentId": "keboola.ex-db-snowflake",
            "configuration": {"parameters": {"db": "prod"}},
            "rows": [],
        }

        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = detail_response

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
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
                [
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "101",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "Production Load" in result.output
        assert "Configuration Detail" in result.output

    def test_config_detail_unknown_alias_exit_code_5(self, tmp_path: Path) -> None:
        """config detail with unknown alias returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "nonexistent",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "101",
                ],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"
        assert "not found" in output["error"]["message"]

    def test_config_detail_api_error_exit_code(self, tmp_path: Path) -> None:
        """config detail with API error returns appropriate exit code."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = MagicMock()
        mock_client.get_config_detail.side_effect = KeboolaApiError(
            message="Config not found",
            status_code=404,
            error_code="NOT_FOUND",
            retryable=False,
        )

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
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
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "999",
                ],
            )

        assert result.exit_code == 1
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "NOT_FOUND"

    def test_config_detail_auth_error_exit_code_3(self, tmp_path: Path) -> None:
        """config detail with auth error returns exit code 3."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = MagicMock()
        mock_client.get_config_detail.side_effect = KeboolaApiError(
            message="Invalid token",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
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
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "101",
                ],
            )

        assert result.exit_code == 3
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_TOKEN"


# ---------------------------------------------------------------------------
# Job list command tests
# ---------------------------------------------------------------------------

SAMPLE_JOBS = [
    {
        "id": 1001,
        "status": "success",
        "component": "keboola.ex-db-snowflake",
        "configId": "101",
        "createdTime": "2026-02-26T10:00:00Z",
        "durationSeconds": 45,
    },
    {
        "id": 1002,
        "status": "error",
        "component": "keboola.wr-db-snowflake",
        "configId": "201",
        "createdTime": "2026-02-26T11:00:00Z",
        "durationSeconds": 120,
    },
]


def _make_list_jobs_client(jobs: list[dict]) -> MagicMock:
    """Create a mock KeboolaClient with list_jobs returning given data."""
    mock_client = MagicMock()
    mock_client.list_jobs.return_value = jobs
    return mock_client


class TestJobList:
    """Tests for `kbagent job list` command."""

    def test_job_list_json_output(self, tmp_path: Path) -> None:
        """job list --json returns structured JSON with jobs from all projects."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_jobs_client(SAMPLE_JOBS)
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            job_service = JobService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockJobService.return_value = job_service

            result = runner.invoke(app, ["--json", "job", "list"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        jobs = output["data"]["jobs"]
        assert len(jobs) == 2
        assert jobs[0]["project_alias"] == "prod"
        assert jobs[0]["id"] == 1001
        assert jobs[0]["status"] == "success"

    def test_job_list_human_output(self, tmp_path: Path) -> None:
        """job list in human mode shows Rich table grouped by project."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_jobs_client(SAMPLE_JOBS)
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            job_service = JobService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockJobService.return_value = job_service

            result = runner.invoke(app, ["job", "list"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "Jobs" in result.output
        assert "prod" in result.output
        assert "1001" in result.output
        assert "success" in result.output

    def test_job_list_project_filter(self, tmp_path: Path) -> None:
        """job list --project X returns jobs only from that project."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_jobs_client(SAMPLE_JOBS)
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
                "dev": {"token": "532-abcdef-ghijklmnopqrst"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            job_service = JobService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockJobService.return_value = job_service

            result = runner.invoke(
                app,
                ["--json", "job", "list", "--project", "prod"],
            )

        assert result.exit_code == 0
        output = json.loads(result.output)
        jobs = output["data"]["jobs"]
        assert len(jobs) == 2
        assert all(j["project_alias"] == "prod" for j in jobs)

    def test_job_list_invalid_status_exit_code_2(self, tmp_path: Path) -> None:
        """job list --status invalid returns exit code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            MockProjService.return_value = ProjectService(config_store=MockStore.return_value)
            MockCfgService.return_value = ConfigService(config_store=MockStore.return_value)
            MockJobService.return_value = JobService(config_store=MockStore.return_value)

            result = runner.invoke(
                app,
                ["--json", "job", "list", "--status", "invalid"],
            )

        assert result.exit_code == 2
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_ARGUMENT"

    def test_job_list_invalid_limit_exit_code_2(self, tmp_path: Path) -> None:
        """job list --limit 0 returns exit code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            MockProjService.return_value = ProjectService(config_store=MockStore.return_value)
            MockCfgService.return_value = ConfigService(config_store=MockStore.return_value)
            MockJobService.return_value = JobService(config_store=MockStore.return_value)

            result = runner.invoke(
                app,
                ["--json", "job", "list", "--limit", "0"],
            )

        assert result.exit_code == 2
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_ARGUMENT"

    def test_job_list_limit_too_high_exit_code_2(self, tmp_path: Path) -> None:
        """job list --limit 501 returns exit code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            MockProjService.return_value = ProjectService(config_store=MockStore.return_value)
            MockCfgService.return_value = ConfigService(config_store=MockStore.return_value)
            MockJobService.return_value = JobService(config_store=MockStore.return_value)

            result = runner.invoke(
                app,
                ["--json", "job", "list", "--limit", "501"],
            )

        assert result.exit_code == 2

    def test_job_list_config_id_without_component_id_exit_code_2(self, tmp_path: Path) -> None:
        """job list --config-id without --component-id returns exit code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            MockProjService.return_value = ProjectService(config_store=MockStore.return_value)
            MockCfgService.return_value = ConfigService(config_store=MockStore.return_value)
            MockJobService.return_value = JobService(config_store=MockStore.return_value)

            result = runner.invoke(
                app,
                ["--json", "job", "list", "--config-id", "42"],
            )

        assert result.exit_code == 2
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "component-id" in output["error"]["message"]

    def test_job_list_unknown_project_exit_code_5(self, tmp_path: Path) -> None:
        """job list --project nonexistent returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            MockProjService.return_value = ProjectService(config_store=MockStore.return_value)
            MockCfgService.return_value = ConfigService(config_store=MockStore.return_value)
            MockJobService.return_value = JobService(config_store=MockStore.return_value)

            result = runner.invoke(
                app,
                ["--json", "job", "list", "--project", "nonexistent"],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"

    def test_job_list_empty_json(self, tmp_path: Path) -> None:
        """job list with no jobs returns empty list."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_jobs_client([])
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            job_service = JobService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockJobService.return_value = job_service

            result = runner.invoke(app, ["--json", "job", "list"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["data"]["jobs"] == []
        assert output["data"]["errors"] == []


JOB_DETAIL_RESPONSE = {
    "id": "1001",
    "status": "error",
    "component": "keboola.ex-db-snowflake",
    "config": "123",
    "mode": "run",
    "type": "standard",
    "createdTime": "2026-02-26T10:00:00Z",
    "startTime": "2026-02-26T10:00:05Z",
    "endTime": "2026-02-26T10:00:50Z",
    "durationSeconds": 45,
    "url": "https://queue.keboola.com/jobs/1001",
    "result": {"message": "Validation Error: missing field", "error": {"type": "user"}},
}


class TestJobDetail:
    """Tests for `kbagent job detail` command."""

    def test_job_detail_json_output(self, tmp_path: Path) -> None:
        """job detail --json returns structured JSON with full job data."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = MagicMock()
        mock_client.get_job_detail.return_value = dict(JOB_DETAIL_RESPONSE)

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            job_service = JobService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockJobService.return_value = job_service

            result = runner.invoke(
                app,
                ["--json", "job", "detail", "--project", "prod", "--job-id", "1001"],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["id"] == "1001"
        assert output["data"]["status"] == "error"
        assert output["data"]["project_alias"] == "prod"

    def test_job_detail_human_output(self, tmp_path: Path) -> None:
        """job detail in human mode shows Rich panel."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = MagicMock()
        mock_client.get_job_detail.return_value = dict(JOB_DETAIL_RESPONSE)

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            job_service = JobService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockJobService.return_value = job_service

            result = runner.invoke(
                app,
                ["job", "detail", "--project", "prod", "--job-id", "1001"],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "1001" in result.output
        assert "Validation Error" in result.output

    def test_job_detail_unknown_project_exit_code_5(self, tmp_path: Path) -> None:
        """job detail --project nonexistent returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            MockProjService.return_value = ProjectService(config_store=MockStore.return_value)
            MockCfgService.return_value = ConfigService(config_store=MockStore.return_value)
            MockJobService.return_value = JobService(config_store=MockStore.return_value)

            result = runner.invoke(
                app,
                ["--json", "job", "detail", "--project", "nonexistent", "--job-id", "1001"],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"

    def test_job_detail_not_found_exit_code_1(self, tmp_path: Path) -> None:
        """job detail for nonexistent job returns exit code 1."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = MagicMock()
        mock_client.get_job_detail.side_effect = KeboolaApiError(
            message="Job not found",
            status_code=404,
            error_code="NOT_FOUND",
            retryable=False,
        )

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            job_service = JobService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockJobService.return_value = job_service

            result = runner.invoke(
                app,
                ["--json", "job", "detail", "--project", "prod", "--job-id", "999999"],
            )

        assert result.exit_code == 1
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Context command tests
# ---------------------------------------------------------------------------


class TestContext:
    """Tests for `kbagent context` command."""

    def test_context_output_contains_key_phrases(self, tmp_path: Path) -> None:
        """context command output contains essential phrases for agents."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["context"])

        assert result.exit_code == 0
        assert "kbagent" in result.output
        assert "--json" in result.output
        assert "Exit Codes" in result.output
        assert "project add" in result.output
        assert "config list" in result.output
        assert "Tips for AI Agents" in result.output

    def test_context_json_output(self, tmp_path: Path) -> None:
        """context --json returns structured JSON with context text."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--json", "context"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert "context" in output["data"]
        assert "kbagent" in output["data"]["context"]
        assert "--json" in output["data"]["context"]
        assert "version" in output["data"]

    def test_context_mentions_all_commands(self, tmp_path: Path) -> None:
        """context output mentions all available commands."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["context"])

        assert result.exit_code == 0
        # All major commands should be mentioned
        assert "project add" in result.output
        assert "project list" in result.output
        assert "project remove" in result.output
        assert "project edit" in result.output
        assert "project status" in result.output
        assert "config list" in result.output
        assert "config detail" in result.output
        assert "context" in result.output
        assert "doctor" in result.output

    def test_context_mentions_exit_codes(self, tmp_path: Path) -> None:
        """context output includes exit codes table."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["context"])

        assert result.exit_code == 0
        assert "Authentication error" in result.output
        assert "Network error" in result.output
        assert "Configuration error" in result.output

    def test_context_mentions_workflows(self, tmp_path: Path) -> None:
        """context output includes common workflows."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["context"])

        assert result.exit_code == 0
        assert "Common workflow" in result.output
        assert "Environment variables" in result.output


# ---------------------------------------------------------------------------
# Doctor command tests
# ---------------------------------------------------------------------------


class TestDoctor:
    """Tests for `kbagent doctor` command."""

    def test_doctor_no_config_file(self, tmp_path: Path) -> None:
        """doctor with no config file shows warning for config and skip for parsing."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--json", "doctor"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        checks = output["data"]["checks"]
        assert len(checks) >= 3

        # Config file check should be warn (not found)
        config_check = next(c for c in checks if c["check"] == "config_file")
        assert config_check["status"] == "warn"

        # Config valid check should be skip
        valid_check = next(c for c in checks if c["check"] == "config_valid")
        assert valid_check["status"] == "skip"

        # Version check should pass
        version_check = next(c for c in checks if c["check"] == "version")
        assert version_check["status"] == "pass"

    def test_doctor_with_valid_config(self, tmp_path: Path) -> None:
        """doctor with a valid config file shows pass for file and valid checks."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = ConfigStore(config_dir=config_dir)
        store.add_project(
            "test",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
                project_name="Test",
                project_id=1234,
            ),
        )

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(app, ["--json", "doctor"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        checks = output["data"]["checks"]

        # Config file check should pass (file exists with 0600)
        config_check = next(c for c in checks if c["check"] == "config_file")
        assert config_check["status"] == "pass"

        # Config valid check should pass
        valid_check = next(c for c in checks if c["check"] == "config_valid")
        assert valid_check["status"] == "pass"
        assert "1 project" in valid_check["message"]

    def test_doctor_json_structure(self, tmp_path: Path) -> None:
        """doctor --json returns proper structure with checks and summary."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--json", "doctor"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"

        data = output["data"]
        assert "checks" in data
        assert "summary" in data
        assert "total" in data["summary"]
        assert "passed" in data["summary"]
        assert "failed" in data["summary"]
        assert "warnings" in data["summary"]
        assert "healthy" in data["summary"]

    def test_doctor_human_output(self, tmp_path: Path) -> None:
        """doctor in human mode shows a Rich panel with check results."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["doctor"])

        assert result.exit_code == 0
        assert "kbagent doctor" in result.output
        assert "WARN" in result.output or "PASS" in result.output or "SKIP" in result.output

    def test_doctor_connectivity_with_mock_client(self, tmp_path: Path) -> None:
        """doctor checks connectivity to projects using the client factory."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = ConfigStore(config_dir=config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
                project_name="Prod",
                project_id=1234,
            ),
        )

        mock_client = make_mock_client(project_name="Prod", project_id=1234)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.services.doctor_service.default_client_factory") as MockFactory,
        ):
            MockStore.return_value = store
            MockFactory.return_value = mock_client

            result = runner.invoke(app, ["--json", "doctor"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        checks = output["data"]["checks"]

        connectivity_checks = [c for c in checks if c["check"] == "connectivity"]
        assert len(connectivity_checks) == 1
        assert connectivity_checks[0]["status"] == "pass"
        assert "Prod" in connectivity_checks[0]["message"]

    def test_doctor_connectivity_failure(self, tmp_path: Path) -> None:
        """doctor shows fail for projects with connectivity issues."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = ConfigStore(config_dir=config_dir)
        store.add_project(
            "bad",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-badtoken-abcdefghijklmn",
                project_name="Bad",
                project_id=9999,
            ),
        )

        fail_client = MagicMock()
        fail_client.verify_token.side_effect = KeboolaApiError(
            message="Invalid token",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.services.doctor_service.default_client_factory") as MockFactory,
        ):
            MockStore.return_value = store
            MockFactory.return_value = fail_client

            result = runner.invoke(app, ["--json", "doctor"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        checks = output["data"]["checks"]

        connectivity_checks = [c for c in checks if c["check"] == "connectivity"]
        assert len(connectivity_checks) == 1
        assert connectivity_checks[0]["status"] == "fail"
        assert "Invalid token" in connectivity_checks[0]["message"]

    def test_doctor_version_check(self, tmp_path: Path) -> None:
        """doctor always includes a version check that passes."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--json", "doctor"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        checks = output["data"]["checks"]

        version_check = next(c for c in checks if c["check"] == "version")
        assert version_check["status"] == "pass"
        assert "kbagent v" in version_check["message"]

    def test_doctor_invalid_json_config(self, tmp_path: Path) -> None:
        """doctor reports fail when config file contains invalid JSON."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        config_path = config_dir / "config.json"
        config_path.write_text("not valid json {{{", encoding="utf-8")
        config_path.chmod(0o600)

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--json", "doctor"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        checks = output["data"]["checks"]

        valid_check = next(c for c in checks if c["check"] == "config_valid")
        assert valid_check["status"] == "fail"
        assert "not valid JSON" in valid_check["message"]


# ---------------------------------------------------------------------------
# --no-color flag tests
# ---------------------------------------------------------------------------


class TestNoColor:
    """Tests for --no-color global flag."""

    def test_no_color_flag_accepted(self, tmp_path: Path) -> None:
        """--no-color flag is accepted without error."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--no-color", "context"])

        assert result.exit_code == 0
        assert "kbagent" in result.output

    def test_no_color_project_list(self, tmp_path: Path) -> None:
        """--no-color works with project list command."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance
            MockService.return_value = ProjectService(config_store=store_instance)

            result = runner.invoke(app, ["--no-color", "project", "list"])

        assert result.exit_code == 0

    def test_no_color_doctor(self, tmp_path: Path) -> None:
        """--no-color works with doctor command."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--no-color", "doctor"])

        assert result.exit_code == 0
        assert "kbagent doctor" in result.output


# ---------------------------------------------------------------------------
# Exit code tests
# ---------------------------------------------------------------------------


class TestExitCodes:
    """Tests for consistent exit codes across commands."""

    def test_auth_error_exit_code_3(self, tmp_path: Path) -> None:
        """Authentication error returns exit code 3."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        fail_client = MagicMock()
        fail_client.verify_token.side_effect = KeboolaApiError(
            message="Invalid or expired token",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": "invalid-token-abcdefgh"}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance
            MockService.return_value = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: fail_client,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--alias",
                    "bad",
                ],
            )

        assert result.exit_code == 3
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_TOKEN"

    def test_network_error_exit_code_4(self, tmp_path: Path) -> None:
        """Network error returns exit code 4."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        fail_client = MagicMock()
        fail_client.verify_token.side_effect = KeboolaApiError(
            message="Connection refused",
            status_code=0,
            error_code="CONNECTION_ERROR",
            retryable=True,
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance
            MockService.return_value = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: fail_client,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--alias",
                    "unreachable",
                ],
            )

        assert result.exit_code == 4
        output = json.loads(result.output)
        assert output["status"] == "error"

    def test_config_error_exit_code_5(self, tmp_path: Path) -> None:
        """Configuration error returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance
            MockService.return_value = ProjectService(config_store=store_instance)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "remove",
                    "--alias",
                    "nonexistent",
                ],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"

    def test_config_error_exit_code_5_config_detail(self, tmp_path: Path) -> None:
        """Configuration error on config detail returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "nonexistent",
                    "--component-id",
                    "test",
                    "--config-id",
                    "123",
                ],
            )

        assert result.exit_code == 5

    def test_auth_error_exit_code_3_config_detail(self, tmp_path: Path) -> None:
        """Auth error on config detail returns exit code 3."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = MagicMock()
        mock_client.get_config_detail.side_effect = KeboolaApiError(
            message="Invalid token",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
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
                client_factory=lambda url, token: mock_client,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--component-id",
                    "test",
                    "--config-id",
                    "123",
                ],
            )

        assert result.exit_code == 3

    def test_network_error_exit_code_4_config_detail(self, tmp_path: Path) -> None:
        """Network error on config detail returns exit code 4."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = MagicMock()
        mock_client.get_config_detail.side_effect = KeboolaApiError(
            message="Request timed out",
            status_code=0,
            error_code="TIMEOUT",
            retryable=True,
        )

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
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
                client_factory=lambda url, token: mock_client,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--component-id",
                    "test",
                    "--config-id",
                    "123",
                ],
            )

        assert result.exit_code == 4


# ---------------------------------------------------------------------------
# Help and usage tests
# ---------------------------------------------------------------------------


class TestHelp:
    """Tests for help output on all commands."""

    def test_root_help(self) -> None:
        """Root --help shows app description and command groups."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "project" in result.output
        assert "config" in result.output
        assert "job" in result.output
        assert "context" in result.output
        assert "doctor" in result.output

    def test_project_help(self) -> None:
        """project --help shows subcommands."""
        result = runner.invoke(app, ["project", "--help"])
        assert result.exit_code == 0
        assert "add" in result.output
        assert "list" in result.output
        assert "remove" in result.output
        assert "edit" in result.output
        assert "status" in result.output

    def test_config_help(self) -> None:
        """config --help shows subcommands."""
        result = runner.invoke(app, ["config", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "detail" in result.output

    def test_project_add_help(self) -> None:
        """project add --help shows options but NOT --token (security)."""
        result = runner.invoke(app, ["project", "add", "--help"])
        assert result.exit_code == 0
        assert "--alias" in result.output
        assert "--url" in result.output
        # --token must NOT appear as a CLI option (S1 security fix)
        assert "--token" not in result.output

    def test_config_list_help(self) -> None:
        """config list --help shows options."""
        result = runner.invoke(app, ["config", "list", "--help"])
        assert result.exit_code == 0
        assert "--project" in result.output
        assert "--component-type" in result.output
        assert "--component-id" in result.output

    def test_config_detail_help(self) -> None:
        """config detail --help shows required options."""
        result = runner.invoke(app, ["config", "detail", "--help"])
        assert result.exit_code == 0
        assert "--project" in result.output
        assert "--component-id" in result.output
        assert "--config-id" in result.output

    def test_job_help(self) -> None:
        """job --help shows subcommands."""
        result = runner.invoke(app, ["job", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "detail" in result.output

    def test_job_list_help(self) -> None:
        """job list --help shows options."""
        result = runner.invoke(app, ["job", "list", "--help"])
        assert result.exit_code == 0
        assert "--project" in result.output
        assert "--component-id" in result.output
        assert "--config-id" in result.output
        assert "--status" in result.output
        assert "--limit" in result.output


class TestVerboseFlag:
    """Tests for --verbose global flag."""

    def test_verbose_flag_accepted(self, tmp_path: Path) -> None:
        """--verbose flag is accepted without error."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--verbose", "context"])

        assert result.exit_code == 0

    def test_verbose_with_json(self, tmp_path: Path) -> None:
        """--verbose and --json can be used together."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--verbose", "--json", "context"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"


class TestMissingRequiredArgs:
    """Tests for missing required arguments."""

    def test_project_add_missing_alias(self, tmp_path: Path) -> None:
        """project add without --alias shows error."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            MockService.return_value = ProjectService(config_store=MockStore.return_value)

            result = runner.invoke(
                app,
                [
                    "project",
                    "add",
                ],
            )

        assert result.exit_code != 0

    def test_project_add_missing_token_non_tty(self, tmp_path: Path) -> None:
        """project add without KBC_TOKEN in non-TTY exits with code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {}, clear=False),
        ):
            # Ensure KBC_TOKEN is not set
            os.environ.pop("KBC_TOKEN", None)
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            MockService.return_value = ProjectService(config_store=MockStore.return_value)

            result = runner.invoke(
                app,
                [
                    "project",
                    "add",
                    "--alias",
                    "test",
                ],
            )

        assert result.exit_code != 0

    def test_project_remove_missing_alias(self, tmp_path: Path) -> None:
        """project remove without --alias shows error."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            MockService.return_value = ProjectService(config_store=MockStore.return_value)

            result = runner.invoke(app, ["project", "remove"])

        assert result.exit_code != 0

    def test_config_detail_missing_project(self, tmp_path: Path) -> None:
        """config detail without --project shows error."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            MockProjService.return_value = ProjectService(config_store=MockStore.return_value)
            MockCfgService.return_value = ConfigService(config_store=MockStore.return_value)

            result = runner.invoke(
                app,
                [
                    "config",
                    "detail",
                    "--component-id",
                    "test",
                    "--config-id",
                    "123",
                ],
            )

        assert result.exit_code != 0


class TestProjectEditTokenReverify:
    """Tests for project edit with token changes."""

    def test_project_edit_token_reverify_json(self, tmp_path: Path) -> None:
        """project edit --new-token triggers re-verification and returns updated info."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        mock_client = make_mock_client(project_name="Original", project_id=100)
        new_mock_client = make_mock_client(project_name="Updated", project_id=200)

        call_count = 0

        def client_factory(url, token):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_client
            return new_mock_client

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=client_factory,
            )
            MockService.return_value = service_instance

            # Add project first
            runner.invoke(
                app,
                [
                    "project",
                    "add",
                    "--alias",
                    "test",
                ],
            )

            # Edit with new token via --new-token flag + env var
            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "edit",
                    "--alias",
                    "test",
                    "--new-token",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"


class TestProjectAddTokenSecurity:
    """Tests for S1: Token input security (env var and interactive prompt)."""

    def test_project_add_token_from_env(self, tmp_path: Path) -> None:
        """Token from KBC_TOKEN env var works for project add."""
        mock_client = make_mock_client(project_name="EnvProject", project_id=999)
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--alias",
                    "envtest",
                    "--url",
                    "https://connection.keboola.com",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["alias"] == "envtest"
        assert output["data"]["project_name"] == "EnvProject"

    def test_project_add_token_interactive(self, tmp_path: Path) -> None:
        """Interactive hidden prompt works for project add when no env var.

        We mock _resolve_token to simulate the interactive prompt returning a token,
        since CliRunner does not have a real TTY and sys.stdin.isatty() is False.
        """
        mock_client = make_mock_client(project_name="PromptProject", project_id=888)
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch(
                "keboola_agent_cli.commands.project._resolve_token",
                return_value=TEST_TOKEN,
            ),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--alias",
                    "prompttest",
                    "--url",
                    "https://connection.keboola.com",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["alias"] == "prompttest"

    def test_project_add_rejects_http_url(self, tmp_path: Path) -> None:
        """http:// URL rejected with error at project add time."""
        mock_client = make_mock_client()
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--alias",
                    "insecure",
                    "--url",
                    "http://connection.keboola.com",
                ],
            )

        assert result.exit_code != 0, f"Expected failure but got: {result.output}"

    def test_project_add_rejects_file_url(self, tmp_path: Path) -> None:
        """file:// URL rejected with error at project add time."""
        mock_client = make_mock_client()
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--alias",
                    "fileurl",
                    "--url",
                    "file:///etc/passwd",
                ],
            )

        assert result.exit_code != 0, f"Expected failure but got: {result.output}"

    def test_project_add_accepts_https_url(self, tmp_path: Path) -> None:
        """https:// URL is accepted at project add time."""
        mock_client = make_mock_client(project_name="SecureProject", project_id=777)
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--alias",
                    "secure",
                    "--url",
                    "https://connection.keboola.com",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["alias"] == "secure"


# ---------------------------------------------------------------------------
# Helpers for tool (MCP) command tests
# ---------------------------------------------------------------------------

SAMPLE_TOOLS = [
    {
        "name": "list_configs",
        "description": "List configurations",
        "inputSchema": {},
        "multi_project": True,
    },
    {
        "name": "get_config",
        "description": "Get configuration detail",
        "inputSchema": {"type": "object"},
        "multi_project": True,
    },
    {
        "name": "create_config",
        "description": "Create a new configuration",
        "inputSchema": {"type": "object"},
        "multi_project": False,
    },
]

SAMPLE_TOOL_RESULT = {
    "results": [
        {
            "content": [{"name": "test-config"}],
            "isError": False,
            "project_alias": "prod",
        },
    ],
    "errors": [],
}

SAMPLE_TOOL_RESULT_MULTI = {
    "results": [
        {
            "content": [{"name": "config-a"}],
            "isError": False,
            "project_alias": "prod",
        },
        {
            "content": [{"name": "config-b"}],
            "isError": False,
            "project_alias": "dev",
        },
    ],
    "errors": [],
}


class TestToolList:
    """Tests for `kbagent tool list` command."""

    def test_tool_list_json_output(self, tmp_path: Path) -> None:
        """tool list --json returns structured JSON with tools."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.McpService") as MockMcpService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            mock_mcp = MagicMock()
            mock_mcp.list_tools.return_value = {
                "tools": SAMPLE_TOOLS,
                "errors": [],
            }
            MockMcpService.return_value = mock_mcp

            result = runner.invoke(app, ["--json", "tool", "list"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        tools = output["data"]["tools"]
        assert len(tools) == 3
        assert tools[0]["name"] == "list_configs"
        assert tools[0]["multi_project"] is True
        assert tools[2]["name"] == "create_config"
        assert tools[2]["multi_project"] is False

    def test_tool_list_human_output(self, tmp_path: Path) -> None:
        """tool list in human mode shows Rich table with tool names."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.McpService") as MockMcpService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            mock_mcp = MagicMock()
            mock_mcp.list_tools.return_value = {
                "tools": SAMPLE_TOOLS,
                "errors": [],
            }
            MockMcpService.return_value = mock_mcp

            result = runner.invoke(app, ["tool", "list"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "MCP Tools" in result.output
        assert "list_configs" in result.output
        assert "get_config" in result.output
        assert "create_config" in result.output

    def test_tool_list_no_projects(self, tmp_path: Path) -> None:
        """tool list with no configured projects returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.McpService") as MockMcpService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            mock_mcp = MagicMock()
            mock_mcp.list_tools.side_effect = ConfigError(
                "No projects configured. Use 'kbagent project add' first."
            )
            MockMcpService.return_value = mock_mcp

            result = runner.invoke(app, ["--json", "tool", "list"])

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"
        assert "No projects configured" in output["error"]["message"]

    def test_tool_list_with_errors(self, tmp_path: Path) -> None:
        """tool list returns tools along with errors for failed projects."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
                "dev": {"token": "532-abcdef-ghijklmnopqrst"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.McpService") as MockMcpService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            mock_mcp = MagicMock()
            mock_mcp.list_tools.return_value = {
                "tools": SAMPLE_TOOLS,
                "errors": [
                    {
                        "project_alias": "dev",
                        "error_code": "MCP_ERROR",
                        "message": "Failed to list tools: Connection refused",
                    },
                ],
            }
            MockMcpService.return_value = mock_mcp

            result = runner.invoke(app, ["--json", "tool", "list"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert len(output["data"]["tools"]) == 3
        assert len(output["data"]["errors"]) == 1
        assert output["data"]["errors"][0]["project_alias"] == "dev"
        assert "Connection refused" in output["data"]["errors"][0]["message"]


class TestToolCall:
    """Tests for `kbagent tool call` command."""

    def test_tool_call_read_json(self, tmp_path: Path) -> None:
        """tool call for a read tool returns multi-project results in JSON."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
                "dev": {"token": "532-abcdef-ghijklmnopqrst"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.McpService") as MockMcpService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            mock_mcp = MagicMock()
            mock_mcp.validate_tool_input.return_value = []
            mock_mcp.call_tool.return_value = SAMPLE_TOOL_RESULT_MULTI
            MockMcpService.return_value = mock_mcp

            result = runner.invoke(
                app,
                ["--json", "tool", "call", "list_configs"],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        results = output["data"]["results"]
        assert len(results) == 2
        assert results[0]["project_alias"] == "prod"
        assert results[1]["project_alias"] == "dev"
        assert results[0]["isError"] is False
        mock_mcp.call_tool.assert_called_once_with(
            tool_name="list_configs",
            tool_input={},
            alias=None,
        )

    def test_tool_call_write_json(self, tmp_path: Path) -> None:
        """tool call for a write tool with --project returns single result."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.McpService") as MockMcpService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            mock_mcp = MagicMock()
            mock_mcp.validate_tool_input.return_value = []
            mock_mcp.call_tool.return_value = SAMPLE_TOOL_RESULT
            MockMcpService.return_value = mock_mcp

            result = runner.invoke(
                app,
                [
                    "--json",
                    "tool",
                    "call",
                    "create_config",
                    "--project",
                    "prod",
                    "--input",
                    '{"name": "New Config", "component_id": "keboola.ex-db-snowflake"}',
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        results = output["data"]["results"]
        assert len(results) == 1
        assert results[0]["project_alias"] == "prod"
        assert results[0]["isError"] is False
        mock_mcp.call_tool.assert_called_once_with(
            tool_name="create_config",
            tool_input={"name": "New Config", "component_id": "keboola.ex-db-snowflake"},
            alias="prod",
        )

    def test_tool_call_invalid_input(self, tmp_path: Path) -> None:
        """tool call with invalid JSON in --input returns exit code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.McpService") as MockMcpService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            mock_mcp = MagicMock()
            MockMcpService.return_value = mock_mcp

            result = runner.invoke(
                app,
                [
                    "--json",
                    "tool",
                    "call",
                    "list_configs",
                    "--input",
                    "not valid json{{{",
                ],
            )

        assert result.exit_code == 2
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_ARGUMENT"
        assert "Invalid JSON" in output["error"]["message"]
        mock_mcp.call_tool.assert_not_called()

    def test_tool_call_config_error(self, tmp_path: Path) -> None:
        """tool call when no projects configured returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.McpService") as MockMcpService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            mock_mcp = MagicMock()
            mock_mcp.validate_tool_input.side_effect = ConfigError(
                "No projects configured. Use 'kbagent project add' first."
            )
            MockMcpService.return_value = mock_mcp

            result = runner.invoke(
                app,
                ["--json", "tool", "call", "list_configs"],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"
        assert "No projects configured" in output["error"]["message"]

    def test_tool_call_human_output(self, tmp_path: Path) -> None:
        """tool call in human mode shows Rich panel with results."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.McpService") as MockMcpService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            mock_mcp = MagicMock()
            mock_mcp.validate_tool_input.return_value = []
            mock_mcp.call_tool.return_value = SAMPLE_TOOL_RESULT
            MockMcpService.return_value = mock_mcp

            result = runner.invoke(
                app,
                ["tool", "call", "list_configs"],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "prod" in result.output
        assert "OK" in result.output or "test-config" in result.output


# ---------------------------------------------------------------------------
# Lineage command tests
# ---------------------------------------------------------------------------

SAMPLE_BUCKETS_SHARED = [
    {
        "id": "in.c-shared-data",
        "name": "Shared Data",
        "sharing": "organization-project",
        "linkedBy": [
            {
                "id": "in.c-linked-data",
                "project": {"id": 7012, "name": "Target Project"},
            }
        ],
    },
    {
        "id": "out.c-normal",
        "name": "Normal Bucket",
    },
]

SAMPLE_BUCKETS_EMPTY = [
    {"id": "in.c-data", "name": "Data"},
]


def _make_list_buckets_client(buckets: list[dict]) -> MagicMock:
    """Create a mock KeboolaClient with list_buckets returning given data."""
    mock_client = MagicMock()
    mock_client.list_buckets.return_value = buckets
    return mock_client


class TestLineageShow:
    """Tests for `kbagent lineage show` command."""

    def test_lineage_json_output(self, tmp_path: Path) -> None:
        """lineage show --json returns structured JSON with edges and summary."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_buckets_client(SAMPLE_BUCKETS_SHARED)
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.LineageService") as MockLineageService,
            patch("keboola_agent_cli.cli.McpService") as MockMcpService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockMcpService.return_value = MagicMock()

            lineage_service = LineageService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockLineageService.return_value = lineage_service

            result = runner.invoke(app, ["--json", "lineage", "show"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        data = output["data"]
        assert "edges" in data
        assert len(data["edges"]) >= 1
        assert "summary" in data

    def test_lineage_human_output(self, tmp_path: Path) -> None:
        """lineage show in human mode displays a Rich table with edge data."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_buckets_client(SAMPLE_BUCKETS_SHARED)
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.LineageService") as MockLineageService,
            patch("keboola_agent_cli.cli.McpService") as MockMcpService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockMcpService.return_value = MagicMock()

            lineage_service = LineageService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockLineageService.return_value = lineage_service

            result = runner.invoke(app, ["lineage", "show"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "Data Flow Edges" in result.output
        # Rich table may truncate long bucket ids; check for prefix or project alias
        assert "in.c-shared" in result.output or "prod" in result.output

    def test_lineage_no_sharing(self, tmp_path: Path) -> None:
        """lineage show with no shared buckets returns empty edges and zero counts."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_buckets_client(SAMPLE_BUCKETS_EMPTY)
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.LineageService") as MockLineageService,
            patch("keboola_agent_cli.cli.McpService") as MockMcpService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockMcpService.return_value = MagicMock()

            lineage_service = LineageService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockLineageService.return_value = lineage_service

            result = runner.invoke(app, ["--json", "lineage", "show"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        data = output["data"]
        assert data["edges"] == []
        assert data["summary"]["total_edges"] == 0

    def test_lineage_project_filter(self, tmp_path: Path) -> None:
        """lineage show --project filters to specific project alias."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_buckets_client(SAMPLE_BUCKETS_SHARED)
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
                "dev": {"token": "532-abcdef-ghijklmnopqrst"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.LineageService") as MockLineageService,
            patch("keboola_agent_cli.cli.McpService") as MockMcpService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockMcpService.return_value = MagicMock()

            lineage_service = LineageService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockLineageService.return_value = lineage_service

            result = runner.invoke(
                app,
                ["--json", "lineage", "show", "--project", "prod"],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"

    def test_lineage_config_error(self, tmp_path: Path) -> None:
        """lineage show --project nonexistent returns exit code 5 with CONFIG_ERROR."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.LineageService") as MockLineageService,
            patch("keboola_agent_cli.cli.McpService") as MockMcpService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockMcpService.return_value = MagicMock()

            lineage_service = LineageService(
                config_store=store,
            )
            MockLineageService.return_value = lineage_service

            result = runner.invoke(
                app,
                ["--json", "lineage", "show", "--project", "nonexistent"],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"

    def test_lineage_default_subcommand(self, tmp_path: Path) -> None:
        """lineage without 'show' subcommand defaults to lineage show."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_buckets_client(SAMPLE_BUCKETS_SHARED)
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.LineageService") as MockLineageService,
            patch("keboola_agent_cli.cli.McpService") as MockMcpService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockMcpService.return_value = MagicMock()

            lineage_service = LineageService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockLineageService.return_value = lineage_service

            result = runner.invoke(app, ["--json", "lineage"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert "edges" in output["data"]


class TestOrgSetup:
    """Tests for `kbagent org setup` command."""

    def _make_org_service_mock(self, result: dict) -> MagicMock:
        """Create a mock OrgService that returns the given result."""
        mock_service = MagicMock()
        mock_service.setup_organization.return_value = result
        return mock_service

    def test_dry_run_json_output(self, tmp_path: Path) -> None:
        """org setup --dry-run with --json outputs structured preview."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        result_data = {
            "organization_id": 123,
            "stack_url": "https://connection.keboola.com",
            "projects_found": 2,
            "projects_added": [
                {"project_id": 100, "project_name": "Alpha", "alias": "alpha", "action": "would_add"},
                {"project_id": 200, "project_name": "Beta", "alias": "beta", "action": "would_add"},
            ],
            "projects_skipped": [],
            "projects_failed": [],
            "dry_run": True,
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch("keboola_agent_cli.commands.org._resolve_manage_token", return_value="manage-token-123456789012345678"),
        ):
            MockStore.return_value = store
            MockOrgService.return_value = self._make_org_service_mock(result_data)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "org",
                    "setup",
                    "--org-id", "123",
                    "--url", "https://connection.keboola.com",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["dry_run"] is True
        assert output["data"]["projects_found"] == 2
        assert len(output["data"]["projects_added"]) == 2
        assert output["data"]["projects_added"][0]["action"] == "would_add"

    def test_success_json_output(self, tmp_path: Path) -> None:
        """org setup with --json outputs structured success with added projects."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        result_data = {
            "organization_id": 123,
            "stack_url": "https://connection.keboola.com",
            "projects_found": 2,
            "projects_added": [
                {"project_id": 100, "project_name": "Alpha", "alias": "alpha", "token": "901-...ab", "action": "added"},
                {"project_id": 200, "project_name": "Beta", "alias": "beta", "token": "901-...cd", "action": "added"},
            ],
            "projects_skipped": [],
            "projects_failed": [],
            "dry_run": False,
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch("keboola_agent_cli.commands.org._resolve_manage_token", return_value="manage-token-123456789012345678"),
        ):
            MockStore.return_value = store
            MockOrgService.return_value = self._make_org_service_mock(result_data)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "org",
                    "setup",
                    "--org-id", "123",
                    "--url", "https://connection.keboola.com",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["dry_run"] is False
        assert len(output["data"]["projects_added"]) == 2
        assert output["data"]["projects_added"][0]["action"] == "added"

    def test_skip_existing_projects(self, tmp_path: Path) -> None:
        """org setup with already registered projects shows them as skipped."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        result_data = {
            "organization_id": 123,
            "stack_url": "https://connection.keboola.com",
            "projects_found": 2,
            "projects_added": [
                {"project_id": 200, "project_name": "Beta", "alias": "beta", "token": "901-...cd", "action": "added"},
            ],
            "projects_skipped": [
                {"project_id": 100, "project_name": "Alpha", "reason": "Already registered in config"},
            ],
            "projects_failed": [],
            "dry_run": False,
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch("keboola_agent_cli.commands.org._resolve_manage_token", return_value="manage-token-123456789012345678"),
        ):
            MockStore.return_value = store
            MockOrgService.return_value = self._make_org_service_mock(result_data)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "org",
                    "setup",
                    "--org-id", "123",
                    "--url", "https://connection.keboola.com",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert len(output["data"]["projects_skipped"]) == 1
        assert output["data"]["projects_skipped"][0]["project_id"] == 100

    def test_auth_error_exit_3(self, tmp_path: Path) -> None:
        """org setup with invalid manage token exits with code 3."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        mock_service = MagicMock()
        mock_service.setup_organization.side_effect = KeboolaApiError(
            message="Invalid or expired manage token",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch("keboola_agent_cli.commands.org._resolve_manage_token", return_value="manage-token-123456789012345678"),
        ):
            MockStore.return_value = store
            MockOrgService.return_value = mock_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "org",
                    "setup",
                    "--org-id", "123",
                    "--url", "https://connection.keboola.com",
                    "--yes",
                ],
            )

        assert result.exit_code == 3, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_TOKEN"

    def test_missing_org_id_exit_2(self) -> None:
        """org setup without --org-id exits with code 2."""
        result = runner.invoke(
            app,
            [
                "--json",
                "org",
                "setup",
                "--url", "https://connection.keboola.com",
            ],
        )

        assert result.exit_code == 2


class TestVerboseFlag:
    """Tests for --verbose flag enabling DEBUG logging."""

    def test_verbose_enables_debug_logging(self, tmp_path: Path) -> None:
        """--verbose sets logging level to DEBUG, output goes to stderr."""
        import logging

        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.logging.basicConfig") as mock_basic_config,
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance
            MockProjService.return_value = ProjectService(config_store=store_instance)

            result = runner.invoke(
                app,
                ["--json", "--verbose", "project", "list"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        # Verify logging.basicConfig was called with DEBUG level
        mock_basic_config.assert_called_once()
        call_kwargs = mock_basic_config.call_args
        assert call_kwargs[1]["level"] == logging.DEBUG

    def test_default_log_level_is_warning(self, tmp_path: Path) -> None:
        """Without --verbose, logging level defaults to WARNING (no debug noise)."""
        import logging

        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.logging.basicConfig") as mock_basic_config,
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance
            MockProjService.return_value = ProjectService(config_store=store_instance)

            result = runner.invoke(
                app,
                ["--json", "project", "list"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        # Verify logging.basicConfig was called with WARNING level
        mock_basic_config.assert_called_once()
        call_kwargs = mock_basic_config.call_args
        assert call_kwargs[1]["level"] == logging.WARNING


# ---------------------------------------------------------------------------
# Lineage command tests
# ---------------------------------------------------------------------------


SAMPLE_LINEAGE_RESULT = {
    "edges": [
        {
            "source_project_alias": "prod",
            "source_project_id": 258,
            "source_project_name": "Production",
            "source_bucket_id": "in.c-shared-data",
            "source_bucket_name": "shared-data",
            "sharing_type": "organization-project",
            "target_project_alias": "dev",
            "target_project_id": 7012,
            "target_project_name": "Development",
            "target_bucket_id": "in.c-linked",
        },
    ],
    "shared_buckets": [
        {
            "project_alias": "prod",
            "project_id": 258,
            "project_name": "Production",
            "bucket_id": "in.c-shared-data",
            "bucket_name": "shared-data",
            "sharing_type": "organization-project",
            "shared_by": {},
        },
    ],
    "linked_buckets": [],
    "summary": {
        "total_shared_buckets": 1,
        "total_linked_buckets": 0,
        "total_edges": 1,
        "projects_queried": 2,
        "projects_with_errors": 0,
    },
    "errors": [],
}


class TestLineageShow:
    """Tests for `kbagent lineage` and `kbagent lineage show` commands."""

    def test_lineage_show_json(self, tmp_path: Path) -> None:
        """lineage show --json returns structured JSON with lineage data."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.LineageService") as MockLineageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            mock_service = MagicMock()
            mock_service.get_lineage.return_value = SAMPLE_LINEAGE_RESULT
            MockLineageService.return_value = mock_service

            result = runner.invoke(app, ["--json", "lineage", "show"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert len(output["data"]["edges"]) == 1
        assert output["data"]["edges"][0]["source_project_alias"] == "prod"
        assert output["data"]["edges"][0]["target_project_alias"] == "dev"
        assert output["data"]["summary"]["total_edges"] == 1
        assert output["data"]["summary"]["projects_queried"] == 2

    def test_lineage_show_human(self, tmp_path: Path) -> None:
        """lineage show in human mode shows Rich table with data flow edges."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.LineageService") as MockLineageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            mock_service = MagicMock()
            mock_service.get_lineage.return_value = SAMPLE_LINEAGE_RESULT
            MockLineageService.return_value = mock_service

            result = runner.invoke(app, ["lineage", "show"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        # Verify human output contains key information
        assert "Data Flow" in result.output or "shared" in result.output
        # Rich may truncate long strings in table columns, so check prefixes
        assert "in.c-shared" in result.output
        assert "organization" in result.output

    def test_lineage_default_subcommand(self, tmp_path: Path) -> None:
        """kbagent lineage (without 'show') invokes the show subcommand."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.LineageService") as MockLineageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            mock_service = MagicMock()
            mock_service.get_lineage.return_value = SAMPLE_LINEAGE_RESULT
            MockLineageService.return_value = mock_service

            result = runner.invoke(app, ["--json", "lineage"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert "edges" in output["data"]
        assert len(output["data"]["edges"]) == 1

    def test_lineage_config_error_exit_code_5(self, tmp_path: Path) -> None:
        """lineage with nonexistent project alias returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.LineageService") as MockLineageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            mock_service = MagicMock()
            mock_service.get_lineage.side_effect = ConfigError("Project 'nonexistent' not found")
            MockLineageService.return_value = mock_service

            result = runner.invoke(
                app,
                ["--json", "lineage", "show", "--project", "nonexistent"],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"

    def test_lineage_show_with_warnings(self, tmp_path: Path) -> None:
        """lineage show in human mode displays per-project warnings."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        result_with_errors = {
            "edges": [],
            "shared_buckets": [],
            "linked_buckets": [],
            "summary": {
                "total_shared_buckets": 0,
                "total_linked_buckets": 0,
                "total_edges": 0,
                "projects_queried": 2,
                "projects_with_errors": 1,
            },
            "errors": [
                {
                    "project_alias": "bad",
                    "error_code": "INVALID_TOKEN",
                    "message": "Token expired",
                },
            ],
        }

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.LineageService") as MockLineageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            mock_service = MagicMock()
            mock_service.get_lineage.return_value = result_with_errors
            MockLineageService.return_value = mock_service

            result = runner.invoke(app, ["lineage", "show"])

        assert result.exit_code == 0
        assert "bad" in result.output
        assert "Token expired" in result.output


# ---------------------------------------------------------------------------
# Org setup command tests
# ---------------------------------------------------------------------------


SAMPLE_ORG_SETUP_RESULT = {
    "organization_id": 42,
    "stack_url": "https://connection.keboola.com",
    "projects_found": 2,
    "projects_added": [
        {
            "project_id": 100,
            "project_name": "New Project",
            "alias": "new-project",
            "token": "100-***",
            "action": "added",
        },
    ],
    "projects_skipped": [
        {
            "project_id": 200,
            "project_name": "Existing Project",
            "reason": "Already registered in config",
        },
    ],
    "projects_failed": [],
    "dry_run": False,
}

SAMPLE_ORG_DRY_RUN_RESULT = {
    "organization_id": 42,
    "stack_url": "https://connection.keboola.com",
    "projects_found": 2,
    "projects_added": [
        {
            "project_id": 100,
            "project_name": "New Project",
            "alias": "new-project",
            "action": "would_add",
        },
    ],
    "projects_skipped": [
        {
            "project_id": 200,
            "project_name": "Existing Project",
            "reason": "Already registered in config",
        },
    ],
    "projects_failed": [],
    "dry_run": True,
}


class TestOrgSetup:
    """Tests for `kbagent org setup` command."""

    def test_org_setup_dry_run(self, tmp_path: Path) -> None:
        """org setup --dry-run returns structured preview without changes."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch(
                "keboola_agent_cli.commands.org._resolve_manage_token",
                return_value="manage-token-abcdef",
            ),
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            mock_service = MagicMock()
            mock_service.setup_organization.return_value = SAMPLE_ORG_DRY_RUN_RESULT
            MockOrgService.return_value = mock_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "org",
                    "setup",
                    "--org-id",
                    "42",
                    "--url",
                    "https://connection.keboola.com",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["dry_run"] is True
        assert len(output["data"]["projects_added"]) == 1
        assert output["data"]["projects_added"][0]["action"] == "would_add"

    def test_org_setup_with_env_token(self, tmp_path: Path) -> None:
        """org setup uses KBC_MANAGE_API_TOKEN env var for authentication."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch.dict(os.environ, {"KBC_MANAGE_API_TOKEN": "manage-test-token"}),
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            mock_service = MagicMock()
            mock_service.setup_organization.return_value = SAMPLE_ORG_DRY_RUN_RESULT
            MockOrgService.return_value = mock_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "org",
                    "setup",
                    "--org-id",
                    "42",
                    "--url",
                    "https://connection.keboola.com",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"

    def test_org_setup_confirmation_declined(self, tmp_path: Path) -> None:
        """org setup exits cleanly when user declines confirmation."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch(
                "keboola_agent_cli.commands.org._resolve_manage_token",
                return_value="manage-token-abcdef",
            ),
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            mock_service = MagicMock()
            # The preview dry-run call returns projects to add
            mock_service.setup_organization.return_value = SAMPLE_ORG_DRY_RUN_RESULT
            MockOrgService.return_value = mock_service

            # Simulate user typing "n" to decline confirmation
            result = runner.invoke(
                app,
                [
                    "org",
                    "setup",
                    "--org-id",
                    "42",
                    "--url",
                    "https://connection.keboola.com",
                ],
                input="n\n",
            )

        assert result.exit_code == 0
        assert "Aborted" in result.output

    def test_org_setup_api_error(self, tmp_path: Path) -> None:
        """org setup with API error returns appropriate exit code."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch(
                "keboola_agent_cli.commands.org._resolve_manage_token",
                return_value="manage-token-abcdef",
            ),
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            mock_service = MagicMock()
            mock_service.setup_organization.side_effect = KeboolaApiError(
                message="Invalid manage token",
                status_code=401,
                error_code="INVALID_TOKEN",
                retryable=False,
            )
            MockOrgService.return_value = mock_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "org",
                    "setup",
                    "--org-id",
                    "42",
                    "--url",
                    "https://connection.keboola.com",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 3
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_TOKEN"

    def test_org_setup_json_with_yes_flag(self, tmp_path: Path) -> None:
        """org setup --json --yes skips confirmation and executes directly."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch(
                "keboola_agent_cli.commands.org._resolve_manage_token",
                return_value="manage-token-abcdef",
            ),
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            mock_service = MagicMock()
            mock_service.setup_organization.return_value = SAMPLE_ORG_SETUP_RESULT
            MockOrgService.return_value = mock_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "org",
                    "setup",
                    "--org-id",
                    "42",
                    "--url",
                    "https://connection.keboola.com",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["organization_id"] == 42
        assert len(output["data"]["projects_added"]) == 1


# ---------------------------------------------------------------------------
# _resolve_manage_token tests
# ---------------------------------------------------------------------------


class TestResolveManageToken:
    """Tests for _resolve_manage_token() in org.py."""

    def test_token_from_env(self) -> None:
        """_resolve_manage_token returns token from KBC_MANAGE_API_TOKEN env var."""
        from keboola_agent_cli.commands.org import _resolve_manage_token

        with patch.dict(os.environ, {"KBC_MANAGE_API_TOKEN": "env-manage-token"}):
            token = _resolve_manage_token()

        assert token == "env-manage-token"

    def test_token_from_prompt(self) -> None:
        """_resolve_manage_token prompts interactively when env var is not set."""
        import sys

        from keboola_agent_cli.commands.org import _resolve_manage_token

        with (
            patch.dict(os.environ, {}, clear=False),
            patch("keboola_agent_cli.commands.org.typer.prompt", return_value="prompted-token"),
            patch.object(sys, "stdin") as mock_stdin,
        ):
            # Ensure KBC_MANAGE_API_TOKEN is not set
            os.environ.pop("KBC_MANAGE_API_TOKEN", None)
            mock_stdin.isatty.return_value = True

            token = _resolve_manage_token()

        assert token == "prompted-token"

    def test_non_tty_error(self) -> None:
        """_resolve_manage_token raises Exit when not TTY and no env var."""
        import sys

        import typer

        from keboola_agent_cli.commands.org import _resolve_manage_token

        with (
            patch.dict(os.environ, {}, clear=False),
            patch.object(sys, "stdin") as mock_stdin,
            pytest.raises(typer.Exit) as exc_info,
        ):
            # Ensure KBC_MANAGE_API_TOKEN is not set
            os.environ.pop("KBC_MANAGE_API_TOKEN", None)
            mock_stdin.isatty.return_value = False

            _resolve_manage_token()

        assert exc_info.value.exit_code == 2
