"""Tests for StorageService describe_* methods (bucket, table, columns, batch).

Also covers the read-back side: extraction of ``KBC.description`` and
``KBC.column.{name}.description`` metadata keys in ``get_bucket_detail`` /
``get_table_detail``, including the precedence between the native API
``description`` field and the ``KBC.description`` metadata entry.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.models import AppConfig, ProjectConfig, TokenVerifyResponse
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
