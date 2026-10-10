"""``sync push`` runs the #274 runtime-safety script guard (follow-up to #686).

``normalize_blocks_codes_script`` closes the gap between the Storage API's lax
shape validator and the Keboola runtime's strict one: a ``script`` that is a
string, or a list element packing several ``;``-separated statements, is
accepted by the API and crashes the job later ("Expected array, got string" /
``MULTI_STATEMENT_COUNT``). ``config update`` and ``transformation
edit/create`` have run it since 0.28.0 / 0.30.8; the GitOps deploy route did
not.

After #686 parts 2+3 the guard is a no-op on this path *by construction* --
``merge_code_files`` rebuilds ``parameters.blocks`` from ``transform.sql``
through the single canonical producer. It is wired in as a REGRESSION
BACKSTOP, and it still catches the one shape that bypasses code extraction
entirely: a hand-authored ``_config.yml`` that carries ``parameters.blocks``
inline with no companion code file (``_merge_sql_transformation`` returns
early when ``transform.sql`` is absent, so those parameters reach the API
verbatim).
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import yaml
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.constants import CONFIG_FILENAME
from keboola_agent_cli.services._sync_push_ops import guard_script_shape
from keboola_agent_cli.services.project_service import ProjectService
from test_sync_baseline_stamping import (
    SQL_COMPONENT,
    FakeApi,
    _config_file,
    _init_and_pull,
    _service,
    _sql_components,
    _sql_file,
)
from test_sync_cli import TEST_TOKEN, _setup_config

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sent_script(api: FakeApi) -> Any:
    """The ``script`` value of the last configuration written to the API."""
    configuration = api.update_calls[-1]["configuration"]
    return configuration["parameters"]["blocks"][0]["codes"][0]["script"]


def _normalization_warnings(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [w for w in result.get("warnings", []) if w.get("change_type") == "script_normalization"]


def _inline_blocks(project_root: Path, script: Any) -> None:
    """Rewrite the pulled config into a code-file-less, inline-blocks tree.

    Deleting ``transform.sql`` is what makes ``merge_code_files`` a no-op, so
    whatever ``parameters.blocks`` the YAML holds is exactly what push sends.
    """
    _sql_file(project_root).unlink()
    config_file = _config_file(project_root)
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    data["parameters"] = {
        "blocks": [{"name": "Block 1", "codes": [{"name": "Code 1", "script": script}]}]
    }
    config_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


# ===================================================================
# The guard fires on a body that would reach the API in a crashing shape
# ===================================================================


def test_push_update_normalizes_string_script(tmp_config_dir: Path, tmp_path: Path) -> None:
    """A string ``script`` in _config.yml is split into an array before send."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1;", "SELECT 2;"]))
    store = _init_and_pull(tmp_config_dir, project_root, api)

    _inline_blocks(project_root, "SELECT 1;\nSELECT 2;")

    result = _service(store, api).push(alias="prod", project_root=project_root)

    assert result["errors"] == []
    assert _sent_script(api) == ["SELECT 1;", "SELECT 2;"]

    records = _normalization_warnings(result)
    assert len(records) == 1
    assert records[0]["action"] == "sql_split"
    assert records[0]["after_length"] == 2
    assert records[0]["component_id"] == SQL_COMPONENT
    assert records[0]["config_id"] == "cfg-sql"
    assert records[0]["path"] == "parameters.blocks[0].codes[0].script"
    assert "runtime" in records[0]["message"].lower()


