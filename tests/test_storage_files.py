"""Tests for Storage Files commands and service methods.

Covers: files, file-upload, file-download, file-detail,
file-delete, file-tag, load-file, unload-table.
"""

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

SAMPLE_FILE = {
    "id": 12345,
    "name": "test-data.csv",
    "sizeBytes": 1024,
    "created": "2026-04-12T10:00:00+0000",
    "isSliced": False,
    "isPermanent": False,
    "tags": ["report", "monthly"],
    "creatorToken": {"id": 1, "description": "My Token"},
    "url": "https://storage.example.com/files/test-data.csv",
}


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


# ------------------------------------------------------------------
# Service layer tests
# ------------------------------------------------------------------


class TestListFilesService:
    """Tests for StorageService.list_files()."""

    def test_list_files_basic(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.list_files.return_value = [SAMPLE_FILE]
        service = _make_service(store, mock_client)

        result = service.list_files(alias="test")

        assert result["project_alias"] == "test"
        assert result["count"] == 1
        assert result["files"][0]["id"] == 12345
        mock_client.list_files.assert_called_once_with(
            limit=20, offset=0, tags=None, since_id=None, query=None, branch_id=None
        )

    def test_list_files_with_tags(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.list_files.return_value = [SAMPLE_FILE]
        service = _make_service(store, mock_client)

        service.list_files(alias="test", tags=["report", "monthly"])

        mock_client.list_files.assert_called_once_with(
            limit=20,
            offset=0,
            tags=["report", "monthly"],
            since_id=None,
            query=None,
            branch_id=None,
        )

    def test_list_files_empty(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.list_files.return_value = []
        service = _make_service(store, mock_client)

        result = service.list_files(alias="test")

        assert result["count"] == 0
        assert result["files"] == []


class TestUploadFileService:
    """Tests for StorageService.upload_file()."""

    def test_upload_file_success(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.upload_file.return_value = {
            "id": 99,
            "name": "data.csv",
            "sizeBytes": 512,
            "tags": ["test"],
            "isPermanent": False,
            "created": "2026-04-12T10:00:00+0000",
        }
        service = _make_service(store, mock_client)

        # Create a test file
        test_file = tmp_path / "data.csv"
        test_file.write_text("col1,col2\na,b\n")

        result = service.upload_file(
            alias="test",
            file_path=str(test_file),
            tags=["test"],
        )

        assert result["id"] == 99
        assert result["project_alias"] == "test"
        mock_client.upload_file.assert_called_once()

    def test_upload_file_with_custom_name(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.upload_file.return_value = {
            "id": 100,
            "name": "custom-name.csv",
            "sizeBytes": 512,
            "tags": [],
            "isPermanent": True,
            "created": "2026-04-12T10:00:00+0000",
        }
        service = _make_service(store, mock_client)

        test_file = tmp_path / "tmp.csv"
        test_file.write_text("col1\na\n")

        result = service.upload_file(
            alias="test",
            file_path=str(test_file),
            name="custom-name.csv",
            is_permanent=True,
        )

        assert result["id"] == 100
        call_kwargs = mock_client.upload_file.call_args[1]
        assert call_kwargs["name"] == "custom-name.csv"
        assert call_kwargs["is_permanent"] is True


class TestDownloadFileService:
    """Tests for StorageService.download_file()."""

    def test_download_by_id(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_file_info.return_value = {
            **SAMPLE_FILE,
            "isSliced": False,
        }
        mock_client.download_file.return_value = 1024
        service = _make_service(store, mock_client)

        result = service.download_file(
            alias="test",
            file_id=12345,
            output_path=str(tmp_path / "out.csv"),
        )

        assert result["file_id"] == 12345
        assert result["file_size_bytes"] == 1024
        mock_client.download_file.assert_called_once()

    def test_download_by_tags(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.list_files.return_value = [SAMPLE_FILE]
        mock_client.get_file_info.return_value = {
            **SAMPLE_FILE,
            "isSliced": False,
        }
        mock_client.download_file.return_value = 1024
        service = _make_service(store, mock_client)

        result = service.download_file(
            alias="test",
            tags=["report"],
            output_path=str(tmp_path / "out.csv"),
        )

        assert result["file_id"] == 12345
        mock_client.list_files.assert_called_once_with(limit=1, tags=["report"])

    def test_download_by_tags_no_match(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.list_files.return_value = []
        service = _make_service(store, mock_client)

        with pytest.raises(KeboolaApiError) as exc_info:
            service.download_file(alias="test", tags=["nonexistent"])

        assert exc_info.value.error_code == "FILE_NOT_FOUND"

    def test_download_sliced_file(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_file_info.return_value = {
            **SAMPLE_FILE,
            "isSliced": True,
        }
        mock_client.download_sliced_file.return_value = 2048
        service = _make_service(store, mock_client)

        result = service.download_file(
            alias="test",
            file_id=12345,
            output_path=str(tmp_path / "out.csv"),
        )

        assert result["is_sliced"] is True
        assert result["file_size_bytes"] == 2048
        mock_client.download_sliced_file.assert_called_once()

    def test_download_parquet_file_routes_to_slice_dir(self, tmp_path: Path) -> None:
        """file-download auto-detects sliced .parquet and saves per-slice files."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_file_info.return_value = {
            **SAMPLE_FILE,
            "name": "users.parquet",
            "isSliced": True,
        }
        out_dir = tmp_path / "parq"
        mock_client.download_sliced_file_to_dir.return_value = {
            "output_dir": str(out_dir.resolve()),
            "slice_count": 2,
            "total_bytes": 4096,
            "slices": [
                {"path": str(out_dir / "a.parquet"), "size_bytes": 2048},
                {"path": str(out_dir / "b.parquet"), "size_bytes": 2048},
            ],
        }
        service = _make_service(store, mock_client)

        result = service.download_file(
            alias="test",
            file_id=12345,
            output_path=str(out_dir),
        )

        assert result["is_sliced"] is True
        assert result["slice_count"] == 2
        assert result["file_size_bytes"] == 4096
        assert len(result["slices"]) == 2
        mock_client.download_sliced_file_to_dir.assert_called_once()
        # CSV concat must not be called for parquet
        mock_client.download_sliced_file.assert_not_called()

    def test_download_requires_id_or_tags(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        with pytest.raises(ValueError, match="Either --file-id or --tag"):
            service.download_file(alias="test")


class TestGetFileInfoService:
    """Tests for StorageService.get_file_info()."""

    def test_get_file_info(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_file_info.return_value = SAMPLE_FILE.copy()
        service = _make_service(store, mock_client)

        result = service.get_file_info(alias="test", file_id=12345)

        assert result["id"] == 12345
        assert result["project_alias"] == "test"


class TestDeleteFilesService:
    """Tests for StorageService.delete_files()."""

    def test_delete_single(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        result = service.delete_files(alias="test", file_ids=[12345])

        assert result["deleted"] == [12345]
        assert result["failed"] == []

    def test_delete_dry_run(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        result = service.delete_files(alias="test", file_ids=[12345], dry_run=True)

        assert result["would_delete"] == [12345]
        assert result["deleted"] == []
        mock_client.delete_file.assert_not_called()

    def test_delete_partial_failure(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.delete_file.side_effect = [
            None,
            KeboolaApiError("Not found", status_code=404, error_code="NOT_FOUND"),
        ]
        service = _make_service(store, mock_client)

        result = service.delete_files(alias="test", file_ids=[100, 200])

        assert result["deleted"] == [100]
        assert len(result["failed"]) == 1
        assert result["failed"][0]["id"] == 200


class TestTagFileService:
    """Tests for StorageService.tag_file()."""

    def test_add_tags(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        result = service.tag_file(alias="test", file_id=12345, add_tags=["report", "2026"])

        assert result["added"] == ["report", "2026"]
        assert mock_client.tag_file.call_count == 2

    def test_remove_tags(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        result = service.tag_file(alias="test", file_id=12345, remove_tags=["draft"])

        assert result["removed"] == ["draft"]
        mock_client.untag_file.assert_called_once_with(12345, "draft")

    def test_add_and_remove(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        result = service.tag_file(
            alias="test",
            file_id=12345,
            add_tags=["final"],
            remove_tags=["draft"],
        )

        assert result["added"] == ["final"]
        assert result["removed"] == ["draft"]

    def test_tag_error_accumulated(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.tag_file.side_effect = KeboolaApiError(
            "Failed", status_code=400, error_code="BAD_REQUEST"
        )
        service = _make_service(store, mock_client)

        result = service.tag_file(alias="test", file_id=12345, add_tags=["bad"])

        assert result["added"] == []
        assert len(result["errors"]) == 1


class TestLoadFileToTableService:
    """Tests for StorageService.load_file_to_table()."""

    def test_load_success(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.import_table_async.return_value = {
            "results": {"importedRowsCount": 100, "warnings": []},
        }
        service = _make_service(store, mock_client)

        result = service.load_file_to_table(alias="test", file_id=12345, table_id="in.c-data.users")

        assert result["imported_rows"] == 100
        assert result["file_id"] == 12345
        assert result["table_id"] == "in.c-data.users"
        mock_client.import_table_async.assert_called_once_with(
            table_id="in.c-data.users",
            file_id=12345,
            incremental=False,
            delimiter=",",
            enclosure='"',
            branch_id=None,
        )

    def test_load_incremental(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.import_table_async.return_value = {
            "results": {"importedRowsCount": 50, "warnings": []},
        }
        service = _make_service(store, mock_client)

        result = service.load_file_to_table(
            alias="test",
            file_id=12345,
            table_id="in.c-data.users",
            incremental=True,
        )

        assert result["incremental"] is True
        call_kwargs = mock_client.import_table_async.call_args[1]
        assert call_kwargs["incremental"] is True


class TestUnloadTableToFileService:
    """Tests for StorageService.unload_table_to_file()."""

    def test_unload_without_download(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.export_table_async.return_value = {
            "results": {"file": {"id": 99}},
        }
        mock_client.get_file_info.return_value = {
            "id": 99,
            "name": "export.csv.gz",
            "sizeBytes": 2048,
            "isSliced": False,
            "tags": [],
        }
        service = _make_service(store, mock_client)

        result = service.unload_table_to_file(alias="test", table_id="in.c-data.users")

        assert result["file_id"] == 99
        assert result["downloaded"] is False
        mock_client.download_file.assert_not_called()

    def test_unload_with_tags(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.export_table_async.return_value = {
            "results": {"file": {"id": 99}},
        }
        mock_client.get_file_info.return_value = {
            "id": 99,
            "name": "export.csv.gz",
            "sizeBytes": 2048,
            "isSliced": False,
            "tags": ["export", "daily"],
        }
        service = _make_service(store, mock_client)

        service.unload_table_to_file(
            alias="test",
            table_id="in.c-data.users",
            tags=["export", "daily"],
        )

        assert mock_client.tag_file.call_count == 2
        mock_client.tag_file.assert_any_call(99, "export", branch_id=None)
        mock_client.tag_file.assert_any_call(99, "daily", branch_id=None)

    def test_unload_with_download(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.export_table_async.return_value = {
            "results": {"file": {"id": 99}},
        }
        mock_client.get_file_info.return_value = {
            "id": 99,
            "name": "export.csv.gz",
            "sizeBytes": 2048,
            "isSliced": False,
            "tags": [],
            "url": "https://storage.example.com/export.csv.gz",
        }
        mock_client.download_file.return_value = 2048
        service = _make_service(store, mock_client)

        result = service.unload_table_to_file(
            alias="test",
            table_id="in.c-data.users",
            download=True,
            output_path=str(tmp_path / "out.csv"),
        )

        assert result["downloaded"] is True
        assert result["downloaded_bytes"] == 2048
        mock_client.download_file.assert_called_once()

    def test_unload_parquet_without_download(self, tmp_path: Path) -> None:
        """Parquet export forwards fileType=parquet to export-async."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.export_table_async.return_value = {
            "results": {"file": {"id": 77}},
        }
        mock_client.get_file_info.return_value = {
            "id": 77,
            "name": "export.parquet",
            "sizeBytes": 10240,
            "isSliced": True,
            "tags": [],
        }
        service = _make_service(store, mock_client)

        result = service.unload_table_to_file(
            alias="test",
            table_id="in.c-data.users",
            file_type="parquet",
        )

        assert result["file_id"] == 77
        assert result["file_type"] == "parquet"
        assert result["is_sliced"] is True
        assert result["downloaded"] is False
        # Verify fileType reached the client
        _, kwargs = mock_client.export_table_async.call_args
        assert kwargs["file_type"] == "parquet"

    def test_unload_parquet_with_download_saves_slices_to_dir(self, tmp_path: Path) -> None:
        """Parquet + download stores each slice as its own file in a directory."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.export_table_async.return_value = {
            "results": {"file": {"id": 77}},
        }
        mock_client.get_file_info.return_value = {
            "id": 77,
            "name": "users.parquet",
            "sizeBytes": 20000,
            "isSliced": True,
            "tags": [],
        }
        out_dir = tmp_path / "parquet_out"
        mock_client.download_sliced_file_to_dir.return_value = {
            "output_dir": str(out_dir.resolve()),
            "slice_count": 3,
            "total_bytes": 18000,
            "slices": [
                {"path": str(out_dir / "part-1.parquet"), "size_bytes": 6000},
                {"path": str(out_dir / "part-2.parquet"), "size_bytes": 6000},
                {"path": str(out_dir / "part-3.parquet"), "size_bytes": 6000},
            ],
        }
        service = _make_service(store, mock_client)

        result = service.unload_table_to_file(
            alias="test",
            table_id="in.c-data.users",
            file_type="parquet",
            download=True,
            output_path=str(out_dir),
        )

        assert result["downloaded"] is True
        assert result["slice_count"] == 3
        assert result["downloaded_bytes"] == 18000
        assert len(result["slices"]) == 3
        mock_client.download_sliced_file_to_dir.assert_called_once()
        # CSV concat path must NOT be used for parquet
        mock_client.download_sliced_file.assert_not_called()

    def test_unload_invalid_file_type_rejected(self, tmp_path: Path) -> None:
        """Unknown file_type must raise validation error before any API call."""
        import pytest

        from keboola_agent_cli.errors import KeboolaApiError

        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        with pytest.raises(KeboolaApiError) as exc_info:
            service.unload_table_to_file(
                alias="test",
                table_id="in.c-data.users",
                file_type="json",
            )
        assert exc_info.value.error_code == "VALIDATION_ERROR"
        mock_client.export_table_async.assert_not_called()


# ------------------------------------------------------------------
# Client layer tests
# ------------------------------------------------------------------


class TestClientListFiles:
    """Tests for KeboolaClient.list_files()."""

    def test_list_files_basic(self, httpx_mock) -> None:
        from keboola_agent_cli.client import KeboolaClient

        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/files?limit=20&offset=0",
            json=[SAMPLE_FILE],
        )
        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        result = client.list_files()
        assert len(result) == 1
        assert result[0]["id"] == 12345
        client.close()

    def test_list_files_with_tags(self, httpx_mock) -> None:
        from keboola_agent_cli.client import KeboolaClient

        httpx_mock.add_response(json=[SAMPLE_FILE])
        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        result = client.list_files(tags=["report", "monthly"])
        assert len(result) == 1

        request = httpx_mock.get_requests()[0]
        assert "tags%5B0%5D=report" in str(request.url)
        assert "tags%5B1%5D=monthly" in str(request.url)
        client.close()

    def test_list_files_with_branch(self, httpx_mock) -> None:
        from keboola_agent_cli.client import KeboolaClient

        httpx_mock.add_response(json=[])
        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        client.list_files(branch_id=42)
        request = httpx_mock.get_requests()[0]
        assert "/v2/storage/branch/42/files" in str(request.url)
        client.close()


class TestClientUploadFile:
    """Tests for KeboolaClient.upload_file()."""

    def test_upload_file_calls_prepare_and_cloud(self, tmp_path: Path) -> None:
        from keboola_agent_cli.client import KeboolaClient

        test_file = tmp_path / "test.csv"
        test_file.write_text("a,b\n1,2\n")

        with (
            patch.object(KeboolaClient, "prepare_file_upload") as mock_prepare,
            patch.object(KeboolaClient, "_upload_to_cloud") as mock_upload,
        ):
            mock_prepare.return_value = {
                "id": 55,
                "name": "test.csv",
                "tags": ["tag1"],
                "isPermanent": False,
                "created": "2026-04-12T10:00:00+0000",
            }

            client = KeboolaClient(
                stack_url="https://connection.keboola.com",
                token=TEST_TOKEN,
            )
            result = client.upload_file(
                file_path=str(test_file),
                tags=["tag1"],
            )

            assert result["id"] == 55
            mock_prepare.assert_called_once()
            mock_upload.assert_called_once()
            client.close()


class TestClientDeleteFile:
    """Tests for KeboolaClient.delete_file()."""

    def test_delete_file(self, httpx_mock) -> None:
        from keboola_agent_cli.client import KeboolaClient

        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/files/12345",
            method="DELETE",
            status_code=204,
        )
        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        client.delete_file(12345)
        assert len(httpx_mock.get_requests()) == 1
        client.close()


class TestClientTagFile:
    """Tests for KeboolaClient.tag_file() and untag_file()."""

    def test_tag_file(self, httpx_mock) -> None:
        from keboola_agent_cli.client import KeboolaClient

        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/files/12345/tags",
            method="POST",
            status_code=201,
        )
        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        client.tag_file(12345, "new-tag")
        request = httpx_mock.get_requests()[0]
        assert b"tag=new-tag" in request.content
        client.close()

    def test_untag_file(self, httpx_mock) -> None:
        from keboola_agent_cli.client import KeboolaClient

        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/files/12345/tags/old-tag",
            method="DELETE",
            status_code=204,
        )
        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        client.untag_file(12345, "old-tag")
        assert len(httpx_mock.get_requests()) == 1
        client.close()


# ------------------------------------------------------------------
# CLI layer tests
# ------------------------------------------------------------------


class TestFileListCli:
    """Tests for `kbagent storage files` command."""

    def test_file_list_json(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            MockSvc.return_value.list_files.return_value = {
                "project_alias": "test",
                "files": [SAMPLE_FILE],
                "count": 1,
            }
            result = runner.invoke(
                app,
                ["--json", "storage", "files", "--project", "test"],
            )

        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["count"] == 1

    def test_file_list_with_tags(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            MockSvc.return_value.list_files.return_value = {
                "project_alias": "test",
                "files": [SAMPLE_FILE],
                "count": 1,
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "files",
                    "--project",
                    "test",
                    "--tag",
                    "report",
                    "--tag",
                    "monthly",
                ],
            )

        assert result.exit_code == 0
        call_kwargs = MockSvc.return_value.list_files.call_args[1]
        assert call_kwargs["tags"] == ["report", "monthly"]

    def test_file_list_empty_human(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            MockSvc.return_value.list_files.return_value = {
                "project_alias": "test",
                "files": [],
                "count": 0,
            }
            result = runner.invoke(
                app,
                ["storage", "files", "--project", "test"],
            )

        assert result.exit_code == 0
        assert "No files found" in result.output


class TestFileUploadCli:
    """Tests for `kbagent storage file-upload` command."""

    def test_file_upload_json(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        test_file = tmp_path / "upload.csv"
        test_file.write_text("a,b\n1,2\n")

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            MockSvc.return_value.upload_file.return_value = {
                "id": 99,
                "name": "upload.csv",
                "file_size_bytes": 10,
                "tags": ["test"],
                "isPermanent": False,
                "project_alias": "test",
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "file-upload",
                    "--project",
                    "test",
                    "--file",
                    str(test_file),
                    "--tag",
                    "test",
                ],
            )

        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["id"] == 99

    def test_file_upload_missing_file(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "file-upload",
                    "--project",
                    "test",
                    "--file",
                    "/nonexistent/file.csv",
                ],
            )

        assert result.exit_code == 2


class TestFileDownloadCli:
    """Tests for `kbagent storage file-download` command."""

    def test_file_download_by_id_json(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            MockSvc.return_value.download_file.return_value = {
                "project_alias": "test",
                "file_id": 12345,
                "file_name": "data.csv",
                "output_path": "/tmp/data.csv",
                "file_size_bytes": 1024,
                "is_sliced": False,
            }
            result = runner.invoke(
                app,
                ["--json", "storage", "file-download", "--project", "test", "--file-id", "12345"],
            )

        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["file_id"] == 12345

    def test_file_download_no_id_no_tag(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(
                app,
                ["--json", "storage", "file-download", "--project", "test"],
            )

        assert result.exit_code == 2


class TestFileInfoCli:
    """Tests for `kbagent storage file-detail` command."""

    def test_file_info_json(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            MockSvc.return_value.get_file_info.return_value = SAMPLE_FILE.copy()
            result = runner.invoke(
                app,
                ["--json", "storage", "file-detail", "--project", "test", "--file-id", "12345"],
            )

        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["id"] == 12345

    def test_file_info_human(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            MockSvc.return_value.get_file_info.return_value = {
                **SAMPLE_FILE,
                "creatorToken": {"id": 1, "description": "My Token"},
            }
            result = runner.invoke(
                app,
                ["storage", "file-detail", "--project", "test", "--file-id", "12345"],
            )

        assert result.exit_code == 0
        assert "12345" in result.output
        assert "test-data.csv" in result.output


class TestFileDeleteCli:
    """Tests for `kbagent storage file-delete` command."""

    def test_file_delete_json(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            MockSvc.return_value.delete_files.return_value = {
                "project_alias": "test",
                "deleted": [12345],
                "failed": [],
                "dry_run": False,
            }
            result = runner.invoke(
                app,
                ["--json", "storage", "file-delete", "--project", "test", "--file-id", "12345"],
            )

        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert 12345 in data["deleted"]


class TestFileTagCli:
    """Tests for `kbagent storage file-tag` command."""

    def test_file_tag_add(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            MockSvc.return_value.tag_file.return_value = {
                "project_alias": "test",
                "file_id": 12345,
                "added": ["new-tag"],
                "removed": [],
                "errors": [],
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "file-tag",
                    "--project",
                    "test",
                    "--file-id",
                    "12345",
                    "--add",
                    "new-tag",
                ],
            )

        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert "new-tag" in data["added"]

    def test_file_tag_no_add_no_remove(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(
                app,
                ["--json", "storage", "file-tag", "--project", "test", "--file-id", "12345"],
            )

        assert result.exit_code == 2


class TestLoadFileCli:
    """Tests for `kbagent storage load-file` command."""

    def test_load_file_json(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            MockSvc.return_value.load_file_to_table.return_value = {
                "project_alias": "test",
                "file_id": 12345,
                "table_id": "in.c-data.users",
                "incremental": False,
                "imported_rows": 100,
                "warnings": [],
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "load-file",
                    "--project",
                    "test",
                    "--file-id",
                    "12345",
                    "--table-id",
                    "in.c-data.users",
                ],
            )

        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["imported_rows"] == 100


class TestUnloadTableCli:
    """Tests for `kbagent storage unload-table` command."""

    def test_unload_json(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            MockSvc.return_value.unload_table_to_file.return_value = {
                "project_alias": "test",
                "table_id": "in.c-data.users",
                "file_id": 99,
                "file_name": "export.csv",
                "file_size_bytes": 2048,
                "is_sliced": False,
                "tags": ["export"],
                "downloaded": False,
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "unload-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-data.users",
                    "--tag",
                    "export",
                ],
            )

        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["file_id"] == 99
        assert data["tags"] == ["export"]

    def test_unload_with_download_json(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            MockSvc.return_value.unload_table_to_file.return_value = {
                "project_alias": "test",
                "table_id": "in.c-data.users",
                "file_id": 99,
                "file_name": "export.csv",
                "file_size_bytes": 2048,
                "is_sliced": False,
                "tags": [],
                "downloaded": True,
                "output_path": "/tmp/users.csv",
                "downloaded_bytes": 2048,
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "unload-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-data.users",
                    "--download",
                ],
            )

        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["downloaded"] is True


# ------------------------------------------------------------------
# Helper function tests
# ------------------------------------------------------------------


class TestFormatFileSize:
    """Tests for _format_file_size helper."""

    def test_none(self) -> None:
        from keboola_agent_cli.commands.storage import _format_file_size

        assert _format_file_size(None) == "unknown"

    def test_bytes(self) -> None:
        from keboola_agent_cli.commands.storage import _format_file_size

        assert _format_file_size(512) == "512 B"

    def test_kilobytes(self) -> None:
        from keboola_agent_cli.commands.storage import _format_file_size

        assert _format_file_size(2048) == "2.0 KB"

    def test_megabytes(self) -> None:
        from keboola_agent_cli.commands.storage import _format_file_size

        assert _format_file_size(5 * 1024 * 1024) == "5.00 MB"

    def test_gigabytes(self) -> None:
        from keboola_agent_cli.commands.storage import _format_file_size

        assert _format_file_size(2 * 1024 * 1024 * 1024) == "2.00 GB"


# ------------------------------------------------------------------
# Branch-scoped file operations (issue #161)
# ------------------------------------------------------------------


class TestClientGetFileInfoBranch:
    """Verify get_file_info uses branch-scoped URL prefix."""

    def test_get_file_info_without_branch(self, httpx_mock) -> None:
        from keboola_agent_cli.client import KeboolaClient

        httpx_mock.add_response(json=SAMPLE_FILE)
        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        client.get_file_info(12345)
        request = httpx_mock.get_requests()[0]
        assert "/v2/storage/files/12345" in str(request.url)
        assert "/branch/" not in str(request.url)
        client.close()

    def test_get_file_info_with_branch(self, httpx_mock) -> None:
        from keboola_agent_cli.client import KeboolaClient

        httpx_mock.add_response(json=SAMPLE_FILE)
        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        client.get_file_info(12345, branch_id=42)
        request = httpx_mock.get_requests()[0]
        assert "/v2/storage/branch/42/files/12345" in str(request.url)
        client.close()


class TestClientDeleteFileBranch:
    """Verify delete_file uses branch-scoped URL prefix."""

    def test_delete_file_with_branch(self, httpx_mock) -> None:
        from keboola_agent_cli.client import KeboolaClient

        httpx_mock.add_response(method="DELETE", status_code=204)
        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        client.delete_file(12345, branch_id=42)
        request = httpx_mock.get_requests()[0]
        assert "/v2/storage/branch/42/files/12345" in str(request.url)
        client.close()


class TestClientTagFileBranch:
    """Verify tag_file and untag_file use branch-scoped URL prefix."""

    def test_tag_file_with_branch(self, httpx_mock) -> None:
        from keboola_agent_cli.client import KeboolaClient

        httpx_mock.add_response(method="POST", status_code=201)
        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        client.tag_file(12345, "my-tag", branch_id=42)
        request = httpx_mock.get_requests()[0]
        assert "/v2/storage/branch/42/files/12345/tags" in str(request.url)
        client.close()

    def test_untag_file_with_branch(self, httpx_mock) -> None:
        from keboola_agent_cli.client import KeboolaClient

        httpx_mock.add_response(method="DELETE", status_code=204)
        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        client.untag_file(12345, "old-tag", branch_id=42)
        request = httpx_mock.get_requests()[0]
        assert "/v2/storage/branch/42/files/12345/tags/old-tag" in str(request.url)
        client.close()


class TestUnloadTableToFileBranchService:
    """Verify unload_table_to_file passes branch_id to file operations."""

    def test_unload_with_branch_passes_branch_to_file_ops(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.export_table_async.return_value = {
            "results": {"file": {"id": 99}},
        }
        mock_client.get_file_info.return_value = {
            "id": 99,
            "name": "export.csv.gz",
            "sizeBytes": 2048,
            "isSliced": False,
            "tags": ["export"],
        }
        service = _make_service(store, mock_client)

        service.unload_table_to_file(
            alias="test",
            table_id="in.c-data.users",
            tags=["export"],
            branch_id=33,
        )

        mock_client.export_table_async.assert_called_once_with(
            table_id="in.c-data.users",
            columns=None,
            limit=None,
            branch_id=33,
            file_type="csv",
        )
        mock_client.tag_file.assert_called_once_with(99, "export", branch_id=33)
        mock_client.get_file_info.assert_called_once_with(99, branch_id=33)


class TestClientDownloadSlicedFileToDir:
    """Tests for KeboolaClient.download_sliced_file_to_dir()."""

    def test_writes_each_slice_as_separate_file(self, tmp_path: Path, httpx_mock) -> None:
        """Manifest entries become individual files + _manifest.json is kept."""
        import json as json_mod

        from keboola_agent_cli.client import KeboolaClient

        manifest = {
            "entries": [
                {"url": "s3://bucket/export/part-1.parquet"},
                {"url": "s3://bucket/export/part-2.parquet"},
            ]
        }
        file_detail = {
            "id": 42,
            "name": "users.parquet",
            "isSliced": True,
            "url": "https://storage.example.com/manifest?sig=abc",
            "provider": "aws",
        }
        httpx_mock.add_response(
            url="https://storage.example.com/manifest?sig=abc",
            content=json_mod.dumps(manifest).encode(),
        )

        # Stub the cloud downloader to avoid real S3/GCS calls. stream_to_file
        # is the OOM-safe path (issue #187): it writes directly to disk rather
        # than returning bytes, so the fake has to side-effect the destination.
        slice_payloads = iter([b"PAR1_slice_1", b"PAR1_slice_2"])

        def _fake_stream_to_file(url: str, dest, decompress_gzip: bool) -> int:
            payload = next(slice_payloads)
            Path(dest).write_bytes(payload)
            return len(payload)

        fake_downloader = MagicMock()
        fake_downloader.resolve_base_url.return_value = "https://bucket.s3.amazonaws.com/export"
        fake_downloader.resolve_slice_url.side_effect = lambda base, entry_url, fd: entry_url
        fake_downloader.stream_to_file.side_effect = _fake_stream_to_file

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )

        out_dir = tmp_path / "parquet_slices"
        with patch(
            "keboola_agent_cli.client._CloudDownloader.create",
            return_value=fake_downloader,
        ):
            result = client.download_sliced_file_to_dir(file_detail, str(out_dir))

        assert result["slice_count"] == 2
        assert result["total_bytes"] == len(b"PAR1_slice_1") + len(b"PAR1_slice_2")
        # Each slice ended up as its own file with the original basename
        assert (out_dir / "part-1.parquet").read_bytes() == b"PAR1_slice_1"
        assert (out_dir / "part-2.parquet").read_bytes() == b"PAR1_slice_2"
        # Manifest is preserved for traceability
        assert (out_dir / "_manifest.json").exists()
        assert json_mod.loads((out_dir / "_manifest.json").read_text()) == manifest

        client.close()

    def test_gunzips_gz_slices_and_strips_suffix(self, tmp_path: Path, httpx_mock) -> None:
        """CSV slices are often .gz; we decompress and drop the suffix."""
        import gzip
        import json as json_mod

        from keboola_agent_cli.client import KeboolaClient

        payload = b"col1,col2\n1,2\n3,4\n"
        gz_payload = gzip.compress(payload)

        manifest = {"entries": [{"url": "s3://bucket/export/part-0.csv.gz"}]}
        file_detail = {
            "id": 43,
            "name": "data.csv.gz",
            "isSliced": True,
            "url": "https://storage.example.com/manifest2",
            "provider": "aws",
        }
        httpx_mock.add_response(
            url="https://storage.example.com/manifest2",
            content=json_mod.dumps(manifest).encode(),
        )

        # The real stream_to_file streams+decompresses gzip; mirror that so
        # the test still verifies the .gz-suffix handling in the caller.
        def _fake_stream_to_file(url: str, dest, decompress_gzip: bool) -> int:
            data = payload if decompress_gzip else gz_payload
            Path(dest).write_bytes(data)
            return len(data)

        fake_downloader = MagicMock()
        fake_downloader.resolve_base_url.return_value = "https://bucket.s3.amazonaws.com/export"
        fake_downloader.resolve_slice_url.side_effect = lambda base, entry_url, fd: entry_url
        fake_downloader.stream_to_file.side_effect = _fake_stream_to_file

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )

        out_dir = tmp_path / "csv_slices"
        with patch(
            "keboola_agent_cli.client._CloudDownloader.create",
            return_value=fake_downloader,
        ):
            result = client.download_sliced_file_to_dir(file_detail, str(out_dir))

        assert result["slice_count"] == 1
        # .gz suffix stripped, content decompressed
        assert (out_dir / "part-0.csv").read_bytes() == payload
        assert not (out_dir / "part-0.csv.gz").exists()

        client.close()

    def test_empty_manifest_raises(self, tmp_path: Path, httpx_mock) -> None:
        """Empty manifest is a server-side problem -- surface it loudly."""
        import json as json_mod

        from keboola_agent_cli.client import KeboolaClient

        file_detail = {
            "id": 44,
            "name": "empty.parquet",
            "isSliced": True,
            "url": "https://storage.example.com/empty-manifest",
            "provider": "aws",
        }
        httpx_mock.add_response(
            url="https://storage.example.com/empty-manifest",
            content=json_mod.dumps({"entries": []}).encode(),
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )

        with (
            patch(
                "keboola_agent_cli.client._CloudDownloader.create",
                return_value=MagicMock(),
            ),
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client.download_sliced_file_to_dir(file_detail, str(tmp_path / "out"))

        assert exc_info.value.error_code == "EXPORT_EMPTY_MANIFEST"
        client.close()
