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

from formal_sync_fake import CONFIG_FILENAME, Crash, FakeStorage, Pulled, Workspace
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


def _removal_via(monkeypatch: pytest.MonkeyPatch, move: Any) -> None:
    """Route the removal's move into the quarantine (the step that takes a folder away)."""
    monkeypatch.setattr(_sync_pending_removals, "_move_aside", move)


def _quarantine_rmtree_via(monkeypatch: pytest.MonkeyPatch, rmtree: Any) -> None:
    """Route the delete of a folder already moved into the quarantine."""
    fake = SimpleNamespace(
        **{name: getattr(shutil, name) for name in dir(shutil) if not name.startswith("__")}
    )
    fake.rmtree = rmtree
    monkeypatch.setattr(_sync_pending_removals, "shutil", fake)


def _die(*_: Any, **__: Any) -> None:
    raise Crash("killed right before the delete")


def _crashed_before_rmtree(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> Pulled:
    """A pulled config trashed in the UI; the pull dies after its checkpoint, before the delete."""
    cid = api.add("Orders", ["SELECT 1;"])
    ws.init_and_pull()
    path = ws.dir_of(cid)
    api.ui_trash(cid)
    _removal_via(monkeypatch, _die)
    with pytest.raises(Crash):
        ws.pull()
    monkeypatch.undo()
    assert path.is_dir()
    assert ws.entry(cid) is None  # the checkpoint dropped the entry
    (item,) = ws.manifest().pending_removals
    assert (item.kind, item.config_id) == ("config", cid)
    return Pulled(cid, path)


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
    _removal_via(monkeypatch, _die)
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
    # Kept, so still recorded (G1): diff and push never re-create it from the folder.
    assert len(ws.manifest().pending_removals) == 1
    assert _no_change_at(ws.diff(), path.relative_to(ws.root / "main").as_posix())


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

    _removal_via(monkeypatch, refuse)
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
    real = _sync_pending_removals._move_aside

    def delete_then_die(*args: Any, **kwargs: Any) -> None:
        real(*args, **kwargs)
        raise Crash("killed right after the row delete")

    _removal_via(monkeypatch, delete_then_die)
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
    # A valid record reached through a link: not deleted, and still recorded (H2).
    assert "PENDING_REMOVAL_KEPT" in [w["code"] for w in result["warnings"]]
    assert [i.path for i in ws.manifest().pending_removals] == ["linked/victim"]


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

    _sync_pending_removals._remove(tmp_path, item)

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
    _removal_via(monkeypatch, refuse)
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
    _removal_via(monkeypatch, refuse)
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
    _removal_via(monkeypatch, _die)
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
    # G1: a folder kept at commit time keeps its record -- its manifest entry is gone.
    assert ws.entry(cid) is None
    assert len(ws.manifest().pending_removals) == 1
    calls = len(api.calls)
    ws.push()
    assert not [c for c in api.calls[calls:] if c[0] == "create_config"]


def test_a_dry_run_pull_lists_the_records_it_did_not_process(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F9c: a dry run changes nothing and says which records a real pull would process."""
    _cid, path = _crashed_before_rmtree(ws, api, monkeypatch)

    result = ws.pull(dry_run=True)

    assert path.is_dir()
    assert [w["code"] for w in result["warnings"]] == ["PENDING_REMOVALS_NOT_PROCESSED"]
    assert len(ws.manifest().pending_removals) == 1


# ------------------------------------------------------------------- round 3


def test_nested_removals_with_the_inner_one_listed_first_delete_both(tmp_path: Path) -> None:
    """G1: the inner folder's delete must not be taken for an edit of the outer one."""
    from keboola_agent_cli.sync.manifest import load_manifest, save_manifest
    from test_sync_untracked_rows import HTTP_DIR, _dir, _nest_config_b_in_a, _pull, _world

    sim = _world(tmp_path)
    child = _nest_config_b_in_a(sim)
    manifest = load_manifest(sim.root)
    manifest.configurations.sort(key=lambda c: c.id != "1003")  # the child first
    save_manifest(sim.root, manifest)

    result = _pull(sim)

    assert not child.exists()
    assert not _dir(sim, HTTP_DIR).exists()
    assert "PENDING_REMOVAL_KEPT" not in [w.get("code") for w in result.get("warnings", [])]
    assert load_manifest(sim.root).pending_removals == []


# ------------------------------------------------------------------- round 4


def test_a_pending_outer_folder_does_not_hide_a_tracked_config_inside_it(
    ws: Workspace, api: FakeStorage
) -> None:
    """H1: a new row of a tracked config inside a still-recorded folder is pushed."""
    outer_id = api.add("Orders", ["SELECT 1;"])
    api.configs[outer_id]["rows"].append(dict(ROW))
    inner_id = api.add("Inner", ["SELECT 2;"])
    ws.init_and_pull()
    outer = ws.dir_of(outer_id)
    outer_rel = outer.relative_to(ws.root / "main").as_posix()
    shutil.move(str(ws.dir_of(inner_id)), str(outer / "inner"))
    (row_template,) = [p for p in (outer / "rows").iterdir() if (p / CONFIG_FILENAME).is_file()]
    manifest_file = ws.root / ".keboola" / "manifest.json"
    raw = json.loads(manifest_file.read_text(encoding="utf-8"))
    raw["configurations"] = [c for c in raw["configurations"] if c["id"] != outer_id]
    for cfg in raw["configurations"]:
        if cfg["id"] == inner_id:
            cfg["path"] = f"{outer_rel}/inner"
    raw["pendingRemovals"] = [
        {"tree": "main", "path": outer_rel, "kind": "config", "componentId": "x", "configId": "9"}
    ]
    manifest_file.write_text(json.dumps(raw), encoding="utf-8")
    new_row = outer / "inner" / "rows" / "new-row"
    new_row.mkdir(parents=True)
    data = ws.read_yaml(row_template)
    data["name"] = "New Row"
    for key in ("row_id", "config_id"):
        (data.get("_keboola") or {}).pop(key, None)
    ws.write_yaml(new_row, data)

    rows = [c for c in ws.diff()["changes"] if c.get("is_row")]

    assert [(c["change_type"], c["parent_config_id"]) for c in rows] == [("added", inner_id)]
    assert _no_change_at(ws.diff(), f"{outer_rel}/rows")  # the outer folder stays hidden


def test_a_pending_folder_that_became_a_link_is_kept_and_recorded(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """H2: a valid record whose folder is now a symbolic link keeps its record; nothing deleted."""
    _cid, path = _crashed_before_rmtree(ws, api, monkeypatch)
    moved = tmp_path / "moved-away"
    shutil.move(str(path), str(moved))
    try:
        os.symlink(moved, path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlink support")

    result = ws.pull()

    assert (moved / CONFIG_FILENAME).is_file()
    assert [w["code"] for w in result["warnings"]] == ["PENDING_REMOVAL_KEPT"], result
    assert len(ws.manifest().pending_removals) == 1


def test_an_inner_edit_between_the_two_snapshots_keeps_both_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H3: the outer delete checks every inner record against the inner's own snapshot."""
    from keboola_agent_cli.sync.manifest import load_manifest, save_manifest
    from test_sync_untracked_rows import HTTP_DIR, _dir, _nest_config_b_in_a, _pull, _world

    sim = _world(tmp_path)
    child = _nest_config_b_in_a(sim)
    manifest = load_manifest(sim.root)
    manifest.configurations.sort(key=lambda c: c.id != "1003")  # the child is planned first
    save_manifest(sim.root, manifest)
    real = _sync_pending_removals.RemovalJournal.plan
    edited: list[bool] = []

    def plan_then_editor_saves(self: Any, *args: Any, **kwargs: Any) -> None:
        real(self, *args, **kwargs)
        if not edited:
            edited.append(True)
            text = (child / CONFIG_FILENAME).read_text(encoding="utf-8")
            (child / CONFIG_FILENAME).write_text(text + "\n# saved between the snapshots\n")

    monkeypatch.setattr(_sync_pending_removals.RemovalJournal, "plan", plan_then_editor_saves)
    result = _pull(sim)
    monkeypatch.undo()

    assert edited
    assert "saved between the snapshots" in (child / CONFIG_FILENAME).read_text(encoding="utf-8")
    assert _dir(sim, HTTP_DIR).is_dir()
    assert "PENDING_REMOVAL_KEPT" in [w.get("code") for w in result.get("warnings", [])]
    assert len(load_manifest(sim.root).pending_removals) == 2


# ------------------------------------------------------------------- round 5


def test_a_pending_row_below_a_tracked_config_inside_a_pending_folder_stays_hidden(
    ws: Workspace, api: FakeStorage
) -> None:
    """I1: the tracked config's exemption cancels the pending ANCESTOR only, not its own row record."""
    outer_id = api.add("Orders", ["SELECT 1;"])
    inner_id = api.add("Inner", ["SELECT 2;"])
    api.configs[inner_id]["rows"].append(dict(ROW))
    ws.init_and_pull()
    outer = ws.dir_of(outer_id)
    outer_rel = outer.relative_to(ws.root / "main").as_posix()
    shutil.move(str(ws.dir_of(inner_id)), str(outer / "inner"))
    (row_dir,) = [p for p in (outer / "inner" / "rows").iterdir() if p.is_dir()]
    row_rel = row_dir.relative_to(ws.root / "main").as_posix()
    manifest_file = ws.root / ".keboola" / "manifest.json"
    raw = json.loads(manifest_file.read_text(encoding="utf-8"))
    raw["configurations"] = [c for c in raw["configurations"] if c["id"] != outer_id]
    for cfg in raw["configurations"]:
        if cfg["id"] == inner_id:
            cfg["path"] = f"{outer_rel}/inner"
            cfg["rows"] = []  # the row was removed; its folder's delete failed
    raw["pendingRemovals"] = [
        {"tree": "main", "path": outer_rel, "kind": "config", "componentId": "x", "configId": "9"},
        {
            "tree": "main",
            "path": row_rel,
            "kind": "row",
            "componentId": "keboola.snowflake-transformation",
            "configId": inner_id,
            "rowId": "500",
        },
    ]
    manifest_file.write_text(json.dumps(raw), encoding="utf-8")
    api.configs[inner_id]["rows"] = []

    rows = [c for c in ws.diff()["changes"] if c.get("is_row")]
    calls = len(api.calls)
    ws.push()

    assert rows == [], rows
    assert not [c for c in api.calls[calls:] if c[0] == "create_config_row"]


def test_under_pending_cancels_only_ancestors_of_a_tracked_folder() -> None:
    from keboola_agent_cli.sync.manifest import PendingPaths, under_pending

    pending = PendingPaths(frozenset({"a", "a/b/rows/x"}), frozenset({"a/b"}))
    assert under_pending("a/_other", pending)
    assert not under_pending("a/b", pending)
    assert not under_pending("a/b/rows/new", pending)
    assert under_pending("a/b/rows/x", pending)
    assert under_pending("a/b/rows/x/deep", pending)
    # A config tracked again at a stale record's path is not hidden by it (round 5).
    exact = PendingPaths(frozenset({"c", "c/rows/x"}), frozenset({"c"}))
    assert not under_pending("c", exact)
    assert not under_pending("c/rows/new", exact)
    assert under_pending("c/rows/x", exact)


@pytest.mark.parametrize("kind", ["row", "weird"])
def test_a_malformed_record_behind_a_link_is_dropped_not_kept(
    ws: Workspace, api: FakeStorage, tmp_path: Path, kind: str
) -> None:
    """I2: the record's structure is checked before a link on the way can keep it."""
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
            "kind": kind,
            "componentId": "x",
            "configId": "1",
            "rowId": "1",
            "treeHash": tree_digest(victim),
        },
    )

    result = ws.pull()

    assert (victim / "notes.txt").is_file()
    assert "PENDING_REMOVAL_INVALID" in [w["code"] for w in result["warnings"]], result
    assert ws.manifest().pending_removals == []


def test_a_stale_record_at_a_config_tracked_again_does_not_hide_its_new_row(
    ws: Workspace, api: FakeStorage
) -> None:
    """Round 5: a pending record naming a config's own folder, the config tracked again there."""
    cid = api.add("Orders", ["SELECT 1;"])
    api.configs[cid]["rows"].append(dict(ROW))
    ws.init_and_pull()
    d = ws.dir_of(cid)
    rel = d.relative_to(ws.root / "main").as_posix()
    _plant(
        ws,
        {"tree": "main", "path": rel, "kind": "config", "componentId": "x", "configId": "9"},
    )
    (template,) = [p for p in (d / "rows").iterdir() if (p / CONFIG_FILENAME).is_file()]
    new_row = d / "rows" / "new-row"
    new_row.mkdir()
    data = ws.read_yaml(template)
    data["name"] = "New Row"
    for key in ("row_id", "config_id"):
        (data.get("_keboola") or {}).pop(key, None)
    ws.write_yaml(new_row, data)

    rows = [c for c in ws.diff()["changes"] if c.get("is_row")]
    ws.push()

    assert [(c["change_type"], c["parent_config_id"]) for c in rows] == [("added", cid)]
    assert [r["name"] for r in api.configs[cid]["rows"]] == ["Existing Row", "New Row"]


def test_pull_row_discovery_honours_tracked_exemptions() -> None:
    """NB-2: row adoption judges pending paths tree-relative, exemptions included."""
    from keboola_agent_cli.services._sync_pull_dirs import IdDirs, without_pending
    from keboola_agent_cli.sync.manifest import PendingPaths

    def dirs() -> IdDirs:
        found = IdDirs()
        found.add("500", "rows/kept")
        found.add("501", "rows/gone")
        return found

    # The config folder itself is pending and NOT tracked: every row folder is hidden.
    hidden = without_pending(dirs(), PendingPaths(frozenset({"c"})), "c")
    assert hidden.by_id == {}
    assert hidden.paths == {"rows/kept", "rows/gone"}  # still reserved
    # Tracked again at "c": its folder is lifted, the row record below it is not (H1, I1).
    lifted = without_pending(
        dirs(), PendingPaths(frozenset({"c", "c/rows/gone"}), frozenset({"c"})), "c"
    )
    assert lifted.by_id == {"500": "rows/kept"}


# ------------------------------------------------------------------- Devin D1, D2


def _refuse(*_: Any, **__: Any) -> None:
    raise PermissionError("busy")


def test_a_pending_row_follows_its_renamed_configuration(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1: delete fails twice, the parent is renamed in between; push creates no row."""
    cid = api.add("Orders", ["SELECT 1;"])
    api.configs[cid]["rows"].append(dict(ROW))
    ws.init_and_pull()
    api.configs[cid]["rows"] = []
    _removal_via(monkeypatch, _refuse)
    ws.pull()  # the row delete fails: recorded
    (first,) = ws.manifest().pending_removals
    assert first.row_path and first.path.endswith(first.row_path)
    api.ui_rename(cid, "Orders renamed")
    ws.pull()  # recovery fails again, and the parent folder is renamed
    monkeypatch.undo()

    entry = ws.entry(cid)
    assert entry is not None and entry.path.endswith("orders-renamed")
    (record,) = ws.manifest().pending_removals
    assert record.path == f"{entry.path}/{record.row_path}"
    assert (ws.root / "main" / record.path / CONFIG_FILENAME).is_file()
    assert not [c for c in ws.diff()["changes"] if c.get("is_row")]
    ws.push()
    assert ("create_config_row", "500") not in api.calls
    assert not [c for c in api.calls if c[0] == "create_config_row"]


def test_a_row_journaled_after_a_rename_in_the_same_pull_survives_a_restart(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2: rename + row removal in one pull, crash before rmtree; the restart deletes the row."""
    cid = api.add("Orders", ["SELECT 1;"])
    api.configs[cid]["rows"].append(dict(ROW))
    ws.init_and_pull()
    api.configs[cid]["rows"] = []
    api.ui_rename(cid, "Orders renamed")
    _removal_via(monkeypatch, _die)
    with pytest.raises(Crash):
        ws.pull()
    monkeypatch.undo()
    entry = ws.entry(cid)
    assert entry is not None and entry.path.endswith("orders-renamed")  # checkpoint took the move
    (record,) = ws.manifest().pending_removals
    row_dir = ws.root / "main" / record.path
    assert row_dir.is_dir() and record.path.startswith(f"{entry.path}/")

    result = ws.pull()

    assert "PENDING_REMOVAL_INVALID" not in [w.get("code") for w in result.get("warnings", [])]
    assert not row_dir.exists()
    assert ws.manifest().pending_removals == []
    ws.push()
    assert not [c for c in api.calls if c[0] == "create_config_row"]


def test_resolve_removal_follows_the_configuration_entry() -> None:
    from keboola_agent_cli.sync.branch_scope import resolve_removal
    from keboola_agent_cli.sync.manifest import (
        ManifestBranch,
        ManifestConfiguration,
        ManifestNaming,
        ManifestProject,
    )

    manifest = Manifest(
        project=ManifestProject(id=1, apiHost="h"),
        naming=ManifestNaming(),
        branches=[ManifestBranch(id=1, path="main")],
        configurations=[ManifestConfiguration(branchId=1, componentId="c", id="9", path="new")],
    )
    row = ManifestPendingRemoval(
        tree="main", path="old/rows/r", kind="row", componentId="c", configId="9", rowPath="rows/r"
    )
    assert resolve_removal(manifest, row).path == "new/rows/r"
    legacy = row.model_copy(update={"row_path": ""})
    assert resolve_removal(manifest, legacy).path == "old/rows/r"
    assert ManifestPendingRemoval(tree="main", path="x", kind="row", rowPath="../x").problem


# ------------------------------------------------------------------- #1005 review


def test_a_promote_reference_does_not_keep_a_remotely_deleted_production_folder(
    tmp_path: Path,
) -> None:
    """#1005 claim 1: B (dev, promoted from A's main/ folder) is no owner of that folder.

    A is deleted on production. The production pull deletes A's folder (B's
    promote entry then goes, its source folder is gone), and the next
    production push creates nothing.
    """
    from test_sync_bookkeeping_differential import CONFIG_FILE, DEV, PROD, VARIANTS, _seeded

    v = VARIANTS["extractor"]
    seeded = _seeded(tmp_path, v)
    w, prod_id = seeded.world, seeded.config_id
    config_dir = w.config_dir(prod_id)
    w.push(branch=DEV)  # B: promoted from main/<A>, a new id on the dev branch
    assert [cid for comp, cid in w.api.remote[DEV] if comp == v.component_id] != [prod_id]
    del w.api.remote[PROD][(v.component_id, prod_id)]

    result = w.pull()

    assert not (config_dir / CONFIG_FILE).exists(), result
    from keboola_agent_cli.sync.manifest import load_manifest

    assert load_manifest(w.root).pending_removals == []
    pushed = w.push()
    assert pushed["created"] == 0, pushed
    assert (v.component_id, prod_id) not in w.api.remote[PROD]
    assert not [key for key in w.api.remote[PROD] if key[0] == v.component_id]
    # B stays on the dev branch; a promote push neither fails nor touches it.
    dev_before = dict(w.api.remote[DEV])
    w.push(branch=DEV)  # asserts errors == []
    assert w.api.remote[DEV] == dev_before


def test_a_partly_deleted_folder_is_finished_by_the_next_pull(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1005 claim 2: an rmtree that removed some files and then failed is finished later."""
    cid = api.add("Orders", ["SELECT 1;"])
    ws.init_and_pull()
    path = ws.dir_of(cid)
    api.ui_trash(cid)

    def delete_one_file_then_fail(target: Any, *_: Any, **__: Any) -> None:
        (Path(target) / "transform.sql").unlink()
        raise PermissionError("busy")

    _quarantine_rmtree_via(monkeypatch, delete_one_file_then_fail)
    first = ws.pull()
    monkeypatch.undo()
    assert [w["code"] for w in first["warnings"]] == ["PENDING_REMOVAL_FAILED"], first

    assert not path.exists()  # moved out of the tree in one step, never half-deleted there
    assert ws.manifest().pending_removals == []

    second = ws.pull()  # permissions fixed: the quarantine is emptied

    assert not path.exists(), second
    assert ws.manifest().pending_removals == []
    quarantine = _sync_pending_removals.quarantine_dir(ws.root)
    assert [p.name for p in quarantine.iterdir()] == [".gitignore"]


# ------------------------------------------------------------------- #1005 review round 2


def test_a_crash_after_the_quarantine_move_keeps_the_dev_promote_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """K1: the checkpoint drops B's promote entry with A's folder; push --branch DEV --force deletes nothing."""
    from test_sync_bookkeeping_differential import DEV, PROD, VARIANTS, _seeded

    v = VARIANTS["extractor"]
    seeded = _seeded(tmp_path, v)
    w, prod_id = seeded.world, seeded.config_id
    w.push(branch=DEV)  # B on the dev branch, promoted from A's main/ folder
    dev_before = dict(w.api.remote[DEV])
    del w.api.remote[PROD][(v.component_id, prod_id)]
    real = _sync_pending_removals._move_aside

    def move_then_die(*args: Any, **kwargs: Any) -> None:
        real(*args, **kwargs)
        raise Crash("killed right after the move into the quarantine")

    _removal_via(monkeypatch, move_then_die)
    with pytest.raises(Crash):
        w.pull()
    monkeypatch.undo()

    result = w.svc.push(alias="prod", project_root=w.root, branch_override=DEV, force=True)

    assert w.api.remote[DEV] == dev_before, result
    assert result.get("deleted", 0) == 0, result


def test_a_linked_quarantine_is_never_used(ws: Workspace, api: FakeStorage, tmp_path: Path) -> None:
    """K2: ``.keboola/removing`` -> elsewhere: nothing is moved there and nothing there is deleted."""
    cid = api.add("Orders", ["SELECT 1;"])
    ws.init_and_pull()
    path = ws.dir_of(cid)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("not kbagent's", encoding="utf-8")
    try:
        os.symlink(elsewhere, _sync_pending_removals.quarantine_dir(ws.root), True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlink support")
    api.ui_trash(cid)

    result = ws.pull()
    second = ws.pull()

    assert (elsewhere / "keep.txt").is_file()
    assert [p.name for p in elsewhere.iterdir()] == ["keep.txt"]
    assert (path / CONFIG_FILENAME).is_file()  # not moved, still recorded
    assert len(ws.manifest().pending_removals) == 1
    assert "PENDING_REMOVAL_FAILED" in [w["code"] for w in result["warnings"]]
    assert "PENDING_REMOVAL_FAILED" in [w["code"] for w in second["warnings"]]


def test_quarantine_problem_refuses_a_linked_keboola_folder(tmp_path: Path) -> None:
    """K2: a symlinked ``.keboola`` makes the quarantine unusable too."""
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    (outside / "removing").mkdir(parents=True)
    try:
        os.symlink(outside, project / ".keboola", True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlink support")

    assert "symbolic link" in (_sync_pending_removals.quarantine_problem(project) or "")
    assert _sync_pending_removals.empty_quarantine(project)[0]["code"] == "PENDING_REMOVAL_FAILED"
    assert (outside / "removing").is_dir()


def test_the_quarantine_ignores_itself_for_git(ws: Workspace, api: FakeStorage) -> None:
    """K3: the quarantine carries a ``.gitignore`` of ``*``, kept by its cleanup."""
    cid = api.add("Orders", ["SELECT 1;"])
    ws.init_and_pull()
    api.ui_trash(cid)

    ws.pull()
    ws.pull()

    ignore = _sync_pending_removals.quarantine_dir(ws.root) / ".gitignore"
    assert ignore.read_text(encoding="utf-8") == "*\n"
