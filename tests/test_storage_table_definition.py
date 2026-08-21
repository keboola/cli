"""`storage table-detail` must surface the table's `definition` (issue #621).

The Storage API's table-detail response (`GET /v2/storage/tables/{id}`) carries a
`definition` object. For a BigQuery table that object is the only readable record
of the registered `timePartitioning` / `rangePartitioning` / `clustering` layout.
`StorageService.get_table_detail()` assembled its return value as an explicit
field allowlist and `definition` was not on it, so the layout that
`storage create-table` + `storage swap-tables` had just applied was invisible:
the write half of the documented repartition flow was supported, the verify half
was not. `create-table` is no substitute -- its JSON echoes the layout the caller
*requested*, and its `--if-not-exists` skip path nulls those keys outright.

Two properties of the upstream response shape these tests. Both were read off
connection's `TableDetailResponseProvider::getResponseArray()` and
`BigqueryDriverConfig::extendTableDefinitionResponse()`:

1. `definition` is set on EVERY table-detail response, typed table or not -- an
   untyped one gets a `definition` built by
   `createUntypedTableDefinitionResponseFromMetadata()`. So a missing/None
   `definition` never means "untyped", and the human-mode render must key off the
   layout keys themselves, never off the presence of `definition`.
2. When partitioning is set the response also carries `requirePartitionFilter`
   and `partitions[]` -- one entry per physical partition, read from
   `INFORMATION_SCHEMA.PARTITIONS`. That list is unbounded (a DAY-partitioned
   table holding three years of data has ~1,100 entries), so human mode prints
   its length and never its contents. JSON mode passes it through unchanged:
   re-dropping an API field is the exact bug this issue is about.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import AppConfig, ProjectConfig
from keboola_agent_cli.services.storage_service import StorageService

runner = CliRunner()

# Canonical fake test token (see tests/helpers conventions).
TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

TABLE_ID = "out.c-my-bucket.my-table"


def _make_store(tmp_path: Path) -> ConfigStore:
    """Config store with a single project aliased 'test'."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    store = ConfigStore(config_dir=config_dir)
    store.save(
        AppConfig(
            projects={
                "test": ProjectConfig(
                    stack_url="https://connection.keboola.com",
                    token=TEST_TOKEN,
                )
            },
        )
    )
    return store


def _table_resource(definition: Any) -> dict[str, Any]:
    """A minimal table-detail payload carrying the supplied `definition`."""
    return {
        "id": TABLE_ID,
        "name": "my-table",
        "displayName": "my-table",
        "bucket": {"id": "out.c-my-bucket", "backend": "bigquery"},
        "primaryKey": ["id"],
        "rowsCount": 6290737,
        "dataSizeBytes": 4096,
        "isAlias": False,
        "lastImportDate": "2026-08-19T17:26:18+0200",
        "lastChangeDate": "2026-08-19T17:26:18+0200",
        "created": "2026-08-01T00:00:00+0200",
        "columns": ["id", "created_at", "tenant_id", "country"],
        "columnMetadata": {},
        "metadata": [],
        "definition": definition,
    }


BIGQUERY_LAYOUT: dict[str, Any] = {
    "primaryKeysNames": ["id"],
    "columns": [{"name": "id", "definition": {"type": "INTEGER"}}],
    "timePartitioning": {"type": "DAY", "field": "created_at"},
    "clustering": {"fields": ["tenant_id", "country"]},
    "requirePartitionFilter": True,
    "partitions": [
        {
            "partitionId": "20260819",
            "rowsNumber": "42",
            "lastModifiedTime": "1755620778000",
            "storageTier": "ACTIVE",
        },
        {
            "partitionId": "20260820",
            "rowsNumber": "43",
            "lastModifiedTime": "1755707178000",
            "storageTier": "ACTIVE",
        },
    ],
}

RANGE_LAYOUT: dict[str, Any] = {
    "primaryKeysNames": ["id"],
    "columns": [{"name": "id", "definition": {"type": "INTEGER"}}],
    "rangePartitioning": {
        "field": "order_id",
        "range": {"start": "0", "end": "1000000", "interval": "1000"},
    },
}

# What connection returns for a table with no typed definition at all: the key is
# present, the layout keys are not.
UNTYPED_DEFINITION: dict[str, Any] = {
    "primaryKeysNames": [],
    "columns": [{"name": "id", "definition": {}}],
}


def _service(tmp_path: Path, definition: Any) -> StorageService:
    client = MagicMock()
    client.get_table_detail.return_value = _table_resource(definition)
    return StorageService(
        config_store=_make_store(tmp_path),
        client_factory=lambda _u, _t: client,
    )


