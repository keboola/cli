"""CLI tests for `kbagent project info` command."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.project_service import ProjectService

runner = CliRunner()

FULL_INFO = {
    "alias": "prod",
    "project_id": 1234,
    "project_name": "Production",
    "stack_url": "https://connection.keboola.com",
    "default_backend": "snowflake",
    "features": ["storage-branches", "orchestrator-tasks"],
    "limits": {"dataSizeBytes": {"name": "dataSizeBytes", "value": 5000000000}},
    "metrics": {"dataSizeBytes": 123456},
    "token_id": "99",
    "token_description": "Agent token",
    "is_master_token": False,
    "token_expires": None,
}


def _make_service_with_info(info: dict, tmp_path: Path) -> ProjectService:
    """Return a ProjectService whose get_info() returns the given dict."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-xxx",
            project_name="Production",
            project_id=1234,
        ),
    )
    service = MagicMock(spec=ProjectService)
    service.get_info.return_value = info
    return service, store


class TestProjectInfoJson:
    """Tests for `kbagent project info --json` output."""

    def test_success_json(self, tmp_path: Path) -> None:
        """project info --json emits structured ok response."""
        service, store = _make_service_with_info(FULL_INFO, tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
            patch("keboola_agent_cli.cli.ProjectService", return_value=service),
        ):
            result = runner.invoke(
                app,
                ["--json", "project", "info", "--project", "prod"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "ok"
        info = data["data"]
        assert info["project_id"] == 1234
        assert info["project_name"] == "Production"
        assert info["default_backend"] == "snowflake"
        assert "storage-branches" in info["features"]
        assert info["token_description"] == "Agent token"
        assert info["is_master_token"] is False
        assert info["token_expires"] is None

    def test_success_json_contains_limits_and_metrics(self, tmp_path: Path) -> None:
        """project info --json includes limits and metrics dicts."""
        service, store = _make_service_with_info(FULL_INFO, tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
            patch("keboola_agent_cli.cli.ProjectService", return_value=service),
        ):
            result = runner.invoke(
                app,
                ["--json", "project", "info", "--project", "prod"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "limits" in data["data"]
        assert "metrics" in data["data"]

    def test_config_error_exit_5(self, tmp_path: Path) -> None:
        """project info with unknown alias exits with code 5."""
        service, store = _make_service_with_info(FULL_INFO, tmp_path)
        service.get_info.side_effect = ConfigError("Project 'missing' not found.")

        with (
            patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
            patch("keboola_agent_cli.cli.ProjectService", return_value=service),
        ):
            result = runner.invoke(
                app,
                ["--json", "project", "info", "--project", "missing"],
            )

        assert result.exit_code == 5
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert "not found" in data["error"]["message"]

    def test_api_error_invalid_token_exit_3(self, tmp_path: Path) -> None:
        """project info with bad token exits with code 3."""
        service, store = _make_service_with_info(FULL_INFO, tmp_path)
        service.get_info.side_effect = KeboolaApiError(
            message="Invalid token",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
            patch("keboola_agent_cli.cli.ProjectService", return_value=service),
        ):
            result = runner.invoke(
                app,
                ["--json", "project", "info", "--project", "prod"],
            )

        assert result.exit_code == 3
        data = json.loads(result.output)
        assert data["status"] == "error"

    def test_api_error_generic_exit_1(self, tmp_path: Path) -> None:
        """project info with generic API error exits with code 1."""
        service, store = _make_service_with_info(FULL_INFO, tmp_path)
        service.get_info.side_effect = KeboolaApiError(
            message="Internal server error",
            status_code=500,
            error_code="INTERNAL_ERROR",
            retryable=True,
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
            patch("keboola_agent_cli.cli.ProjectService", return_value=service),
        ):
            result = runner.invoke(
                app,
                ["--json", "project", "info", "--project", "prod"],
            )

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["status"] == "error"


class TestProjectInfoHuman:
    """Tests for `kbagent project info` human-readable output."""

    def test_human_output_contains_key_fields(self, tmp_path: Path) -> None:
        """project info human output contains project name and ID."""
        service, store = _make_service_with_info(FULL_INFO, tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
            patch("keboola_agent_cli.cli.ProjectService", return_value=service),
        ):
            result = runner.invoke(
                app,
                ["project", "info", "--project", "prod"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "Production" in result.output
        assert "1234" in result.output
        assert "snowflake" in result.output

    def test_human_output_shows_features(self, tmp_path: Path) -> None:
        """project info human output lists feature flags."""
        service, store = _make_service_with_info(FULL_INFO, tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
            patch("keboola_agent_cli.cli.ProjectService", return_value=service),
        ):
            result = runner.invoke(
                app,
                ["project", "info", "--project", "prod"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert "storage-branches" in result.output

    def test_human_no_features_shows_none(self, tmp_path: Path) -> None:
        """project info human output gracefully handles empty features list."""
        info = dict(FULL_INFO)
        info["features"] = []
        service, store = _make_service_with_info(info, tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
            patch("keboola_agent_cli.cli.ProjectService", return_value=service),
        ):
            result = runner.invoke(
                app,
                ["project", "info", "--project", "prod"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert "(none)" in result.output

    def test_master_token_displays(self, tmp_path: Path) -> None:
        """project info shows master token status."""
        info = dict(FULL_INFO)
        info["is_master_token"] = True
        service, store = _make_service_with_info(info, tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
            patch("keboola_agent_cli.cli.ProjectService", return_value=service),
        ):
            result = runner.invoke(
                app,
                ["project", "info", "--project", "prod"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert "Yes" in result.output
