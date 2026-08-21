"""``storage table-detail`` surfaces a typed table's ``definition`` (issue #621).

``storage create-table`` can apply BigQuery ``timePartitioning`` /
``rangePartitioning`` / ``clustering`` and ``storage swap-tables`` promotes the
result into place, but nothing could read that layout back: the service built
its response from an explicit field allowlist that dropped ``definition``.

That made the repartition flow unverifiable from kbagent -- the table id is
unchanged whether the swap happened or not, so the layout is the only field
that tells the two apart, and on a Keboola-managed BigQuery project it may be
the only view of the registered layout reachable at all.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.commands._storage_format import _format_table_layout
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import AppConfig, ProjectConfig
from keboola_agent_cli.services.storage_service import StorageService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
TABLE_ID = "out.c-my-bucket.my-table"

PARTITIONED_DEFINITION = {
    "primaryKeysNames": ["id"],
    "timePartitioning": {"type": "DAY", "field": "created_at"},
    "clustering": {"fields": ["tenant_id", "country"]},
}


def _make_store(tmp_path: Path) -> ConfigStore:
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    store = ConfigStore(config_dir=config_dir)
    store.save(
        AppConfig(
            projects={
                "test": ProjectConfig(
                    stack_url="https://connection.europe-west3.gcp.keboola.com",
                    token=TEST_TOKEN,
                )
            },
            default_project="test",
        )
    )
    return store


def _table_resource(definition: object | None) -> dict:
    resource: dict = {
        "id": TABLE_ID,
        "name": "my-table",
        "displayName": "my-table",
        "bucket": {"id": "out.c-my-bucket", "backend": "bigquery"},
        "columns": ["id", "created_at"],
        "primaryKey": ["id"],
        "rowsCount": 6290737,
        "columnMetadata": {},
        "metadata": [],
    }
    if definition is not None:
        resource["definition"] = definition
    return resource


class TestServiceSurfacesDefinition:
    """StorageService.get_table_detail passes ``definition`` through verbatim."""

    def test_typed_table_definition_passed_through(self, tmp_path: Path) -> None:
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = _table_resource(PARTITIONED_DEFINITION)
        service = StorageService(
            config_store=_make_store(tmp_path), client_factory=lambda _u, _t: mock_client
        )

        result = service.get_table_detail("test", TABLE_ID)

        assert result["definition"] == PARTITIONED_DEFINITION

    def test_untyped_table_definition_is_none(self, tmp_path: Path) -> None:
        """Absent on an untyped table -- ``None`` needs no special-casing."""
        mock_client = MagicMock()
        mock_client.get_table_detail.return_value = _table_resource(None)
        service = StorageService(
            config_store=_make_store(tmp_path), client_factory=lambda _u, _t: mock_client
        )

        result = service.get_table_detail("test", TABLE_ID)

        assert result["definition"] is None


class TestFormatTableLayout:
    """The human-mode renderer only emits rows for a layout that exists."""

    def test_no_layout_renders_nothing(self) -> None:
        assert _format_table_layout(None) == []
        assert _format_table_layout({}) == []
        assert _format_table_layout({"primaryKeysNames": ["id"]}) == []

    def test_non_dict_definition_renders_nothing(self) -> None:
        """A Storage API deploy once served ``definition`` as ``[]``.

        See tests/test_storage_empty_definition.py -- the renderer must not
        reintroduce that crash class.
        """
        assert _format_table_layout([]) == []
        assert _format_table_layout("nonsense") == []

    def test_time_partitioning_and_clustering(self) -> None:
        assert _format_table_layout(PARTITIONED_DEFINITION) == [
            ("Partitioning", "DAY on created_at"),
            ("Clustering", "tenant_id, country"),
        ]

    def test_ingestion_time_partitioning_has_no_field(self) -> None:
        """BigQuery partitions on load time when ``field`` is absent."""
        assert _format_table_layout({"timePartitioning": {"type": "HOUR"}}) == [
            ("Partitioning", "HOUR")
        ]

    def test_range_partitioning_with_bounds(self) -> None:
        definition = {
            "rangePartitioning": {
                "field": "customer_id",
                "range": {"start": "0", "end": "1000", "interval": "10"},
            }
        }
        assert _format_table_layout(definition) == [
            ("Range partitioning", "customer_id [0, 1000) step 10")
        ]

    def test_range_partitioning_without_bounds(self) -> None:
        definition = {"rangePartitioning": {"field": "customer_id"}}
        assert _format_table_layout(definition) == [("Range partitioning", "customer_id")]


class TestTableDetailCli:
    """End-to-end through the CLI, both output modes."""

    def _invoke(self, tmp_path: Path, definition: object, json_mode: bool) -> str:
        runner = CliRunner()
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = _make_store(tmp_path)
            MockSvc.return_value.get_table_detail.return_value = {
                "project_alias": "test",
                "table_id": TABLE_ID,
                "name": "my-table",
                "display_name": "my-table",
                "bucket_id": "out.c-my-bucket",
                "rows_count": 6290737,
                "data_size_bytes": 1024,
                "primary_key": ["id"],
                "column_details": [],
                "last_import_date": "2026-08-19T17:26:18+0200",
                "definition": definition,
            }
            argv = ["storage", "table-detail", "--project", "test", "--table-id", TABLE_ID]
            result = runner.invoke(app, (["--json"] if json_mode else []) + argv)
        assert result.exit_code == 0, result.output
        return result.output

    def test_human_output_shows_layout(self, tmp_path: Path) -> None:
        output = self._invoke(tmp_path, PARTITIONED_DEFINITION, json_mode=False)

        assert "Partitioning: DAY on created_at" in output
        assert "Clustering: tenant_id, country" in output
        # Ordered between the primary key and the last-import line.
        assert output.index("Primary key") < output.index("Partitioning")
        assert output.index("Clustering") < output.index("Last import")

    def test_human_output_unchanged_without_layout(self, tmp_path: Path) -> None:
        """Snowflake / untyped tables print exactly what they printed before."""
        output = self._invoke(tmp_path, None, json_mode=False)

        assert "Partitioning" not in output
        assert "Clustering" not in output
        assert "Primary key: id" in output

    def test_json_output_carries_definition(self, tmp_path: Path) -> None:
        import json

        payload = json.loads(self._invoke(tmp_path, PARTITIONED_DEFINITION, json_mode=True))

        assert payload["data"]["definition"] == PARTITIONED_DEFINITION
