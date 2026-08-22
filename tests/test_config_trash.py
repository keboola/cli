"""Tests for the config trash safety net: delete preflight, restore, trash-list.

The Storage API overloads ``DELETE .../configs/{id}``: on a live configuration
it soft-deletes into the trash, but on a configuration ALREADY in the trash it
purges permanently -- versions, rows and metadata gone. A timed-out delete
followed by a retry is exactly that second call, so ``delete_config`` must
locate the configuration first and never issue a DELETE at anything but a
live config. The assertions on ``client.delete_config.assert_not_called()``
are the point of this file: they prove the purge call cannot happen.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner, Result

from helpers import setup_single_project
from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.project_service import ProjectService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

runner = CliRunner()


def _not_found() -> KeboolaApiError:
    return KeboolaApiError(
        message="Configuration not found",
        error_code=ErrorCode.NOT_FOUND,
        status_code=404,
    )


def _make_service(
    tmp_config_dir: Path,
    *,
    live: bool,
    in_trash: bool,
) -> tuple[ConfigService, MagicMock]:
    """ConfigService over a mock client representing one config state."""
    store = setup_single_project(tmp_config_dir)
    client = MagicMock()
    if live:
        client.get_config_detail.return_value = {"id": "cfg-1", "name": "Probe"}
    else:
        client.get_config_detail.side_effect = _not_found()
    client.list_deleted_configs.return_value = (
        [{"id": "cfg-1", "name": "Probe", "version": 3}] if in_trash else []
    )
    client.restore_config.return_value = {"id": "cfg-1", "name": "Probe", "version": 3}
    service = ConfigService(config_store=store, client_factory=lambda url, token: client)
    return service, client


class TestDeletePreflight:
    """delete_config never sends the DELETE that would purge a trashed config."""

    def test_live_config_is_deleted(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir, live=True, in_trash=False)
        result = service.delete_config("prod", "keboola.comp", "cfg-1")
        assert result["status"] == "deleted"
        client.delete_config.assert_called_once()

    def test_trashed_config_is_never_deleted_again(self, tmp_config_dir: Path) -> None:
        """The retry scenario: config already in trash -> refuse the second DELETE."""
        service, client = _make_service(tmp_config_dir, live=False, in_trash=True)
        result = service.delete_config("prod", "keboola.comp", "cfg-1")
        assert result["status"] == "already_in_trash"
        assert "restore" in result["message"]
        client.delete_config.assert_not_called()

    def test_missing_config_raises_not_found(self, tmp_config_dir: Path) -> None:
        """Absent from live AND trash -> NOT_FOUND, and no blind DELETE either."""
        service, client = _make_service(tmp_config_dir, live=False, in_trash=False)
        with pytest.raises(KeboolaApiError) as exc_info:
            service.delete_config("prod", "keboola.comp", "cfg-1")
        assert exc_info.value.error_code == ErrorCode.NOT_FOUND
        client.delete_config.assert_not_called()

    def test_non_404_lookup_error_propagates(self, tmp_config_dir: Path) -> None:
        """A 500 on the preflight must not be misread as 'not live' -- it aborts."""
        service, client = _make_service(tmp_config_dir, live=True, in_trash=False)
        client.get_config_detail.side_effect = KeboolaApiError(
            message="boom", error_code=ErrorCode.API_ERROR, status_code=500
        )
        with pytest.raises(KeboolaApiError):
            service.delete_config("prod", "keboola.comp", "cfg-1")
        client.delete_config.assert_not_called()

    def test_dry_run_on_live_config_writes_nothing(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir, live=True, in_trash=False)
        result = service.delete_config("prod", "keboola.comp", "cfg-1", dry_run=True)
        assert result["status"] == "would_delete"
        assert result["dry_run"] is True
        client.delete_config.assert_not_called()

    def test_dry_run_on_trashed_config_reports_state(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir, live=False, in_trash=True)
        result = service.delete_config("prod", "keboola.comp", "cfg-1", dry_run=True)
        assert result["status"] == "already_in_trash"
        client.delete_config.assert_not_called()


class TestRestore:
    def test_restore_returns_name_and_version(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir, live=False, in_trash=True)
        result = service.restore_config("prod", "keboola.comp", "cfg-1")
        assert result["status"] == "restored"
        assert result["name"] == "Probe"
        assert result["version"] == 3
        client.restore_config.assert_called_once_with(
            component_id="keboola.comp", config_id="cfg-1", branch_id=None
        )


class TestTrashList:
    def test_component_scope_passes_through(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir, live=False, in_trash=True)
        result = service.list_config_trash("prod", component_id="keboola.comp")
        assert result["trash"][0]["config_id"] == "cfg-1"
        assert result["trash"][0]["component_id"] == "keboola.comp"
        client.list_deleted_configs.assert_called_once_with(
            component_id="keboola.comp", branch_id=None
        )

    def test_project_wide_uses_flattened_component_id(self, tmp_config_dir: Path) -> None:
        """Without --component-id the client stamps component_id per entry."""
        service, client = _make_service(tmp_config_dir, live=False, in_trash=False)
        client.list_deleted_configs.return_value = [
            {"id": "a", "name": "A", "version": 1, "component_id": "keboola.x"},
        ]
        result = service.list_config_trash("prod")
        assert result["trash"][0]["component_id"] == "keboola.x"


# --- CLI layer ---------------------------------------------------------------


def _setup_store(config_dir: Path) -> ConfigStore:
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
            project_name="Production",
            project_id=1234,
        ),
    )
    return store


def _invoke(args: list[str], *, config_dir: Path, svc: MagicMock) -> Result:
    store = _setup_store(config_dir)
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
        patch("keboola_agent_cli.cli.ConfigService") as MockConfigService,
    ):
        MockStore.return_value = store
        MockProjService.return_value = ProjectService(config_store=store)
        MockConfigService.return_value = svc
        return runner.invoke(app, args)


class TestRestoreCli:
    def test_json_envelope(self, tmp_path: Path) -> None:
        svc = MagicMock()
        svc.restore_config.return_value = {
            "status": "restored",
            "project_alias": "prod",
            "component_id": "keboola.comp",
            "config_id": "cfg-1",
            "branch_id": None,
            "name": "Probe",
            "version": 3,
        }
        result = _invoke(
            [
                "--json",
                "config",
                "restore",
                "--project",
                "prod",
                "--component-id",
                "keboola.comp",
                "--config-id",
                "cfg-1",
            ],
            config_dir=tmp_path,
            svc=svc,
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["data"]["status"] == "restored"

    def test_human_mode_renders(self, tmp_path: Path) -> None:
        svc = MagicMock()
        svc.restore_config.return_value = {
            "status": "restored",
            "project_alias": "prod",
            "component_id": "keboola.comp",
            "config_id": "cfg-1",
            "branch_id": None,
            "name": "Probe",
            "version": 3,
        }
        result = _invoke(
            [
                "config",
                "restore",
                "--project",
                "prod",
                "--component-id",
                "keboola.comp",
                "--config-id",
                "cfg-1",
            ],
            config_dir=tmp_path,
            svc=svc,
        )
        assert result.exit_code == 0
        assert "Restored" in result.stdout


class TestTrashListCli:
    def test_json_envelope(self, tmp_path: Path) -> None:
        svc = MagicMock()
        svc.list_config_trash.return_value = {
            "project_alias": "prod",
            "branch_id": None,
            "component_id": None,
            "trash": [
                {
                    "component_id": "keboola.comp",
                    "config_id": "cfg-1",
                    "name": "Probe",
                    "version": 3,
                    "deleted_change_description": None,
                    "deleted_at": None,
                }
            ],
        }
        result = _invoke(
            ["--json", "config", "trash-list", "--project", "prod"],
            config_dir=tmp_path,
            svc=svc,
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["data"]["trash"][0]["config_id"] == "cfg-1"

    def test_human_mode_empty_trash(self, tmp_path: Path) -> None:
        svc = MagicMock()
        svc.list_config_trash.return_value = {
            "project_alias": "prod",
            "branch_id": None,
            "component_id": None,
            "trash": [],
        }
        result = _invoke(
            ["config", "trash-list", "--project", "prod"],
            config_dir=tmp_path,
            svc=svc,
        )
        assert result.exit_code == 0
        assert "empty" in result.stdout


class TestDeleteCliDryRun:
    def test_dry_run_flag_reaches_service(self, tmp_path: Path) -> None:
        svc = MagicMock()
        svc.delete_config.return_value = {
            "status": "would_delete",
            "dry_run": True,
            "project_alias": "prod",
            "component_id": "keboola.comp",
            "config_id": "cfg-1",
            "branch_id": None,
        }
        result = _invoke(
            [
                "config",
                "delete",
                "--project",
                "prod",
                "--component-id",
                "keboola.comp",
                "--config-id",
                "cfg-1",
                "--dry-run",
            ],
            config_dir=tmp_path,
            svc=svc,
        )
        assert result.exit_code == 0
        assert "DRY RUN" in result.stdout
        assert svc.delete_config.call_args.kwargs["dry_run"] is True

    def test_already_in_trash_is_exit_zero(self, tmp_path: Path) -> None:
        """The retry path must stay a success for scripts."""
        svc = MagicMock()
        svc.delete_config.return_value = {
            "status": "already_in_trash",
            "dry_run": False,
            "project_alias": "prod",
            "component_id": "keboola.comp",
            "config_id": "cfg-1",
            "branch_id": None,
            "message": "Configuration 'keboola.comp/cfg-1' is already in the trash; "
            "not deleting again. Use 'kbagent config restore' to bring it back.",
        }
        result = _invoke(
            [
                "config",
                "delete",
                "--project",
                "prod",
                "--component-id",
                "keboola.comp",
                "--config-id",
                "cfg-1",
            ],
            config_dir=tmp_path,
            svc=svc,
        )
        assert result.exit_code == 0
        assert "already in the trash" in result.stdout
