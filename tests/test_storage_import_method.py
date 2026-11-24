"""``--import-method`` on ``storage upload-table`` / ``load-file`` (DMD-2856).

Storage's ``import-async`` takes ``importMethod`` (``auto`` |
``single_statement`` | ``multi_statement``). These tests pin that:

- client: ``auto`` sends no ``importMethod`` (the platform default), any other
  value is sent as-is, an unknown value fails before any request;
- pre-flight: an explicit method reads the bucket's backend (or the project
  default for a bucket upload-table would create) and is refused before any
  write; ``auto`` reads nothing; the SDK facade does the same;
- service: the 400 ``storage.tables.importMethodNotSupported`` (explicit
  method on a non-Snowflake table) becomes ``INVALID_ARGUMENT`` naming the
  option, keeps its details, and its ``load-file`` recovery drops the method;
  other failures repeat the method in the recovery command;
- CLI: the option reaches the service, an unknown value is a usage error;
- serve: the upload form field and the load-to-table body field.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import AppConfig, ImportMethod, ProjectConfig
from keboola_agent_cli.services.storage_service import StorageService

runner = CliRunner()

_BASE = "https://connection.keboola.com"
_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
_TABLE = "in.c-b.users"
_IMPORT_URL = f"{_BASE}/v2/storage/tables/{_TABLE}/import-async"
_BUCKET_URL = f"{_BASE}/v2/storage/buckets/in.c-b"
_NOT_SUPPORTED_BODY = {
    "error": (
        'Import method "single_statement" is supported only on the Snowflake backend; '
        'table "in.c-b.users" is on the "bigquery" backend. Omit "importMethod" or use "auto".'
    ),
    "code": "storage.tables.importMethodNotSupported",
    "params": {"tableId": _TABLE, "backend": "bigquery", "importMethod": "single_statement"},
}


def _make_store(tmp_path: Path) -> ConfigStore:
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    store = ConfigStore(config_dir=config_dir)
    store.save(AppConfig(projects={"test": ProjectConfig(stack_url=_BASE, token=_TOKEN)}))
    return store


def _real_client_service(tmp_path: Path) -> StorageService:
    """A service over a real KeboolaClient, so the HTTP error parsing is exercised."""
    return StorageService(
        config_store=_make_store(tmp_path),
        client_factory=lambda url, token: KeboolaClient(stack_url=url, token=token),
    )


def _import_form(httpx_mock) -> dict[str, list[str]]:
    posts = [r for r in httpx_mock.get_requests() if str(r.url) == _IMPORT_URL]
    assert len(posts) == 1
    return parse_qs(posts[0].content.decode())


def _csv(tmp_path: Path) -> Path:
    csv_file = tmp_path / "d.csv"
    csv_file.write_text("id\n1\n")
    return csv_file


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class TestClientSendsImportMethod:
    def _queue(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=_IMPORT_URL, method="POST", json={"id": 42, "status": "waiting"}, status_code=202
        )

    def test_default_sends_no_import_method(self, httpx_mock) -> None:
        self._queue(httpx_mock)
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            client.import_table_async(table_id=_TABLE, file_id=9, wait=False)
        assert "importMethod" not in _import_form(httpx_mock)

    def test_explicit_auto_sends_no_import_method(self, httpx_mock) -> None:
        self._queue(httpx_mock)
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            client.import_table_async(table_id=_TABLE, file_id=9, wait=False, import_method="auto")
        assert "importMethod" not in _import_form(httpx_mock)

    @pytest.mark.parametrize("method", ["single_statement", "multi_statement"])
    def test_explicit_method_is_sent(self, httpx_mock, method: str) -> None:
        self._queue(httpx_mock)
        with KeboolaClient(stack_url=_BASE, token=_TOKEN) as client:
            client.import_table_async(
                table_id=_TABLE, file_id=9, wait=False, import_method=ImportMethod(method)
            )
        assert _import_form(httpx_mock)["importMethod"] == [method]

    def test_unknown_method_fails_before_any_request(self, httpx_mock) -> None:
        with (
            KeboolaClient(stack_url=_BASE, token=_TOKEN) as client,
            pytest.raises(ValueError, match="merge"),
        ):
            client.import_table_async(table_id=_TABLE, file_id=9, wait=False, import_method="merge")
        assert httpx_mock.get_requests() == []

    def test_raw_upload_table_forwards_the_method(self, httpx_mock, tmp_path: Path) -> None:
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/files/prepare", method="POST", json={"id": 100}
        )
        self._queue(httpx_mock)
        with (
            KeboolaClient(stack_url=_BASE, token=_TOKEN) as client,
            patch.object(KeboolaClient, "_upload_to_cloud"),
        ):
            client.upload_table(
                table_id=_TABLE,
                file_path=str(_csv(tmp_path)),
                wait=False,
                import_method="multi_statement",
            )
        assert _import_form(httpx_mock)["importMethod"] == ["multi_statement"]


# ---------------------------------------------------------------------------
# Service: the non-Snowflake 400 and the recovery hint
# ---------------------------------------------------------------------------


class TestNonSnowflakeRefusal:
    """The Storage 400, reached when the pre-flight cannot read a backend."""

    def test_upload_refusal_is_invalid_argument_and_drops_the_method(
        self, httpx_mock, tmp_path: Path
    ) -> None:
        httpx_mock.add_response(url=_BUCKET_URL, method="GET", json={"id": "in.c-b"})
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/files/prepare", method="POST", json={"id": 100}
        )
        httpx_mock.add_response(
            url=_IMPORT_URL, method="POST", json=_NOT_SUPPORTED_BODY, status_code=400
        )
        service = _real_client_service(tmp_path)
        with (
            patch.object(KeboolaClient, "_upload_to_cloud"),
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            service.upload_table(
                alias="test",
                table_id=_TABLE,
                file_path=str(_csv(tmp_path)),
                incremental=True,
                auto_create=False,
                import_method=ImportMethod.single_statement,
            )
        exc = exc_info.value
        assert exc.error_code == ErrorCode.INVALID_ARGUMENT
        assert exc.retryable is False
        assert exc.status_code == 400
        assert exc.details["api_error_code"] == "storage.tables.importMethodNotSupported"
        assert exc.details["file_id"] == 100
        assert "Import method 'single_statement' works only on Snowflake tables" in exc.message
        assert "on the bigquery backend" in exc.message
        assert "--import-method" in exc.message
        # The server's own text is kept for the operator.
        assert 'Omit "importMethod"' in exc.message
        hint = exc.message.split("import it with: ", 1)[1]
        assert hint == (
            "kbagent storage load-file --project test --file-id 100 --table-id in.c-b.users "
            "--incremental --branch 0"
        )

    def test_refusal_names_the_auto_created_table(self, tmp_path: Path) -> None:
        client = MagicMock()
        client.get_bucket_detail.return_value = {"id": "in.c-b"}
        client.list_tables.return_value = []
        client.upload_table_with_outcome.side_effect = KeboolaApiError(
            "API error 400",
            status_code=400,
            error_code=ErrorCode.API_ERROR,
            details={
                "api_error_code": "storage.tables.importMethodNotSupported",
                "file_id": 100,
            },
        )
        service = StorageService(
            config_store=_make_store(tmp_path), client_factory=lambda url, token: client
        )
        with pytest.raises(KeboolaApiError) as exc_info:
            service.upload_table(
                alias="test",
                table_id=_TABLE,
                file_path=str(_csv(tmp_path)),
                import_method=ImportMethod.single_statement,
            )
        exc = exc_info.value
        assert exc.error_code == ErrorCode.INVALID_ARGUMENT
        client.create_table.assert_called_once()
        assert exc.message.endswith(
            "This command created the table in.c-b.users before the import was refused. "
            "Nothing was imported; the import without --import-method uses it."
        )

    def test_load_file_refusal_is_invalid_argument_without_hint(
        self, httpx_mock, tmp_path: Path
    ) -> None:
        httpx_mock.add_response(
            url=_IMPORT_URL, method="POST", json=_NOT_SUPPORTED_BODY, status_code=400
        )
        service = _real_client_service(tmp_path)
        with pytest.raises(KeboolaApiError) as exc_info:
            service.load_file_to_table(
                alias="test",
                file_id=7,
                table_id=_TABLE,
                import_method=ImportMethod.single_statement,
            )
        exc = exc_info.value
        assert exc.error_code == ErrorCode.INVALID_ARGUMENT
        assert "--import-method" in exc.message
        assert "load-file" not in exc.message
        assert _import_form(httpx_mock)["importMethod"] == ["single_statement"]

    def test_other_400_keeps_api_error(self, httpx_mock, tmp_path: Path) -> None:
        httpx_mock.add_response(
            url=_IMPORT_URL,
            method="POST",
            json={"error": "Invalid delimiter", "code": "storage.tables.validation"},
            status_code=400,
        )
        service = _real_client_service(tmp_path)
        with pytest.raises(KeboolaApiError) as exc_info:
            service.load_file_to_table(alias="test", file_id=7, table_id=_TABLE)
        assert exc_info.value.error_code == ErrorCode.API_ERROR

    def test_auto_keeps_the_storage_refusal_as_api_error(self, tmp_path: Path) -> None:
        # With auto kbagent sent no method, so "use auto" would be no advice.
        refusal = KeboolaApiError(
            "API error 400",
            status_code=400,
            error_code=ErrorCode.API_ERROR,
            details={"api_error_code": "storage.tables.importMethodNotSupported"},
        )
        client = MagicMock()
        client.import_table_async.side_effect = refusal
        service = StorageService(
            config_store=_make_store(tmp_path), client_factory=lambda url, token: client
        )
        with pytest.raises(KeboolaApiError) as exc_info:
            service.load_file_to_table(alias="test", file_id=7, table_id=_TABLE)
        assert exc_info.value.error_code == ErrorCode.API_ERROR
        assert exc_info.value.message == "API error 400"

    def test_other_failure_hint_repeats_the_method(self, tmp_path: Path) -> None:
        client = MagicMock()
        client.get_bucket_detail.return_value = {"id": "in.c-b", "backend": "snowflake"}
        client.upload_table_with_outcome.side_effect = KeboolaApiError(
            "Bad request",
            status_code=400,
            error_code=ErrorCode.VALIDATION_ERROR,
            details={"file_id": 100},
        )
        service = StorageService(
            config_store=_make_store(tmp_path), client_factory=lambda url, token: client
        )
        with pytest.raises(KeboolaApiError) as exc_info:
            service.upload_table(
                alias="test",
                table_id=_TABLE,
                file_path=str(_csv(tmp_path)),
                auto_create=False,
                import_method=ImportMethod.multi_statement,
            )
        assert exc_info.value.message.endswith(
            "kbagent storage load-file --project test --file-id 100 --table-id in.c-b.users "
            "--import-method multi_statement --branch 0"
        )
        assert client.upload_table_with_outcome.call_args.kwargs["import_method"] == (
            ImportMethod.multi_statement
        )

    def test_result_reports_the_method(self, tmp_path: Path) -> None:
        client = MagicMock()
        client.import_table_async.return_value = {"id": 5, "status": "waiting"}
        service = StorageService(
            config_store=_make_store(tmp_path), client_factory=lambda url, token: client
        )
        result = service.load_file_to_table(
            alias="test",
            file_id=7,
            table_id=_TABLE,
            wait=False,
            import_method=ImportMethod.single_statement,
        )
        assert result["import_method"] == "single_statement"
        default = service.load_file_to_table(alias="test", file_id=7, table_id=_TABLE, wait=False)
        assert default["import_method"] == "auto"


# ---------------------------------------------------------------------------
# Pre-flight: the backend is read before anything is written
# ---------------------------------------------------------------------------


def _upload(service: StorageService, tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    return service.upload_table(
        alias="test", table_id=_TABLE, file_path=str(_csv(tmp_path)), **kwargs
    )


class TestPreflight:
    def test_bigquery_bucket_is_refused_before_any_write(self, httpx_mock, tmp_path: Path) -> None:
        httpx_mock.add_response(
            url=_BUCKET_URL, method="GET", json={"id": "in.c-b", "backend": "bigquery"}
        )
        with pytest.raises(KeboolaApiError) as exc_info:
            _upload(_real_client_service(tmp_path), tmp_path, import_method="single_statement")
        exc = exc_info.value
        assert exc.error_code == ErrorCode.INVALID_ARGUMENT
        assert exc.status_code == 400
        assert exc.details == {
            "table_id": _TABLE,
            "backend": "bigquery",
            "import_method": "single_statement",
        }
        assert "table in.c-b.users is in bucket in.c-b on the bigquery backend" in exc.message
        assert "Nothing was created or uploaded." in exc.message
        # The bucket read is the only request: no create, no prepare, no import.
        assert [r.method for r in httpx_mock.get_requests()] == ["GET"]

    def test_missing_bucket_uses_the_project_default_backend(
        self, httpx_mock, tmp_path: Path
    ) -> None:
        httpx_mock.add_response(
            url=_BUCKET_URL, method="GET", json={"error": "Bucket not found"}, status_code=404
        )
        httpx_mock.add_response(
            url=f"{_BASE}/v2/storage/tokens/verify",
            method="GET",
            json={"id": "1", "owner": {"id": 1, "name": "p", "defaultBackend": "bigquery"}},
        )
        with pytest.raises(KeboolaApiError) as exc_info:
            _upload(_real_client_service(tmp_path), tmp_path, import_method="multi_statement")
        exc = exc_info.value
        assert exc.error_code == ErrorCode.INVALID_ARGUMENT
        assert "bucket in.c-b does not exist yet" in exc.message
        assert "project's default backend, bigquery" in exc.message
        assert all(r.method == "GET" for r in httpx_mock.get_requests())

    @pytest.mark.parametrize(
        ("status_code", "error_code"),
        [(404, ErrorCode.NOT_FOUND), (403, ErrorCode.ACCESS_DENIED)],
    )
    def test_unreadable_bucket_without_auto_create_is_left_to_storage(
        self, tmp_path: Path, status_code: int, error_code: ErrorCode
    ) -> None:
        client = MagicMock()
        client.get_bucket_detail.side_effect = KeboolaApiError(
            "bucket read failed", status_code=status_code, error_code=error_code
        )
        client.upload_table_with_outcome.return_value = MagicMock(
            file_id=100, job={"id": 5, "status": "waiting"}
        )
        service = StorageService(
            config_store=_make_store(tmp_path), client_factory=lambda url, token: client
        )
        result = _upload(
            service, tmp_path, auto_create=False, wait=False, import_method="single_statement"
        )
        assert result["import_method"] == "single_statement"
        client.verify_token.assert_not_called()
        kwargs = client.upload_table_with_outcome.call_args.kwargs
        assert kwargs["import_method"] is ImportMethod.single_statement

    def test_forbidden_bucket_with_auto_create_is_not_refused_by_the_check(
        self, tmp_path: Path
    ) -> None:
        client = MagicMock()
        forbidden = KeboolaApiError(
            "forbidden", status_code=403, error_code=ErrorCode.ACCESS_DENIED
        )
        client.get_bucket_detail.side_effect = forbidden
        service = StorageService(
            config_store=_make_store(tmp_path), client_factory=lambda url, token: client
        )
        # The check passes; the auto-create step's own bucket read then fails.
        with pytest.raises(KeboolaApiError) as exc_info:
            _upload(service, tmp_path, import_method="single_statement")
        assert exc_info.value.error_code == ErrorCode.ACCESS_DENIED
        assert client.get_bucket_detail.call_count == 2
        client.verify_token.assert_not_called()
        client.create_bucket.assert_not_called()

    def test_unreadable_default_backend_is_left_to_storage(self, tmp_path: Path) -> None:
        client = MagicMock()
        client.get_bucket_detail.side_effect = KeboolaApiError(
            "Bucket not found", status_code=404, error_code=ErrorCode.NOT_FOUND
        )
        client.verify_token.side_effect = KeboolaApiError(
            "verify failed", status_code=500, error_code=ErrorCode.API_ERROR
        )
        client.list_tables.return_value = []
        client.upload_table_with_outcome.return_value = MagicMock(
            file_id=100, job={"id": 5, "status": "waiting"}
        )
        service = StorageService(
            config_store=_make_store(tmp_path), client_factory=lambda url, token: client
        )
        result = _upload(service, tmp_path, wait=False, import_method="multi_statement")
        assert result["import_method"] == "multi_statement"
        client.create_bucket.assert_called_once()

    def test_snowflake_bucket_proceeds(self, tmp_path: Path) -> None:
        client = MagicMock()
        client.get_bucket_detail.return_value = {"id": "in.c-b", "backend": "snowflake"}
        client.list_tables.return_value = [{"name": "users"}]
        client.upload_table_with_outcome.return_value = MagicMock(
            file_id=100, job={"id": 5, "status": "waiting"}
        )
        service = StorageService(
            config_store=_make_store(tmp_path), client_factory=lambda url, token: client
        )
        result = _upload(service, tmp_path, wait=False, import_method="single_statement")
        assert result["import_method"] == "single_statement"
        kwargs = client.upload_table_with_outcome.call_args.kwargs
        assert kwargs["import_method"] is ImportMethod.single_statement

    def test_auto_reads_no_backend(self, tmp_path: Path) -> None:
        client = MagicMock()
        client.upload_table_with_outcome.return_value = MagicMock(
            file_id=100, job={"id": 5, "status": "waiting"}
        )
        service = StorageService(
            config_store=_make_store(tmp_path), client_factory=lambda url, token: client
        )
        result = _upload(service, tmp_path, auto_create=False, wait=False)
        assert result["import_method"] == "auto"
        client.get_bucket_detail.assert_not_called()
        client.verify_token.assert_not_called()

    def test_string_method_is_coerced_by_the_service(self, tmp_path: Path) -> None:
        client = MagicMock()
        client.import_table_async.return_value = {"id": 5, "status": "waiting"}
        service = StorageService(
            config_store=_make_store(tmp_path), client_factory=lambda url, token: client
        )
        result = service.load_file_to_table(
            alias="test",
            file_id=7,
            table_id=_TABLE,
            wait=False,
            import_method="multi_statement",
        )
        assert result["import_method"] == "multi_statement"
        with pytest.raises(ValueError, match="merge"):
            service.load_file_to_table(
                alias="test",
                file_id=7,
                table_id=_TABLE,
                import_method="merge",
            )
        assert client.import_table_async.call_count == 1

    def test_client_upload_checks_the_value_before_any_request(
        self, httpx_mock, tmp_path: Path
    ) -> None:
        with (
            KeboolaClient(stack_url=_BASE, token=_TOKEN) as client,
            pytest.raises(ValueError, match="merge"),
        ):
            client.upload_table(
                table_id=_TABLE, file_path=str(_csv(tmp_path)), import_method="merge"
            )
        assert httpx_mock.get_requests() == []


class TestSdkFacade:
    def _client(self, kc: MagicMock):
        from keboola_agent_cli.lib import Client

        with patch("keboola_agent_cli.lib.KeboolaClient", return_value=kc):
            return Client(url=_BASE, token=_TOKEN)

    def test_method_is_passed_and_reported(self, tmp_path: Path) -> None:
        kc = MagicMock()
        kc.get_bucket_detail.return_value = {"id": "in.c-b", "backend": "snowflake"}
        kc.upload_table_with_outcome.return_value = MagicMock(
            file_id=7, job={"id": 9, "status": "waiting"}
        )
        result = self._client(kc).upload_table(
            _TABLE, _csv(tmp_path), wait=False, import_method="single_statement"
        )
        assert result.import_method == "single_statement"
        kwargs = kc.upload_table_with_outcome.call_args.kwargs
        assert kwargs["import_method"] is ImportMethod.single_statement

    def test_non_snowflake_is_refused_before_the_upload(self, tmp_path: Path) -> None:
        kc = MagicMock()
        kc.get_bucket_detail.return_value = {"id": "in.c-b", "backend": "bigquery"}
        with pytest.raises(KeboolaApiError) as exc_info:
            self._client(kc).upload_table(_TABLE, _csv(tmp_path), import_method="multi_statement")
        assert exc_info.value.error_code == ErrorCode.INVALID_ARGUMENT
        kc.upload_table_with_outcome.assert_not_called()

    def test_unreadable_bucket_is_left_to_storage(self, tmp_path: Path) -> None:
        kc = MagicMock()
        kc.get_bucket_detail.side_effect = KeboolaApiError(
            "forbidden", status_code=403, error_code=ErrorCode.ACCESS_DENIED
        )
        kc.upload_table_with_outcome.return_value = MagicMock(
            file_id=7, job={"id": 9, "status": "waiting"}
        )
        result = self._client(kc).upload_table(
            _TABLE, _csv(tmp_path), wait=False, import_method="single_statement"
        )
        assert result.import_method == "single_statement"
        kc.verify_token.assert_not_called()
        kwargs = kc.upload_table_with_outcome.call_args.kwargs
        assert kwargs["import_method"] is ImportMethod.single_statement

    def test_default_reads_no_backend(self, tmp_path: Path) -> None:
        kc = MagicMock()
        kc.upload_table_with_outcome.return_value = MagicMock(
            file_id=7, job={"id": 9, "status": "waiting"}
        )
        result = self._client(kc).upload_table(_TABLE, _csv(tmp_path), wait=False)
        assert result.import_method == "auto"
        kc.get_bucket_detail.assert_not_called()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _invoke(store: ConfigStore, svc: MagicMock, args: list[str]) -> Any:
    with (
        patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
        patch("keboola_agent_cli.cli.StorageService", return_value=svc),
    ):
        return runner.invoke(app, args)


_LOAD_ARGS = ["storage", "load-file", "--project", "test", "--file-id", "7", "--table-id", _TABLE]


class TestCli:
    def test_upload_table_passes_the_method(self, tmp_path: Path) -> None:
        svc = MagicMock()
        svc.upload_table.return_value = {
            "project_alias": "test",
            "table_id": _TABLE,
            "incremental": False,
            "import_method": "single_statement",
            "file_size_bytes": 5,
            "file_id": 100,
            "job_id": 55,
            "job_status": "success",
            "imported_rows": 1,
            "warnings": [],
        }
        args = ["storage", "upload-table", "--project", "test", "--table-id", _TABLE]
        args += ["--file", str(_csv(tmp_path)), "--import-method", "single_statement"]
        result = _invoke(_make_store(tmp_path), svc, args)
        assert result.exit_code == 0, result.output
        assert svc.upload_table.call_args.kwargs["import_method"] == ImportMethod.single_statement
        assert "Import method: single_statement" in result.output

    def test_load_file_defaults_to_auto(self, tmp_path: Path) -> None:
        svc = MagicMock()
        svc.load_file_to_table.return_value = {
            "project_alias": "test",
            "file_id": 7,
            "table_id": _TABLE,
            "incremental": False,
            "import_method": "auto",
            "job_id": 5,
            "job_status": "success",
            "imported_rows": 1,
            "warnings": [],
        }
        result = _invoke(_make_store(tmp_path), svc, _LOAD_ARGS)
        assert result.exit_code == 0, result.output
        assert svc.load_file_to_table.call_args.kwargs["import_method"] == ImportMethod.auto
        assert "Import method" not in result.output

    def test_unknown_value_is_usage_error(self, tmp_path: Path) -> None:
        svc = MagicMock()
        result = _invoke(
            _make_store(tmp_path), svc, ["--json", *_LOAD_ARGS, "--import-method", "merge"]
        )
        assert result.exit_code == 2
        svc.load_file_to_table.assert_not_called()

    def test_refusal_exits_two_with_details(self, tmp_path: Path) -> None:
        svc = MagicMock()
        svc.load_file_to_table.side_effect = KeboolaApiError(
            "Import method 'single_statement' works only on Snowflake tables",
            status_code=400,
            error_code=ErrorCode.INVALID_ARGUMENT,
            retryable=False,
            details={"api_error_code": "storage.tables.importMethodNotSupported"},
        )
        result = _invoke(
            _make_store(tmp_path),
            svc,
            ["--json", *_LOAD_ARGS, "--import-method", "single_statement"],
        )
        assert result.exit_code == 2
        error = json.loads(result.output)["error"]
        assert error["code"] == "INVALID_ARGUMENT"
        assert error["details"]["api_error_code"] == "storage.tables.importMethodNotSupported"


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
    serve_app = create_app(config_dir=str(tmp_path), auth_token="test-token")
    serve_app.dependency_overrides[get_registry] = lambda: registry
    return fastapi_testclient.TestClient(serve_app)


class TestServe:
    def _upload(self, client: Any, **form: str) -> Any:
        return client.post(
            "/storage/tables/proj/upload",
            headers=_AUTH,
            data={"table_id": _TABLE, **form},
            files={"file": ("d.csv", b"id\n1\n", "text/csv")},
        )

    def test_upload_form_field_is_forwarded(self, tmp_path: Path) -> None:
        storage = MagicMock()
        storage.upload_table.return_value = {"job_id": 1}
        res = self._upload(_serve_client(tmp_path, storage), import_method="multi_statement")
        assert res.status_code == 200, res.text
        assert storage.upload_table.call_args.kwargs["import_method"] == (
            ImportMethod.multi_statement
        )

    def test_upload_without_field_is_auto(self, tmp_path: Path) -> None:
        storage = MagicMock()
        storage.upload_table.return_value = {"job_id": 1}
        res = self._upload(_serve_client(tmp_path, storage))
        assert res.status_code == 200, res.text
        assert storage.upload_table.call_args.kwargs["import_method"] == ImportMethod.auto

    def test_upload_unknown_value_is_rejected(self, tmp_path: Path) -> None:
        storage = MagicMock()
        res = self._upload(_serve_client(tmp_path, storage), import_method="merge")
        assert res.status_code == 422
        storage.upload_table.assert_not_called()

    def test_load_to_table_body_field(self, tmp_path: Path) -> None:
        storage = MagicMock()
        storage.load_file_to_table.return_value = {"job_id": 1}
        client = _serve_client(tmp_path, storage)
        res = client.post(
            "/storage/files/proj/load-to-table",
            headers=_AUTH,
            json={"file_id": 1, "table_id": _TABLE, "import_method": "single_statement"},
        )
        assert res.status_code == 200, res.text
        assert storage.load_file_to_table.call_args.kwargs["import_method"] == (
            ImportMethod.single_statement
        )
        res = client.post(
            "/storage/files/proj/load-to-table",
            headers=_AUTH,
            json={"file_id": 1, "table_id": _TABLE},
        )
        assert storage.load_file_to_table.call_args.kwargs["import_method"] == ImportMethod.auto

    def test_refusal_is_http_400(self, tmp_path: Path) -> None:
        storage = MagicMock()
        storage.load_file_to_table.side_effect = KeboolaApiError(
            "Import method 'single_statement' works only on Snowflake tables",
            status_code=400,
            error_code=ErrorCode.INVALID_ARGUMENT,
            retryable=False,
            details={"api_error_code": "storage.tables.importMethodNotSupported"},
        )
        res = _serve_client(tmp_path, storage).post(
            "/storage/files/proj/load-to-table",
            headers=_AUTH,
            json={"file_id": 1, "table_id": _TABLE, "import_method": "single_statement"},
        )
        assert res.status_code == 400
        assert res.json()["error"]["code"] == "INVALID_ARGUMENT"
