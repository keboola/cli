"""Tests for CLI commands via CliRunner - project and config commands."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig, TokenVerifyResponse
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.project_service import ProjectService

runner = CliRunner()


def _make_mock_client(
    project_name: str = "Test Project",
    project_id: int = 1234,
) -> MagicMock:
    """Create a mock client for the factory."""
    mock_client = MagicMock()
    mock_client.verify_token.return_value = TokenVerifyResponse(
        token_id="12345",
        token_description="My Token",
        project_id=project_id,
        project_name=project_name,
        owner_name=project_name,
    )
    return mock_client


class TestProjectAdd:
    """Tests for `kbagent project add` command."""

    def test_project_add_success_json(self, tmp_path: Path) -> None:
        """project add with --json outputs structured success response."""
        mock_client = _make_mock_client(project_name="Prod Project", project_id=5678)
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockService:

            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            result = runner.invoke(app, [
                "--json",
                "project", "add",
                "--alias", "prod",
                "--url", "https://connection.keboola.com",
                "--token", "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ])

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
        mock_client = _make_mock_client()
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockService:

            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            result = runner.invoke(app, [
                "project", "add",
                "--alias", "test",
                "--url", "https://connection.keboola.com",
                "--token", "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ])

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

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockService:

            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: fail_client,
            )
            MockService.return_value = service_instance

            result = runner.invoke(app, [
                "--json",
                "project", "add",
                "--alias", "bad",
                "--token", "invalid-token-abcdefgh",
            ])

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

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockService:

            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: timeout_client,
            )
            MockService.return_value = service_instance

            result = runner.invoke(app, [
                "--json",
                "project", "add",
                "--alias", "timeout",
                "--token", "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ])

        assert result.exit_code == 4


class TestProjectList:
    """Tests for `kbagent project list` command."""

    def test_project_list_json_empty(self, tmp_path: Path) -> None:
        """project list --json with no projects returns empty data."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockService:

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
        mock_client = _make_mock_client()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockService:

            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            # Add a project first
            runner.invoke(app, [
                "project", "add",
                "--alias", "test",
                "--url", "https://connection.keboola.com",
                "--token", "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ])

            result = runner.invoke(app, ["--json", "project", "list"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert len(output["data"]) == 1
        assert output["data"][0]["alias"] == "test"
        # Token must be masked
        full_token = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"
        assert output["data"][0]["token"] != full_token

    def test_project_list_human_mode(self, tmp_path: Path) -> None:
        """project list in human mode outputs a Rich table."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        mock_client = _make_mock_client(project_name="My Production")

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockService:

            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            runner.invoke(app, [
                "project", "add",
                "--alias", "prod",
                "--url", "https://connection.keboola.com",
                "--token", "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ])

            result = runner.invoke(app, ["project", "list"])

        assert result.exit_code == 0
        assert "prod" in result.output
        assert "Connected Projects" in result.output

    def test_project_list_human_empty(self, tmp_path: Path) -> None:
        """project list in human mode with no projects shows helpful message."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockService:

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
        mock_client = _make_mock_client()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockService:

            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            runner.invoke(app, [
                "project", "add",
                "--alias", "test",
                "--token", "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ])

            result = runner.invoke(app, [
                "--json",
                "project", "remove",
                "--alias", "test",
            ])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["alias"] == "test"

    def test_project_remove_nonexistent_exit_code_5(self, tmp_path: Path) -> None:
        """project remove with nonexistent alias returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockService:

            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance
            MockService.return_value = ProjectService(config_store=store_instance)

            result = runner.invoke(app, [
                "--json",
                "project", "remove",
                "--alias", "nonexistent",
            ])

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
        mock_client = _make_mock_client(project_name="Prod", project_id=123)

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockService:

            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            runner.invoke(app, [
                "project", "add",
                "--alias", "prod",
                "--token", "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ])

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
        mock_client = _make_mock_client()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockService:

            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            runner.invoke(app, [
                "project", "add",
                "--alias", "test",
                "--token", "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ])

            result = runner.invoke(app, ["project", "status"])

        assert result.exit_code == 0
        assert "Project Status" in result.output


class TestProjectEdit:
    """Tests for `kbagent project edit` command."""

    def test_project_edit_url_json(self, tmp_path: Path) -> None:
        """project edit --url with --json updates URL and returns result."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        mock_client = _make_mock_client()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockService:

            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            runner.invoke(app, [
                "project", "add",
                "--alias", "test",
                "--url", "https://old.keboola.com",
                "--token", "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ])

            result = runner.invoke(app, [
                "--json",
                "project", "edit",
                "--alias", "test",
                "--url", "https://new.keboola.com",
            ])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["stack_url"] == "https://new.keboola.com"

    def test_project_edit_config_error_exit_code_5(self, tmp_path: Path) -> None:
        """project edit with no changes returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        mock_client = _make_mock_client()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockService:

            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            runner.invoke(app, [
                "project", "add",
                "--alias", "test",
                "--token", "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ])

            result = runner.invoke(app, [
                "--json",
                "project", "edit",
                "--alias", "test",
            ])

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
            store.add_project(alias, ProjectConfig(
                stack_url=info.get("stack_url", "https://connection.keboola.com"),
                token=info["token"],
                project_name=info.get("project_name", alias),
                project_id=info.get("project_id", 1234),
            ))
    return store


class TestConfigList:
    """Tests for `kbagent config list` command."""

    def test_config_list_json_output(self, tmp_path: Path) -> None:
        """config list --json returns structured JSON with configs from all projects."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_components_client(SAMPLE_COMPONENTS)
        store = _setup_config_test(config_dir, {
            "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
        })

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockProjService, \
             patch("keboola_agent_cli.cli.ConfigService") as MockCfgService:

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
        store = _setup_config_test(config_dir, {
            "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
        })

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockProjService, \
             patch("keboola_agent_cli.cli.ConfigService") as MockCfgService:

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

        store = _setup_config_test(config_dir, {
            "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            "dev": {"token": "532-abcdef-ghijklmnopqrst"},
        })

        def factory(url, token):
            if "901" in token:
                return prod_client
            return dev_client

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockProjService, \
             patch("keboola_agent_cli.cli.ConfigService") as MockCfgService:

            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=factory,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(app, [
                "--json", "config", "list",
                "--project", "prod",
            ])

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

        store = _setup_config_test(config_dir, {
            "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
            "dev": {"token": "532-abcdef-ghijklmnopqrst"},
        })

        def factory(url, token):
            if "901" in token:
                return prod_client
            return dev_client

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockProjService, \
             patch("keboola_agent_cli.cli.ConfigService") as MockCfgService:

            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=factory,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(app, [
                "--json", "config", "list",
                "--project", "prod",
                "--project", "dev",
            ])

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

        store = _setup_config_test(config_dir, {
            "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
        })

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockProjService, \
             patch("keboola_agent_cli.cli.ConfigService") as MockCfgService:

            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(app, [
                "--json", "config", "list",
                "--component-type", "extractor",
            ])

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
        store = _setup_config_test(config_dir, {
            "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
        })

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockProjService, \
             patch("keboola_agent_cli.cli.ConfigService") as MockCfgService:

            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(app, [
                "--json", "config", "list",
                "--component-id", "keboola.wr-db-snowflake",
            ])

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

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockProjService, \
             patch("keboola_agent_cli.cli.ConfigService") as MockCfgService:

            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(app, [
                "--json", "config", "list",
                "--project", "nonexistent",
            ])

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

        store = _setup_config_test(config_dir, {
            "good": {"token": "901-good-abcdefghijklmnop"},
            "bad": {"token": "532-bad-abcdefghijklmnopq"},
        })

        def factory(url, token):
            if "good" in token:
                return good_client
            return bad_client

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockProjService, \
             patch("keboola_agent_cli.cli.ConfigService") as MockCfgService:

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

        store = _setup_config_test(config_dir, {
            "good": {"token": "901-good-abcdefghijklmnop"},
            "bad": {"token": "532-bad-abcdefghijklmnopq"},
        })

        def factory(url, token):
            if "good" in token:
                return good_client
            return bad_client

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockProjService, \
             patch("keboola_agent_cli.cli.ConfigService") as MockCfgService:

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
        store = _setup_config_test(config_dir, {
            "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
        })

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockProjService, \
             patch("keboola_agent_cli.cli.ConfigService") as MockCfgService:

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

        store = _setup_config_test(config_dir, {
            "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
        })

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockProjService, \
             patch("keboola_agent_cli.cli.ConfigService") as MockCfgService:

            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(app, [
                "--json", "config", "list",
                "--component-type", "invalid-type",
            ])

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

        store = _setup_config_test(config_dir, {
            "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
        })

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockProjService, \
             patch("keboola_agent_cli.cli.ConfigService") as MockCfgService:

            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(app, [
                "--json", "config", "detail",
                "--project", "prod",
                "--component-id", "keboola.ex-db-snowflake",
                "--config-id", "101",
            ])

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

        store = _setup_config_test(config_dir, {
            "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
        })

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockProjService, \
             patch("keboola_agent_cli.cli.ConfigService") as MockCfgService:

            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(app, [
                "config", "detail",
                "--project", "prod",
                "--component-id", "keboola.ex-db-snowflake",
                "--config-id", "101",
            ])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "Production Load" in result.output
        assert "Configuration Detail" in result.output

    def test_config_detail_unknown_alias_exit_code_5(self, tmp_path: Path) -> None:
        """config detail with unknown alias returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(config_dir)

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockProjService, \
             patch("keboola_agent_cli.cli.ConfigService") as MockCfgService:

            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(app, [
                "--json", "config", "detail",
                "--project", "nonexistent",
                "--component-id", "keboola.ex-db-snowflake",
                "--config-id", "101",
            ])

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

        store = _setup_config_test(config_dir, {
            "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
        })

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockProjService, \
             patch("keboola_agent_cli.cli.ConfigService") as MockCfgService:

            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(app, [
                "--json", "config", "detail",
                "--project", "prod",
                "--component-id", "keboola.ex-db-snowflake",
                "--config-id", "999",
            ])

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

        store = _setup_config_test(config_dir, {
            "prod": {"token": "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"},
        })

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore, \
             patch("keboola_agent_cli.cli.ProjectService") as MockProjService, \
             patch("keboola_agent_cli.cli.ConfigService") as MockCfgService:

            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(app, [
                "--json", "config", "detail",
                "--project", "prod",
                "--component-id", "keboola.ex-db-snowflake",
                "--config-id", "101",
            ])

        assert result.exit_code == 3
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_TOKEN"
