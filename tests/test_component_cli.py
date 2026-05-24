"""Tests for component CLI commands and config new command via CliRunner.

Tests component list, component detail, and config new subcommands.
Follows the existing CLI test pattern from test_workspace_cli.py with
patched services in ctx.obj.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.project_service import ProjectService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

runner = CliRunner()


def _setup_config(config_dir: Path, projects: dict[str, dict] | None = None) -> ConfigStore:
    """Set up a ConfigStore with given projects for CLI tests."""
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


def _make_component_mock() -> MagicMock:
    """Create a fresh MagicMock for ComponentService."""
    return MagicMock()


# ---------------------------------------------------------------------------
# component list
# ---------------------------------------------------------------------------


class TestComponentList:
    """Tests for `kbagent component list` command."""

    def test_component_list_json(self, tmp_path: Path) -> None:
        """component list --json returns components in structured JSON."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_svc = _make_component_mock()
        mock_svc.list_components.return_value = {
            "components": [
                {
                    "component_id": "keboola.ex-http",
                    "component_name": "HTTP",
                    "component_type": "extractor",
                    "categories": ["web"],
                    "description": "Download files via HTTP",
                },
                {
                    "component_id": "keboola.snowflake-transformation",
                    "component_name": "Snowflake SQL",
                    "component_type": "transformation",
                    "categories": [],
                    "description": "",
                },
            ],
            "errors": [],
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ComponentService") as MockCompService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCompService.return_value = mock_svc

            result = runner.invoke(app, ["--json", "component", "list"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert len(output["data"]["components"]) == 2
        assert output["data"]["components"][0]["component_id"] == "keboola.ex-http"
        assert output["data"]["errors"] == []
        mock_svc.list_components.assert_called_once_with(
            aliases=None,
            component_type=None,
            query=None,
        )

    def test_component_list_with_query_json(self, tmp_path: Path) -> None:
        """component list --json --query filters via AI search."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_svc = _make_component_mock()
        mock_svc.list_components.return_value = {
            "components": [
                {
                    "component_id": "keboola.ex-aws-s3",
                    "component_name": "AWS S3",
                    "component_type": "extractor",
                    "categories": ["cloud"],
                    "description": "Extract data from S3 buckets",
                    "score": 0.95,
                    "query": "s3 extractor",
                },
            ],
            "errors": [],
            "query": "s3 extractor",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ComponentService") as MockCompService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCompService.return_value = mock_svc

            result = runner.invoke(
                app,
                ["--json", "component", "list", "--query", "s3 extractor"],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert len(output["data"]["components"]) == 1
        assert output["data"]["components"][0]["component_id"] == "keboola.ex-aws-s3"
        mock_svc.list_components.assert_called_once_with(
            aliases=None,
            component_type=None,
            query="s3 extractor",
        )

    def test_component_list_invalid_type(self, tmp_path: Path) -> None:
        """component list with invalid --type returns exit code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_svc = _make_component_mock()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ComponentService") as MockCompService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCompService.return_value = mock_svc

            result = runner.invoke(
                app,
                ["--json", "component", "list", "--type", "nonexistent"],
            )

        assert result.exit_code == 2
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "INVALID_ARGUMENT" in output["error"]["code"]
        # Service should NOT have been called
        mock_svc.list_components.assert_not_called()

    def test_component_list_human(self, tmp_path: Path) -> None:
        """component list in human mode outputs a Rich table."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_svc = _make_component_mock()
        mock_svc.list_components.return_value = {
            "components": [
                {
                    "component_id": "keboola.ex-http",
                    "component_name": "HTTP",
                    "component_type": "extractor",
                    "categories": ["web"],
                    "description": "Download files via HTTP",
                },
            ],
            "errors": [],
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ComponentService") as MockCompService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCompService.return_value = mock_svc

            result = runner.invoke(app, ["component", "list"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        # Human mode should contain table content
        assert "Components" in result.output
        assert "keboola.ex-http" in result.output
        assert "HTTP" in result.output


# ---------------------------------------------------------------------------
# component detail
# ---------------------------------------------------------------------------


class TestComponentDetail:
    """Tests for `kbagent component detail` command."""

    def test_component_detail_json(self, tmp_path: Path) -> None:
        """component detail --json returns component detail."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_svc = _make_component_mock()
        mock_svc.get_component_detail.return_value = {
            "component_id": "keboola.ex-db-snowflake",
            "component_name": "Snowflake Extractor",
            "component_type": "extractor",
            "description": "Extract data from Snowflake",
            "long_description": "Full-featured Snowflake extractor with incremental loading.",
            "categories": ["database", "cloud"],
            "documentation_url": "https://help.keboola.com/components/extractors/database/snowflake/",
            "schema_summary": {
                "property_count": 5,
                "required_count": 2,
                "has_row_schema": True,
            },
            "examples_count": 1,
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ComponentService") as MockCompService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCompService.return_value = mock_svc

            result = runner.invoke(
                app,
                [
                    "--json",
                    "component",
                    "detail",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["component_id"] == "keboola.ex-db-snowflake"
        assert output["data"]["component_name"] == "Snowflake Extractor"
        assert output["data"]["schema_summary"]["property_count"] == 5
        mock_svc.get_component_detail.assert_called_once_with(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
        )

    def test_component_detail_not_found(self, tmp_path: Path) -> None:
        """component detail with unknown component returns appropriate exit code."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_svc = _make_component_mock()
        mock_svc.get_component_detail.side_effect = KeboolaApiError(
            message="Component 'no.such.component' not found",
            status_code=404,
            error_code="NOT_FOUND",
            retryable=False,
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ComponentService") as MockCompService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCompService.return_value = mock_svc

            result = runner.invoke(
                app,
                [
                    "--json",
                    "component",
                    "detail",
                    "--component-id",
                    "no.such.component",
                    "--project",
                    "prod",
                ],
            )

        # NOT_FOUND maps to exit code 1 (general error)
        assert result.exit_code == 1
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "NOT_FOUND" in output["error"]["code"]


# ---------------------------------------------------------------------------
# config new
# ---------------------------------------------------------------------------


class TestConfigNew:
    """Tests for `kbagent config new` command (scaffold generation)."""

    def _make_scaffold(self) -> dict:
        """Return a representative scaffold dict for tests."""
        return {
            "component_id": "keboola.ex-http",
            "component_name": "HTTP",
            "component_type": "extractor",
            "directory": "extractor/keboola.ex-http/http-configuration",
            "files": [
                {
                    "path": "_config.yml",
                    "content": (
                        "# Component: HTTP (keboola.ex-http)\n"
                        "version: 2\n"
                        'name: "HTTP Configuration"\n'
                        "parameters:\n"
                        "  baseUrl: https://example.com\n"
                    ),
                },
                {
                    "path": "_description.md",
                    "content": "TODO: describe this configuration\n",
                },
            ],
        }

    def test_config_new_json(self, tmp_path: Path) -> None:
        """config new --json returns scaffold in structured JSON."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        scaffold = self._make_scaffold()
        mock_svc = _make_component_mock()
        mock_svc.generate_scaffold.return_value = scaffold

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ComponentService") as MockCompService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCompService.return_value = mock_svc

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "new",
                    "--component-id",
                    "keboola.ex-http",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["component_id"] == "keboola.ex-http"
        assert len(output["data"]["files"]) == 2
        assert output["data"]["files"][0]["path"] == "_config.yml"
        mock_svc.generate_scaffold.assert_called_once_with(
            alias="prod",
            component_id="keboola.ex-http",
            name=None,
        )

    def test_config_new_to_stdout(self, tmp_path: Path) -> None:
        """config new in human mode prints syntax-highlighted scaffold to stdout."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        scaffold = self._make_scaffold()
        mock_svc = _make_component_mock()
        mock_svc.generate_scaffold.return_value = scaffold

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ComponentService") as MockCompService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCompService.return_value = mock_svc

            result = runner.invoke(
                app,
                [
                    "config",
                    "new",
                    "--component-id",
                    "keboola.ex-http",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        # Human mode should include scaffold metadata and file names
        assert "keboola.ex-http" in result.output
        assert "_config.yml" in result.output

    def test_config_new_to_disk(self, tmp_path: Path) -> None:
        """config new --output-dir writes scaffold files to disk."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        output_dir = tmp_path / "scaffold_output"
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        scaffold = self._make_scaffold()
        mock_svc = _make_component_mock()
        mock_svc.generate_scaffold.return_value = scaffold

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ComponentService") as MockCompService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCompService.return_value = mock_svc

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "new",
                    "--component-id",
                    "keboola.ex-http",
                    "--project",
                    "prod",
                    "--output-dir",
                    str(output_dir),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert "files_written" in output["data"]
        assert len(output["data"]["files_written"]) == 2

        # Verify files were actually written to disk
        base_path = output_dir / scaffold["directory"]
        config_yml = base_path / "_config.yml"
        description_md = base_path / "_description.md"

        assert config_yml.exists(), f"Expected {config_yml} to exist"
        assert description_md.exists(), f"Expected {description_md} to exist"

        config_content = config_yml.read_text(encoding="utf-8")
        assert "keboola.ex-http" in config_content
        assert "version: 2" in config_content

        desc_content = description_md.read_text(encoding="utf-8")
        assert "TODO" in desc_content

    def test_config_new_to_disk_with_kbc_project(self, tmp_path: Path) -> None:
        """config new --output-dir in a kbc project writes under branch path (e.g. main/)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        output_dir = tmp_path / "kbc_project"
        output_dir.mkdir()

        # Create .keboola/manifest.json to simulate a kbc project
        keboola_dir = output_dir / ".keboola"
        keboola_dir.mkdir()
        manifest = {
            "version": 2,
            "branches": [{"id": 120, "path": "main", "metadata": {}}],
        }
        (keboola_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        scaffold = self._make_scaffold()
        mock_svc = _make_component_mock()
        mock_svc.generate_scaffold.return_value = scaffold

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ComponentService") as MockCompService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCompService.return_value = mock_svc

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "new",
                    "--component-id",
                    "keboola.ex-http",
                    "--project",
                    "prod",
                    "--output-dir",
                    str(output_dir),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"

        # Files should be under main/ prefix
        expected_path = output_dir / "main" / scaffold["directory"] / "_config.yml"
        assert expected_path.exists(), (
            f"Expected {expected_path} to exist (kbc branch prefix 'main/')"
        )

    def test_config_new_missing_component_id(self, tmp_path: Path) -> None:
        """config new without --component-id returns exit code 2 (usage error)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_svc = _make_component_mock()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ComponentService") as MockCompService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCompService.return_value = mock_svc

            result = runner.invoke(
                app,
                ["--json", "config", "new"],
            )

        # Typer returns exit code 2 for missing required options
        assert result.exit_code == 2
        # Service should NOT have been called
        mock_svc.generate_scaffold.assert_not_called()
