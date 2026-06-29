"""Tests for storage create-bucket, create-table, and upload-table commands and service methods."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
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

    def test_legacy_branch_storage_flagged_when_feature_missing(self, tmp_path: Path) -> None:
        """create-bucket --branch X on a fake-branch project surfaces the warning flag."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_bucket.return_value = {
            "id": "out.c-recon",
            "stage": "out",
            "backend": "snowflake",
            "description": "",
        }
        # Simulate fake-branch project: storage-branches feature is OFF.
        mock_client.has_feature.return_value = False
        service = _make_service(store, mock_client)

        result = service.create_bucket(alias="test", stage="out", name="recon", branch_id=12345)

        assert result["legacy_branch_storage"] is True
        mock_client.has_feature.assert_called_once_with("storage-branches")

    def test_legacy_branch_storage_false_on_modern_project(self, tmp_path: Path) -> None:
        """create-bucket --branch X on storage-branches=ON project flags False."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_bucket.return_value = {
            "id": "out.c-recon",
            "stage": "out",
            "backend": "snowflake",
            "description": "",
        }
        mock_client.has_feature.return_value = True
        service = _make_service(store, mock_client)

        result = service.create_bucket(alias="test", stage="out", name="recon", branch_id=12345)

        assert result["legacy_branch_storage"] is False

    def test_no_feature_check_on_production_writes(self, tmp_path: Path) -> None:
        """Without --branch we never consult features (no extra verify_token cost)."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_bucket.return_value = {
            "id": "in.c-prod",
            "stage": "in",
            "backend": "snowflake",
            "description": "",
        }
        service = _make_service(store, mock_client)

        result = service.create_bucket(alias="test", stage="in", name="prod")

        assert result["legacy_branch_storage"] is False
        mock_client.has_feature.assert_not_called()

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
            source=None,
            time_partitioning=None,
            range_partitioning=None,
            clustering=None,
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
            source=None,
            time_partitioning=None,
            range_partitioning=None,
            clustering=None,
        )

    def test_unknown_type_is_passed_through_to_api(self, tmp_path: Path) -> None:
        """Non-base types are no longer rejected by CLI -- API decides validity.

        Keboola Storage API has accurate per-backend validation ("'10' is not
        valid length for INTEGER") and accepts a huge native-type surface
        (VARCHAR, NUMBER, TIMESTAMP_TZ, VARIANT, ...) that varies per backend.
        Maintaining a CLI whitelist is both wrong (rejects legitimate native
        types) and redundant (API already validates). This regression test
        pins the pass-through behaviour.
        """
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_table.return_value = {"id": "in.c-b.t"}
        service = _make_service(store, mock_client)

        service.create_table(alias="test", bucket_id="in.c-b", name="t", columns=["x:BANANA"])

        assert mock_client.create_table.call_args.kwargs["columns"] == [
            {"name": "x", "definition": {"type": "BANANA"}}
        ]

    def test_malformed_spec_raises(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service = _make_service(store, MagicMock())

        # Length argument must be numeric. 'abc' is not -- rejected at CLI.
        with pytest.raises(ValueError, match="Invalid column spec"):
            service.create_table(
                alias="test", bucket_id="in.c-b", name="t", columns=["x:TYPE(abc)"]
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

    # ---- Native types with length -----------------------------------------

    def test_native_type_with_length(self, tmp_path: Path) -> None:
        """VARCHAR(40), NUMBER(18,2) etc. produce definition.length on the API body."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_table.return_value = {"id": "in.c-b.t"}
        service = _make_service(store, mock_client)

        service.create_table(
            alias="test",
            bucket_id="in.c-b",
            name="t",
            columns=[
                "pk:VARCHAR(40)",
                "amount:NUMERIC(18,2)",
                "ts:TIMESTAMP_TZ",
                "meta:VARIANT",
            ],
        )

        assert mock_client.create_table.call_args.kwargs["columns"] == [
            {"name": "pk", "definition": {"type": "VARCHAR", "length": "40"}},
            {"name": "amount", "definition": {"type": "NUMERIC", "length": "18,2"}},
            {"name": "ts", "definition": {"type": "TIMESTAMP_TZ"}},
            {"name": "meta", "definition": {"type": "VARIANT"}},
        ]

    def test_length_with_spaces_is_stripped(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_table.return_value = {"id": "in.c-b.t"}
        service = _make_service(store, mock_client)

        service.create_table(
            alias="test",
            bucket_id="in.c-b",
            name="t",
            columns=["amt:NUMBER(18, 2)"],
        )

        assert (
            mock_client.create_table.call_args.kwargs["columns"][0]["definition"]["length"]
            == "18,2"
        )

    # ---- NOT NULL + DEFAULT -----------------------------------------------

    def test_not_null_and_default(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_table.return_value = {"id": "in.c-b.sales"}
        service = _make_service(store, mock_client)

        service.create_table(
            alias="test",
            bucket_id="in.c-b",
            name="sales",
            columns=["pk:VARCHAR(40)", "amount:NUMERIC(18,2)", "is_paid:BOOLEAN"],
            primary_key=["pk"],
            not_null_columns=["pk", "amount"],
            defaults=["amount=0", "is_paid=false"],
        )

        assert mock_client.create_table.call_args.kwargs["columns"] == [
            {"name": "pk", "definition": {"type": "VARCHAR", "length": "40", "nullable": False}},
            {
                "name": "amount",
                "definition": {
                    "type": "NUMERIC",
                    "length": "18,2",
                    "nullable": False,
                    "default": "0",
                },
            },
            {
                "name": "is_paid",
                "definition": {"type": "BOOLEAN", "default": "false"},
            },
        ]

    def test_not_null_references_unknown_column(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service = _make_service(store, MagicMock())

        with pytest.raises(ValueError, match="--not-null references unknown column"):
            service.create_table(
                alias="test",
                bucket_id="in.c-b",
                name="t",
                columns=["id:INTEGER"],
                not_null_columns=["typo_id"],
            )

    def test_default_references_unknown_column(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service = _make_service(store, MagicMock())

        with pytest.raises(ValueError, match="--default references unknown column"):
            service.create_table(
                alias="test",
                bucket_id="in.c-b",
                name="t",
                columns=["id:INTEGER"],
                defaults=["other=42"],
            )

    def test_malformed_default_assignment(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service = _make_service(store, MagicMock())

        with pytest.raises(ValueError, match="Invalid --default"):
            service.create_table(
                alias="test",
                bucket_id="in.c-b",
                name="t",
                columns=["id:INTEGER"],
                defaults=["no_equals_sign"],
            )

    # ---- Auto-materialize bucket in dev branch ----------------------------

    def test_branch_auto_materialize_bucket_on_404(self, tmp_path: Path) -> None:
        """In a dev branch, a 404 on get_bucket_detail triggers create_bucket."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_bucket_detail.side_effect = KeboolaApiError(
            "Bucket not found", status_code=404, error_code="NOT_FOUND"
        )
        mock_client.create_bucket.return_value = {"id": "in.c-my-bucket"}
        mock_client.set_bucket_metadata.return_value = []
        mock_client.create_table.return_value = {"id": "in.c-my-bucket.t"}
        service = _make_service(store, mock_client)

        result = service.create_table(
            alias="test",
            bucket_id="in.c-my-bucket",
            name="t",
            columns=["id:INTEGER"],
            branch_id=12345,
        )

        mock_client.get_bucket_detail.assert_called_once_with("in.c-my-bucket", branch_id=12345)
        mock_client.create_bucket.assert_called_once_with(
            stage="in", name="my-bucket", branch_id=12345
        )
        # Issue #224: after create_bucket, KBC.createdBy.branch.id must be
        # set with provider="system" so output-mapping's branched-storage
        # check (BucketCreator::checkDevBucketMetadata) passes.
        mock_client.set_bucket_metadata.assert_called_once_with(
            bucket_id="in.c-my-bucket",
            entries=[("KBC.createdBy.branch.id", "12345")],
            provider="system",
            branch_id=12345,
        )
        assert result["auto_created_bucket"] is True

    def test_branch_auto_materialize_metadata_failure_does_not_abort(self, tmp_path: Path) -> None:
        """If set_bucket_metadata fails, the materialize+table-create still proceeds.

        The metadata write is best-effort: an Insufficient-Permissions or
        flaky 5xx must not block the user's create-table call. The runner
        will fail later with a clearer message if the bucket really cannot
        be assigned.
        """
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_bucket_detail.side_effect = KeboolaApiError(
            "Bucket not found", status_code=404, error_code="NOT_FOUND"
        )
        mock_client.create_bucket.return_value = {"id": "in.c-my-bucket"}
        mock_client.set_bucket_metadata.side_effect = KeboolaApiError(
            "Forbidden", status_code=403, error_code="FORBIDDEN"
        )
        mock_client.create_table.return_value = {"id": "in.c-my-bucket.t"}
        service = _make_service(store, mock_client)

        result = service.create_table(
            alias="test",
            bucket_id="in.c-my-bucket",
            name="t",
            columns=["id:INTEGER"],
            branch_id=12345,
        )

        mock_client.create_bucket.assert_called_once()
        mock_client.set_bucket_metadata.assert_called_once()
        mock_client.create_table.assert_called_once()
        assert result["auto_created_bucket"] is True

    def test_branch_no_materialize_when_bucket_exists(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_bucket_detail.return_value = {"id": "in.c-b"}
        mock_client.create_table.return_value = {"id": "in.c-b.t"}
        service = _make_service(store, mock_client)

        result = service.create_table(
            alias="test",
            bucket_id="in.c-b",
            name="t",
            columns=["id:INTEGER"],
            branch_id=777,
        )

        mock_client.create_bucket.assert_not_called()
        # No materialize -> no metadata write either; we only set the
        # branch-id stamp on buckets we just created (existing buckets
        # already have whatever metadata they need).
        mock_client.set_bucket_metadata.assert_not_called()
        assert result["auto_created_bucket"] is False

    def test_create_table_branch_legacy_storage_flagged(self, tmp_path: Path) -> None:
        """create-table --branch X on a fake-branch project: both auto-materialize
        AND legacy_branch_storage=True surface in the response. The metadata
        stamp still runs (best-effort) since storage-branches=OFF projects also
        accept it; the runner just won't read it.
        """
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_bucket_detail.side_effect = KeboolaApiError(
            "Bucket not found", status_code=404, error_code="NOT_FOUND"
        )
        mock_client.create_bucket.return_value = {"id": "out.c-recon"}
        mock_client.set_bucket_metadata.return_value = []
        mock_client.create_table.return_value = {"id": "out.c-recon.probe"}
        mock_client.has_feature.return_value = False  # fake-branch
        service = _make_service(store, mock_client)

        result = service.create_table(
            alias="test",
            bucket_id="out.c-recon",
            name="probe",
            columns=["id:INTEGER"],
            branch_id=12345,
        )

        assert result["auto_created_bucket"] is True
        assert result["legacy_branch_storage"] is True
        mock_client.has_feature.assert_called_once_with("storage-branches")

    def test_create_table_branch_modern_storage_no_warning(self, tmp_path: Path) -> None:
        """create-table --branch X on storage-branches=ON: legacy flag stays False."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_bucket_detail.return_value = {"id": "out.c-recon"}
        mock_client.create_table.return_value = {"id": "out.c-recon.probe"}
        mock_client.has_feature.return_value = True
        service = _make_service(store, mock_client)

        result = service.create_table(
            alias="test",
            bucket_id="out.c-recon",
            name="probe",
            columns=["id:INTEGER"],
            branch_id=12345,
        )

        assert result["auto_created_bucket"] is False
        assert result["legacy_branch_storage"] is False

    def test_create_table_no_branch_no_feature_check(self, tmp_path: Path) -> None:
        """Without --branch, create_table skips the feature lookup entirely."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_table.return_value = {"id": "in.c-prod.t"}
        service = _make_service(store, mock_client)

        result = service.create_table(
            alias="test",
            bucket_id="in.c-prod",
            name="t",
            columns=["id:INTEGER"],
        )

        assert result["legacy_branch_storage"] is False
        mock_client.has_feature.assert_not_called()

    def test_production_never_materializes(self, tmp_path: Path) -> None:
        """Without --branch, we never peek at bucket existence (production path)."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_table.return_value = {"id": "in.c-b.t"}
        service = _make_service(store, mock_client)

        result = service.create_table(
            alias="test",
            bucket_id="in.c-b",
            name="t",
            columns=["id:INTEGER"],
        )

        mock_client.get_bucket_detail.assert_not_called()
        mock_client.create_bucket.assert_not_called()
        assert result["auto_created_bucket"] is False

    def test_branch_non_404_propagates(self, tmp_path: Path) -> None:
        """403/500 on bucket check must not swallow the error into a silent create."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_bucket_detail.side_effect = KeboolaApiError(
            "Forbidden", status_code=403, error_code="FORBIDDEN"
        )
        service = _make_service(store, mock_client)

        with pytest.raises(KeboolaApiError, match="Forbidden"):
            service.create_table(
                alias="test",
                bucket_id="in.c-b",
                name="t",
                columns=["id:INTEGER"],
                branch_id=42,
            )
        mock_client.create_bucket.assert_not_called()


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
# Service tests: create_table --if-not-exists (v0.47.0)
# ---------------------------------------------------------------------------


class TestCreateTableIfNotExists:
    """`if_not_exists=True` turns duplicate-display-name into a skip."""

    @staticmethod
    def _duplicate_display_name_error() -> KeboolaApiError:
        return KeboolaApiError(
            message=(
                "Bucket in.c-b.users already has the same display name in "
                "bucket in.c-b. Please rename one of them."
            ),
            status_code=500,
            error_code=ErrorCode.STORAGE_JOB_FAILED,
        )

    def test_skip_on_existing_when_flag_set(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_table.side_effect = self._duplicate_display_name_error()
        mock_client.get_table_detail.return_value = {
            "id": "in.c-b.users",
            "name": "users",
            "columns": ["id", "name"],
            "primaryKey": ["id"],
        }
        service = _make_service(store, mock_client)

        result = service.create_table(
            alias="test",
            bucket_id="in.c-b",
            name="users",
            columns=["id:INTEGER", "name:STRING"],
            primary_key=["id"],
            if_not_exists=True,
        )

        assert result["action"] == "skipped"
        assert result["skip_reason"] == "table already exists"
        assert result["table_id"] == "in.c-b.users"
        mock_client.get_table_detail.assert_called_once_with("in.c-b.users", branch_id=None)
        mock_client.close.assert_called_once()

    def test_skip_returns_actual_schema_not_requested(self, tmp_path: Path) -> None:
        """keboola/cli#349: the skipped envelope must report the EXISTING
        table's schema, not re-echo the caller's request. Here the existing
        table has fewer columns and a different PK than what was requested."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_table.side_effect = self._duplicate_display_name_error()
        mock_client.get_table_detail.return_value = {
            "id": "in.c-b.users",
            "name": "users",
            "columns": ["id", "name"],
            "primaryKey": ["id"],
        }
        service = _make_service(store, mock_client)

        result = service.create_table(
            alias="test",
            bucket_id="in.c-b",
            name="users",
            columns=["id:INTEGER", "name:STRING", "extra:STRING"],
            primary_key=["extra"],
            if_not_exists=True,
        )

        # Actual existing schema is reported.
        assert result["columns"] == ["id", "name"]
        assert result["primary_key"] == ["id"]
        assert result["name"] == "users"
        # Caller's request is mirrored, not lost.
        assert result["requested_columns"] == ["id", "name", "extra"]
        assert result["requested_primary_key"] == ["extra"]
        # Divergence is flagged.
        assert result["schema_drift"] is True

    def test_no_schema_drift_when_existing_matches_request(self, tmp_path: Path) -> None:
        """When the existing table matches the request, schema_drift is False
        and columns/primary_key are still sourced from the actual table."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_table.side_effect = self._duplicate_display_name_error()
        mock_client.get_table_detail.return_value = {
            "id": "in.c-b.users",
            "name": "users",
            "columns": ["id", "name"],
            "primaryKey": ["id"],
        }
        service = _make_service(store, mock_client)

        result = service.create_table(
            alias="test",
            bucket_id="in.c-b",
            name="users",
            columns=["id:INTEGER", "name:STRING"],
            primary_key=["id"],
            if_not_exists=True,
        )

        assert result["schema_drift"] is False
        assert result["columns"] == ["id", "name"]
        assert result["primary_key"] == ["id"]

    def test_drift_is_order_insensitive(self, tmp_path: Path) -> None:
        """Column/PK reordering between request and existing table is the same
        set of columns -- not a drift (set comparison, not list equality)."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_table.side_effect = self._duplicate_display_name_error()
        mock_client.get_table_detail.return_value = {
            "id": "in.c-b.users",
            "name": "users",
            "columns": ["name", "id"],
            "primaryKey": ["id"],
        }
        service = _make_service(store, mock_client)

        result = service.create_table(
            alias="test",
            bucket_id="in.c-b",
            name="users",
            columns=["id:INTEGER", "name:STRING"],
            primary_key=["id"],
            if_not_exists=True,
        )

        assert result["schema_drift"] is False

    def test_reraises_when_flag_unset(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_table.side_effect = self._duplicate_display_name_error()
        service = _make_service(store, mock_client)

        with pytest.raises(KeboolaApiError) as excinfo:
            service.create_table(
                alias="test",
                bucket_id="in.c-b",
                name="users",
                columns=["id:INTEGER"],
            )
        assert excinfo.value.error_code == ErrorCode.STORAGE_JOB_FAILED
        # No probe when flag is off.
        mock_client.get_table_detail.assert_not_called()
        mock_client.close.assert_called_once()

    def test_reraises_when_target_table_missing(self, tmp_path: Path) -> None:
        """Duplicate-name error but the table at the expected id doesn't
        resolve → a different table is conflicting; surface the real error."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_table.side_effect = self._duplicate_display_name_error()
        mock_client.get_table_detail.side_effect = KeboolaApiError(
            message="404", status_code=404, error_code=ErrorCode.NOT_FOUND
        )
        service = _make_service(store, mock_client)

        with pytest.raises(KeboolaApiError) as excinfo:
            service.create_table(
                alias="test",
                bucket_id="in.c-b",
                name="users",
                columns=["id:INTEGER"],
                if_not_exists=True,
            )
        # The ORIGINAL error must propagate, not the lookup error.
        assert excinfo.value.error_code == ErrorCode.STORAGE_JOB_FAILED

    def test_non_duplicate_error_reraises_even_with_flag(self, tmp_path: Path) -> None:
        """A non-duplicate STORAGE_JOB_FAILED still surfaces — the IF-NOT-
        EXISTS path is gated on the specific message substring."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_table.side_effect = KeboolaApiError(
            message="quota exceeded",
            status_code=500,
            error_code=ErrorCode.STORAGE_JOB_FAILED,
        )
        service = _make_service(store, mock_client)

        with pytest.raises(KeboolaApiError) as excinfo:
            service.create_table(
                alias="test",
                bucket_id="in.c-b",
                name="users",
                columns=["id:INTEGER"],
                if_not_exists=True,
            )
        assert "quota" in str(excinfo.value.message).lower()
        mock_client.get_table_detail.assert_not_called()

    def test_success_path_unchanged_with_flag(self, tmp_path: Path) -> None:
        """When the create succeeds, the flag has no effect on the envelope."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_table.return_value = {"id": "in.c-b.users"}
        service = _make_service(store, mock_client)

        result = service.create_table(
            alias="test",
            bucket_id="in.c-b",
            name="users",
            columns=["id:INTEGER"],
            if_not_exists=True,
        )

        assert result["action"] == "created"
        assert result["table_id"] == "in.c-b.users"


# ---------------------------------------------------------------------------
# CLI tests: create-table
# ---------------------------------------------------------------------------


class TestCreateTableCLI:
    """CLI tests for `kbagent storage create-table`."""

    def test_human_renders_skip_when_action_is_skipped(self, tmp_path: Path) -> None:
        """When --if-not-exists triggers a skip, human mode prints
        'Skipped (already exists)' instead of the misleading 'Created table'."""
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
                "action": "skipped",
                "skip_reason": "table already exists",
            }
            result = runner.invoke(
                app,
                [
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
                    "--if-not-exists",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "Skipped" in result.output
        assert "in.c-b.users" in result.output
        assert "table already exists" in result.output
        assert "Created table" not in result.output, (
            "must NOT print the misleading success line on a skipped row"
        )

    def test_human_warns_and_shows_actual_schema_on_drift(self, tmp_path: Path) -> None:
        """When the skipped table's schema diverges from the request, human
        mode warns and prints the ACTUAL existing columns/PK (keboola/cli#349)."""
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
                "requested_primary_key": ["extra"],
                "requested_columns": ["id", "name", "extra"],
                "schema_drift": True,
                "action": "skipped",
                "skip_reason": "table already exists",
            }
            result = runner.invoke(
                app,
                [
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
                    "--if-not-exists",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "Skipped" in result.output
        assert "Warning" in result.output
        # Actual existing columns are shown, not the requested 'extra'.
        assert "id, name" in result.output

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
            not_null_columns=None,
            defaults=None,
            if_not_exists=False,
            source_table_id=None,
            source_branch_id=None,
            time_partitioning_type=None,
            time_partitioning_field=None,
            time_partitioning_expiration_ms=None,
            range_partitioning_field=None,
            range_partitioning_start=None,
            range_partitioning_end=None,
            range_partitioning_interval=None,
            clustering_fields=None,
        )

    def test_create_table_native_types_and_attributes(self, tmp_path: Path) -> None:
        """End-to-end CLI smoke test covering VARCHAR(40), NUMERIC(18,2),
        TIMESTAMP_TZ plus --not-null and --default flags."""
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.create_table.return_value = {
                "project_alias": "test",
                "table_id": "in.c-b.sales",
                "name": "sales",
                "bucket_id": "in.c-b",
                "primary_key": ["pk"],
                "columns": ["pk", "amount", "ts", "is_paid"],
                "auto_created_bucket": False,
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
                    "sales",
                    "--column",
                    "pk:VARCHAR(40)",
                    "--column",
                    "amount:NUMERIC(18,2)",
                    "--column",
                    "ts:TIMESTAMP_TZ",
                    "--column",
                    "is_paid:BOOLEAN",
                    "--primary-key",
                    "pk",
                    "--not-null",
                    "pk",
                    "--not-null",
                    "amount",
                    "--default",
                    "amount=0",
                    "--default",
                    "is_paid=false",
                ],
            )
        assert result.exit_code == 0
        kwargs = svc.create_table.call_args.kwargs
        assert kwargs["columns"] == [
            "pk:VARCHAR(40)",
            "amount:NUMERIC(18,2)",
            "ts:TIMESTAMP_TZ",
            "is_paid:BOOLEAN",
        ]
        assert kwargs["not_null_columns"] == ["pk", "amount"]
        assert kwargs["defaults"] == ["amount=0", "is_paid=false"]

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

    def test_create_table_malformed_column_spec(self, tmp_path: Path) -> None:
        """Malformed specs exit 2 (INVALID_ARGUMENT); unknown type strings no
        longer trigger a CLI-side rejection -- they are sent to the API."""
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            MockSvc.return_value.create_table.side_effect = ValueError(
                "Invalid column spec 'bad col:STR'."
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
                    "bad col:STR",
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
            source=None,
            time_partitioning=None,
            range_partitioning=None,
            clustering=None,
        )
        # In a branch we check bucket existence first (auto-materialize).
        mock_client.get_bucket_detail.assert_called_once_with("in.c-b", branch_id=77)

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


# ---------------------------------------------------------------------------
# Service tests: download_table
# ---------------------------------------------------------------------------


class TestDownloadTableService:
    """Tests for StorageService.download_table()."""

    def test_success_full_export(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.export_table_async.return_value = {
            "results": {"file": {"id": 42}},
        }
        mock_client.get_file_info.return_value = {
            "id": 42,
            "url": "https://s3.example.com/data.csv",
            "isSliced": False,
        }
        mock_client.list_tables.return_value = [
            {"id": "in.c-b.users", "columns": ["id", "name", "email"]},
        ]

        out_file = tmp_path / "output.csv"

        def _fake_download(url, path):
            Path(path).write_text('"1","Alice","a@b.c"\n')
            return 1024

        mock_client.download_file.side_effect = _fake_download
        service = _make_service(store, mock_client)

        result = service.download_table(
            alias="test",
            table_id="in.c-b.users",
            output_path=str(out_file),
        )

        assert result["table_id"] == "in.c-b.users"
        assert result["output_path"] == str(out_file.resolve())
        assert result["columns"] == ["id", "name", "email"]
        # Header was prepended
        content = out_file.read_text()
        assert content.startswith('"id","name","email"\n')
        mock_client.export_table_async.assert_called_once_with(
            table_id="in.c-b.users",
            columns=None,
            limit=None,
            branch_id=None,
            where_column=None,
            where_operator="eq",
            where_values=None,
            changed_since=None,
            changed_until=None,
        )
        mock_client.get_file_info.assert_called_once_with(42, branch_id=None)
        mock_client.close.assert_called_once()

    def test_with_columns_and_limit(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.export_table_async.return_value = {
            "results": {"file": {"id": 99}},
        }
        mock_client.get_file_info.return_value = {
            "id": 99,
            "url": "https://s3.example.com/filtered.csv",
            "isSliced": False,
        }
        out_file = tmp_path / "events.csv"

        def _fake_download(url, path):
            Path(path).write_text('"1","Alice"\n')
            return 512

        mock_client.download_file.side_effect = _fake_download
        mock_client.list_tables.return_value = []
        service = _make_service(store, mock_client)

        result = service.download_table(
            alias="test",
            table_id="in.c-b.events",
            output_path=str(out_file),
            columns=["id", "name"],
            limit=100,
        )

        assert result["columns"] == ["id", "name"]
        assert result["limit"] == 100
        # Check header was prepended
        content = out_file.read_text()
        assert content.startswith('"id","name"\n')
        mock_client.export_table_async.assert_called_once_with(
            table_id="in.c-b.events",
            columns=["id", "name"],
            limit=100,
            branch_id=None,
            where_column=None,
            where_operator="eq",
            where_values=None,
            changed_since=None,
            changed_until=None,
        )

    def test_derives_filename_from_table_id(self, tmp_path: Path) -> None:
        import os

        os.chdir(tmp_path)
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.export_table_async.return_value = {
            "results": {"file": {"id": 1}},
        }
        mock_client.get_file_info.return_value = {
            "id": 1,
            "url": "https://s3.example.com/data.csv",
            "isSliced": False,
        }
        mock_client.list_tables.return_value = []

        def _fake_download(url, path):
            Path(path).write_text('"data"\n')
            return 256

        mock_client.download_file.side_effect = _fake_download
        service = _make_service(store, mock_client)

        result = service.download_table(
            alias="test",
            table_id="in.c-my-bucket.my-table",
        )

        assert result["output_path"].endswith("my-table.csv")

    def test_sliced_file_calls_download_sliced(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.export_table_async.return_value = {
            "results": {"file": {"id": 55}},
        }
        file_detail = {
            "id": 55,
            "url": "https://s3.example.com/manifest",
            "isSliced": True,
            "provider": "aws",
        }
        mock_client.get_file_info.return_value = file_detail
        mock_client.list_tables.return_value = [
            {"id": "in.c-b.huge", "columns": ["a", "b"]},
        ]
        out_path = str(tmp_path / "out.csv")

        def _fake_sliced_download(detail, path):
            Path(path).write_text('"1","2"\n')
            return 4096

        mock_client.download_sliced_file.side_effect = _fake_sliced_download
        service = _make_service(store, mock_client)

        result = service.download_table(
            alias="test",
            table_id="in.c-b.huge",
            output_path=out_path,
        )

        assert result["columns"] == ["a", "b"]
        mock_client.download_sliced_file.assert_called_once_with(file_detail, out_path)
        mock_client.close.assert_called_once()

    def test_no_file_id_raises_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.export_table_async.return_value = {
            "results": {},
        }
        mock_client.list_tables.return_value = []
        service = _make_service(store, mock_client)

        with pytest.raises(KeboolaApiError, match="no file ID"):
            service.download_table(
                alias="test",
                table_id="in.c-b.t",
                output_path=str(tmp_path / "out.csv"),
            )

        mock_client.close.assert_called_once()

    def test_api_error_propagates(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.export_table_async.side_effect = KeboolaApiError(
            "Table not found", status_code=404, error_code="NOT_FOUND"
        )
        service = _make_service(store, mock_client)

        with pytest.raises(KeboolaApiError):
            service.download_table(
                alias="test",
                table_id="in.c-b.missing",
                output_path=str(tmp_path / "out.csv"),
            )

        mock_client.close.assert_called_once()

    def test_with_branch_id(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.export_table_async.return_value = {
            "results": {"file": {"id": 7}},
        }
        mock_client.get_file_info.return_value = {
            "id": 7,
            "url": "https://s3.example.com/branch.csv",
            "isSliced": False,
        }
        mock_client.list_tables.return_value = []

        def _fake_download(url, path):
            Path(path).write_text('"data"\n')
            return 128

        mock_client.download_file.side_effect = _fake_download
        service = _make_service(store, mock_client)

        service.download_table(
            alias="test",
            table_id="in.c-b.t",
            output_path=str(tmp_path / "out.csv"),
            branch_id=42,
        )

        mock_client.export_table_async.assert_called_once_with(
            table_id="in.c-b.t",
            columns=None,
            limit=None,
            branch_id=42,
            where_column=None,
            where_operator="eq",
            where_values=None,
            changed_since=None,
            changed_until=None,
        )
        # Issue #161: get_file_info must also receive branch_id
        mock_client.get_file_info.assert_called_once_with(7, branch_id=42)


# ---------------------------------------------------------------------------
# CLI tests: download-table
# ---------------------------------------------------------------------------


class TestDownloadTableCLI:
    """CLI tests for `kbagent storage download-table`."""

    def test_download_table_json(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.download_table.return_value = {
                "project_alias": "test",
                "table_id": "in.c-b.users",
                "output_path": "/tmp/users.csv",
                "file_size_bytes": 2048,
                "columns": None,
                "limit": None,
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "download-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-b.users",
                    "--output",
                    "/tmp/users.csv",
                ],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["table_id"] == "in.c-b.users"
        assert data["file_size_bytes"] == 2048
        svc.download_table.assert_called_once_with(
            alias="test",
            table_id="in.c-b.users",
            output_path="/tmp/users.csv",
            columns=None,
            limit=None,
            branch_id=None,
            keep_slices=False,
            where_column=None,
            where_operator="eq",
            where_values=None,
            changed_since=None,
            changed_until=None,
        )

    def test_download_table_with_columns_and_limit(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.download_table.return_value = {
                "project_alias": "test",
                "table_id": "in.c-b.events",
                "output_path": "/tmp/events.csv",
                "file_size_bytes": 512,
                "columns": ["id", "name"],
                "limit": 50,
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "download-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-b.events",
                    "--output",
                    "/tmp/events.csv",
                    "--columns",
                    "id",
                    "--columns",
                    "name",
                    "--limit",
                    "50",
                ],
            )
        assert result.exit_code == 0
        svc.download_table.assert_called_once_with(
            alias="test",
            table_id="in.c-b.events",
            output_path="/tmp/events.csv",
            columns=["id", "name"],
            limit=50,
            branch_id=None,
            keep_slices=False,
            where_column=None,
            where_operator="eq",
            where_values=None,
            changed_since=None,
            changed_until=None,
        )

    def test_download_table_api_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.download_table.side_effect = KeboolaApiError(
                "Table not found", status_code=404, error_code="NOT_FOUND"
            )
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "download-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-b.missing",
                ],
            )
        assert result.exit_code == 1

    def test_download_table_human_mode(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.download_table.return_value = {
                "project_alias": "test",
                "table_id": "in.c-b.users",
                "output_path": "/tmp/users.csv",
                "file_size_bytes": 1048576,
                "columns": None,
                "limit": None,
            }
            result = runner.invoke(
                app,
                [
                    "storage",
                    "download-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-b.users",
                    "--output",
                    "/tmp/users.csv",
                ],
            )
        assert result.exit_code == 0
        assert "Exported" in result.output

    def test_download_table_with_branch(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.download_table.return_value = {
                "project_alias": "test",
                "table_id": "in.c-b.data",
                "output_path": "/tmp/data.csv",
                "file_size_bytes": 100,
                "columns": None,
                "limit": None,
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "download-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-b.data",
                    "--branch",
                    "33",
                ],
            )
        assert result.exit_code == 0
        call_kwargs = svc.download_table.call_args.kwargs
        assert call_kwargs["branch_id"] == 33
