"""Tests for `kbagent config examples` (issue #393, MCP get_config_examples port).

Covers the L2 ComponentService.get_config_examples method (full example
bodies surfaced, alias resolution, error propagation) and the L1 CLI command
(--json structure, --row filter, human render, API error mapping).
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from helpers import setup_single_project
from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.component_service import ComponentService
from keboola_agent_cli.services.project_service import ProjectService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

runner = CliRunner()

COMPONENT_DETAIL_RESPONSE: dict[str, Any] = {
    "componentId": "keboola.ex-google-drive",
    "componentName": "Google Drive",
    "componentType": "extractor",
    "componentCategories": [],
    "componentFlags": [],
    "description": "Extract files from Google Drive",
    "longDescription": "",
    "documentationUrl": "",
    "documentation": "",
    "configurationSchema": {},
    "configurationRowSchema": {},
    "rootConfigurationExamples": [
        {"parameters": {"baseUrl": "https://example.com", "auth": {"type": "oauth"}}},
        {"parameters": {"baseUrl": "https://other.example.com"}},
    ],
    "rowConfigurationExamples": [
        {"parameters": {"fileId": "abc123", "outputTable": "out.c-main.drive"}},
    ],
}


def _make_ai_client(detail_response: dict[str, Any]) -> MagicMock:
    ai_client = MagicMock()
    ai_client.get_component_detail.return_value = detail_response
    return ai_client


def _make_service(tmp_config_dir: Path, ai_client: MagicMock) -> ComponentService:
    store = setup_single_project(tmp_config_dir)
    return ComponentService(
        config_store=store,
        ai_client_factory=lambda url, token: ai_client,
    )


# ---------------------------------------------------------------------------
# L2 service tests
# ---------------------------------------------------------------------------


class TestGetConfigExamplesService:
    """Tests for ComponentService.get_config_examples."""

    def test_returns_full_example_bodies(self, tmp_config_dir: Path) -> None:
        """Both example lists are returned verbatim (not just counts)."""
        ai_client = _make_ai_client(COMPONENT_DETAIL_RESPONSE)
        service = _make_service(tmp_config_dir, ai_client)

        result = service.get_config_examples(alias="prod", component_id="keboola.ex-google-drive")

        assert result == {
            "component_id": "keboola.ex-google-drive",
            "root_examples": COMPONENT_DETAIL_RESPONSE["rootConfigurationExamples"],
            "row_examples": COMPONENT_DETAIL_RESPONSE["rowConfigurationExamples"],
        }
        ai_client.get_component_detail.assert_called_once_with("keboola.ex-google-drive")
        ai_client.close.assert_called_once()

    def test_alias_none_uses_first_project(self, tmp_config_dir: Path) -> None:
        """When alias is None the first configured project is used."""
        ai_client = _make_ai_client(COMPONENT_DETAIL_RESPONSE)
        service = _make_service(tmp_config_dir, ai_client)

        result = service.get_config_examples(alias=None, component_id="keboola.ex-google-drive")

        assert result["component_id"] == "keboola.ex-google-drive"
        ai_client.get_component_detail.assert_called_once_with("keboola.ex-google-drive")

    def test_no_projects_raises_config_error(self, tmp_config_dir: Path) -> None:
        """Without any configured project a ConfigError is raised."""
        store = ConfigStore(config_dir=tmp_config_dir)
        ai_client = _make_ai_client(COMPONENT_DETAIL_RESPONSE)
        service = ComponentService(
            config_store=store,
            ai_client_factory=lambda url, token: ai_client,
        )

        with pytest.raises(ConfigError, match="No projects configured"):
            service.get_config_examples(alias=None, component_id="keboola.ex-http")

        ai_client.get_component_detail.assert_not_called()

    def test_api_error_propagates_and_closes_client(self, tmp_config_dir: Path) -> None:
        """AI Service errors bubble up as KeboolaApiError; client is closed."""
        ai_client = MagicMock()
        ai_client.get_component_detail.side_effect = KeboolaApiError(
            message="Component not found",
            status_code=404,
            error_code="NOT_FOUND",
            retryable=False,
        )
        service = _make_service(tmp_config_dir, ai_client)

        with pytest.raises(KeboolaApiError):
            service.get_config_examples(alias="prod", component_id="no.such.component")

        ai_client.close.assert_called_once()

    def test_component_detail_contract_unchanged(self, tmp_config_dir: Path) -> None:
        """get_component_detail still returns counts only (no example bodies)."""
        ai_client = _make_ai_client(COMPONENT_DETAIL_RESPONSE)
        service = _make_service(tmp_config_dir, ai_client)

        result = service.get_component_detail(alias="prod", component_id="keboola.ex-google-drive")

        assert result["examples_count"] == 2
        assert result["row_examples_count"] == 1
        assert "root_examples" not in result
        assert "row_examples" not in result


# ---------------------------------------------------------------------------
# L1 CLI tests
# ---------------------------------------------------------------------------


def _setup_config(config_dir: Path) -> ConfigStore:
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
            project_name="prod",
            project_id=1234,
        ),
    )
    return store


def _invoke(tmp_path: Path, mock_svc: MagicMock, args: list[str]):
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    store = _setup_config(config_dir)

    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
        patch("keboola_agent_cli.cli.ComponentService") as MockCompService,
    ):
        MockStore.return_value = store
        MockProjService.return_value = ProjectService(config_store=store)
        MockCompService.return_value = mock_svc

        return runner.invoke(app, args)


def _examples_result() -> dict[str, Any]:
    return {
        "component_id": "keboola.ex-google-drive",
        "root_examples": [
            {"parameters": {"baseUrl": "https://example.com"}},
            {"parameters": {"baseUrl": "https://other.example.com"}},
        ],
        "row_examples": [
            {"parameters": {"fileId": "abc123"}},
        ],
    }


class TestConfigExamplesCli:
    """Tests for `kbagent config examples` command."""

    def test_examples_json(self, tmp_path: Path) -> None:
        """--json emits the structured {component_id, root_examples, row_examples} dict."""
        mock_svc = MagicMock()
        mock_svc.get_config_examples.return_value = _examples_result()

        result = _invoke(
            tmp_path,
            mock_svc,
            [
                "--json",
                "config",
                "examples",
                "--component-id",
                "keboola.ex-google-drive",
                "--project",
                "prod",
            ],
        )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["component_id"] == "keboola.ex-google-drive"
        assert len(output["data"]["root_examples"]) == 2
        assert output["data"]["root_examples"][0]["parameters"]["baseUrl"] == (
            "https://example.com"
        )
        assert len(output["data"]["row_examples"]) == 1
        mock_svc.get_config_examples.assert_called_once_with(
            alias="prod",
            component_id="keboola.ex-google-drive",
        )

    def test_examples_row_filter_json(self, tmp_path: Path) -> None:
        """--row filters the JSON payload to row examples only."""
        mock_svc = MagicMock()
        mock_svc.get_config_examples.return_value = _examples_result()

        result = _invoke(
            tmp_path,
            mock_svc,
            [
                "--json",
                "config",
                "examples",
                "--component-id",
                "keboola.ex-google-drive",
                "--row",
            ],
        )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["component_id"] == "keboola.ex-google-drive"
        assert "root_examples" not in output["data"]
        assert len(output["data"]["row_examples"]) == 1
        # --project omitted -> service receives alias=None (first available)
        mock_svc.get_config_examples.assert_called_once_with(
            alias=None,
            component_id="keboola.ex-google-drive",
        )

    def test_examples_human(self, tmp_path: Path) -> None:
        """Human mode renders numbered JSON blocks under both headings."""
        mock_svc = MagicMock()
        mock_svc.get_config_examples.return_value = _examples_result()

        result = _invoke(
            tmp_path,
            mock_svc,
            [
                "config",
                "examples",
                "--component-id",
                "keboola.ex-google-drive",
                "--project",
                "prod",
            ],
        )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "Root Configuration Examples" in result.output
        assert "Row Configuration Examples" in result.output
        assert "Example 1" in result.output
        assert "Example 2" in result.output
        assert "baseUrl" in result.output
        assert "fileId" in result.output

    def test_examples_human_row_only(self, tmp_path: Path) -> None:
        """--row in human mode hides the root section entirely."""
        mock_svc = MagicMock()
        mock_svc.get_config_examples.return_value = _examples_result()

        result = _invoke(
            tmp_path,
            mock_svc,
            [
                "config",
                "examples",
                "--component-id",
                "keboola.ex-google-drive",
                "--row",
            ],
        )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "Root Configuration Examples" not in result.output
        assert "Row Configuration Examples" in result.output
        assert "fileId" in result.output

    def test_examples_empty_lists_human(self, tmp_path: Path) -> None:
        """Components without examples render a (none) placeholder, exit 0."""
        mock_svc = MagicMock()
        mock_svc.get_config_examples.return_value = {
            "component_id": "keboola.ex-empty",
            "root_examples": [],
            "row_examples": [],
        }

        result = _invoke(
            tmp_path,
            mock_svc,
            ["config", "examples", "--component-id", "keboola.ex-empty"],
        )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "(none)" in result.output

    def test_examples_api_error(self, tmp_path: Path) -> None:
        """API NOT_FOUND maps to exit code 1 with structured error output."""
        mock_svc = MagicMock()
        mock_svc.get_config_examples.side_effect = KeboolaApiError(
            message="Component 'no.such.component' not found",
            status_code=404,
            error_code="NOT_FOUND",
            retryable=False,
        )

        result = _invoke(
            tmp_path,
            mock_svc,
            [
                "--json",
                "config",
                "examples",
                "--component-id",
                "no.such.component",
                "--project",
                "prod",
            ],
        )

        assert result.exit_code == 1
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "NOT_FOUND" in output["error"]["code"]

    def test_examples_config_error(self, tmp_path: Path) -> None:
        """ConfigError (e.g. unknown alias) maps to exit code 5."""
        mock_svc = MagicMock()
        mock_svc.get_config_examples.side_effect = ConfigError("Project 'nope' not found")

        result = _invoke(
            tmp_path,
            mock_svc,
            [
                "--json",
                "config",
                "examples",
                "--component-id",
                "keboola.ex-http",
                "--project",
                "nope",
            ],
        )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "CONFIG_ERROR" in output["error"]["code"]

    def test_examples_missing_component_id(self, tmp_path: Path) -> None:
        """Missing required --component-id is a usage error (exit 2)."""
        mock_svc = MagicMock()

        result = _invoke(tmp_path, mock_svc, ["--json", "config", "examples"])

        assert result.exit_code == 2
        mock_svc.get_config_examples.assert_not_called()
