"""REST mirrors added for issue #657, plus the idempotency fields #427 built.

Three gaps from the 0.89.0 serve audit:

1. `storage describe-batch` had no route at all -- the whole bulk-documentation
   path was CLI-only.
2. `storage unload-table` had no route despite `unload_table_to_file` existing;
   the sync preview covers small reads, unload covers the big ones.
3. `POST /jobs/{p}/run` dropped `idempotency_key` / `force_rerun` even though
   `JobService.run_job` takes both -- and retrying a POST is precisely the case
   #427 built the store for.

The service layer is mocked throughout: what is under test is the router ->
service contract (kwarg names, defaults, and which CLI-only options are
deliberately NOT exposed), not Keboola behaviour.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

if importlib.util.find_spec("fastapi") is None:  # pragma: no cover
    pytest.skip(
        "FastAPI not installed; run `uv pip install -e '.[server]'`", allow_module_level=True
    )

from fastapi.testclient import TestClient

from keboola_agent_cli.server import create_app
from keboola_agent_cli.server.dependencies import ServiceRegistry, get_registry

AUTH = {"Authorization": "Bearer test-token"}
PROJECT = "my-proj"
TABLE_ID = "in.c-main.mytable"


def _client(tmp_path: Path, **services: Any) -> TestClient:
    registry = ServiceRegistry.__new__(ServiceRegistry)
    for name, mock in services.items():
        setattr(registry, name, mock)
    app = create_app(config_dir=str(tmp_path), auth_token="test-token")
    app.dependency_overrides[get_registry] = lambda: registry
    return TestClient(app)


def _storage_mock() -> MagicMock:
    svc = MagicMock()
    svc.describe_batch_document.return_value = {
        "project_alias": PROJECT,
        "applied": [],
        "errors": [],
        "applied_count": 0,
        "error_count": 0,
    }
    svc.unload_table_to_file.return_value = {"file_id": 123, "table_id": TABLE_ID}
    return svc


class TestDescribeBatchRoute:
    def test_sections_reach_the_service_as_one_document(self, tmp_path: Path) -> None:
        storage = _storage_mock()
        with _client(tmp_path, storage=storage) as client:
            res = client.post(
                f"/storage/describe-batch/{PROJECT}",
                headers=AUTH,
                json={
                    "buckets": {"in.c-sales": "All sales data"},
                    "tables": {"in.c-sales.orders": "One row per order"},
                    "columns": {"in.c-sales.orders": {"id": "Primary key"}},
                    "branch_id": 42,
                },
            )
        assert res.status_code == 200
        kwargs = storage.describe_batch_document.call_args.kwargs
        assert kwargs["alias"] == PROJECT
        assert kwargs["branch_id"] == 42
        assert kwargs["document"] == {
            "buckets": {"in.c-sales": "All sales data"},
            "tables": {"in.c-sales.orders": "One row per order"},
            "columns": {"in.c-sales.orders": {"id": "Primary key"}},
        }

    def test_omitted_sections_travel_as_none(self, tmp_path: Path) -> None:
        """An absent section must stay absent, not become an empty dict.

        `_describe_batch_input` treats absent and empty identically, but the
        route inventing `{}` would hide a caller sending nothing at all.
        """
        storage = _storage_mock()
        with _client(tmp_path, storage=storage) as client:
            res = client.post(
                f"/storage/describe-batch/{PROJECT}",
                headers=AUTH,
                json={"buckets": {"in.c-sales": "All sales data"}},
            )
        assert res.status_code == 200
        document = storage.describe_batch_document.call_args.kwargs["document"]
        assert document["tables"] is None
        assert document["columns"] is None

    def test_branch_id_defaults_to_none(self, tmp_path: Path) -> None:
        storage = _storage_mock()
        with _client(tmp_path, storage=storage) as client:
            client.post(f"/storage/describe-batch/{PROJECT}", headers=AUTH, json={})
        assert storage.describe_batch_document.call_args.kwargs["branch_id"] is None

    def test_shape_error_answers_422_with_the_cli_message(self, tmp_path: Path) -> None:
        """The point of sharing the validator: the REST caller gets #645's message."""
        storage = MagicMock()
        storage.describe_batch_document.side_effect = ValueError(
            "'buckets' must be a mapping of bucket ID to description, got a list."
        )
        with _client(tmp_path, storage=storage) as client:
            res = client.post(
                f"/storage/describe-batch/{PROJECT}",
                headers=AUTH,
                json={"buckets": ["in.c-sales"]},
            )
        assert res.status_code == 422
        assert "must be a mapping of bucket ID to description" in res.json()["error"]["message"]

    def test_per_item_api_errors_are_returned_not_raised(self, tmp_path: Path) -> None:
        """Shape is a 422; an API refusal mid-batch is a 200 with `errors`."""
        storage = MagicMock()
        storage.describe_batch_document.return_value = {
            "project_alias": PROJECT,
            "applied": [],
            "errors": [{"type": "bucket", "id": "in.c-nope", "error": "Bucket not found"}],
            "applied_count": 0,
            "error_count": 1,
        }
        with _client(tmp_path, storage=storage) as client:
            res = client.post(
                f"/storage/describe-batch/{PROJECT}",
                headers=AUTH,
                json={"buckets": {"in.c-nope": "x"}},
            )
        assert res.status_code == 200
        assert res.json()["error_count"] == 1


