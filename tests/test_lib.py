"""Tests for the public in-process library facade (keboola_agent_cli.lib).

The facade wraps a single KeboolaClient, so every test patches
``keboola_agent_cli.lib.KeboolaClient`` with a MagicMock and asserts the facade
translates between the high-level shapes (list[dict] rows, bytes, FileEntry) and
the low-level client calls. No network.
"""

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from keboola_agent_cli import (
    Client,
    ConfigDetailResult,
    FileEntry,
    Files,
    JobResult,
    QueryResult,
    UploadTableResult,
)
from keboola_agent_cli.errors import KeboolaApiError

# Canonical fake token (projectId-tokenId-secret); never a realistic secret.
FAKE_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
STACK_URL = "https://connection.keboola.com"


@pytest.fixture
def mock_kc() -> MagicMock:
    return MagicMock()


@pytest.fixture
def client(mock_kc: MagicMock) -> Client:
    """A Client whose underlying KeboolaClient is the shared mock."""
    with patch("keboola_agent_cli.lib.KeboolaClient", return_value=mock_kc):
        return Client(url=STACK_URL, token=FAKE_TOKEN)


def _make_client(mock_kc: MagicMock, *, branch_id: int | None = None) -> Client:
    with patch("keboola_agent_cli.lib.KeboolaClient", return_value=mock_kc):
        return Client(url=STACK_URL, token=FAKE_TOKEN, branch_id=branch_id)


class TestConstruction:
    def test_requires_url(self) -> None:
        with pytest.raises(ValueError, match="url is required"):
            Client(url="", token=FAKE_TOKEN)

    def test_requires_token(self) -> None:
        with pytest.raises(ValueError, match="token is required"):
            Client(url=STACK_URL, token="")

    def test_raw_exposes_underlying_client(self, client: Client, mock_kc: MagicMock) -> None:
        assert client.raw is mock_kc

    def test_files_namespace_present(self, client: Client) -> None:
        assert isinstance(client.files, Files)

    def test_context_manager_closes(self, mock_kc: MagicMock) -> None:
        with _make_client(mock_kc) as c:
            assert c is not None
        mock_kc.close.assert_called_once()


