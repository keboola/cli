"""A manifest entry belongs to exactly one branch (promote path, KFR-07 / #792 D).

``sync push --branch DEV`` with no ``dev/`` tree reads ``main/``, whose entries
belong to production. A dev branch created from production holds the same
config ids, so every write site must pick the entry of the branch it writes to,
and pull must never act on another branch's entry. Each test drives one of the
changed write sites through the real ``SyncService`` over the in-memory API of
the bookkeeping differential test.
"""

from __future__ import annotations

import copy
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.services import _sync_push_create
from keboola_agent_cli.services._sync_bindings import _refresh_binding_hashes
from keboola_agent_cli.services._sync_models import CreatedConfig
from keboola_agent_cli.services._sync_promote_remap import (
    PromoteRemap,
    SentPayload,
    finish_promote_rebind,
    recover_pending,
)
from keboola_agent_cli.services._sync_push_create import CreateTarget, ManifestCheckpointError
from keboola_agent_cli.services._sync_push_ops import push_update
from keboola_agent_cli.services._sync_writeback import writeback_create_config_in_manifest
from keboola_agent_cli.services.component_service import materialize_pushed_config
from keboola_agent_cli.sync.branch_scope import find_untracked_rows, same_branch
from keboola_agent_cli.sync.code_extraction import merge_code_files
from keboola_agent_cli.sync.config_format import api_config_to_local, dump_config_yaml
from keboola_agent_cli.sync.diff_engine import config_hash
from keboola_agent_cli.sync.manifest import (
    Manifest,
    ManifestBranch,
    ManifestConfigRow,
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
    # Listed, never hidden: with no baseline the row is a conflict (D22).
    assert w.diff()["summary"]["conflict"] == 1


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


# -- risks 11 / 23: unreadable dev rows stay visible, are never created twice


def _rows_unreadable(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """The update response carries no rows and the detail read fails."""
    original_update = w.api._update

    def update_without_rows(*args: Any, **kwargs: Any) -> dict:
        response = original_update(*args, **kwargs)
        response.pop("rows", None)
        return response

    def detail_fails(*_: Any, **__: Any) -> dict:
        raise KeboolaApiError("detail unavailable", 500, ErrorCode.API_ERROR)

    monkeypatch.setattr(w.api, "_update", update_without_rows)
    monkeypatch.setattr(w.api, "_detail", detail_fails)


def test_unreadable_dev_rows_create_a_missing_row_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v = VARIANTS["rows"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    config_dir = w.config_dir(cid)
    w.api.branch_from_prod()
    w.api.get(DEV, v.component_id, cid)["rows"].pop(0)
    _rows_unreadable(w, monkeypatch)
    semantic_edit(config_dir / CONFIG_FILE)
    w.push(branch=DEV)  # the missing row is "added" before the push: created here

    assert len(w.api.get(DEV, v.component_id, cid)["rows"]) == 2
    assert w.diff(branch=DEV)["summary"]["added"] == 0
    assert w.push(branch=DEV)["status"] == "no_changes"
    assert len(w.api.get(DEV, v.component_id, cid)["rows"]) == 2


def test_unreadable_dev_rows_list_a_row_whose_create_failed_and_recover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v = VARIANTS["rows"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    config_dir = w.config_dir(cid)
    w.api.branch_from_prod()
    gone = w.api.get(DEV, v.component_id, cid)["rows"].pop(0)["id"]
    _rows_unreadable(w, monkeypatch)

    def create_row_fails(*_: Any, **__: Any) -> dict:
        raise KeboolaApiError("row create refused", 500, ErrorCode.API_ERROR)

    monkeypatch.setattr(w.api, "_create_row", create_row_fails)
    semantic_edit(config_dir / CONFIG_FILE)
    assert w.svc.push(alias="prod", project_root=w.root, branch_override=DEV)["errors"]

    changes = w.diff(branch=DEV)["changes"]
    assert [c["change_type"] for c in changes if c["config_id"] == gone] == ["added"]
    monkeypatch.undo()  # rows readable, creates succeed
    w.push(branch=DEV)
    assert len(w.api.get(DEV, v.component_id, cid)["rows"]) == 2
    assert w.push(branch=DEV)["status"] == "no_changes"


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


def test_prod_pull_keeps_promote_entries_of_ignored_components_untouched(
    tmp_path: Path,
) -> None:
    p = _authored_and_promoted(tmp_path)
    w, config_dir, dev_id = p.world, p.config_dir, p.dev_id
    (before,) = _entries(w, dev_id, DEV)
    manifest = load_manifest(w.root)
    manifest.ignored_components = [VARIANTS["extractor"].component_id]
    save_manifest(w.root, manifest)
    w.pull()
    # Risk 75 revises D13c: kept as it was, back in scope once un-ignored.
    assert _entries(w, dev_id, DEV) == [before]
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


# ===========================================================================
# Round 3 (design D22-D28): regression tests per risk
# ===========================================================================


def _flow_body(component_id: str, config_id: str) -> dict:
    return {
        "phases": [{"id": "1", "name": "P", "dependsOn": []}],
        "tasks": [
            {
                "id": "t1",
                "name": "T",
                "phase": "1",
                "enabled": True,
                "task": {
                    "type": "job",
                    "componentId": component_id,
                    "configId": config_id,
                    "mode": "run",
                },
            }
        ],
    }


@dataclass(frozen=True)
class FlowWorld:
    """Production with an extractor (``cfg-ext``) and a flow (``cfg-flow``) that runs it."""

    world: World
    extractor_id: str
    flow_id: str


def _seed_flow_world(tmp_path: Path, *, flow_first: bool = False) -> FlowWorld:
    v = VARIANTS["extractor"]
    w = World(tmp_path)
    w.api.types[v.component_id] = v.component_type
    w.api.types["keboola.flow"] = "other"
    extractor = {
        "id": "cfg-ext",
        "name": "Orders",
        "description": "",
        "configuration": v.body(),
        "rows": [],
        "isDisabled": False,
    }
    flow = {
        "id": "cfg-flow",
        "name": "Nightly",
        "description": "",
        "configuration": _flow_body(v.component_id, "cfg-ext"),
        "rows": [],
        "isDisabled": False,
    }
    items = [(("keboola.flow", "cfg-flow"), flow), ((v.component_id, "cfg-ext"), extractor)]
    for key, cfg in items if flow_first else reversed(items):
        w.api.remote[PROD][key] = cfg
    w.init()
    w.pull()
    return FlowWorld(world=w, extractor_id="cfg-ext", flow_id="cfg-flow")


def _dev_task_target(w: World) -> str:
    (flow,) = [cfg for (comp, _), cfg in w.api.remote[DEV].items() if comp == "keboola.flow"]
    return flow["configuration"]["tasks"][0]["task"]["configId"]


def _dev_id_of(w: World, component_id: str) -> str:
    (dev_id,) = [cid for comp, cid in w.api.remote[DEV] if comp == component_id]
    return dev_id


# -- risk 24 / D22: an adopted stale row is never pushed over a remote edit ---


def test_adopted_stale_row_is_a_conflict_not_pushed_over_a_remote_edit(tmp_path: Path) -> None:
    v = VARIANTS["rows"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    w.api.branch_from_prod()
    w.pull(branch=DEV)  # re-targets: main/ is rediscovered next
    row = w.api.get(PROD, v.component_id, cid)["rows"][0]
    row["configuration"]["parameters"]["table"] = "edited in the UI"
    w.pull()  # keeps the stale main/ row: no baseline to tell stale from edited

    changes = [c for c in w.diff()["changes"] if c["config_id"] == row["id"]]
    assert [c["change_type"] for c in changes] == ["conflict"]
    w.push()
    assert row["configuration"]["parameters"]["table"] == "edited in the UI"
    w.pull(theirs=True)  # the one way to resolve it
    assert w.diff()["changes"] == []


# -- risk 26 / D23: a later promote update never writes the DEV id into main/ --


def test_promote_update_keeps_the_production_id_in_main(tmp_path: Path) -> None:
    v = VARIANTS["extractor"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    config_dir = w.config_dir(cid)
    w.push(branch=DEV)  # created under a new id
    semantic_edit(config_dir / CONFIG_FILE)
    assert w.push(branch=DEV)["updated"] == 1
    assert _file_id(config_dir) == cid


def test_promote_update_of_an_authored_config_keeps_it_pushable_to_production(
    tmp_path: Path,
) -> None:
    p = _authored_and_promoted(tmp_path)
    w, config_dir = p.world, p.config_dir
    semantic_edit(config_dir / CONFIG_FILE)
    assert w.push(branch=DEV)["updated"] == 1
    assert "config_id" not in (_yaml(config_dir).get("_keboola") or {})
    assert w.push()["created"] == 1
    ((_, prod_id),) = w.api.remote[PROD]
    assert _file_id(config_dir) == prod_id


# -- risk 27 / D24: references are remapped on EVERY promote push -----------


def test_flow_edited_after_promote_still_runs_the_dev_extractor(tmp_path: Path) -> None:
    f = _seed_flow_world(tmp_path)
    w = f.world
    flow_file = w.config_dir(f.flow_id) / CONFIG_FILE
    w.push(branch=DEV)
    semantic_edit(flow_file)
    w.push(branch=DEV)

    assert _dev_task_target(w) == _dev_id_of(w, VARIANTS["extractor"].component_id)
    assert "cfg-ext" in flow_file.read_text(encoding="utf-8")  # main/ keeps production ids


def test_flow_on_dev_runs_an_extractor_promoted_in_the_same_push(tmp_path: Path) -> None:
    f = _seed_flow_world(tmp_path)
    w = f.world
    w.api.branch_from_prod()
    del w.api.remote[DEV][(VARIANTS["extractor"].component_id, f.extractor_id)]
    semantic_edit(w.config_dir(f.flow_id) / CONFIG_FILE)
    w.push(branch=DEV)

    new_ext = _dev_id_of(w, VARIANTS["extractor"].component_id)
    assert new_ext != f.extractor_id
    assert _dev_task_target(w) == new_ext


def test_promoted_schedule_targets_the_dev_config(tmp_path: Path) -> None:
    v = VARIANTS["extractor"]
    w = World(tmp_path)
    ext_id = w.api.seed(v)
    w.api.types["keboola.scheduler"] = "other"
    w.api.remote[PROD][("keboola.scheduler", "cfg-sched")] = {
        "id": "cfg-sched",
        "name": "Daily",
        "description": "",
        "configuration": {
            "schedule": {"cronTab": "0 6 * * *", "timezone": "UTC", "state": "disabled"},
            "target": {"componentId": v.component_id, "configurationId": ext_id, "mode": "run"},
        },
        "rows": [],
        "isDisabled": False,
    }
    w.init()
    w.pull()
    w.push(branch=DEV)

    (schedule,) = [c for (comp, _), c in w.api.remote[DEV].items() if comp == "keboola.scheduler"]
    assert schedule["configuration"]["target"]["configurationId"] == _dev_id_of(w, v.component_id)


# -- risk 28 / D24: all reference kinds remapped in ONE payload -------------


def test_transformation_promoted_with_variables_and_shared_code_gets_both_dev_ids(
    tmp_path: Path,
) -> None:
    w = World(tmp_path)
    sql = "keboola.snowflake-transformation"
    w.api.types.update(
        {"keboola.variables": "other", "keboola.shared-code": "other", sql: "transformation"}
    )
    w.api.remote[PROD][("keboola.variables", "cfg-vars")] = {
        "id": "cfg-vars",
        "name": "Vars",
        "description": "",
        "configuration": {"variables": [{"name": "x", "type": "string"}]},
        "rows": [
            {
                "id": "row-vals",
                "name": "Default",
                "description": "",
                "configuration": {"values": [{"name": "x", "value": TOKEN}]},
                "isDisabled": False,
            }
        ],
        "isDisabled": False,
    }
    w.api.remote[PROD][("keboola.shared-code", "cfg-sc")] = {
        "id": "cfg-sc",
        "name": "Snippets",
        "description": "",
        "configuration": {"componentId": sql},
        "rows": [
            {
                "id": "row-sc",
                "name": "Snippet",
                "description": "",
                "configuration": {"code_content": ["SELECT 1;"]},
                "isDisabled": False,
            }
        ],
        "isDisabled": False,
    }
    w.api.remote[PROD][(sql, "cfg-trans")] = {
        "id": "cfg-trans",
        "name": "Orders",
        "description": "",
        "configuration": {
            "parameters": {
                "blocks": [
                    {"name": "B", "codes": [{"name": "C", "script": ["{{row-sc}}", "SELECT 2;"]}]}
                ]
            },
            "variables_id": "cfg-vars",
            "variables_values_id": "row-vals",
            "shared_code_id": "cfg-sc",
            "shared_code_row_ids": ["row-sc"],
        },
        "rows": [],
        "isDisabled": False,
    }
    w.init()
    w.pull()
    w.push(branch=DEV)

    dev = w.api.remote[DEV]
    (dev_vars,) = [c for (comp, _), c in dev.items() if comp == "keboola.variables"]
    (dev_sc,) = [c for (comp, _), c in dev.items() if comp == "keboola.shared-code"]
    (dev_trans,) = [c for (comp, _), c in dev.items() if comp == sql]
    conf = dev_trans["configuration"]
    sc_row = dev_sc["rows"][0]["id"]
    assert conf["variables_id"] == dev_vars["id"] != "cfg-vars"
    assert conf["variables_values_id"] == dev_vars["rows"][0]["id"]
    assert conf["shared_code_id"] == dev_sc["id"] != "cfg-sc"
    assert conf["shared_code_row_ids"] == [sc_row]
    assert "{{" + sc_row + "}}" in str(conf["parameters"])


# -- risk 29 / D25: a checkpoint that cannot be written stops the push ------


def test_failed_checkpoint_stops_the_push_and_the_retry_creates_nothing_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v = VARIANTS["extractor"]
    w = World(tmp_path)
    w.api.types[v.component_id] = v.component_type
    w.init()
    w.pull()
    author_config(w.root / "main", v, "First")
    author_config(w.root / "main", v, "Second")

    def save_fails(*_: Any, **__: Any) -> None:
        raise PermissionError("manifest is read-only")

    monkeypatch.setattr("keboola_agent_cli.services._sync_push_create.save_manifest", save_fails)
    with pytest.raises(ManifestCheckpointError) as raised:
        w.svc.push(alias="prod", project_root=w.root)
    ((_, created),) = w.api.remote[PROD]
    assert created in raised.value.message  # names the config it created
    monkeypatch.undo()
    w.push()  # the retry
    assert len(w.api.remote[PROD]) == 2


def test_manifest_save_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    v = VARIANTS["extractor"]
    seeded = _seeded(tmp_path, v)
    w = seeded.world
    manifest_path = w.root / ".keboola" / "manifest.json"
    before = manifest_path.read_bytes()
    manifest = load_manifest(w.root)
    manifest.configurations = []

    def replace_dies(*_: Any, **__: Any) -> None:
        raise OSError("killed mid-write")

    monkeypatch.setattr("keboola_agent_cli.sync.manifest.os.replace", replace_dies)
    with pytest.raises(OSError):
        save_manifest(w.root, manifest)
    monkeypatch.undo()
    assert manifest_path.read_bytes() == before
    assert len(load_manifest(w.root).configurations) == 1
    assert not [p for p in manifest_path.parent.iterdir() if p.name.startswith(".manifest")]


# -- risk 30 / D24: a crash before the rebind is finished by the next push ---


def test_crash_before_the_rebind_is_finished_by_the_next_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _seed_flow_world(tmp_path, flow_first=True).world

    def crash(*_: Any, **__: Any) -> bool:
        raise _Crash

    monkeypatch.setattr("keboola_agent_cli.services.sync_service.finish_promote_rebind", crash)
    with pytest.raises(_Crash):
        w.svc.push(alias="prod", project_root=w.root, branch_override=DEV)
    monkeypatch.undo()
    w.push(branch=DEV)  # no local edit

    assert len(w.api.remote[DEV]) == 2  # nothing created twice
    assert _dev_task_target(w) == _dev_id_of(w, VARIANTS["extractor"].component_id)


# -- risk 31 / D26: every candidate path is checked; ID-less dirs reserved ---


def test_suffixed_path_owned_by_another_config_is_not_overwritten(tmp_path: Path) -> None:
    v = VARIANTS["extractor"]
    w = World(tmp_path)
    first = w.api.seed(v, name="Foo")  # path foo
    w.api.seed(v, name="Foo cfg-1002")  # path foo-cfg-1002
    w.init()
    w.pull()
    taken = {c.path for c in load_manifest(w.root).configurations}
    before = {p: (w.root / "main" / p / CONFIG_FILE).read_bytes() for p in taken}
    new = w.api._create(v.component_id, "Foo", v.body(), "third")
    assert new["id"] == "cfg-1002"
    w.pull()

    assert {p: (w.root / "main" / p / CONFIG_FILE).read_bytes() for p in taken} == before
    paths = {c.id: c.path for c in load_manifest(w.root).configurations}
    assert paths[new["id"]] not in taken
    assert first in paths


def test_row_suffix_collision_gets_distinct_dirs(tmp_path: Path) -> None:
    v = VARIANTS["rows"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    rows = w.api.get(PROD, v.component_id, cid)["rows"]
    for row_id in ("row-new-a", "row-new-b"):  # same first 8 characters
        rows.append(
            {
                "id": row_id,
                "name": "Row 1",
                "description": "",
                "configuration": {"parameters": {"table": row_id}},
                "isDisabled": False,
            }
        )
    w.pull()

    (entry,) = _entries(w, cid, PROD)
    paths = [r.path for r in entry.rows]
    assert len(set(paths)) == len(paths) == 4
    config_dir = w.config_dir(cid)
    for row_id in ("row-new-a", "row-new-b"):
        (path,) = [r.path for r in entry.rows if r.id == row_id]
        assert row_id in (config_dir / path / CONFIG_FILE).read_text(encoding="utf-8")


def test_pull_never_writes_into_an_authored_id_less_dir(tmp_path: Path) -> None:
    v = VARIANTS["extractor"]
    w = World(tmp_path)
    w.api.types[v.component_id] = v.component_type
    w.init()
    w.pull()
    authored = author_config(w.root / "main", v, "Foo")
    before = (authored / CONFIG_FILE).read_bytes()
    new = w.api._create(v.component_id, "Foo", v.body(), "same name, from the UI")
    w.pull()

    assert (authored / CONFIG_FILE).read_bytes() == before
    paths = {c.id: c.path for c in load_manifest(w.root).configurations}
    assert paths[new["id"]] != authored.relative_to(w.root / "main").as_posix()


# -- risk 32 / D22: a kept adopted row survives a remote delete --------------


def test_kept_adopted_row_survives_a_remote_delete(tmp_path: Path) -> None:
    r = _retargeted(tmp_path, "rows")
    w, cid, config_dir = r.world, r.config_id, r.config_dir
    row_file = config_dir / "rows" / "row-1" / CONFIG_FILE
    semantic_edit(row_file)
    w.pull()  # kept, base unknown
    rows = w.api.get(PROD, VARIANTS["rows"].component_id, cid)["rows"]
    rows[:] = [row for row in rows if row["name"] != "Row 1"]
    result = w.pull()

    assert "edit_marker" in row_file.read_text(encoding="utf-8")
    assert not [d for d in result["details"] if d["action"] == "removed"]
    assert any(
        d["action"] == "skipped" and d["path"].endswith("rows/row-1") for d in result["details"]
    )


# -- risk 33 / D27: pulling a third branch keeps the promote association -----


def test_pull_of_another_dev_branch_keeps_the_promote_association(tmp_path: Path) -> None:
    v = VARIANTS["extractor"]
    seeded = _seeded(tmp_path, v)
    w = seeded.world
    w.push(branch=DEV)  # DEV lacks it: created there
    other = 88888
    w.api.remote[other] = copy.deepcopy(w.api.remote[PROD])
    w.pull(branch=other)
    w.pull()
    w.push(branch=DEV)
    assert len(w.api.remote[DEV]) == 1, sorted(w.api.remote[DEV])


# -- risk 34 / D28: an unregistered branch never supplies the shadow ---------


def test_unregistered_branch_entry_never_becomes_the_shadow_source(tmp_path: Path) -> None:
    v = VARIANTS["rows"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    config_dir = w.config_dir(cid)
    manifest = load_manifest(w.root)
    (prod,) = manifest.configurations
    stray = ManifestConfiguration(
        branchId=999,  # not registered: its tree falls back to main/
        componentId=v.component_id,
        id=cid,
        path="extractor/elsewhere",
        metadata={"pull_hash": "UNKNOWN", "pull_config_hash": "UNKNOWN"},
    )
    manifest.configurations.insert(0, stray)
    save_manifest(w.root, manifest)
    w.api.branch_from_prod()
    semantic_edit(config_dir / CONFIG_FILE)
    w.push(branch=DEV)

    (shadow,) = _entries(w, cid, DEV)
    assert shadow.path == prod.path
    assert {r.id for r in shadow.rows} == {r.id for r in prod.rows}
    assert shadow.metadata["pull_hash"] != "UNKNOWN"


# -- risk 36 / D21: push branch None finds an entry under the default id ------


def test_binding_refresh_with_branch_none_finds_an_entry_under_the_default_id(
    tmp_path: Path,
) -> None:
    v = VARIANTS["extractor"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    config_dir = w.config_dir(cid)
    manifest = load_manifest(w.root)
    (entry,) = manifest.configurations
    entry.branch_id = PROD
    entry.metadata["pull_config_hash"] = "stale"
    _refresh_binding_hashes(
        w.svc,
        None,
        created=CreatedConfig(component_id=v.component_id, config_id=cid, config_dir=config_dir),
        manifest=manifest,
        branch_id=None,
        response=w.api.get(PROD, v.component_id, cid),
        warnings=[],
    )
    assert entry.metadata["pull_config_hash"] != "stale"


# ===========================================================================
# Round 4 (risks 38-49)
# ===========================================================================


def _adopted_edited_row(tmp_path: Path) -> Retargeted:
    """A rediscovered config whose edited ``rows/row-1`` pull kept with an unknown base."""
    r = _retargeted(tmp_path, "rows")
    semantic_edit(r.config_dir / "rows" / "row-1" / CONFIG_FILE)
    r.world.pull()
    return r


def _crash_before_rebind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """Flow + extractor promoted (flow first); the push dies before the final rebind."""
    w = _seed_flow_world(tmp_path, flow_first=True).world

    def crash(*_: Any, **__: Any) -> bool:
        raise _Crash

    monkeypatch.setattr("keboola_agent_cli.services.sync_service.finish_promote_rebind", crash)
    with pytest.raises(_Crash):
        w.svc.push(alias="prod", project_root=w.root, branch_override=DEV)
    monkeypatch.undo()
    return w


def _dev_flow(w: World) -> dict:
    (flow,) = [cfg for (comp, _), cfg in w.api.remote[DEV].items() if comp == "keboola.flow"]
    return flow


# -- risk 39: a secondary failure never masks the fatal checkpoint error ------


def test_secondary_writeback_failure_never_masks_the_checkpoint_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v = VARIANTS["extractor"]
    w = World(tmp_path)
    w.api.types[v.component_id] = v.component_type
    w.init()
    w.pull()
    author_config(w.root / "main", v, "First")
    author_config(w.root / "main", v, "Second")

    def disk_full(*_: Any, **__: Any) -> None:
        raise PermissionError("no space left on device")

    monkeypatch.setattr("keboola_agent_cli.services._sync_push_create.save_manifest", disk_full)
    monkeypatch.setattr(
        "keboola_agent_cli.services._sync_push_create.writeback_after_push", disk_full
    )
    with pytest.raises(ManifestCheckpointError):
        w.svc.push(alias="prod", project_root=w.root)
    assert len(w.api.remote[PROD]) == 1  # the push stopped after the first create


# -- risk 40: a local delete of an unknown-base row is a conflict, never a DELETE


def test_local_delete_of_an_unknown_base_row_is_a_conflict_not_a_delete(tmp_path: Path) -> None:
    r = _adopted_edited_row(tmp_path)
    w, cid = r.world, r.config_id
    shutil.rmtree(r.config_dir / "rows" / "row-1")
    rows = w.api.get(PROD, VARIANTS["rows"].component_id, cid)["rows"]
    (row_id,) = [row["id"] for row in rows if row["name"] == "Row 1"]

    assert [c["change_type"] for c in w.diff()["changes"] if c["config_id"] == row_id] == [
        "conflict"
    ]
    w.push(force=True)
    assert any(row["id"] == row_id for row in rows)


# -- risk 41: a remote parent delete keeps a dir holding an unknown-base row --


def test_remote_config_delete_keeps_a_dir_with_an_unknown_base_row(tmp_path: Path) -> None:
    r = _adopted_edited_row(tmp_path)
    w, cid = r.world, r.config_id
    del w.api.remote[PROD][(VARIANTS["rows"].component_id, cid)]
    result = w.pull()

    assert "edit_marker" in (r.config_dir / "rows" / "row-1" / CONFIG_FILE).read_text(
        encoding="utf-8"
    )
    assert [
        d["action"]
        for d in result["details"]
        if d["path"].startswith(r.config_dir.relative_to(w.root / "main").as_posix())
    ] == ["skipped"]


# -- risk 42: a flow's row selection maps to the dev rows -------------------


def test_flow_row_selection_maps_to_the_rows_created_on_dev(tmp_path: Path) -> None:
    v = VARIANTS["rows"]
    w = World(tmp_path)
    ext_id = w.api.seed(v)  # listed before the flow: created first on DEV
    row_id = w.api.get(PROD, v.component_id, ext_id)["rows"][0]["id"]
    w.api.types["keboola.flow"] = "other"
    body = _flow_body(v.component_id, ext_id)
    body["tasks"][0]["task"]["configRowIds"] = [row_id]
    w.api.remote[PROD][("keboola.flow", "cfg-flow")] = {
        "id": "cfg-flow",
        "name": "Nightly",
        "description": "",
        "configuration": body,
        "rows": [],
        "isDisabled": False,
    }
    w.init()
    w.pull()
    w.push(branch=DEV)

    dev_ext = _dev_id_of(w, v.component_id)
    (dev_row,) = [
        row["id"]
        for row in w.api.get(DEV, v.component_id, dev_ext)["rows"]
        if row["name"] == "Row 1"
    ]
    task = _dev_flow(w)["configuration"]["tasks"][0]["task"]
    assert (task["configId"], task["configRowIds"]) == (dev_ext, [dev_row])


# -- risk 43: a failed final rebind of an UPDATE is retried ------------------


def test_failed_final_rebind_of_an_updated_flow_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = _seed_flow_world(tmp_path, flow_first=True)
    w = f.world
    w.api.branch_from_prod()
    del w.api.remote[DEV][(VARIANTS["extractor"].component_id, f.extractor_id)]
    semantic_edit(w.config_dir(f.flow_id) / CONFIG_FILE)
    original = w.api._update

    def remap_put_fails(*args: Any, **kwargs: Any) -> dict:
        if str(kwargs.get("change_description", "")).startswith("Remap"):
            raise KeboolaApiError("refused", 500, ErrorCode.API_ERROR)
        return original(*args, **kwargs)

    monkeypatch.setattr(w.api, "_update", remap_put_fails)
    assert w.svc.push(alias="prod", project_root=w.root, branch_override=DEV)["errors"]
    monkeypatch.undo()
    w.push(branch=DEV)  # no local edit since

    assert _dev_task_target(w) == _dev_id_of(w, VARIANTS["extractor"].component_id)


# -- risk 44: a pending rebind goes through the plan ------------------------


def test_pending_rebind_never_overwrites_a_remote_change_on_dev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _crash_before_rebind(tmp_path, monkeypatch)
    _dev_flow(w)["name"] = "Edited on dev"
    w.push(branch=DEV)

    # Recovery rewrites only the references, from the target's own config (D29).
    assert _dev_flow(w)["name"] == "Edited on dev"
    assert _dev_task_target(w) == _dev_id_of(w, VARIANTS["extractor"].component_id)


def test_pending_rebind_of_a_config_deleted_on_dev_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _crash_before_rebind(tmp_path, monkeypatch)
    (flow_key,) = [key for key in w.api.remote[DEV] if key[0] == "keboola.flow"]
    del w.api.remote[DEV][flow_key]
    w.push(branch=DEV)  # no error: no PUT to the gone id
    w.push(branch=DEV)
    assert not [c for c in load_manifest(w.root).configurations if c.metadata.get("rebind_pending")]


# -- risk 45: a second data-app promote keeps the dev app id ---------------


def test_second_data_app_promote_keeps_the_dev_app_id(tmp_path: Path) -> None:
    app = "keboola.data-apps"
    w = World(tmp_path)
    w.api.types[app] = "other"
    w.api.remote[PROD][(app, "cfg-app")] = {
        "id": "cfg-app",
        "name": "Dash",
        "description": "",
        "configuration": {"parameters": {"id": "prod-app", "dataApp": {"slug": "dash"}}},
        "rows": [],
        "isDisabled": False,
    }
    ds = MagicMock()
    ds.list_apps.return_value = []

    def create_app(
        *, type_: str, name: str, description: str, config: dict, branch_id: Any
    ) -> dict:
        created = w.api._create(app, name, config, branch_id=DEV)
        return {"id": "dev-app", "configId": created["id"]}

    ds.create_app.side_effect = create_app
    w.svc._ds_client_factory = lambda url, token: ds
    w.init()
    w.pull()
    config_file = w.config_dir("cfg-app") / CONFIG_FILE
    w.push(branch=DEV)
    semantic_edit(config_file)
    w.push(branch=DEV)

    ((_, dev_cfg),) = w.api.remote[DEV].items()
    assert dev_cfg["configuration"]["parameters"]["id"] == "dev-app"
    assert "prod-app" in config_file.read_text(encoding="utf-8")


# -- risks 46 / 49: manifest mode and directory sync ------------------------


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_new_manifest_respects_the_umask_and_an_existing_mode_is_kept(tmp_path: Path) -> None:
    manifest_path = tmp_path / ".keboola" / "manifest.json"
    old = os.umask(0o077)
    try:
        save_manifest(tmp_path, _manifest((PROD, "main")))
    finally:
        os.umask(old)
    assert manifest_path.stat().st_mode & 0o777 == 0o600
    manifest_path.chmod(0o640)
    save_manifest(tmp_path, _manifest((PROD, "main")))
    assert manifest_path.stat().st_mode & 0o777 == 0o640


@pytest.mark.skipif(os.name != "posix", reason="directory fsync is POSIX-only")
def test_manifest_save_syncs_its_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[int] = []
    opened: dict[int, str] = {}
    real_open, real_fsync = os.open, os.fsync

    def recording_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        opened[fd] = str(path)
        return fd

    def recording_fsync(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "fsync", recording_fsync)
    save_manifest(tmp_path, _manifest((PROD, "main")))
    monkeypatch.undo()
    assert str(tmp_path / ".keboola") in [opened.get(fd) for fd in synced]


# -- risk 47: a pending rebind on DEV does not make a production push a write --


def test_pending_rebind_on_dev_leaves_a_production_push_with_no_changes(tmp_path: Path) -> None:
    v = VARIANTS["extractor"]
    w = _seeded(tmp_path, v).world
    w.push(branch=DEV)
    manifest = load_manifest(w.root)
    for entry in manifest.configurations:
        if same_branch(manifest, entry.branch_id, DEV):
            entry.metadata["rebind_pending"] = True
    save_manifest(w.root, manifest)
    assert w.push()["status"] == "no_changes"


# ===========================================================================
# Round 5 (risks 50-54)
# ===========================================================================


# -- risk 50: unknown-base rows protect the dir even without the parent file --


def test_unknown_base_row_keeps_its_dir_when_the_parent_file_is_gone(tmp_path: Path) -> None:
    r = _adopted_edited_row(tmp_path)
    w, cid = r.world, r.config_id
    (r.config_dir / CONFIG_FILE).unlink()
    del w.api.remote[PROD][(VARIANTS["rows"].component_id, cid)]
    w.pull()

    row_file = r.config_dir / "rows" / "row-1" / CONFIG_FILE
    assert "edit_marker" in row_file.read_text(encoding="utf-8")


# -- risk 51: rebind_pending is on disk before the PUT ---------------------


def _flow_running_two_extractors_crashes_after_put(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, local_disabled: bool | None = None
) -> World:
    """The flow's promote update PUT lands, then the process dies.

    Production flow F (-> E1) and E1 are pulled; the dev branch lacks E1, so a
    first promote creates it. Production then gains E2; F is edited in main/
    to run both. The next promote updates F first -- E1 by its dev id, E2
    still by its SOURCE id (not created yet) -- and dies right after F's PUT,
    before E2's create. *local_disabled* writes an explicit ``is_disabled``.
    """
    v = VARIANTS["extractor"]
    comp = v.component_id
    w = World(tmp_path)
    w.api.types[comp] = v.component_type
    w.api.types["keboola.flow"] = "other"
    w.api.remote[PROD][("keboola.flow", "cfg-flow")] = {  # listed first: pushed first
        "id": "cfg-flow",
        "name": "Nightly",
        "description": "",
        "configuration": _flow_body(comp, "cfg-e1"),
        "rows": [],
        "isDisabled": False,
    }
    w.api.remote[PROD][(comp, "cfg-e1")] = {
        "id": "cfg-e1",
        "name": "Orders",
        "description": "",
        "configuration": v.body(),
        "rows": [],
        "isDisabled": False,
    }
    w.init()
    w.pull()
    w.api.branch_from_prod()
    del w.api.remote[DEV][(comp, "cfg-e1")]
    w.push(branch=DEV)  # E1 created on the dev branch
    w.api.remote[PROD][(comp, "cfg-e2")] = {
        "id": "cfg-e2",
        "name": "Customers",
        "description": "",
        "configuration": v.body(),
        "rows": [],
        "isDisabled": False,
    }
    w.pull()
    flow_file = w.config_dir("cfg-flow") / CONFIG_FILE
    data = _yaml(flow_file.parent)
    second = copy.deepcopy(data["_configuration_extra"]["tasks"][0])
    second["id"] = "t2"
    second["task"]["configId"] = "cfg-e2"
    data["_configuration_extra"]["tasks"].append(second)
    if local_disabled is not None:
        data["is_disabled"] = local_disabled  # the update then sends this state
    flow_file.write_text(dump_config_yaml(data), encoding="utf-8", newline="")
    original = w.api._update

    def put_then_crash(*args: Any, **kwargs: Any) -> dict:
        response = original(*args, **kwargs)
        if kwargs.get("component_id") == "keboola.flow" and str(
            kwargs.get("change_description", "")
        ).startswith("Updated"):
            raise _Crash
        return response

    monkeypatch.setattr(w.api, "_update", put_then_crash)
    with pytest.raises(_Crash):
        w.svc.push(alias="prod", project_root=w.root, branch_override=DEV)
    monkeypatch.undo()
    return w


def _dev_task_targets(w: World) -> list[str]:
    return [t["task"]["configId"] for t in _dev_flow(w)["configuration"]["tasks"]]


def test_crash_after_an_update_put_is_finished_by_the_next_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _flow_running_two_extractors_crashes_after_put(tmp_path, monkeypatch)
    pending = [c for c in load_manifest(w.root).configurations if c.metadata.get("rebind_pending")]
    assert [c.component_id for c in pending] == ["keboola.flow"]  # saved BEFORE the PUT
    result = w.push(branch=DEV)

    comp = VARIANTS["extractor"].component_id
    dev_extractors = sorted(cid for c, cid in w.api.remote[DEV] if c == comp)
    assert len(dev_extractors) == 2  # nothing created twice
    assert sorted(_dev_task_targets(w)) == dev_extractors
    assert not any(x["change_type"] == "promote_link_pending" for x in result.get("warnings", []))
    assert not [c for c in load_manifest(w.root).configurations if c.metadata.get("rebind_pending")]


def test_crash_after_an_update_put_keeps_a_real_remote_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _flow_running_two_extractors_crashes_after_put(tmp_path, monkeypatch)
    _dev_flow(w)["name"] = "Edited on dev"  # a real change after our PUT
    w.push(branch=DEV)

    assert _dev_flow(w)["name"] == "Edited on dev"
    comp = VARIANTS["extractor"].component_id
    assert sorted(_dev_task_targets(w)) == sorted(cid for c, cid in w.api.remote[DEV] if c == comp)


# -- risk 52: every reference is mapped once, from its source id -----------


def test_overlapping_row_ids_are_mapped_once_from_the_source_id() -> None:
    remap = PromoteRemap(
        configs={("keboola.ex-http", "P"): "D", ("keboola.shared-code", "S"): "S2"},
        rows={("D", "1"): "2", ("D", "2"): "3", ("S2", "a"): "b", ("S2", "b"): "c"},
    )
    flow = {
        "tasks": [
            {
                "id": "t",
                "task": {
                    "type": "job",
                    "componentId": "keboola.ex-http",
                    "configId": "P",
                    "configRowIds": ["1"],
                },
            }
        ]
    }
    sent = remap.apply("keboola.flow", flow)
    task = sent["tasks"][0]["task"]
    assert (task["configId"], task["configRowIds"]) == ("D", ["2"])
    assert flow["tasks"][0]["task"]["configRowIds"] == ["1"]  # the original is untouched
    assert remap.apply("keboola.flow", flow) == sent  # the final pass starts from it too

    transformation = {
        "shared_code_id": "S",
        "shared_code_row_ids": ["a"],
        "parameters": {"blocks": [{"codes": [{"script": ["{{a}}"]}]}]},
    }
    out = remap.apply("keboola.snowflake-transformation", transformation)
    assert out["shared_code_row_ids"] == ["b"]
    assert out["parameters"]["blocks"][0]["codes"][0]["script"] == ["{{b}}"]


# -- risk 53: pending recovery never PUTs an ignored component -------------


def test_pending_rebind_of_an_ignored_component_is_dropped_without_a_put(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _crash_before_rebind(tmp_path, monkeypatch)
    manifest = load_manifest(w.root)
    manifest.ignored_components = ["keboola.flow"]
    save_manifest(w.root, manifest)
    _dev_flow(w)["name"] = "Edited on dev"
    w.push(branch=DEV)

    assert _dev_flow(w)["name"] == "Edited on dev"
    assert not [c for c in load_manifest(w.root).configurations if c.metadata.get("rebind_pending")]


# -- risk 54: no process-wide umask change ----------------------------------


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_manifest_save_never_touches_the_process_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = os.umask(0o077)
    try:

        def umask_forbidden(*_: Any) -> int:
            raise AssertionError("save_manifest changed the process umask")

        monkeypatch.setattr(os, "umask", umask_forbidden)
        save_manifest(tmp_path, _manifest((PROD, "main")))
        monkeypatch.undo()
    finally:
        os.umask(old)
    mode = (tmp_path / ".keboola" / "manifest.json").stat().st_mode & 0o777
    assert mode == 0o600  # the OS applied the umask to the new file


# ===========================================================================
# Round 6 (risks 55-57)
# ===========================================================================


# -- risk 55: recovery never re-enables a config disabled on the target ------


def test_crash_after_an_update_put_keeps_a_remote_disable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _flow_running_two_extractors_crashes_after_put(tmp_path, monkeypatch, local_disabled=False)
    _dev_flow(w)["isDisabled"] = True  # someone disabled it on the dev branch
    w.push(branch=DEV)

    assert _dev_flow(w)["isDisabled"] is True
    comp = VARIANTS["extractor"].component_id
    assert sorted(_dev_task_targets(w)) == sorted(cid for c, cid in w.api.remote[DEV] if c == comp)


# -- risk 56: the final backfill keeps the target data-app id ---------------


def test_data_app_with_a_variables_link_keeps_its_dev_app_id_on_every_promote(
    tmp_path: Path,
) -> None:
    app, variables = "keboola.data-apps", "keboola.variables"
    w = World(tmp_path)
    w.api.types.update({app: "other", variables: "other"})
    w.api.remote[PROD][(variables, "cfg-vars")] = {
        "id": "cfg-vars",
        "name": "Vars",
        "description": "",
        "configuration": {"variables": [{"name": "x", "type": "string"}]},
        "rows": [],
        "isDisabled": False,
    }
    w.api.remote[PROD][(app, "cfg-app")] = {
        "id": "cfg-app",
        "name": "Dash",
        "description": "",
        "configuration": {
            "parameters": {"id": "prod-app", "dataApp": {"slug": "dash"}},
            "variables_id": "cfg-vars",
        },
        "rows": [],
        "isDisabled": False,
    }
    ds = MagicMock()
    ds.list_apps.return_value = []

    def create_app(
        *, type_: str, name: str, description: str, config: dict, branch_id: Any
    ) -> dict:
        created = w.api._create(app, name, config, branch_id=DEV)
        return {"id": "dev-app", "configId": created["id"]}

    ds.create_app.side_effect = create_app
    w.svc._ds_client_factory = lambda url, token: ds
    w.init()
    w.pull()

    def dev_app() -> dict:
        (cfg,) = [c for (comp, _), c in w.api.remote[DEV].items() if comp == app]
        return cfg["configuration"]

    w.push(branch=DEV)
    dev_vars = _dev_id_of(w, variables)
    assert (dev_app()["parameters"]["id"], dev_app()["variables_id"]) == ("dev-app", dev_vars)
    semantic_edit(w.config_dir("cfg-app") / CONFIG_FILE)
    w.push(branch=DEV)
    assert (dev_app()["parameters"]["id"], dev_app()["variables_id"]) == ("dev-app", dev_vars)


# -- risk 57: a failed dependency create keeps the rebind pending ------------


def test_failed_dependency_create_keeps_the_rebind_pending_until_it_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = _seed_flow_world(tmp_path, flow_first=True)
    w = f.world
    comp = VARIANTS["extractor"].component_id
    w.api.branch_from_prod()
    del w.api.remote[DEV][(comp, f.extractor_id)]
    semantic_edit(w.config_dir(f.flow_id) / CONFIG_FILE)
    original = w.api._create

    def extractor_create_fails(*args: Any, **kwargs: Any) -> dict:
        if kwargs.get("component_id") == comp:
            raise KeboolaApiError("create refused", 500, ErrorCode.API_ERROR)
        return original(*args, **kwargs)

    monkeypatch.setattr(w.api, "_create", extractor_create_fails)
    result = w.svc.push(alias="prod", project_root=w.root, branch_override=DEV)
    assert result["errors"]
    assert [
        c.id for c in load_manifest(w.root).configurations if c.metadata.get("rebind_pending")
    ] == [f.flow_id]
    monkeypatch.undo()
    w.push(branch=DEV)  # creates the extractor, then rebinds the flow

    assert _dev_task_target(w) == _dev_id_of(w, comp)
    assert not [c for c in load_manifest(w.root).configurations if c.metadata.get("rebind_pending")]


# ===========================================================================
# Round 7 (D29, D30; risks 58-60)
# ===========================================================================


def _dev_links_resolved(w: World) -> bool:
    comp = VARIANTS["extractor"].component_id
    return sorted(_dev_task_targets(w)) == sorted(cid for c, cid in w.api.remote[DEV] if c == comp)


# -- risk 58: recovery PUTs only the configuration, never the enabled state --


@pytest.mark.parametrize(
    ("local_disabled", "remote_disabled"),
    [(None, True), (True, False)],
    ids=["remote-disable-survives", "remote-enable-survives"],
)
def test_recovery_never_changes_the_enabled_state_on_the_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_disabled: bool | None,
    remote_disabled: bool,
) -> None:
    w = _flow_running_two_extractors_crashes_after_put(
        tmp_path, monkeypatch, local_disabled=local_disabled
    )
    _dev_flow(w)["isDisabled"] = remote_disabled  # toggled on the dev branch since
    if local_disabled is None:  # the local file now states the opposite explicitly
        flow_file = w.config_dir("cfg-flow") / CONFIG_FILE
        data = _yaml(flow_file.parent)
        data["is_disabled"] = not remote_disabled
        flow_file.write_text(dump_config_yaml(data), encoding="utf-8", newline="")
    w.push(branch=DEV)

    assert _dev_flow(w)["isDisabled"] is remote_disabled
    assert _dev_links_resolved(w)


# -- risk 59: a pending reference is cleared only when it resolves (D30) ----


def test_ignoring_a_missing_dependency_keeps_the_rebind_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = _seed_flow_world(tmp_path, flow_first=True)
    w = f.world
    comp = VARIANTS["extractor"].component_id
    w.api.branch_from_prod()
    del w.api.remote[DEV][(comp, f.extractor_id)]
    semantic_edit(w.config_dir(f.flow_id) / CONFIG_FILE)
    original = w.api._create

    def extractor_create_fails(*args: Any, **kwargs: Any) -> dict:
        if kwargs.get("component_id") == comp:
            raise KeboolaApiError("create refused", 500, ErrorCode.API_ERROR)
        return original(*args, **kwargs)

    monkeypatch.setattr(w.api, "_create", extractor_create_fails)
    assert w.svc.push(alias="prod", project_root=w.root, branch_override=DEV)["errors"]
    monkeypatch.undo()

    def pending() -> list[str]:
        return [
            c.id for c in load_manifest(w.root).configurations if c.metadata.get("rebind_pending")
        ]

    manifest = load_manifest(w.root)
    manifest.ignored_components = [comp]
    save_manifest(w.root, manifest)
    w.push(branch=DEV)  # the dependency is out of this push: it stays unresolved
    assert pending() == [f.flow_id]

    manifest = load_manifest(w.root)
    manifest.ignored_components = []
    save_manifest(w.root, manifest)
    w.push(branch=DEV)  # creates the extractor, then rebinds the flow
    assert _dev_task_target(w) == _dev_id_of(w, comp)
    assert pending() == []


# -- risk 60: recovery keeps every other change made on the target ----------


def test_recovery_keeps_a_nested_version_changed_on_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _flow_running_two_extractors_crashes_after_put(tmp_path, monkeypatch)
    _dev_flow(w)["configuration"]["parameters"] = {"version": "v2"}  # changed after our PUT
    w.push(branch=DEV)

    assert _dev_flow(w)["configuration"]["parameters"] == {"version": "v2"}
    assert _dev_links_resolved(w)


# ===========================================================================
# Round 8 (risks 61-62)
# ===========================================================================


# -- risk 61: a resolved reference leaves the pending list, never applied again


def test_repeated_recovery_never_remaps_a_resolved_row_again() -> None:
    http = "keboola.ex-http"
    manifest = _manifest((PROD, "main"), (DEV, "dev"))
    flow = ManifestConfiguration(
        branchId=DEV,
        componentId="keboola.flow",
        id="F",
        path="other/keboola.flow/f",
        metadata={"rebind_pending": [[http, "P"], [http, "P", "1"], [http, "P", "2"], [http, "E"]]},
    )
    manifest.configurations.append(flow)
    # P -> D with overlapping row ids, and E still missing on the branch.
    remap = PromoteRemap(configs={(http, "P"): "D"}, rows={("D", "1"): "2", ("D", "2"): "3"})
    remote = {
        "configuration": {
            "tasks": [
                {
                    "id": "t",
                    "task": {  # still in source form after an interrupted promote
                        "type": "job",
                        "componentId": http,
                        "configId": "P",
                        "configRowIds": ["1", "2"],
                    },
                },
                {  # E is still missing on the branch
                    "id": "t2",
                    "task": {"type": "job", "componentId": http, "configId": "E"},
                },
            ]
        }
    }
    client = MagicMock()
    client.get_config_detail.side_effect = lambda **_: copy.deepcopy(remote)

    def put(**kwargs: Any) -> dict:
        remote["configuration"] = copy.deepcopy(kwargs["configuration"])
        return copy.deepcopy(remote)

    client.update_config.side_effect = put
    for _ in range(2):  # E stays unresolved, so the flow stays pending
        recover_pending(client, manifest, remap, "keboola.flow", "F", DEV, [])

    task = remote["configuration"]["tasks"][0]["task"]
    assert (task["configId"], task["configRowIds"]) == ("D", ["2", "3"])
    assert flow.metadata["rebind_pending"] == [[http, "E"]]


# -- risk 62: an update merges the pending list, never replaces it ---------


def test_editing_a_pending_flow_never_forgets_an_ignored_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = _seed_flow_world(tmp_path, flow_first=True)
    w = f.world
    http, mysql = VARIANTS["extractor"].component_id, "keboola.ex-db-mysql"
    w.api.branch_from_prod()
    del w.api.remote[DEV][(http, f.extractor_id)]
    flow_file = w.config_dir(f.flow_id) / CONFIG_FILE
    semantic_edit(flow_file)
    original = w.api._create

    def http_create_fails(*args: Any, **kwargs: Any) -> dict:
        if kwargs.get("component_id") == http:
            raise KeboolaApiError("create refused", 500, ErrorCode.API_ERROR)
        return original(*args, **kwargs)

    monkeypatch.setattr(w.api, "_create", http_create_fails)
    assert w.svc.push(alias="prod", project_root=w.root, branch_override=DEV)["errors"]
    monkeypatch.undo()

    def set_ignored(components: list[str]) -> None:
        manifest = load_manifest(w.root)
        manifest.ignored_components = components
        save_manifest(w.root, manifest)

    def dev_pending() -> Any:
        manifest = load_manifest(w.root)
        (entry,) = [
            c
            for c in manifest.configurations
            if c.id == f.flow_id and same_branch(manifest, c.branch_id, DEV)
        ]
        return entry.metadata.get("rebind_pending")

    # Production gains X; E's component is ignored; F now also runs X.
    w.api.types[mysql] = "extractor"
    w.api.remote[PROD][(mysql, "cfg-x")] = {
        "id": "cfg-x",
        "name": "Customers",
        "description": "",
        "configuration": VARIANTS["extractor"].body(),
        "rows": [],
        "isDisabled": False,
    }
    w.pull()
    set_ignored([http])
    data = _yaml(flow_file.parent)
    second = copy.deepcopy(data["_configuration_extra"]["tasks"][0])
    second["id"] = "t2"
    second["task"].update({"componentId": mysql, "configId": "cfg-x"})
    data["_configuration_extra"]["tasks"].append(second)
    flow_file.write_text(dump_config_yaml(data), encoding="utf-8", newline="")
    w.push(branch=DEV)  # creates X and rebinds it; E is still missing
    assert dev_pending() == [[http, f.extractor_id]]

    set_ignored([])
    w.push(branch=DEV)  # creates E, then rebinds the flow
    assert _dev_task_targets(w) == [_dev_id_of(w, http), _dev_id_of(w, mysql)]
    assert dev_pending() is None


# ===========================================================================
# Round 9 (D31; risks 63-64)
# ===========================================================================


@dataclass
class PendingFlow:
    """A dev-branch flow F pending a rebind, and the target's own copy of it."""

    manifest: Manifest
    flow: ManifestConfiguration
    remote: dict


def _pending_flow_on_dev(refs: list[list[str]], task: dict) -> PendingFlow:
    manifest = _manifest((PROD, "main"), (DEV, "dev"))
    flow = ManifestConfiguration(
        branchId=DEV,
        componentId="keboola.flow",
        id="F",
        path="other/keboola.flow/f",
        metadata={"rebind_pending": refs},
    )
    manifest.configurations.append(flow)
    remote = {"configuration": {"tasks": [{"id": "t", "task": task}]}}
    return PendingFlow(manifest=manifest, flow=flow, remote=remote)


def _in_memory_target(remote: dict, others: dict | None = None) -> MagicMock:
    """The flow's target copy; *others* answers other configs' reads (an exception raises)."""
    client = MagicMock()

    def detail(**kwargs: Any) -> dict:
        other = (others or {}).get((kwargs["component_id"], kwargs["config_id"]))
        if isinstance(other, Exception):
            raise other
        return copy.deepcopy(remote if other is None else other)

    client.get_config_detail.side_effect = detail

    def put(**kwargs: Any) -> dict:
        remote["configuration"] = copy.deepcopy(kwargs["configuration"])
        return copy.deepcopy(remote)

    client.update_config.side_effect = put
    return client


# -- risk 63: a task's config and rows are rewritten as ONE unit (D31) -------


def test_staggered_row_resolution_rewrites_the_task_only_when_complete() -> None:
    http = "keboola.ex-http"
    refs = [[http, "P"], [http, "P", "1"], [http, "P", "2"]]
    task = {"type": "job", "componentId": http, "configId": "P", "configRowIds": ["1", "2"]}
    pending = _pending_flow_on_dev(refs, task)
    manifest, flow, remote = pending.manifest, pending.flow, pending.remote
    client = _in_memory_target(remote)

    # Only row 1 exists on the branch yet: the task stays wholly in source form.
    first = PromoteRemap(configs={(http, "P"): "D"}, rows={("D", "1"): "2"})
    recover_pending(client, manifest, first, "keboola.flow", "F", DEV, [])
    sent = remote["configuration"]["tasks"][0]["task"]
    assert (sent["configId"], sent["configRowIds"]) == ("P", ["1", "2"])
    assert flow.metadata["rebind_pending"] == refs

    # Row 2 now exists: the whole unit is rewritten at once, the list cleared.
    second = PromoteRemap(configs={(http, "P"): "D"}, rows={("D", "1"): "2", ("D", "2"): "3"})
    recover_pending(client, manifest, second, "keboola.flow", "F", DEV, [])
    sent = remote["configuration"]["tasks"][0]["task"]
    assert (sent["configId"], sent["configRowIds"]) == ("D", ["2", "3"])
    assert "rebind_pending" not in flow.metadata


# -- risk 64: references leave the pending list only after a successful PUT --


def test_a_rejected_update_never_drops_a_pending_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = _seed_flow_world(tmp_path, flow_first=True)
    w = f.world
    http = VARIANTS["extractor"].component_id
    w.api.branch_from_prod()
    del w.api.remote[DEV][(http, f.extractor_id)]
    flow_file = w.config_dir(f.flow_id) / CONFIG_FILE
    semantic_edit(flow_file)
    original_create, original_update = w.api._create, w.api._update

    def http_create_fails(*args: Any, **kwargs: Any) -> dict:
        if kwargs.get("component_id") == http:
            raise KeboolaApiError("create refused", 500, ErrorCode.API_ERROR)
        return original_create(*args, **kwargs)

    monkeypatch.setattr(w.api, "_create", http_create_fails)
    assert w.svc.push(alias="prod", project_root=w.root, branch_override=DEV)["errors"]
    pending_file = flow_file.read_bytes()

    # Edit F so it no longer runs E; the dev branch rejects the update (400).
    data = _yaml(flow_file.parent)
    data["_configuration_extra"]["tasks"] = []
    flow_file.write_text(dump_config_yaml(data), encoding="utf-8", newline="")

    def flow_update_rejected(*args: Any, **kwargs: Any) -> dict:
        if kwargs.get("component_id") == "keboola.flow":
            raise KeboolaApiError("invalid configuration", 400, ErrorCode.API_ERROR)
        return original_update(*args, **kwargs)

    monkeypatch.setattr(w.api, "_update", flow_update_rejected)
    assert w.svc.push(alias="prod", project_root=w.root, branch_override=DEV)["errors"]
    monkeypatch.undo()

    flow_file.write_bytes(pending_file)  # back to what the dev branch holds
    w.push(branch=DEV)  # creates E, then rebinds the flow
    assert _dev_task_target(w) == _dev_id_of(w, http)
    assert not [c for c in load_manifest(w.root).configurations if c.metadata.get("rebind_pending")]


# ===========================================================================
# Round 10 (D32; risks 65-66)
# ===========================================================================


def _task_of(remote: dict) -> tuple[str, list[str]]:
    task = remote["configuration"]["tasks"][0]["task"]
    return task["configId"], task["configRowIds"]


# -- risk 65: a config the branch holds under the production id resolves -----


def test_recovery_resolves_rows_under_a_config_the_branch_holds_under_its_own_id() -> None:
    http = "keboola.ex-http"
    task = {"type": "job", "componentId": http, "configId": "P", "configRowIds": ["1"]}
    pending = _pending_flow_on_dev([[http, "P"], [http, "P", "1"]], task)
    # The dev branch holds P under the same id (a shadow); row "1" was created
    # there as "2", then the push died before its final rebind.
    pending.manifest.configurations.append(
        ManifestConfiguration(
            branchId=DEV,
            componentId=http,
            id="P",
            path="extractor/keboola.ex-http/p",
            metadata={"promoted_from": {"tree": "main", "config_id": "P"}},
            rows=[ManifestConfigRow(id="2", path="rows/r", metadata={"promoted_from_id": "1"})],
        )
    )
    remap = PromoteRemap()
    remap.refresh(pending.manifest, DEV, {}, {})
    client = _in_memory_target(pending.remote, {(http, "P"): {"rows": [{"id": "2"}]}})
    recover_pending(client, pending.manifest, remap, "keboola.flow", "F", DEV, [])

    assert _task_of(pending.remote) == ("P", ["2"])
    assert "rebind_pending" not in pending.flow.metadata


# -- risk 66: completion is computed from what the target holds now ---------


@pytest.mark.parametrize("all_rows_exist", [True, False], ids=["complete", "row-missing"])
def test_recovery_follows_a_row_selection_edited_on_the_branch(all_rows_exist: bool) -> None:
    http = "keboola.ex-http"
    # Pending for P/["1"]; meanwhile someone on the dev branch selected row 2 too.
    task = {"type": "job", "componentId": http, "configId": "P", "configRowIds": ["1", "2"]}
    pending = _pending_flow_on_dev([[http, "P"], [http, "P", "1"]], task)
    rows = {("D", "1"): "11", ("D", "2"): "22"} if all_rows_exist else {("D", "1"): "11"}
    remap = PromoteRemap(configs={(http, "P"): "D"}, rows=rows)
    client = _in_memory_target(pending.remote)
    recover_pending(client, pending.manifest, remap, "keboola.flow", "F", DEV, [])

    if all_rows_exist:
        assert _task_of(pending.remote) == ("D", ["11", "22"])
        assert "rebind_pending" not in pending.flow.metadata
    else:
        assert _task_of(pending.remote) == ("P", ["1", "2"])
        assert pending.flow.metadata["rebind_pending"] == [
            [http, "P"],
            [http, "P", "1"],
            [http, "P", "2"],
        ]


# ===========================================================================
# Round 11 (D33; risks 67-68): identity parents decided by the target's rows
# ===========================================================================


def test_identity_parent_row_already_on_the_branch_is_never_remapped_again() -> None:
    http = "keboola.ex-http"
    task = {"type": "job", "componentId": http, "configId": "P", "configRowIds": ["1"]}
    pending = _pending_flow_on_dev([[http, "P"], [http, "P", "1"], [http, "E"]], task)
    pending.remote["configuration"]["tasks"].append(
        {"id": "t2", "task": {"type": "job", "componentId": http, "configId": "E"}}
    )  # E is still missing, so the flow stays pending across recoveries
    remap = PromoteRemap(configs={(http, "P"): "P"}, rows={("P", "1"): "2", ("P", "2"): "3"})
    client = _in_memory_target(pending.remote, {(http, "P"): {"rows": [{"id": "2"}, {"id": "3"}]}})
    for _ in range(2):
        recover_pending(client, pending.manifest, remap, "keboola.flow", "F", DEV, [])

    assert _task_of(pending.remote) == ("P", ["2"])
    assert pending.flow.metadata["rebind_pending"] == [[http, "E"]]


def test_identity_parent_unit_with_a_missing_row_is_left_whole() -> None:
    http = "keboola.ex-http"
    task = {"type": "job", "componentId": http, "configId": "P", "configRowIds": ["1", "2"]}
    pending = _pending_flow_on_dev([[http, "P"], [http, "P", "1"]], task)
    remap = PromoteRemap(configs={(http, "P"): "P"}, rows={("P", "1"): "11"})
    client = _in_memory_target(pending.remote, {(http, "P"): {"rows": [{"id": "11"}]}})
    recover_pending(client, pending.manifest, remap, "keboola.flow", "F", DEV, [])

    client.update_config.assert_not_called()
    assert _task_of(pending.remote) == ("P", ["1", "2"])
    assert pending.flow.metadata["rebind_pending"] == [
        [http, "P"],
        [http, "P", "1"],
        [http, "P", "2"],
    ]


def test_identity_parent_with_unreadable_rows_is_left_untouched_and_pending() -> None:
    http = "keboola.ex-http"
    task = {"type": "job", "componentId": http, "configId": "P", "configRowIds": ["1"]}
    pending = _pending_flow_on_dev([[http, "P"], [http, "P", "1"]], task)
    remap = PromoteRemap(configs={(http, "P"): "P"}, rows={("P", "1"): "2"})
    unreadable = KeboolaApiError("rows unavailable", 500, ErrorCode.API_ERROR)
    client = _in_memory_target(pending.remote, {(http, "P"): unreadable})
    recover_pending(client, pending.manifest, remap, "keboola.flow", "F", DEV, [])

    client.update_config.assert_not_called()
    assert _task_of(pending.remote) == ("P", ["1"])
    assert pending.flow.metadata["rebind_pending"] == [[http, "P"], [http, "P", "1"]]


# ===========================================================================
# Round 12 (D34; risks 69-70): SOURCE mode for main/ payloads, TARGET mode for recovery
# ===========================================================================


def _flow_selecting(tmp_path: Path, row_id: str) -> World:
    """A pulled production flow whose task runs extractor P, row *row_id*."""
    w = World(tmp_path)
    w.api.types["keboola.flow"] = "other"
    body = _flow_body("keboola.ex-http", "P")
    body["tasks"][0]["task"]["configRowIds"] = [row_id]
    w.api.remote[PROD][("keboola.flow", "cfg-flow")] = {
        "id": "cfg-flow",
        "name": "Nightly",
        "description": "",
        "configuration": body,
        "rows": [],
        "isDisabled": False,
    }
    w.init()
    w.pull()
    return w


def _overlapping_identity_remap() -> PromoteRemap:
    """P is held on the dev branch under its own id; rows 1 -> 2 and 2 -> 3 overlap."""
    remap = PromoteRemap(
        configs={("keboola.ex-http", "P"): "P"}, rows={("P", "1"): "2", ("P", "2"): "3"}
    )
    remap.row_reader = lambda component_id, config_id: {"2", "3"}  # the dev copy's rows
    return remap


def _sent_rows(client: MagicMock) -> list[str]:
    configuration = client.update_config.call_args.kwargs["configuration"]
    return configuration["tasks"][0]["task"]["configRowIds"]


# -- risk 69: a main/ payload maps its source rows, never by dev existence ----


def test_promote_update_maps_a_source_row_even_when_the_dev_copy_holds_that_id(
    tmp_path: Path,
) -> None:
    w = _flow_selecting(tmp_path, "2")  # source row 2 -> dev row 3
    client = MagicMock()
    client.update_config.return_value = {}
    push_update(
        w.svc,
        client,
        "keboola.flow",
        "cfg-flow",
        w.config_dir("cfg-flow").relative_to(w.root / "main").as_posix(),
        w.root,
        load_manifest(w.root),
        DEV,
        remap=_overlapping_identity_remap(),
    )
    assert _sent_rows(client) == ["3"]


def test_final_backfill_maps_a_source_row_even_when_the_dev_copy_holds_that_id(
    tmp_path: Path,
) -> None:
    w = _flow_selecting(tmp_path, "2")
    config_dir = w.config_dir("cfg-flow")
    original = copy.deepcopy(w.api.remote[PROD][("keboola.flow", "cfg-flow")]["configuration"])
    remap = _overlapping_identity_remap()
    remap.sent[("keboola.flow", "cfg-flow")] = SentPayload(
        config_dir=config_dir,
        config_path=config_dir.relative_to(w.root / "main").as_posix(),
        original=original,
        configuration=original,  # sent before the row map knew row 2
    )
    client = MagicMock()
    client.get_config_detail.return_value = {"rows": [{"id": "2"}, {"id": "3"}]}
    client.update_config.side_effect = lambda **kwargs: {"configuration": kwargs["configuration"]}
    finish_promote_rebind(
        w.svc,
        client,
        remap,
        manifest=load_manifest(w.root),
        target=CreateTarget(w.root, "main", DEV),
        warnings=[],
        errors=[],
        diff_changes={},
        ignored_components=frozenset(),
    )
    assert _sent_rows(client) == ["3"]


# -- risk 70: a TARGET-mode identity unit with an unresolved row stays pending --


@pytest.mark.parametrize("rows_readable", [True, False], ids=["row-not-on-dev", "rows-unreadable"])
def test_identity_unit_edited_to_an_unknown_row_keeps_the_config_pending(
    rows_readable: bool,
) -> None:
    http = "keboola.ex-http"
    # Pending for P/["1"]; on the dev branch the task was edited to row 99.
    task = {"type": "job", "componentId": http, "configId": "P", "configRowIds": ["99"]}
    pending = _pending_flow_on_dev([[http, "P"], [http, "P", "1"]], task)
    remap = PromoteRemap(configs={(http, "P"): "P"}, rows={("P", "1"): "2"})
    dev_rows: object = (
        {"rows": [{"id": "2"}]}
        if rows_readable
        else KeboolaApiError("rows unavailable", 500, ErrorCode.API_ERROR)
    )
    client = _in_memory_target(pending.remote, {(http, "P"): dev_rows})
    recover_pending(client, pending.manifest, remap, "keboola.flow", "F", DEV, [])

    client.update_config.assert_not_called()
    assert _task_of(pending.remote) == ("P", ["99"])
    assert pending.flow.metadata["rebind_pending"] == [[http, "P"], [http, "P", "99"]]


# ===========================================================================
# Round 13 (risk 71): a row added after the promote keeps its source identity
# ===========================================================================


def test_row_added_to_a_promoted_config_is_mapped_when_a_flow_selects_it(
    tmp_path: Path,
) -> None:
    v = VARIANTS["rows"]
    w = World(tmp_path)
    ext_id = w.api.seed(v)  # P, listed before the flow
    w.api.types["keboola.flow"] = "other"
    w.api.remote[PROD][("keboola.flow", "cfg-flow")] = {
        "id": "cfg-flow",
        "name": "Nightly",
        "description": "",
        "configuration": _flow_body(v.component_id, ext_id),
        "rows": [],
        "isDisabled": False,
    }
    w.init()
    w.pull()
    w.push(branch=DEV)  # 1. promote P -> D
    dev_ext = _dev_id_of(w, v.component_id)

    # 2. production gains row r2; pull production (main/ tracks it, the DEV entry does not)
    r2 = w.api._create_row(
        v.component_id, ext_id, name="Row 3", configuration={"parameters": {"table": "t3"}}
    )["id"]
    w.pull()

    # 3. the source flow now selects P / [r2]
    flow_file = w.config_dir("cfg-flow") / CONFIG_FILE
    data = _yaml(flow_file.parent)
    data["_configuration_extra"]["tasks"][0]["task"]["configRowIds"] = [r2]
    flow_file.write_text(dump_config_yaml(data), encoding="utf-8", newline="")

    # 4. push to DEV: r2 is created under D as d2, and the task follows it
    w.push(branch=DEV)
    (d2,) = [
        row["id"]
        for row in w.api.get(DEV, v.component_id, dev_ext)["rows"]
        if row["name"] == "Row 3"
    ]
    task = _dev_flow(w)["configuration"]["tasks"][0]["task"]
    assert (task["configId"], task["configRowIds"]) == (dev_ext, [d2])
    manifest = load_manifest(w.root)
    (dev_entry,) = [c for c in manifest.configurations if c.id == dev_ext and c.branch_id == DEV]
    (row_entry,) = [r for r in dev_entry.rows if r.id == d2]
    assert row_entry.metadata["promoted_from_id"] == r2  # recorded on create
    assert not any(c.metadata.get("rebind_pending") for c in manifest.configurations)


# ===========================================================================
# Round 14 (risks 72-73): source row identity in planning and source lookup
# ===========================================================================


def test_flow_selecting_a_new_source_row_stays_pending_when_its_rebind_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v = VARIANTS["rows"]
    w = World(tmp_path)
    ext_id = w.api.seed(v)
    w.api.types["keboola.flow"] = "other"
    w.api.remote[PROD][("keboola.flow", "cfg-flow")] = {
        "id": "cfg-flow",
        "name": "Nightly",
        "description": "",
        "configuration": _flow_body(v.component_id, ext_id),
        "rows": [],
        "isDisabled": False,
    }
    w.api.branch_from_prod()  # DEV holds P under the production id
    w.init()
    w.pull()
    semantic_edit(w.config_dir(ext_id) / CONFIG_FILE)
    w.push(branch=DEV)  # the dev entry of P exists from here on

    r2 = w.api._create_row(
        v.component_id, ext_id, name="Row 3", configuration={"parameters": {"table": "t3"}}
    )["id"]
    w.pull()
    flow_file = w.config_dir("cfg-flow") / CONFIG_FILE
    data = _yaml(flow_file.parent)
    data["_configuration_extra"]["tasks"][0]["task"]["configRowIds"] = [r2]
    flow_file.write_text(dump_config_yaml(data), encoding="utf-8", newline="")

    original = w.api._update

    def remap_put_fails(*args: Any, **kwargs: Any) -> dict:
        if str(kwargs.get("change_description", "")).startswith("Remap"):
            raise KeboolaApiError("refused", 500, ErrorCode.API_ERROR)
        return original(*args, **kwargs)

    monkeypatch.setattr(w.api, "_update", remap_put_fails)
    assert w.svc.push(alias="prod", project_root=w.root, branch_override=DEV)["errors"]
    monkeypatch.undo()
    (flow_entry,) = [
        c
        for c in load_manifest(w.root).configurations
        if c.component_id == "keboola.flow" and c.branch_id == DEV
    ]
    assert flow_entry.metadata.get("rebind_pending")  # marked before the PUT

    w.push(branch=DEV)  # no local edit since: recovery finishes it
    (d2,) = [
        row["id"]
        for row in w.api.get(DEV, v.component_id, ext_id)["rows"]
        if row["name"] == "Row 3"
    ]
    task = _dev_flow(w)["configuration"]["tasks"][0]["task"]
    assert (task["configId"], task["configRowIds"]) == (ext_id, [d2])


def test_source_row_lookup_ignores_an_unregistered_branch_entry(tmp_path: Path) -> None:
    def entry(branch_id: int, config_id: str, row_id: str | None) -> ManifestConfiguration:
        return ManifestConfiguration(
            branchId=branch_id,
            componentId="keboola.ex-http",
            id=config_id,
            path="extractor/keboola.ex-http/orders",
            rows=[ManifestConfigRow(id=row_id, path="rows/new")] if row_id else [],
        )

    dev = entry(DEV, "D", None)
    dev.metadata["promoted_from"] = {"tree": "main", "config_id": "P"}
    manifest = _manifest(
        (PROD, "main"),
        (DEV, "dev"),
        configurations=[entry(999, "P", "wrong"), entry(PROD, "P", "r2"), dev],
    )
    row_dir = tmp_path / "main" / dev.path / "rows" / "new"
    row_dir.mkdir(parents=True)
    (row_dir / CONFIG_FILE).write_text("name: New\n", encoding="utf-8")

    found = find_untracked_rows(
        tmp_path, manifest, lambda d: yaml.safe_load((d / CONFIG_FILE).read_text()), "main", [dev]
    )
    assert [r["source_row_id"] for r in found] == ["r2"]


# ===========================================================================
# Round 15 (risk 74): rows promoted before production created them
# ===========================================================================


def test_rows_promoted_before_production_created_them_map_to_the_dev_rows(
    tmp_path: Path,
) -> None:
    v = VARIANTS["rows"]
    w = World(tmp_path)
    w.api.types[v.component_id] = v.component_type
    w.api.types["keboola.flow"] = "other"
    w.init()
    w.pull()
    author_config(w.root / "main", v)  # a config with rows, no ids
    for _ in range(2):  # a second push is a no-op once every row exists
        w.push(branch=DEV)  # 1. promoted first: D, d1
    for _ in range(2):
        w.push()  # 2. then created on production: P, r1

    prod_ext = _prod_id_of(w, v.component_id)
    dev_ext = _dev_id_of(w, v.component_id)
    (r1,) = [
        r["id"] for r in w.api.get(PROD, v.component_id, prod_ext)["rows"] if r["name"] == "Row 1"
    ]
    (d1,) = [
        r["id"] for r in w.api.get(DEV, v.component_id, dev_ext)["rows"] if r["name"] == "Row 1"
    ]

    # 3. a flow selecting P / [r1], promoted
    body = _flow_body(v.component_id, prod_ext)
    body["tasks"][0]["task"]["configRowIds"] = [r1]
    w.api.remote[PROD][("keboola.flow", "cfg-flow")] = {
        "id": "cfg-flow",
        "name": "Nightly",
        "description": "",
        "configuration": body,
        "rows": [],
        "isDisabled": False,
    }
    w.pull()
    w.push(branch=DEV)

    task = _dev_flow(w)["configuration"]["tasks"][0]["task"]
    assert (task["configId"], task["configRowIds"]) == (dev_ext, [d1])
    assert not any(c.metadata.get("rebind_pending") for c in load_manifest(w.root).configurations)


def _prod_id_of(w: World, component_id: str) -> str:
    (prod_id,) = [cid for comp, cid in w.api.remote[PROD] if comp == component_id]
    return prod_id


# ===========================================================================
# Round 16 (risks 75-77)
# ===========================================================================


# -- risk 75: ignore, pull, un-ignore, pull: the promote still updates D -------


def test_unignored_component_promotes_into_its_dev_config_not_a_duplicate(
    tmp_path: Path,
) -> None:
    v = VARIANTS["extractor"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    semantic_edit(w.config_dir(cid) / CONFIG_FILE)
    w.push(branch=DEV)  # P promoted as D
    dev_id = _dev_id_of(w, v.component_id)

    for ignored in ([v.component_id], []):
        _set_ignored(w, ignored)
        w.pull()
    # Risk 78: the ignored pull removed main/'s directory (documented), and the
    # first edit lived only there and on DEV. Re-pulled, main/ holds production
    # again, so one edit recreates exactly what D holds: nothing to push.
    assert "edit_marker" not in (_yaml(w.config_dir(cid)).get("parameters") or {})
    semantic_edit(w.config_dir(cid) / CONFIG_FILE)
    assert w.push(branch=DEV)["status"] == "no_changes"
    semantic_edit(w.config_dir(cid) / CONFIG_FILE)
    w.push(branch=DEV)

    assert list(w.api.remote[DEV]) == [(v.component_id, dev_id)]  # no duplicate
    dev_config = w.api.get(DEV, v.component_id, dev_id)["configuration"]
    assert dev_config["parameters"]["edit_marker"] == 2  # D got the new edit


def _set_ignored(w: World, ignored: list[str]) -> None:
    manifest = load_manifest(w.root)
    manifest.ignored_components = ignored
    save_manifest(w.root, manifest)


# -- risk 76: a crash right after the production create's checkpoint ---------


def test_crash_after_the_production_create_checkpoint_keeps_the_promote_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v = VARIANTS["rows"]
    w = World(tmp_path)
    w.api.types[v.component_id] = v.component_type
    w.api.types["keboola.flow"] = "other"
    w.init()
    w.pull()
    author_config(w.root / "main", v)
    for _ in range(2):
        w.push(branch=DEV)  # D, d1
    dev_ext = _dev_id_of(w, v.component_id)

    def crash(*args: Any, **kwargs: Any) -> None:
        raise KeboolaApiError("crashed after the checkpoint", 500, ErrorCode.API_ERROR)

    monkeypatch.setattr(_sync_push_create, "writeback_after_push", crash)
    w.svc.push(alias="prod", project_root=w.root)  # P created and checkpointed, then dies
    monkeypatch.undo()
    for _ in range(2):
        w.push()  # the retry creates r1

    prod_ext = _prod_id_of(w, v.component_id)
    (r1,) = [
        r["id"] for r in w.api.get(PROD, v.component_id, prod_ext)["rows"] if r["name"] == "Row 1"
    ]
    (d1,) = [
        r["id"] for r in w.api.get(DEV, v.component_id, dev_ext)["rows"] if r["name"] == "Row 1"
    ]
    body = _flow_body(v.component_id, prod_ext)
    body["tasks"][0]["task"]["configRowIds"] = [r1]
    w.api.remote[PROD][("keboola.flow", "cfg-flow")] = {
        "id": "cfg-flow",
        "name": "Nightly",
        "description": "",
        "configuration": body,
        "rows": [],
        "isDisabled": False,
    }
    w.pull()
    w.push(branch=DEV)

    task = _dev_flow(w)["configuration"]["tasks"][0]["task"]
    assert (task["configId"], task["configRowIds"]) == (dev_ext, [d1])


# -- risk 77: a row mapped to its own id is resolved ---------------------------


def test_a_row_mapped_to_its_own_id_resolves_the_unit() -> None:
    http = "keboola.ex-http"
    dev = ManifestConfiguration(
        branchId=DEV,
        componentId=http,
        id="D",
        path="extractor/keboola.ex-http/orders",
        metadata={"promoted_from": {"tree": "main", "config_id": "P"}},
        rows=[ManifestConfigRow(id="r1", path="rows/a", metadata={"promoted_from_id": "r1"})],
    )
    remap = PromoteRemap()
    remap.refresh(_manifest((PROD, "main"), (DEV, "dev"), configurations=[dev]), DEV, {}, {})
    body = _flow_body(http, "P")
    body["tasks"][0]["task"]["configRowIds"] = ["r1"]
    task = remap.apply("keboola.flow", body)["tasks"][0]["task"]
    assert (task["configId"], task["configRowIds"]) == ("D", ["r1"])


# ===========================================================================
# Round 17 (risks 79-81)
# ===========================================================================


# -- risk 79: an ignored DEV pull keeps the promote entry it owns -------------


def test_ignored_dev_pull_keeps_the_promote_entry_and_never_duplicates(
    tmp_path: Path,
) -> None:
    v = VARIANTS["extractor"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    semantic_edit(w.config_dir(cid) / CONFIG_FILE)
    w.push(branch=DEV)  # P promoted as D, the only component
    dev_id = _dev_id_of(w, v.component_id)
    (before,) = _entries(w, dev_id, DEV)

    _set_ignored(w, [v.component_id])
    w.pull(branch=DEV)
    assert _entries(w, dev_id, DEV) == [before]  # carried untouched
    _set_ignored(w, [])
    w.pull()
    semantic_edit(w.config_dir(cid) / CONFIG_FILE)
    w.push(branch=DEV)

    assert list(w.api.remote[DEV]) == [(v.component_id, dev_id)]  # no duplicate


# -- risk 80: a push with nothing to send still repairs an empty association --


def test_push_with_no_changes_links_a_promote_entry_left_without_its_source(
    tmp_path: Path,
) -> None:
    p = _authored_and_promoted(tmp_path)
    w, dev_id = p.world, p.dev_id
    w.push()  # production creates P
    prod_id = _prod_id_of(w, VARIANTS["extractor"].component_id)
    manifest = load_manifest(w.root)
    (dev,) = [c for c in manifest.configurations if c.id == dev_id]
    dev.metadata["promoted_from"] = {"tree": "main", "config_id": ""}  # as a crash left it
    save_manifest(w.root, manifest)

    assert w.push()["status"] == "no_changes"
    (dev,) = _entries(w, dev_id, DEV)
    assert dev.metadata["promoted_from"] == {"tree": "main", "config_id": prod_id}


# -- risk 81: a row reusing a path is never linked to a surviving shadow row ---


def test_replacement_row_reusing_a_path_is_never_mapped_to_the_shadow_row(
    tmp_path: Path,
) -> None:
    v = VARIANTS["rows"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    w.api.types["keboola.flow"] = "other"
    w.api.branch_from_prod()  # DEV inherits P/r1
    semantic_edit(w.config_dir(cid) / CONFIG_FILE)
    w.push(branch=DEV)  # the shadow of P tracks r1
    prod_rows = w.api.get(PROD, v.component_id, cid)["rows"]
    (r1,) = [r["id"] for r in prod_rows if r["name"] == "Row 1"]

    # Production deletes r1, then creates r2 under the same name: same row dir.
    prod_rows[:] = [r for r in prod_rows if r["id"] != r1]
    w.pull()
    r2 = w.api._create_row(
        v.component_id, cid, name="Row 1", configuration={"parameters": {"table": "t9"}}
    )["id"]
    w.pull()
    body = _flow_body(v.component_id, cid)
    body["tasks"][0]["task"]["configRowIds"] = [r2]
    w.api.remote[PROD][("keboola.flow", "cfg-flow")] = {
        "id": "cfg-flow",
        "name": "Nightly",
        "description": "",
        "configuration": body,
        "rows": [],
        "isDisabled": False,
    }
    w.pull()
    w.push(branch=DEV)

    (shadow,) = _entries(w, cid, DEV)
    assert all(
        r.metadata.get("promoted_from_id") in (None, r.id) for r in shadow.rows
    )  # never linked to r2
    task = _dev_flow(w)["configuration"]["tasks"][0]["task"]
    assert task["configRowIds"] != [r1]
