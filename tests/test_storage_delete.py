"""Tests for storage delete-table and delete-bucket commands and service methods."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import KeboolaApiError
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


class TestDeleteTablesService:
    """Tests for StorageService.delete_tables()."""

    def test_single_table_success(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.delete_table.return_value = {"id": 1, "status": "success"}
        service = _make_service(store, mock_client)

        result = service.delete_tables(alias="test", table_ids=["in.c-data.users"])

        assert result["deleted"] == ["in.c-data.users"]
        assert result["failed"] == []
        assert result["dry_run"] is False
        mock_client.delete_table.assert_called_once_with(
            "in.c-data.users", branch_id=None, force=False
        )

    def test_force_flag(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.delete_table.return_value = {"id": 1, "status": "success"}
        service = _make_service(store, mock_client)

        result = service.delete_tables(alias="test", table_ids=["in.c-data.users"], force=True)

        assert result["deleted"] == ["in.c-data.users"]
        assert result["failed"] == []
        mock_client.delete_table.assert_called_once_with(
            "in.c-data.users", branch_id=None, force=True
        )

    def test_batch_partial_failure(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.delete_table.side_effect = [
            {"id": 1, "status": "success"},
            KeboolaApiError("Table not found", status_code=404, error_code="NOT_FOUND"),
        ]
        service = _make_service(store, mock_client)

        result = service.delete_tables(
            alias="test", table_ids=["in.c-data.users", "in.c-data.missing"]
        )

        assert result["deleted"] == ["in.c-data.users"]
        assert len(result["failed"]) == 1
        assert result["failed"][0]["id"] == "in.c-data.missing"

    def test_dry_run(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        result = service.delete_tables(alias="test", table_ids=["in.c-data.users"], dry_run=True)

        assert result["dry_run"] is True
        assert result["would_delete"] == ["in.c-data.users"]
        mock_client.delete_table.assert_not_called()

    def test_unknown_project(self, tmp_path: Path) -> None:
        from keboola_agent_cli.errors import ConfigError

        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        with pytest.raises(ConfigError):
            service.delete_tables(alias="nonexistent", table_ids=["t"])


class TestDeleteBucketsService:
    """Tests for StorageService.delete_buckets()."""

    def test_single_bucket_success(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_bucket_detail.return_value = {"id": "in.c-data"}
        mock_client.delete_bucket.return_value = {"id": 1, "status": "success"}
        service = _make_service(store, mock_client)

        result = service.delete_buckets(alias="test", bucket_ids=["in.c-data"])

        assert result["deleted"] == ["in.c-data"]
        assert result["failed"] == []
        mock_client.delete_bucket.assert_called_once_with("in.c-data", force=False, branch_id=None)

    def test_force_flag(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_bucket_detail.return_value = {"id": "in.c-data"}
        mock_client.delete_bucket.return_value = {"id": 1, "status": "success"}
        service = _make_service(store, mock_client)

        service.delete_buckets(alias="test", bucket_ids=["in.c-data"], force=True)

        mock_client.delete_bucket.assert_called_once_with("in.c-data", force=True, branch_id=None)

    def test_linked_bucket_blocked(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_bucket_detail.return_value = {
            "id": "in.c-linked",
            "sourceBucket": {"id": "in.c-original", "project": {"id": 123, "name": "Source"}},
        }
        service = _make_service(store, mock_client)

        result = service.delete_buckets(alias="test", bucket_ids=["in.c-linked"])

        assert result["deleted"] == []
        assert len(result["failed"]) == 1
        assert "linked" in result["failed"][0]["error"].lower()
        mock_client.delete_bucket.assert_not_called()

    def test_shared_bucket_blocked_without_force(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_bucket_detail.return_value = {
            "id": "in.c-shared",
            "sharing": "organization",
        }
        service = _make_service(store, mock_client)

        result = service.delete_buckets(alias="test", bucket_ids=["in.c-shared"])

        assert result["deleted"] == []
        assert len(result["failed"]) == 1
        assert "shared" in result["failed"][0]["error"].lower()

    def test_shared_bucket_allowed_with_force(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_bucket_detail.return_value = {
            "id": "in.c-shared",
            "sharing": "organization",
        }
        mock_client.delete_bucket.return_value = {"id": 1, "status": "success"}
        service = _make_service(store, mock_client)

        result = service.delete_buckets(alias="test", bucket_ids=["in.c-shared"], force=True)

        assert result["deleted"] == ["in.c-shared"]
        mock_client.delete_bucket.assert_called_once()

    def test_dry_run(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_bucket_detail.return_value = {"id": "in.c-data"}
        service = _make_service(store, mock_client)

        result = service.delete_buckets(alias="test", bucket_ids=["in.c-data"], dry_run=True)

        assert result["dry_run"] is True
        assert result["would_delete"] == ["in.c-data"]
        mock_client.delete_bucket.assert_not_called()

    def test_batch_partial_failure(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_bucket_detail.side_effect = [
            {"id": "in.c-ok"},
            {"id": "in.c-fail"},
        ]
        mock_client.delete_bucket.side_effect = [
            {"id": 1, "status": "success"},
            KeboolaApiError("Bucket has tables", status_code=400, error_code="VALIDATION_ERROR"),
        ]
        service = _make_service(store, mock_client)

        result = service.delete_buckets(alias="test", bucket_ids=["in.c-ok", "in.c-fail"])

        assert result["deleted"] == ["in.c-ok"]
        assert len(result["failed"]) == 1
        assert result["failed"][0]["id"] == "in.c-fail"


class TestDeleteTableCLI:
    """CLI tests for `kbagent storage delete-table`."""

    def test_delete_table_json(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.delete_tables.return_value = {
                "deleted": ["in.c-data.users"],
                "failed": [],
                "dry_run": False,
                "project_alias": "test",
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "delete-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-data.users",
                    "--yes",
                ],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["deleted"] == ["in.c-data.users"]

    def test_delete_table_dry_run(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.delete_tables.return_value = {
                "deleted": [],
                "failed": [],
                "would_delete": ["in.c-data.users"],
                "dry_run": True,
                "project_alias": "test",
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "delete-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-data.users",
                    "--dry-run",
                ],
            )
        assert result.exit_code == 0

    def test_delete_table_force_json(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.delete_tables.return_value = {
                "deleted": ["out.c-shared.users"],
                "failed": [],
                "dry_run": False,
                "project_alias": "test",
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "delete-table",
                    "--project",
                    "test",
                    "--table-id",
                    "out.c-shared.users",
                    "--force",
                    "--yes",
                ],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["deleted"] == ["out.c-shared.users"]
        svc.delete_tables.assert_called_once_with(
            alias="test",
            table_ids=["out.c-shared.users"],
            force=True,
            branch_id=None,
        )

    def test_delete_table_exit_1_on_failure(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.delete_tables.return_value = {
                "deleted": [],
                "failed": [{"id": "in.c-data.x", "error": "not found"}],
                "dry_run": False,
                "project_alias": "test",
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "delete-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-data.x",
                    "--yes",
                ],
            )
        assert result.exit_code == 1


class TestDeleteBucketCLI:
    """CLI tests for `kbagent storage delete-bucket`."""

    def test_delete_bucket_json(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.delete_buckets.return_value = {
                "deleted": ["in.c-data"],
                "failed": [],
                "dry_run": False,
                "project_alias": "test",
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "delete-bucket",
                    "--project",
                    "test",
                    "--bucket-id",
                    "in.c-data",
                    "--yes",
                ],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["deleted"] == ["in.c-data"]

    def test_delete_bucket_force(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.delete_buckets.return_value = {
                "deleted": ["in.c-data"],
                "failed": [],
                "dry_run": False,
                "project_alias": "test",
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "delete-bucket",
                    "--project",
                    "test",
                    "--bucket-id",
                    "in.c-data",
                    "--force",
                    "--yes",
                ],
            )
        assert result.exit_code == 0
        svc.delete_buckets.assert_called_once_with(
            alias="test",
            bucket_ids=["in.c-data"],
            force=True,
            dry_run=False,
            branch_id=None,
        )

    def test_delete_bucket_linked_blocked(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.delete_buckets.return_value = {
                "deleted": [],
                "failed": [{"id": "in.c-linked", "error": "linked bucket"}],
                "dry_run": False,
                "project_alias": "test",
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "delete-bucket",
                    "--project",
                    "test",
                    "--bucket-id",
                    "in.c-linked",
                    "--yes",
                ],
            )
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# delete-column: service layer
# ---------------------------------------------------------------------------


class TestDeleteColumnsService:
    """Tests for StorageService.delete_columns()."""

    def test_single_column_success(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.delete_column.return_value = None
        service = _make_service(store, mock_client)

        result = service.delete_columns(alias="test", table_id="in.c-data.users", columns=["age"])

        assert result["deleted"] == ["age"]
        assert result["failed"] == []
        assert result["dry_run"] is False
        assert result["table_id"] == "in.c-data.users"
        mock_client.delete_column.assert_called_once_with(
            "in.c-data.users", "age", branch_id=None, force=False
        )

    def test_batch_multiple_columns(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.delete_column.return_value = None
        service = _make_service(store, mock_client)

        result = service.delete_columns(
            alias="test", table_id="in.c-data.users", columns=["age", "email"]
        )

        assert result["deleted"] == ["age", "email"]
        assert result["failed"] == []
        assert mock_client.delete_column.call_count == 2

    def test_batch_partial_failure(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.delete_column.side_effect = [
            None,
            KeboolaApiError("Column not found", status_code=404, error_code="NOT_FOUND"),
        ]
        service = _make_service(store, mock_client)

        result = service.delete_columns(
            alias="test", table_id="in.c-data.users", columns=["age", "missing"]
        )

        assert result["deleted"] == ["age"]
        assert len(result["failed"]) == 1
        assert result["failed"][0]["column"] == "missing"

    def test_dry_run(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        result = service.delete_columns(
            alias="test", table_id="in.c-data.users", columns=["age"], dry_run=True
        )

        assert result["dry_run"] is True
        assert result["would_delete"] == ["age"]
        assert result["table_id"] == "in.c-data.users"
        mock_client.delete_column.assert_not_called()

    def test_unknown_project(self, tmp_path: Path) -> None:
        from keboola_agent_cli.errors import ConfigError

        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        with pytest.raises(ConfigError):
            service.delete_columns(alias="nonexistent", table_id="in.c-data.t", columns=["col"])


# ---------------------------------------------------------------------------
# delete-column: CLI layer
# ---------------------------------------------------------------------------


class TestDeleteColumnCLI:
    """CLI tests for `kbagent storage delete-column`."""

    def test_delete_column_json(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.delete_columns.return_value = {
                "deleted": ["age"],
                "failed": [],
                "dry_run": False,
                "project_alias": "test",
                "table_id": "in.c-data.users",
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "delete-column",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-data.users",
                    "--column",
                    "age",
                    "--yes",
                ],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["deleted"] == ["age"]
        assert data["table_id"] == "in.c-data.users"

    def test_delete_column_dry_run(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.delete_columns.return_value = {
                "deleted": [],
                "failed": [],
                "would_delete": ["age"],
                "dry_run": True,
                "project_alias": "test",
                "table_id": "in.c-data.users",
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "delete-column",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-data.users",
                    "--column",
                    "age",
                    "--dry-run",
                ],
            )
        assert result.exit_code == 0

    def test_delete_column_multiple(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.delete_columns.return_value = {
                "deleted": ["age", "email"],
                "failed": [],
                "dry_run": False,
                "project_alias": "test",
                "table_id": "in.c-data.users",
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "delete-column",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-data.users",
                    "--column",
                    "age",
                    "--column",
                    "email",
                    "--yes",
                ],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["deleted"] == ["age", "email"]

    def test_delete_column_exit_1_on_failure(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.delete_columns.return_value = {
                "deleted": [],
                "failed": [{"column": "missing", "error": "not found"}],
                "dry_run": False,
                "project_alias": "test",
                "table_id": "in.c-data.users",
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "delete-column",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-data.users",
                    "--column",
                    "missing",
                    "--yes",
                ],
            )
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Branch support tests
# ---------------------------------------------------------------------------


class TestDeleteTableBranch:
    """Tests for --branch support in delete-table."""

    def test_service_passes_branch_id(self, tmp_path: Path) -> None:
        """delete_tables passes branch_id to client.delete_table."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.delete_table.return_value = {"id": 1, "status": "success"}
        service = _make_service(store, mock_client)

        result = service.delete_tables(alias="test", table_ids=["in.c-data.users"], branch_id=42)

        assert result["deleted"] == ["in.c-data.users"]
        mock_client.delete_table.assert_called_once_with(
            "in.c-data.users", branch_id=42, force=False
        )

    def test_cli_branch_flag_json(self, tmp_path: Path) -> None:
        """storage delete-table --branch 42 passes branch_id to service."""
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.delete_tables.return_value = {
                "deleted": ["in.c-data.users"],
                "failed": [],
                "dry_run": False,
                "project_alias": "test",
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "delete-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-data.users",
                    "--branch",
                    "42",
                    "--yes",
                ],
            )
        assert result.exit_code == 0
        call_kwargs = svc.delete_tables.call_args.kwargs
        assert call_kwargs["branch_id"] == 42


