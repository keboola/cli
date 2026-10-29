"""A hand-moved config directory keeps its identity (issue #792 finding I).

Every risk of the design (M1-M15) and of the design review has a test here,
driven through the real ``SyncService`` against the branch-aware in-memory
Storage API of the random walk (configs and rows).
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from helpers import setup_single_project
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.sync.manifest import load_manifest
from test_sync_random_walk import DEV, HTTP, PROD, SQL, FakeApi, make_service

MANIFEST = Path(".keboola") / "manifest.json"


class LoggingApi(FakeApi):
    """FakeApi whose clients record every config/row create, update and delete."""

    def __init__(self) -> None:
        super().__init__()
        self.log: list[str] = []
        self.fail_update: BaseException | None = None

    def client(self) -> MagicMock:
        c = super().client()
        create, update, delete = (
            c.create_config.side_effect,
            c.update_config.side_effect,
            (c.delete_config.side_effect),
        )
        create_row, delete_row = c.create_config_row.side_effect, c.delete_config_row.side_effect

        def logged_create(*args: Any, **kw: Any) -> dict:
            out = create(*args, **kw)
            self.log.append(f"CREATE {out['id']}")
            return out

        def logged_update(*args: Any, **kw: Any) -> dict:
            if self.fail_update is not None:
                raise self.fail_update
            out = update(*args, **kw)
            self.log.append(f"UPDATE {out['id']}")
            return out

        def logged_delete(*args: Any, **kw: Any) -> None:
            delete(*args, **kw)
            self.log.append(f"DELETE {args or kw}")

        def logged_create_row(*args: Any, **kw: Any) -> dict:
            out = create_row(*args, **kw)
            self.log.append(f"CREATE-ROW {out['id']}")
            return out

        def logged_delete_row(*args: Any, **kw: Any) -> None:
            delete_row(*args, **kw)
            self.log.append(f"DELETE-ROW {args or kw}")

        c.create_config.side_effect = logged_create
        c.update_config.side_effect = logged_update
        c.delete_config.side_effect = logged_delete
        c.create_config_row.side_effect = logged_create_row
        c.delete_config_row.side_effect = logged_delete_row
        return c

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
    rows: Sequence[tuple[str, str, str]] = (),
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

    def retired() -> dict:
        (main,) = [b for b in load_manifest(orders.root).branches if b.path == "main"]
        return main.metadata.get("retired_rows") or {}

    record = retired()
    assert any(key.endswith("/r2") for key in record), record
    moved = orders.move(orders.dir_of("500"), "orders-moved")
    orders.diff()
    orders.svc.status(orders.root)
    orders.push(dry_run=True)
    assert retired() == record, "a move or a read-only command changed the retirement record"

    cfg = orders.api.remote[PROD][(HTTP, "500")]
    cfg["rows"] = [r for r in cfg["rows"] if r["id"] != "r2"]  # UI deletes r2
    orders.pull()
    (main_dir,) = orders.dirs_of("500")
    assert main_dir == moved or not moved.exists()
    row_ids = sorted(
        yaml.safe_load(f.read_text())["_keboola"]["row_id"]
        for f in (main_dir / "rows").rglob("_config.yml")
    )
    assert row_ids == ["r1"], "a row the remote deleted came back"
    (entry,) = [e for e in orders.entries("500") if e.path == orders.rel(main_dir)]
    assert [r.id for r in entry.rows] == ["r1"]
    diff = orders.diff()
    assert not any(c["change_type"] in ("added", "deleted") for c in diff["changes"])
    orders.push(force=True)
    assert orders.api.writes("CREATE") == [] and orders.api.writes("DELETE") == []
    assert [r["id"] for r in orders.api.remote[PROD][(HTTP, "500")]["rows"]] == ["r1"]


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
    from keboola_agent_cli.services._sync_moves import settle_moves

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
    return ConfigService(config_store=t.svc._config_store, client_factory=lambda u, k: MagicMock())


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
    # The promoted copy keeps its baseline: a rename changes neither its
    # branch's remote state nor the files, so its diff stays clean.
    (dev_entry,) = orders.entries(dev_id)
    assert dev_entry.metadata.get("pull_config_hash")
    assert orders.diff(branch=DEV)["changes"] == []
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


# ---------------------------------------------------------------------------
# Round-1 review findings (F1-F5)
# ---------------------------------------------------------------------------


def _row_dir(config_dir: Path, row_id: str) -> Path:
    (found,) = [
        f.parent
        for f in (config_dir / "rows").rglob("_config.yml")
        if yaml.safe_load(f.read_text())["_keboola"]["row_id"] == row_id
    ]
    return found


def test_f1_removed_config_file_in_a_surviving_dir_is_no_move(orders: Tree) -> None:
    a = orders.dir_of("500")
    b = a.parent / "orders-copy"
    shutil.copytree(a, b)
    (a / "_config.yml").unlink()
    r1 = _row_dir(a, "r1") / "_config.yml"
    data = yaml.safe_load(r1.read_text())
    data["parameters"]["value"] = "edited-in-a"
    r1.write_text(yaml.safe_dump(data, sort_keys=False))
    shutil.rmtree(_row_dir(b, "r1"))

    diff = orders.diff()
    assert "moves" not in diff
    r1_changes = [c for c in diff["changes"] if c.get("is_row") and c["config_id"] == "r1"]
    assert not any(c["change_type"] == "deleted" for c in r1_changes), r1_changes
    orders.push(force=True)
    assert not any(e.startswith("DELETE-ROW") and "r1" in e for e in orders.api.log)


@pytest.mark.parametrize("pull_kw", [{}, {"force": True}, {"theirs": True}])
def test_f2_held_move_survives_a_remote_delete_and_pull(orders: Tree, pull_kw: dict) -> None:
    (entry_before,) = orders.entries("500")
    new = orders.move(orders.dir_of("500"), "orders-moved")
    shutil.copytree(new, new.parent / "orders-copy")
    del orders.api.remote[PROD][(HTTP, "500")]
    pulled = orders.pull(**pull_kw)
    assert not any(d["action"] == "removed" for d in pulled["details"])
    (entry,) = orders.entries("500")
    assert entry.path == entry_before.path
    diff = orders.diff()
    assert not any(c["change_type"] == "added" for c in diff["changes"])
    assert diff["ambiguous_moves"][0]["config_id"] == "500"
    orders.push(force=True)
    assert orders.api.log == [], "a held ambiguous move was created"
    assert remote_ids(orders.api) == []


def test_f3_rename_on_a_branch_the_tree_does_not_track_touches_nothing(orders: Tree) -> None:
    main_dir = orders.dir_of("500")
    before = orders.manifest_bytes()
    result = _config_service(orders)._rename_sync_directory(
        directory=orders.root,
        component_id=HTTP,
        config_id="500",
        new_name="Renamed On Dev",
        branch_id=DEV,
    )
    assert result is not None and "skipped" in result
    assert main_dir.exists(), "a dev-branch rename renamed the production dir"
    assert orders.manifest_bytes() == before


def test_f3_rename_config_reports_the_skipped_local_rename(orders: Tree) -> None:
    client = MagicMock()
    client.get_config_detail.return_value = {"name": "Orders"}
    svc = ConfigService(config_store=orders.svc._config_store, client_factory=lambda u, k: client)
    result = svc.rename_config(
        alias="prod",
        component_id=HTTP,
        config_id="500",
        name="Renamed",
        branch_id=DEV,
        directory=orders.root,
    )
    assert "sync" not in result
    assert "not changed" in result["sync_skipped"]
    orders.dir_of("500")


def _three(tmp_path: Path) -> Tree:
    t = Tree(tmp_path)
    put(t.api, PROD, "500", "Orders", "a")
    put(t.api, PROD, "600", "Query", "b")
    put(t.api, PROD, "700", "Report", "c")
    t.init()
    t.pull()
    return t


def _assert_held(t: Tree, ids: set[str], branch: int | None = None) -> dict:
    """Diff holds every id in *ids* as an identity conflict; nothing else changes."""
    diff = t.diff(branch=branch)
    held = {c["config_id"] for c in diff["changes"] if c.get("identity_conflict")}
    assert held == ids, diff["changes"]
    assert all(c["change_type"] == "conflict" for c in diff["changes"]), diff["changes"]
    assert "moves" not in diff
    assert diff["identity_conflicts"]
    return diff


def _values(t: Tree) -> dict[str, str]:
    return {
        cid: cfg["configuration"]["parameters"]["value"]
        for (_c, cid), cfg in t.api.remote[PROD].items()
    }


def _held_push_twice(t: Tree, ids: set[str]) -> None:
    """Push and push --force twice: nothing reaches the remote, the manifest is unchanged."""
    before_remote = _values(t)
    before = t.manifest_bytes()
    _assert_held(t, ids)
    t.push()
    assert t.manifest_bytes() == before, "an identity conflict was persisted"
    t.push(force=True)
    t.push(force=True)
    assert t.api.log == []
    assert _values(t) == before_remote
    assert t.manifest_bytes() == before


def test_f4_dir_replaced_by_another_config_is_held_across_two_pushes(tmp_path: Path) -> None:
    t = _three(tmp_path)
    a, b = t.dir_of("500"), t.dir_of("600")
    shutil.rmtree(b)
    shutil.move(str(a), str(b))  # 500 now lives in 600's old directory
    _held_push_twice(t, {"500", "600"})


def test_f4_rotation_of_two_is_held(tmp_path: Path) -> None:
    t = _three(tmp_path)
    a, b = t.dir_of("500"), t.dir_of("600")
    shutil.move(str(b), str(b.parent / "query-old"))
    shutil.move(str(a), str(b))
    _held_push_twice(t, {"500", "600"})


def test_f4_rotation_of_three_is_held(tmp_path: Path) -> None:
    t = _three(tmp_path)
    a, b, c = t.dir_of("500"), t.dir_of("600"), t.dir_of("700")
    shutil.move(str(c), str(c.parent / "report-old"))
    shutil.move(str(b), str(c))
    shutil.move(str(a), str(b))
    _held_push_twice(t, {"500", "600", "700"})


def test_f4_rotation_plus_copy_is_held(tmp_path: Path) -> None:
    t = _three(tmp_path)
    a, b = t.dir_of("500"), t.dir_of("600")
    old = b.parent / "query-old"
    shutil.move(str(b), str(old))
    shutil.move(str(a), str(b))
    shutil.copytree(old, b.parent / "query-copy")
    _held_push_twice(t, {"500", "600"})
    shutil.rmtree(b.parent / "query-copy")
    _held_push_twice(t, {"500", "600"})


def test_f4_symlink_to_another_config_dir_is_held(tmp_path: Path) -> None:
    t = _three(tmp_path)
    a, b = t.dir_of("500"), t.dir_of("600")
    shutil.move(str(b), str(b.parent / "query-old"))
    try:
        b.symlink_to(a, target_is_directory=True)
    except OSError:
        pytest.skip("no symlinks here")
    _held_push_twice(t, {"500", "600"})


def test_f4_symlink_is_never_a_move_target(orders: Tree) -> None:
    a = orders.dir_of("500")
    real = orders.root / "elsewhere" / "orders"
    real.parent.mkdir()
    shutil.move(str(a), str(real))
    try:
        (a.parent / "orders-link").symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("no symlinks here")
    diff = orders.diff()
    assert "moves" not in diff


def test_f4_swap_is_held_and_moving_back_resolves_it(tmp_path: Path) -> None:
    t = _three(tmp_path)
    a, b = t.dir_of("500"), t.dir_of("600")
    tmp = a.parent / "swap-tmp"
    shutil.move(str(a), str(tmp))
    shutil.move(str(b), str(a))
    shutil.move(str(tmp), str(b))
    _held_push_twice(t, {"500", "600"})
    pulled = t.pull(theirs=True)
    assert {d["config_id"] for d in pulled["details"] if d["action"] == "skipped"} >= {"500", "600"}
    assert t.api.log == []
    shutil.move(str(a), str(tmp))  # move them back
    shutil.move(str(b), str(a))
    shutil.move(str(tmp), str(b))
    diff = t.diff()
    assert diff["changes"] == [] and "identity_conflicts" not in diff
    edit_value(t.dir_of("500"), "after")
    t.push()
    assert t.api.writes("UPDATE") == ["UPDATE 500"]


def test_f4_held_conflict_keeps_the_promoted_copy(tmp_path: Path) -> None:
    t = _three(tmp_path)
    t.push(branch=DEV)  # 500, 600, 700 promoted to DEV
    dev_before = remote_ids(t.api, DEV)
    (dev_600,) = [
        e.id
        for e in load_manifest(t.root).configurations
        if e.branch_id == DEV and (e.metadata.get("promoted_from") or {}).get("config_id") == "600"
    ]
    t.api.log.clear()
    a, b = t.dir_of("500"), t.dir_of("600")
    shutil.rmtree(b)
    shutil.move(str(a), str(b))
    t.pull()
    assert dev_600 in {e.id for e in load_manifest(t.root).configurations}
    t.push(branch=DEV)
    t.push(branch=DEV, force=True)
    assert t.api.log == [], "a held conflict created or changed a dev config"
    assert remote_ids(t.api, DEV) == dev_before
    assert dev_600 in {e.id for e in load_manifest(t.root).configurations}


def test_f5_move_in_a_git_branching_tree(tmp_path: Path) -> None:
    from unittest.mock import patch

    t = Tree(tmp_path)
    put(t.api, PROD, "500", "Orders", "a", rows=[("r1", "One", "x")])
    with (
        patch("keboola_agent_cli.services.sync_service.is_git_repo", return_value=True),
        patch("keboola_agent_cli.services.sync_service.get_default_branch", return_value="main"),
        patch("keboola_agent_cli.sync.git_utils.get_current_branch", return_value="main"),
    ):
        t.svc.init_sync(alias="prod", project_root=t.root, git_branching=True)
        assert load_manifest(t.root).git_branching.enabled
        t.pull()
        new = t.move(t.dir_of("500"), "orders-moved")
        edit_value(new, "moved-and-edited")
        before = t.manifest_bytes()
        diff = t.diff()
        assert [(c["change_type"], c["config_id"]) for c in diff["changes"]] == [
            ("modified", "500")
        ]
        assert diff["moves"][0]["moved_to"] == t.rel(new)
        assert t.manifest_bytes() == before
        t.push(force=True)
        assert t.api.writes("CREATE") == [] and t.api.writes("DELETE") == []
        (entry,) = t.entries("500")
        assert entry.path == t.rel(new)
        assert t.diff()["changes"] == []
        t.pull()
        assert t.api.writes("CREATE") == [] and len(t.entries("500")) == 1
        assert t.diff()["changes"] == []
    value = t.api.remote[PROD][(HTTP, "500")]["configuration"]["parameters"]["value"]
    assert value == "moved-and-edited"


def test_f4_hashless_promoted_entry_is_held_with_its_source(tmp_path: Path) -> None:
    """A promoted entry an interrupted create left without pull_hash is held too."""
    t = _three(tmp_path)
    t.push(branch=DEV)
    path = t.root / MANIFEST
    data = json.loads(path.read_text())
    (dev_600,) = [
        c
        for c in data["configurations"]
        if c["branchId"] == DEV
        and (c["metadata"].get("promoted_from") or {}).get("config_id") == "600"
    ]
    dev_600["metadata"].pop("pull_hash", None)  # as the create checkpoint leaves it
    path.write_text(json.dumps(data))
    dev_before = {cid: dict(cfg["configuration"]) for (_c, cid), cfg in t.api.remote[DEV].items()}
    t.api.log.clear()
    a, b = t.dir_of("500"), t.dir_of("600")
    shutil.rmtree(b)
    shutil.move(str(a), str(b))
    before = t.manifest_bytes()
    diff = t.diff(branch=DEV)
    assert not any(c["change_type"] == "modified" for c in diff["changes"]), diff["changes"]
    t.push(branch=DEV)
    t.push(branch=DEV, force=True)
    assert t.api.log == [], "the hashless promoted copy was pushed with another config's content"
    after = {cid: dict(cfg["configuration"]) for (_c, cid), cfg in t.api.remote[DEV].items()}
    assert after == dev_before
    assert t.manifest_bytes() == before


# ---------------------------------------------------------------------------
# Human output through the CLI (PR #936 review): a pure move is no change, so
# every human renderer must say it instead of "in sync" / "no local changes".
# ---------------------------------------------------------------------------


def _human(tmp_path: Path, args: list[str], method: str, result: dict) -> str:
    from typer.testing import CliRunner

    from keboola_agent_cli.cli import app
    from keboola_agent_cli.services.project_service import ProjectService

    store = setup_single_project(tmp_path / "cli-cfg")
    service = MagicMock()
    getattr(service, method).return_value = result
    with (
        patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
        patch(
            "keboola_agent_cli.cli.ProjectService", return_value=ProjectService(config_store=store)
        ),
        patch("keboola_agent_cli.cli.SyncService", return_value=service),
    ):
        out = CliRunner().invoke(app, [*args, "--directory", str(tmp_path)])
    assert out.exit_code == 0, out.output
    return " ".join(out.output.split())


MOVE = {
    "component_id": HTTP,
    "config_id": "500",
    "branch_id": PROD,
    "moved_from": "extractor/keboola.ex-http/orders",
    "moved_to": "extractor/keboola.ex-http/orders-moved",
}


def test_cli_human_diff_names_a_pure_move(tmp_path: Path) -> None:
    result = {
        "changes": [],
        "moves": [MOVE],
        "summary": {"added": 0, "modified": 0, "deleted": 0, "unchanged": 1, "moved": 1},
    }
    text = _human(tmp_path, ["sync", "diff", "--project", "prod"], "diff", result)
    assert f"MOVED {HTTP}/500: {MOVE['moved_from']} -> {MOVE['moved_to']}" in text


def test_cli_human_diff_names_held_moves(tmp_path: Path) -> None:
    result = {
        "changes": [],
        "ambiguous_moves": [{"message": "ambiguous-hint-text"}],
        "identity_conflicts": [{"message": "identity-hint-text"}],
        "summary": {"added": 0, "modified": 0, "deleted": 0, "unchanged": 0, "moved": 0},
    }
    text = _human(tmp_path, ["sync", "diff", "--project", "prod"], "diff", result)
    assert "AMBIGUOUS MOVE ambiguous-hint-text" in text
    assert "IDENTITY CONFLICT identity-hint-text" in text


def test_cli_human_status_names_a_pure_move(tmp_path: Path) -> None:
    result = {
        "modified": [],
        "added": [],
        "deleted": [],
        "never_fetched": [],
        "unchanged": 1,
        "total_tracked": 1,
        "plaintext_secret_warnings": [],
        "moves": [MOVE],
    }
    text = _human(tmp_path, ["sync", "status"], "status", result)
    assert f"MOVED {HTTP}/500: {MOVE['moved_from']} -> {MOVE['moved_to']}" in text


def test_cli_human_pull_names_a_pure_move(tmp_path: Path) -> None:
    result = {
        "status": "pulled",
        "project_alias": "prod",
        "branch_id": PROD,
        "branch_dir": "main",
        "folder_lookup_failed": False,
        "configs_pulled": 0,
        "rows_pulled": 0,
        "files_written": 0,
        "jobs_written": 0,
        "storage": {},
        "details": [
            {
                "action": "moved",
                "component_id": HTTP,
                "config_id": "500",
                "config_name": "orders-moved",
                "path": MOVE["moved_to"],
                "old_path": MOVE["moved_from"],
            }
        ],
    }
    text = _human(tmp_path, ["sync", "pull", "--project", "prod"], "pull", result)
    assert "Already up to date" not in text
    assert f"{MOVE['moved_from']} -> {HTTP}/orders-moved" in text
