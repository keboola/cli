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
            stage="in", name="my-bucket", description="Test bucket", backend=None
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
            stage="out", name="result", description=None, backend="bigquery"
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
        )

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
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.upload_table.return_value = {"importedRowsCount": 42, "warnings": []}
        service = _make_service(store, mock_client)

        result = service.upload_table(
            alias="test",
            table_id="in.c-b.users",
            file_path="/data/users.csv",
        )

        assert result["table_id"] == "in.c-b.users"
        assert result["incremental"] is False
        assert result["imported_rows"] == 42
        assert result["warnings"] == []
        mock_client.upload_table.assert_called_once_with(
            table_id="in.c-b.users",
            file_path="/data/users.csv",
            incremental=False,
            delimiter=",",
            enclosure='"',
        )
        mock_client.close.assert_called_once()

    def test_success_incremental(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.upload_table.return_value = {"importedRowsCount": 10, "warnings": []}
        service = _make_service(store, mock_client)

        result = service.upload_table(
            alias="test",
            table_id="in.c-b.events",
            file_path="/data/events.csv",
            incremental=True,
        )

        assert result["incremental"] is True
        mock_client.upload_table.assert_called_once_with(
            table_id="in.c-b.events",
            file_path="/data/events.csv",
            incremental=True,
            delimiter=",",
            enclosure='"',
        )

    def test_custom_delimiter_and_enclosure(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.upload_table.return_value = {"importedRowsCount": 5, "warnings": []}
        service = _make_service(store, mock_client)

        service.upload_table(
            alias="test",
            table_id="in.c-b.t",
            file_path="/data/t.csv",
            delimiter=";",
            enclosure="'",
        )

        mock_client.upload_table.assert_called_once_with(
            table_id="in.c-b.t",
            file_path="/data/t.csv",
            incremental=False,
            delimiter=";",
            enclosure="'",
        )

    def test_warnings_passed_through(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.upload_table.return_value = {
            "importedRowsCount": 3,
            "warnings": ["Duplicate rows skipped"],
        }
        service = _make_service(store, mock_client)

        result = service.upload_table(alias="test", table_id="in.c-b.t", file_path="/x.csv")

        assert result["warnings"] == ["Duplicate rows skipped"]

    def test_api_error_propagates(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.upload_table.side_effect = KeboolaApiError(
            "Table not found", status_code=404, error_code="NOT_FOUND"
        )
        service = _make_service(store, mock_client)

        with pytest.raises(KeboolaApiError):
            service.upload_table(alias="test", table_id="in.c-b.t", file_path="/x.csv")

        mock_client.close.assert_called_once()


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
        )

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
