"""Tests for CLI commands via CliRunner - project add, list in JSON and human mode."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import TokenVerifyResponse
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