class TestUnloadTableRoute:
    def test_defaults_never_touch_the_server_filesystem(self, tmp_path: Path) -> None:
        """`download` is hard-wired False: the caller's disk is not the server's."""
        storage = _storage_mock()
        with _client(tmp_path, storage=storage) as client:
            res = client.post(f"/storage/tables/{PROJECT}/{TABLE_ID}/unload", headers=AUTH, json={})
        assert res.status_code == 200
        kwargs = storage.unload_table_to_file.call_args.kwargs
        assert kwargs["download"] is False
        assert "output_path" not in kwargs
        assert kwargs == {
            "alias": PROJECT,
            "table_id": TABLE_ID,
            "columns": None,
            "limit": None,
            "tags": None,
            "download": False,
            "branch_id": None,
            "file_type": "csv",
            "keep_slices": False,
        }

    def test_body_is_optional(self, tmp_path: Path) -> None:
        """An unload with no options is the common case; requiring `{}` is noise."""
        storage = _storage_mock()
        with _client(tmp_path, storage=storage) as client:
            res = client.post(f"/storage/tables/{PROJECT}/{TABLE_ID}/unload", headers=AUTH)
        assert res.status_code == 200
        assert storage.unload_table_to_file.call_args.kwargs["file_type"] == "csv"

    def test_all_options_reach_the_service(self, tmp_path: Path) -> None:
        storage = _storage_mock()
        with _client(tmp_path, storage=storage) as client:
            client.post(
                f"/storage/tables/{PROJECT}/{TABLE_ID}/unload",
                headers=AUTH,
                json={
                    "columns": ["id", "amount"],
                    "limit": 100,
                    "tags": ["export"],
                    "file_type": "parquet",
                    "keep_slices": True,
                    "branch_id": 7,
                },
            )
        kwargs = storage.unload_table_to_file.call_args.kwargs
        assert kwargs["columns"] == ["id", "amount"]
        assert kwargs["limit"] == 100
        assert kwargs["tags"] == ["export"]
        assert kwargs["file_type"] == "parquet"
        assert kwargs["keep_slices"] is True
        assert kwargs["branch_id"] == 7

    def test_dotted_table_id_survives_the_path_converter(self, tmp_path: Path) -> None:
        """`{table_id:path}` is greedy -- prove `/unload` is not eaten as part of it."""
        storage = _storage_mock()
        with _client(tmp_path, storage=storage) as client:
            client.post(
                f"/storage/tables/{PROJECT}/out.c-my.deeply.dotted/unload", headers=AUTH, json={}
            )
        assert storage.unload_table_to_file.call_args.kwargs["table_id"] == "out.c-my.deeply.dotted"


class TestJobRunIdempotency:
    def _job_mock(self) -> MagicMock:
        job = MagicMock()
        job.run_job.return_value = {"id": "job-1", "status": "created"}
        return job

    def test_key_and_force_rerun_reach_run_job(self, tmp_path: Path) -> None:
        job = self._job_mock()
        with _client(tmp_path, job=job) as client:
            res = client.post(
                f"/jobs/{PROJECT}/run",
                headers=AUTH,
                json={
                    "component_id": "keboola.ex-http",
                    "config_id": "42",
                    "idempotency_key": "nightly-2026-08-24",
                    "force_rerun": True,
                },
            )
        assert res.status_code == 200
        kwargs = job.run_job.call_args.kwargs
        assert kwargs["idempotency_key"] == "nightly-2026-08-24"
        assert kwargs["force_rerun"] is True

    def test_defaults_preserve_the_pre_657_call(self, tmp_path: Path) -> None:
        """Omitting both must be indistinguishable from the old body."""
        job = self._job_mock()
        with _client(tmp_path, job=job) as client:
            client.post(
                f"/jobs/{PROJECT}/run",
                headers=AUTH,
                json={"component_id": "keboola.ex-http", "config_id": "42"},
            )
        kwargs = job.run_job.call_args.kwargs
        assert kwargs["idempotency_key"] is None
        assert kwargs["force_rerun"] is False


class TestDescribeBatchValidatorIsShared:
    """The file and inline paths must accept and reject exactly the same documents."""

    @pytest.mark.parametrize(
        "document",
        [
            {"buckets": ["in.c-sales"]},
            {"tables": {"in.c-sales.orders": None}},
            {"columns": {"in.c-sales.orders": "not-a-mapping"}},
            "not-a-mapping-at-all",
        ],
    )
    def test_rejected_documents_raise_valueerror(self, document: Any) -> None:
        from keboola_agent_cli.services._describe_batch_input import (
            parse_describe_batch_document,
        )

        with pytest.raises(ValueError):
            parse_describe_batch_document(document, "request body")

    @pytest.mark.parametrize("empty", [None, {}, {"buckets": None}, {"buckets": {}}])
    def test_empty_sections_stay_a_silent_no_op(self, empty: Any) -> None:
        from keboola_agent_cli.services._describe_batch_input import (
            parse_describe_batch_document,
        )

        assert parse_describe_batch_document(empty, "request body").total == 0

    def test_source_label_names_the_body_not_a_filename(self) -> None:
        from keboola_agent_cli.services._describe_batch_input import (
            parse_describe_batch_document,
        )

        with pytest.raises(ValueError, match="'request body' must be"):
            parse_describe_batch_document("nope", "request body")
