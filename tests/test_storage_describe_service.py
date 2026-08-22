"""Tests for StorageService describe_* methods (bucket, table, columns, batch).

Also covers the read-back side: extraction of ``KBC.description`` and
``KBC.column.{name}.description`` metadata keys in ``get_bucket_detail`` /
``get_table_detail``, including the precedence between the native API
``description`` field and the ``KBC.description`` metadata entry.
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.models import AppConfig, ProjectConfig, TokenVerifyResponse
from keboola_agent_cli.services.storage_service import StorageService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

_META_RESPONSE = [
    {
        "id": "9001",
        "key": "KBC.description",
        "value": "A test description",
        "provider": "user",
        "timestamp": "2026-04-22T10:00:00Z",
    }
]


def _make_store(tmp_path: Path) -> ConfigStore:
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    store = ConfigStore(config_dir=config_dir)
    store.save(
        AppConfig(
            projects={
                "prod": ProjectConfig(
                    stack_url="https://connection.keboola.com",
                    token=TEST_TOKEN,
                )
            }
        )
    )
    return store


def _make_service(store: ConfigStore, mock_client: MagicMock) -> StorageService:
    return StorageService(
        config_store=store,
        client_factory=lambda url, token: mock_client,
    )


class TestDescribeBucketService:
    """Tests for StorageService.describe_bucket()."""

    def test_success(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.set_bucket_metadata.return_value = _META_RESPONSE
        service = _make_service(store, mock_client)

        result = service.describe_bucket(
            alias="prod",
            bucket_id="in.c-sales",
            description="Sales data bucket",
        )

        assert result["project_alias"] == "prod"
        assert result["bucket_id"] == "in.c-sales"
        assert result["description"] == "Sales data bucket"
        assert result["result"] == _META_RESPONSE
        assert "message" in result
        mock_client.set_bucket_metadata.assert_called_once_with(
            bucket_id="in.c-sales",
            entries=[("KBC.description", "Sales data bucket")],
            branch_id=None,
        )
        mock_client.close.assert_called_once()

    def test_with_branch(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.set_bucket_metadata.return_value = _META_RESPONSE
        service = _make_service(store, mock_client)

        service.describe_bucket(
            alias="prod",
            bucket_id="in.c-sales",
            description="desc",
            branch_id=42,
        )

        mock_client.set_bucket_metadata.assert_called_once_with(
            bucket_id="in.c-sales",
            entries=[("KBC.description", "desc")],
            branch_id=42,
        )

    def test_api_error_propagates(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.set_bucket_metadata.side_effect = KeboolaApiError(
            message="Bucket not found",
            status_code=404,
            error_code="BUCKET_NOT_FOUND",
            retryable=False,
        )
        service = _make_service(store, mock_client)

        with pytest.raises(KeboolaApiError, match="Bucket not found"):
            service.describe_bucket(alias="prod", bucket_id="in.c-missing", description="x")

        mock_client.close.assert_called_once()


class TestDescribeTableService:
    """Tests for StorageService.describe_table()."""

    def test_success(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.set_table_metadata.return_value = _META_RESPONSE
        service = _make_service(store, mock_client)

        result = service.describe_table(
            alias="prod",
            table_id="in.c-sales.orders",
            description="All sales orders",
        )

        assert result["project_alias"] == "prod"
        assert result["table_id"] == "in.c-sales.orders"
        assert result["description"] == "All sales orders"
        assert result["result"] == _META_RESPONSE
        mock_client.set_table_metadata.assert_called_once_with(
            table_id="in.c-sales.orders",
            entries=[("KBC.description", "All sales orders")],
            branch_id=None,
        )
        mock_client.close.assert_called_once()

    def test_with_branch(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.set_table_metadata.return_value = _META_RESPONSE
        service = _make_service(store, mock_client)

        service.describe_table(
            alias="prod",
            table_id="in.c-sales.orders",
            description="desc",
            branch_id=99,
        )

        mock_client.set_table_metadata.assert_called_once_with(
            table_id="in.c-sales.orders",
            entries=[("KBC.description", "desc")],
            branch_id=99,
        )

    def test_api_error_propagates(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.set_table_metadata.side_effect = KeboolaApiError(
            message="Table not found",
            status_code=404,
            error_code="TABLE_NOT_FOUND",
            retryable=False,
        )
        service = _make_service(store, mock_client)

        with pytest.raises(KeboolaApiError, match="Table not found"):
            service.describe_table(alias="prod", table_id="in.c-missing.t", description="x")

        mock_client.close.assert_called_once()


def _table_detail(
    columns: list[str] | None = None,
    metadata: list[dict[str, Any]] | None = None,
    column_metadata: dict[str, list[dict[str, Any]]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a Storage API table-detail payload for describe/migrate tests."""
    table: dict[str, Any] = {
        "id": "in.c-sales.orders",
        "name": "orders",
        "displayName": "orders",
        "bucket": {"id": "in.c-sales"},
        "columns": columns if columns is not None else ["c1", "c2"],
        "primaryKey": [],
        "columnMetadata": column_metadata or {},
        "metadata": metadata or [],
    }
    table.update(extra)
    return table


_JOB_OK = {"id": 4242, "status": "success", "operationName": "tableDefinitionUpdate"}


def _legacy_entry(entry_id: int, column: str, value: str) -> dict[str, Any]:
    return {
        "id": entry_id,
        "key": f"KBC.column.{column}.description",
        "value": value,
        "provider": "user",
        "timestamp": "2026-08-21T10:00:00Z",
    }


