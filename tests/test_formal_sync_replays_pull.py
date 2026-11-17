"""Replays of the formal sync model's pull interleavings (#827, findings I-L, P-R).

Each test re-enacts one counterexample of the formal sync model against the
REAL ``SyncService`` -- real ``pull`` / ``push`` / ``diff`` on a real working
tree in ``tmp_path``. Only the Storage API client is faked
(``tests/formal_sync_fake.py``). Process death is injected by wrapping a
function pull calls at the exact step (``_rename_directory``,
``shutil.rmtree``): the wrapper does the real work, then raises ``Crash``,
which escapes every ``except Exception`` just like SIGKILL.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from formal_sync_fake import Crash, FakeStorage, Workspace, timeout_error
from keboola_agent_cli.constants import CONFIG_FILENAME
from keboola_agent_cli.errors import SyncConflictError
from keboola_agent_cli.services import _sync_stale as _sync_pending_removals
from keboola_agent_cli.services.sync_service import SyncService

PULLED_SQL = ["SELECT 1;"]
LOCAL_SQL = "SELECT 'local edit, never pushed';\n"
LOCAL_DESCRIPTION = "local edit, never pushed"


def _text_survives(root: Path, needle: str) -> bool:
    """True iff some file in the working tree (outside .keboola/) still holds *needle*."""
    for path in root.rglob("*"):
        if (
            path.is_file()
            and ".keboola" not in path.parts
            and needle in path.read_text(encoding="utf-8", errors="replace")
        ):
            return True
    return False


def _crash_after_first(real: Any) -> Any:
    """Wrap *real*: the first call does the real work and then the process dies."""
    state = {"fired": False}

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        if not state["fired"]:
            state["fired"] = True
            raise Crash("kbagent killed right after this step")
        return result

    return wrapper


def _crash_before_first(real: Any) -> Any:
    """Wrap *real*: the process dies right before its first call."""
    state = {"fired": False}

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not state["fired"]:
            state["fired"] = True
            raise Crash("kbagent killed right before this step")
        return real(*args, **kwargs)

    return wrapper


def patch_rmtree(monkeypatch: pytest.MonkeyPatch, wrapper: Any) -> None:
    """Route the stale sweep's ``shutil.rmtree`` through *wrapper*."""
    fake_shutil = SimpleNamespace(
        **{name: getattr(shutil, name) for name in dir(shutil) if not name.startswith("__")}
    )
    fake_shutil.rmtree = wrapper(shutil.rmtree)
    monkeypatch.setattr(_sync_pending_removals, "shutil", fake_shutil)


@pytest.fixture
def api() -> FakeStorage:
    return FakeStorage()


@pytest.fixture(params=[False, True], ids=["push", "push_force"])
def ws(
    request: pytest.FixtureRequest, tmp_config_dir: Path, tmp_path: Path, api: FakeStorage
) -> Workspace:
    return Workspace(tmp_config_dir, tmp_path / "proj", api, push_force=bool(request.param))


# --------------------------------------------------------------------------- I
@pytest.mark.parametrize("force", [False, True], ids=["plain_pull", "force_pull"])
def test_I_companion_edit_still_reported_after_pull(
    ws: Workspace, api: FakeStorage, force: bool
) -> None:
    """Finding I: a transform.sql-only edit stays visible after a pull with the remote unchanged."""
    cid = api.add("Orders", PULLED_SQL)
    ws.init_and_pull()
    d1 = ws.dir_of(cid)

    ws.edit_sql(d1, LOCAL_SQL)
    assert ws.change_of(cid) == "modified"

    ws.pull(force=force)
    assert (d1 / "transform.sql").read_text(encoding="utf-8") == LOCAL_SQL
    assert api.script(cid) == PULLED_SQL

    assert ws.change_of(cid) in ("modified", "conflict")


# --------------------------------------------------------------------------- J
def test_J_companion_edit_after_push_create_is_reported(ws: Workspace, api: FakeStorage) -> None:
    """Finding J: a companion edit of a push-created config is reported."""
    cid = api.add("Orders", PULLED_SQL)
    ws.init_and_pull()
    d2 = ws.new_local_config(ws.dir_of(cid), "Fresh One")

    result = ws.push()
    created = [c for c in api.live() if c["name"] == "Fresh One"]
    assert len(created) == 1, result
    new_id = created[0]["id"]
    assert ws.change_of(new_id) == "unchanged"

    ws.edit_sql(d2, LOCAL_SQL)

    assert ws.change_of(new_id) in ("modified", "conflict")