class TestServicePassthrough:
    """The service must stop filtering `definition` out of the response."""

    def test_bigquery_layout_reaches_the_caller_verbatim(self, tmp_path: Path) -> None:
        result = _service(tmp_path, BIGQUERY_LAYOUT).get_table_detail("test", TABLE_ID)

        assert result["definition"] == BIGQUERY_LAYOUT

    def test_partitions_list_is_not_re_dropped(self, tmp_path: Path) -> None:
        """`partitions[]` is large but passing it on is the point of the issue."""
        result = _service(tmp_path, BIGQUERY_LAYOUT).get_table_detail("test", TABLE_ID)

        assert result["definition"]["partitions"] == BIGQUERY_LAYOUT["partitions"]

    def test_untyped_table_still_carries_a_definition(self, tmp_path: Path) -> None:
        """A missing layout is NOT a missing `definition` -- connection always sends one."""
        result = _service(tmp_path, UNTYPED_DEFINITION).get_table_detail("test", TABLE_ID)

        assert result["definition"] == UNTYPED_DEFINITION

    def test_absent_definition_becomes_none(self, tmp_path: Path) -> None:
        """Defensive: an older stack that omits the key must not KeyError."""
        client = MagicMock()
        payload = _table_resource(None)
        del payload["definition"]
        client.get_table_detail.return_value = payload
        service = StorageService(
            config_store=_make_store(tmp_path),
            client_factory=lambda _u, _t: client,
        )

        assert service.get_table_detail("test", TABLE_ID)["definition"] is None

    def test_empty_array_wire_shape_survives(self, tmp_path: Path) -> None:
        """The SUPPORT-16581 shape: PHP serialized an empty definition as `[]`.

        See tests/test_storage_empty_definition.py -- a Storage API deploy emitted
        `"definition":[]` and broke the Go CLI's strict decoder. Passing the value
        through must not raise, and must not coerce it into something else.
        """
        result = _service(tmp_path, []).get_table_detail("test", TABLE_ID)

        assert result["definition"] == []


def _invoke_detail(tmp_path: Path, definition: Any, *, json_mode: bool = False) -> Any:
    store = _make_store(tmp_path)
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.StorageService") as MockSvc,
    ):
        MockStore.return_value = store
        client = MagicMock()
        client.get_table_detail.return_value = _table_resource(definition)
        MockSvc.return_value.get_table_detail.side_effect = lambda **kwargs: StorageService(
            config_store=store, client_factory=lambda _u, _t: client
        ).get_table_detail(**kwargs)
        argv = ["storage", "table-detail", "--project", "test", "--table-id", TABLE_ID]
        return runner.invoke(app, (["--json"] if json_mode else []) + argv)


class TestHumanOutput:
    """Human mode renders the layout; without one it prints nothing new."""

    def test_time_partitioning_and_clustering_are_rendered(self, tmp_path: Path) -> None:
        result = _invoke_detail(tmp_path, BIGQUERY_LAYOUT)

        assert result.exit_code == 0, result.output
        assert "Time partitioning: DAY on created_at" in result.output
        assert "Clustering: tenant_id, country" in result.output

    def test_partitions_are_summarized_not_listed(self, tmp_path: Path) -> None:
        """Printing 1,100 partition rows would bury the answer the user asked for."""
        result = _invoke_detail(tmp_path, BIGQUERY_LAYOUT)

        assert "Partitions: 2" in result.output
        assert "20260819" not in result.output

    def test_require_partition_filter_is_reported(self, tmp_path: Path) -> None:
        """A query without a filter fails outright -- worth one line."""
        result = _invoke_detail(tmp_path, BIGQUERY_LAYOUT)

        assert "Partition filter required: yes" in result.output

    def test_range_partitioning_is_rendered_with_its_bounds(self, tmp_path: Path) -> None:
        result = _invoke_detail(tmp_path, RANGE_LAYOUT)

        assert result.exit_code == 0, result.output
        assert "Range partitioning: order_id [0, 1000000) step 1000" in result.output

    def test_no_layout_prints_no_layout_lines(self, tmp_path: Path) -> None:
        """Snowflake / untyped output stays as it was -- the whole block is guarded."""
        result = _invoke_detail(tmp_path, UNTYPED_DEFINITION)

        assert result.exit_code == 0, result.output
        for absent in ("partitioning", "Clustering", "Partitions:"):
            assert absent not in result.output

    def test_non_dict_definition_does_not_crash_the_render(self, tmp_path: Path) -> None:
        """`definition: []` is a shape the API has really emitted; `.get` would blow up."""
        result = _invoke_detail(tmp_path, [])

        assert result.exit_code == 0, result.output
        assert "Clustering" not in result.output


class TestJsonOutput:
    def test_definition_passes_through_json_mode(self, tmp_path: Path) -> None:
        import json

        result = _invoke_detail(tmp_path, BIGQUERY_LAYOUT, json_mode=True)

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        data = payload.get("data", payload)
        assert data["definition"] == BIGQUERY_LAYOUT
