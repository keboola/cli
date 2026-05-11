"""Tests for storage truncate-table: client, service, and CLI."""

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

TEST_TOKEN = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"


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


class TestTruncateTableClient:
    """Tests for KeboolaClient.truncate_table() - HTTP layer."""

    def test_correct_url_and_query_params(self, httpx_mock) -> None:
        """DELETE /v2/storage/tables/{id}/rows?allowTruncate=1.

        The Storage API requires the allowTruncate=1 safety opt-in but
        REJECTS async=true on this endpoint (verified live 2026-05-11
        on connection.europe-west3.gcp.keboola.com); the endpoint is
        inherently async and returns a queued job (HTTP 202). For unit
        coverage, this test returns status=success directly so
        _wait_for_storage_job's fast-path is exercised.
        """
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tables/in.c-foo.data/rows?allowTruncate=1",
            method="DELETE",
            json={
                "id": 386488069,
                "status": "success",
                "operationName": "tableRowsDelete",
            },
            status_code=200,
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        result = client.truncate_table(table_id="in.c-foo.data")
        client.close()

        assert result["status"] == "success"
        assert result["operationName"] == "tableRowsDelete"

    def test_branch_prefix_in_url(self, httpx_mock) -> None:
        """branch_id=42 routes through /v2/storage/branch/42/tables/.../rows."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/42/tables/in.c-foo.data/rows?allowTruncate=1",
            method="DELETE",
            json={"id": 1, "status": "success", "operationName": "tableRowsDelete"},
            status_code=200,
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        client.truncate_table(table_id="in.c-foo.data", branch_id=42)
        client.close()

    def test_polls_async_job_to_completion(self, httpx_mock) -> None:
        """Initial status=waiting triggers GET /v2/storage/jobs/{id} until success.

        Dev-branch path: the helper polls until the job reaches a terminal
        state, then returns the final job dict.
        """
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/77/tables/in.c-foo.data/rows?allowTruncate=1",
            method="DELETE",
            json={"id": 555, "status": "waiting", "operationName": "tableRowsDelete"},
            status_code=200,
        )
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/jobs/555",
            method="GET",
            json={"id": 555, "status": "success", "operationName": "tableRowsDelete"},
            status_code=200,
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        result = client.truncate_table(table_id="in.c-foo.data", branch_id=77)
        client.close()

        assert result["status"] == "success"
        assert result["id"] == 555

    def test_url_encoding_for_special_characters(self, httpx_mock) -> None:
        """Table IDs with dots and dashes are URL-encoded in the path."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tables/in.c-bucket-with-dashes.tbl/rows?allowTruncate=1",
            method="DELETE",
            json={"id": 1, "status": "success"},
            status_code=200,
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        client.truncate_table(table_id="in.c-bucket-with-dashes.tbl")
        client.close()

    def test_api_error_propagates(self, httpx_mock) -> None:
        """Storage API 4xx propagates as KeboolaApiError."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tables/in.c-foo.x/rows?allowTruncate=1",
            method="DELETE",
            json={"error": "Table in.c-foo.x not found"},
            status_code=404,
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        with pytest.raises(KeboolaApiError):
            client.truncate_table(table_id="in.c-foo.x")
        client.close()


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------


class TestTruncateTableService:
    """Tests for StorageService.truncate_tables()."""

    def test_single_table_success(self, tmp_path: Path) -> None:
        """Happy path: capture rows_before, truncate, report rows_after=0."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = {
            "id": "in.c-foo.data",
            "rowsCount": 100,
        }
        mock_client.truncate_table.return_value = {"status": "success"}
        service = _make_service(store, mock_client)

        result = service.truncate_tables(alias="test", table_ids=["in.c-foo.data"])

        assert result["failed"] == []
        assert result["dry_run"] is False
        assert result["project_alias"] == "test"
        assert len(result["truncated"]) == 1
        entry = result["truncated"][0]
        assert entry["table_id"] == "in.c-foo.data"
        assert entry["rows_before"] == 100
        assert entry["rows_after"] == 0
        assert entry["branch_id"] is None
        mock_client.truncate_table.assert_called_once_with("in.c-foo.data", branch_id=None)

    def test_branch_id_carried_on_entry(self, tmp_path: Path) -> None:
        """Each truncated[] entry records the branch_id used for the call."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = {"rowsCount": 50}
        mock_client.truncate_table.return_value = {"status": "success"}
        service = _make_service(store, mock_client)

        result = service.truncate_tables(alias="test", table_ids=["in.c-foo.data"], branch_id=42)

        assert result["truncated"][0]["branch_id"] == 42
        mock_client.truncate_table.assert_called_once_with("in.c-foo.data", branch_id=42)

    def test_rows_count_non_numeric_defaults_to_zero(self, tmp_path: Path) -> None:
        """Defensive: a non-int rowsCount must not crash the batch."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = {"rowsCount": "not-a-number"}
        mock_client.truncate_table.return_value = {"status": "success"}
        service = _make_service(store, mock_client)

        result = service.truncate_tables(alias="test", table_ids=["in.c-foo.data"])

        assert result["failed"] == []
        assert result["truncated"][0]["rows_before"] == 0
        assert result["truncated"][0]["rows_after"] == 0

    def test_rows_count_missing_defaults_to_zero(self, tmp_path: Path) -> None:
        """Defensive: missing rowsCount key → rows_before=0, not KeyError."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = {"id": "in.c-foo.data"}
        mock_client.truncate_table.return_value = {"status": "success"}
        service = _make_service(store, mock_client)

        result = service.truncate_tables(alias="test", table_ids=["in.c-foo.data"])

        assert result["truncated"][0]["rows_before"] == 0

    def test_batch_partial_failure(self, tmp_path: Path) -> None:
        """One missing table does not abort the batch."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        # First detail succeeds; second raises NOT_FOUND.
        mock_client.get_table_detail.side_effect = [
            {"rowsCount": 42},
            KeboolaApiError("Table not found", status_code=404, error_code="NOT_FOUND"),
        ]
        mock_client.truncate_table.return_value = {"status": "success"}
        service = _make_service(store, mock_client)

        result = service.truncate_tables(
            alias="test",
            table_ids=["in.c-foo.data", "in.c-foo.missing"],
        )

        assert len(result["truncated"]) == 1
        assert result["truncated"][0]["table_id"] == "in.c-foo.data"
        assert len(result["failed"]) == 1
        assert result["failed"][0]["id"] == "in.c-foo.missing"
        assert "not found" in result["failed"][0]["error"].lower()
        # truncate_table must NOT have been called for the missing table.
        mock_client.truncate_table.assert_called_once()

    def test_dry_run_skips_truncate(self, tmp_path: Path) -> None:
        """dry_run captures rows_before via get_table_detail but never truncates."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = {"rowsCount": 999}
        service = _make_service(store, mock_client)

        result = service.truncate_tables(
            alias="test",
            table_ids=["in.c-foo.data"],
            dry_run=True,
        )

        assert result["dry_run"] is True
        assert result["truncated"] == []
        assert result["failed"] == []
        assert len(result["would_truncate"]) == 1
        wt = result["would_truncate"][0]
        assert wt["table_id"] == "in.c-foo.data"
        assert wt["rows_before"] == 999
        assert wt["branch_id"] is None
        mock_client.truncate_table.assert_not_called()

    def test_branch_id_propagates_to_client(self, tmp_path: Path) -> None:
        """branch_id flows into both get_table_detail and truncate_table."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = {"rowsCount": 1}
        mock_client.truncate_table.return_value = {"status": "success"}
        service = _make_service(store, mock_client)

        service.truncate_tables(alias="test", table_ids=["in.c-foo.data"], branch_id=99)

        mock_client.get_table_detail.assert_called_once_with("in.c-foo.data", branch_id=99)
        mock_client.truncate_table.assert_called_once_with("in.c-foo.data", branch_id=99)

    def test_unknown_project(self, tmp_path: Path) -> None:
        """Unknown alias surfaces as ConfigError from resolve_projects()."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        with pytest.raises(ConfigError):
            service.truncate_tables(alias="nonexistent", table_ids=["in.c-foo.data"])

    def test_client_closed_even_when_truncate_raises(self, tmp_path: Path) -> None:
        """try/finally contract: client.close() runs even on API failure."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = {"rowsCount": 1}
        mock_client.truncate_table.side_effect = KeboolaApiError(
            "Permission denied", status_code=403, error_code="STORAGE_FORBIDDEN"
        )
        service = _make_service(store, mock_client)

        result = service.truncate_tables(alias="test", table_ids=["in.c-foo.data"])

        # API errors do not propagate -- they accumulate in failed[].
        assert result["truncated"] == []
        assert result["failed"][0]["id"] == "in.c-foo.data"
        # Regression guard: close() always runs.
        mock_client.close.assert_called_once()

    def test_empty_table_ids_list(self, tmp_path: Path) -> None:
        """Empty input → empty envelope, no client calls."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        result = service.truncate_tables(alias="test", table_ids=[])

        assert result["truncated"] == []
        assert result["failed"] == []
        assert result["dry_run"] is False
        mock_client.get_table_detail.assert_not_called()
        mock_client.truncate_table.assert_not_called()


# ---------------------------------------------------------------------------
# CLI layer
# ---------------------------------------------------------------------------


class TestTruncateTableCLI:
    """CLI tests for `kbagent storage truncate-table`."""

    def _project_with_active_branch(self, store: ConfigStore, branch_id: int) -> None:
        config = store.load()
        config.projects["test"].active_branch_id = branch_id
        store.save(config)

    def test_json_happy_path(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.truncate_tables.return_value = {
                "truncated": [
                    {
                        "table_id": "in.c-foo.data",
                        "rows_before": 1230,
                        "rows_after": 0,
                        "branch_id": None,
                    }
                ],
                "failed": [],
                "dry_run": False,
                "project_alias": "test",
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "truncate-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-foo.data",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["truncated"][0]["table_id"] == "in.c-foo.data"
        assert data["truncated"][0]["rows_before"] == 1230
        assert data["truncated"][0]["rows_after"] == 0
        svc.truncate_tables.assert_called_once_with(
            alias="test",
            table_ids=["in.c-foo.data"],
            branch_id=None,
        )

    def test_dry_run_json(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.truncate_tables.return_value = {
                "truncated": [],
                "failed": [],
                "would_truncate": [
                    {"table_id": "in.c-foo.data", "rows_before": 7, "branch_id": None}
                ],
                "dry_run": True,
                "project_alias": "test",
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "truncate-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-foo.data",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["dry_run"] is True
        assert data["would_truncate"][0]["rows_before"] == 7
        call_kwargs = svc.truncate_tables.call_args.kwargs
        assert call_kwargs["dry_run"] is True

    def test_branch_flag_passes_through(self, tmp_path: Path) -> None:
        """--branch 42 overrides any active branch and reaches the service."""
        store = _make_store(tmp_path)
        self._project_with_active_branch(store, 100)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.truncate_tables.return_value = {
                "truncated": [
                    {
                        "table_id": "in.c-foo.data",
                        "rows_before": 1,
                        "rows_after": 0,
                        "branch_id": 42,
                    }
                ],
                "failed": [],
                "dry_run": False,
                "project_alias": "test",
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "truncate-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-foo.data",
                    "--branch",
                    "42",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, result.output
        call_kwargs = svc.truncate_tables.call_args.kwargs
        assert call_kwargs["branch_id"] == 42

    def test_active_branch_used_when_no_flag(self, tmp_path: Path) -> None:
        """When no --branch is passed, the project's active_branch_id is used.

        Destructive writes (including truncate-table) honor the active branch
        unlike pure-read storage commands which skip it.
        """
        store = _make_store(tmp_path)
        self._project_with_active_branch(store, 15931)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.truncate_tables.return_value = {
                "truncated": [
                    {
                        "table_id": "in.c-foo.data",
                        "rows_before": 0,
                        "rows_after": 0,
                        "branch_id": 15931,
                    }
                ],
                "failed": [],
                "dry_run": False,
                "project_alias": "test",
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "truncate-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-foo.data",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, result.output
        call_kwargs = svc.truncate_tables.call_args.kwargs
        assert call_kwargs["branch_id"] == 15931

    def test_failed_truncation_exits_1(self, tmp_path: Path) -> None:
        """A non-empty failed[] returns exit code 1."""
        store = _make_store(tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.truncate_tables.return_value = {
                "truncated": [],
                "failed": [{"id": "in.c-foo.missing", "error": "Table not found"}],
                "dry_run": False,
                "project_alias": "test",
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "truncate-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-foo.missing",
                    "--yes",
                ],
            )

        assert result.exit_code == 1
        data = json.loads(result.output)["data"]
        assert data["failed"][0]["id"] == "in.c-foo.missing"

    def test_config_error_exits_5(self, tmp_path: Path) -> None:
        """ConfigError from the service surfaces as exit 5 with CONFIG_ERROR."""
        store = _make_store(tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.truncate_tables.side_effect = ConfigError("Unknown project alias")
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "truncate-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-foo.data",
                    "--yes",
                ],
            )

        assert result.exit_code == 5
        payload = json.loads(result.output)
        assert payload["status"] == "error"
