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


class TestDeleteIsNeverRetriedAtTheTransport:
    """The purge path the locate-first guard alone cannot close.

    ``DELETE`` sits in ``RETRY_SAFE_METHODS``, so before this fix the HTTP
    layer repeated a timed-out or 5xx'd config delete on its own -- the server
    trashed the config, the response was lost, and the retry purged it
    permanently before any caller saw a result. The service guard runs ONCE
    per call and cannot see inside that loop, so the opt-out has to live at
    the transport.
    """

    URL = "https://connection.keboola.com/v2/storage/components/keboola.comp/configs/cfg-1"

    def _client(self):
        from keboola_agent_cli.client import KeboolaClient

        return KeboolaClient("https://connection.keboola.com", TEST_TOKEN)

    def test_read_timeout_sends_exactly_one_delete(self, httpx_mock) -> None:
        """Server already trashed it; the lost response must NOT trigger a purge."""
        import httpx

        import keboola_agent_cli.http_base as hb

        # Exactly ONE outcome is registered. If the transport retried, the
        # second attempt would find no mock at all -- so a passing test proves
        # a single DELETE left the client.
        httpx_mock.add_exception(httpx.ReadTimeout("lost"), url=self.URL)

        client = self._client()
        original_sleep = hb.time.sleep
        hb.time.sleep = lambda *a, **k: None  # ty: ignore[invalid-assignment]
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client.delete_config("keboola.comp", "cfg-1")
            assert exc_info.value.error_code == "TIMEOUT"
            sent = [r for r in httpx_mock.get_requests() if r.method == "DELETE"]
            assert len(sent) == 1, f"a second DELETE would purge; sent {len(sent)}"
        finally:
            hb.time.sleep = original_sleep
            client.close()

    def test_server_error_sends_exactly_one_delete(self, httpx_mock) -> None:
        """A 5xx after a successful soft delete is the same trap as a timeout."""
        import keboola_agent_cli.http_base as hb

        httpx_mock.add_response(url=self.URL, status_code=503, text="unavailable")

        client = self._client()
        original_sleep = hb.time.sleep
        hb.time.sleep = lambda *a, **k: None  # ty: ignore[invalid-assignment]
        try:
            with pytest.raises(KeboolaApiError):
                client.delete_config("keboola.comp", "cfg-1")
            sent = [r for r in httpx_mock.get_requests() if r.method == "DELETE"]
            assert len(sent) == 1, f"a second DELETE would purge; sent {len(sent)}"
        finally:
            hb.time.sleep = original_sleep
            client.close()

    def test_default_delete_still_retries(self, httpx_mock) -> None:
        """The opt-out is per call, not a blanket policy change.

        A DELETE without the override keeps the resilience RETRY_SAFE_METHODS
        exists to provide -- deleting a table converges on repeat, so losing
        that would trade one endpoint's safety for every other endpoint's.
        """
        import keboola_agent_cli.http_base as hb

        url = "https://connection.keboola.com/plain-delete"
        httpx_mock.add_response(url=url, status_code=503, text="unavailable")
        httpx_mock.add_response(url=url, status_code=204)

        client = self._client()
        original_sleep = hb.time.sleep
        hb.time.sleep = lambda *a, **k: None  # ty: ignore[invalid-assignment]
        try:
            response = client._do_request("DELETE", "/plain-delete")
            assert response.status_code == 204
            assert len(httpx_mock.get_requests()) == 2
        finally:
            hb.time.sleep = original_sleep
            client.close()

    def test_override_is_what_stops_it(self, httpx_mock) -> None:
        """Same request, retry_safe=False -> exactly one attempt."""
        url = "https://connection.keboola.com/plain-delete"
        httpx_mock.add_response(url=url, status_code=503, text="unavailable")

        client = self._client()
        try:
            with pytest.raises(KeboolaApiError):
                client._do_request("DELETE", "/plain-delete", retry_safe=False)
            assert len(httpx_mock.get_requests()) == 1
        finally:
            client.close()


class TestLocateDoesNotTrustTheStatusCode:
    """A 200 carrying isDeleted must not be read as 'live' and then DELETEd."""

    def test_tombstone_body_reports_trashed(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir, live=True, in_trash=False)
        client.get_config_detail.return_value = {"id": "cfg-1", "isDeleted": True}
        result = service.delete_config("prod", "keboola.comp", "cfg-1")
        assert result["status"] == "already_in_trash"
        client.delete_config.assert_not_called()
