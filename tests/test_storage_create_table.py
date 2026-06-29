"""Tests for storage create-table: source copy + BigQuery partition/clustering.

Covers the create-table-from-source / repartition capability added to mirror
keboola/connection#7697:
- Client: conditional request body (source vs columns, partition/clustering).
- Service: columns/source XOR validation, partition-flag validation, the
  BigQuery pre-flight backend guard, and the result envelope.
- CLI: new flags reach the service; --column is no longer required.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.config_store import ConfigStore
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
            ),
        },
    )
    store.save(config)
    return store


def _make_service(store: ConfigStore, mock_client: MagicMock) -> StorageService:
    return StorageService(
        config_store=store,
        client_factory=lambda url, token: mock_client,
    )


def _bigquery_client(create_result: dict | None = None) -> MagicMock:
    """MagicMock KeboolaClient that reports a BigQuery backend.

    branch_id is None in these tests, so the bucket-exists / legacy-branch
    helpers short-circuit and only verify_token + create_table are exercised.
    """
    client = MagicMock()
    client.verify_token.return_value = MagicMock(default_backend="bigquery")
    client.create_table.return_value = create_result or {"id": "in.c-main.events_repart"}
    return client


# ---------------------------------------------------------------------------
# Client layer
# ---------------------------------------------------------------------------


class TestCreateTableClientBody:
    """KeboolaClient.create_table() request body shaping."""

    def test_source_mode_omits_columns(self, httpx_mock) -> None:
        """Source mode sends `source` and NO `columns` key."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/buckets/in.c-main/tables-definition",
            method="POST",
            json={"id": 1, "status": "success", "results": {"id": "in.c-main.events_repart"}},
            status_code=200,
        )
        client = KeboolaClient(stack_url="https://connection.keboola.com", token=TEST_TOKEN)
        client.create_table(
            bucket_id="in.c-main",
            name="events_repart",
            source={"tableId": "in.c-main.events", "branchId": 123},
            primary_key=["id"],
            time_partitioning={"type": "DAY", "field": "created_at"},
            clustering={"fields": ["tenant_id"]},
        )

        body = json.loads(httpx_mock.get_request().content.decode("utf-8"))
        assert body["name"] == "events_repart"
        assert body["primaryKeysNames"] == ["id"]
        assert body["source"] == {"tableId": "in.c-main.events", "branchId": 123}
        assert "columns" not in body
        assert body["timePartitioning"] == {"type": "DAY", "field": "created_at"}
        assert body["clustering"] == {"fields": ["tenant_id"]}
        client.close()

    def test_range_partitioning_body_uses_strings(self, httpx_mock) -> None:
        """rangePartitioning bounds are sent as strings, matching the API."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/buckets/in.c-main/tables-definition",
            method="POST",
            json={"id": 1, "status": "success", "results": {"id": "in.c-main.t"}},
            status_code=200,
        )
        client = KeboolaClient(stack_url="https://connection.keboola.com", token=TEST_TOKEN)
        client.create_table(
            bucket_id="in.c-main",
            name="t",
            source={"tableId": "in.c-main.src"},
            range_partitioning={
                "field": "id",
                "range": {"start": "0", "end": "1000000", "interval": "1000"},
            },
        )

        body = json.loads(httpx_mock.get_request().content.decode("utf-8"))
        assert body["rangePartitioning"] == {
            "field": "id",
            "range": {"start": "0", "end": "1000000", "interval": "1000"},
        }
        assert body["source"] == {"tableId": "in.c-main.src"}
        client.close()

    def test_columns_mode_unchanged(self, httpx_mock) -> None:
        """Plain columns create still sends `columns` and no `source`/layout."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/buckets/in.c-main/tables-definition",
            method="POST",
            json={"id": 1, "status": "success", "results": {"id": "in.c-main.t"}},
            status_code=200,
        )
        client = KeboolaClient(stack_url="https://connection.keboola.com", token=TEST_TOKEN)
        client.create_table(
            bucket_id="in.c-main",
            name="t",
            columns=[{"name": "id", "definition": {"type": "INTEGER"}}],
            primary_key=["id"],
        )

        body = json.loads(httpx_mock.get_request().content.decode("utf-8"))
        assert body["columns"] == [{"name": "id", "definition": {"type": "INTEGER"}}]
        assert "source" not in body
        assert "timePartitioning" not in body
        assert "clustering" not in body
        client.close()


