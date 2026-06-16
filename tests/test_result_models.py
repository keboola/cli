"""Contract tests for the typed SDK return models (result_models.py, issue #428).

These models are the committed, semver-stable surface a downstream in-process
consumer types against. The tests pin three invariants per model:

  1. the real API keys (camelCase, sometimes asymmetric like ``component`` vs
     ``configId``) map onto the snake_case fields,
  2. unknown/extra fields survive (``extra="allow"``) so backend drift never
     raises,
  3. the convenience properties compute correctly.
"""

from keboola_agent_cli import (
    ConfigDetailResult,
    JobResult,
    QueryResult,
    SyncPushResult,
    UploadTableResult,
)
from keboola_agent_cli.result_models import _ApiResultModel


class TestJobResult:
    def test_maps_real_queue_keys(self) -> None:
        j = JobResult.model_validate(
            {
                "id": "98765",
                "status": "success",
                "isFinished": True,
                "component": "keboola.ex-db-snowflake",
                "configId": "12345",
                "mode": "run",
                "result": {"message": "Extraction finished"},
                "resolvedVariableValuesId": "row-7",
                "logTail": [{"message": "done"}],
            }
        )
        assert j.id == "98765"
        assert j.is_finished is True
        # NB: the Queue job response is asymmetric -- component but configId.
        assert j.component_id == "keboola.ex-db-snowflake"
        assert j.config_id == "12345"
        assert j.result == {"message": "Extraction finished"}
        assert j.resolved_variable_values_id == "row-7"
        assert j.log_tail == [{"message": "done"}]

    def test_succeeded_and_failed_properties(self) -> None:
        assert JobResult(status="success").succeeded is True
        assert JobResult(status="success").failed is False
        for bad in ("error", "terminated", "cancelled"):
            assert JobResult(status=bad).failed is True
            assert JobResult(status=bad).succeeded is False
        # processing is neither
        assert JobResult(status="processing").failed is False
        assert JobResult(status="processing").succeeded is False

    def test_extras_preserved(self) -> None:
        j = JobResult.model_validate(
            {"id": "1", "status": "success", "branchId": 42, "durationSeconds": 9, "url": "u"}
        )
        dumped = j.model_dump()
        assert dumped["branchId"] == 42
        assert dumped["durationSeconds"] == 9
        assert dumped["url"] == "u"

    def test_populate_by_field_name(self) -> None:
        j = JobResult(id="x", status="error", is_finished=True, component_id="c", config_id="cc")
        assert j.component_id == "c" and j.config_id == "cc" and j.is_finished is True

    def test_empty_defaults(self) -> None:
        j = JobResult()
        assert j.id == "" and j.status == "" and j.is_finished is False
        assert j.result is None and j.log_tail is None


class TestQueryResult:
    def test_shape_and_row_count(self) -> None:
        q = QueryResult(
            columns=["ID", "NAME"],
            rows=[{"ID": "1", "NAME": "alice"}, {"ID": "2", "NAME": "bob"}],
            truncated=True,
            total_rows=999,
        )
        assert q.columns == ["ID", "NAME"]
        assert q.row_count == 2
        assert q.truncated is True
        assert q.total_rows == 999

    def test_defaults_empty(self) -> None:
        q = QueryResult()
        assert q.columns == [] and q.rows == [] and q.row_count == 0
        assert q.truncated is False and q.total_rows is None


class TestUploadTableResult:
    def test_maps_imported_rows_count_alias(self) -> None:
        up = UploadTableResult.model_validate(
            {"table_id": "in.c-x.t", "importedRowsCount": 50, "warnings": ["w"]}
        )
        assert up.imported_rows == 50
        assert up.table_id == "in.c-x.t"
        assert up.warnings == ["w"]
        assert up.auto_created_bucket is False and up.auto_created_table is False

    def test_service_shape_with_snake_case(self) -> None:
        up = UploadTableResult.model_validate(
            {
                "project_alias": "prod",
                "table_id": "in.c-x.t",
                "incremental": True,
                "file_size_bytes": 1024,
                "imported_rows": 7,
                "auto_created_bucket": True,
                "auto_created_table": True,
            }
        )
        assert up.imported_rows == 7 and up.incremental is True
        assert up.file_size_bytes == 1024
        assert up.auto_created_bucket and up.auto_created_table
        assert up.project_alias == "prod"


class TestSyncPushResult:
    def test_ok_property(self) -> None:
        clean = SyncPushResult.model_validate(
            {"status": "pushed", "created": 3, "updated": 1, "errors": []}
        )
        assert clean.ok is True and clean.created == 3 and clean.updated == 1

        failed = SyncPushResult.model_validate(
            {"status": "pushed", "errors": [{"message": "boom"}]}
        )
        assert failed.ok is False

    def test_defaults(self) -> None:
        r = SyncPushResult()
        assert r.created == 0 and r.updated == 0 and r.deleted == 0
        assert r.errors == [] and r.pushed_details == [] and r.name_drift_warnings == []
        assert r.ok is True


class TestConfigDetailResult:
    def test_current_version_alias(self) -> None:
        cd = ConfigDetailResult.model_validate(
            {
                "id": "cfg-1",
                "name": "My Config",
                "description": "d",
                "currentVersion": 5,
                "configuration": {"parameters": {"x": 1}},
                "rows": [{"id": "r1"}],
            }
        )
        assert cd.id == "cfg-1" and cd.version == 5
        assert cd.configuration == {"parameters": {"x": 1}}
        assert cd.rows == [{"id": "r1"}]

    def test_plain_version_field(self) -> None:
        cd = ConfigDetailResult.model_validate({"id": "c", "version": 2})
        assert cd.version == 2

    def test_component_id_alias_and_extras(self) -> None:
        cd = ConfigDetailResult.model_validate(
            {"id": "c", "componentId": "keboola.ex-http", "isDisabled": True}
        )
        assert cd.component_id == "keboola.ex-http"
        # untyped Storage fields survive
        assert cd.model_dump()["isDisabled"] is True


class TestBaseConfig:
    def test_all_models_allow_extra(self) -> None:
        for model in (
            JobResult,
            QueryResult,
            UploadTableResult,
            SyncPushResult,
            ConfigDetailResult,
        ):
            assert issubclass(model, _ApiResultModel)
            assert model.model_config.get("extra") == "allow"
            assert model.model_config.get("populate_by_name") is True
