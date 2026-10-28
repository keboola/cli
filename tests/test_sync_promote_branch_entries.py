"""A manifest entry belongs to exactly one branch (promote path, KFR-07 / #792 D).

``sync push --branch DEV`` with no ``dev/`` tree reads ``main/``, whose entries
belong to production. A dev branch created from production holds the same
config ids, so every write site must pick the entry of the branch it writes to,
and pull must never act on another branch's entry. Each test drives one of the
changed write sites through the real ``SyncService`` over the in-memory API of
the bookkeeping differential test.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.services._sync_bindings import _refresh_binding_hashes
from keboola_agent_cli.services._sync_models import CreatedConfig
from keboola_agent_cli.services._sync_writeback import writeback_create_config_in_manifest
from keboola_agent_cli.services.component_service import materialize_pushed_config
from keboola_agent_cli.sync.branch_scope import same_branch
from keboola_agent_cli.sync.code_extraction import merge_code_files
from keboola_agent_cli.sync.config_format import api_config_to_local
from keboola_agent_cli.sync.diff_engine import config_hash
from keboola_agent_cli.sync.manifest import (
    Manifest,
    ManifestBranch,
    ManifestConfiguration,
    ManifestNaming,
    ManifestProject,
    load_manifest,
    save_manifest,
)
from test_sync_bookkeeping_differential import (
    CONFIG_FILE,
    DEV,
    PROD,
    TOKEN,
    VARIANTS,
    World,
    _rows,
    _seeded,
    author_config,
    author_row,
    semantic_edit,
)


def _entries(world: World, config_id: str, branch: int) -> list[ManifestConfiguration]:
    manifest = load_manifest(world.root)
    return [
        c
        for c in manifest.configurations
        if c.id == config_id and same_branch(manifest, c.branch_id, branch)
    ]


def _manifest(
    *branches: tuple[int, str], configurations: list[ManifestConfiguration] | None = None
) -> Manifest:
    return Manifest(
        project=ManifestProject(id=258, apiHost="connection.example.com"),
        naming=ManifestNaming(),
        branches=[ManifestBranch(id=b, path=path) for b, path in branches],
        configurations=configurations or [],
    )


def _file_id(config_dir: Path) -> str:
    data = yaml.safe_load((config_dir / CONFIG_FILE).read_text(encoding="utf-8"))
    return str(data["_keboola"]["config_id"])


# -- the branch normalizer ----------------------------------------------------


def test_same_branch_treats_every_spelling_of_production_alike() -> None:
    manifest = _manifest((PROD, "main"), (DEV, "dev"))
    assert same_branch(manifest, None, 0)
    assert same_branch(manifest, 0, PROD)
    assert same_branch(manifest, None, PROD)
    assert not same_branch(manifest, PROD, DEV)
    assert same_branch(manifest, DEV, DEV)


def test_create_writeback_updates_a_placeholder_recorded_under_the_default_branch_id() -> None:
    """A production push targets ``None`` while pull records the default branch id."""
    placeholder = ManifestConfiguration(
        branchId=PROD, componentId="keboola.ex-http", id="draft", path="extractor/x"
    )
    manifest = _manifest((PROD, "main"), configurations=[placeholder])
    result = writeback_create_config_in_manifest(
        manifest=manifest,
        component_id="keboola.ex-http",
        branch_id=None,
        config_path_str="extractor/x",
        new_id="cfg-1",
        file_hash="f",
        cfg_hash="c",
        extra_hashes={},
    )
    assert result.previous_id == "draft"
    assert [c.id for c in manifest.configurations] == ["cfg-1"]


# -- push --branch DEV onto a dev branch that has the config (same id) -------


def test_promote_update_records_a_dev_shadow_and_leaves_the_prod_entry(tmp_path: Path) -> None:
    v = VARIANTS["sql"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    w.api.branch_from_prod()
    config_dir = w.config_dir(cid)
    (prod_before,) = _entries(w, cid, PROD)
    semantic_edit(config_dir / CONFIG_FILE)
    w.push(branch=DEV)

    (prod_after,) = _entries(w, cid, PROD)
    assert prod_after.metadata == prod_before.metadata
    (shadow,) = _entries(w, cid, DEV)
    assert shadow.path == prod_after.path
    dev_cfg = api_config_to_local(v.component_id, w.api.get(DEV, v.component_id, cid), cid)
    assert shadow.metadata["pull_config_hash"] == config_hash(dev_cfg)
    assert shadow.metadata["pull_hash"] == w.svc._file_hash(config_dir / CONFIG_FILE)
    # Production still lacks the edit: a local change, not a remote one.
    summary = w.diff()["summary"]
    assert (summary["modified"], summary["remote_modified"]) == (1, 0)
    assert w.diff(branch=DEV)["summary"]["modified"] == 0


def test_shadow_copies_only_the_rows_the_dev_branch_has(tmp_path: Path) -> None:
    v = VARIANTS["rows"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    w.api.branch_from_prod()
    dev_rows = w.api.get(DEV, v.component_id, cid)["rows"]
    gone = dev_rows.pop(0)["id"]
    semantic_edit(w.config_dir(cid) / CONFIG_FILE)
    w.push(branch=DEV)

    (prod,) = _entries(w, cid, PROD)
    (shadow,) = _entries(w, cid, DEV)
    assert gone in {r.id for r in prod.rows}
    assert {r.id for r in shadow.rows} == {
        r["id"] for r in w.api.get(DEV, v.component_id, cid)["rows"]
    }


def test_rows_of_a_shadow_are_not_created_again_on_the_next_dev_push(tmp_path: Path) -> None:
    v = VARIANTS["rows"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    w.api.branch_from_prod()
    semantic_edit(w.config_dir(cid) / CONFIG_FILE)
    w.push(branch=DEV)

    summary = w.diff(branch=DEV)["summary"]
    assert summary["added"] == 0
    assert w.push(branch=DEV)["status"] == "no_changes"
    assert len(w.api.get(DEV, v.component_id, cid)["rows"]) == 2


def test_row_created_on_dev_is_recorded_under_the_dev_entry(tmp_path: Path) -> None:
    v = VARIANTS["rows"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    w.api.branch_from_prod()
    (prod_before,) = _entries(w, cid, PROD)
    author_row(w.config_dir(cid), v, {**_rows()[0], "name": "Row 3"})
    w.push(branch=DEV)

    (prod_after,) = _entries(w, cid, PROD)
    assert [r.id for r in prod_after.rows] == [r.id for r in prod_before.rows]
    (shadow,) = _entries(w, cid, DEV)
    assert "rows/row-3" in {r.path for r in shadow.rows}
    assert len(shadow.rows) == 3
    # The new row is still pending for production.
    assert w.diff()["summary"]["added"] == 1


def test_force_delete_on_dev_keeps_the_prod_entry(tmp_path: Path) -> None:
    v = VARIANTS["extractor"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    w.api.branch_from_prod()
    shutil.rmtree(w.config_dir(cid))
    assert w.push(branch=DEV, force=True)["deleted"] == 1

    assert (v.component_id, cid) not in w.api.remote[DEV]
    assert len(_entries(w, cid, PROD)) == 1
    assert w.diff()["summary"]["deleted"] == 1  # still pending for production


# -- push --branch DEV onto a dev branch that lacks the config ---------------


def test_promote_create_keeps_the_prod_id_in_main(tmp_path: Path) -> None:
    v = VARIANTS["rows"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    config_dir = w.config_dir(cid)
    w.push(branch=DEV)

    ((_, dev_id),) = w.api.remote[DEV]
    assert dev_id != cid
    assert _file_id(config_dir) == cid
    (dev_entry,) = _entries(w, dev_id, DEV)
    assert dev_entry.path == _entries(w, cid, PROD)[0].path
    # The rows went under the new dev config and are recorded there.
    assert len(w.api.get(DEV, v.component_id, dev_id)["rows"]) == 2
    assert len(dev_entry.rows) == 2


def test_prod_pull_keeps_the_promote_entry_and_its_directory(tmp_path: Path) -> None:
    v = VARIANTS["sql"]
    w = World(tmp_path)
    w.api.types[v.component_id] = v.component_type
    w.init()
    w.pull()
    config_dir = author_config(w.root / "main", v)
    w.push(branch=DEV)
    ((_, dev_id),) = w.api.remote[DEV]

    result = w.pull()
    assert config_dir.is_dir()
    assert [d for d in result["details"] if d["action"] == "removed"] == []
    assert len(_entries(w, dev_id, DEV)) == 1
    # The next promote updates that config instead of creating another one.
    semantic_edit(config_dir / "transform.sql")
    assert w.push(branch=DEV)["updated"] == 1
    assert len(w.api.remote[DEV]) == 1


# -- pull after the manifest was re-targeted to another branch ---------------


def test_pull_writes_into_the_dir_that_carries_the_config_id(tmp_path: Path) -> None:
    """A name changed since the tree was pulled must not leave a copy behind."""
    v = VARIANTS["extractor"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    config_dir = w.config_dir(cid)
    data = yaml.safe_load((config_dir / CONFIG_FILE).read_text(encoding="utf-8"))
    data["name"] = "Renamed"
    (config_dir / CONFIG_FILE).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    w.push()
    w.pull(branch=DEV)  # re-targets the manifest; main/ stays as it was
    w.pull()

    assert [c.path for c in load_manifest(w.root).configurations] == [
        config_dir.relative_to(w.root / "main").as_posix()
    ]
    assert w.diff()["summary"]["added"] == 0


# -- description of a config without _description.md ------------------------


def test_merge_without_description_file_means_empty_description(tmp_path: Path) -> None:
    data: dict = {"name": "x", "parameters": {}}
    merge_code_files("keboola.ex-http", data, tmp_path)
    assert data["description"] == ""
    (tmp_path / "_description.md").write_text("text", encoding="utf-8")
    merge_code_files("keboola.ex-http", data, tmp_path)
    assert data["description"] == "text"


# ===========================================================================
# Round 2 (design D1', D13-D21): regression tests per risk
# ===========================================================================


class _Crash(BaseException):
    """A process death in the middle of a push (not caught by ``except Exception``)."""


def _yaml(config_dir: Path) -> dict:
    return yaml.safe_load((config_dir / CONFIG_FILE).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Promoted:
    """A config authored in main/ and pushed to an empty dev branch."""

    world: World
    config_dir: Path
    dev_id: str


def _authored_and_promoted(tmp_path: Path, variant: str = "extractor") -> Promoted:
    v = VARIANTS[variant]
    w = World(tmp_path)
    w.api.types[v.component_id] = v.component_type
    w.init()
    w.pull()
    config_dir = author_config(w.root / "main", v)
    w.push(branch=DEV)
    ((_, dev_id),) = w.api.remote[DEV]
    return Promoted(world=w, config_dir=config_dir, dev_id=dev_id)


@dataclass(frozen=True)
class Retargeted:
    """Production pulled, then ``pull --branch DEV``: main/ is rediscovered next."""

    world: World
    config_id: str
    config_dir: Path


def _retargeted(tmp_path: Path, variant: str) -> Retargeted:
    v = VARIANTS[variant]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    config_dir = w.config_dir(cid)
    w.api.branch_from_prod()
    w.pull(branch=DEV)
    return Retargeted(world=w, config_id=cid, config_dir=config_dir)


# -- risk 3 / D1': unknown ids are never the same branch ----------------------


def test_unregistered_dev_branch_ids_are_never_the_same_branch() -> None:
    manifest = _manifest((PROD, "main"))
    assert not same_branch(manifest, 99999, 88888)
    assert not same_branch(manifest, None, 99999)
    assert not same_branch(manifest, PROD, 99999)
    assert same_branch(manifest, 99999, 99999)
    unregistered = _manifest()
    assert not same_branch(unregistered, PROD, DEV)
    assert same_branch(unregistered, None, 0)


# -- risk 4 / D18: the promoted config is tracked under its dev id ----------


def test_dev_diff_tracks_a_promoted_config_under_its_dev_id(tmp_path: Path) -> None:
    v = VARIANTS["extractor"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    w.push(branch=DEV)  # the dev branch lacks it: created under a new id
    result = w.diff(branch=DEV)
    assert result["remote_only"] == []
    assert result["changes"] == []
    assert _file_id(w.config_dir(cid)) == cid


# -- risk 5 / D15: a rediscovered row keeps its local edit ------------------


def test_pull_keeps_an_edited_row_of_a_rediscovered_config(tmp_path: Path) -> None:
    r = _retargeted(tmp_path, "rows")
    w, config_dir = r.world, r.config_dir
    row_file = config_dir / "rows" / "row-1" / CONFIG_FILE
    semantic_edit(row_file)
    result = w.pull()

    assert "edit_marker" in row_file.read_text(encoding="utf-8")
    assert any(
        d["action"] == "skipped" and d["path"].endswith("rows/row-1") for d in result["details"]
    )
    assert w.diff()["summary"]["modified"] == 1  # pending, not hidden


def test_pull_keeps_an_edited_row_in_a_noncanonical_folder(tmp_path: Path) -> None:
    r = _retargeted(tmp_path, "rows")
    w, cid, config_dir = r.world, r.config_id, r.config_dir
    custom = config_dir / "rows" / "my-own-name"
    (config_dir / "rows" / "row-1").rename(custom)
    semantic_edit(custom / CONFIG_FILE)
    w.pull()

    assert "edit_marker" in (custom / CONFIG_FILE).read_text(encoding="utf-8")
    assert not (config_dir / "rows" / "row-1").exists()
    (entry,) = _entries(w, cid, PROD)
    assert "rows/my-own-name" in {r.path for r in entry.rows}


# -- risk 6 / D14: owned paths are reserved, whatever the remote order ------


@pytest.mark.parametrize("new_first", [True, False], ids=["new-listed-first", "new-listed-last"])
def test_new_config_never_lands_in_a_dir_another_config_owns(
    tmp_path: Path, new_first: bool
) -> None:
    v = VARIANTS["extractor"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    config_dir = w.config_dir(cid)
    before = (config_dir / CONFIG_FILE).read_bytes()
    new = w.api._create(v.component_id, "Orders", v.body(), "same name, other config")
    if new_first:
        prod = w.api.remote[PROD]
        w.api.remote[PROD] = {k: prod[k] for k in sorted(prod, key=lambda k: k[1] != new["id"])}
    w.pull()

    assert (config_dir / CONFIG_FILE).read_bytes() == before
    paths = {c.id: c.path for c in load_manifest(w.root).configurations}
    assert paths[cid] == config_dir.relative_to(w.root / "main").as_posix()
    assert paths[new["id"]] != paths[cid]


@pytest.mark.parametrize("new_first", [True, False], ids=["new-listed-first", "new-listed-last"])
def test_new_row_never_lands_in_a_rediscovered_row_dir(tmp_path: Path, new_first: bool) -> None:
    r = _retargeted(tmp_path, "rows")
    w, cid, config_dir = r.world, r.config_id, r.config_dir
    row_file = config_dir / "rows" / "row-1" / CONFIG_FILE
    before = row_file.read_bytes()
    rows = w.api.get(PROD, VARIANTS["rows"].component_id, cid)["rows"]
    old_id = rows[0]["id"]
    new_row = {
        "id": "row-new",
        "name": "Row 1",  # the same folder name as the rediscovered row
        "description": "",
        "configuration": {"parameters": {"table": "other"}},
        "isDisabled": False,
    }
    if new_first:
        rows.insert(0, new_row)
    else:
        rows.append(new_row)
    w.pull()

    assert row_file.read_bytes() == before
    (entry,) = _entries(w, cid, PROD)
    paths = {r.id: r.path for r in entry.rows}
    assert paths[old_id] == "rows/row-1"
    assert paths["row-new"] != "rows/row-1"
    assert len(set(paths.values())) == len(paths)


def test_a_row_id_carried_by_two_dirs_adopts_neither(tmp_path: Path) -> None:
    r = _retargeted(tmp_path, "rows")
    w, cid, config_dir = r.world, r.config_id, r.config_dir
    original = config_dir / "rows" / "row-1"
    copy_dir = config_dir / "rows" / "row-1-copy"
    shutil.copytree(original, copy_dir)
    semantic_edit(copy_dir / CONFIG_FILE)
    before = {d: (d / CONFIG_FILE).read_bytes() for d in (original, copy_dir)}
    result = w.pull()

    assert {d: (d / CONFIG_FILE).read_bytes() for d in before} == before
    assert any(x["change_type"] == "ambiguous_row_id" for x in result.get("warnings", []))
    (entry,) = _entries(w, cid, PROD)
    assert not {r.path for r in entry.rows} & {"rows/row-1", "rows/row-1-copy"}


# -- risks 7 / 17 / D13a: a production rename keeps the association ---------


def test_r3_prod_rename_after_promote_create_does_not_duplicate_on_dev(tmp_path: Path) -> None:
    v = VARIANTS["extractor"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    w.push(branch=DEV)  # DEV lacks the config -> created there
    assert len(w.api.remote[DEV]) == 1
    w.api.remote[PROD][(v.component_id, cid)]["name"] = "Renamed In Prod"
    w.pull()  # renames main/<orders> -> main/<renamed-in-prod>
    w.push(branch=DEV)
    assert len(w.api.remote[DEV]) == 1, sorted(w.api.remote[DEV])


# -- risk 8 / D16: promoted references reach the DEV payload ---------------


def test_promoted_flow_runs_the_dev_id_of_the_promoted_extractor(tmp_path: Path) -> None:
    v = VARIANTS["extractor"]
    w = World(tmp_path)
    extractor_id = w.api.seed(v)
    w.api.types["keboola.flow"] = "other"
    w.api.remote[PROD][("keboola.flow", "cfg-flow")] = {
        "id": "cfg-flow",
        "name": "Nightly",
        "description": "",
        "configuration": {
            "phases": [{"id": "1", "name": "P", "dependsOn": []}],
            "tasks": [
                {
                    "id": "t1",
                    "name": "T",
                    "phase": "1",
                    "enabled": True,
                    "task": {
                        "type": "job",
                        "componentId": v.component_id,
                        "configId": extractor_id,
                        "mode": "run",
                    },
                }
            ],
        },
        "rows": [],
        "isDisabled": False,
    }
    w.init()
    w.pull()
    flow_file = w.config_dir("cfg-flow") / CONFIG_FILE
    before = flow_file.read_bytes()
    w.push(branch=DEV)

    (dev_extractor,) = [cid for comp, cid in w.api.remote[DEV] if comp == v.component_id]
    (dev_flow,) = [cfg for (comp, _), cfg in w.api.remote[DEV].items() if comp == "keboola.flow"]
    assert dev_extractor != extractor_id
    assert dev_flow["configuration"]["tasks"][0]["task"]["configId"] == dev_extractor
    assert flow_file.read_bytes() == before  # main/ keeps the production ids


# -- risk 9 / D13b: deleting a promoted dir is a local delete for DEV --------


def test_deleted_promoted_dir_is_a_held_back_deletion_without_force(tmp_path: Path) -> None:
    v = VARIANTS["extractor"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    config_dir = w.config_dir(cid)
    w.push(branch=DEV)
    ((_, dev_id),) = w.api.remote[DEV]
    shutil.rmtree(config_dir)
    result = w.push(branch=DEV)

    assert [d["config_id"] for d in result["skipped_deletions"]] == [dev_id]
    assert (v.component_id, dev_id) in w.api.remote[DEV]


def test_force_push_deletes_only_the_dev_config_of_a_deleted_promoted_dir(tmp_path: Path) -> None:
    v = VARIANTS["extractor"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    config_dir = w.config_dir(cid)
    w.push(branch=DEV)
    ((_, dev_id),) = w.api.remote[DEV]
    shutil.rmtree(config_dir)
    assert w.push(branch=DEV, force=True)["deleted"] == 1

    assert w.api.remote[DEV] == {}
    assert (v.component_id, cid) in w.api.remote[PROD]
    assert len(_entries(w, cid, PROD)) == 1
    assert _entries(w, dev_id, DEV) == []


# -- risk 10 / D17: a crash after a create does not create it again ----------


def test_crash_after_the_first_promote_create_does_not_create_it_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v = VARIANTS["extractor"]
    w = World(tmp_path)
    w.api.types[v.component_id] = v.component_type
    w.init()
    w.pull()
    author_config(w.root / "main", v, "First")
    author_config(w.root / "main", v, "Second")
    original = w.api._create
    calls: list[str] = []

    def create_then_crash(*args: Any, **kwargs: Any) -> dict:
        calls.append("create")
        if len(calls) == 2:
            raise _Crash
        return original(*args, **kwargs)

    monkeypatch.setattr(w.api, "_create", create_then_crash)
    with pytest.raises(_Crash):
        w.svc.push(alias="prod", project_root=w.root, branch_override=DEV)
    monkeypatch.undo()
    w.push(branch=DEV)  # the retry

    assert len(w.api.remote[DEV]) == 2


def test_crash_after_the_first_row_create_does_not_create_rows_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v = VARIANTS["rows"]
    seeded = _seeded(tmp_path, v)
    w = seeded.world
    original = w.api._create_row
    calls: list[str] = []

    def create_row_then_crash(*args: Any, **kwargs: Any) -> dict:
        calls.append("row")
        if len(calls) == 2:
            raise _Crash
        return original(*args, **kwargs)

    monkeypatch.setattr(w.api, "_create_row", create_row_then_crash)
    with pytest.raises(_Crash):
        w.svc.push(alias="prod", project_root=w.root, branch_override=DEV)
    monkeypatch.undo()
    w.push(branch=DEV)  # the retry

    ((_, cfg),) = w.api.remote[DEV].items()
    assert len(cfg["rows"]) == 2


# -- risk 11: unreadable dev rows stay visible -------------------------------


def test_unreadable_dev_rows_are_listed_not_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v = VARIANTS["rows"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    config_dir = w.config_dir(cid)
    w.api.branch_from_prod()
    gone = w.api.get(DEV, v.component_id, cid)["rows"].pop(0)["id"]
    original_update = w.api._update

    def update_without_rows(*args: Any, **kwargs: Any) -> dict:
        response = original_update(*args, **kwargs)
        response.pop("rows", None)
        return response

    def detail_fails(*_: Any, **__: Any) -> dict:
        raise KeboolaApiError("detail unavailable", 500, ErrorCode.API_ERROR)

    monkeypatch.setattr(w.api, "_update", update_without_rows)
    monkeypatch.setattr(w.api, "_detail", detail_fails)
    semantic_edit(config_dir / CONFIG_FILE)
    w.push(branch=DEV)

    changes = w.diff(branch=DEV)["changes"]
    assert any(c["config_id"] == gone and c["change_type"] == "remote_deleted" for c in changes)


# -- risk 14: the shadow's source is the source tree's entry -----------------


def test_shadow_source_is_the_source_tree_entry_with_three_branches(tmp_path: Path) -> None:
    v = VARIANTS["rows"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    config_dir = w.config_dir(cid)
    manifest = load_manifest(w.root)
    (prod,) = manifest.configurations
    other = ManifestConfiguration(
        branchId=77777,
        componentId=v.component_id,
        id=cid,
        path="extractor/elsewhere",
        metadata={"pull_hash": "other-branch"},
    )
    manifest.branches.append(ManifestBranch(id=77777, path="other"))
    manifest.configurations.insert(0, other)  # first match on any branch
    save_manifest(w.root, manifest)
    w.api.branch_from_prod()
    semantic_edit(config_dir / CONFIG_FILE)
    w.push(branch=DEV)

    (shadow,) = _entries(w, cid, DEV)
    assert shadow.path == prod.path
    assert {r.id for r in shadow.rows} == {r.id for r in prod.rows}
    (kept,) = _entries(w, cid, 77777)
    assert (kept.path, kept.metadata) == (other.path, other.metadata)


# -- risk 15: promote entries have their own reason in production diff -------


def test_prod_diff_reports_promote_entries_with_their_own_reason(tmp_path: Path) -> None:
    v = VARIANTS["extractor"]
    seeded = _seeded(tmp_path, v)
    w = seeded.world
    w.push(branch=DEV)
    w.pull()  # the promote entry is kept across the production pull
    orphaned = w.diff()["orphaned"]
    assert [o["reason"] for o in orphaned] == ["promoted_from_this_tree"]


# -- risk 18 / D20: an adopted production update is tracked at its own dir ---


def test_r4_adopted_prod_update_is_tracked_at_its_own_dir(tmp_path: Path) -> None:
    v = VARIANTS["extractor"]
    w = World(tmp_path)
    w.api.types[v.component_id] = v.component_type
    w.init()
    w.pull()
    created = w.api._create(v.component_id, "Scaffolded", v.body(), f"{TOKEN} description")
    w.api.branch_from_prod()
    w.api.remote[DEV][(v.component_id, created["id"])]["name"] = "Dev Renamed"
    w.pull(branch=DEV)
    d = w.root / "main" / v.component_type / v.component_id / "scaffolded"
    materialize_pushed_config(
        v.component_id,
        created["id"],
        created["name"],
        created["description"],
        created["configuration"],
        d,
    )
    semantic_edit(d / CONFIG_FILE)
    assert w.push()["updated"] == 1  # production, adopted by id

    summary = w.diff()["summary"]
    assert (summary["added"], summary["deleted"]) == (0, 0), summary
    w.push()
    assert len(w.api.remote[PROD]) == 1, sorted(w.api.remote[PROD])


# -- risk 19 / D14: a production pull never overwrites a promoted dir --------


def test_r5_prod_pull_does_not_overwrite_a_promoted_dir(tmp_path: Path) -> None:
    v = VARIANTS["extractor"]
    p = _authored_and_promoted(tmp_path)
    w, config_dir = p.world, p.config_dir
    semantic_edit(config_dir / CONFIG_FILE)  # local work after the promote
    # A different production config with the same name (same generated path).
    w.api._create(v.component_id, "Authored", v.body(), "someone else's")
    w.pull()

    assert "config_id" not in (_yaml(config_dir).get("_keboola") or {})
    assert "edit_marker" in (config_dir / CONFIG_FILE).read_text(encoding="utf-8")


# -- risk 20 / D19: an authored config promoted to DEV can go to production --


def test_promote_create_writes_no_dev_id_into_an_authored_main_dir(tmp_path: Path) -> None:
    p = _authored_and_promoted(tmp_path)
    w, config_dir, dev_id = p.world, p.config_dir, p.dev_id
    assert "config_id" not in (_yaml(config_dir).get("_keboola") or {})
    assert len(_entries(w, dev_id, DEV)) == 1


def test_r6_authored_config_promoted_to_dev_can_be_pushed_to_prod(tmp_path: Path) -> None:
    p = _authored_and_promoted(tmp_path)
    w, config_dir, dev_id = p.world, p.config_dir, p.dev_id
    result = w.push()
    assert result["created"] == 1, result
    assert len(w.api.remote[PROD]) == 1
    # The next promote still updates the same dev config, never a second one.
    semantic_edit(config_dir / CONFIG_FILE)
    w.push(branch=DEV)
    assert list(w.api.remote[DEV]) == [(VARIANTS["extractor"].component_id, dev_id)]


# -- risk 21 / D13c: kept promote entries are pruned -------------------------


def test_prod_pull_drops_a_promote_entry_whose_dir_is_gone(tmp_path: Path) -> None:
    p = _authored_and_promoted(tmp_path)
    w, config_dir, dev_id = p.world, p.config_dir, p.dev_id
    shutil.rmtree(config_dir)
    w.pull()
    assert _entries(w, dev_id, DEV) == []


def test_prod_pull_drops_promote_entries_of_ignored_components(tmp_path: Path) -> None:
    p = _authored_and_promoted(tmp_path)
    w, config_dir, dev_id = p.world, p.config_dir, p.dev_id
    manifest = load_manifest(w.root)
    manifest.ignored_components = [VARIANTS["extractor"].component_id]
    save_manifest(w.root, manifest)
    w.pull()
    assert _entries(w, dev_id, DEV) == []
    assert config_dir.is_dir()  # never deleted because of another branch's entry


# -- risk 22 / D21: the link backfill finds an entry under any production spelling


def test_binding_refresh_finds_an_entry_recorded_under_branch_zero(tmp_path: Path) -> None:
    v = VARIANTS["extractor"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    config_dir = w.config_dir(cid)
    manifest = load_manifest(w.root)
    (entry,) = manifest.configurations
    entry.branch_id = 0
    entry.metadata["pull_config_hash"] = "stale"
    _refresh_binding_hashes(
        w.svc,
        None,
        created=CreatedConfig(component_id=v.component_id, config_id=cid, config_dir=config_dir),
        manifest=manifest,
        branch_id=PROD,
        response=w.api.get(PROD, v.component_id, cid),
        warnings=[],
    )
    assert entry.metadata["pull_config_hash"] != "stale"
