"""Tests for table-usage resolution behind `storage tables --include-usage`.

Mirrors keboola-mcp-server's `get_tables(include_usage=True)`, which is a
config-based scan limited to the storage input/output mapping scopes.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from helpers import setup_single_project
from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.project_service import ProjectService
from keboola_agent_cli.services.storage_service import StorageService
from keboola_agent_cli.services.table_usage import collect_table_usage

COMPONENTS = [
    {
        "id": "keboola.ex-db-snowflake",
        "name": "Snowflake Extractor",
        "type": "extractor",
        "configurations": [
            {
                "id": "123",
                "name": "Load orders",
                "configuration": {
                    "storage": {
                        "output": {
                            "tables": [{"destination": "in.c-main.orders", "source": "o.csv"}]
                        }
                    }
                },
                "rows": [],
            }
        ],
    },
    {
        "id": "keboola.snowflake-transformation",
        "name": "Snowflake SQL",
        "type": "transformation",
        "configurations": [
            {
                "id": "456",
                "name": "Aggregate",
                "configuration": {
                    "parameters": {"blocks": [{"script": "SELECT * FROM in.c-main.orders"}]},
                    "storage": {
                        "input": {"tables": [{"source": "in.c-main.orders"}]},
                        "output": {"tables": [{"destination": "out.c-main.daily"}]},
                    },
                },
                "rows": [],
            }
        ],
    },
    {
        "id": "keboola.wr-db-snowflake",
        "name": "Snowflake Writer",
        "type": "writer",
        "configurations": [
            {
                "id": "789",
                "name": "Write DWH",
                "configuration": {"parameters": {}},
                "rows": [
                    {
                        "id": "row-1",
                        "name": "daily row",
                        "configuration": {
                            "storage": {"input": {"tables": [{"source": "out.c-main.daily"}]}}
                        },
                    }
                ],
            }
        ],
    },
]


class TestCollectTableUsage:
    def test_output_mapping_reference_is_found(self) -> None:
        usage = collect_table_usage(COMPONENTS, ["in.c-main.orders"])

        refs = usage["in.c-main.orders"]
        assert {"keboola.ex-db-snowflake", "keboola.snowflake-transformation"} == {
            r["component_id"] for r in refs
        }

    def test_scope_is_recorded_per_reference(self) -> None:
        usage = collect_table_usage(COMPONENTS, ["in.c-main.orders"])

        by_config = {r["config_id"]: r for r in usage["in.c-main.orders"]}
        assert by_config["123"]["scope"] == "storage.output"
        assert by_config["456"]["scope"] == "storage.input"

    def test_sql_body_reference_is_not_counted(self) -> None:
        """The transformation's SQL also names the table; only mappings count."""
        usage = collect_table_usage(COMPONENTS, ["in.c-main.orders"])

        scopes = {r["scope"] for r in usage["in.c-main.orders"] if r["config_id"] == "456"}
        assert scopes == {"storage.input"}

    def test_row_level_reference_carries_row_id(self) -> None:
        usage = collect_table_usage(COMPONENTS, ["out.c-main.daily"])

        row_refs = [
            r for r in usage["out.c-main.daily"] if r["component_id"].startswith("keboola.wr")
        ]
        assert len(row_refs) == 1
        assert row_refs[0]["row_id"] == "row-1"
        assert row_refs[0]["config_id"] == "789"

    def test_unused_table_maps_to_empty_list(self) -> None:
        usage = collect_table_usage(COMPONENTS, ["in.c-main.never_used"])

        assert usage["in.c-main.never_used"] == []

    def test_match_is_case_insensitive(self) -> None:
        """Keboola configs spell one table id several ways (see issue #569)."""
        usage = collect_table_usage(COMPONENTS, ["IN.C-MAIN.ORDERS"])

        assert len(usage["IN.C-MAIN.ORDERS"]) == 2

    def test_partial_id_does_not_match(self) -> None:
        """`in.c-main.order` must not match the table `in.c-main.orders`."""
        usage = collect_table_usage(COMPONENTS, ["in.c-main.order"])

        assert usage["in.c-main.order"] == []

    def test_no_target_ids_returns_empty_mapping(self) -> None:
        assert collect_table_usage(COMPONENTS, []) == {}

    def test_reference_is_reported_once_per_config(self) -> None:
        """A table used twice in one config's mapping yields one entry per scope."""
        components = [
            {
                "id": "keboola.snowflake-transformation",
                "name": "T",
                "type": "transformation",
                "configurations": [
                    {
                        "id": "1",
                        "name": "dup",
                        "configuration": {
                            "storage": {
                                "input": {
                                    "tables": [
                                        {"source": "in.c-main.orders"},
                                        {"source": "in.c-main.orders"},
                                    ]
                                }
                            }
                        },
                        "rows": [],
                    }
                ],
            }
        ]

        usage = collect_table_usage(components, ["in.c-main.orders"])

        assert len(usage["in.c-main.orders"]) == 1


