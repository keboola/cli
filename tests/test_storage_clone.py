"""Tests for storage clone-table (pull endpoint): client, service, and CLI."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import AppConfig, ProjectConfig
from keboola_agent_cli.services.storage_service import StorageService

runner = CliRunner()

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"


def _make_store(tmp_path: Path) -> ConfigStore:
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    store = ConfigStore(config_dir=config_dir)
    config = AppConfig(
        projects={
            "test": ProjectConfig(
                stack_url="https://connection.keboola.com",
                token=TEST_TOKEN,
            )
        },
    )
    store.save(config)
    return store


def _make_service(store: ConfigStore, mock_client: MagicMock) -> StorageService:
    return StorageService(
        config_store=store,
        client_factory=lambda url, token: mock_client,
    )


# ---------------------------------------------------------------------------
# Client layer
# ---------------------------------------------------------------------------


class TestPullTableClient:
    """Tests for KeboolaClient.pull_table() - HTTP layer."""

    def test_correct_url_and_no_body(self, httpx_mock) -> None:
        """POSTs to /v2/storage/branch/{branch}/tables/{tid}/pull with no body.

        The Storage API responds with a queued storage job
        (operationName=devBranchTablePull) which the client polls to
        completion. Returning ``status: success`` from the first response
        avoids exercising the poll loop in this unit test.
        """
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/9999/tables/in.c-foo.data/pull",
            method="POST",
            json={
                "id": 388266099,
                "status": "success",
                "operationName": "devBranchTablePull",
                "operationParams": {"branchId": 9999},
            },
            status_code=200,
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        result = client.pull_table(table_id="in.c-foo.data", branch_id=9999)

        # Returned dict is the completed storage job
        assert result["status"] == "success"
        assert result["operationName"] == "devBranchTablePull"

        # The pull endpoint takes no request body (verified live against the API)
        sent_request = httpx_mock.get_request()
        assert sent_request.content == b""
        client.close()

    def test_url_encoding_for_special_characters(self, httpx_mock) -> None:
        """Table IDs with dots/dashes are URL-encoded in the path."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/1/tables/in.c-bucket-with-dashes.tbl/pull",
            method="POST",
            json={"id": 1, "status": "success", "operationName": "devBranchTablePull"},
            status_code=200,
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        client.pull_table(table_id="in.c-bucket-with-dashes.tbl", branch_id=1)
        client.close()

    def test_polls_async_job_to_completion(self, httpx_mock) -> None:
        """If POST returns ``status: waiting``, client polls /v2/storage/jobs/{id}."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/42/tables/in.c-foo.a/pull",
            method="POST",
            json={"id": 555, "status": "waiting", "operationName": "devBranchTablePull"},
            status_code=200,
        )
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/jobs/555",
            method="GET",
            json={"id": 555, "status": "success", "operationName": "devBranchTablePull"},
            status_code=200,
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        result = client.pull_table(table_id="in.c-foo.a", branch_id=42)
        assert result["status"] == "success"
        client.close()

    def test_api_error_propagates(self, httpx_mock) -> None:
        """Storage API 4xx propagates as KeboolaApiError."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/9999/tables/in.c-foo.x/pull",
            method="POST",
            json={"error": "Table not found in the default branch"},
            status_code=404,
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        with pytest.raises(KeboolaApiError):
            client.pull_table(table_id="in.c-foo.x", branch_id=9999)
        client.close()


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------


