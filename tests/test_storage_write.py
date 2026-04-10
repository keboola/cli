"""Tests for storage create-bucket, create-table, and upload-table commands and service methods."""

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
# Service tests: create_bucket
# ---------------------------------------------------------------------------


class TestCreateBucketService:
    """Tests for StorageService.create_bucket()."""

    def test_success(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_bucket.return_value = {
            "id": "in.c-my-bucket",
            "displayName": "my-bucket",
            "stage": "in",
            "backend": "snowflake",
            "description": "Test bucket",
        }
        service = _make_service(store, mock_client)

        result = service.create_bucket(
            alias="test", stage="in", name="my-bucket", description="Test bucket"
        )

        assert result["id"] == "in.c-my-bucket"
        assert result["stage"] == "in"
        assert result["project_alias"] == "test"
        mock_client.create_bucket.assert_called_once_with(
            stage="in",
            name="my-bucket",
            description="Test bucket",
            backend=None,
            branch_id=None,
        )
        mock_client.close.assert_called_once()

    def test_with_backend(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_bucket.return_value = {
            "id": "out.c-result",
            "stage": "out",
            "backend": "bigquery",
            "description": "",
        }
        service = _make_service(store, mock_client)

        service.create_bucket(alias="test", stage="out", name="result", backend="bigquery")

        mock_client.create_bucket.assert_called_once_with(
            stage="out",
            name="result",
            description=None,
            backend="bigquery",
            branch_id=None,
        )

    def test_api_error_propagates(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_bucket.side_effect = KeboolaApiError(
            "Bucket already exists", status_code=422, error_code="BUCKET_ALREADY_EXISTS"
        )
        service = _make_service(store, mock_client)

        with pytest.raises(KeboolaApiError, match="Bucket already exists"):
            service.create_bucket(alias="test", stage="in", name="existing")

        mock_client.close.assert_called_once()

    def test_invalid_stage_raises(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service = _make_service(store, MagicMock())

        with pytest.raises(ValueError, match="Invalid stage"):
            service.create_bucket(alias="test", stage="bad", name="x")

    def test_unknown_project(self, tmp_path: Path) -> None:
        from keboola_agent_cli.errors import ConfigError

        store = _make_store(tmp_path)
        service = _make_service(store, MagicMock())

        with pytest.raises(ConfigError):
            service.create_bucket(alias="nonexistent", stage="in", name="x")


# ---------------------------------------------------------------------------
# Service tests: create_table
# ---------------------------------------------------------------------------


class TestCreateTableService:
    """Tests for StorageService.create_table()."""

    def test_success(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_table.return_value = {"id": "in.c-my-bucket.users"}
        service = _make_service(store, mock_client)

        result = service.create_table(
            alias="test",
            bucket_id="in.c-my-bucket",
            name="users",
            columns=["id:INTEGER", "name:STRING"],
            primary_key=["id"],
        )

        assert result["table_id"] == "in.c-my-bucket.users"
        assert result["name"] == "users"
        assert result["primary_key"] == ["id"]
        assert result["columns"] == ["id", "name"]
        mock_client.create_table.assert_called_once_with(
            bucket_id="in.c-my-bucket",
            name="users",
            columns=[
                {"name": "id", "definition": {"type": "INTEGER"}},
                {"name": "name", "definition": {"type": "STRING"}},
            ],
            primary_key=["id"],
            branch_id=None,
        )
        mock_client.close.assert_called_once()

    def test_column_without_type_defaults_to_string(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_table.return_value = {"id": "in.c-b.t"}
        service = _make_service(store, mock_client)

        service.create_table(alias="test", bucket_id="in.c-b", name="t", columns=["label"])

        call_args = mock_client.create_table.call_args
        assert call_args.kwargs["columns"] == [{"name": "label", "definition": {"type": "STRING"}}]

    def test_column_type_uppercased(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_table.return_value = {"id": "in.c-b.t"}
        service = _make_service(store, mock_client)

        service.create_table(alias="test", bucket_id="in.c-b", name="t", columns=["amount:numeric"])

        call_args = mock_client.create_table.call_args
        assert call_args.kwargs["columns"][0]["definition"]["type"] == "NUMERIC"

    def test_no_primary_key(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_table.return_value = {"id": "in.c-b.t"}
        service = _make_service(store, mock_client)

        result = service.create_table(
            alias="test", bucket_id="in.c-b", name="t", columns=["x:STRING"]
        )

        assert result["primary_key"] == []
        mock_client.create_table.assert_called_once_with(
            bucket_id="in.c-b",
            name="t",
            columns=[{"name": "x", "definition": {"type": "STRING"}}],
            primary_key=None,
            branch_id=None,
        )

    def test_invalid_column_type_raises(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service = _make_service(store, MagicMock())

        with pytest.raises(ValueError, match="Invalid column type 'BANANA'"):
            service.create_table(alias="test", bucket_id="in.c-b", name="t", columns=["x:BANANA"])

    def test_api_error_propagates(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_table.side_effect = KeboolaApiError(
            "Table already exists", status_code=422, error_code="TABLE_ALREADY_EXISTS"
        )
        service = _make_service(store, mock_client)

        with pytest.raises(KeboolaApiError):
            service.create_table(alias="test", bucket_id="in.c-b", name="t", columns=["x:STRING"])

        mock_client.close.assert_called_once()


# ---------------------------------------------------------------------------
# Service tests: upload_table
# ---------------------------------------------------------------------------


class TestUploadTableService:
    """Tests for StorageService.upload_table()."""

    def test_success_full_load(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "users.csv"
        csv_file.write_text("id,name\n1,Alice\n")
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.upload_table.return_value = {"importedRowsCount": 42, "warnings": []}
        service = _make_service(store, mock_client)

        result = service.upload_table(
            alias="test",
            table_id="in.c-b.users",
            file_path=str(csv_file),
            auto_create=False,
        )

        assert result["table_id"] == "in.c-b.users"
        assert result["incremental"] is False
        assert result["imported_rows"] == 42
        assert result["warnings"] == []
        assert "file_size_bytes" in result
        assert result["file_size_bytes"] > 0
        mock_client.upload_table.assert_called_once_with(
            table_id="in.c-b.users",
            file_path=str(csv_file),
            incremental=False,
            delimiter=",",
            enclosure='"',
            branch_id=None,
        )
        mock_client.close.assert_called_once()

    def test_success_incremental(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "events.csv"
        csv_file.write_text("ts,msg\n2024-01-01,hello\n")
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.upload_table.return_value = {"importedRowsCount": 10, "warnings": []}
        service = _make_service(store, mock_client)

        result = service.upload_table(
            alias="test",
            table_id="in.c-b.events",
            file_path=str(csv_file),
            incremental=True,
            auto_create=False,
        )

        assert result["incremental"] is True
        mock_client.upload_table.assert_called_once_with(
            table_id="in.c-b.events",
            file_path=str(csv_file),
            incremental=True,
            delimiter=",",
            enclosure='"',
            branch_id=None,
        )

    def test_custom_delimiter_and_enclosure(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "t.csv"
        csv_file.write_text("a;b\n1;2\n")
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.upload_table.return_value = {"importedRowsCount": 5, "warnings": []}
        service = _make_service(store, mock_client)

        service.upload_table(
            alias="test",
            table_id="in.c-b.t",
            file_path=str(csv_file),
            delimiter=";",
            enclosure="'",
            auto_create=False,
        )

        mock_client.upload_table.assert_called_once_with(
            table_id="in.c-b.t",
            file_path=str(csv_file),
            incremental=False,
            delimiter=";",
            enclosure="'",
            branch_id=None,
        )

    def test_warnings_passed_through(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "x.csv"
        csv_file.write_text("id\n1\n")
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.upload_table.return_value = {
            "importedRowsCount": 3,
            "warnings": ["Duplicate rows skipped"],
        }
        service = _make_service(store, mock_client)

        result = service.upload_table(
            alias="test", table_id="in.c-b.t", file_path=str(csv_file), auto_create=False
        )

        assert result["warnings"] == ["Duplicate rows skipped"]

    def test_api_error_propagates(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "x.csv"
        csv_file.write_text("id\n1\n")
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.upload_table.side_effect = KeboolaApiError(
            "Table not found", status_code=404, error_code="NOT_FOUND"
        )
        service = _make_service(store, mock_client)

        with pytest.raises(KeboolaApiError):
            service.upload_table(
                alias="test", table_id="in.c-b.t", file_path=str(csv_file), auto_create=False
            )

        mock_client.close.assert_called_once()


# ---------------------------------------------------------------------------
# Service tests: upload_table auto-create
# ---------------------------------------------------------------------------


class TestUploadTableAutoCreate:
    """Tests for StorageService.upload_table() auto-create behaviour."""

    def test_auto_creates_bucket_and_table(self, tmp_path: Path) -> None:
        """When bucket and table are missing, both are created before upload."""
        csv_file = tmp_path / "users.csv"
        csv_file.write_text("id,name,email\n1,Alice,a@b.com\n")
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        # Bucket does not exist → 404
        mock_client.get_bucket_detail.side_effect = KeboolaApiError(
            "Bucket not found", status_code=404, error_code="storage.buckets.notFound"
        )
        mock_client.list_tables.return_value = []  # table also absent after bucket create
        mock_client.upload_table.return_value = {"importedRowsCount": 1, "warnings": []}
        service = _make_service(store, mock_client)

        result = service.upload_table(
            alias="test",
            table_id="in.c-users.users",
            file_path=str(csv_file),
        )

        mock_client.create_bucket.assert_called_once_with(
            stage="in",
            name="users",
            branch_id=None,
        )
        mock_client.create_table.assert_called_once_with(
            bucket_id="in.c-users",
            name="users",
            columns=[
                {"name": "id", "definition": {"type": "STRING"}},
                {"name": "name", "definition": {"type": "STRING"}},
                {"name": "email", "definition": {"type": "STRING"}},
            ],
            primary_key=None,
            branch_id=None,
        )
        assert result["auto_created_bucket"] is True
        assert result["auto_created_table"] is True

    def test_auto_creates_table_only_when_bucket_exists(self, tmp_path: Path) -> None:
        """When bucket exists but table is missing, only the table is created."""
        csv_file = tmp_path / "events.csv"
        csv_file.write_text("ts,payload\n2024-01-01,hello\n")
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_bucket_detail.return_value = {"id": "in.c-logs"}  # bucket exists
        mock_client.list_tables.return_value = []  # table absent
        mock_client.upload_table.return_value = {"importedRowsCount": 1, "warnings": []}
        service = _make_service(store, mock_client)

        result = service.upload_table(
            alias="test",
            table_id="in.c-logs.events",
            file_path=str(csv_file),
        )

        mock_client.create_bucket.assert_not_called()
        mock_client.create_table.assert_called_once_with(
            bucket_id="in.c-logs",
            name="events",
            columns=[
                {"name": "ts", "definition": {"type": "STRING"}},
                {"name": "payload", "definition": {"type": "STRING"}},
            ],
            primary_key=None,
            branch_id=None,
        )
        assert result["auto_created_bucket"] is False
        assert result["auto_created_table"] is True

    def test_no_auto_create_when_both_exist(self, tmp_path: Path) -> None:
        """When bucket and table both exist, nothing is created."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("x\n1\n")
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_bucket_detail.return_value = {"id": "in.c-b"}
        mock_client.list_tables.return_value = [{"name": "data"}]
        mock_client.upload_table.return_value = {"importedRowsCount": 1, "warnings": []}
        service = _make_service(store, mock_client)

        result = service.upload_table(
            alias="test",
            table_id="in.c-b.data",
            file_path=str(csv_file),
        )

        mock_client.create_bucket.assert_not_called()
        mock_client.create_table.assert_not_called()
        assert result["auto_created_bucket"] is False
        assert result["auto_created_table"] is False

    def test_auto_create_false_skips_all_checks(self, tmp_path: Path) -> None:
        """With auto_create=False, no existence checks or creates are made."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("x\n1\n")
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.upload_table.return_value = {"importedRowsCount": 1, "warnings": []}
        service = _make_service(store, mock_client)

        service.upload_table(
            alias="test",
            table_id="in.c-b.data",
            file_path=str(csv_file),
            auto_create=False,
        )

        mock_client.get_bucket_detail.assert_not_called()
        mock_client.list_tables.assert_not_called()
        mock_client.create_bucket.assert_not_called()
        mock_client.create_table.assert_not_called()

    def test_bucket_404_non_404_api_error_propagates(self, tmp_path: Path) -> None:
        """A non-404 error from get_bucket_detail is re-raised."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("x\n1\n")
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_bucket_detail.side_effect = KeboolaApiError(
            "Forbidden", status_code=403, error_code="FORBIDDEN"
        )
        service = _make_service(store, mock_client)

        with pytest.raises(KeboolaApiError, match="Forbidden"):
            service.upload_table(
                alias="test",
                table_id="in.c-b.t",
                file_path=str(csv_file),
            )

    def test_empty_csv_header_raises_value_error(self, tmp_path: Path) -> None:
        """If the CSV has an empty header row, a ValueError is raised."""
        csv_file = tmp_path / "empty.csv"
        csv_file.write_text("\n1,2\n")
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_bucket_detail.return_value = {"id": "in.c-b"}
        mock_client.list_tables.return_value = []  # table missing → will try to read header
        service = _make_service(store, mock_client)

        with pytest.raises(ValueError, match="no column headers"):
            service.upload_table(
                alias="test",
                table_id="in.c-b.t",
                file_path=str(csv_file),
            )


# ---------------------------------------------------------------------------
# CLI tests: create-bucket
# ---------------------------------------------------------------------------


class TestCreateBucketCLI:
    """CLI tests for `kbagent storage create-bucket`."""

    def test_create_bucket_json(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.create_bucket.return_value = {
                "project_alias": "test",
                "id": "in.c-my-bucket",
                "display_name": "my-bucket",
                "stage": "in",
                "backend": "snowflake",
                "description": "",
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
                    "my-bucket",
                ],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["id"] == "in.c-my-bucket"

    def test_create_bucket_with_description_and_backend(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.create_bucket.return_value = {
                "project_alias": "test",
                "id": "out.c-result",
                "display_name": "result",
                "stage": "out",
                "backend": "bigquery",
                "description": "My output",
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
                    "out",
                    "--name",
                    "result",
                    "--description",
                    "My output",
                    "--backend",
                    "bigquery",
                ],
            )
        assert result.exit_code == 0
        svc.create_bucket.assert_called_once_with(
            alias="test",
            stage="out",
            name="result",
            description="My output",
            backend="bigquery",
            branch_id=None,
        )

    def test_create_bucket_invalid_stage(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            MockSvc.return_value.create_bucket.side_effect = ValueError("Invalid stage 'foo'")
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "create-bucket",
                    "--project",
                    "test",
                    "--stage",
                    "foo",
                    "--name",
                    "x",
                ],
            )
        assert result.exit_code == 2

    def test_create_bucket_api_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.create_bucket.side_effect = KeboolaApiError(
                "Bucket already exists", status_code=422, error_code="BUCKET_ALREADY_EXISTS"
            )
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
                    "existing",
                ],
            )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# CLI tests: create-table
# ---------------------------------------------------------------------------


class TestCreateTableCLI:
    """CLI tests for `kbagent storage create-table`."""

    def test_create_table_json(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.create_table.return_value = {
                "project_alias": "test",
                "table_id": "in.c-b.users",
                "name": "users",
                "bucket_id": "in.c-b",
                "primary_key": ["id"],
                "columns": ["id", "name"],
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "create-table",
                    "--project",
                    "test",
                    "--bucket-id",
                    "in.c-b",
                    "--name",
                    "users",
                    "--column",
                    "id:INTEGER",
                    "--column",
                    "name:STRING",
                    "--primary-key",
                    "id",
                ],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["table_id"] == "in.c-b.users"
        svc.create_table.assert_called_once_with(
            alias="test",
            bucket_id="in.c-b",
            name="users",
            columns=["id:INTEGER", "name:STRING"],
            primary_key=["id"],
            branch_id=None,
        )

    def test_create_table_no_primary_key(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.create_table.return_value = {
                "project_alias": "test",
                "table_id": "in.c-b.events",
                "name": "events",
                "bucket_id": "in.c-b",
                "primary_key": [],
                "columns": ["ts", "payload"],
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "create-table",
                    "--project",
                    "test",
                    "--bucket-id",
                    "in.c-b",
                    "--name",
                    "events",
                    "--column",
                    "ts:TIMESTAMP",
                    "--column",
                    "payload:STRING",
                ],
            )
        assert result.exit_code == 0

    def test_create_table_invalid_column_type(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            MockSvc.return_value.create_table.side_effect = ValueError(
                "Invalid column type 'BANANA'"
            )
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "create-table",
                    "--project",
                    "test",
                    "--bucket-id",
                    "in.c-b",
                    "--name",
                    "t",
                    "--column",
                    "x:BANANA",
                ],
            )
        assert result.exit_code == 2

    def test_create_table_api_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.create_table.side_effect = KeboolaApiError(
                "Table already exists", status_code=422, error_code="TABLE_ALREADY_EXISTS"
            )
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "create-table",
                    "--project",
                    "test",
                    "--bucket-id",
                    "in.c-b",
                    "--name",
                    "existing",
                    "--column",
                    "x:STRING",
                ],
            )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# CLI tests: upload-table
# ---------------------------------------------------------------------------


class TestUploadTableCLI:
    """CLI tests for `kbagent storage upload-table`."""

    def test_upload_table_json(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("id,name\n1,Alice\n")
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.upload_table.return_value = {
                "project_alias": "test",
                "table_id": "in.c-b.users",
                "incremental": False,
                "imported_rows": 1,
                "file_size_bytes": 16,
                "warnings": [],
                "auto_created_bucket": False,
                "auto_created_table": False,
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "upload-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-b.users",
                    "--file",
                    str(csv_file),
                ],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["imported_rows"] == 1
        svc.upload_table.assert_called_once_with(
            alias="test",
            table_id="in.c-b.users",
            file_path=str(csv_file),
            incremental=False,
            delimiter=",",
            enclosure='"',
            auto_create=True,
            branch_id=None,
        )

    def test_upload_table_incremental(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "events.csv"
        csv_file.write_text("ts,msg\n2024-01-01,hello\n")
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.upload_table.return_value = {
                "project_alias": "test",
                "table_id": "in.c-b.events",
                "incremental": True,
                "imported_rows": 1,
                "warnings": [],
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "upload-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-b.events",
                    "--file",
                    str(csv_file),
                    "--incremental",
                ],
            )
        assert result.exit_code == 0
        svc.upload_table.assert_called_once_with(
            alias="test",
            table_id="in.c-b.events",
            file_path=str(csv_file),
            incremental=True,
            delimiter=",",
            enclosure='"',
            auto_create=True,
            branch_id=None,
        )

    def test_upload_table_file_not_found(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService"),
        ):
            MockStore.return_value = store
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "upload-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-b.t",
                    "--file",
                    "/nonexistent/file.csv",
                ],
            )
        assert result.exit_code == 2

    def test_upload_table_api_error(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("id\n1\n")
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.upload_table.side_effect = KeboolaApiError(
                "Table not found", status_code=404, error_code="NOT_FOUND"
            )
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "upload-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-b.missing",
                    "--file",
                    str(csv_file),
                ],
            )
        assert result.exit_code != 0

    def test_upload_table_no_auto_create_flag(self, tmp_path: Path) -> None:
        """--no-auto-create passes auto_create=False to the service."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("id\n1\n")
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.upload_table.return_value = {
                "project_alias": "test",
                "table_id": "in.c-b.users",
                "incremental": False,
                "imported_rows": 1,
                "warnings": [],
                "auto_created_bucket": False,
                "auto_created_table": False,
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "upload-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-b.users",
                    "--file",
                    str(csv_file),
                    "--no-auto-create",
                ],
            )
        assert result.exit_code == 0
        svc.upload_table.assert_called_once_with(
            alias="test",
            table_id="in.c-b.users",
            file_path=str(csv_file),
            incremental=False,
            delimiter=",",
            enclosure='"',
            auto_create=False,
            branch_id=None,
        )


# ---------------------------------------------------------------------------
# Branch support tests
# ---------------------------------------------------------------------------


class TestCreateBucketBranch:
    """Tests for --branch support in create-bucket."""

    def test_service_passes_branch_id(self, tmp_path: Path) -> None:
        """create_bucket passes branch_id to client."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_bucket.return_value = {
            "id": "in.c-my-bucket",
            "displayName": "my-bucket",
            "stage": "in",
            "backend": "snowflake",
            "description": "",
        }
        service = _make_service(store, mock_client)

        service.create_bucket(alias="test", stage="in", name="my-bucket", branch_id=55)

        mock_client.create_bucket.assert_called_once_with(
            stage="in",
            name="my-bucket",
            description=None,
            backend=None,
            branch_id=55,
        )

    def test_cli_branch_flag(self, tmp_path: Path) -> None:
        """storage create-bucket --branch 55 passes branch_id to service."""
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.create_bucket.return_value = {
                "project_alias": "test",
                "id": "in.c-my-bucket",
                "display_name": "my-bucket",
                "stage": "in",
                "backend": "snowflake",
                "description": "",
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
                    "my-bucket",
                    "--branch",
                    "55",
                ],
            )
        assert result.exit_code == 0
        call_kwargs = svc.create_bucket.call_args.kwargs
        assert call_kwargs["branch_id"] == 55


class TestCreateTableBranch:
    """Tests for --branch support in create-table."""

    def test_service_passes_branch_id(self, tmp_path: Path) -> None:
        """create_table passes branch_id to client."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_table.return_value = {"id": "in.c-b.users"}
        service = _make_service(store, mock_client)

        service.create_table(
            alias="test",
            bucket_id="in.c-b",
            name="users",
            columns=["id:INTEGER"],
            branch_id=77,
        )

        mock_client.create_table.assert_called_once_with(
            bucket_id="in.c-b",
            name="users",
            columns=[{"name": "id", "definition": {"type": "INTEGER"}}],
            primary_key=None,
            branch_id=77,
        )

    def test_cli_branch_flag(self, tmp_path: Path) -> None:
        """storage create-table --branch 77 passes branch_id to service."""
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.create_table.return_value = {
                "project_alias": "test",
                "table_id": "in.c-b.users",
                "name": "users",
                "bucket_id": "in.c-b",
                "primary_key": [],
                "columns": ["id"],
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "create-table",
                    "--project",
                    "test",
                    "--bucket-id",
                    "in.c-b",
                    "--name",
                    "users",
                    "--column",
                    "id:INTEGER",
                    "--branch",
                    "77",
                ],
            )
        assert result.exit_code == 0
        call_kwargs = svc.create_table.call_args.kwargs
        assert call_kwargs["branch_id"] == 77


class TestUploadTableBranch:
    """Tests for --branch support in upload-table."""

    def test_service_passes_branch_id(self, tmp_path: Path) -> None:
        """upload_table passes branch_id to client."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("id\n1\n")
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.upload_table.return_value = {"importedRowsCount": 1, "warnings": []}
        service = _make_service(store, mock_client)

        service.upload_table(
            alias="test",
            table_id="in.c-b.data",
            file_path=str(csv_file),
            auto_create=False,
            branch_id=33,
        )

        mock_client.upload_table.assert_called_once_with(
            table_id="in.c-b.data",
            file_path=str(csv_file),
            incremental=False,
            delimiter=",",
            enclosure='"',
            branch_id=33,
        )

    def test_cli_branch_flag(self, tmp_path: Path) -> None:
        """storage upload-table --branch 33 passes branch_id to service."""
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("id\n1\n")
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.upload_table.return_value = {
                "project_alias": "test",
                "table_id": "in.c-b.data",
                "incremental": False,
                "imported_rows": 1,
                "file_size_bytes": 5,
                "warnings": [],
                "auto_created_bucket": False,
                "auto_created_table": False,
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "upload-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-b.data",
                    "--file",
                    str(csv_file),
                    "--branch",
                    "33",
                ],
            )
        assert result.exit_code == 0
        call_kwargs = svc.upload_table.call_args.kwargs
        assert call_kwargs["branch_id"] == 33
