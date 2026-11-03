"""Tests for config metadata CLI commands and service methods.

Covers: metadata-list, get-metadata, set-metadata, delete-metadata, set-folder.
Uses mocked services (CLI layer) and mocked HTTP client (service layer).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, FeatureNotEnabledError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.job_service import JobService
from keboola_agent_cli.services.project_service import ProjectService
from keboola_agent_cli.services.workspace_service import WorkspaceService

runner = CliRunner()

TEST_TOKEN = "test-token-123"
TEST_URL = "https://connection.keboola.com"
COMP_ID = "keboola.ex-db-snowflake"
CFG_ID = "my-config"

SAMPLE_ENTRIES = [
    {
        "id": 1,
        "key": "KBC.configuration.folderName",
        "value": "extractors",
        "provider": "user",
        "timestamp": "2025-01-01T00:00:00Z",
    },
    {
        "id": 2,
        "key": "my.custom.tag",
        "value": "production",
        "provider": "user",
        "timestamp": "2025-01-01T00:00:00Z",
    },
]


def _setup_store(tmp_path: Path) -> ConfigStore:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url=TEST_URL,
            token=TEST_TOKEN,
            project_name="Prod",
            project_id=1,
        ),
    )
    return store


def _invoke(store: ConfigStore, mock_cfg_svc: MagicMock, *args: str) -> Any:
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.ProjectService") as MockProjSvc,
        patch("keboola_agent_cli.cli.ConfigService") as MockCfgSvc,
        patch("keboola_agent_cli.cli.JobService") as MockJobSvc,
        patch("keboola_agent_cli.cli.WorkspaceService") as MockWsSvc,
    ):
        MockStore.return_value = store
        MockProjSvc.return_value = ProjectService(config_store=store)
        MockCfgSvc.return_value = mock_cfg_svc
        MockJobSvc.return_value = JobService(config_store=store)
        MockWsSvc.return_value = WorkspaceService(config_store=store)
        return runner.invoke(app, list(args))


# ── metadata-list ──────────────────────────────────────────────────────


class TestConfigMetadataList:
    def test_list_json_success(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.list_config_metadata.return_value = {
            "project_alias": "prod",
            "component_id": COMP_ID,
            "config_id": CFG_ID,
            "branch_id": None,
            "metadata": SAMPLE_ENTRIES,
        }
        result = _invoke(
            store,
            mock_svc,
            "--json",
            "config",
            "metadata-list",
            "--project",
            "prod",
            "--component-id",
            COMP_ID,
            "--config-id",
            CFG_ID,
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert len(data["data"]["metadata"]) == 2
        mock_svc.list_config_metadata.assert_called_once_with(
            alias="prod", component_id=COMP_ID, config_id=CFG_ID, branch_id=None
        )

    def test_list_empty(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.list_config_metadata.return_value = {
            "project_alias": "prod",
            "component_id": COMP_ID,
            "config_id": CFG_ID,
            "branch_id": None,
            "metadata": [],
        }
        result = _invoke(
            store,
            mock_svc,
            "--json",
            "config",
            "metadata-list",
            "--project",
            "prod",
            "--component-id",
            COMP_ID,
            "--config-id",
            CFG_ID,
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["metadata"] == []

    def test_list_api_error(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.list_config_metadata.side_effect = KeboolaApiError(
            message="Not found", status_code=404, error_code="NOT_FOUND", retryable=False
        )
        result = _invoke(
            store,
            mock_svc,
            "--json",
            "config",
            "metadata-list",
            "--project",
            "prod",
            "--component-id",
            COMP_ID,
            "--config-id",
            CFG_ID,
        )
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["status"] == "error"

    def test_list_config_error_exits_5(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.list_config_metadata.side_effect = ConfigError("No active branch")
        result = _invoke(
            store,
            mock_svc,
            "--json",
            "config",
            "metadata-list",
            "--project",
            "prod",
            "--component-id",
            COMP_ID,
            "--config-id",
            CFG_ID,
        )
        assert result.exit_code == 5
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["error"]["code"] == "CONFIG_ERROR"


# ── get-metadata ───────────────────────────────────────────────────────


class TestConfigGetMetadata:
    def test_get_json_success(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.get_config_metadata_value.return_value = {
            "project_alias": "prod",
            "component_id": COMP_ID,
            "config_id": CFG_ID,
            "branch_id": None,
            "key": "my.custom.tag",
            "value": "production",
            "metadata_id": 2,
        }
        result = _invoke(
            store,
            mock_svc,
            "--json",
            "config",
            "get-metadata",
            "--project",
            "prod",
            "--component-id",
            COMP_ID,
            "--config-id",
            CFG_ID,
            "--key",
            "my.custom.tag",
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["value"] == "production"
        assert data["data"]["metadata_id"] == 2

    def test_get_not_found_exits_1(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.get_config_metadata_value.side_effect = KeboolaApiError(
            message="Metadata key 'missing' not found.",
            status_code=404,
            error_code="NOT_FOUND",
            retryable=False,
        )
        result = _invoke(
            store,
            mock_svc,
            "--json",
            "config",
            "get-metadata",
            "--project",
            "prod",
            "--component-id",
            COMP_ID,
            "--config-id",
            CFG_ID,
            "--key",
            "missing",
        )
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["error"]["code"] == "NOT_FOUND"

    def test_get_config_error_exits_5(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.get_config_metadata_value.side_effect = ConfigError("Branch resolution failed")
        result = _invoke(
            store,
            mock_svc,
            "--json",
            "config",
            "get-metadata",
            "--project",
            "prod",
            "--component-id",
            COMP_ID,
            "--config-id",
            CFG_ID,
            "--key",
            "k",
        )
        assert result.exit_code == 5
        data = json.loads(result.output)
        assert data["error"]["code"] == "CONFIG_ERROR"

    def test_set_folder_unsupported_stack_exits_5_feature_not_enabled(self, tmp_path: Path) -> None:
        """A stack without the folder field is an error, never a success line."""
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.set_config_folder.side_effect = FeatureNotEnabledError("no folder field")
        result = _invoke(
            store,
            mock_svc,
            "--json",
            "config",
            "set-folder",
            "--project",
            "prod",
            "--component-id",
            COMP_ID,
            "--config-id",
            CFG_ID,
            "--name",
            "Folder",
        )
        assert result.exit_code == 5
        data = json.loads(result.output)
        assert data["error"]["code"] == "FEATURE_NOT_ENABLED"


# ── set-metadata ───────────────────────────────────────────────────────


class TestConfigSetMetadata:
    def test_set_json_success(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.set_config_metadata.return_value = {
            "project_alias": "prod",
            "component_id": COMP_ID,
            "config_id": CFG_ID,
            "branch_id": None,
            "key": "env",
            "value": "production",
            "result": [{"id": 5, "key": "env", "value": "production"}],
            "message": "Metadata 'env' set on config ...",
        }
        result = _invoke(
            store,
            mock_svc,
            "--json",
            "config",
            "set-metadata",
            "--project",
            "prod",
            "--component-id",
            COMP_ID,
            "--config-id",
            CFG_ID,
            "--key",
            "env",
            "--value",
            "production",
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["data"]["key"] == "env"
        mock_svc.set_config_metadata.assert_called_once_with(
            alias="prod",
            component_id=COMP_ID,
            config_id=CFG_ID,
            key="env",
            value="production",
            branch_id=None,
        )

    def test_set_api_error(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.set_config_metadata.side_effect = KeboolaApiError(
            message="Config not found", status_code=404, error_code="NOT_FOUND", retryable=False
        )
        result = _invoke(
            store,
            mock_svc,
            "--json",
            "config",
            "set-metadata",
            "--project",
            "prod",
            "--component-id",
            COMP_ID,
            "--config-id",
            "bad-id",
            "--key",
            "k",
            "--value",
            "v",
        )
        assert result.exit_code == 1

    def test_set_config_error_exits_5(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.set_config_metadata.side_effect = ConfigError("Branch resolution failed")
        result = _invoke(
            store,
            mock_svc,
            "--json",
            "config",
            "set-metadata",
            "--project",
            "prod",
            "--component-id",
            COMP_ID,
            "--config-id",
            CFG_ID,
            "--key",
            "k",
            "--value",
            "v",
        )
        assert result.exit_code == 5
        data = json.loads(result.output)
        assert data["error"]["code"] == "CONFIG_ERROR"


# ── delete-metadata ────────────────────────────────────────────────────


class TestConfigDeleteMetadata:
    def test_delete_with_yes_flag(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.delete_config_metadata.return_value = {
            "project_alias": "prod",
            "component_id": COMP_ID,
            "config_id": CFG_ID,
            "branch_id": None,
            "metadata_id": 2,
            "message": "Metadata ID 2 deleted.",
        }
        result = _invoke(
            store,
            mock_svc,
            "--json",
            "config",
            "delete-metadata",
            "--project",
            "prod",
            "--component-id",
            COMP_ID,
            "--config-id",
            CFG_ID,
            "--metadata-id",
            "2",
            "--yes",
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["metadata_id"] == 2
        mock_svc.delete_config_metadata.assert_called_once_with(
            alias="prod",
            component_id=COMP_ID,
            config_id=CFG_ID,
            metadata_id=2,
            branch_id=None,
        )

    def test_delete_config_error_exits_5(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.delete_config_metadata.side_effect = ConfigError("Branch resolution failed")
        result = _invoke(
            store,
            mock_svc,
            "--json",
            "config",
            "delete-metadata",
            "--project",
            "prod",
            "--component-id",
            COMP_ID,
            "--config-id",
            CFG_ID,
            "--metadata-id",
            "1",
            "--yes",
        )
        assert result.exit_code == 5
        data = json.loads(result.output)
        assert data["error"]["code"] == "CONFIG_ERROR"

    def test_delete_api_error(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.delete_config_metadata.side_effect = KeboolaApiError(
            message="Not found", status_code=404, error_code="NOT_FOUND", retryable=False
        )
        result = _invoke(
            store,
            mock_svc,
            "--json",
            "config",
            "delete-metadata",
            "--project",
            "prod",
            "--component-id",
            COMP_ID,
            "--config-id",
            CFG_ID,
            "--metadata-id",
            "999",
            "--yes",
        )
        assert result.exit_code == 1


# ── set-folder ─────────────────────────────────────────────────────────


class TestConfigSetFolder:
    def test_set_folder_success(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.set_config_folder.return_value = {
            "project_alias": "prod",
            "component_id": COMP_ID,
            "config_id": CFG_ID,
            "branch_id": None,
            "folder": "My Folder",
            "configuration": {"id": CFG_ID, "name": "Cfg", "folder": "My Folder"},
            "message": "Folder 'My Folder' set on config ...",
        }
        result = _invoke(
            store,
            mock_svc,
            "--json",
            "config",
            "set-folder",
            "--project",
            "prod",
            "--component-id",
            COMP_ID,
            "--config-id",
            CFG_ID,
            "--name",
            "My Folder",
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["folder"] == "My Folder"
        mock_svc.set_config_folder.assert_called_once_with(
            alias="prod",
            component_id=COMP_ID,
            config_id=CFG_ID,
            folder_name="My Folder",
            branch_id=None,
        )

    def test_set_folder_config_error_exits_5(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.set_config_folder.side_effect = ConfigError("Branch resolution failed")
        result = _invoke(
            store,
            mock_svc,
            "--json",
            "config",
            "set-folder",
            "--project",
            "prod",
            "--component-id",
            COMP_ID,
            "--config-id",
            CFG_ID,
            "--name",
            "Bad Folder",
        )
        assert result.exit_code == 5
        data = json.loads(result.output)
        assert data["error"]["code"] == "CONFIG_ERROR"

    def test_set_folder_empty_clears(self, tmp_path: Path) -> None:
        """Empty folder name is a valid call (clearing the folder)."""
        store = _setup_store(tmp_path)
        mock_svc = MagicMock()
        mock_svc.set_config_folder.return_value = {
            "project_alias": "prod",
            "component_id": COMP_ID,
            "config_id": CFG_ID,
            "branch_id": None,
            "folder": "",
            "message": "Folder '' set.",
        }
        result = _invoke(
            store,
            mock_svc,
            "--json",
            "config",
            "set-folder",
            "--project",
            "prod",
            "--component-id",
            COMP_ID,
            "--config-id",
            CFG_ID,
            "--name",
            "",
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["folder"] == ""
        mock_svc.set_config_folder.assert_called_once_with(
            alias="prod",
            component_id=COMP_ID,
            config_id=CFG_ID,
            folder_name="",
            branch_id=None,
        )


# ── ConfigService unit tests (mocked client) ──────────────────────────


class TestConfigServiceMetadata:
    """Test ConfigService metadata methods with a mocked KeboolaClient."""

    def _make_service(self, tmp_path: Path) -> tuple[ConfigService, MagicMock]:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)
        store.add_project(
            "prod",
            # active_branch_id set so _metadata_branch_id skips the API call
            ProjectConfig(
                stack_url=TEST_URL,
                token=TEST_TOKEN,
                project_name="Prod",
                project_id=1,
                active_branch_id=1,
            ),
        )
        mock_client = MagicMock()
        mock_client.close = MagicMock()
        svc = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )
        return svc, mock_client

    def test_list_config_metadata_sorted(self, tmp_path: Path) -> None:
        svc, mock_client = self._make_service(tmp_path)
        mock_client.list_config_metadata.return_value = [
            {"id": 2, "key": "z.key", "value": "b"},
            {"id": 1, "key": "a.key", "value": "a"},
        ]
        result = svc.list_config_metadata("prod", COMP_ID, CFG_ID)
        assert result["metadata"][0]["key"] == "a.key"
        assert result["metadata"][1]["key"] == "z.key"
        mock_client.list_config_metadata.assert_called_once_with(COMP_ID, CFG_ID, branch_id=1)
        mock_client.close.assert_called_once()

    def test_get_config_metadata_value_found(self, tmp_path: Path) -> None:
        svc, mock_client = self._make_service(tmp_path)
        mock_client.list_config_metadata.return_value = [
            {"id": 5, "key": "my.key", "value": "hello", "provider": "user"},
        ]
        result = svc.get_config_metadata_value("prod", COMP_ID, CFG_ID, "my.key")
        assert result["value"] == "hello"
        assert result["metadata_id"] == 5

    def test_get_config_metadata_value_not_found(self, tmp_path: Path) -> None:
        svc, mock_client = self._make_service(tmp_path)
        mock_client.list_config_metadata.return_value = []
        with pytest.raises(KeboolaApiError) as exc_info:
            svc.get_config_metadata_value("prod", COMP_ID, CFG_ID, "missing")
        assert exc_info.value.error_code == "NOT_FOUND"

    def test_set_config_metadata_wire_shape(self, tmp_path: Path) -> None:
        svc, mock_client = self._make_service(tmp_path)
        mock_client.set_config_metadata.return_value = [{"id": 7, "key": "env", "value": "prod"}]
        result = svc.set_config_metadata("prod", COMP_ID, CFG_ID, key="env", value="prod")
        assert result["key"] == "env"
        assert result["value"] == "prod"
        # Verify the client receives entries as list of tuples
        mock_client.set_config_metadata.assert_called_once_with(
            COMP_ID, CFG_ID, entries=[("env", "prod")], branch_id=1
        )

    def test_delete_config_metadata(self, tmp_path: Path) -> None:
        svc, mock_client = self._make_service(tmp_path)
        mock_client.delete_config_metadata.return_value = None
        result = svc.delete_config_metadata("prod", COMP_ID, CFG_ID, metadata_id=7)
        assert result["metadata_id"] == 7
        mock_client.delete_config_metadata.assert_called_once_with(COMP_ID, CFG_ID, 7, branch_id=1)
        mock_client.close.assert_called_once()

    def test_set_config_folder_writes_the_folder_field(self, tmp_path: Path) -> None:
        """set-folder PUTs the config's ``folder`` field, never the metadata key."""
        svc, mock_client = self._make_service(tmp_path)
        mock_client.update_config.return_value = {
            "id": CFG_ID,
            "name": "Cfg",
            "folder": "My Group",
        }
        result = svc.set_config_folder("prod", COMP_ID, CFG_ID, folder_name=" My Group ")
        mock_client.update_config.assert_called_once_with(
            component_id=COMP_ID,
            config_id=CFG_ID,
            folder=" My Group ",
            branch_id=1,
        )
        mock_client.set_config_metadata.assert_not_called()
        assert result["folder"] == "My Group"
        # The metadata-era keys are removed since 1.0.0.
        assert not {"key", "value", "result"} & result.keys()
        assert result["configuration"] == {"id": CFG_ID, "name": "Cfg", "folder": "My Group"}
        assert result["branch_id"] == 1

    def test_set_config_folder_clear_reports_empty_value(self, tmp_path: Path) -> None:
        """An empty name clears the folder: the API answers null, folder is ''."""
        svc, mock_client = self._make_service(tmp_path)
        mock_client.update_config.return_value = {"id": CFG_ID, "name": "Cfg", "folder": None}
        result = svc.set_config_folder("prod", COMP_ID, CFG_ID, folder_name="")
        assert mock_client.update_config.call_args.kwargs["folder"] == ""
        assert result["folder"] == ""
        assert result["configuration"]["folder"] is None
        assert "cleared" in result["message"]

    def test_set_config_folder_fails_when_response_has_no_folder(self, tmp_path: Path) -> None:
        """A stack without the field ignores the param: no false success, no metadata write."""
        svc, mock_client = self._make_service(tmp_path)
        mock_client.update_config.return_value = {"id": CFG_ID, "name": "Cfg"}
        with pytest.raises(FeatureNotEnabledError, match="folder field"):
            svc.set_config_folder("prod", COMP_ID, CFG_ID, folder_name="My Group")
        mock_client.set_config_metadata.assert_not_called()
