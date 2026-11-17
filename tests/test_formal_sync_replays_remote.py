"""Replays of the formal sync model's "remote side" interleavings (#827, findings D-H, N, O).

Each test re-enacts one counterexample of the formal sync model (``Sync.tla``,
keboola/cli#827) against the REAL ``SyncService`` (push / pull / diff on a
real working tree in ``tmp_path``); only the Storage API client is faked
(``formal_sync_fake``). The interleavings -- a human acting in the Keboola UI
while a ``sync push`` is between its diff and its write, a lost response, a
crash before the manifest is saved -- are forced with the fake's one-shot
hooks. Every test asserts the intended property, and runs for a plain
``sync push`` and for ``sync push --force``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from formal_sync_fake import CONFIG_FILENAME, Crash, FakeStorage, Workspace

ORIGINAL = ["SELECT 1"]
UI_SCRIPT = ["SELECT 'edited in the UI'"]


@pytest.fixture(params=[False, True], ids=["push", "push_force"])
def push_force(request: pytest.FixtureRequest) -> bool:
    return bool(request.param)


def _pulled(
    tmp_config_dir: Path, tmp_path: Path, api: FakeStorage, push_force: bool
) -> tuple[Workspace, str]:
    """One tracked SQL config, freshly pulled."""
    cid = api.add("Orders", ORIGINAL)
    ws = Workspace(tmp_config_dir, tmp_path / "repo", api, push_force=push_force)
    ws.init_and_pull()
    assert ws.entry(cid) is not None
    return ws, cid


# --------------------------------------------------------------------------- D


def test_d_ui_edit_between_diff_and_put_is_not_overwritten(
    tmp_config_dir: Path, tmp_path: Path, push_force: bool
) -> None:
    """Finding D: a UI edit landing between push's diff and its PUT survives.

    The user edits ``transform.sql``; push diffs the config as ``modified``;
    a human edits the description in the UI; the PUT carries the version the
    diff read (``If-Match``), so the Storage API refuses it (412) and push
    reports a conflict instead of overwriting the UI edit.
    """
    api = FakeStorage()
    ws, cid = _pulled(tmp_config_dir, tmp_path, api, push_force)
    d = ws.dir_of(cid)
    ws.edit_sql(d, "SELECT 2;\n")
    entry_before = ws.entry(cid)
    file_before = (d / CONFIG_FILENAME).read_bytes()

    api.on("update_config", "before", lambda _cid: api.ui_describe(cid, "set in the UI"))
    result = ws.push()

    assert api.configs[cid]["description"] == "set in the UI", result
    assert [e["error_code"] for e in result["errors"]] == ["SYNC_CONFLICT"], result
    assert "kbagent sync pull" in result["errors"][0]["message"]
    assert result["updated"] == 0
    # The local file and the manifest entry are untouched.
    assert (d / CONFIG_FILENAME).read_bytes() == file_before
    assert ws.entry(cid) == entry_before
    assert (d / "transform.sql").read_text(encoding="utf-8") == "SELECT 2;\n"


# --------------------------------------------------------------------------- E


def test_e_delete_of_ui_edited_config_reports_conflict(
    tmp_config_dir: Path, tmp_path: Path, push_force: bool
) -> None:
    """Finding E: a config edited in the UI since the last pull is not deleted."""
    api = FakeStorage()
    ws, cid = _pulled(tmp_config_dir, tmp_path, api, push_force)
    shutil.rmtree(ws.dir_of(cid))
    api.ui_edit(cid, UI_SCRIPT)

    result = ws.push()

    assert api.configs[cid]["state"] == "live", result


# --------------------------------------------------------------------------- F


def test_f_adopt_by_id_after_crash_does_not_overwrite_ui_edit(
    tmp_config_dir: Path, tmp_path: Path, push_force: bool
) -> None:
    """Finding F: the re-run after a crash between two creates does not PUT over a UI edit."""
    api = FakeStorage()
    ws, cid = _pulled(tmp_config_dir, tmp_path, api, push_force)
    ws.new_local_config(ws.dir_of(cid), "Orders Copy A")
    ws.new_local_config(ws.dir_of(cid), "Orders Copy B")

    def die(_name: str) -> None:
        raise Crash

    api.on("create_config", "before", lambda _name: None)  # first POST goes through
    api.on("create_config", "before", die)  # process dies before the 2nd POST
    with pytest.raises(Crash):
        ws.push()

    created = [c for c in api.live() if c["id"] != cid]
    assert len(created) == 1
    new_id = created[0]["id"]

    api.ui_edit(new_id, UI_SCRIPT)
    result = ws.push()

    assert api.script(new_id) == UI_SCRIPT, result


def test_f2_crash_between_post_and_checkpoint_does_not_overwrite_ui_edit(
    tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, push_force: bool
) -> None:
    """Finding F at the create checkpoint: only ``pending_creates`` knows the id."""
    from keboola_agent_cli.services import _sync_push_create

    api = FakeStorage()
    ws, cid = _pulled(tmp_config_dir, tmp_path, api, push_force)
    ws.new_local_config(ws.dir_of(cid), "Orders Copy A")
    real = _sync_push_create.save_manifest

    def guarded(*a, **kw):  # type: ignore[no-untyped-def]
        if any(c[0] == "create_config" for c in api.calls):
            raise Crash
        real(*a, **kw)

    monkeypatch.setattr(_sync_push_create, "save_manifest", guarded)
    with pytest.raises(Crash):
        ws.push()
    monkeypatch.undo()

    created = [c for c in api.live() if c["id"] != cid]
    assert len(created) == 1
    new_id = created[0]["id"]
    api.ui_edit(new_id, UI_SCRIPT)
    result = ws.push()

    assert api.script(new_id) == UI_SCRIPT, result
    assert len([c for c in api.live() if c["id"] != cid]) == 1, result


# --------------------------------------------------------------------------- H


def test_h_ui_trash_between_diff_and_delete_is_not_purged(
    tmp_config_dir: Path, tmp_path: Path, push_force: bool
) -> None:
    """Finding H: a config trashed in the UI between diff and DELETE is never purged."""
    api = FakeStorage()
    ws, cid = _pulled(tmp_config_dir, tmp_path, api, push_force)
    shutil.rmtree(ws.dir_of(cid))

    api.on("delete_config", "before", lambda _cid: api.ui_trash(cid))
    result = ws.push()

    assert cid not in api.purged, result


# --------------------------------------------------------------------------- N


def test_n_push_does_not_recreate_config_trashed_in_ui(
    tmp_config_dir: Path, tmp_path: Path, push_force: bool
) -> None:
    """Finding N: push never re-creates a config deleted in the UI with no local edit."""
    api = FakeStorage()
    ws, cid = _pulled(tmp_config_dir, tmp_path, api, push_force)
    api.ui_trash(cid)

    result = ws.push()

    assert not [c for c in api.calls if c[0] == "create_config"], result


# --------------------------------------------------------------------------- O


def test_o_unparseable_config_yaml_is_not_deleted_remotely(
    tmp_config_dir: Path, tmp_path: Path, push_force: bool
) -> None:
    """Finding O: an unparseable but present ``_config.yml`` never becomes a remote DELETE."""
    api = FakeStorage()
    ws, cid = _pulled(tmp_config_dir, tmp_path, api, push_force)
    d = ws.dir_of(cid)
    (d / CONFIG_FILENAME).write_text("name: [unclosed\n", encoding="utf-8")

    result = ws.push()

    assert api.configs[cid]["state"] == "live", result