class TestListTablesIncludeUsage:
    """`StorageService.list_tables(include_usage=True)` annotates each row."""

    @staticmethod
    def _service(tmp_config_dir, mock_client):
        store = setup_single_project(tmp_config_dir)
        return StorageService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

    def test_used_by_is_attached(self, tmp_config_dir) -> None:
        mock_client = MagicMock()
        mock_client.list_tables.return_value = [
            {"id": "in.c-main.orders", "name": "orders", "bucket": {"id": "in.c-main"}}
        ]
        mock_client.list_components_with_configs.return_value = COMPONENTS

        result = self._service(tmp_config_dir, mock_client).list_tables(
            aliases=["prod"], include_usage=True
        )

        used_by = result["tables"][0]["used_by"]
        assert {r["config_id"] for r in used_by} == {"123", "456"}
        mock_client.close.assert_called_once()

    def test_usage_costs_one_extra_call_per_project(self, tmp_config_dir) -> None:
        """The component listing is fetched once, not once per table."""
        mock_client = MagicMock()
        mock_client.list_tables.return_value = [
            {"id": "in.c-main.orders", "name": "orders", "bucket": {"id": "in.c-main"}},
            {"id": "out.c-main.daily", "name": "daily", "bucket": {"id": "out.c-main"}},
        ]
        mock_client.list_components_with_configs.return_value = COMPONENTS

        self._service(tmp_config_dir, mock_client).list_tables(aliases=["prod"], include_usage=True)

        assert mock_client.list_components_with_configs.call_count == 1

    def test_off_by_default(self, tmp_config_dir) -> None:
        mock_client = MagicMock()
        mock_client.list_tables.return_value = [
            {"id": "in.c-main.orders", "name": "orders", "bucket": {"id": "in.c-main"}}
        ]

        result = self._service(tmp_config_dir, mock_client).list_tables(aliases=["prod"])

        mock_client.list_components_with_configs.assert_not_called()
        assert "used_by" not in result["tables"][0]

    def test_component_listing_failure_degrades_to_empty_usage(self, tmp_config_dir) -> None:
        """Losing the usage scan must not lose the table listing itself."""
        mock_client = MagicMock()
        mock_client.list_tables.return_value = [
            {"id": "in.c-main.orders", "name": "orders", "bucket": {"id": "in.c-main"}}
        ]
        mock_client.list_components_with_configs.side_effect = KeboolaApiError(
            status_code=403, error_code="FORBIDDEN", message="no component read"
        )

        result = self._service(tmp_config_dir, mock_client).list_tables(
            aliases=["prod"], include_usage=True
        )

        assert result["tables"][0]["id"] == "in.c-main.orders"
        assert result["tables"][0]["used_by"] == []


class TestStorageTablesUsageCli:
    """`kbagent storage tables --include-usage` reaches the service and renders."""

    @staticmethod
    def _invoke(tmp_path, args: list[str], mock_client):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-55555-fakeTestTokenDoNotUseXXXXXXXX",
                project_name="Prod",
                project_id=1234,
            ),
        )
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.StorageService") as MockStorageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockStorageService.return_value = StorageService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            return CliRunner().invoke(app, args)

    @staticmethod
    def _client() -> MagicMock:
        mock_client = MagicMock()
        mock_client.list_tables.return_value = [
            {"id": "in.c-main.orders", "name": "orders", "bucket": {"id": "in.c-main"}}
        ]
        mock_client.list_components_with_configs.return_value = COMPONENTS
        return mock_client

    def test_json_output_carries_used_by(self, tmp_path) -> None:
        result = self._invoke(
            tmp_path,
            ["--json", "storage", "tables", "--project", "prod", "--include-usage"],
            self._client(),
        )

        assert result.exit_code == 0, result.output
        tables = json.loads(result.output)["data"]["tables"]
        assert {r["config_id"] for r in tables[0]["used_by"]} == {"123", "456"}

    def test_human_output_shows_used_by_column(self, tmp_path) -> None:
        result = self._invoke(
            tmp_path,
            ["storage", "tables", "--project", "prod", "--include-usage"],
            self._client(),
        )

        assert result.exit_code == 0, result.output
        assert "Used By" in result.output

    def test_flag_off_skips_component_listing(self, tmp_path) -> None:
        mock_client = self._client()
        result = self._invoke(
            tmp_path,
            ["--json", "storage", "tables", "--project", "prod"],
            mock_client,
        )

        assert result.exit_code == 0, result.output
        mock_client.list_components_with_configs.assert_not_called()
