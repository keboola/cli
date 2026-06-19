"""CLI tests for `kbagent branch metadata-*` and `kbagent project description-*`.

Uses CliRunner + patched BranchService following the existing CLI test pattern
(see tests/test_workspace_cli.py). Verifies JSON mode, Rich mode, error
mapping, and the --text / --file / --stdin input resolver.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner, Result

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

runner = CliRunner()


def _setup_store(config_dir: Path) -> ConfigStore:
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
            project_name="Production",
            project_id=258,
        ),
    )
    return store


def _run(
    store: ConfigStore,
    mock_svc: MagicMock,
    args: list[str],
    *,
    input_text: str | None = None,
) -> Result:
    """Invoke the CLI with a mocked BranchService while letting everything else construct normally."""
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.BranchService") as MockBranchSvc,
    ):
        MockStore.return_value = store
        MockBranchSvc.return_value = mock_svc
        return runner.invoke(app, args, input=input_text)


class TestBranchMetadataList:
    def test_list_json(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.list_branch_metadata.return_value = {
            "project_alias": "prod",
            "branch_id": "default",
            "metadata": [{"id": 1, "key": "x", "value": "v", "provider": "user", "timestamp": "t"}],
        }

        result = _run(
            store,
            mock_svc,
            ["--json", "branch", "metadata-list", "--project", "prod"],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        assert payload["data"]["metadata"][0]["key"] == "x"
        mock_svc.list_branch_metadata.assert_called_once_with(alias="prod", branch_id="default")

    def test_list_human_renders_table(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.list_branch_metadata.return_value = {
            "project_alias": "prod",
            "branch_id": "default",
            "metadata": [],
        }
        result = _run(
            store,
            mock_svc,
            ["branch", "metadata-list", "--project", "prod"],
        )
        assert result.exit_code == 0, result.output
        assert "No metadata" in result.output

    def test_list_api_error_maps_exit_code(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.list_branch_metadata.side_effect = KeboolaApiError(
            message="bad", status_code=401, error_code="INVALID_TOKEN", retryable=False
        )
        result = _run(
            store,
            mock_svc,
            ["--json", "branch", "metadata-list", "--project", "prod"],
        )
        assert result.exit_code == 3
        payload = json.loads(result.output)
        assert payload["error"]["code"] == "INVALID_TOKEN"


class TestBranchMetadataGet:
    def test_get_json(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.get_branch_metadata.return_value = {
            "project_alias": "prod",
            "branch_id": "default",
            "key": "KBC.projectDescription",
            "value": "# hi",
        }
        result = _run(
            store,
            mock_svc,
            [
                "--json",
                "branch",
                "metadata-get",
                "--project",
                "prod",
                "--key",
                "KBC.projectDescription",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["data"]["value"] == "# hi"

    def test_get_not_found_returns_exit_1(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.get_branch_metadata.side_effect = KeboolaApiError(
            message="nope", status_code=404, error_code="NOT_FOUND", retryable=False
        )
        result = _run(
            store,
            mock_svc,
            [
                "--json",
                "branch",
                "metadata-get",
                "--project",
                "prod",
                "--key",
                "missing",
            ],
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["error"]["code"] == "NOT_FOUND"


class TestBranchMetadataSet:
    def test_set_text(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.set_branch_metadata.return_value = {
            "project_alias": "prod",
            "branch_id": "default",
            "key": "k",
            "value": "v",
            "result": [],
            "message": "Metadata 'k' set on branch 'default' of project 'prod'.",
        }
        result = _run(
            store,
            mock_svc,
            [
                "--json",
                "branch",
                "metadata-set",
                "--project",
                "prod",
                "--key",
                "k",
                "--text",
                "v",
            ],
        )
        assert result.exit_code == 0, result.output
        mock_svc.set_branch_metadata.assert_called_once_with(
            alias="prod", key="k", value="v", branch_id="default"
        )

    def test_set_file(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        payload_file = tmp_path / "content.md"
        payload_file.write_text("# Hello from file", encoding="utf-8")
        mock_svc = MagicMock()
        mock_svc.set_branch_metadata.return_value = {
            "project_alias": "prod",
            "branch_id": "default",
            "key": "k",
            "value": "# Hello from file",
            "result": [],
            "message": "ok",
        }
        result = _run(
            store,
            mock_svc,
            [
                "--json",
                "branch",
                "metadata-set",
                "--project",
                "prod",
                "--key",
                "k",
                "--file",
                str(payload_file),
            ],
        )
        assert result.exit_code == 0, result.output
        mock_svc.set_branch_metadata.assert_called_once_with(
            alias="prod", key="k", value="# Hello from file", branch_id="default"
        )

    def test_set_stdin(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.set_branch_metadata.return_value = {
            "project_alias": "prod",
            "branch_id": "default",
            "key": "k",
            "value": "stdin-value",
            "result": [],
            "message": "ok",
        }
        result = _run(
            store,
            mock_svc,
            [
                "--json",
                "branch",
                "metadata-set",
                "--project",
                "prod",
                "--key",
                "k",
                "--stdin",
            ],
            input_text="stdin-value",
        )
        assert result.exit_code == 0, result.output
        mock_svc.set_branch_metadata.assert_called_once_with(
            alias="prod", key="k", value="stdin-value", branch_id="default"
        )

    def test_set_no_source_errors(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        result = _run(
            store,
            mock_svc,
            [
                "--json",
                "branch",
                "metadata-set",
                "--project",
                "prod",
                "--key",
                "k",
            ],
        )
        assert result.exit_code == 2
        mock_svc.set_branch_metadata.assert_not_called()

    def test_set_multiple_sources_errors(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        result = _run(
            store,
            mock_svc,
            [
                "--json",
                "branch",
                "metadata-set",
                "--project",
                "prod",
                "--key",
                "k",
                "--text",
                "a",
                "--stdin",
            ],
        )
        assert result.exit_code == 2
        mock_svc.set_branch_metadata.assert_not_called()


class TestBranchMetadataDelete:
    def test_delete_json(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.delete_branch_metadata.return_value = {
            "project_alias": "prod",
            "branch_id": "default",
            "metadata_id": 99,
            "message": "Metadata ID 99 deleted from branch 'default' of project 'prod'.",
        }
        result = _run(
            store,
            mock_svc,
            [
                "--json",
                "branch",
                "metadata-delete",
                "--project",
                "prod",
                "--metadata-id",
                "99",
            ],
        )
        assert result.exit_code == 0, result.output
        mock_svc.delete_branch_metadata.assert_called_once_with(
            alias="prod", metadata_id=99, branch_id="default"
        )


class TestProjectDescription:
    def test_get_json_with_value(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.get_project_description.return_value = {
            "project_alias": "prod",
            "key": "KBC.projectDescription",
            "description": "# My project",
        }
        result = _run(
            store,
            mock_svc,
            ["--json", "project", "description-get", "--project", "prod"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["data"]["description"] == "# My project"

    def test_get_json_empty(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.get_project_description.return_value = {
            "project_alias": "prod",
            "key": "KBC.projectDescription",
            "description": "",
        }
        result = _run(
            store,
            mock_svc,
            ["--json", "project", "description-get", "--project", "prod"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["data"]["description"] == ""

    def test_set_via_text(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.set_project_description.return_value = {
            "project_alias": "prod",
            "key": "KBC.projectDescription",
            "description": "# New",
            "result": [],
            "message": "Project description updated for 'prod' (5 chars).",
        }
        result = _run(
            store,
            mock_svc,
            [
                "--json",
                "project",
                "description-set",
                "--project",
                "prod",
                "--text",
                "# New",
            ],
        )
        assert result.exit_code == 0, result.output
        mock_svc.set_project_description.assert_called_once_with(alias="prod", description="# New")

    def test_set_via_stdin(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.set_project_description.return_value = {
            "project_alias": "prod",
            "key": "KBC.projectDescription",
            "description": "from stdin",
            "result": [],
            "message": "ok",
        }
        result = _run(
            store,
            mock_svc,
            [
                "--json",
                "project",
                "description-set",
                "--project",
                "prod",
                "--stdin",
            ],
            input_text="from stdin",
        )
        assert result.exit_code == 0, result.output
        mock_svc.set_project_description.assert_called_once_with(
            alias="prod", description="from stdin"
        )

    def test_set_config_error(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.set_project_description.side_effect = ConfigError("Project 'nope' not found.")
        result = _run(
            store,
            mock_svc,
            [
                "--json",
                "project",
                "description-set",
                "--project",
                "prod",
                "--text",
                "x",
            ],
        )
        assert result.exit_code == 5
        payload = json.loads(result.output)
        assert payload["error"]["code"] == "CONFIG_ERROR"