# --------------------------------------------------------------------------- K
@pytest.mark.parametrize("force", [False, True], ids=["plain_pull", "force_pull"])
def test_K_pull_does_not_silently_overwrite_companion_edit(
    ws: Workspace, api: FakeStorage, force: bool
) -> None:
    """Finding K: remote changed + local transform.sql edit: pull never destroys the edit."""
    cid = api.add("Orders", PULLED_SQL)
    ws.init_and_pull()
    d1 = ws.dir_of(cid)

    ws.edit_sql(d1, LOCAL_SQL)
    api.ui_edit(cid, ["SELECT 'ui edit';"])

    try:
        ws.pull(force=force)
    except SyncConflictError:
        return  # refusing to overwrite is acceptable

    assert _text_survives(ws.root, "local edit, never pushed")


# --------------------------------------------------------------------------- L
@pytest.mark.parametrize("edited", ["config_yml", "transform_sql"])
def test_L_pull_does_not_destroy_edit_of_remotely_deleted_config(
    ws: Workspace, api: FakeStorage, edited: str
) -> None:
    """Finding L: local edit + UI trash: pull keeps the directory holding the edit."""
    cid = api.add("Orders", PULLED_SQL)
    ws.init_and_pull()
    d1 = ws.dir_of(cid)

    if edited == "config_yml":
        ws.edit_description(d1, LOCAL_DESCRIPTION)
    else:
        ws.edit_sql(d1, LOCAL_SQL)
    api.ui_trash(cid)

    try:
        ws.pull()
    except SyncConflictError:
        return

    assert _text_survives(ws.root, "local edit, never pushed")


# --------------------------------------------------------------------------- P
def test_P_pull_crash_after_rename_then_push_keeps_identity(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding P: a crash between pull's dir rename and its save; push neither creates nor deletes."""
    cid = api.add("Orders", PULLED_SQL)
    ws.init_and_pull()
    assert ws.dir_of(cid).name == "orders"

    api.ui_rename(cid, "Orders v2")
    monkeypatch.setattr(
        SyncService, "_rename_directory", _crash_after_first(SyncService._rename_directory)
    )
    with pytest.raises(Crash):
        ws.pull()
    monkeypatch.undo()

    ws.push()

    created = [c for c in api.calls if c[0] == "create_config"]
    deleted = [c for c in api.calls if c[0] == "delete_config"]
    assert (created, deleted) == ([], [])
    assert api.configs[cid]["state"] == "live"


# --------------------------------------------------------------------------- Q
@pytest.mark.parametrize("route", ["ui_delete_recreate", "lost_post_response"])
def test_Q_pull_path_reuse_keeps_new_config_dir(
    ws: Workspace, api: FakeStorage, route: str
) -> None:
    """Finding Q: a config re-created under a removed config's name keeps its directory."""
    cid = api.add("Orders", PULLED_SQL)
    ws.init_and_pull()

    api.ui_trash(cid)
    if route == "ui_delete_recreate":
        new_id = api.add("Orders", ["SELECT 2;"])
    else:
        api.on("create_config", "after", lambda _cid: (_ for _ in ()).throw(timeout_error()))
        pushed = ws.push()
        # Push classifies the UI-trashed tracked config remote_deleted and sends no POST.
        assert not any(c[0] == "create_config" for c in api.calls), pushed
        return
    assert new_id != cid

    ws.pull()
    entry = ws.entry(new_id)
    assert entry is not None

    calls_before = len(api.calls)
    ws.push()
    deletes = [c for c in api.calls[calls_before:] if c[0] == "delete_config"]

    assert (ws.root / "main" / entry.path / CONFIG_FILENAME).exists() and deletes == []


# --------------------------------------------------------------------------- R
def test_R_pull_crash_after_rmtree_leaves_no_stale_entry(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding R: a crash between pull's rmtree and its final save leaves no stale entry.

    Pull saves the manifest without the removed config (and with a
    ``pending_removals`` record) BEFORE it deletes the directory. Restoring
    the config in the UI afterwards must not make push delete it again.
    """
    cid = api.add("Orders", PULLED_SQL)
    ws.init_and_pull()

    api.ui_trash(cid)
    patch_rmtree(monkeypatch, _crash_after_first)
    with pytest.raises(Crash):
        ws.pull()
    monkeypatch.undo()

    missing = [
        e.path for e in ws.manifest().configurations if not (ws.root / "main" / e.path).is_dir()
    ]
    assert missing == []

    api.ui_restore(cid)
    result = ws.push()
    assert not [c for c in api.calls if c[0] == "delete_config"], result
    assert api.configs[cid]["state"] == "live"
    ws.pull()
    assert ws.entry(cid) is not None
