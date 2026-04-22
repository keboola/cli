"""Tests for StorageService describe_* methods (bucket, table, columns, batch)."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.models import AppConfig, ProjectConfig
from keboola_agent_cli.services.storage_service import StorageService

TEST_TOKEN = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"

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


class TestDescribeColumnsService:
    """Tests for StorageService.describe_columns()."""

    def test_success_namespaced_keys(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.set_table_metadata.return_value = []
        service = _make_service(store, mock_client)

        result = service.describe_columns(
            alias="prod",
            table_id="in.c-sales.orders",
            columns={"order_id": "Unique order identifier", "total": "Order total in USD"},
        )

        assert result["project_alias"] == "prod"
        assert result["table_id"] == "in.c-sales.orders"
        assert result["columns"]["order_id"] == "Unique order identifier"
        assert result["columns"]["total"] == "Order total in USD"
        mock_client.set_table_metadata.assert_called_once_with(
            table_id="in.c-sales.orders",
            entries=[
                ("KBC.column.order_id.description", "Unique order identifier"),
                ("KBC.column.total.description", "Order total in USD"),
            ],
            branch_id=None,
        )
        mock_client.close.assert_called_once()

    def test_empty_columns_raises_value_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        with pytest.raises(ValueError, match="At least one column"):
            service.describe_columns(alias="prod", table_id="in.c-sales.orders", columns={})

        mock_client.set_table_metadata.assert_not_called()

    def test_with_branch(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.set_table_metadata.return_value = []
        service = _make_service(store, mock_client)

        service.describe_columns(
            alias="prod",
            table_id="in.c-sales.orders",
            columns={"col1": "First column"},
            branch_id=77,
        )

        mock_client.set_table_metadata.assert_called_once_with(
            table_id="in.c-sales.orders",
            entries=[("KBC.column.col1.description", "First column")],
            branch_id=77,
        )


class TestDescribeBatchService:
    """Tests for StorageService.describe_batch()."""

    def test_success_all_sections(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.set_bucket_metadata.return_value = []
        mock_client.set_table_metadata.return_value = []
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
        # Bucket metadata called once (for the bucket), table metadata called twice
        # (once for table description, once for column descriptions)
        assert mock_client.set_bucket_metadata.call_count == 1
        assert mock_client.set_table_metadata.call_count == 2

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
