"""Tests for storage export row-filters (where / changed_since) and add-column (0.62.0)."""

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
STACK = "https://connection.keboola.com"


def _make_store(tmp_path: Path) -> ConfigStore:
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    store = ConfigStore(config_dir=config_dir)
    store.save(AppConfig(projects={"test": ProjectConfig(stack_url=STACK, token=TEST_TOKEN)}))
    return store


def _make_service(store: ConfigStore, mock_client: MagicMock) -> StorageService:
    return StorageService(config_store=store, client_factory=lambda url, token: mock_client)


class TestApplyTableFilters:
    """Direct unit tests for the shared filter helper."""

    def test_where_and_changed(self) -> None:
        params: dict = {"fileType": "csv"}
        KeboolaClient._apply_table_filters(
            params,
            where_column="status",
            where_operator="neq",
            where_values=["active", "pending"],
            changed_since="-2 days",
            changed_until="now",
        )
        assert params["whereColumn"] == "status"
        assert params["whereOperator"] == "neq"
        assert params["whereValues[]"] == ["active", "pending"]
        assert params["changedSince"] == "-2 days"
        assert params["changedUntil"] == "now"

    def test_no_filters_leaves_params_untouched(self) -> None:
        params: dict = {"limit": 100}
        KeboolaClient._apply_table_filters(params)
        assert params == {"limit": 100}

    def test_invalid_operator_raises(self) -> None:
        with pytest.raises(ValueError, match="where_operator must be 'eq' or 'neq'"):
            KeboolaClient._apply_table_filters(
                {}, where_column="c", where_operator="like", where_values=["x"]
            )

    def test_half_specified_where_raises(self) -> None:
        with pytest.raises(ValueError, match="must be given together"):
            KeboolaClient._apply_table_filters({}, where_column="c")
        with pytest.raises(ValueError, match="must be given together"):
            KeboolaClient._apply_table_filters({}, where_values=["x"])


class TestExportFiltersClient:
    def _client(self) -> KeboolaClient:
        return KeboolaClient(STACK, TEST_TOKEN)

    def test_export_async_forwards_filters(self) -> None:
        client = self._client()
        with (
            patch.object(client, "_request") as mock_req,
            patch.object(client, "_wait_for_storage_job", return_value={"status": "success"}),
        ):
            mock_req.return_value.json.return_value = {"id": "job1"}
            client.export_table_async(
                "in.c-b.t", where_column="x", where_values=["1"], changed_since="-1 day"
            )
        data = mock_req.call_args.kwargs["data"]
        assert data["whereColumn"] == "x"
        assert data["whereValues[]"] == ["1"]
        assert data["whereOperator"] == "eq"
        assert data["changedSince"] == "-1 day"

    def test_preview_forwards_filters(self) -> None:
        client = self._client()
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value.text = "id\n1\n"
            client.get_table_data_preview("in.c-b.t", where_column="x", where_values=["1"])
        params = mock_req.call_args.kwargs["params"]
        assert params["whereColumn"] == "x"
        assert params["whereValues[]"] == ["1"]


class TestAddColumnClient:
    def test_posts_to_columns_endpoint(self) -> None:
        client = KeboolaClient(STACK, TEST_TOKEN)
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value.json.return_value = {"id": "in.c-b.t"}
            client.add_column(
                "in.c-b.t", name="status", definition={"type": "VARCHAR", "length": "20"}
            )
        assert mock_req.call_args.args[0] == "POST"
        assert mock_req.call_args.args[1].endswith("/tables/in.c-b.t/columns")
        assert mock_req.call_args.kwargs["json"] == {
            "name": "status",
            "definition": {"type": "VARCHAR", "length": "20"},
        }

    def test_untyped_column_omits_definition(self) -> None:
        client = KeboolaClient(STACK, TEST_TOKEN)
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value.json.return_value = {}
            client.add_column("in.c-b.t", name="notes", definition=None)
        assert mock_req.call_args.kwargs["json"] == {"name": "notes"}


class TestAddColumnService:
    def test_parses_spec_and_calls_client(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.add_column.return_value = {"id": "in.c-b.t"}
        service = _make_service(store, mock_client)

        result = service.add_column(
            alias="test",
            table_id="in.c-b.t",
            column="amount:NUMBER(18,2)",
            not_null=True,
            default="0",
        )
        assert result["column"] == "amount"
        assert result["definition"] == {
            "type": "NUMBER",
            "length": "18,2",
            "nullable": False,
            "default": "0",
        }
        mock_client.add_column.assert_called_once_with(
            "in.c-b.t",
            name="amount",
            definition={"type": "NUMBER", "length": "18,2", "nullable": False, "default": "0"},
            branch_id=None,
        )
        mock_client.close.assert_called_once()


class TestStorageCLI:
    def _invoke(self, tmp_path: Path, mock_client: MagicMock, args: list[str]):
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
            patch(
                "keboola_agent_cli.cli.StorageService",
                return_value=_make_service(store, mock_client),
            ),
        ):
            return runner.invoke(app, args)

    def test_add_column_cli(self, tmp_path: Path) -> None:
        mock_client = MagicMock()
        mock_client.add_column.return_value = {"id": "in.c-b.t"}
        result = self._invoke(
            tmp_path,
            mock_client,
            [
                "--json",
                "storage",
                "add-column",
                "--project",
                "test",
                "--table-id",
                "in.c-b.t",
                "--column",
                "status:VARCHAR(20)",
            ],
        )
        assert result.exit_code == 0, result.output
        assert mock_client.add_column.call_args.kwargs["name"] == "status"
        assert mock_client.add_column.call_args.kwargs["definition"]["type"] == "VARCHAR"

    def test_download_table_forwards_filters(self, tmp_path: Path) -> None:
        mock_service = MagicMock()
        mock_service.download_table.return_value = {
            "table_id": "in.c-b.t",
            "file_size_bytes": 10,
            "output_path": "t.csv",
        }
        store = _make_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
            patch("keboola_agent_cli.cli.StorageService", return_value=mock_service),
        ):
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "download-table",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-b.t",
                    "--where-column",
                    "status",
                    "--where-value",
                    "active",
                    "--where-operator",
                    "neq",
                    "--changed-since",
                    "-2 days",
                ],
            )
        assert result.exit_code == 0, result.output
        kwargs = mock_service.download_table.call_args.kwargs
        assert kwargs["where_column"] == "status"
        assert kwargs["where_values"] == ["active"]
        assert kwargs["where_operator"] == "neq"
        assert kwargs["changed_since"] == "-2 days"