class TestCloneTableService:
    """Tests for StorageService.clone_table()."""

    def test_success(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.pull_table.return_value = {"status": "ok"}
        service = _make_service(store, mock_client)

        result = service.clone_table(
            alias="test",
            table_id="in.c-foo.data",
            branch_id=9999,
        )

        assert result["project_alias"] == "test"
        assert result["branch_id"] == 9999
        assert result["table_id"] == "in.c-foo.data"
        assert result["dry_run"] is False
        assert result["response"] == {"status": "ok"}
        mock_client.pull_table.assert_called_once_with(
            table_id="in.c-foo.data",
            branch_id=9999,
        )
        mock_client.close.assert_called_once()

    def test_dry_run_skips_client_call(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        result = service.clone_table(
            alias="test",
            table_id="in.c-foo.a",
            branch_id=42,
            dry_run=True,
        )

        assert result["dry_run"] is True
        assert "response" not in result
        mock_client.pull_table.assert_not_called()

    def test_no_branch_raises_config_error(self, tmp_path: Path) -> None:
        """Mandatory branch enforcement: pull is one-way default -> branch."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        with pytest.raises(ConfigError, match="dev branch"):
            service.clone_table(
                alias="test",
                table_id="in.c-foo.a",
                branch_id=None,
            )
        mock_client.pull_table.assert_not_called()

    def test_unknown_project(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        with pytest.raises(ConfigError):
            service.clone_table(
                alias="nonexistent",
                table_id="in.c-foo.a",
                branch_id=42,
            )

    def test_api_error_propagates(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.pull_table.side_effect = KeboolaApiError(
            "Table not found", status_code=404, error_code="NOT_FOUND"
        )
        service = _make_service(store, mock_client)

        with pytest.raises(KeboolaApiError):
            service.clone_table(
                alias="test",
                table_id="in.c-foo.a",
                branch_id=42,
            )
        # Service must close the client even when the API call raises
        # (try/finally contract -- regression guard for the lifecycle).
        mock_client.close.assert_called_once()


# ---------------------------------------------------------------------------
# CLI layer
# ---------------------------------------------------------------------------


class TestCloneTableCLI:
    """CLI tests for `kbagent storage clone-table`."""

    def _project_with_active_branch(self, store: ConfigStore, branch_id: int) -> None:
        config = store.load()
        config.projects["test"].active_branch_id = branch_id
        store.save(config)

    def test_clone_json(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        self._project_with_active_branch(store, 9999)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.clone_table.return_value = {
                "project_alias": "test",
                "branch_id": 9999,
                "table_id": "in.c-foo.data",
                "dry_run": False,
                "response": {"status": "ok"},
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "clone-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-foo.data",
                ],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["table_id"] == "in.c-foo.data"
        assert data["branch_id"] == 9999

        svc.clone_table.assert_called_once_with(
            alias="test",
            table_id="in.c-foo.data",
            branch_id=9999,
            dry_run=False,
        )

    def test_clone_dry_run(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        self._project_with_active_branch(store, 42)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.clone_table.return_value = {
                "project_alias": "test",
                "branch_id": 42,
                "table_id": "in.c-foo.a",
                "dry_run": True,
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "clone-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-foo.a",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["dry_run"] is True
        svc.clone_table.assert_called_once()
        call_kwargs = svc.clone_table.call_args.kwargs
        assert call_kwargs["dry_run"] is True

    def test_clone_explicit_branch_overrides_active(self, tmp_path: Path) -> None:
        """--branch flag takes precedence over project's active_branch_id."""
        store = _make_store(tmp_path)
        self._project_with_active_branch(store, 100)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.clone_table.return_value = {
                "project_alias": "test",
                "branch_id": 555,
                "table_id": "in.c-foo.a",
                "dry_run": False,
                "response": {"status": "ok"},
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "clone-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-foo.a",
                    "--branch",
                    "555",
                ],
            )

        assert result.exit_code == 0, result.output
        call_kwargs = svc.clone_table.call_args.kwargs
        assert call_kwargs["branch_id"] == 555

    def test_clone_missing_branch_fails_clearly(self, tmp_path: Path) -> None:
        """Without an active branch and without --branch, ConfigError -> exit 5."""
        store = _make_store(tmp_path)
        # No active_branch_id set on project

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.clone_table.side_effect = ConfigError("clone-table requires a dev branch.")
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "clone-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-foo.a",
                ],
            )

        assert result.exit_code == 5
        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert "dev branch" in payload["error"]["message"]