class TestDescribeColumnsService:
    """Tests for StorageService.describe_columns() (native definition endpoint)."""

    def test_describe_columns_uses_native_endpoint(self, tmp_path: Path) -> None:
        """The write goes through PUT .../definition, never the flat metadata key."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = _table_detail(columns=["c1"])
        mock_client.update_table_definition.return_value = _JOB_OK
        service = _make_service(store, mock_client)

        result = service.describe_columns(
            alias="prod",
            table_id="in.c-sales.orders",
            columns={"c1": "d1"},
        )

        mock_client.update_table_definition.assert_called_once_with(
            table_id="in.c-sales.orders",
            columns=[{"name": "c1", "description": "d1"}],
            is_description_system_managed=False,
            branch_id=None,
        )
        mock_client.set_table_metadata.assert_not_called()
        assert result["project_alias"] == "prod"
        assert result["table_id"] == "in.c-sales.orders"
        assert result["columns"] == {"c1": "d1"}
        assert result["migrated"] == {}
        assert result["skipped"] == []
        assert result["result"] == _JOB_OK
        mock_client.close.assert_called_once()

    def test_describe_columns_with_branch(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = _table_detail(columns=["c1"])
        mock_client.update_table_definition.return_value = _JOB_OK
        service = _make_service(store, mock_client)

        service.describe_columns(
            alias="prod",
            table_id="in.c-sales.orders",
            columns={"c1": "First column"},
            branch_id=77,
        )

        mock_client.get_table_detail.assert_called_once_with("in.c-sales.orders", branch_id=77)
        mock_client.update_table_definition.assert_called_once_with(
            table_id="in.c-sales.orders",
            columns=[{"name": "c1", "description": "First column"}],
            is_description_system_managed=False,
            branch_id=77,
        )

    def test_empty_columns_raises_value_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        with pytest.raises(ValueError, match="At least one column"):
            service.describe_columns(alias="prod", table_id="in.c-sales.orders", columns={})

        mock_client.update_table_definition.assert_not_called()

    def test_describe_columns_unknown_column_fails_fast(self, tmp_path: Path) -> None:
        """Unknown column names abort BEFORE any write (old flat write accepted anything)."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = _table_detail(columns=["c1", "c2"])
        service = _make_service(store, mock_client)

        with pytest.raises(ValueError, match="nope"):
            service.describe_columns(
                alias="prod",
                table_id="in.c-sales.orders",
                columns={"c1": "ok", "nope": "bad"},
            )

        mock_client.update_table_definition.assert_not_called()

    def test_describe_columns_migrates_legacy_sibling(self, tmp_path: Path) -> None:
        """A sibling legacy flat key rides along in the same native write, then is deleted."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = _table_detail(
            columns=["c1", "c2"],
            metadata=[_legacy_entry(7, "c2", "old")],
        )
        mock_client.update_table_definition.return_value = _JOB_OK
        service = _make_service(store, mock_client)

        result = service.describe_columns(
            alias="prod",
            table_id="in.c-sales.orders",
            columns={"c1": "new"},
        )

        payload = mock_client.update_table_definition.call_args.kwargs["columns"]
        assert {"name": "c1", "description": "new"} in payload
        assert {"name": "c2", "description": "old"} in payload
        mock_client.delete_table_metadata.assert_called_once_with(
            "in.c-sales.orders", 7, branch_id=None
        )
        assert result["migrated"] == {"c2": "old"}
        assert result["skipped"] == []

    def test_describe_columns_migration_conflict_skipped(self, tmp_path: Path) -> None:
        """A legacy value that clashes with the visible description is skipped, not deleted."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = _table_detail(
            columns=["c1", "c2"],
            metadata=[_legacy_entry(7, "c2", "old")],
            column_metadata={"c2": [{"key": "KBC.description", "value": "newer"}]},
        )
        mock_client.update_table_definition.return_value = _JOB_OK
        service = _make_service(store, mock_client)

        result = service.describe_columns(
            alias="prod",
            table_id="in.c-sales.orders",
            columns={"c1": "new"},
        )

        payload = mock_client.update_table_definition.call_args.kwargs["columns"]
        assert payload == [{"name": "c1", "description": "new"}]
        mock_client.delete_table_metadata.assert_not_called()
        assert result["migrated"] == {}
        assert result["skipped"] == [
            {"column": "c2", "reason": "conflict", "legacy": "old", "current": "newer"}
        ]

    def test_describe_columns_migration_identical_deletes_only(self, tmp_path: Path) -> None:
        """Identical legacy + visible value: nothing to write, the stale entry still goes."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = _table_detail(
            columns=["c1", "c2"],
            metadata=[_legacy_entry(7, "c2", "same")],
            column_metadata={"c2": [{"key": "KBC.description", "value": "same"}]},
        )
        mock_client.update_table_definition.return_value = _JOB_OK
        service = _make_service(store, mock_client)

        result = service.describe_columns(
            alias="prod",
            table_id="in.c-sales.orders",
            columns={"c1": "new"},
        )

        payload = mock_client.update_table_definition.call_args.kwargs["columns"]
        assert payload == [{"name": "c1", "description": "new"}]
        mock_client.delete_table_metadata.assert_called_once_with(
            "in.c-sales.orders", 7, branch_id=None
        )
        assert result["migrated"] == {}

    def test_describe_columns_orphan_skipped(self, tmp_path: Path) -> None:
        """A legacy key for a dropped column is reported, never deleted implicitly."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = _table_detail(
            columns=["c1"],
            metadata=[_legacy_entry(9, "ghost", "gone")],
        )
        mock_client.update_table_definition.return_value = _JOB_OK
        service = _make_service(store, mock_client)

        result = service.describe_columns(
            alias="prod",
            table_id="in.c-sales.orders",
            columns={"c1": "new"},
        )

        mock_client.delete_table_metadata.assert_not_called()
        assert result["skipped"] == [{"column": "ghost", "reason": "orphan", "legacy": "gone"}]

    def test_describe_columns_user_value_wins_over_legacy(self, tmp_path: Path) -> None:
        """A legacy key on a column the user is describing loses -- and is cleaned up."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = _table_detail(
            columns=["c1"],
            metadata=[_legacy_entry(5, "c1", "old")],
        )
        mock_client.update_table_definition.return_value = _JOB_OK
        service = _make_service(store, mock_client)

        result = service.describe_columns(
            alias="prod",
            table_id="in.c-sales.orders",
            columns={"c1": "new"},
        )

        payload = mock_client.update_table_definition.call_args.kwargs["columns"]
        assert payload == [{"name": "c1", "description": "new"}]
        mock_client.delete_table_metadata.assert_called_once_with(
            "in.c-sales.orders", 5, branch_id=None
        )
        assert result["migrated"] == {}
        assert result["skipped"] == []

    def test_describe_columns_delete_failure_does_not_fail(self, tmp_path: Path) -> None:
        """The native write is durable; a failed legacy cleanup is reported, not raised."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = _table_detail(
            columns=["c1", "c2"],
            metadata=[_legacy_entry(7, "c2", "old")],
        )
        mock_client.update_table_definition.return_value = _JOB_OK
        mock_client.delete_table_metadata.side_effect = KeboolaApiError(
            message="boom",
            status_code=500,
            error_code="SERVER_ERROR",
            retryable=False,
        )
        service = _make_service(store, mock_client)

        result = service.describe_columns(
            alias="prod",
            table_id="in.c-sales.orders",
            columns={"c1": "new"},
        )

        assert result["result"] == _JOB_OK
        assert result["migrated"] == {"c2": "old"}
        assert [s["reason"] for s in result["skipped"]] == ["delete_failed"]
        assert result["skipped"][0]["column"] == "c2"