class TestQuery:
    @staticmethod
    def _wire_single_select(mock_kc: MagicMock, *, num_rows: int = 2) -> None:
        mock_kc.list_dev_branches.return_value = [{"id": 123, "isDefault": True}]
        mock_kc.submit_query.return_value = {"queryJobId": "qj1"}
        mock_kc.wait_for_query_job.return_value = {
            "statements": [{"id": "s1", "status": "completed", "numberOfRows": num_rows}]
        }
        mock_kc.get_query_results.return_value = {
            "columns": [{"name": "id"}, {"name": "name"}],
            # The Query Service /results endpoint returns Snowflake scalars as
            # JSON strings (see Client.query docstring); the facade is
            # transparent, so the mock reflects the real string contract, not
            # coerced ints.
            "data": [["1", "alice"], ["2", "bob"]],
            "numberOfRows": num_rows,
        }

    def test_maps_columns_and_rows_to_dicts(self, client: Client, mock_kc: MagicMock) -> None:
        self._wire_single_select(mock_kc)
        rows = client.query(456, "SELECT id, name FROM t")
        assert rows == [{"id": "1", "name": "alice"}, {"id": "2", "name": "bob"}]

    def test_submits_with_resolved_default_branch(self, client: Client, mock_kc: MagicMock) -> None:
        self._wire_single_select(mock_kc)
        client.query(456, "SELECT 1")
        mock_kc.submit_query.assert_called_once_with(
            branch_id=123, workspace_id=456, statements=["SELECT 1"], transactional=False
        )

    def test_explicit_branch_skips_resolution(self, mock_kc: MagicMock) -> None:
        c = _make_client(mock_kc, branch_id=999)
        self._wire_single_select(mock_kc)
        c.query(456, "SELECT 1")
        mock_kc.list_dev_branches.assert_not_called()
        assert mock_kc.submit_query.call_args.kwargs["branch_id"] == 999

    def test_no_default_branch_raises(self, client: Client, mock_kc: MagicMock) -> None:
        mock_kc.list_dev_branches.return_value = [{"id": 5, "isDefault": False}]
        with pytest.raises(KeboolaApiError, match="No default branch"):
            client.query(1, "SELECT 1")

    def test_statement_without_result_set_returns_empty(
        self, client: Client, mock_kc: MagicMock
    ) -> None:
        mock_kc.list_dev_branches.return_value = [{"id": 1, "isDefault": True}]
        mock_kc.submit_query.return_value = {"queryJobId": "qj"}
        mock_kc.wait_for_query_job.return_value = {
            "statements": [{"id": "s1", "status": "completed", "numberOfRows": 0}]
        }
        assert client.query(1, "CREATE TABLE t (id INT)") == []
        mock_kc.get_query_results.assert_not_called()

    def test_multi_statement_returns_last_result_set(
        self, client: Client, mock_kc: MagicMock
    ) -> None:
        mock_kc.list_dev_branches.return_value = [{"id": 1, "isDefault": True}]
        mock_kc.submit_query.return_value = {"queryJobId": "qj"}
        mock_kc.wait_for_query_job.return_value = {
            "statements": [
                {"id": "s1", "status": "completed", "numberOfRows": 0},
                {"id": "s2", "status": "completed", "numberOfRows": 1},
            ]
        }
        mock_kc.get_query_results.return_value = {
            "columns": [{"name": "n"}],
            "data": [["7"]],  # warehouse-serialized string, per the documented contract
            "numberOfRows": 1,
        }
        rows = client.query(1, "USE WAREHOUSE x; SELECT 7 AS n")
        assert rows == [{"n": "7"}]
        # Only the result-producing statement triggers a results fetch.
        mock_kc.get_query_results.assert_called_once()
        assert mock_kc.get_query_results.call_args.args[1] == "s2"

    def test_truncation_logs_warning(
        self, client: Client, mock_kc: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_kc.list_dev_branches.return_value = [{"id": 1, "isDefault": True}]
        mock_kc.submit_query.return_value = {"queryJobId": "qj"}
        mock_kc.wait_for_query_job.return_value = {
            "statements": [{"id": "s1", "status": "completed", "numberOfRows": 100}]
        }
        mock_kc.get_query_results.return_value = {
            "columns": [{"name": "id"}],
            "data": [[1], [2]],
            "numberOfRows": 100,  # warehouse has more than we keep
        }
        with caplog.at_level(logging.WARNING, logger="keboola_agent_cli.lib"):
            rows = client.query(1, "SELECT id FROM big", limit=2)
        assert len(rows) == 2
        assert "truncated" in caplog.text


class TestFilesList:
    def test_returns_uniform_file_entries(self, client: Client, mock_kc: MagicMock) -> None:
        mock_kc.list_files.return_value = [
            {"id": 1, "name": "a.csv", "tags": ["x"], "created": "2026-01-01", "sizeBytes": 9},
            {"id": 2, "name": "b.csv", "tags": [], "created": "2026-01-02", "isPermanent": True},
        ]
        entries = client.files.list(tags=["x"])
        assert all(isinstance(e, FileEntry) for e in entries)
        assert entries[0].id == 1 and entries[0].tags == ["x"]
        assert entries[1].is_permanent is True
        mock_kc.list_files.assert_called_once_with(
            limit=100, offset=0, tags=["x"], since_id=None, query=None, branch_id=None
        )

    def test_branch_scoped_list(self, mock_kc: MagicMock) -> None:
        c = _make_client(mock_kc, branch_id=42)
        mock_kc.list_files.return_value = []
        c.files.list()
        assert mock_kc.list_files.call_args.kwargs["branch_id"] == 42


class TestFilesUpload:
    def test_upload_from_path(self, client: Client, mock_kc: MagicMock, tmp_path: Path) -> None:
        p = tmp_path / "data.csv"
        p.write_text("a,b\n1,2\n")
        mock_kc.upload_file.return_value = {"id": 5, "name": "data.csv", "tags": ["t"]}
        entry = client.files.upload(str(p), tags=["t"], permanent=True)
        assert entry.id == 5
        call = mock_kc.upload_file.call_args.kwargs
        assert call["file_path"] == str(p)
        assert call["name"] is None and call["is_permanent"] is True and call["tags"] == ["t"]

    def test_upload_from_bytes_stages_temp_file(self, client: Client, mock_kc: MagicMock) -> None:
        captured: dict[str, object] = {}

        def fake_upload(*, file_path: str, name: str, tags, is_permanent, branch_id):
            captured["path"] = file_path
            captured["content"] = Path(file_path).read_bytes()
            return {"id": 9, "name": name, "tags": tags or []}

        mock_kc.upload_file.side_effect = fake_upload
        entry = client.files.upload(b"hello bytes", name="greeting.txt")
        assert entry.id == 9
        assert captured["content"] == b"hello bytes"
        # temp file is cleaned up after the upload returns
        assert not Path(str(captured["path"])).exists()

    def test_upload_bytes_without_name_raises(self, client: Client) -> None:
        with pytest.raises(ValueError, match="name is required"):
            client.files.upload(b"x")


class TestFilesReadBytes:
    def test_non_sliced_download(self, client: Client, mock_kc: MagicMock) -> None:
        mock_kc.get_file_info.return_value = {"isSliced": False, "url": "https://signed/url"}

        def fake_download(url: str, output_path: str) -> int:
            Path(output_path).write_bytes(b"file-content")
            return 12

        mock_kc.download_file.side_effect = fake_download
        assert client.files.read_bytes(42) == b"file-content"
        mock_kc.get_file_info.assert_called_once_with(42, branch_id=None)

    def test_sliced_download(self, client: Client, mock_kc: MagicMock) -> None:
        mock_kc.get_file_info.return_value = {"isSliced": True}

        def fake_sliced(info: dict, output_path: str) -> int:
            Path(output_path).write_bytes(b"sliced-content")
            return 14

        mock_kc.download_sliced_file.side_effect = fake_sliced
        assert client.files.read_bytes(7) == b"sliced-content"
        mock_kc.download_file.assert_not_called()

    def test_missing_url_raises(self, client: Client, mock_kc: MagicMock) -> None:
        mock_kc.get_file_info.return_value = {"isSliced": False}  # no url
        with pytest.raises(KeboolaApiError, match="no download URL"):
            client.files.read_bytes(9)


class TestFilesDelete:
    def test_delete_forwards(self, client: Client, mock_kc: MagicMock) -> None:
        client.files.delete(11)
        mock_kc.delete_file.assert_called_once_with(11, branch_id=None)


class TestQueryResult:
    def test_returns_typed_shape_with_columns_and_truncation(
        self, client: Client, mock_kc: MagicMock
    ) -> None:
        mock_kc.list_dev_branches.return_value = [{"id": 1, "isDefault": True}]
        mock_kc.submit_query.return_value = {"queryJobId": "qj"}
        mock_kc.wait_for_query_job.return_value = {
            "statements": [{"id": "s1", "status": "completed", "numberOfRows": 100}]
        }
        mock_kc.get_query_results.return_value = {
            "columns": [{"name": "id"}, {"name": "name"}],
            "data": [["1", "alice"], ["2", "bob"]],
            "numberOfRows": 100,
        }
        result = client.query_result(456, "SELECT id, name FROM big", limit=2)
        assert isinstance(result, QueryResult)
        assert result.columns == ["id", "name"]
        assert result.rows == [{"id": "1", "name": "alice"}, {"id": "2", "name": "bob"}]
        assert result.row_count == 2
        assert result.truncated is True
        assert result.total_rows == 100

    def test_query_and_query_result_share_rows(self, client: Client, mock_kc: MagicMock) -> None:
        mock_kc.list_dev_branches.return_value = [{"id": 1, "isDefault": True}]
        mock_kc.submit_query.return_value = {"queryJobId": "qj"}
        mock_kc.wait_for_query_job.return_value = {
            "statements": [{"id": "s1", "status": "completed", "numberOfRows": 1}]
        }
        mock_kc.get_query_results.return_value = {
            "columns": [{"name": "n"}],
            "data": [["7"]],
            "numberOfRows": 1,
        }
        assert client.query_result(1, "SELECT 7 AS n").rows == client.query(1, "SELECT 7 AS n")


class TestRunJob:
    def test_create_only_returns_job_result(self, client: Client, mock_kc: MagicMock) -> None:
        mock_kc.create_job.return_value = {
            "id": "job-1",
            "status": "processing",
            "component": "keboola.ex-db-snowflake",
            "configId": "cfg-9",
        }
        result = client.run_job("keboola.ex-db-snowflake", "cfg-9")
        assert isinstance(result, JobResult)
        assert result.id == "job-1" and result.component_id == "keboola.ex-db-snowflake"
        assert result.config_id == "cfg-9"
        mock_kc.wait_for_queue_job.assert_not_called()
        # production (no branch) by default, no variable resolution in the facade
        call = mock_kc.create_job.call_args.kwargs
        assert call["branch_id"] is None and call["variable_values_id"] is None

    def test_wait_polls_for_terminal_state(self, client: Client, mock_kc: MagicMock) -> None:
        mock_kc.create_job.return_value = {"id": "job-2", "status": "processing"}
        mock_kc.wait_for_queue_job.return_value = {
            "id": "job-2",
            "status": "success",
            "isFinished": True,
        }
        result = client.run_job("c", "cfg", wait=True, timeout=30, poll_strategy="fixed")
        assert result.succeeded and result.is_finished
        mock_kc.wait_for_queue_job.assert_called_once_with(
            "job-2", max_wait=30, poll_strategy="fixed"
        )

    def test_branch_and_row_ids_forwarded(self, mock_kc: MagicMock) -> None:
        c = _make_client(mock_kc, branch_id=77)
        mock_kc.create_job.return_value = {"id": "j", "status": "created"}
        c.run_job("c", "cfg", config_row_ids=["r1"], variable_values_id="vv1", mode="debug")
        call = mock_kc.create_job.call_args.kwargs
        assert call["branch_id"] == 77
        assert call["config_row_ids"] == ["r1"]
        assert call["variable_values_id"] == "vv1"
        assert call["mode"] == "debug"

    def test_explicit_branch_overrides_client_branch(self, mock_kc: MagicMock) -> None:
        c = _make_client(mock_kc, branch_id=77)
        mock_kc.create_job.return_value = {"id": "j", "status": "created"}
        c.run_job("c", "cfg", branch_id=88)
        assert mock_kc.create_job.call_args.kwargs["branch_id"] == 88


class TestConfigDetail:
    def test_returns_typed_detail(self, client: Client, mock_kc: MagicMock) -> None:
        mock_kc.get_config_detail.return_value = {
            "id": "cfg-1",
            "name": "My Config",
            "currentVersion": 4,
            "configuration": {"parameters": {"x": 1}},
            "rows": [],
        }
        detail = client.config_detail("keboola.ex-http", "cfg-1")
        assert isinstance(detail, ConfigDetailResult)
        assert detail.id == "cfg-1" and detail.version == 4
        assert detail.component_id == "keboola.ex-http"  # injected from the arg
        assert detail.configuration == {"parameters": {"x": 1}}
        mock_kc.get_config_detail.assert_called_once_with(
            "keboola.ex-http", "cfg-1", branch_id=None
        )

    def test_branch_scoped(self, mock_kc: MagicMock) -> None:
        c = _make_client(mock_kc, branch_id=55)
        mock_kc.get_config_detail.return_value = {"id": "cfg-2", "name": "n"}
        detail = c.config_detail("keboola.ex-http", "cfg-2")
        assert mock_kc.get_config_detail.call_args.kwargs["branch_id"] == 55
        assert detail.branch_id == 55


class TestUploadTable:
    def test_computes_size_and_maps_imported_rows(
        self, client: Client, mock_kc: MagicMock, tmp_path: Path
    ) -> None:
        csv = tmp_path / "data.csv"
        csv.write_text("a,b\n1,2\n3,4\n")
        mock_kc.upload_table.return_value = {"importedRowsCount": 2, "warnings": []}
        result = client.upload_table("in.c-x.t", csv, incremental=True)
        assert isinstance(result, UploadTableResult)
        assert result.table_id == "in.c-x.t"
        assert result.incremental is True
        assert result.imported_rows == 2
        assert result.file_size_bytes == csv.stat().st_size
        # facade never auto-creates
        assert result.auto_created_bucket is False and result.auto_created_table is False
        call = mock_kc.upload_table.call_args.kwargs
        assert call["table_id"] == "in.c-x.t" and call["incremental"] is True
        assert call["branch_id"] is None

    def test_branch_scoped_upload(self, mock_kc: MagicMock, tmp_path: Path) -> None:
        c = _make_client(mock_kc, branch_id=33)
        csv = tmp_path / "d.csv"
        csv.write_text("a\n1\n")
        mock_kc.upload_table.return_value = {"importedRowsCount": 1}
        c.upload_table("in.c-x.t", csv)
        assert mock_kc.upload_table.call_args.kwargs["branch_id"] == 33


class TestModuleLayout:
    def test_pagination_helper_relocated_to_client(self) -> None:
        """_collect_inline_results lives in client.py; workspace_service re-exports it."""
        import keboola_agent_cli.services.workspace_service as ws
        from keboola_agent_cli.client import InlineQueryResult, _collect_inline_results

        assert ws._collect_inline_results is _collect_inline_results
        assert InlineQueryResult.__module__ == "keboola_agent_cli.client"

    def test_public_surface(self) -> None:
        import keboola_agent_cli as pkg

        assert set(pkg.__all__) >= {
            "Client",
            "Files",
            "FileEntry",
            "JobResult",
            "QueryResult",
            "UploadTableResult",
            "SyncPushResult",
            "ConfigDetailResult",
        }