# ---------------------------------------------------------------------------
# Service layer -- validation
# ---------------------------------------------------------------------------


class TestCreateTableServiceValidation:
    """Argument validation happens before any HTTP call."""

    def test_columns_and_source_both_rejected(self, tmp_path: Path) -> None:
        service = _make_service(_make_store(tmp_path), MagicMock())
        with pytest.raises(ValueError, match="must not be combined"):
            service.create_table(
                alias="test",
                bucket_id="in.c-main",
                name="t",
                columns=["id:INTEGER"],
                source_table_id="in.c-main.src",
            )

    def test_neither_columns_nor_source_rejected(self, tmp_path: Path) -> None:
        service = _make_service(_make_store(tmp_path), MagicMock())
        with pytest.raises(ValueError, match="either --column"):
            service.create_table(alias="test", bucket_id="in.c-main", name="t")

    def test_not_null_rejected_in_source_mode(self, tmp_path: Path) -> None:
        service = _make_service(_make_store(tmp_path), MagicMock())
        with pytest.raises(ValueError, match="--not-null is not valid"):
            service.create_table(
                alias="test",
                bucket_id="in.c-main",
                name="t",
                source_table_id="in.c-main.src",
                not_null_columns=["id"],
            )

    def test_default_rejected_in_source_mode(self, tmp_path: Path) -> None:
        service = _make_service(_make_store(tmp_path), MagicMock())
        with pytest.raises(ValueError, match="--default is not valid"):
            service.create_table(
                alias="test",
                bucket_id="in.c-main",
                name="t",
                source_table_id="in.c-main.src",
                defaults=["id=0"],
            )

    def test_source_branch_id_without_source_table_rejected(self, tmp_path: Path) -> None:
        service = _make_service(_make_store(tmp_path), MagicMock())
        with pytest.raises(ValueError, match="--source-branch-id requires --source-table-id"):
            service.create_table(
                alias="test",
                bucket_id="in.c-main",
                name="t",
                columns=["id:INTEGER"],
                source_branch_id=42,
            )

    def test_incomplete_range_partitioning_rejected(self, tmp_path: Path) -> None:
        service = _make_service(_make_store(tmp_path), MagicMock())
        with pytest.raises(ValueError, match="--range-partitioning requires all"):
            service.create_table(
                alias="test",
                bucket_id="in.c-main",
                name="t",
                source_table_id="in.c-main.src",
                range_partitioning_field="id",
                range_partitioning_start="0",
                # end + interval missing
            )

    def test_time_and_range_partitioning_mutually_exclusive(self, tmp_path: Path) -> None:
        service = _make_service(_make_store(tmp_path), MagicMock())
        with pytest.raises(ValueError, match="mutually exclusive"):
            service.create_table(
                alias="test",
                bucket_id="in.c-main",
                name="t",
                source_table_id="in.c-main.src",
                time_partitioning_type="DAY",
                range_partitioning_field="id",
                range_partitioning_start="0",
                range_partitioning_end="100",
                range_partitioning_interval="10",
            )

    def test_time_partitioning_field_without_type_rejected(self, tmp_path: Path) -> None:
        service = _make_service(_make_store(tmp_path), MagicMock())
        with pytest.raises(ValueError, match="--time-partitioning-type is required"):
            service.create_table(
                alias="test",
                bucket_id="in.c-main",
                name="t",
                source_table_id="in.c-main.src",
                time_partitioning_field="created_at",
            )


# ---------------------------------------------------------------------------
# Service layer -- BigQuery pre-flight guard
# ---------------------------------------------------------------------------


