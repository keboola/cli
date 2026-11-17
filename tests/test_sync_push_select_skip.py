"""``sync push`` warns about statements the transformation runtime skips.

The Snowflake / BigQuery transformation runtime skips a ``script[]`` element
that starts with SELECT, so the job succeeds without running it. Push reports
each such statement of a config it creates or updates as a
``SQL_SELECT_SKIPPED_BY_RUNTIME`` entry (dry run too) and still pushes.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import yaml
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.services.project_service import ProjectService
from keboola_agent_cli.sync.sql_select_skip import SQL_SELECT_SKIPPED_CODE
from test_sync_baseline_stamping import (
    SQL_COMPONENT,
    FakeApi,
    _init_and_pull,
    _service,
    _sql_components,
    _sql_file,
)
from test_sync_cli import TEST_TOKEN, _setup_config

runner = CliRunner()

CREATE = 'CREATE TABLE "a" AS SELECT 1;'


def _select_skips(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        w for w in result.get("companion_warnings", []) if w.get("code") == SQL_SELECT_SKIPPED_CODE
    ]


def _append_select(project_root: Path) -> None:
    sql_file = _sql_file(project_root)
    sql_file.write_text(
        sql_file.read_text(encoding="utf-8").replace(
            CREATE, f"{CREATE}\nSELECT * FROM table_that_does_not_exist;"
        ),
        encoding="utf-8",
    )


def test_dry_run_reports_the_skipped_select(tmp_config_dir: Path, tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components([CREATE]))
    store = _init_and_pull(tmp_config_dir, project_root, api)
    _append_select(project_root)

    result = _service(store, api).push(alias="prod", project_root=project_root, dry_run=True)

    assert result["status"] == "dry_run"
    (warning,) = _select_skips(result)
    assert warning["component_id"] == SQL_COMPONENT
    assert warning["config_id"] == "cfg-sql"
    assert warning["statement"]["index"] == 1
    assert warning["statement"]["excerpt"] == "SELECT * FROM table_that_does_not_exist;"
    assert api.update_calls == []


def test_push_reports_and_still_writes(tmp_config_dir: Path, tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components([CREATE]))
    store = _init_and_pull(tmp_config_dir, project_root, api)
    _append_select(project_root)

    result = _service(store, api).push(alias="prod", project_root=project_root)

    assert result["errors"] == []
    assert result["updated"] == 1
    assert len(_select_skips(result)) == 1
    sent = api.update_calls[-1]["configuration"]["parameters"]["blocks"][0]["codes"][0]
    assert sent["script"][-1] == "SELECT * FROM table_that_does_not_exist;"


def test_unchanged_config_is_not_reported(tmp_config_dir: Path, tmp_path: Path) -> None:
    """Only configs this push writes are checked."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components(["SELECT 1;"]))
    store = _init_and_pull(tmp_config_dir, project_root, api)

    result = _service(store, api).push(alias="prod", project_root=project_root, dry_run=True)

    assert _select_skips(result) == []


def test_cli_moves_the_entry_to_the_envelope(tmp_config_dir: Path, tmp_path: Path) -> None:
    """``--json``: a top-level ``warnings[]`` entry with the code, not a data key."""
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components([CREATE]))
    store = _init_and_pull(tmp_config_dir, project_root, api)
    _append_select(project_root)
    dry = _service(store, api).push(alias="prod", project_root=project_root, dry_run=True)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    cli_store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})
    mock_sync = MagicMock()
    mock_sync.push.return_value = dry

    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
        patch("keboola_agent_cli.cli.SyncService") as MockSyncService,
    ):
        MockStore.return_value = cli_store
        MockProjService.return_value = ProjectService(config_store=cli_store)
        MockSyncService.return_value = mock_sync
        argv = ["--json", "sync", "push", "--project", "prod", "--directory", str(project_root)]
        result = runner.invoke(app, [*argv, "--dry-run"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "companion_warnings" not in payload["data"]
    (warning,) = payload["warnings"]
    assert warning["code"] == SQL_SELECT_SKIPPED_CODE
    assert warning["statement"]["index"] == 1


def test_inline_multi_statement_script_is_scanned_as_sent(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    """A _config.yml with inline SQL (no transform.sql) is split before it is sent.

    The SELECT after the first ``;`` becomes a statement of its own on the
    wire, so the runtime skips it; the scan must see that shape too.
    """
    project_root = tmp_path / "project"
    api = FakeApi(_sql_components([CREATE]))
    store = _init_and_pull(tmp_config_dir, project_root, api)
    sql_file = _sql_file(project_root)
    config_file = sql_file.parent / "_config.yml"
    sql_file.unlink()
    local = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    local["parameters"] = {
        **(local.get("parameters") or {}),
        "blocks": [
            {
                "name": "main",
                "codes": [
                    {
                        "name": "q",
                        "script": [f"{CREATE} SELECT * FROM table_that_does_not_exist;"],
                    }
                ],
            }
        ],
    }
    config_file.write_text(yaml.safe_dump(local), encoding="utf-8")

    result = _service(store, api).push(alias="prod", project_root=project_root, dry_run=True)

    (warning,) = _select_skips(result)
    assert warning["statement"]["block"] == "main"
    assert warning["statement"]["index"] == 1
    assert warning["statement"]["excerpt"] == "SELECT * FROM table_that_does_not_exist;"
