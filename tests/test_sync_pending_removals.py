"""Two-phase stale-directory removal in ``sync pull`` (#827, item 11).

Pull checkpoints the manifest without a removed config or row (and with a
``pendingRemovals`` record) before it deletes the directory. A crash or a
failed delete leaves the record: diff and push treat the path as absent, and
the next pull finishes the removal first -- only while the directory is
still what pull decided to delete. Process death is injected by wrapping the
journal's ``shutil.rmtree``; ``Crash`` escapes every ``except Exception``.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from formal_sync_fake import CONFIG_FILENAME, Crash, FakeStorage, Workspace
from keboola_agent_cli.services import _sync_pending_removals
from keboola_agent_cli.services._sync_pending_removals import _file_hash, tree_digest
from keboola_agent_cli.sync.manifest import (
    Manifest,
    ManifestPendingRemoval,
    relative_path_problem,
)

ROW = {
    "id": "500",
    "name": "Existing Row",
    "description": "",
    "configuration": {"parameters": {"table": "a"}},
}


@pytest.fixture
def api() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def ws(tmp_config_dir: Path, tmp_path: Path, api: FakeStorage) -> Workspace:
    return Workspace(tmp_config_dir, tmp_path / "proj", api, push_force=True)


def _rmtree_via(monkeypatch: pytest.MonkeyPatch, rmtree: Any) -> None:
    fake = SimpleNamespace(
        **{name: getattr(shutil, name) for name in dir(shutil) if not name.startswith("__")}
    )
    fake.rmtree = rmtree
    monkeypatch.setattr(_sync_pending_removals, "shutil", fake)


def _die(*_: Any, **__: Any) -> None:
    raise Crash("killed right before the delete")


def _crashed_before_rmtree(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, Path]:
    """A pulled config trashed in the UI; the pull dies after its checkpoint, before the delete."""
    cid = api.add("Orders", ["SELECT 1;"])
    ws.init_and_pull()
    path = ws.dir_of(cid)
    api.ui_trash(cid)
    _rmtree_via(monkeypatch, _die)
    with pytest.raises(Crash):
        ws.pull()
    monkeypatch.undo()
    assert path.is_dir()
    assert ws.entry(cid) is None  # the checkpoint dropped the entry
    (item,) = ws.manifest().pending_removals
    assert (item.kind, item.config_id) == ("config", cid)
    return cid, path


def _no_change_at(diff: dict[str, Any], rel: str) -> bool:
    return not [c for c in diff["changes"] if c.get("path", "").startswith(rel)]


# ------------------------------------------------------------------- R14


@pytest.mark.parametrize("carries_id", [True, False], ids=["with_config_id", "without_config_id"])
def test_a_pending_directory_is_neither_created_nor_adopted(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch, carries_id: bool
) -> None:
    cid, path = _crashed_before_rmtree(ws, api, monkeypatch)
    if not carries_id:
        data = ws.read_yaml(path)
        data["_keboola"].pop("config_id")
        ws.write_yaml(path, data)
    api.ui_restore(cid)
    rel = path.relative_to(ws.root / "main").as_posix()

    assert _no_change_at(ws.diff(), rel)
    calls = len(api.calls)
    ws.push()
    assert [c for c in api.calls[calls:] if c[0] not in ("list", "get_config_detail")] == []


def test_a_pending_directory_is_no_move_target(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = api.add("Customers", ["SELECT 2;"])
    _cid, path = _crashed_before_rmtree(ws, api, monkeypatch)
    other_dir = ws.dir_of(other)
    data = ws.read_yaml(path)
    data["_keboola"]["config_id"] = other  # the pending dir now carries the other id
    ws.write_yaml(path, data)
    shutil.rmtree(other_dir)

    diff = ws.diff()

    assert not diff.get("moves"), diff
    (change,) = [c for c in diff["changes"] if c["config_id"] == other]
    assert change["change_type"] == "deleted"  # its folder is gone, not moved
    entry = ws.entry(other)
    assert entry is not None
    assert ws.root / "main" / entry.path == other_dir


def test_a_pending_row_directory_is_no_new_row(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = api.add("Orders", ["SELECT 1;"])
    api.configs[cid]["rows"].append(dict(ROW))
    ws.init_and_pull()
    api.configs[cid]["rows"] = []  # the row is deleted in the UI
    _rmtree_via(monkeypatch, _die)
    with pytest.raises(Crash):
        ws.pull()
    monkeypatch.undo()
    (item,) = ws.manifest().pending_removals
    assert (item.kind, item.row_id) == ("row", "500")
    assert (ws.root / "main" / item.path / CONFIG_FILENAME).is_file()

    assert not [c for c in ws.diff()["changes"] if c.get("is_row")]


# ------------------------------------------------------------------- R15


def test_a_pending_directory_edited_after_the_checkpoint_is_kept(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cid, path = _crashed_before_rmtree(ws, api, monkeypatch)
    ws.edit_sql(path, "SELECT 'edited after the crash';\n")

    result = ws.pull()

    assert (path / "transform.sql").read_text(encoding="utf-8").startswith("SELECT 'edited")
    assert [w["code"] for w in result["warnings"]] == ["PENDING_REMOVAL_KEPT"]
    assert ws.manifest().pending_removals == []


def test_a_pending_directory_with_a_symlink_is_kept(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _cid, path = _crashed_before_rmtree(ws, api, monkeypatch)
    target = tmp_path / "elsewhere.txt"
    target.write_text("keep me", encoding="utf-8")
    try:
        os.symlink(target, path / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("no symlink support")

    result = ws.pull()

    assert path.is_dir() and target.is_file()
    assert [w["code"] for w in result["warnings"]] == ["PENDING_REMOVAL_KEPT"]


# ------------------------------------------------------------------- R16


def test_recovery_runs_before_a_new_config_gets_a_path(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The finished removal frees the path, so a same-named new config gets it unsuffixed."""
    _cid, path = _crashed_before_rmtree(ws, api, monkeypatch)
    new_id = api.add("Orders", ["SELECT 'new';"])

    ws.pull()

    entry = ws.entry(new_id)
    assert entry is not None
    assert ws.root / "main" / entry.path == path
    assert ws.read_yaml(path)["_keboola"]["config_id"] == new_id
    assert ws.manifest().pending_removals == []