class TestCreateTableBackendGuard:
    """The BigQuery-only guard fires before the create POST."""

    def test_source_on_snowflake_rejected_before_post(self, tmp_path: Path) -> None:
        client = MagicMock()
        client.verify_token.return_value = MagicMock(default_backend="snowflake")
        service = _make_service(_make_store(tmp_path), client)

        with pytest.raises(ValueError, match="require a BigQuery backend"):
            service.create_table(
                alias="test",
                bucket_id="in.c-main",
                name="t",
                source_table_id="in.c-main.src",
            )
        client.create_table.assert_not_called()

    def test_partitioning_on_snowflake_rejected(self, tmp_path: Path) -> None:
        client = MagicMock()
        client.verify_token.return_value = MagicMock(default_backend="snowflake")
        service = _make_service(_make_store(tmp_path), client)

        with pytest.raises(ValueError, match="require a BigQuery backend"):
            service.create_table(
                alias="test",
                bucket_id="in.c-main",
                name="t",
                columns=["id:INTEGER"],
                clustering_fields=["id"],
            )
        client.create_table.assert_not_called()

    def test_source_on_bigquery_passes(self, tmp_path: Path) -> None:
        client = _bigquery_client(
            {"id": "in.c-main.events_repart", "columns": ["id", "created_at"]}
        )
        service = _make_service(_make_store(tmp_path), client)

        result = service.create_table(
            alias="test",
            bucket_id="in.c-main",
            name="events_repart",
            source_table_id="in.c-main.events",
            time_partitioning_type="DAY",
            time_partitioning_field="created_at",
            clustering_fields=["tenant_id"],
            primary_key=["id"],
        )

        client.create_table.assert_called_once()
        kwargs = client.create_table.call_args.kwargs
        assert kwargs["columns"] is None
        assert kwargs["source"] == {"tableId": "in.c-main.events"}
        assert kwargs["time_partitioning"] == {"type": "DAY", "field": "created_at"}
        assert kwargs["clustering"] == {"fields": ["tenant_id"]}
        # Columns are derived from the completed job in source mode.
        assert result["columns"] == ["id", "created_at"]
        assert result["source_table_id"] == "in.c-main.events"
        assert result["action"] == "created"

    def test_plain_columns_create_skips_backend_check(self, tmp_path: Path) -> None:
        """No BigQuery-only feature => verify_token is never called (no regression)."""
        client = MagicMock()
        client.create_table.return_value = {"id": "in.c-main.t"}
        service = _make_service(_make_store(tmp_path), client)

        service.create_table(
            alias="test",
            bucket_id="in.c-main",
            name="t",
            columns=["id:INTEGER"],
        )
        client.verify_token.assert_not_called()


# ---------------------------------------------------------------------------
# CLI layer
# ---------------------------------------------------------------------------


class TestCreateTableCli:
    def test_source_flags_passed_through(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.create_table.return_value = {
                "project_alias": "test",
                "table_id": "in.c-main.events_repart",
                "name": "events_repart",
                "bucket_id": "in.c-main",
                "primary_key": ["id"],
                "columns": ["id", "created_at"],
                "auto_created_bucket": False,
                "legacy_branch_storage": False,
                "action": "created",
                "source_table_id": "in.c-main.events",
                "source_branch_id": None,
                "time_partitioning": {"type": "DAY", "field": "created_at"},
                "range_partitioning": None,
                "clustering": {"fields": ["tenant_id"]},
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
                    "in.c-main",
                    "--name",
                    "events_repart",
                    "--source-table-id",
                    "in.c-main.events",
                    "--time-partitioning-type",
                    "DAY",
                    "--time-partitioning-field",
                    "created_at",
                    "--clustering-field",
                    "tenant_id",
                    "--primary-key",
                    "id",
                ],
            )

        assert result.exit_code == 0, result.output
        kwargs = svc.create_table.call_args.kwargs
        assert kwargs["source_table_id"] == "in.c-main.events"
        assert kwargs["time_partitioning_type"] == "DAY"
        assert kwargs["time_partitioning_field"] == "created_at"
        assert kwargs["clustering_fields"] == ["tenant_id"]
        # --column is optional now; nothing was passed.
        assert not kwargs["columns"]

    def test_column_no_longer_required(self, tmp_path: Path) -> None:
        """Invoking with only --source-table-id (no --column) must not be a usage error."""
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.create_table.return_value = {
                "project_alias": "test",
                "table_id": "in.c-main.t",
                "name": "t",
                "bucket_id": "in.c-main",
                "primary_key": [],
                "columns": ["id"],
                "auto_created_bucket": False,
                "legacy_branch_storage": False,
                "action": "created",
                "source_table_id": "in.c-main.src",
                "source_branch_id": None,
                "time_partitioning": None,
                "range_partitioning": None,
                "clustering": None,
            }
            result = runner.invoke(
                app,
                [
                    "storage",
                    "create-table",
                    "--project",
                    "test",
                    "--bucket-id",
                    "in.c-main",
                    "--name",
                    "t",
                    "--source-table-id",
                    "in.c-main.src",
                ],
            )

        assert result.exit_code == 0, result.output
        svc.create_table.assert_called_once()
