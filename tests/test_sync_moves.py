"""A hand-moved config directory keeps its identity (issue #792 finding I).

Every risk of the design (M1-M15) and of the design review has a test here,
driven through the real ``SyncService`` against the branch-aware in-memory
Storage API of the random walk (configs and rows).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.services._sync_moves import settle_moves
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.sync.manifest import load_manifest
from test_sync_random_walk import DEV, HTTP, PROD, SQL, FakeApi, make_service

MANIFEST = Path(".keboola") / "manifest.json"


class LoggingApi(FakeApi):
    """FakeApi that records every config/row create and delete."""

    def __init__(self) -> None:
        super().__init__()
        self.log: list[str] = []
        self.fail_update: Exception | BaseException | None = None

    def _create(self, component_id: str, name: str, configuration: dict, **kw: Any) -> dict:
        out = super()._create(component_id, name, configuration, **kw)
        self.log.append(f"CREATE {component_id}/{out['id']}")
        return out

    def _delete(self, component_id: str, config_id: str, branch_id: int | None = None) -> None:
        self.log.append(f"DELETE {component_id}/{config_id}")
        super()._delete(component_id, config_id, branch_id)

    def _update(self, component_id: str, config_id: str, **kw: Any) -> dict:
        if self.fail_update is not None:
            raise self.fail_update
        self.log.append(f"UPDATE {component_id}/{config_id}")
        return super()._update(component_id, config_id, **kw)

    def _create_row(self, component_id: str, config_id: str, name: str, configuration: dict, **kw):
        out = super()._create_row(component_id, config_id, name, configuration, **kw)
        self.log.append(f"CREATE-ROW {config_id}/{out['id']}")
        return out

    def _delete_row(self, component_id: str, config_id: str, row_id: str, **kw: Any) -> None:
        self.log.append(f"DELETE-ROW {config_id}/{row_id}")
        super()._delete_row(component_id, config_id, row_id, **kw)

    def writes(self, kind: str) -> list[str]:
        return [e for e in self.log if e.startswith(kind)]


def put(
    api: FakeApi,
    branch: int,
    cid: str,
    name: str,
    value: str,
    *,
    component: str = HTTP,
    rows: tuple[tuple[str, str, str], ...] = (),
) -> None:
    api.remote[branch][(component, cid)] = {
        "id": cid,
        "name": name,
        "description": "",
        "configuration": {"parameters": {"value": value}},
        "rows": [
            {
                "id": rid,
                "name": rname,
                "description": "",
                "configuration": {"parameters": {"value": rvalue}},
                "isDisabled": False,
            }
            for rid, rname, rvalue in rows
        ],
        "isDisabled": False,
    }


class Tree:
    """A sync working tree bound to a LoggingApi."""

    def __init__(self, tmp: Path) -> None:
        self.api = LoggingApi()
        fx = make_service(tmp, self.api)
        self.svc, self.root = fx.svc, fx.root

    def init(self) -> None:
        self.svc.init_sync(alias="prod", project_root=self.root)

    def pull(self, branch: int | None = None, **kw: Any) -> dict:
        return self.svc.pull(alias="prod", project_root=self.root, branch_override=branch, **kw)

    def diff(self, branch: int | None = None) -> dict:
        return self.svc.diff(alias="prod", project_root=self.root, branch_override=branch)

    def push(self, branch: int | None = None, **kw: Any) -> dict:
        return self.svc.push(alias="prod", project_root=self.root, branch_override=branch, **kw)

    def dir_of(self, config_id: str, tree: str = "main") -> Path:
        """The one config dir of *tree* whose _config.yml carries *config_id*."""
        found = self.dirs_of(config_id, tree)
        assert len(found) == 1, found
        return found[0]

    def dirs_of(self, config_id: str, tree: str = "main") -> list[Path]:
        out = []
        for f in (self.root / tree).rglob("_config.yml"):
            if "rows" in f.relative_to(self.root).parts:
                continue
            meta = (yaml.safe_load(f.read_text()) or {}).get("_keboola") or {}
            if str(meta.get("config_id")) == config_id:
                out.append(f.parent)
        return sorted(out)

    def rel(self, path: Path, tree: str = "main") -> str:
        return path.relative_to(self.root / tree).as_posix()

    def entries(self, config_id: str) -> list[Any]:
        return [c for c in load_manifest(self.root).configurations if c.id == config_id]

    def manifest_bytes(self) -> bytes:
        return (self.root / MANIFEST).read_bytes()

    def move(self, src: Path, name: str) -> Path:
        dst = src.parent / name
        shutil.move(str(src), str(dst))
        return dst


def edit_value(config_dir: Path, value: str) -> None:
    path = config_dir / "_config.yml"
    data = yaml.safe_load(path.read_text())
    data["parameters"]["value"] = value
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def kinds(result: dict) -> list[tuple[str, bool]]:
    return sorted((c["change_type"], bool(c.get("is_row"))) for c in result["changes"])


def remote_ids(api: FakeApi, branch: int = PROD) -> list[str]:
    return sorted(cid for _comp, cid in api.remote[branch])


@pytest.fixture
def orders(tmp_path: Path) -> Tree:
    """Production config 500 with two rows, pulled into main/."""
    t = Tree(tmp_path)
    put(t.api, PROD, "500", "Orders", "a", rows=[("r1", "One", "x"), ("r2", "Two", "y")])
    t.init()
    t.pull()
    return t


# ---------------------------------------------------------------------------
# M1 / M13 -- an unchanged move with rows: bookkeeping only
# ---------------------------------------------------------------------------


def test_m1_unchanged_move_with_rows_is_path_bookkeeping_only(orders: Tree) -> None:
    old = orders.dir_of("500")
    old_rel = orders.rel(old)
    (entry_before,) = orders.entries("500")
    new = orders.move(old, "orders-moved")
    new_rel = orders.rel(new)

    before = orders.manifest_bytes()
    diff = orders.diff()
    assert diff["changes"] == []
    assert diff["moves"] == [
        {
            "component_id": HTTP,
            "config_id": "500",
            "branch_id": entry_before.branch_id,
            "moved_from": old_rel,
            "moved_to": new_rel,
        }
    ]
    assert diff["summary"]["moved"] == 1
    dry = orders.push(dry_run=True, force=True)
    assert dry["moves"] == diff["moves"]
    assert orders.manifest_bytes() == before, "diff / dry-run changed the manifest (M13)"

    result = orders.push(force=True)
    assert result["status"] == "no_changes"
    assert result["moves"] == diff["moves"]
    assert orders.api.log == []
    (entry,) = orders.entries("500")
    assert entry.path == new_rel
    assert [r.path for r in entry.rows] == [r.path for r in entry_before.rows]
    assert entry.metadata["pull_config_hash"] == entry_before.metadata["pull_config_hash"]

    after = orders.diff()
    assert after["changes"] == [] and "moves" not in after
    pulled = orders.pull()
    assert orders.api.log == []
    assert len(orders.entries("500")) == 1
    # Pull names the directory after the config again, as for any dir name.
    assert orders.rel(orders.dir_of("500")) == old_rel
    assert [d["action"] for d in pulled["details"]] == ["renamed"]
    assert orders.diff()["changes"] == []
    assert remote_ids(orders.api) == ["500"]


def test_m1_pull_right_after_a_move_keeps_one_directory(orders: Tree) -> None:
    orders.move(orders.dir_of("500"), "orders-moved")
    result = orders.pull()
    assert [d["action"] for d in result["details"]][:2] == ["moved", "renamed"]
    orders.dir_of("500")
    assert len(orders.entries("500")) == 1
    assert orders.diff()["changes"] == []
    orders.push(force=True)
    assert orders.api.log == []


# ---------------------------------------------------------------------------
# M2 / M3 -- content after the move is classified three-way
# ---------------------------------------------------------------------------


def test_moved_and_edited_config_is_pushed_as_update(orders: Tree) -> None:
    new = orders.move(orders.dir_of("500"), "orders-moved")
    edit_value(new, "local")
    diff = orders.diff()
    (change,) = diff["changes"]
    assert change["change_type"] == "modified"
    assert change["config_id"] == "500"
    assert change["moved_to"] == orders.rel(new)
    orders.push(force=True)
    assert orders.api.writes("CREATE") == [] and orders.api.writes("DELETE") == []
    assert orders.api.remote[PROD][(HTTP, "500")]["configuration"]["parameters"]["value"] == "local"
    assert orders.diff()["changes"] == []


def test_m2_move_edit_and_ui_edit_is_conflict_without_new_baseline(orders: Tree) -> None:
    (entry_before,) = orders.entries("500")
    new = orders.move(orders.dir_of("500"), "orders-moved")
    edit_value(new, "local")
    orders.api.remote[PROD][(HTTP, "500")]["configuration"]["parameters"]["value"] = "ui"
    diff = orders.diff()
    (change,) = [c for c in diff["changes"] if not c.get("is_row")]
    assert change["change_type"] == "conflict"
    assert (change["moved_from"], change["moved_to"]) == (
        entry_before.path,
        orders.rel(new),
    )
    orders.push(force=True)
    assert orders.api.log == []
    assert orders.api.remote[PROD][(HTTP, "500")]["configuration"]["parameters"]["value"] == "ui"
    (entry,) = orders.entries("500")
    assert entry.path == orders.rel(new)
    assert entry.metadata["pull_config_hash"] == entry_before.metadata["pull_config_hash"]
    assert entry.metadata["pull_hash"] == entry_before.metadata["pull_hash"]


def test_m3_move_then_ui_delete_is_remote_deleted_never_create(orders: Tree) -> None:
    orders.move(orders.dir_of("500"), "orders-moved")
    del orders.api.remote[PROD][(HTTP, "500")]
    diff = orders.diff()
    configs = [c for c in diff["changes"] if not c.get("is_row")]
    assert [(c["change_type"], c["config_id"]) for c in configs] == [("remote_deleted", "500")]
    orders.push(force=True)
    assert orders.api.writes("CREATE") == []
    assert remote_ids(orders.api) == []


# ---------------------------------------------------------------------------
# M4 -- rows of a moved config
# ---------------------------------------------------------------------------


def test_m4_moved_config_rows_follow_remote_row_changes(orders: Tree) -> None:
    orders.move(orders.dir_of("500"), "orders-moved")
    cfg = orders.api.remote[PROD][(HTTP, "500")]
    cfg["rows"][0]["configuration"]["parameters"]["value"] = "ui-row"  # r1 edited
    cfg["rows"] = [r for r in cfg["rows"] if r["id"] != "r2"]  # r2 deleted
    cfg["rows"].append(
        {
            "id": "r3",
            "name": "Three",
            "description": "",
            "configuration": {"parameters": {"value": "z"}},
            "isDisabled": False,
        }
    )
    diff = orders.diff()
    assert not any(c.get("refused_delete") for c in diff["changes"])
    assert not any(c["change_type"] in ("added", "deleted") for c in diff["changes"])
    rows = sorted((c["change_type"], c["config_id"]) for c in diff["changes"] if c.get("is_row"))
    assert rows == [("remote_deleted", "r2"), ("remote_modified", "r1")]
    assert [r["row_id"] for r in diff["remote_only_rows"]] == ["r3"]
    orders.push(force=True)
    assert orders.api.log == []


def test_m4_local_row_edit_under_moved_config_updates_the_row(orders: Tree) -> None:
    new = orders.move(orders.dir_of("500"), "orders-moved")
    (row_file,) = [
        f
        for f in (new / "rows").rglob("_config.yml")
        if yaml.safe_load(f.read_text())["_keboola"]["row_id"] == "r1"
    ]
    data = yaml.safe_load(row_file.read_text())
    data["parameters"]["value"] = "local-row"
    row_file.write_text(yaml.safe_dump(data, sort_keys=False))
    diff = orders.diff()
    assert kinds(diff) == [("modified", True)]
    orders.push(force=True)
    assert orders.api.writes("CREATE") == [] and orders.api.writes("DELETE") == []
    rows = {r["id"]: r for r in orders.api.remote[PROD][(HTTP, "500")]["rows"]}
    assert rows["r1"]["configuration"]["parameters"]["value"] == "local-row"
    assert sorted(rows) == ["r1", "r2"]


# ---------------------------------------------------------------------------
# M5 / M6 / M7 -- ambiguity, copies, unreadable original
# ---------------------------------------------------------------------------


def test_m5_move_plus_copy_is_ambiguous_nothing_created_or_deleted(orders: Tree) -> None:
    (entry_before,) = orders.entries("500")
    new = orders.move(orders.dir_of("500"), "orders-moved")
    copy = new.parent / "orders-copy"
    shutil.copytree(new, copy)
    before = orders.manifest_bytes()

    diff = orders.diff()
    assert not any(c["change_type"] in ("added", "deleted") for c in diff["changes"])
    (held,) = diff["changes"]
    assert held["change_type"] == "conflict" and held["ambiguous_move"] is True
    assert held["candidates"] == sorted([orders.rel(new), orders.rel(copy)])
    assert diff["ambiguous_moves"][0]["path"] == entry_before.path
    assert diff["remote_only"] == []
    assert "moves" not in diff

    result = orders.push(force=True)
    assert orders.api.log == []
    assert result["ambiguous_moves"] == diff["ambiguous_moves"]
    assert orders.manifest_bytes() == before
    assert remote_ids(orders.api) == ["500"]

    pulled = orders.pull(force=True)
    assert any(d["action"] == "skipped" and d.get("config_id") == "500" for d in pulled["details"])
    assert not (orders.root / "main" / entry_before.path).exists(), "pull wrote the original back"
    (entry,) = orders.entries("500")
    assert entry.path == entry_before.path
    orders.push(force=True)
    assert orders.api.log == []


def test_m6_copy_with_original_kept_is_still_a_fork(orders: Tree) -> None:
    original = orders.dir_of("500")
    shutil.copytree(original, original.parent / "orders-fork")
    diff = orders.diff()
    assert "moves" not in diff
    assert [c["change_type"] for c in diff["changes"] if not c.get("is_row")] == ["added"]
    orders.push(force=True)
    assert len(orders.api.writes("CREATE ")) == 1
    assert "500" in remote_ids(orders.api)


def test_m7_unreadable_original_is_not_a_move(orders: Tree) -> None:
    original = orders.dir_of("500")
    shutil.copytree(original, original.parent / "orders-copy")
    (original / "_config.yml").write_text("name: [unclosed\n")
    diff = orders.diff()
    assert "moves" not in diff
    orders.push()
    assert orders.api.writes("DELETE") == []
    (entry,) = orders.entries("500")
    assert entry.path == orders.rel(original)
    assert json.loads(orders.manifest_bytes())["configurations"][0]["path"] == entry.path


# ---------------------------------------------------------------------------
# M8 / M9 -- ignored component, cross-component-directory move
# ---------------------------------------------------------------------------


def test_m8_moved_dir_of_ignored_component_is_left_alone(orders: Tree) -> None:
    path = orders.root / MANIFEST
    data = json.loads(path.read_text())
    data["ignoredComponents"] = [HTTP]
    path.write_text(json.dumps(data))
    (entry_before,) = orders.entries("500")
    orders.move(orders.dir_of("500"), "orders-moved")
    diff = orders.diff()
    assert "moves" not in diff and diff["changes"] == []
    orders.push(force=True)
    assert orders.api.log == []
    (entry,) = orders.entries("500")
    assert entry.path == entry_before.path


def test_m9_move_into_another_component_directory_is_a_move(orders: Tree) -> None:
    old = orders.dir_of("500")
    target = orders.root / "main" / "extractor" / "keboola.other-component" / "orders"
    target.parent.mkdir(parents=True)
    shutil.move(str(old), str(target))
    diff = orders.diff()
    assert diff["changes"] == []
    assert diff["moves"][0]["moved_to"] == orders.rel(target)
    orders.push(force=True)
    assert orders.api.log == []
    assert orders.entries("500")[0].path == orders.rel(target)


# ---------------------------------------------------------------------------
# M10 -- promote associations move with their source
# ---------------------------------------------------------------------------


def test_m10_promoted_config_and_its_source_keep_their_ids(orders: Tree) -> None:
    first = orders.push(branch=DEV)
    assert first["created"] == 3  # the config and its two rows
    (dev_id,) = remote_ids(orders.api, DEV)
    orders.api.log.clear()
    old = orders.dir_of("500")
    new = orders.move(old, "orders-moved")

    diff = orders.diff(branch=DEV)
    assert not any(c["change_type"] in ("added", "deleted") for c in diff["changes"])
    moved = {(m["config_id"], m["branch_id"]) for m in diff["moves"]}
    assert ("500", PROD) in moved or ("500", 0) in moved
    assert any(cid == dev_id for cid, _b in moved)

    orders.push(branch=DEV, force=True)
    assert orders.api.log == []
    assert remote_ids(orders.api, DEV) == [dev_id]
    assert orders.diff(branch=DEV)["changes"] == []

    orders.push(force=True)
    assert orders.api.log == []
    assert remote_ids(orders.api) == ["500"]
    for entry in load_manifest(orders.root).configurations:
        assert entry.path == orders.rel(new)
    edit_value(new, "after-move")
    orders.push(branch=DEV)
    assert orders.api.writes("CREATE") == []
    dev_cfg = orders.api.remote[DEV][(HTTP, dev_id)]
    assert dev_cfg["configuration"]["parameters"]["value"] == "after-move"


# ---------------------------------------------------------------------------
# M11 -- retired rows stay deletion provenance
# ---------------------------------------------------------------------------


def test_m11_retired_rows_survive_a_move_of_an_orphaned_dir(orders: Tree) -> None:
    put(orders.api, DEV, "500", "Orders", "a", rows=[("r1", "One", "x"), ("r2", "Two", "y")])
    orders.pull(branch=DEV)  # main/ entries retired, main/ files stay
    moved = orders.move(orders.dir_of("500"), "orders-moved")
    cfg = orders.api.remote[PROD][(HTTP, "500")]
    cfg["rows"] = [r for r in cfg["rows"] if r["id"] != "r2"]  # UI deletes r2
    orders.pull()
    (main_dir,) = orders.dirs_of("500")
    row_ids = sorted(
        yaml.safe_load(f.read_text())["_keboola"]["row_id"]
        for f in (main_dir / "rows").rglob("_config.yml")
    )
    assert row_ids == ["r1"], "a row the remote deleted came back"
    assert moved.exists() or main_dir.exists()
    diff = orders.diff()
    assert not any(c["change_type"] == "added" for c in diff["changes"])


# ---------------------------------------------------------------------------
# M12 -- production spellings and an unregistered dev branch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spelling", [0, PROD])
def test_m12_production_spelling_of_the_entry_does_not_matter(orders: Tree, spelling: int) -> None:
    path = orders.root / MANIFEST
    data = json.loads(path.read_text())
    data["configurations"][0]["branchId"] = spelling
    path.write_text(json.dumps(data))
    new = orders.move(orders.dir_of("500"), "orders-moved")
    diff = orders.diff()
    assert diff["changes"] == []
    assert diff["moves"][0]["branch_id"] == spelling
    orders.push(force=True)
    assert orders.api.log == []
    assert orders.entries("500")[0].path == orders.rel(new)
    assert orders.entries("500")[0].branch_id == spelling


def test_m12_unregistered_branch_entry_moves_in_the_default_tree(orders: Tree) -> None:
    manifest = load_manifest(orders.root)
    manifest.configurations[0].branch_id = 777  # not in manifest.branches
    new = orders.move(orders.dir_of("500"), "orders-moved")
    plan = settle_moves(orders.svc, manifest, orders.root, "main", frozenset(), drop_held=True)
    assert [(m.branch_id, m.moved_to) for m in plan.moves] == [(777, orders.rel(new))]
    assert manifest.configurations[0].path == orders.rel(new)


# ---------------------------------------------------------------------------
# Failure scenarios: partial push, interrupted push
# ---------------------------------------------------------------------------


def test_failed_update_after_move_persists_only_the_path(orders: Tree) -> None:
    (entry_before,) = orders.entries("500")
    new = orders.move(orders.dir_of("500"), "orders-moved")
    edit_value(new, "local")
    orders.api.fail_update = KeboolaApiError("boom", 500, ErrorCode.API_ERROR)
    result = orders.push()
    assert result["errors"]
    (entry,) = orders.entries("500")
    assert entry.path == orders.rel(new)
    assert entry.metadata["pull_config_hash"] == entry_before.metadata["pull_config_hash"]

    orders.api.fail_update = None
    diff = orders.diff()
    assert kinds(diff) == [("modified", False)] and "moves" not in diff
    orders.push()
    assert orders.api.writes("CREATE") == [] and orders.api.writes("DELETE") == []
    assert orders.api.remote[PROD][(HTTP, "500")]["configuration"]["parameters"]["value"] == "local"


def test_interrupted_push_after_move_is_safe_to_rerun(orders: Tree) -> None:
    new = orders.move(orders.dir_of("500"), "orders-moved")
    edit_value(new, "local")
    before = orders.manifest_bytes()
    orders.api.fail_update = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        orders.push()
    assert orders.manifest_bytes() == before

    orders.api.fail_update = None
    diff = orders.diff()
    assert diff["moves"][0]["moved_to"] == orders.rel(new)
    orders.push()
    assert orders.api.writes("CREATE") == [] and orders.api.writes("DELETE") == []
    assert orders.entries("500")[0].path == orders.rel(new)
    assert orders.diff()["changes"] == []


def test_two_moved_configs_in_one_push(tmp_path: Path) -> None:
    t = Tree(tmp_path)
    put(t.api, PROD, "500", "Orders", "a")
    put(t.api, PROD, "600", "Query", "b", component=SQL)
    t.init()
    t.pull()
    t.move(t.dir_of("500"), "orders-moved")
    t.move(t.dir_of("600"), "query-moved")
    diff = t.diff()
    assert diff["changes"] == [] and len(diff["moves"]) == 2
    t.push(force=True)
    assert t.api.log == []
    assert t.diff()["changes"] == []


# ---------------------------------------------------------------------------
# M14 -- `config rename --directory` keeps working
# ---------------------------------------------------------------------------


def _config_service(t: Tree) -> ConfigService:
    return ConfigService(config_store=t.svc._config_store, client_factory=lambda u, k: None)


def test_m14_rename_directory_picks_the_entry_of_the_branch(orders: Tree) -> None:
    path = orders.root / MANIFEST
    data = json.loads(path.read_text())
    prod_entry = data["configurations"][0]
    data["branches"].append({"id": DEV, "path": "dev", "metadata": {}})
    data["configurations"].append({**prod_entry, "branchId": DEV})
    path.write_text(json.dumps(data))
    main_dir = orders.dir_of("500")
    shutil.copytree(main_dir, orders.root / "dev" / prod_entry["path"])

    result = _config_service(orders)._rename_sync_directory(
        directory=orders.root,
        component_id=HTTP,
        config_id="500",
        new_name="Renamed",
        branch_id=DEV,
    )
    assert result is not None
    assert main_dir.exists(), "the production directory was renamed for a dev-branch rename"
    assert (orders.root / "dev" / result["new_path"]).exists()
    by_branch = {c.branch_id: c.path for c in load_manifest(orders.root).configurations}
    assert by_branch[DEV] == result["new_path"]
    assert by_branch[prod_entry["branchId"]] == prod_entry["path"]


def test_m14_rename_directory_relocates_the_promoted_copy(orders: Tree) -> None:
    orders.push(branch=DEV)
    (dev_id,) = remote_ids(orders.api, DEV)
    orders.api.log.clear()
    result = _config_service(orders)._rename_sync_directory(
        directory=orders.root, component_id=HTTP, config_id="500", new_name="Renamed"
    )
    assert result is not None
    paths = {c.id: c.path for c in load_manifest(orders.root).configurations}
    assert paths == {"500": result["new_path"], dev_id: result["new_path"]}
    diff = orders.diff(branch=DEV)
    assert not any(c["change_type"] in ("added", "deleted") for c in diff["changes"])
    orders.push(branch=DEV, force=True)
    assert orders.api.writes("CREATE") == [] and orders.api.writes("DELETE") == []
    assert remote_ids(orders.api, DEV) == [dev_id]


def test_human_output_names_the_move_and_the_ambiguous_one(orders: Tree) -> None:
    from types import SimpleNamespace

    from rich.console import Console

    from keboola_agent_cli.commands._sync_diff_render import print_moves

    new = orders.move(orders.dir_of("500"), "orders-moved")
    console = Console(record=True, width=300)
    print_moves(SimpleNamespace(console=console), orders.diff())
    shutil.copytree(new, new.parent / "orders-copy")
    print_moves(SimpleNamespace(console=console), orders.diff())
    text = console.export_text()
    assert f"MOVED {HTTP}/500: extractor/{HTTP}/orders -> {orders.rel(new)}" in text
    assert "AMBIGUOUS MOVE" in text and "orders-copy" in text


def test_human_pull_output_of_moved_and_held_configs(orders: Tree) -> None:
    from types import SimpleNamespace

    from rich.console import Console

    from keboola_agent_cli.commands.sync import _format_pull_result

    new = orders.move(orders.dir_of("500"), "orders-moved")
    shutil.copytree(new, new.parent / "orders-copy")
    console = Console(record=True, width=300)
    result = orders.pull(dry_run=True)
    _format_pull_result(SimpleNamespace(console=console), result)
    assert f"{HTTP}/orders" in console.export_text()


def test_status_reports_a_move_not_a_delete_plus_add(orders: Tree) -> None:
    before = orders.manifest_bytes()
    new = orders.move(orders.dir_of("500"), "orders-moved")
    status = orders.svc.status(orders.root)
    assert status["deleted"] == [] and status["added"] == []
    assert [m["moved_to"] for m in status["moves"]] == [orders.rel(new)]
    assert status["unchanged"] == 1
    assert orders.manifest_bytes() == before