def test_push_update_resplits_packed_list_element(tmp_config_dir: Path, tmp_path: Path) -> None:
    """One list element packing two statements is re-split (#274 crash shape)."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1;", "SELECT 2;"]))
    store = _init_and_pull(tmp_config_dir, project_root, api)

    _inline_blocks(project_root, ["SELECT 1;\nSELECT 2;"])

    result = _service(store, api).push(alias="prod", project_root=project_root)

    assert result["errors"] == []
    assert _sent_script(api) == ["SELECT 1;", "SELECT 2;"]

    records = _normalization_warnings(result)
    assert len(records) == 1
    assert records[0]["action"] == "sql_resplit"
    assert records[0]["after_length"] == 2


def test_push_create_normalizes_string_script(tmp_config_dir: Path, tmp_path: Path) -> None:
    """The CREATE path is guarded too (a hand-authored, never-pulled config)."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1;"]))
    store = _init_and_pull(tmp_config_dir, project_root, api)

    new_dir = _config_file(project_root).parent.parent / "new-transformation"
    new_dir.mkdir(parents=True)
    (new_dir / CONFIG_FILENAME).write_text(
        yaml.safe_dump(
            {
                "name": "New transformation",
                "description": "",
                "_keboola": {"component_id": SQL_COMPONENT},
                "parameters": {
                    "blocks": [
                        {
                            "name": "Block 1",
                            "codes": [{"name": "Code 1", "script": "SELECT 9;\nSELECT 8;"}],
                        }
                    ]
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = _service(store, api).push(alias="prod", project_root=project_root)

    assert result["errors"] == []
    assert result["created"] == 1
    created = next(
        c
        for component in api.components
        if component["id"] == SQL_COMPONENT
        for c in component["configurations"]
        if c["name"] == "New transformation"
    )
    assert created["configuration"]["parameters"]["blocks"][0]["codes"][0]["script"] == [
        "SELECT 9;",
        "SELECT 8;",
    ]

    records = _normalization_warnings(result)
    assert len(records) == 1
    assert records[0]["action"] == "sql_split"
    assert records[0]["config_path"].endswith("new-transformation")


# ===================================================================
# No-op path: a canonical body produces NO records
# ===================================================================


def test_canonical_push_produces_no_normalization_records(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """The ordinary pull -> edit transform.sql -> push flow stays silent."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1;", "SELECT 2;", "SELECT 3;"]))
    store = _init_and_pull(tmp_config_dir, project_root, api)

    sql_file = _sql_file(project_root)
    sql_file.write_text(
        sql_file.read_text(encoding="utf-8").replace("SELECT 3;", "SELECT 4;"), encoding="utf-8"
    )

    result = _service(store, api).push(alias="prod", project_root=project_root)

    assert result["errors"] == []
    assert result["updated"] == 1
    assert _normalization_warnings(result) == []
    assert _sent_script(api) == ["SELECT 1;", "SELECT 2;", "SELECT 4;"]
    # The guard must not perturb the baseline it was wired in beside.
    diff_result = _service(store, api).diff(alias="prod", project_root=project_root)
    assert diff_result["summary"]["remote_modified"] == 0
    assert diff_result["summary"]["modified"] == 0


def test_canonical_push_without_semicolons_produces_no_records(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """Semicolon-less canonical elements are already one-statement-per-element."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1", "SELECT 2"]))
    store = _init_and_pull(tmp_config_dir, project_root, api)

    sql_file = _sql_file(project_root)
    sql_file.write_text(
        sql_file.read_text(encoding="utf-8").replace("SELECT 2", "SELECT 20"), encoding="utf-8"
    )

    result = _service(store, api).push(alias="prod", project_root=project_root)

    assert result["errors"] == []
    assert _normalization_warnings(result) == []
    assert _sent_script(api) == ["SELECT 1", "SELECT 20"]


def test_non_transformation_push_is_untouched(tmp_config_dir: Path, tmp_path: Path) -> None:
    """A component without blocks/codes never grows a normalization record."""
    project_root = tmp_path / "project"
    api = FakeApi(
        [
            {
                "id": "keboola.ex-http",
                "type": "extractor",
                "configurations": [
                    {
                        "id": "cfg-001",
                        "name": "My HTTP Extractor",
                        "description": "",
                        "configuration": {"parameters": {"baseUrl": "https://api.example.com"}},
                        "rows": [],
                    }
                ],
            }
        ]
    )
    store = _init_and_pull(tmp_config_dir, project_root, api)

    config_file = _config_file(project_root)
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    data["parameters"]["baseUrl"] = "https://api.example.org"
    config_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    result = _service(store, api).push(alias="prod", project_root=project_root)

    assert result["errors"] == []
    assert result["updated"] == 1
    assert _normalization_warnings(result) == []


# ===================================================================
# The shared helper -- also used by the Phase C variables backfill, which
# re-PUTs the WHOLE body and would otherwise undo push_create's fix
# ===================================================================


def test_guard_script_shape_records_identity_and_normalizes() -> None:
    """The helper normalizes in place and shapes one warning per record."""
    configuration = {
        "parameters": {
            "blocks": [{"name": "B", "codes": [{"name": "C", "script": "SELECT 1;\nSELECT 2;"}]}]
        }
    }
    warnings: list[dict[str, Any]] = []

    out = guard_script_shape(SQL_COMPONENT, configuration, warnings, config_id="cfg-sql")

    assert out["parameters"]["blocks"][0]["codes"][0]["script"] == ["SELECT 1;", "SELECT 2;"]
    assert len(warnings) == 1
    assert warnings[0]["change_type"] == "script_normalization"
    assert warnings[0]["config_id"] == "cfg-sql"
    assert warnings[0]["action"] == "sql_split"


def test_guard_script_shape_is_silent_on_canonical_bodies() -> None:
    """A canonical body produces no records and is returned untouched."""
    configuration = {
        "parameters": {"blocks": [{"name": "B", "codes": [{"name": "C", "script": ["SELECT 1;"]}]}]}
    }
    warnings: list[dict[str, Any]] = []

    out = guard_script_shape(SQL_COMPONENT, configuration, warnings, config_id="cfg-sql")

    assert out["parameters"]["blocks"][0]["codes"][0]["script"] == ["SELECT 1;"]
    assert warnings == []


def test_guard_script_shape_still_normalizes_without_a_warning_sink() -> None:
    """``warnings=None`` drops the records but never the fix itself."""
    configuration = {
        "parameters": {
            "blocks": [{"name": "B", "codes": [{"name": "C", "script": "SELECT 1;\nSELECT 2;"}]}]
        }
    }

    out = guard_script_shape(SQL_COMPONENT, configuration, None)

    assert out["parameters"]["blocks"][0]["codes"][0]["script"] == ["SELECT 1;", "SELECT 2;"]


# ===================================================================
# CLI surfacing (human + JSON), the same channel push warnings use
# ===================================================================


def _push_envelope() -> dict[str, Any]:
    return {
        "status": "pushed",
        "created": 0,
        "updated": 1,
        "deleted": 0,
        "errors": [],
        "warnings": [
            {
                "change_type": "script_normalization",
                "component_id": SQL_COMPONENT,
                "config_id": "cfg-sql",
                "config_path": "",
                "path": "parameters.blocks[0].codes[0].script",
                "action": "sql_split",
                "before_type": "str",
                "after_type": "list",
                "after_length": 2,
                "message": "Normalized keboola.snowflake-transformation/cfg-sql script",
            }
        ],
    }


def _invoke_push(tmp_path: Path, *, json_mode: bool) -> Any:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

    mock_sync = MagicMock()
    mock_sync.push.return_value = _push_envelope()

    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
        patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
    ):
        MockStore.return_value = store
        MockProjService.return_value = ProjectService(config_store=store)
        MockSyncService.return_value = mock_sync
        argv = ["sync", "push", "--project", "prod", "--directory", str(tmp_path)]
        return runner.invoke(app, (["--json"] if json_mode else []) + argv)


def test_cli_push_human_mode_prints_normalization(tmp_path: Path) -> None:
    """Human mode surfaces the record through the shared warnings channel."""
    result = _invoke_push(tmp_path, json_mode=False)
    assert result.exit_code == 0, result.output
    assert "Normalized keboola.snowflake-transformation/cfg-sql script" in result.output


def test_cli_push_json_mode_carries_normalization(tmp_path: Path) -> None:
    """JSON mode carries the structured record on the push envelope."""
    result = _invoke_push(tmp_path, json_mode=True)
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    record = next(w for w in data["warnings"] if w["change_type"] == "script_normalization")
    assert record["action"] == "sql_split"
    assert record["after_length"] == 2