class TestDescribeBatchService:
    """Tests for StorageService.describe_batch()."""

    def test_success_all_sections(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.set_bucket_metadata.return_value = []
        mock_client.set_table_metadata.return_value = []
        mock_client.get_table_detail.return_value = _table_detail(columns=["order_id"])
        mock_client.update_table_definition.return_value = _JOB_OK
        service = _make_service(store, mock_client)

        batch_file = tmp_path / "batch.yaml"
        batch_file.write_text(
            "buckets:\n"
            "  in.c-sales: Sales data\n"
            "tables:\n"
            "  in.c-sales.orders: Order data\n"
            "columns:\n"
            "  in.c-sales.orders:\n"
            "    order_id: Unique order ID\n",
            encoding="utf-8",
        )

        result = service.describe_batch(alias="prod", from_file=batch_file)

        assert result["project_alias"] == "prod"
        assert len(result["applied"]) == 3
        assert result["errors"] == []
        applied_types = [a["type"] for a in result["applied"]]
        assert "bucket" in applied_types
        assert "table" in applied_types
        assert "columns" in applied_types
        # Bucket metadata called once (for the bucket); the table description
        # still goes through table metadata, column descriptions now go through
        # the native definition endpoint.
        assert mock_client.set_bucket_metadata.call_count == 1
        assert mock_client.set_table_metadata.call_count == 1
        assert mock_client.update_table_definition.call_count == 1

    def test_file_not_found(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        with pytest.raises(ValueError, match="Batch file not found"):
            service.describe_batch(alias="prod", from_file=tmp_path / "missing.yaml")

    def test_partial_errors_collected(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.set_bucket_metadata.side_effect = [
            [],
            KeboolaApiError(
                message="Bucket not found",
                status_code=404,
                error_code="NOT_FOUND",
                retryable=False,
            ),
        ]
        service = _make_service(store, mock_client)

        batch_file = tmp_path / "partial.yaml"
        batch_file.write_text(
            "buckets:\n  in.c-good: Good bucket\n  in.c-bad: Bad bucket\n",
            encoding="utf-8",
        )

        result = service.describe_batch(alias="prod", from_file=batch_file)

        assert len(result["applied"]) == 1
        assert len(result["errors"]) == 1
        assert result["applied"][0]["id"] == "in.c-good"
        assert result["errors"][0]["id"] == "in.c-bad"
        assert "Bucket not found" in result["errors"][0]["error"]

    def test_empty_yaml(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        batch_file = tmp_path / "empty.yaml"
        batch_file.write_text("", encoding="utf-8")

        result = service.describe_batch(alias="prod", from_file=batch_file)

        assert result["applied"] == []
        assert result["errors"] == []
        mock_client.set_bucket_metadata.assert_not_called()
        mock_client.set_table_metadata.assert_not_called()

    def test_invalid_yaml_not_mapping(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        batch_file = tmp_path / "invalid.yaml"
        batch_file.write_text("- item1\n- item2\n", encoding="utf-8")

        with pytest.raises(ValueError, match="must be a YAML mapping"):
            service.describe_batch(alias="prod", from_file=batch_file)


class TestDescribeBatchShapeValidation:
    """Malformed `--from-file` documents are rejected before any write (#640).

    Each case used to reach the write loop and die on ``.items()`` with an
    ``AttributeError`` -- a traceback even under ``--json``. They must now
    raise ``ValueError`` (mapped to INVALID_ARGUMENT / exit 2 by the command),
    name the offending key and its actual type, and touch no API.
    """

    def _service(self, tmp_path: Path) -> tuple[StorageService, MagicMock]:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        return _make_service(store, mock_client), mock_client

    def _run(self, tmp_path: Path, yaml_text: str) -> tuple[str, MagicMock]:
        """Run describe_batch on ``yaml_text``, returning the message + client."""
        service, mock_client = self._service(tmp_path)
        batch_file = tmp_path / "batch.yaml"
        batch_file.write_text(yaml_text, encoding="utf-8")
        with pytest.raises(ValueError) as excinfo:
            service.describe_batch(alias="prod", from_file=batch_file)
        return str(excinfo.value), mock_client

    def _assert_no_writes(self, mock_client: MagicMock) -> None:
        mock_client.set_bucket_metadata.assert_not_called()
        mock_client.set_table_metadata.assert_not_called()
        mock_client.update_table_definition.assert_not_called()

    def test_tables_as_list(self, tmp_path: Path) -> None:
        """The exact repro from issue #640: `tables:` is a list of objects."""
        message, mock_client = self._run(
            tmp_path,
            "tables:\n  - table_id: in.c-test.part_verify\n    columns:\n      id: Surrogate key\n",
        )

        assert "'tables' must be a mapping of table ID to description" in message
        assert "got a list" in message
        # The message carries a copy-pasteable example of the right shape.
        assert "in.c-sales.orders: All sales orders" in message
        self._assert_no_writes(mock_client)

    def test_buckets_as_list(self, tmp_path: Path) -> None:
        message, mock_client = self._run(tmp_path, "buckets:\n  - in.c-sales\n")

        assert "'buckets' must be a mapping of bucket ID to description" in message
        assert "got a list" in message
        self._assert_no_writes(mock_client)

    def test_columns_as_list(self, tmp_path: Path) -> None:
        message, mock_client = self._run(tmp_path, "columns:\n  - in.c-sales.orders\n")

        assert "'columns' must be a mapping of table ID to a column mapping" in message
        assert "got a list" in message
        self._assert_no_writes(mock_client)

    def test_columns_entry_scalar(self, tmp_path: Path) -> None:
        message, mock_client = self._run(
            tmp_path, "columns:\n  in.c-sales.orders: Unique order ID\n"
        )

        assert "'columns.in.c-sales.orders' must be a mapping of column name to description" in (
            message
        )
        assert "got a string" in message
        self._assert_no_writes(mock_client)

    def test_columns_entry_list(self, tmp_path: Path) -> None:
        message, mock_client = self._run(
            tmp_path, "columns:\n  in.c-sales.orders:\n    - order_id\n"
        )

        assert "'columns.in.c-sales.orders' must be" in message
        assert "got a list" in message
        self._assert_no_writes(mock_client)

    def test_column_description_is_a_container(self, tmp_path: Path) -> None:
        """A nested mapping under a column name would be str()'d into garbage."""
        message, mock_client = self._run(
            tmp_path,
            "columns:\n  in.c-sales.orders:\n    order_id:\n      text: Unique order ID\n",
        )

        assert "'columns.in.c-sales.orders.order_id' must be a description string" in message
        assert "got a mapping" in message
        self._assert_no_writes(mock_client)

    def test_table_description_is_a_container(self, tmp_path: Path) -> None:
        message, mock_client = self._run(
            tmp_path, "tables:\n  in.c-sales.orders:\n    - All sales orders\n"
        )

        assert "'tables.in.c-sales.orders' must be a description string" in message
        assert "got a list" in message
        self._assert_no_writes(mock_client)

    def test_top_level_scalar(self, tmp_path: Path) -> None:
        message, mock_client = self._run(tmp_path, "just a string\n")

        assert "must be a YAML mapping of 'buckets' / 'tables' / 'columns' sections" in message
        assert "got a string" in message
        self._assert_no_writes(mock_client)

    def test_malformed_yaml_syntax(self, tmp_path: Path) -> None:
        message, mock_client = self._run(tmp_path, "tables:\n  - [unclosed\n")

        assert "Batch file is not valid YAML" in message
        self._assert_no_writes(mock_client)

    def test_nothing_is_applied_when_a_later_section_is_malformed(self, tmp_path: Path) -> None:
        """Validation is up front: a bad `columns:` must not let buckets through."""
        message, mock_client = self._run(
            tmp_path,
            "buckets:\n  in.c-sales: Sales data\ncolumns:\n  - in.c-sales.orders\n",
        )

        assert "'columns' must be" in message
        self._assert_no_writes(mock_client)

    def test_valid_file_still_applies(self, tmp_path: Path) -> None:
        """Happy-path regression: the documented schema is untouched."""
        service, mock_client = self._service(tmp_path)
        mock_client.set_bucket_metadata.return_value = []
        mock_client.set_table_metadata.return_value = []
        mock_client.get_table_detail.return_value = _table_detail(columns=["order_id"])
        mock_client.update_table_definition.return_value = _JOB_OK

        batch_file = tmp_path / "good.yaml"
        batch_file.write_text(
            "buckets:\n"
            "  in.c-sales: Sales data\n"
            "tables:\n"
            "  in.c-sales.orders: 2026\n"
            "columns:\n"
            "  in.c-sales.orders:\n"
            "    order_id: Unique order ID\n",
            encoding="utf-8",
        )

        result = service.describe_batch(alias="prod", from_file=batch_file)

        assert result["error_count"] == 0
        assert result["applied_count"] == 3
        # Non-string scalars stay coerced to str, as before the validation pass.
        table_applied = next(a for a in result["applied"] if a["type"] == "table")
        assert table_applied["description"] == "2026"


def _token_info(project_id: int = 258) -> TokenVerifyResponse:
    return TokenVerifyResponse(
        token_id="12345",
        token_description="Test Token",
        project_id=project_id,
        project_name="Production",
        owner_name="Production",
    )


class TestGetBucketDetailDescriptionExtraction:
    """Verify get_bucket_detail extracts description from metadata and exposes raw metadata."""

    def test_extracts_description_from_kbc_description_metadata(self, tmp_path: Path) -> None:
        """KBC.description (provider=user) in the metadata array is exposed as 'description'."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.verify_token.return_value = _token_info()
        mock_client.get_bucket_detail.return_value = {
            "id": "in.c-sales",
            "displayName": "sales",
            "stage": "in",
            "description": "",  # native field empty
            "backend": "snowflake",
            "backendPath": ["SAPI_258", "in.c-sales"],
            "metadata": [
                {
                    "id": "9001",
                    "key": "KBC.description",
                    "value": "Revenue numbers",
                    "provider": "user",
                    "timestamp": "2026-04-22T10:00:00Z",
                },
                {
                    "id": "9002",
                    "key": "KBC.createdBy.component.id",
                    "value": "keboola.orchestrator",
                    "provider": "system",
                    "timestamp": "2026-04-22T10:00:00Z",
                },
            ],
            "tables": [],
        }
        service = _make_service(store, mock_client)

        result = service.get_bucket_detail(alias="prod", bucket_id="in.c-sales")

        assert result["description"] == "Revenue numbers"
        # raw_metadata must surface as the 'metadata' field
        assert isinstance(result["metadata"], list)
        assert len(result["metadata"]) == 2
        assert result["metadata"][0]["key"] == "KBC.description"

    def test_metadata_description_wins_over_native_description(self, tmp_path: Path) -> None:
        """Precedence: KBC.description metadata entry overrides the native 'description' field.

        This pins current behavior: when both are present the metadata entry wins, because
        the native field is only settable at bucket-create time via the Storage API; any
        user-visible description updates flow through the metadata endpoint.
        """
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.verify_token.return_value = _token_info()
        mock_client.get_bucket_detail.return_value = {
            "id": "in.c-sales",
            "displayName": "sales",
            "stage": "in",
            "description": "Legacy native description",
            "backend": "snowflake",
            "backendPath": ["SAPI_258", "in.c-sales"],
            "metadata": [
                {
                    "id": "9001",
                    "key": "KBC.description",
                    "value": "New metadata description",
                    "provider": "user",
                    "timestamp": "2026-04-22T10:00:00Z",
                }
            ],
            "tables": [],
        }
        service = _make_service(store, mock_client)

        result = service.get_bucket_detail(alias="prod", bucket_id="in.c-sales")

        assert result["description"] == "New metadata description"

    def test_falls_back_to_native_description_when_no_metadata(self, tmp_path: Path) -> None:
        """With no KBC.description in metadata, the native 'description' field is used."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.verify_token.return_value = _token_info()
        mock_client.get_bucket_detail.return_value = {
            "id": "in.c-sales",
            "displayName": "sales",
            "stage": "in",
            "description": "Only native",
            "backend": "snowflake",
            "backendPath": ["SAPI_258", "in.c-sales"],
            "metadata": [],
            "tables": [],
        }
        service = _make_service(store, mock_client)

        result = service.get_bucket_detail(alias="prod", bucket_id="in.c-sales")

        assert result["description"] == "Only native"
        assert result["metadata"] == []

    def test_ignores_non_user_provider_kbc_description(self, tmp_path: Path) -> None:
        """A KBC.description entry with provider != 'user' must not be picked up."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.verify_token.return_value = _token_info()
        mock_client.get_bucket_detail.return_value = {
            "id": "in.c-sales",
            "displayName": "sales",
            "stage": "in",
            "description": "Native wins here",
            "backend": "snowflake",
            "backendPath": ["SAPI_258", "in.c-sales"],
            "metadata": [
                {
                    "id": "9001",
                    "key": "KBC.description",
                    "value": "System-set",
                    "provider": "system",
                    "timestamp": "2026-04-22T10:00:00Z",
                }
            ],
            "tables": [],
        }
        service = _make_service(store, mock_client)

        result = service.get_bucket_detail(alias="prod", bucket_id="in.c-sales")

        assert result["description"] == "Native wins here"


class TestGetBucketDetailBackendPaths:
    """Verify dialect-aware path quoting in get_bucket_detail (Snowflake vs BigQuery).

    Background: prior to 0.25.3, ``get_bucket_detail`` always emitted
    ``snowflake_path`` with double-quoted identifiers regardless of backend,
    which is a syntax error on BigQuery. Now we surface backend-native keys
    (``snowflake_*`` for Snowflake, ``bigquery_*`` for BigQuery) plus
    backend-agnostic ``sql_dialect`` + per-table ``sql_path``.
    """

    def test_snowflake_uses_double_quotes_and_keeps_legacy_keys(self, tmp_path: Path) -> None:
        """Snowflake keeps ``snowflake_database`` / ``snowflake_schema`` /
        per-table ``snowflake_path`` (BC), and adds ``sql_dialect`` / ``sql_path``."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.verify_token.return_value = _token_info()
        mock_client.get_bucket_detail.return_value = {
            "id": "in.c-sales",
            "displayName": "sales",
            "stage": "in",
            "description": "",
            "backend": "snowflake",
            "backendPath": ["SAPI_258", "in.c-sales"],
            "metadata": [],
            "tables": [
                {
                    "id": "in.c-sales.orders",
                    "name": "orders",
                    "displayName": "orders",
                    "isAlias": False,
                }
            ],
        }
        service = _make_service(store, mock_client)

        result = service.get_bucket_detail(alias="prod", bucket_id="in.c-sales")

        assert result["sql_dialect"] == "snowflake"
        assert result["snowflake_database"] == "SAPI_258"
        assert result["snowflake_schema"] == "in.c-sales"
        assert "bigquery_dataset" not in result
        assert "bigquery_project" not in result

        table = result["tables"][0]
        expected_sf = '"SAPI_258"."in.c-sales"."orders"'
        assert table["snowflake_path"] == expected_sf
        assert table["sql_path"] == expected_sf
        assert "bigquery_path" not in table

    def test_bigquery_uses_backticks_and_omits_snowflake_keys(self, tmp_path: Path) -> None:
        """BigQuery emits ``bigquery_dataset`` and backtick-quoted ``bigquery_path``;
        misleading ``snowflake_*`` keys are NOT included."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.verify_token.return_value = _token_info(project_id=9621)
        mock_client.get_bucket_detail.return_value = {
            "id": "in.c-test-bucket",
            "displayName": "test-bucket",
            "stage": "in",
            "description": "",
            "backend": "bigquery",
            "backendPath": ["in_c_test_bucket"],
            "databaseName": "",
            "metadata": [],
            "tables": [
                {
                    "id": "in.c-test-bucket.test-bigquery-table",
                    "name": "test-bigquery-table",
                    "displayName": "test-bigquery-table",
                    "isAlias": False,
                }
            ],
        }
        service = _make_service(store, mock_client)

        result = service.get_bucket_detail(alias="prod", bucket_id="in.c-test-bucket")

        assert result["sql_dialect"] == "bigquery"
        assert result["bigquery_dataset"] == "in_c_test_bucket"
        assert result["bigquery_project"] == ""
        # Snowflake-only keys must NOT leak onto BigQuery results -- they are
        # syntactically wrong and historically misled callers.
        assert "snowflake_database" not in result
        assert "snowflake_schema" not in result

        table = result["tables"][0]
        # No GCP project from API -> dataset-qualified path only.
        expected_bq = "`in_c_test_bucket`.`test-bigquery-table`"
        assert table["bigquery_path"] == expected_bq
        assert table["sql_path"] == expected_bq
        assert "snowflake_path" not in table

    def test_bigquery_with_database_name_emits_full_fqn(self, tmp_path: Path) -> None:
        """When the API surfaces ``databaseName`` (GCP project), BigQuery paths
        include all three components: ``project.dataset.table``."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.verify_token.return_value = _token_info(project_id=9621)
        mock_client.get_bucket_detail.return_value = {
            "id": "in.c-foo",
            "displayName": "foo",
            "stage": "in",
            "description": "",
            "backend": "bigquery",
            "backendPath": ["dataset_foo"],
            "databaseName": "kbc-bq-9621",
            "metadata": [],
            "tables": [
                {
                    "id": "in.c-foo.bar",
                    "name": "bar",
                    "displayName": "bar",
                    "isAlias": False,
                }
            ],
        }
        service = _make_service(store, mock_client)

        result = service.get_bucket_detail(alias="prod", bucket_id="in.c-foo")

        assert result["bigquery_project"] == "kbc-bq-9621"
        assert result["bigquery_dataset"] == "dataset_foo"
        expected = "`kbc-bq-9621`.`dataset_foo`.`bar`"
        assert result["tables"][0]["bigquery_path"] == expected
        assert result["tables"][0]["sql_path"] == expected

    def test_snowflake_linked_bucket_uses_source_backend_path_when_present(
        self, tmp_path: Path
    ) -> None:
        """Linked Snowflake bucket: backendPath from the response wins over the
        sourceBucket fallback (matches pre-0.25.3 behaviour)."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.verify_token.return_value = _token_info()
        mock_client.get_bucket_detail.return_value = {
            "id": "in.c-linked",
            "displayName": "linked",
            "stage": "in",
            "description": "",
            "backend": "snowflake",
            "backendPath": ["SAPI_999", "in.c-source"],
            "sourceBucket": {
                "id": "in.c-source",
                "project": {"id": 999, "name": "Source Project"},
            },
            "metadata": [],
            "tables": [{"id": "in.c-source.t", "name": "t", "displayName": "t", "isAlias": True}],
        }
        service = _make_service(store, mock_client)

        result = service.get_bucket_detail(alias="prod", bucket_id="in.c-linked")

        assert result["is_linked"] is True
        assert result["snowflake_database"] == "SAPI_999"
        assert result["snowflake_schema"] == "in.c-source"
        assert result["tables"][0]["snowflake_path"] == '"SAPI_999"."in.c-source"."t"'
        assert result["tables"][0]["sql_path"] == '"SAPI_999"."in.c-source"."t"'


class TestGetTableDetailDescriptionExtraction:
    """Verify get_table_detail extracts table + per-column descriptions from metadata."""

    def test_extracts_table_and_column_descriptions(self, tmp_path: Path) -> None:
        """KBC.description + KBC.column.{name}.description are surfaced on the response."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = {
            "id": "in.c-sales.orders",
            "name": "orders",
            "displayName": "orders",
            "bucket": {"id": "in.c-sales"},
            "columns": ["order_id", "total"],
            "primaryKey": ["order_id"],
            "rowsCount": 42,
            "columnMetadata": {},
            "metadata": [
                {
                    "id": "1",
                    "key": "KBC.description",
                    "value": "Sales orders fact table",
                    "provider": "user",
                    "timestamp": "2026-04-22T10:00:00Z",
                },
                {
                    "id": "2",
                    "key": "KBC.column.order_id.description",
                    "value": "Unique order identifier",
                    "provider": "user",
                    "timestamp": "2026-04-22T10:00:00Z",
                },
                {
                    "id": "3",
                    "key": "KBC.column.total.description",
                    "value": "Order total in USD",
                    "provider": "user",
                    "timestamp": "2026-04-22T10:00:00Z",
                },
            ],
        }
        service = _make_service(store, mock_client)

        result = service.get_table_detail(alias="prod", table_id="in.c-sales.orders")

        assert result["description"] == "Sales orders fact table"

        # column_details must be a list of dicts, one per column, with descriptions
        col_map = {c["name"]: c for c in result["column_details"]}
        assert col_map["order_id"]["description"] == "Unique order identifier"
        assert col_map["total"]["description"] == "Order total in USD"

        # raw_metadata must be exposed as the 'metadata' field
        assert isinstance(result["metadata"], list)
        assert len(result["metadata"]) == 3

    def test_surfaces_bucket_backend(self, tmp_path: Path) -> None:
        """The owning bucket's storage backend is exposed on the response.

        Consumed by semantic-layer build to pick the INFORMATION_SCHEMA
        dialect when resolving column types for alias / linked tables.
        """
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = {
            "id": "in.c-sales.orders",
            "name": "orders",
            "displayName": "orders",
            "bucket": {"id": "in.c-sales", "backend": "bigquery"},
            "columns": ["order_id"],
            "columnMetadata": {},
            "metadata": [],
        }
        service = _make_service(store, mock_client)

        result = service.get_table_detail(alias="prod", table_id="in.c-sales.orders")

        assert result["backend"] == "bigquery"

    def test_columns_without_description_have_no_description_key(self, tmp_path: Path) -> None:
        """Columns without a matching KBC.column.{name}.description entry omit 'description'."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = {
            "id": "in.c-sales.orders",
            "name": "orders",
            "displayName": "orders",
            "bucket": {"id": "in.c-sales"},
            "columns": ["order_id", "total"],
            "primaryKey": [],
            "rowsCount": 0,
            "columnMetadata": {},
            "metadata": [
                {
                    "id": "1",
                    "key": "KBC.column.order_id.description",
                    "value": "Unique order id",
                    "provider": "user",
                    "timestamp": "2026-04-22T10:00:00Z",
                }
            ],
        }
        service = _make_service(store, mock_client)

        result = service.get_table_detail(alias="prod", table_id="in.c-sales.orders")

        col_map = {c["name"]: c for c in result["column_details"]}
        assert col_map["order_id"]["description"] == "Unique order id"
        assert "description" not in col_map["total"]
        # Table-level description absent when no KBC.description entry
        assert result["description"] == ""

    def test_null_numeric_fields_coerced_to_zero(self, tmp_path: Path) -> None:
        """Companion to issue #233: API may return null for rowsCount /
        dataSizeBytes on empty tables. get_table_detail must surface 0,
        not null, to keep the JSON output well-typed.
        """
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = {
            "id": "in.c-empty.t",
            "name": "t",
            "displayName": "t",
            "bucket": {"id": "in.c-empty"},
            "columns": ["a"],
            "primaryKey": [],
            "rowsCount": None,
            "dataSizeBytes": None,
            "columnMetadata": {},
            "metadata": [],
        }
        service = _make_service(store, mock_client)

        result = service.get_table_detail(alias="prod", table_id="in.c-empty.t")

        assert result["rows_count"] == 0
        assert result["data_size_bytes"] == 0

    def test_backend_surfaced_from_bucket(self, tmp_path: Path) -> None:
        """The owning bucket's backend is exposed so the web UI can gate
        BigQuery-only features (repartition) on it."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = {
            "id": "in.c-sales.orders",
            "name": "orders",
            "displayName": "orders",
            "bucket": {"id": "in.c-sales", "backend": "bigquery"},
            "columns": ["a"],
            "primaryKey": [],
            "columnMetadata": {},
            "metadata": [],
        }
        service = _make_service(store, mock_client)

        result = service.get_table_detail(alias="prod", table_id="in.c-sales.orders")

        assert result["backend"] == "bigquery"

    def test_backend_defaults_to_empty_when_absent(self, tmp_path: Path) -> None:
        """A bucket object without a backend key yields an empty string, not a
        KeyError -- the UI simply hides the BigQuery-only tab."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = {
            "id": "in.c-sales.orders",
            "name": "orders",
            "displayName": "orders",
            "bucket": {"id": "in.c-sales"},
            "columns": ["a"],
            "primaryKey": [],
            "columnMetadata": {},
            "metadata": [],
        }
        service = _make_service(store, mock_client)

        result = service.get_table_detail(alias="prod", table_id="in.c-sales.orders")

        assert result["backend"] == ""


def _listing_row(table_id: str, metadata: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Row as returned by ``list_tables(include="metadata")``."""
    return {"id": table_id, "name": table_id.split(".")[-1], "metadata": metadata or []}


class TestDescribeMigrateService:
    """Tests for StorageService.describe_migrate() (bulk legacy conversion)."""

    def _client(
        self, listing: list[dict[str, Any]], details: dict[str, dict[str, Any]]
    ) -> MagicMock:
        mock_client = MagicMock()
        mock_client.list_tables.return_value = listing
        mock_client.get_table_detail.side_effect = lambda tid, branch_id=None: details[tid]
        mock_client.update_table_definition.return_value = _JOB_OK
        return mock_client

    def test_describe_migrate_dry_run_reports_no_writes(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        legacy = [_legacy_entry(7, "c2", "old")]
        mock_client = self._client(
            [_listing_row("in.c-x.t1", legacy), _listing_row("in.c-x.t2")],
            {
                "in.c-x.t1": _table_detail(columns=["c1", "c2"], metadata=legacy),
                "in.c-x.t2": _table_detail(columns=["c1"]),
            },
        )
        service = _make_service(store, mock_client)

        result = service.describe_migrate(alias="prod", dry_run=True)

        assert result["dry_run"] is True
        assert result["tables_scanned"] == 2
        assert result["tables_migrated"] == 0
        assert result["migrated"] == [{"table_id": "in.c-x.t1", "columns": {"c2": "old"}}]
        mock_client.update_table_definition.assert_not_called()
        mock_client.delete_table_metadata.assert_not_called()

    def test_describe_migrate_applies_and_deletes(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        legacy = [_legacy_entry(7, "c2", "old")]
        mock_client = self._client(
            [_listing_row("in.c-x.t1", legacy), _listing_row("in.c-x.t2")],
            {
                "in.c-x.t1": _table_detail(columns=["c1", "c2"], metadata=legacy),
                "in.c-x.t2": _table_detail(columns=["c1"]),
            },
        )
        service = _make_service(store, mock_client)

        result = service.describe_migrate(alias="prod")

        mock_client.update_table_definition.assert_called_once_with(
            table_id="in.c-x.t1",
            columns=[{"name": "c2", "description": "old"}],
            is_description_system_managed=False,
            branch_id=None,
        )
        mock_client.delete_table_metadata.assert_called_once_with("in.c-x.t1", 7, branch_id=None)
        assert result["tables_migrated"] == 1
        assert result["errors"] == []

    def test_describe_migrate_scope_bucket(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        legacy = [_legacy_entry(7, "c2", "old")]
        mock_client = self._client(
            [_listing_row("in.c-x.t1", legacy), _listing_row("in.c-y.t9", legacy)],
            {
                "in.c-x.t1": _table_detail(columns=["c1", "c2"], metadata=legacy),
                "in.c-y.t9": _table_detail(columns=["c1", "c2"], metadata=legacy),
            },
        )
        service = _make_service(store, mock_client)

        result = service.describe_migrate(alias="prod", bucket_id="in.c-x", dry_run=True)

        assert result["tables_scanned"] == 1
        assert [m["table_id"] for m in result["migrated"]] == ["in.c-x.t1"]

    def test_describe_migrate_scope_table_ids(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        legacy = [_legacy_entry(7, "c2", "old")]
        mock_client = self._client(
            [], {"in.c-x.t1": _table_detail(columns=["c1", "c2"], metadata=legacy)}
        )
        service = _make_service(store, mock_client)

        result = service.describe_migrate(alias="prod", table_ids=["in.c-x.t1"], dry_run=True)

        mock_client.list_tables.assert_not_called()
        assert result["tables_scanned"] == 1
        assert result["migrated"] == [{"table_id": "in.c-x.t1", "columns": {"c2": "old"}}]

    def test_describe_migrate_both_scopes_raises(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        with pytest.raises(ValueError, match="mutually exclusive"):
            service.describe_migrate(alias="prod", table_ids=["in.c-x.t1"], bucket_id="in.c-x")

    def test_describe_migrate_prune_orphans(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        legacy = [_legacy_entry(11, "ghost", "gone")]
        details = {"in.c-x.t1": _table_detail(columns=["c1"], metadata=legacy)}

        store_client = self._client([_listing_row("in.c-x.t1", legacy)], details)
        service = _make_service(store, store_client)
        result = service.describe_migrate(alias="prod")
        store_client.delete_table_metadata.assert_not_called()
        assert result["skipped"] == [
            {"table_id": "in.c-x.t1", "column": "ghost", "reason": "orphan", "legacy": "gone"}
        ]
        assert result["pruned_orphans"] == []

        prune_client = self._client([_listing_row("in.c-x.t1", legacy)], details)
        service = _make_service(store, prune_client)
        result = service.describe_migrate(alias="prod", prune_orphans=True)
        prune_client.delete_table_metadata.assert_called_once_with("in.c-x.t1", 11, branch_id=None)
        assert result["pruned_orphans"] == [{"table_id": "in.c-x.t1", "column": "ghost"}]

    def test_describe_migrate_error_accumulation(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        legacy = [_legacy_entry(7, "c2", "old")]
        mock_client = self._client(
            [_listing_row("in.c-x.a", legacy), _listing_row("in.c-x.b", legacy)],
            {
                "in.c-x.a": _table_detail(columns=["c1", "c2"], metadata=legacy),
                "in.c-x.b": _table_detail(columns=["c1", "c2"], metadata=legacy),
            },
        )
        mock_client.update_table_definition.side_effect = [
            KeboolaApiError(
                message="boom", status_code=500, error_code="SERVER_ERROR", retryable=False
            ),
            _JOB_OK,
        ]
        service = _make_service(store, mock_client)

        result = service.describe_migrate(alias="prod")

        assert result["tables_migrated"] == 1
        assert len(result["errors"]) == 1
        assert result["errors"][0]["table_id"] == "in.c-x.a"
        assert "boom" in result["errors"][0]["error"]
        assert [m["table_id"] for m in result["migrated"]] == ["in.c-x.b"]

    def test_describe_migrate_progress_callback(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        legacy = [_legacy_entry(7, "c2", "old")]
        mock_client = self._client(
            [_listing_row("in.c-x.t1", legacy), _listing_row("in.c-x.t2")],
            {
                "in.c-x.t1": _table_detail(columns=["c1", "c2"], metadata=legacy),
                "in.c-x.t2": _table_detail(columns=["c1"]),
            },
        )
        service = _make_service(store, mock_client)
        seen: list[tuple[str, int, int]] = []

        service.describe_migrate(
            alias="prod",
            dry_run=True,
            progress_callback=lambda tid, cur, total: seen.append((tid, cur, total)),
        )

        assert seen == [("in.c-x.t1", 1, 2), ("in.c-x.t2", 2, 2)]


class TestGetTableDetailDescriptionPrecedence:
    """Read path: native definition -> columnMetadata KBC.description -> legacy flat key."""

    def test_table_detail_native_definition_wins(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = _table_detail(
            columns=["c1"],
            metadata=[_legacy_entry(1, "c1", "legacy")],
            column_metadata={"c1": [{"key": "KBC.description", "value": "meta"}]},
            definition={"columns": [{"name": "c1", "definition": {"description": "native"}}]},
        )
        service = _make_service(store, mock_client)

        result = service.get_table_detail(alias="prod", table_id="in.c-sales.orders")

        col_map = {c["name"]: c for c in result["column_details"]}
        assert col_map["c1"]["description"] == "native"
        # The stale flat key is still present on the table -> flagged for migration.
        assert result["legacy_column_descriptions"] == ["c1"]

    def test_table_detail_column_metadata_fallback(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = _table_detail(
            columns=["c1"],
            column_metadata={"c1": [{"key": "KBC.description", "value": "meta"}]},
        )
        service = _make_service(store, mock_client)

        result = service.get_table_detail(alias="prod", table_id="in.c-sales.orders")

        col_map = {c["name"]: c for c in result["column_details"]}
        assert col_map["c1"]["description"] == "meta"
        assert result["legacy_column_descriptions"] == []

    def test_table_detail_legacy_fallback_and_warning_key(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = _table_detail(
            columns=["c1"],
            metadata=[_legacy_entry(1, "c1", "legacy only")],
        )
        service = _make_service(store, mock_client)

        result = service.get_table_detail(alias="prod", table_id="in.c-sales.orders")

        col_map = {c["name"]: c for c in result["column_details"]}
        assert col_map["c1"]["description"] == "legacy only"
        assert result["legacy_column_descriptions"] == ["c1"]

    def test_table_detail_alias_source_metadata(self, tmp_path: Path) -> None:
        """Alias tables inherit the source table's columnMetadata (MCP-server parity)."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = _table_detail(
            columns=["c1"],
            isAlias=True,
            sourceTable={
                "id": "in.c-src.orders",
                "columnMetadata": {"c1": [{"key": "KBC.description", "value": "from source"}]},
            },
        )
        service = _make_service(store, mock_client)

        result = service.get_table_detail(alias="prod", table_id="in.c-sales.orders")

        col_map = {c["name"]: c for c in result["column_details"]}
        assert col_map["c1"]["description"] == "from source"
        assert result["legacy_column_descriptions"] == []

    def test_table_detail_alias_source_definition(self, tmp_path: Path) -> None:
        """Alias tables without their own definition read the source table's."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = _table_detail(
            columns=["c1"],
            isAlias=True,
            sourceTable={
                "id": "in.c-src.orders",
                "definition": {
                    "columns": [{"name": "c1", "definition": {"description": "src native"}}]
                },
            },
        )
        service = _make_service(store, mock_client)

        result = service.get_table_detail(alias="prod", table_id="in.c-sales.orders")

        col_map = {c["name"]: c for c in result["column_details"]}
        assert col_map["c1"]["description"] == "src native"

    def test_table_detail_no_descriptions_empty_legacy_list(self, tmp_path: Path) -> None:
        """The key is always present so callers never need a .get() guard."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = _table_detail(columns=["c1"])
        service = _make_service(store, mock_client)

        result = service.get_table_detail(alias="prod", table_id="in.c-sales.orders")

        assert result["legacy_column_descriptions"] == []
        assert "description" not in result["column_details"][0]