def test_a_failed_delete_stays_pending_until_a_pull_deletes_it(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = api.add("Orders", ["SELECT 1;"])
    ws.init_and_pull()
    path = ws.dir_of(cid)
    api.ui_trash(cid)

    def refuse(*_: Any, **__: Any) -> None:
        raise PermissionError("busy")

    _rmtree_via(monkeypatch, refuse)
    result = ws.pull()
    monkeypatch.undo()

    assert [w["code"] for w in result["warnings"]] == ["PENDING_REMOVAL_FAILED"]
    assert path.is_dir() and ws.entry(cid) is None
    assert [i.path for i in ws.manifest().pending_removals] == [
        path.relative_to(ws.root / "main").as_posix()
    ]
    assert _no_change_at(ws.diff(), path.relative_to(ws.root / "main").as_posix())

    ws.pull()

    assert not path.exists()
    assert ws.manifest().pending_removals == []


def test_a_manifest_without_pending_removals_writes_no_key(ws: Workspace, api: FakeStorage) -> None:
    api.add("Orders", ["SELECT 1;"])
    ws.init_and_pull()
    raw = json.loads((ws.root / ".keboola" / "manifest.json").read_text(encoding="utf-8"))
    assert "pendingRemovals" not in raw
    assert Manifest.model_validate(raw).pending_removals == []


def test_an_older_kbagent_keeps_the_key_it_does_not_know(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``extra="allow"``: a model without the field round-trips the key untouched."""
    _crashed_before_rmtree(ws, api, monkeypatch)
    raw = json.loads((ws.root / ".keboola" / "manifest.json").read_text(encoding="utf-8"))

    class OldManifest(BaseModel):  # the manifest model before #827, reduced
        model_config = ConfigDict(populate_by_name=True, extra="allow")

        version: int

    old = OldManifest.model_validate(raw)

    assert old.model_dump(mode="json", by_alias=True)["pendingRemovals"] == raw["pendingRemovals"]


# ------------------------------------------------------------------- R17


def test_a_row_restored_after_a_crashed_row_removal_is_not_deleted(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = api.add("Orders", ["SELECT 1;"])
    api.configs[cid]["rows"].append(dict(ROW))
    ws.init_and_pull()
    api.configs[cid]["rows"] = []
    real = shutil.rmtree

    def delete_then_die(*args: Any, **kwargs: Any) -> None:
        real(*args, **kwargs)
        raise Crash("killed right after the row delete")

    _rmtree_via(monkeypatch, delete_then_die)
    with pytest.raises(Crash):
        ws.pull()
    monkeypatch.undo()
    entry = ws.entry(cid)
    assert entry is not None and entry.rows == []  # checkpointed without the row

    api.configs[cid]["rows"].append(dict(ROW))  # restored in the UI
    api.ui_edit_row(cid)
    result = ws.push()  # push --force

    assert ("delete_config_row", "500") not in api.calls, result
    assert [r["id"] for r in api.configs[cid]["rows"]] == ["500"]


# ------------------------------------------------------------------- round 2


def _plant(ws: Workspace, record: dict[str, Any]) -> None:
    """Write *record* into ``pendingRemovals`` by hand (a crafted or a stale manifest)."""
    manifest_file = ws.root / ".keboola" / "manifest.json"
    raw = json.loads(manifest_file.read_text(encoding="utf-8"))
    raw["pendingRemovals"] = [record]
    manifest_file.write_text(json.dumps(raw), encoding="utf-8")


def _victim(tmp_path: Path) -> Path:
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "notes.txt").write_text("precious", encoding="utf-8")
    return victim


@pytest.mark.parametrize(
    ("tree", "path"),
    [
        ("main", "../../victim"),
        ("main", "ABSOLUTE"),
        ("main", "C:/victim"),
        ("main", "//server/share/victim"),
        ("main", "a/./b"),
        ("main", ""),
        ("../..", "victim"),
        ("elsewhere", "victim"),
    ],
    ids=[
        "dotdot",
        "absolute",
        "drive",
        "unc",
        "dot_segment",
        "empty",
        "tree_dotdot",
        "tree_unknown",
    ],
)
def test_a_pending_removal_outside_its_tree_is_never_deleted(
    ws: Workspace, api: FakeStorage, tmp_path: Path, tree: str, path: str
) -> None:
    """F1: an unsafe record is dropped with a warning and nothing is deleted."""
    api.add("Orders", ["SELECT 1;"])
    ws.init_and_pull()
    victim = _victim(tmp_path)
    if path == "ABSOLUTE":
        path = victim.as_posix()
    _plant(
        ws,
        {
            "tree": tree,
            "path": path,
            "kind": "config",
            "componentId": "x",
            "configId": "1",
            "fileHash": "",
            "treeHash": tree_digest(victim),
        },
    )

    result = ws.pull()

    assert (victim / "notes.txt").is_file()
    assert [w["code"] for w in result["warnings"]] == ["PENDING_REMOVAL_INVALID"], result
    assert ws.manifest().pending_removals == []


def test_a_pending_removal_through_a_symlinked_ancestor_is_never_deleted(
    ws: Workspace, api: FakeStorage, tmp_path: Path
) -> None:
    """F1: every directory from the project root down to the target is checked for a link."""
    api.add("Orders", ["SELECT 1;"])
    ws.init_and_pull()
    outside = tmp_path / "outside"
    victim = outside / "victim"
    victim.mkdir(parents=True)
    (victim / "notes.txt").write_text("precious", encoding="utf-8")
    try:
        os.symlink(outside, ws.root / "main" / "linked", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlink support")
    _plant(
        ws,
        {
            "tree": "main",
            "path": "linked/victim",
            "kind": "config",
            "componentId": "x",
            "configId": "1",
            "fileHash": "",
            "treeHash": tree_digest(victim),
        },
    )

    result = ws.pull()

    assert (victim / "notes.txt").is_file()
    assert "PENDING_REMOVAL_INVALID" in [w["code"] for w in result["warnings"]]


def test_a_row_record_outside_its_rows_folder_is_never_deleted(
    ws: Workspace, api: FakeStorage
) -> None:
    """F1: a ``row`` record must name a folder under its configuration's rows folder."""
    cid = api.add("Orders", ["SELECT 1;"])
    ws.init_and_pull()
    d = ws.dir_of(cid)
    rel = d.relative_to(ws.root / "main").as_posix()
    _plant(
        ws,
        {
            "tree": "main",
            "path": rel,
            "kind": "row",
            "componentId": "keboola.snowflake-transformation",
            "configId": cid,
            "rowId": "1",
            "fileHash": _file_hash(d),
            "treeHash": tree_digest(d),
        },
    )

    result = ws.pull()

    assert (d / CONFIG_FILENAME).is_file()
    assert [w["code"] for w in result["warnings"]] == ["PENDING_REMOVAL_INVALID"]


def test_the_empty_parent_cleanup_stops_at_the_tree(tmp_path: Path) -> None:
    """F1: ``_remove`` never removes the tree folder or anything above it."""
    tree_dir = tmp_path / "main"
    (tree_dir / "a" / "b").mkdir(parents=True)
    item = ManifestPendingRemoval(tree="main", path="a/b", kind="config")

    _sync_pending_removals._remove(tree_dir, item)

    assert tree_dir.is_dir() and not (tree_dir / "a").exists()


def test_relative_path_problem_refuses_every_escape() -> None:
    for bad in ("", "/x", "//srv/x", "C:/x", "a/C:x", "..", "a/../b", "a/./b", "a//b"):
        assert relative_path_problem(bad) is not None, bad
    assert relative_path_problem("transformation/x/orders") is None


def test_a_recovery_delete_that_fails_again_stays_pending(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2 (R16): a record whose delete fails in recovery survives the pull's final save."""

    def refuse(*_: Any, **__: Any) -> None:
        raise PermissionError("busy")

    cid = api.add("Orders", ["SELECT 1;"])
    ws.init_and_pull()
    path = ws.dir_of(cid)
    api.ui_trash(cid)
    _rmtree_via(monkeypatch, refuse)
    ws.pull()
    assert len(ws.manifest().pending_removals) == 1
    second = ws.pull()
    monkeypatch.undo()

    assert path.is_dir()
    assert [w["code"] for w in second["warnings"]] == ["PENDING_REMOVAL_FAILED"], second
    assert len(ws.manifest().pending_removals) == 1
    assert [c for c in ws.diff()["changes"] if c["change_type"] == "added"] == []


def test_a_pending_row_directory_is_not_adopted_by_pull(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F5: a restored row is never written into a row folder whose delete is still pending."""

    def refuse(*_: Any, **__: Any) -> None:
        raise PermissionError("busy")

    cid = api.add("Orders", ["SELECT 1;"])
    api.configs[cid]["rows"].append(dict(ROW))
    ws.init_and_pull()
    api.configs[cid]["rows"] = []
    _rmtree_via(monkeypatch, refuse)
    ws.pull()  # the row delete fails: recorded
    (item,) = ws.manifest().pending_removals
    api.configs[cid]["rows"].append(dict(ROW))  # restored in the UI
    api.ui_edit_row(cid)
    ws.pull()  # recovery fails again
    monkeypatch.undo()

    entry = ws.entry(cid)
    assert entry is not None
    (row,) = entry.rows
    assert f"{entry.path}/{row.path}" != item.path
    assert [i.path for i in ws.manifest().pending_removals] == [item.path]


def test_recovery_keeps_a_pending_row_folder_that_is_tracked_again(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F5: a folder an older kbagent tracked again as the row is not deleted by recovery."""
    cid = api.add("Orders", ["SELECT 1;"])
    api.configs[cid]["rows"].append(dict(ROW))
    ws.init_and_pull()
    api.configs[cid]["rows"] = []
    _rmtree_via(monkeypatch, _die)
    with pytest.raises(Crash):
        ws.pull()
    monkeypatch.undo()
    (item,) = ws.manifest().pending_removals
    manifest_file = ws.root / ".keboola" / "manifest.json"
    raw = json.loads(manifest_file.read_text(encoding="utf-8"))
    (cfg,) = [c for c in raw["configurations"] if c["id"] == cid]
    cfg["rows"] = [{"id": "500", "path": item.path.removeprefix(f"{cfg['path']}/")}]
    manifest_file.write_text(json.dumps(raw), encoding="utf-8")
    api.configs[cid]["rows"].append(dict(ROW))  # live again

    result = ws.pull()

    assert (ws.root / "main" / item.path / CONFIG_FILENAME).is_file()
    assert "PENDING_REMOVAL_KEPT" in [w["code"] for w in result["warnings"]], result


def test_a_folder_edited_between_planning_and_delete_is_kept(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6: the commit checks each folder again right before it deletes it."""
    cid = api.add("Orders", ["SELECT 1;"])
    ws.init_and_pull()
    path = ws.dir_of(cid)
    api.ui_trash(cid)
    real = _sync_pending_removals.save_manifest

    def edit_then_checkpoint(*args: Any, **kwargs: Any) -> None:
        ws.edit_sql(path, "SELECT 'saved while pull ran';\n")
        real(*args, **kwargs)

    monkeypatch.setattr(_sync_pending_removals, "save_manifest", edit_then_checkpoint)
    result = ws.pull()
    monkeypatch.undo()

    assert (path / "transform.sql").read_text(encoding="utf-8").startswith("SELECT 'saved")
    assert [w["code"] for w in result["warnings"]] == ["PENDING_REMOVAL_KEPT"], result
    assert ws.manifest().pending_removals == []


def test_a_dry_run_pull_lists_the_records_it_did_not_process(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F9c: a dry run changes nothing and says which records a real pull would process."""
    _cid, path = _crashed_before_rmtree(ws, api, monkeypatch)

    result = ws.pull(dry_run=True)

    assert path.is_dir()
    assert [w["code"] for w in result["warnings"]] == ["PENDING_REMOVALS_NOT_PROCESSED"]
    assert len(ws.manifest().pending_removals) == 1
