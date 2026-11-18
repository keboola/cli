"""Rows are read once and never written over, like configs (#827 item 8, PR #1005 review).

A row's ``_config.yml`` is read once: the bytes hashed are the bytes parsed
and sent. A file that changes while push reads it is not sent
(``SYNC_LOCAL_FILE_CHANGED``); one edited while the row is written is not
written over (``LOCAL_EDIT_DURING_PUSH``); the manifest stamps the bytes push
wrote (or, when it kept an edit, the bytes it sent) -- never a fresh read.
A row held back because its configuration failed carries the configuration's
own error code.

Real ``SyncService`` on a real tree; only the Storage API is faked.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from formal_sync_fake import FakeStorage, Workspace
from keboola_agent_cli.constants import CONFIG_FILENAME
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.services import _sync_local_snapshot, _sync_push_update


@pytest.fixture
def api() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def ws(tmp_config_dir: Path, tmp_path: Path, api: FakeStorage) -> Workspace:
    return Workspace(tmp_config_dir, tmp_path / "repo", api)


def _pulled_with_row(ws: Workspace, api: FakeStorage) -> tuple[str, Path]:
    cid = api.add("Orders", ["SELECT 1"])
    api.configs[cid]["rows"].append(
        {"id": "500", "name": "Row a", "description": "", "configuration": {"parameters": {}}}
    )
    ws.init_and_pull()
    (row_file,) = (ws.dir_of(cid) / "rows").rglob(CONFIG_FILENAME)
    return cid, row_file


def _set_table(path: Path, value: str) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["parameters"] = {"table": value}
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _remote_table(api: FakeStorage, cid: str, row_id: str) -> Any:
    row = next(r for r in api.configs[cid]["rows"] if r["id"] == row_id)
    return row["configuration"].get("parameters", {}).get("table")


def _row_change(ws: Workspace, row_id: str = "") -> str:
    for change in ws.diff()["changes"]:
        if change.get("is_row") and (not row_id or change["config_id"] == row_id):
            return change["change_type"]
    return "unchanged"


def _new_row(ws: Workspace, cid: str, template: Path, table: str) -> Path:
    new_dir = template.parent.parent / "row-new"
    shutil.copytree(template.parent, new_dir)
    data = yaml.safe_load((new_dir / CONFIG_FILENAME).read_text(encoding="utf-8"))
    data["name"] = "Row new"
    data["parameters"] = {"table": table}
    data["_keboola"].pop("row_id", None)
    (new_dir / CONFIG_FILENAME).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return new_dir / CONFIG_FILENAME


def _flip_during_read(monkeypatch: pytest.MonkeyPatch, row_file: Path, value: str) -> None:
    """An editor saves *row_file* exactly while push reads it."""
    real = _sync_push_update.capture_local_input

    def editor_saves_during_read(*args: Any, **kwargs: Any) -> Any:
        if Path(args[1]) == row_file.parent:
            _set_table(row_file, value)
        return real(*args, **kwargs)

    # The row path reads through the shared one-read helper (before the fix it
    # read the file directly, so this hook never fired and the edit was sent).
    monkeypatch.setattr(_sync_local_snapshot, "capture_local_input", editor_saves_during_read)


# ---------------------------------------------------------------------------
# A row held back by its configuration carries the configuration's code
# ---------------------------------------------------------------------------


def test_a_row_held_by_a_changed_config_file_carries_that_code(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid, row_file = _pulled_with_row(ws, api)
    config_dir = ws.dir_of(cid)
    ws.edit_description(config_dir, "A")
    _set_table(row_file, "z")
    real = _sync_push_update.capture_local_input

    def editor_saves_config_during_read(*args: Any, **kwargs: Any) -> Any:
        if Path(args[1]) == config_dir:
            ws.edit_description(config_dir, "B")
        return real(*args, **kwargs)

    monkeypatch.setattr(_sync_push_update, "capture_local_input", editor_saves_config_during_read)
    result = ws.push()

    (row_error,) = [e for e in result["errors"] if e.get("parent_config_id")]
    assert row_error["error_code"] == ErrorCode.SYNC_LOCAL_FILE_CHANGED
    assert row_error["parent_config_id"] == cid
    assert "SYNC_LOCAL_FILE_CHANGED" in row_error["message"]
    assert _remote_table(api, cid, "500") is None


def test_a_row_held_by_a_server_error_carries_that_code(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid, row_file = _pulled_with_row(ws, api)
    ws.edit_description(ws.dir_of(cid), "A")
    _set_table(row_file, "z")

    def server_error(*_: Any, **__: Any) -> Any:
        raise KeboolaApiError("Internal error", status_code=500, error_code=ErrorCode.API_ERROR)

    monkeypatch.setattr(api, "update_config", server_error)
    result = ws.push()

    (row_error,) = [e for e in result["errors"] if e.get("parent_config_id")]
    assert row_error["error_code"] == ErrorCode.API_ERROR
    assert "API_ERROR" in row_error["message"]