class TestDeleteBucketBranch:
    """Tests for --branch support in delete-bucket."""

    def test_service_passes_branch_id(self, tmp_path: Path) -> None:
        """delete_buckets passes branch_id to client calls."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_bucket_detail.return_value = {"id": "in.c-data"}
        mock_client.delete_bucket.return_value = {"id": 1, "status": "success"}
        service = _make_service(store, mock_client)

        result = service.delete_buckets(alias="test", bucket_ids=["in.c-data"], branch_id=99)

        assert result["deleted"] == ["in.c-data"]
        mock_client.get_bucket_detail.assert_called_once_with("in.c-data", branch_id=99)
        mock_client.delete_bucket.assert_called_once_with("in.c-data", force=False, branch_id=99)

    def test_cli_branch_flag_json(self, tmp_path: Path) -> None:
        """storage delete-bucket --branch 99 passes branch_id to service."""
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.delete_buckets.return_value = {
                "deleted": ["in.c-data"],
                "failed": [],
                "dry_run": False,
                "project_alias": "test",
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "delete-bucket",
                    "--project",
                    "test",
                    "--bucket-id",
                    "in.c-data",
                    "--branch",
                    "99",
                    "--yes",
                ],
            )
        assert result.exit_code == 0
        call_kwargs = svc.delete_buckets.call_args.kwargs
        assert call_kwargs["branch_id"] == 99


class TestStorageBucketsBranch:
    """Tests for --branch support in storage buckets (list) command."""

    def test_cli_branch_flag(self, tmp_path: Path) -> None:
        """storage buckets --branch 10 --project test passes branch_id to service."""
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.list_buckets.return_value = {"buckets": [], "errors": []}
            result = runner.invoke(
                app,
                ["--json", "storage", "buckets", "--project", "test", "--branch", "10"],
            )
        assert result.exit_code == 0
        call_kwargs = svc.list_buckets.call_args.kwargs
        assert call_kwargs["branch_id"] == 10

    def test_cli_branch_requires_single_project(self, tmp_path: Path) -> None:
        """storage buckets --branch without --project returns exit code 2."""
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            MockSvc.return_value.list_buckets.return_value = {"buckets": [], "errors": []}
            result = runner.invoke(
                app,
                ["--json", "storage", "buckets", "--branch", "10"],
            )
        assert result.exit_code == 2
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "INVALID_ARGUMENT" in output["error"]["code"]


class TestStorageBucketDetailBranch:
    """Tests for --branch support in storage bucket-detail command."""

    def test_cli_branch_flag(self, tmp_path: Path) -> None:
        """storage bucket-detail --branch 20 passes branch_id to service."""
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.get_bucket_detail.return_value = {
                "project_alias": "test",
                "project_id": 1234,
                "bucket_id": "in.c-data",
                "display_name": "data",
                "stage": "in",
                "description": "",
                "backend": "snowflake",
                "is_linked": False,
                "source_project_id": None,
                "source_project_name": "",
                "source_bucket_id": "",
                "snowflake_database": "SAPI_1234",
                "snowflake_schema": "in.c-data",
                "tables": [],
                "table_count": 0,
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "bucket-detail",
                    "--project",
                    "test",
                    "--bucket-id",
                    "in.c-data",
                    "--branch",
                    "20",
                ],
            )
        assert result.exit_code == 0
        call_kwargs = svc.get_bucket_detail.call_args.kwargs
        assert call_kwargs["branch_id"] == 20


class TestStorageTablesBranch:
    """Tests for --branch support in storage tables command."""

    def test_cli_branch_flag(self, tmp_path: Path) -> None:
        """storage tables --branch 30 passes branch_id to service."""
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.list_tables.return_value = {"tables": [], "errors": []}
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "tables",
                    "--project",
                    "test",
                    "--branch",
                    "30",
                ],
            )
        assert result.exit_code == 0
        call_kwargs = svc.list_tables.call_args.kwargs
        assert call_kwargs["branch_id"] == 30
        # Multi-project CLI passes --project as a list
        assert call_kwargs["aliases"] == ["test"]


class TestDeleteColumnBranch:
    """Tests for --branch support in delete-column."""

    def test_service_passes_branch_id(self, tmp_path: Path) -> None:
        """delete_columns passes branch_id to client.delete_column."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.delete_column.return_value = None
        service = _make_service(store, mock_client)

        result = service.delete_columns(
            alias="test", table_id="in.c-data.users", columns=["age"], branch_id=42
        )

        assert result["deleted"] == ["age"]
        mock_client.delete_column.assert_called_once_with(
            "in.c-data.users", "age", branch_id=42, force=False
        )

    def test_cli_branch_flag_json(self, tmp_path: Path) -> None:
        """storage delete-column --branch 42 passes branch_id to service."""
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.delete_columns.return_value = {
                "deleted": ["age"],
                "failed": [],
                "dry_run": False,
                "project_alias": "test",
                "table_id": "in.c-data.users",
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "delete-column",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-data.users",
                    "--column",
                    "age",
                    "--branch",
                    "42",
                    "--yes",
                ],
            )
        assert result.exit_code == 0
        call_kwargs = svc.delete_columns.call_args.kwargs
        assert call_kwargs["branch_id"] == 42


