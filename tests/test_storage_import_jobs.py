"""Async Storage imports and `storage job-detail` (issue #834).

A table import is an async Storage job. For a ~200 GB upload it can outlive
any local wait, and giving up locally does not stop it. These tests pin that
every layer keeps the job and the uploaded file reachable:

- client: ``wait=False`` returns the enqueue response without polling; a wait
  timeout is NOT retryable and names the job + file; an enqueue failure after
  the upload carries the file ID;
- service: import results gain ``file_id`` / ``job_id`` / ``job_status``; the
  timeout message names the ``storage job-detail`` command; the gzip-aware
  CSV header reader;
- CLI: ``upload-table`` / ``load-file`` ``--no-wait`` / ``--timeout`` and the
  new ``job-detail`` exit codes (0 / 1 / 2 / 4);
- serve: the ``GET /storage/jobs/{project}/{job_id}`` route, its permission
  check, and the upload / load-file wait fields.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.client import KeboolaClient, TableUploadOutcome
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import AppConfig, ProjectConfig
from keboola_agent_cli.services._storage_jobs import read_csv_header
from keboola_agent_cli.services.storage_service import StorageService

runner = CliRunner()

_BASE = "https://connection.keboola.com"
_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
_IMPORT_URL = f"{_BASE}/v2/storage/tables/in.c-b.users/import-async"


def _make_store(tmp_path: Path) -> ConfigStore:
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    store = ConfigStore(config_dir=config_dir)
    store.save(AppConfig(projects={"test": ProjectConfig(stack_url=_BASE, token=_TOKEN)}))
    return store


def _make_service(store: ConfigStore, mock_client: MagicMock) -> StorageService:
    return StorageService(config_store=store, client_factory=lambda url, token: mock_client)


def _invoke(store: ConfigStore, svc: MagicMock | StorageService, args: list[str]) -> Any:
    with (
        patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
        patch("keboola_agent_cli.cli.StorageService", return_value=svc),
    ):
        return runner.invoke(app, args)


def _import_timeout(job_id: int = 55, file_id: int = 100) -> KeboolaApiError:
    """The error the client raises when the import wait budget runs out."""
    return KeboolaApiError(
        message=f"Storage import job {job_id} did not finish within 600s.",
        status_code=504,
        error_code=ErrorCode.STORAGE_JOB_TIMEOUT,
        retryable=False,
        details={"job_id": job_id, "file_id": file_id},
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class TestClientImport:
    def test_no_wait_returns_enqueue_response_without_polling(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=_IMPORT_URL, method="POST", json={"id": 42, "status": "waiting"}, status_code=202
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            job = client.import_table_async(table_id="in.c-b.users", file_id=9, wait=False)
        assert job == {"id": 42, "status": "waiting"}
        assert [r.method for r in httpx_mock.get_requests()] == ["POST"]

    def test_wait_timeout_is_not_retryable_and_names_job_and_file(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=_IMPORT_URL, method="POST", json={"id": 42, "status": "waiting"}, status_code=202
        )
        with (
            KeboolaClient(stack_url=_BASE, token=_TOKEN) as client,
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client.import_table_async(table_id="in.c-b.users", file_id=9, max_wait=0)
        exc = exc_info.value
        assert exc.error_code == ErrorCode.STORAGE_JOB_TIMEOUT
        assert exc.retryable is False
        assert exc.details == {"job_id": 42, "file_id": 9}
        assert "42" in exc.message and "file 9" in exc.message
        assert "keeps running server-side" in exc.message
        assert "duplicate rows" in exc.message

    def test_job_failure_carries_file_id(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=_IMPORT_URL,
            method="POST",
            json={"id": 42, "status": "error", "error": {"message": "bad header"}},
            status_code=202,
        )
        with (
            KeboolaClient(stack_url=_BASE, token=_TOKEN) as client,
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client.import_table_async(table_id="in.c-b.users", file_id=9)
        assert exc_info.value.error_code == ErrorCode.STORAGE_JOB_FAILED
        assert exc_info.value.details == {"job_id": 42, "file_id": 9}

    def _prepare(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/files/prepare", method="POST", json={"id": 100}
        )

    def test_upload_no_wait_skips_polling(self, httpx_mock, tmp_path: Path) -> None:
        csv_file = tmp_path / "d.csv"
        csv_file.write_text("id\n1\n")
        self._prepare(httpx_mock)
        httpx_mock.add_response(
            url=_IMPORT_URL, method="POST", json={"id": 42, "status": "waiting"}, status_code=202
        )
        progress = MagicMock()
        with (
            KeboolaClient(stack_url=_BASE, token=_TOKEN) as client,
            patch.object(KeboolaClient, "_upload_to_cloud") as upload,
        ):
            outcome = client.upload_table(
                table_id="in.c-b.users", file_path=str(csv_file), wait=False, on_progress=progress
            )
        assert outcome == TableUploadOutcome(file_id=100, job={"id": 42, "status": "waiting"})
        assert upload.call_args.kwargs["on_progress"] is progress
        assert not any("/v2/storage/jobs/" in str(r.url) for r in httpx_mock.get_requests())

    def test_enqueue_failure_after_upload_keeps_file_id(self, httpx_mock, tmp_path: Path) -> None:
        csv_file = tmp_path / "d.csv"
        csv_file.write_text("id\n1\n")
        self._prepare(httpx_mock)
        httpx_mock.add_response(
            url=_IMPORT_URL,
            method="POST",
            json={"error": "Table in.c-b.users not found", "code": "storage.tables.notFound"},
            status_code=404,
        )
        with (
            KeboolaClient(stack_url=_BASE, token=_TOKEN) as client,
            patch.object(KeboolaClient, "_upload_to_cloud"),
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client.upload_table(table_id="in.c-b.users", file_path=str(csv_file))
        assert exc_info.value.details["file_id"] == 100
        assert "Storage file 100" in exc_info.value.message
        assert "job_id" not in exc_info.value.details


class TestClientStorageJob:
    def test_get_storage_job_is_one_get(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/jobs/7", method="GET", json={"id": 7, "status": "waiting"}
        )
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            assert client.get_storage_job(7)["status"] == "waiting"
        assert len(httpx_mock.get_requests()) == 1

    def test_follow_returns_failed_job_instead_of_raising(self, httpx_mock) -> None:
        failed = {"id": 7, "status": "error", "error": {"message": "boom", "code": "x"}}
        httpx_mock.add_response(url=f"{_BASE}/v2/storage/jobs/7", method="GET", json=failed)
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            assert client.follow_storage_job(7) == failed

    def test_follow_timeout_names_job(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/jobs/7", method="GET", json={"id": 7, "status": "processing"}
        )
        with (
            KeboolaClient(stack_url=_BASE, token=_TOKEN) as client,
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client.follow_storage_job(7, max_wait=0)
        assert exc_info.value.error_code == ErrorCode.STORAGE_JOB_TIMEOUT
        assert exc_info.value.details == {"job_id": 7}


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class TestServiceImport:
    def test_upload_no_wait_reports_queued_job(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "d.csv"
        csv_file.write_text("id\n1\n")
        client = MagicMock()
        client.upload_table.return_value = TableUploadOutcome(
            file_id=100, job={"id": 55, "status": "waiting"}
        )
        service = _make_service(_make_store(tmp_path), client)
        result = service.upload_table(
            alias="test",
            table_id="in.c-b.users",
            file_path=str(csv_file),
            auto_create=False,
            wait=False,
            timeout=30,
        )
        assert (result["file_id"], result["job_id"], result["job_status"]) == (100, 55, "waiting")
        assert result["imported_rows"] is None and result["warnings"] == []
        kwargs = client.upload_table.call_args.kwargs
        assert kwargs["wait"] is False and kwargs["max_wait"] == 30

    def test_upload_timeout_names_job_detail_command(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "d.csv"
        csv_file.write_text("id\n1\n")
        client = MagicMock()
        client.upload_table.side_effect = _import_timeout()
        service = _make_service(_make_store(tmp_path), client)
        with pytest.raises(KeboolaApiError) as exc_info:
            service.upload_table(
                alias="test", table_id="in.c-b.users", file_path=str(csv_file), auto_create=False
            )
        exc = exc_info.value
        assert "kbagent storage job-detail --project test --job-id 55 --wait" in exc.message
        assert exc.retryable is False
        assert exc.details == {"job_id": 55, "file_id": 100}
        client.close.assert_called_once()

    def test_enqueue_failure_names_load_file_command(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "d.csv"
        csv_file.write_text("id\n1\n")
        client = MagicMock()
        client.upload_table.side_effect = KeboolaApiError(
            "Table not found",
            status_code=404,
            error_code=ErrorCode.NOT_FOUND,
            details={"file_id": 100},
        )
        service = _make_service(_make_store(tmp_path), client)
        with pytest.raises(KeboolaApiError) as exc_info:
            service.upload_table(
                alias="test", table_id="in.c-b.users", file_path=str(csv_file), auto_create=False
            )
        assert (
            "kbagent storage load-file --project test --file-id 100 --table-id in.c-b.users"
            in exc_info.value.message
        )
        assert exc_info.value.error_code == ErrorCode.NOT_FOUND

    @pytest.mark.parametrize("bad", [0, -1, float("inf"), float("nan")])
    def test_invalid_timeout_rejected_before_any_call(self, tmp_path: Path, bad: float) -> None:
        client = MagicMock()
        service = _make_service(_make_store(tmp_path), client)
        with pytest.raises(ValueError, match="--timeout"):
            service.load_file_to_table(alias="test", file_id=1, table_id="in.c-b.t", timeout=bad)
        with pytest.raises(ValueError, match="--timeout"):
            service.storage_job_detail(alias="test", job_id=1, timeout=bad)
        client.import_table_async.assert_not_called()
        client.get_storage_job.assert_not_called()

    def test_load_file_no_wait(self, tmp_path: Path) -> None:
        client = MagicMock()
        client.import_table_async.return_value = {"id": 56, "status": "processing"}
        service = _make_service(_make_store(tmp_path), client)
        result = service.load_file_to_table(
            alias="test", file_id=1, table_id="in.c-b.t", wait=False
        )
        assert (result["job_id"], result["job_status"], result["imported_rows"]) == (
            56,
            "processing",
            None,
        )
        assert client.import_table_async.call_args.kwargs["wait"] is False


class TestServiceJobDetail:
    _JOB: ClassVar[dict[str, Any]] = {
        "id": 55,
        "status": "success",
        "operationName": "tableImport",
        "tableId": "in.c-b.users",
        "operationParams": {"source": {"type": "file", "fileId": 100}},
        "createdTime": "2026-10-09T10:00:00+0200",
        "startTime": "2026-10-09T10:00:01+0200",
        "endTime": "2026-10-09T10:05:00+0200",
        "results": {"importedRowsCount": 12, "warnings": ["w1"]},
    }

    def test_without_wait_is_one_get(self, tmp_path: Path) -> None:
        client = MagicMock()
        client.get_storage_job.return_value = dict(self._JOB)
        service = _make_service(_make_store(tmp_path), client)
        result = service.storage_job_detail(alias="test", job_id=55)
        assert result == {
            "project_alias": "test",
            "job_id": 55,
            "status": "success",
            "operation_name": "tableImport",
            "table_id": "in.c-b.users",
            "file_id": 100,
            "created_time": "2026-10-09T10:00:00+0200",
            "start_time": "2026-10-09T10:00:01+0200",
            "end_time": "2026-10-09T10:05:00+0200",
            "imported_rows": 12,
            "warnings": ["w1"],
            "results": {"importedRowsCount": 12, "warnings": ["w1"]},
            "error": None,
        }
        client.get_storage_job.assert_called_once_with(55)
        client.follow_storage_job.assert_not_called()
        client.close.assert_called_once()

    def test_wait_keeps_failed_job_details(self, tmp_path: Path) -> None:
        client = MagicMock()
        client.follow_storage_job.return_value = {
            "id": 55,
            "status": "error",
            "tableId": "in.c-b.users",
            "error": {"message": "Invalid CSV", "code": "storage.csvImport"},
        }
        service = _make_service(_make_store(tmp_path), client)
        result = service.storage_job_detail(alias="test", job_id=55, wait=True, timeout=10)
        assert result["error"] == {"message": "Invalid CSV", "code": "storage.csvImport"}
        assert result["table_id"] == "in.c-b.users" and result["file_id"] is None
        client.follow_storage_job.assert_called_once_with(55, max_wait=10)


class TestReadCsvHeader:
    def test_plain_csv_with_enclosure_and_delimiter(self, tmp_path: Path) -> None:
        f = tmp_path / "a.csv"
        f.write_text("'id';'first;name'\n1;x\n", encoding="utf-8")
        assert read_csv_header(str(f), ";", "'") == ["id", "first;name"]

    def test_gzip_detected_by_magic_bytes_with_bom(self, tmp_path: Path) -> None:
        f = tmp_path / "data.csv"  # extension lies on purpose
        with gzip.open(f, "wt", encoding="utf-8-sig", newline="") as fh:
            fh.write('"id","a,b"\n1,2\n')
        assert read_csv_header(str(f)) == ["id", "a,b"]

    def test_empty_enclosure_means_no_quoting(self, tmp_path: Path) -> None:
        f = tmp_path / "a.csv"
        f.write_text('"id",name\n', encoding="utf-8")
        assert read_csv_header(str(f), ",", "") == ['"id"', "name"]

    def test_corrupt_gzip_is_a_value_error(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.csv.gz"
        f.write_bytes(b"\x1f\x8bnot really gzip")
        with pytest.raises(ValueError, match="corrupt gzip"):
            read_csv_header(str(f))

    def test_truncated_gzip_is_a_value_error(self, tmp_path: Path) -> None:
        f = tmp_path / "trunc.csv.gz"
        f.write_bytes(gzip.compress(b"id,name\n1,x\n")[:12])
        with pytest.raises(ValueError):
            read_csv_header(str(f))

    def test_undecodable_header_is_a_value_error(self, tmp_path: Path) -> None:
        f = tmp_path / "latin.csv"
        f.write_bytes("n\xe1zev\n".encode("latin-1"))
        with pytest.raises(ValueError, match="UTF-8"):
            read_csv_header(str(f))

    def test_auto_create_reads_gzip_header(self, tmp_path: Path) -> None:
        f = tmp_path / "d.csv.gz"
        f.write_bytes(gzip.compress(b"id|name\n1|x\n"))
        client = MagicMock()
        client.get_bucket_detail.return_value = {"id": "in.c-b"}
        client.list_tables.return_value = []
        client.upload_table.return_value = TableUploadOutcome(
            file_id=1, job={"id": 2, "status": "success", "results": {"importedRowsCount": 1}}
        )
        service = _make_service(_make_store(tmp_path), client)
        service.upload_table(alias="test", table_id="in.c-b.users", file_path=str(f), delimiter="|")
        columns = client.create_table.call_args.kwargs["columns"]
        assert [c["name"] for c in columns] == ["id", "name"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _job_summary(status: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "project_alias": "test",
        "job_id": 55,
        "status": status,
        "operation_name": "tableImport",
        "table_id": "in.c-b.users",
        "file_id": 100,
        "created_time": "2026-10-09T10:00:00+0200",
        "start_time": None,
        "end_time": None,
        "imported_rows": None,
        "warnings": [],
        "results": None,
        "error": None,
    }
    base.update(extra)
    return base


class TestJobDetailCli:
    _ARGS: ClassVar[list[str]] = ["storage", "job-detail", "--project", "test", "--job-id", "55"]

    @pytest.mark.parametrize("status", ["success", "waiting", "processing"])
    def test_json_exit_zero(self, tmp_path: Path, status: str) -> None:
        svc = MagicMock()
        svc.storage_job_detail.return_value = _job_summary(status)
        result = _invoke(_make_store(tmp_path), svc, ["--json", *self._ARGS])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["status"] == status
        svc.storage_job_detail.assert_called_once_with(
            alias="test", job_id=55, wait=False, timeout=None
        )

    def test_failed_job_exit_one_with_details(self, tmp_path: Path) -> None:
        svc = MagicMock()
        svc.storage_job_detail.return_value = _job_summary(
            "error", error={"message": "Invalid CSV", "code": "csv"}
        )
        result = _invoke(
            _make_store(tmp_path), svc, ["--json", *self._ARGS, "--wait", "--timeout", "30"]
        )
        assert result.exit_code == 1
        error = json.loads(result.output)["error"]
        assert error["code"] == ErrorCode.STORAGE_JOB_FAILED
        assert "Invalid CSV" in error["message"]
        assert error["details"]["table_id"] == "in.c-b.users"
        assert error["details"]["file_id"] == 100
        svc.storage_job_detail.assert_called_once_with(
            alias="test", job_id=55, wait=True, timeout=30.0
        )

    def test_wait_timeout_exit_four(self, tmp_path: Path) -> None:
        svc = MagicMock()
        svc.storage_job_detail.side_effect = KeboolaApiError(
            "Storage job 55 did not complete within 600s.",
            status_code=504,
            error_code=ErrorCode.STORAGE_JOB_TIMEOUT,
            retryable=True,
            details={"job_id": 55},
        )
        result = _invoke(_make_store(tmp_path), svc, ["--json", *self._ARGS, "--wait"])
        assert result.exit_code == 4
        assert json.loads(result.output)["error"]["details"] == {"job_id": 55}

    def test_invalid_timeout_exit_two(self, tmp_path: Path) -> None:
        svc = MagicMock()
        svc.storage_job_detail.side_effect = ValueError("--timeout must be a positive number")
        result = _invoke(_make_store(tmp_path), svc, ["--json", *self._ARGS, "--timeout", "0"])
        assert result.exit_code == 2

    def test_human_output(self, tmp_path: Path) -> None:
        svc = MagicMock()
        svc.storage_job_detail.return_value = _job_summary("success", imported_rows=12)
        result = _invoke(_make_store(tmp_path), svc, self._ARGS)
        assert result.exit_code == 0, result.output
        assert "tableImport" in result.output and "12" in result.output


class TestUploadAndLoadCli:
    def test_upload_no_wait_json(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "d.csv"
        csv_file.write_text("id\n1\n")
        svc = MagicMock()
        svc.upload_table.return_value = {
            "project_alias": "test",
            "table_id": "in.c-b.users",
            "incremental": False,
            "file_size_bytes": 5,
            "file_id": 100,
            "job_id": 55,
            "job_status": "waiting",
            "imported_rows": None,
            "warnings": [],
            "auto_created_bucket": False,
            "auto_created_table": False,
        }
        args = ["storage", "upload-table", "--project", "test", "--table-id", "in.c-b.users"]
        args += ["--file", str(csv_file), "--no-wait", "--timeout", "120"]
        result = _invoke(_make_store(tmp_path), svc, ["--json", *args])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert (data["file_id"], data["job_id"], data["job_status"]) == (100, 55, "waiting")
        kwargs = svc.upload_table.call_args.kwargs
        assert kwargs["wait"] is False and kwargs["timeout"] == 120.0
        # No progress bar outside a terminal.
        assert kwargs["on_progress"] is None

        human = _invoke(_make_store(tmp_path), svc, args)
        assert human.exit_code == 0, human.output
        assert "Import job 55 queued" in human.output
        assert "kbagent storage job-detail --project test --job-id 55 --wait" in human.output

    def test_upload_timeout_error_json(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "d.csv"
        csv_file.write_text("id\n1\n")
        svc = MagicMock()
        exc = _import_timeout()
        exc.message += (
            " Follow it with: kbagent storage job-detail --project test --job-id 55 --wait"
        )
        svc.upload_table.side_effect = exc
        result = _invoke(
            _make_store(tmp_path),
            svc,
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
        assert result.exit_code == 4
        error = json.loads(result.output)["error"]
        assert error["retryable"] is False
        assert error["details"] == {"job_id": 55, "file_id": 100}
        assert "--job-id 55" in error["message"]

    def test_load_file_no_wait(self, tmp_path: Path) -> None:
        svc = MagicMock()
        svc.load_file_to_table.return_value = {
            "project_alias": "test",
            "file_id": 100,
            "table_id": "in.c-b.users",
            "incremental": False,
            "job_id": 56,
            "job_status": "processing",
            "imported_rows": None,
            "warnings": [],
        }
        result = _invoke(
            _make_store(tmp_path),
            svc,
            [
                "storage",
                "load-file",
                "--project",
                "test",
                "--file-id",
                "100",
                "--table-id",
                "in.c-b.users",
                "--no-wait",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Import job 56 queued" in result.output
        kwargs = svc.load_file_to_table.call_args.kwargs
        assert kwargs["wait"] is False and kwargs["timeout"] is None


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


fastapi_testclient = pytest.importorskip("fastapi.testclient")

_AUTH = {"Authorization": "Bearer test-token"}


def _serve_client(tmp_path: Path, storage: MagicMock) -> Any:
    from keboola_agent_cli.server import create_app
    from keboola_agent_cli.server.dependencies import ServiceRegistry, get_registry

    registry = ServiceRegistry.__new__(ServiceRegistry)
    registry.storage = storage
    app = create_app(config_dir=str(tmp_path), auth_token="test-token")
    app.dependency_overrides[get_registry] = lambda: registry
    return fastapi_testclient.TestClient(app)


class TestServeRoutes:
    def test_job_detail_route(self, tmp_path: Path) -> None:
        storage = MagicMock()
        storage.storage_job_detail.return_value = _job_summary("error", error={"message": "x"})
        client = _serve_client(tmp_path, storage)
        res = client.get("/storage/jobs/proj/55?wait=true&timeout=12", headers=_AUTH)
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "error"
        storage.storage_job_detail.assert_called_once_with(
            alias="proj", job_id=55, wait=True, timeout=12.0
        )

    def test_job_detail_rejects_non_positive_timeout(self, tmp_path: Path) -> None:
        client = _serve_client(tmp_path, MagicMock())
        assert client.get("/storage/jobs/proj/55?timeout=0", headers=_AUTH).status_code == 422

    def test_job_detail_permission_denied(self, tmp_path: Path) -> None:
        from keboola_agent_cli.models import PermissionPolicy

        store = ConfigStore(config_dir=tmp_path)
        config = store.load()
        config.permissions = PermissionPolicy(mode="allow", deny=["storage.job-detail"])
        store.save(config)
        storage = MagicMock()
        res = _serve_client(tmp_path, storage).get("/storage/jobs/proj/55", headers=_AUTH)
        assert res.status_code == 403
        assert res.json()["error"]["code"] == "PERMISSION_DENIED"
        storage.storage_job_detail.assert_not_called()

    def test_upload_route_streams_file_and_forwards_wait(self, tmp_path: Path) -> None:
        storage = MagicMock()
        seen: dict[str, Any] = {}

        def _upload(**kwargs: Any) -> dict[str, Any]:
            seen.update(kwargs)
            seen["content"] = Path(kwargs["file_path"]).read_bytes()
            return {"job_id": 55, "job_status": "waiting"}

        storage.upload_table.side_effect = _upload
        client = _serve_client(tmp_path, storage)
        res = client.post(
            "/storage/tables/proj/upload",
            headers=_AUTH,
            data={
                "table_id": "in.c-b.users",
                "wait": "false",
                "timeout": "90",
                "delimiter": ";",
                "auto_create": "false",
            },
            files={"file": ("d.csv", b"id;name\n1;x\n", "text/csv")},
        )
        assert res.status_code == 200, res.text
        assert seen["content"] == b"id;name\n1;x\n"
        assert (seen["wait"], seen["timeout"], seen["delimiter"]) == (False, 90.0, ";")
        assert seen["auto_create"] is False
        # The temp copy is removed once the call returns.
        assert not Path(seen["file_path"]).exists()

    def test_load_file_route_forwards_wait(self, tmp_path: Path) -> None:
        storage = MagicMock()
        storage.load_file_to_table.return_value = {"job_id": 56}
        client = _serve_client(tmp_path, storage)
        res = client.post(
            "/storage/files/proj/load-to-table",
            headers=_AUTH,
            json={"file_id": 1, "table_id": "in.c-b.t", "wait": False, "timeout": 5},
        )
        assert res.status_code == 200, res.text
        kwargs = storage.load_file_to_table.call_args.kwargs
        assert kwargs["wait"] is False and kwargs["timeout"] == 5.0


# ---------------------------------------------------------------------------
# Review follow-ups: recovery hints and fail-fast no-wait errors
# ---------------------------------------------------------------------------


def _upload_with_error(tmp_path: Path, error: KeboolaApiError, **kwargs: Any) -> KeboolaApiError:
    """Run service.upload_table against a client that raises ``error``."""
    csv_file = tmp_path / "d.csv"
    csv_file.write_text("id\n1\n")
    client = MagicMock()
    client.upload_table.side_effect = error
    service = _make_service(_make_store(tmp_path), client)
    with pytest.raises(KeboolaApiError) as exc_info:
        service.upload_table(
            alias="test",
            table_id="in.c-b.users",
            file_path=str(csv_file),
            auto_create=False,
            **kwargs,
        )
    return exc_info.value


class TestRecoveryHintKeepsImportOptions:
    """The load-file hint must re-run the SAME import, not a full production load."""

    def test_incremental_branch_delimiter_enclosure_are_rendered(self, tmp_path: Path) -> None:
        exc = _upload_with_error(
            tmp_path,
            KeboolaApiError(
                "Bad request",
                status_code=400,
                error_code=ErrorCode.VALIDATION_ERROR,
                details={"file_id": 100},
            ),
            incremental=True,
            delimiter=";",
            enclosure="",
            branch_id=123,
        )
        assert (
            "kbagent storage load-file --project test --file-id 100 --table-id in.c-b.users "
            "--incremental --delimiter ';' --enclosure '' --branch 123"
        ) in exc.message

    def test_defaults_render_no_extra_flags(self, tmp_path: Path) -> None:
        exc = _upload_with_error(
            tmp_path,
            KeboolaApiError(
                "Bad request",
                status_code=400,
                error_code=ErrorCode.VALIDATION_ERROR,
                details={"file_id": 100},
            ),
        )
        assert exc.message.endswith(
            "kbagent storage load-file --project test --file-id 100 --table-id in.c-b.users"
        )

    def test_shell_metacharacters_are_quoted(self, tmp_path: Path) -> None:
        exc = _upload_with_error(
            tmp_path,
            KeboolaApiError(
                "Bad request",
                status_code=400,
                error_code=ErrorCode.VALIDATION_ERROR,
                details={"file_id": 100},
            ),
            enclosure="'",
        )
        assert "--enclosure ''\"'\"''" in exc.message


class TestAmbiguousEnqueueFailure:
    """A lost enqueue response may hide a running import: never advise re-import first."""

    def _upload(self, httpx_mock, tmp_path: Path) -> KeboolaApiError:
        csv_file = tmp_path / "d.csv"
        csv_file.write_text("id\n1\n")
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/files/prepare", method="POST", json={"id": 100}
        )
        with (
            KeboolaClient(stack_url=_BASE, token=_TOKEN) as client,
            patch.object(KeboolaClient, "_upload_to_cloud"),
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client.upload_table(table_id="in.c-b.users", file_path=str(csv_file))
        return exc_info.value

    def test_5xx_marks_import_may_be_running(self, httpx_mock, tmp_path: Path) -> None:
        httpx_mock.add_response(
            url=_IMPORT_URL, method="POST", json={"error": "Internal"}, status_code=503
        )
        exc = self._upload(httpx_mock, tmp_path)
        assert exc.details["import_may_be_running"] is True
        assert exc.details["file_id"] == 100
        assert "instead of uploading it again" not in exc.message
        assert "may already be running" in exc.message
        assert exc.retryable is False

    def test_read_timeout_marks_import_may_be_running(self, httpx_mock, tmp_path: Path) -> None:
        httpx_mock.add_exception(httpx.ReadTimeout("read timed out"), url=_IMPORT_URL)
        exc = self._upload(httpx_mock, tmp_path)
        assert exc.details["import_may_be_running"] is True
        assert exc.details["file_id"] == 100

    def test_raw_transport_error_marks_import_may_be_running(
        self, httpx_mock, tmp_path: Path
    ) -> None:
        httpx_mock.add_exception(httpx.RemoteProtocolError("server disconnected"), url=_IMPORT_URL)
        exc = self._upload(httpx_mock, tmp_path)
        assert exc.error_code == ErrorCode.CONNECTION_ERROR
        assert exc.details["import_may_be_running"] is True
        assert exc.details["file_id"] == 100

    def test_4xx_stays_definitive(self, httpx_mock, tmp_path: Path) -> None:
        httpx_mock.add_response(
            url=_IMPORT_URL, method="POST", json={"error": "bad delimiter"}, status_code=400
        )
        exc = self._upload(httpx_mock, tmp_path)
        assert "import_may_be_running" not in exc.details
        assert "instead of uploading it again" in exc.message

    def test_load_file_5xx_keeps_file_id_and_flag(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=_IMPORT_URL, method="POST", json={"error": "Internal"}, status_code=502
        )
        with (
            KeboolaClient(stack_url=_BASE, token=_TOKEN) as client,
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client.import_table_async(table_id="in.c-b.users", file_id=9, wait=False)
        assert exc_info.value.details == {"file_id": 9, "import_may_be_running": True}

    def test_service_hint_says_check_first_not_load_file(self, tmp_path: Path) -> None:
        exc = _upload_with_error(
            tmp_path,
            KeboolaApiError(
                "API error 503",
                status_code=503,
                error_code=ErrorCode.API_ERROR,
                details={"file_id": 100, "import_may_be_running": True},
            ),
            incremental=True,
            branch_id=123,
        )
        assert "Import it with" not in exc.message
        assert "may already be running" in exc.message
        check = "kbagent storage table-detail --project test --table-id in.c-b.users --branch 123"
        reimport = "kbagent storage load-file --project test --file-id 100"
        assert check in exc.message
        # The re-import command, if named at all, comes only after the check.
        assert exc.message.index(check) < exc.message.find(reimport) or reimport not in exc.message
        assert exc.details["import_may_be_running"] is True


class TestNoWaitTerminalJob:
    _FAILED: ClassVar[dict[str, Any]] = {
        "id": 55,
        "status": "error",
        "error": {"message": "Invalid CSV header", "code": "storage.import"},
    }

    def test_upload_no_wait_terminal_error_raises(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "d.csv"
        csv_file.write_text("id\n1\n")
        client = MagicMock()
        client.upload_table.return_value = TableUploadOutcome(file_id=100, job=self._FAILED)
        service = _make_service(_make_store(tmp_path), client)
        with pytest.raises(KeboolaApiError) as exc_info:
            service.upload_table(
                alias="test",
                table_id="in.c-b.users",
                file_path=str(csv_file),
                auto_create=False,
                wait=False,
            )
        exc = exc_info.value
        assert exc.error_code == ErrorCode.STORAGE_JOB_FAILED
        assert exc.message == "Invalid CSV header"
        assert exc.details == {"job_id": 55, "file_id": 100}

    def test_load_file_no_wait_terminal_error_raises(self, tmp_path: Path) -> None:
        client = MagicMock()
        client.import_table_async.return_value = self._FAILED
        service = _make_service(_make_store(tmp_path), client)
        with pytest.raises(KeboolaApiError) as exc_info:
            service.load_file_to_table(alias="test", file_id=7, table_id="in.c-b.t", wait=False)
        exc = exc_info.value
        assert exc.error_code == ErrorCode.STORAGE_JOB_FAILED
        assert exc.message == "Invalid CSV header"
        assert exc.details == {"job_id": 55, "file_id": 7}

    def test_load_file_no_wait_cli_exits_non_zero(self, tmp_path: Path) -> None:
        client = MagicMock()
        client.import_table_async.return_value = self._FAILED
        store = _make_store(tmp_path)
        svc = _make_service(store, client)
        result = _invoke(
            store,
            svc,
            [
                "--json",
                "storage",
                "load-file",
                "--project",
                "test",
                "--file-id",
                "7",
                "--table-id",
                "in.c-b.t",
                "--no-wait",
            ],
        )
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload["error"]["code"] == "STORAGE_JOB_FAILED"

    def test_load_file_no_wait_terminal_success_reports_rows(self, tmp_path: Path) -> None:
        client = MagicMock()
        client.import_table_async.return_value = {
            "id": 56,
            "status": "success",
            "results": {"importedRowsCount": 3, "warnings": ["w"]},
        }
        service = _make_service(_make_store(tmp_path), client)
        result = service.load_file_to_table(
            alias="test", file_id=7, table_id="in.c-b.t", wait=False
        )
        assert (result["job_status"], result["imported_rows"], result["warnings"]) == (
            "success",
            3,
            ["w"],
        )
