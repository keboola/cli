"""Tests for storage clustering-set: client, service, and CLI (DMD-3042)."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import CLUSTERING_UPDATE_NOT_ALLOWED_CODE, STORAGE_JOB_MAX_WAIT
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import AppConfig, ProjectConfig
from keboola_agent_cli.services.storage_service import StorageService

runner = CliRunner()

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
STACK = "https://connection.keboola.com"
TABLE = "in.c-b.t"
DEFINITION_URL = f"{STACK}/v2/storage/branch/default/tables/{TABLE}/definition"


def _make_store(tmp_path: Path) -> ConfigStore:
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    store = ConfigStore(config_dir=config_dir)
    store.save(AppConfig(projects={"test": ProjectConfig(stack_url=STACK, token=TEST_TOKEN)}))
    return store


def _make_service(store: ConfigStore, mock_client: MagicMock) -> StorageService:
    return StorageService(config_store=store, client_factory=lambda url, token: mock_client)


def _table(fields: list[str] | None, is_alias: bool = False) -> dict[str, Any]:
    definition: dict[str, Any] = {"columns": [{"name": "id"}]}
    if fields is not None:
        definition["clustering"] = {"fields": fields}
    return {"id": TABLE, "isAlias": is_alias, "definition": definition}


def _client(before: list[str] | None, after: list[str] | None = None, **kw: Any) -> MagicMock:
    client = MagicMock()
    client.get_table_detail.side_effect = [_table(before, **kw), _table(after)]
    client.update_table_definition.return_value = {"id": 555, "status": "success"}
    return client


def _sent_body(httpx_mock) -> dict[str, Any]:
    request = httpx_mock.get_requests(method="PUT")[0]
    return json.loads(request.content.decode("utf-8"))


def _not_allowed() -> KeboolaApiError:
    return KeboolaApiError(
        message="Clustering is supported only for BigQuery tables.",
        status_code=422,
        error_code=ErrorCode.API_ERROR,
        retryable=False,
        details={"api_error_code": CLUSTERING_UPDATE_NOT_ALLOWED_CODE},
    )


def _timeout() -> KeboolaApiError:
    return KeboolaApiError(
        message="Storage clustering change job 777 did not finish within 60s.",
        status_code=504,
        error_code=ErrorCode.STORAGE_JOB_TIMEOUT,
        retryable=False,
        details={"job_id": 777},
    )


# ---------------------------------------------------------------------------
# Client layer
# ---------------------------------------------------------------------------


class TestClusteringClient:
    def _put(self, httpx_mock, url: str = DEFINITION_URL, **kwargs: Any) -> dict[str, Any]:
        httpx_mock.add_response(
            url=url, method="PUT", json={"id": 555, "status": "success"}, status_code=202
        )
        client = KeboolaClient(stack_url=STACK, token=TEST_TOKEN)
        try:
            return client.update_table_definition(TABLE, **kwargs)
        finally:
            client.close()

    def test_put_sends_clustering_fields(self, httpx_mock) -> None:
        result = self._put(httpx_mock, clustering=["a", "b"], clustering_set=True)
        assert result["id"] == 555
        assert _sent_body(httpx_mock) == {"clustering": {"fields": ["a", "b"]}}

    def test_clear_sends_null(self, httpx_mock) -> None:
        self._put(httpx_mock, clustering=None, clustering_set=True)
        assert _sent_body(httpx_mock) == {"clustering": None}

    def test_dev_branch_path(self, httpx_mock) -> None:
        url = f"{STACK}/v2/storage/branch/123/tables/{TABLE}/definition"
        self._put(httpx_mock, url=url, clustering=["a"], clustering_set=True, branch_id=123)
        assert _sent_body(httpx_mock) == {"clustering": {"fields": ["a"]}}

    def test_description_body_has_no_clustering_key(self, httpx_mock) -> None:
        self._put(httpx_mock, description="text", description_set=True)
        assert _sent_body(httpx_mock) == {"description": "text"}

    def test_timeout_is_not_retryable_and_carries_job_id(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=DEFINITION_URL, method="PUT", json={"id": 777, "status": "waiting"}, status_code=202
        )
        client = KeboolaClient(stack_url=STACK, token=TEST_TOKEN)
        with pytest.raises(KeboolaApiError) as exc_info:
            # A budget in the past expires before the first poll (see test_storage_swap).
            client.update_table_definition(
                TABLE, clustering=["a"], clustering_set=True, max_wait=-1
            )
        client.close()
        exc = exc_info.value
        assert exc.error_code == ErrorCode.STORAGE_JOB_TIMEOUT
        assert exc.retryable is False
        assert exc.details["job_id"] == 777
        assert "keeps running server-side" in exc.message


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------


class TestClusteringService:
    def _run(self, tmp_path: Path, client: MagicMock, **kwargs: Any) -> dict[str, Any]:
        service = _make_service(_make_store(tmp_path), client)
        args: dict[str, Any] = {"fields": None, "clear": False, **kwargs}
        return service.set_table_clustering(alias="test", table_id=TABLE, **args)

    def test_set_returns_updated_job_id_and_post_read_clustering(self, tmp_path: Path) -> None:
        client = _client(["x"], ["a", "b"])
        result = self._run(tmp_path, client, fields=["a", "b"])
        assert result["status"] == "updated"
        assert result["job_id"] == 555
        assert result["clustering_before"] == ["x"]
        assert result["clustering"] == ["a", "b"]
        client.update_table_definition.assert_called_once_with(
            TABLE,
            clustering=["a", "b"],
            clustering_set=True,
            branch_id=None,
            max_wait=STORAGE_JOB_MAX_WAIT,
        )
        client.close.assert_called_once()

    def test_same_fields_is_unchanged_without_put(self, tmp_path: Path) -> None:
        client = _client(["a", "b"])
        result = self._run(tmp_path, client, fields=["a", "b"])
        assert result["status"] == "unchanged"
        assert result["job_id"] is None
        assert result["clustering"] == ["a", "b"]
        client.update_table_definition.assert_not_called()

    def test_clear_on_unclustered_table_is_unchanged(self, tmp_path: Path) -> None:
        client = _client(None)
        result = self._run(tmp_path, client, clear=True)
        assert result["status"] == "unchanged"
        assert result["clustering"] is None
        client.update_table_definition.assert_not_called()

    def test_reordered_fields_is_an_update(self, tmp_path: Path) -> None:
        client = _client(["a", "b"], ["b", "a"])
        result = self._run(tmp_path, client, fields=["b", "a"])
        assert result["status"] == "updated"
        client.update_table_definition.assert_called_once()

    def test_alias_is_never_unchanged_shortcut(self, tmp_path: Path) -> None:
        client = _client(None, is_alias=True)
        client.update_table_definition.side_effect = _not_allowed()
        with pytest.raises(KeboolaApiError):
            self._run(tmp_path, client, clear=True)
        client.update_table_definition.assert_called_once()

    @pytest.mark.parametrize(
        ("before", "is_alias", "would_change"),
        [(["x"], False, True), (["a"], False, False), (["a"], True, True)],
        ids=["changed", "equal", "alias"],
    )
    def test_dry_run_reads_once_and_never_puts(
        self, tmp_path: Path, before: list[str], is_alias: bool, would_change: bool
    ) -> None:
        client = _client(before, is_alias=is_alias)
        result = self._run(tmp_path, client, fields=["a"], dry_run=True)
        assert result == {
            "dry_run": True,
            "project_alias": "test",
            "table_id": TABLE,
            "branch_id": None,
            "clustering_before": before,
            "clustering": ["a"],
            "would_change": would_change,
        }
        client.get_table_detail.assert_called_once()
        client.update_table_definition.assert_not_called()

    def _assert_rejected(self, tmp_path: Path, needle: str, **kwargs: Any) -> None:
        client = _client(None)
        with pytest.raises(KeboolaApiError) as exc_info:
            self._run(tmp_path, client, **kwargs)
        assert exc_info.value.error_code == ErrorCode.INVALID_ARGUMENT
        assert exc_info.value.status_code == 400
        assert needle in exc_info.value.message
        client.get_table_detail.assert_not_called()

    def test_rejects_fields_and_clear_together(self, tmp_path: Path) -> None:
        self._assert_rejected(tmp_path, "mutually exclusive", fields=["a"], clear=True)

    def test_rejects_neither_fields_nor_clear(self, tmp_path: Path) -> None:
        self._assert_rejected(tmp_path, "--clustering-field COL")

    def test_rejects_more_than_max_fields(self, tmp_path: Path) -> None:
        self._assert_rejected(tmp_path, "at most 4", fields=["a", "b", "c", "d", "e"])

    def test_rejects_duplicate_field(self, tmp_path: Path) -> None:
        self._assert_rejected(tmp_path, "twice", fields=["a", "a"])

    def test_rejects_blank_field(self, tmp_path: Path) -> None:
        self._assert_rejected(tmp_path, "blank", fields=["a", " "])

    def test_clustering_422_becomes_invalid_argument_with_api_error_code(
        self, tmp_path: Path
    ) -> None:
        client = _client(None)
        client.update_table_definition.side_effect = _not_allowed()
        with pytest.raises(KeboolaApiError) as exc_info:
            self._run(tmp_path, client, fields=["missing"])
        exc = exc_info.value
        assert exc.error_code == ErrorCode.INVALID_ARGUMENT
        assert exc.status_code == 400
        assert exc.details["api_error_code"] == CLUSTERING_UPDATE_NOT_ALLOWED_CODE
        assert "BigQuery" in exc.message
        client.close.assert_called_once()

    def test_other_api_errors_pass_unchanged(self, tmp_path: Path) -> None:
        client = _client(None)
        client.update_table_definition.side_effect = KeboolaApiError(
            message="Forbidden", status_code=403, error_code=ErrorCode.ACCESS_DENIED
        )
        with pytest.raises(KeboolaApiError) as exc_info:
            self._run(tmp_path, client, fields=["a"])
        assert exc_info.value.error_code == ErrorCode.ACCESS_DENIED
        assert exc_info.value.status_code == 403

    def test_failed_reread_after_job_reports_updated_with_warning(self, tmp_path: Path) -> None:
        client = _client(["x"])
        client.get_table_detail.side_effect = [
            _table(["x"]),
            KeboolaApiError(message="Bad gateway", status_code=502, error_code=ErrorCode.API_ERROR),
        ]
        result = self._run(tmp_path, client, fields=["a"])
        assert result["status"] == "updated"
        assert result["job_id"] == 555
        assert result["clustering"] == ["a"]
        assert [w["code"] for w in result["warnings"]] == ["CLUSTERING_REREAD_FAILED"]
        client.close.assert_called_once()

    def test_timeout_names_job_detail_and_table_detail(self, tmp_path: Path) -> None:
        client = _client(None)
        client.update_table_definition.side_effect = _timeout()
        with pytest.raises(KeboolaApiError) as exc_info:
            self._run(tmp_path, client, fields=["a"], branch_id=42)
        exc = exc_info.value
        assert exc.error_code == ErrorCode.STORAGE_JOB_TIMEOUT
        assert exc.retryable is False
        assert "kbagent storage job-detail --project test --job-id 777 --wait" in exc.message
        assert f"kbagent storage table-detail --project test --table-id {TABLE}" in exc.message


# ---------------------------------------------------------------------------
# CLI layer
# ---------------------------------------------------------------------------

UPDATED = {
    "project_alias": "test",
    "table_id": TABLE,
    "branch_id": None,
    "clustering_before": None,
    "clustering": ["a", "b"],
    "status": "updated",
    "job_id": 555,
    "result": {"id": 555},
    "message": "Clustering set on table.",
}


def _invoke(tmp_path: Path, args: list[str], *, result: Any = None, error: Any = None):
    store = _make_store(tmp_path)
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.StorageService") as MockSvc,
    ):
        MockStore.return_value = store
        svc = MockSvc.return_value
        svc.set_table_clustering.return_value = result
        svc.set_table_clustering.side_effect = error
        out = runner.invoke(
            app,
            ["storage", "clustering-set", "--project", "test", "--table-id", TABLE, *args],
        )
    return out, svc


class TestClusteringCLI:
    def test_json_updated(self, tmp_path: Path) -> None:
        out, svc = _invoke(
            tmp_path,
            ["--clustering-field", "a", "--clustering-field", "b", "--json"],
            result=UPDATED,
        )
        assert out.exit_code == 0, out.output
        data = json.loads(out.output)["data"]
        assert data["status"] == "updated"
        assert data["job_id"] == 555
        svc.set_table_clustering.assert_called_once_with(
            alias="test",
            table_id=TABLE,
            fields=["a", "b"],
            clear=False,
            branch_id=None,
            dry_run=False,
            timeout=STORAGE_JOB_MAX_WAIT,
        )

    def test_json_unchanged(self, tmp_path: Path) -> None:
        unchanged = {**UPDATED, "status": "unchanged", "job_id": None, "result": None}
        out, _ = _invoke(tmp_path, ["--clustering-field", "a", "--json"], result=unchanged)
        assert out.exit_code == 0, out.output
        assert json.loads(out.output)["data"]["status"] == "unchanged"

    def test_human_set_and_clear(self, tmp_path: Path) -> None:
        out, _ = _invoke(tmp_path, ["--clustering-field", "a"], result=UPDATED)
        assert out.exit_code == 0, out.output
        assert f"Clustering set: {TABLE} -> a, b (job 555)" in out.output
        assert "existing rows keep their order" in out.output

        cleared = {**UPDATED, "clustering_before": ["a"], "clustering": None}
        out, svc = _invoke(tmp_path, ["--clear"], result=cleared)
        assert out.exit_code == 0, out.output
        assert f"Clustering removed: {TABLE} (job 555)" in out.output
        assert svc.set_table_clustering.call_args.kwargs["clear"] is True

    def test_dry_run_json(self, tmp_path: Path) -> None:
        dry = {
            "dry_run": True,
            "project_alias": "test",
            "table_id": TABLE,
            "branch_id": None,
            "clustering_before": ["c"],
            "clustering": ["a"],
            "would_change": True,
        }
        out, svc = _invoke(tmp_path, ["--clustering-field", "a", "--dry-run", "--json"], result=dry)
        assert out.exit_code == 0, out.output
        assert json.loads(out.output)["data"]["would_change"] is True
        assert svc.set_table_clustering.call_args.kwargs["dry_run"] is True

    def test_invalid_combination_exit_2(self, tmp_path: Path) -> None:
        error = KeboolaApiError(
            message="--clustering-field and --clear are mutually exclusive.",
            status_code=400,
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        out, _ = _invoke(tmp_path, ["--clustering-field", "a", "--clear", "--json"], error=error)
        assert out.exit_code == 2, out.output
        assert json.loads(out.output)["error"]["code"] == "INVALID_ARGUMENT"

    def test_422_exit_2_keeps_details(self, tmp_path: Path) -> None:
        error = KeboolaApiError(
            message="Column 'missing' does not exist.",
            status_code=400,
            error_code=ErrorCode.INVALID_ARGUMENT,
            details={"api_error_code": CLUSTERING_UPDATE_NOT_ALLOWED_CODE},
        )
        out, _ = _invoke(tmp_path, ["--clustering-field", "missing", "--json"], error=error)
        assert out.exit_code == 2, out.output
        payload = json.loads(out.output)["error"]
        assert payload["code"] == "INVALID_ARGUMENT"
        assert payload["details"]["api_error_code"] == CLUSTERING_UPDATE_NOT_ALLOWED_CODE

    def test_timeout_exit_4(self, tmp_path: Path) -> None:
        out, _ = _invoke(tmp_path, ["--clustering-field", "a", "--json"], error=_timeout())
        assert out.exit_code == 4, out.output
        payload = json.loads(out.output)["error"]
        assert payload["code"] == ErrorCode.STORAGE_JOB_TIMEOUT
        assert payload["retryable"] is False
        assert payload["details"]["job_id"] == 777

    @pytest.mark.parametrize("bad", ["0", "-5", "nan"])
    def test_bad_timeout_exit_2(self, tmp_path: Path, bad: str) -> None:
        out, svc = _invoke(
            tmp_path, ["--clustering-field", "a", "--timeout", bad, "--json"], result=UPDATED
        )
        assert out.exit_code == 2, out.output
        assert json.loads(out.output)["error"]["code"] == "INVALID_ARGUMENT"
        svc.set_table_clustering.assert_not_called()