def _make_store_with_active_branch(tmp_path: Path, branch_id: int) -> ConfigStore:
    """Build a config store with one project that has an active dev branch."""
    store = _make_store(tmp_path)
    store.set_project_branch("test", branch_id)
    return store


class TestStorageReadIgnoresActiveBranch:
    """Regression tests for GitHub issue #207.

    Storage READ commands (tables, buckets, bucket-detail, table-detail,
    files) must NOT follow the implicit active dev branch -- the Storage
    API branch-scoped endpoint returns only locally-modified resources,
    so a fresh dev branch lists nothing. Explicit --branch still wins.
    Storage WRITE commands stay branch-aware (user intent to modify).
    """

    # Read commands ---------------------------------------------------------

    def test_storage_tables_with_active_branch_uses_production(self, tmp_path: Path) -> None:
        """storage tables sends branch_id=None when only an active branch is set."""
        store = _make_store_with_active_branch(tmp_path, 15931)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.list_tables.return_value = {"tables": [], "project_alias": "test"}

            result = runner.invoke(
                app,
                ["--json", "storage", "tables", "--project", "test"],
            )

        assert result.exit_code == 0, result.output
        call_kwargs = svc.list_tables.call_args.kwargs
        assert call_kwargs["branch_id"] is None

    def test_storage_tables_explicit_branch_overrides_active(self, tmp_path: Path) -> None:
        """Explicit --branch wins even when active_branch_id is set."""
        store = _make_store_with_active_branch(tmp_path, 15931)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.list_tables.return_value = {"tables": [], "project_alias": "test"}

            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "tables",
                    "--project",
                    "test",
                    "--branch",
                    "99",
                ],
            )

        assert result.exit_code == 0, result.output
        call_kwargs = svc.list_tables.call_args.kwargs
        assert call_kwargs["branch_id"] == 99

    def test_storage_buckets_with_active_branch_uses_production(self, tmp_path: Path) -> None:
        """storage buckets ignores active_branch_id (single-project case)."""
        store = _make_store_with_active_branch(tmp_path, 15931)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.list_buckets.return_value = {"buckets": [], "errors": []}

            result = runner.invoke(
                app,
                ["--json", "storage", "buckets", "--project", "test"],
            )

        assert result.exit_code == 0, result.output
        call_kwargs = svc.list_buckets.call_args.kwargs
        assert call_kwargs["branch_id"] is None

    def test_storage_buckets_explicit_branch_overrides_active(self, tmp_path: Path) -> None:
        """Explicit --branch wins for storage buckets too."""
        store = _make_store_with_active_branch(tmp_path, 15931)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.list_buckets.return_value = {"buckets": [], "errors": []}

            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "buckets",
                    "--project",
                    "test",
                    "--branch",
                    "77",
                ],
            )

        assert result.exit_code == 0, result.output
        call_kwargs = svc.list_buckets.call_args.kwargs
        assert call_kwargs["branch_id"] == 77

    def test_storage_bucket_detail_with_active_branch_uses_production(self, tmp_path: Path) -> None:
        """storage bucket-detail sends branch_id=None despite active dev branch."""
        store = _make_store_with_active_branch(tmp_path, 15931)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.get_bucket_detail.return_value = {
                "project_alias": "test",
                "project_id": 1234,
                "bucket_id": "in.c-data",
                "display_name": "data",
                "stage": "in",
                "description": "",
                "backend": "snowflake",
                "is_linked": False,
                "source_project_id": None,
                "source_project_name": "",
                "source_bucket_id": "",
                "snowflake_database": "SAPI_1234",
                "snowflake_schema": "in.c-data",
                "tables": [],
                "table_count": 0,
            }

            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "bucket-detail",
                    "--project",
                    "test",
                    "--bucket-id",
                    "in.c-data",
                ],
            )

        assert result.exit_code == 0, result.output
        call_kwargs = svc.get_bucket_detail.call_args.kwargs
        assert call_kwargs["branch_id"] is None

    def test_storage_table_detail_with_active_branch_uses_production(self, tmp_path: Path) -> None:
        """storage table-detail sends branch_id=None despite active dev branch."""
        store = _make_store_with_active_branch(tmp_path, 15931)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.get_table_detail.return_value = {
                "project_alias": "test",
                "project_id": 1234,
                "table_id": "in.c-data.users",
                "name": "users",
                "display_name": "users",
                "bucket_id": "in.c-data",
                "rows_count": 10,
                "data_size_bytes": 1024,
                "primary_key": [],
                "column_details": [],
                "last_import_date": None,
            }

            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "table-detail",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-data.users",
                ],
            )

        assert result.exit_code == 0, result.output
        call_kwargs = svc.get_table_detail.call_args.kwargs
        assert call_kwargs["branch_id"] is None

    def test_storage_files_with_active_branch_uses_production(self, tmp_path: Path) -> None:
        """storage files sends branch_id=None despite active dev branch."""
        store = _make_store_with_active_branch(tmp_path, 15931)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.list_files.return_value = {"files": [], "count": 0}

            result = runner.invoke(
                app,
                ["--json", "storage", "files", "--project", "test"],
            )

        assert result.exit_code == 0, result.output
        call_kwargs = svc.list_files.call_args.kwargs
        assert call_kwargs["branch_id"] is None

    def test_storage_files_explicit_branch_overrides_active(self, tmp_path: Path) -> None:
        """Explicit --branch wins for storage files."""
        store = _make_store_with_active_branch(tmp_path, 15931)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.list_files.return_value = {"files": [], "count": 0}

            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "files",
                    "--project",
                    "test",
                    "--branch",
                    "55",
                ],
            )

        assert result.exit_code == 0, result.output
        call_kwargs = svc.list_files.call_args.kwargs
        assert call_kwargs["branch_id"] == 55

    # Write commands remain branch-aware -----------------------------------

    def test_storage_create_bucket_still_follows_active_branch(self, tmp_path: Path) -> None:
        """Write command create-bucket keeps branch-awareness (user intent)."""
        store = _make_store_with_active_branch(tmp_path, 15931)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.create_bucket.return_value = {
                "id": "in.c-foo",
                "stage": "in",
                "project_alias": "test",
                "display_name": "foo",
                "description": "",
                "backend": "snowflake",
            }

            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "create-bucket",
                    "--project",
                    "test",
                    "--stage",
                    "in",
                    "--name",
                    "foo",
                ],
            )

        assert result.exit_code == 0, result.output
        call_kwargs = svc.create_bucket.call_args.kwargs
        assert call_kwargs["branch_id"] == 15931

    def test_storage_delete_table_still_follows_active_branch(self, tmp_path: Path) -> None:
        """Destructive delete-table keeps branch-awareness."""
        store = _make_store_with_active_branch(tmp_path, 15931)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.delete_tables.return_value = {
                "deleted": ["in.c-data.users"],
                "failed": [],
                "dry_run": False,
                "project_alias": "test",
            }

            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "delete-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-data.users",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, result.output
        call_kwargs = svc.delete_tables.call_args.kwargs
        assert call_kwargs["branch_id"] == 15931
