"""Regression: Storage API serializes a non-typed column's ``definition`` as ``[]``.

Observed in a support incident (Storage API on a GCP eu-west3 stack, non-typed
table): a Storage API deploy started emitting an empty JSON array for the
per-column ``definition`` of NON-TYPED tables -- ``"definition":[]`` -- instead
of an object ``{}`` or ``null``. The legacy Go ``kbc`` CLI (v2.47.3) decodes the
table resource into a strict typed struct (``keboola.Column.Definition``), so
its decoder blows up::

    cannot decode JSON result: ... keboola.Column.Definition:
    readObjectStart: expect { or n, but found [ ...
    |columns":[{"name":"inward_issue_id","definition":[]}, ...

The failing request in that flow is the table-detail GET
(``GET /v2/storage/branch/<id>/tables/<table_id>``) that the download command
issues to build the CSV header.

These tests pin the contract that kbagent is immune to that exact wire shape:

1. ``get_table_detail`` hits the SAME endpoint the Go CLI chokes on; it must
   parse the corrupt payload without raising and still surface the columns +
   types (which it reads from ``columnMetadata``, never from ``definition``).
2. ``download_table`` -- the operation the client actually ran -- must complete
   end to end even when the table resource carries ``"definition":[]``.

Python's ``json.loads`` parses ``[]`` and ``{}`` alike, and we never strictly
deserialize the ``definition`` block, so both paths are structurally safe. A
future refactor doing ``col.get("definition", {}).get("type")`` over a value
that is ``[]`` (present, so the ``{}`` default is skipped) would reintroduce
the bug -- these tests guard that regression.
"""

import csv
from pathlib import Path
from unittest.mock import MagicMock

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import AppConfig, ProjectConfig
from keboola_agent_cli.services.storage_service import StorageService

# Canonical fake test token (see tests/helpers conventions).
TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

TABLE_ID = "out.c-jira-rds.deps_out"
COLUMN_NAMES = ["inward_issue_id", "inward_issue_key"]


def _make_store(tmp_path: Path) -> ConfigStore:
    """Config store with a single project aliased 'test'."""
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
        )
    )
    return store


def _corrupt_table_resource() -> dict:
    """A table resource shaped exactly like the incident payload.

    The per-column ``definition`` inside the typed-table block is an empty
    ARRAY (``[]``) -- the PHP ``json_encode([])`` foot-gun -- instead of an
    object. Legacy top-level ``columns`` (string names) and ``columnMetadata``
    (where kbagent actually reads types) are present alongside it.
    """
    return {
        "id": TABLE_ID,
        "name": "deps_out",
        "displayName": "deps_out",
        "bucket": {"id": "out.c-jira-rds"},
        "primaryKey": [],
        "rowsCount": 42,
        "dataSizeBytes": 4096,
        "isAlias": False,
        "lastImportDate": "2026-06-09T00:00:00+0000",
        # Legacy top-level columns: a list of NAME STRINGS (what kbagent reads).
        "columns": COLUMN_NAMES,
        # The NEW typed-table block carrying the server-side bug: each
        # non-typed column's `definition` comes back as [] rather than {}.
        "definition": {
            "columns": [{"name": name, "definition": []} for name in COLUMN_NAMES],
        },
        # Types live here (legacy KBC.datatype metadata), never in `definition`.
        "columnMetadata": {
            name: [{"key": "KBC.datatype.basetype", "value": "STRING", "provider": "storage"}]
            for name in COLUMN_NAMES
        },
        "metadata": [],
    }


def test_get_table_detail_survives_empty_array_column_definition(tmp_path: Path) -> None:
    """The exact endpoint the Go CLI chokes on parses cleanly in kbagent."""
    store = _make_store(tmp_path)
    mock_client = MagicMock()
    mock_client.get_table_detail.return_value = _corrupt_table_resource()

    service = StorageService(config_store=store, client_factory=lambda _u, _t: mock_client)

    # Must not raise (the Go CLI raises a JSON decode error right here).
    result = service.get_table_detail("test", TABLE_ID)

    assert result["table_id"] == TABLE_ID
    assert result["columns"] == COLUMN_NAMES
    # Types are recovered from columnMetadata despite the corrupt `definition`.
    assert [c["name"] for c in result["column_details"]] == COLUMN_NAMES
    assert all(c["type"] == "STRING" for c in result["column_details"])
    mock_client.close.assert_called_once()


def test_download_table_survives_empty_array_column_definition(tmp_path: Path) -> None:
    """The client's actual operation (download) completes despite ``definition:[]``."""
    store = _make_store(tmp_path)
    output_path = tmp_path / "deps_out.csv"

    mock_client = MagicMock()
    # download_table reads columns via list_tables(include="columns"); return the
    # corrupt resource so the empty-array definition is in the payload it sees.
    mock_client.list_tables.return_value = [_corrupt_table_resource()]
    mock_client.export_table_async.return_value = {"results": {"file": {"id": 83799479}}}
    mock_client.get_file_info.return_value = {
        "id": 83799479,
        "url": "https://example.invalid/download/83799479",
        "isSliced": False,
    }

    def _write_body(url: str, dest: str) -> int:
        Path(dest).write_text("INWARD-1,KEY-1\nINWARD-2,KEY-2\n", encoding="utf-8")
        return Path(dest).stat().st_size

    mock_client.download_file.side_effect = _write_body

    service = StorageService(config_store=store, client_factory=lambda _u, _t: mock_client)

    result = service.download_table("test", TABLE_ID, output_path=str(output_path))

    assert result["table_id"] == TABLE_ID
    assert result["columns"] == COLUMN_NAMES
    # Header (from the corrupt resource) was prepended, body preserved. The
    # header is CSV-quoted, so parse it back rather than string-matching.
    rows = list(csv.reader(output_path.read_text(encoding="utf-8").splitlines()))
    assert rows[0] == COLUMN_NAMES
    assert rows[1] == ["INWARD-1", "KEY-1"]
    mock_client.close.assert_called_once()
