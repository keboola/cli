"""Integration tests for Keboola Agent CLI using real API credentials.

These tests are skipped unless the following environment variables are set:
  - KBA_TEST_TOKEN_AWS: Storage API token for AWS stack
  - KBA_TEST_URL_AWS: Stack URL for AWS stack (default: https://connection.keboola.com)

To run integration tests:
    KBA_TEST_TOKEN_AWS=your-token uv run pytest tests/test_integration.py -v

These tests exercise the full workflow: add project, list, status, config list, remove.
"""

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore

runner = CliRunner()

# Environment variable names for test credentials
ENV_TOKEN_AWS = "KBA_TEST_TOKEN_AWS"
ENV_URL_AWS = "KBA_TEST_URL_AWS"

# Skip all tests in this module if credentials are not available
HAS_AWS_CREDENTIALS = os.environ.get(ENV_TOKEN_AWS) is not None

skip_without_credentials = pytest.mark.skipif(
    not HAS_AWS_CREDENTIALS,
    reason=f"Integration tests require {ENV_TOKEN_AWS} environment variable",
)


@pytest.fixture
def integration_config_dir(tmp_path: Path) -> Path:
    """Provide a temporary config directory for integration tests."""
    config_dir = tmp_path / "integration_config"
    config_dir.mkdir()
    return config_dir


def _invoke_with_store(config_dir: Path, args: list[str]):
    """Invoke the CLI app with a custom config store pointed at config_dir."""
    from unittest.mock import patch

    with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
        MockStore.return_value = ConfigStore(config_dir=config_dir)
        return runner.invoke(app, args)


@skip_without_credentials
@pytest.mark.integration
class TestFullWorkflow:
    """End-to-end integration test: add project, list, status, config list, remove."""

    def test_full_workflow(self, integration_config_dir: Path) -> None:
        """Full workflow: add -> list -> status -> config list -> remove."""
        token = os.environ[ENV_TOKEN_AWS]
        url = os.environ.get(ENV_URL_AWS, "https://connection.keboola.com")
        alias = "integration-test"

        from unittest.mock import patch

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            store = ConfigStore(config_dir=integration_config_dir)
            MockStore.return_value = store

            # Step 1: Add project
            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--alias",
                    alias,
                    "--url",
                    url,
                    "--token",
                    token,
                ],
            )
            assert result.exit_code == 0, f"project add failed: {result.output}"
            add_output = json.loads(result.output)
            assert add_output["status"] == "ok"
            assert add_output["data"]["alias"] == alias
            assert add_output["data"]["project_name"]  # Should have a name
            assert add_output["data"]["project_id"] > 0  # Should have an ID

            # Verify token is masked in output
            assert token not in result.output

            # Step 2: List projects
            result = runner.invoke(app, ["--json", "project", "list"])
            assert result.exit_code == 0, f"project list failed: {result.output}"
            list_output = json.loads(result.output)
            assert list_output["status"] == "ok"
            assert len(list_output["data"]) >= 1
            project_aliases = [p["alias"] for p in list_output["data"]]
            assert alias in project_aliases

            # Verify token is masked in list output too
            assert token not in result.output

            # Step 3: Project status
            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "status",
                    "--project",
                    alias,
                ],
            )
            assert result.exit_code == 0, f"project status failed: {result.output}"
            status_output = json.loads(result.output)
            assert status_output["status"] == "ok"
            assert len(status_output["data"]) == 1
            assert status_output["data"][0]["alias"] == alias
            assert status_output["data"][0]["status"] == "ok"
            assert status_output["data"][0]["response_time_ms"] >= 0

            # Step 4: Config list
            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "list",
                    "--project",
                    alias,
                ],
            )
            assert result.exit_code == 0, f"config list failed: {result.output}"
            config_output = json.loads(result.output)
            assert config_output["status"] == "ok"
            assert "configs" in config_output["data"]
            assert "errors" in config_output["data"]
            assert config_output["data"]["errors"] == []
            # Configs may or may not be empty depending on the project
            # but the structure should be correct
            for cfg in config_output["data"]["configs"]:
                assert cfg["project_alias"] == alias
                assert "component_id" in cfg
                assert "config_name" in cfg

            # Step 5: Doctor check
            result = runner.invoke(app, ["--json", "doctor"])
            assert result.exit_code == 0, f"doctor failed: {result.output}"
            doctor_output = json.loads(result.output)
            assert doctor_output["status"] == "ok"
            assert doctor_output["data"]["summary"]["healthy"] is True

            # Step 6: Remove project
            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "remove",
                    "--alias",
                    alias,
                ],
            )
            assert result.exit_code == 0, f"project remove failed: {result.output}"
            remove_output = json.loads(result.output)
            assert remove_output["status"] == "ok"

            # Verify project is gone
            result = runner.invoke(app, ["--json", "project", "list"])
            assert result.exit_code == 0
            final_list = json.loads(result.output)
            remaining_aliases = [p["alias"] for p in final_list["data"]]
            assert alias not in remaining_aliases

    def test_add_with_invalid_token_returns_error(self, integration_config_dir: Path) -> None:
        """Adding a project with a deliberately invalid token returns an auth error."""
        from unittest.mock import patch

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=integration_config_dir)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--alias",
                    "bad-project",
                    "--url",
                    "https://connection.keboola.com",
                    "--token",
                    "000-invalid-token-definitely-wrong",
                ],
            )

        assert result.exit_code == 3
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_TOKEN"

    def test_context_command_works(self, integration_config_dir: Path) -> None:
        """Context command outputs useful agent instructions."""
        from unittest.mock import patch

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=integration_config_dir)
            result = runner.invoke(app, ["context"])

        assert result.exit_code == 0
        assert "kbagent" in result.output
        assert "--json" in result.output
