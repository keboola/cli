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
from pathlib import Path

import yaml

from keboola_agent_cli.services._sync_writeback import writeback_create_config_in_manifest
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
)
from test_sync_bookkeeping_differential import (
    CONFIG_FILE,
    DEV,
    PROD,
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
    w, cid = _seeded(tmp_path, v)
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
    w, cid = _seeded(tmp_path, v)
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
    w, cid = _seeded(tmp_path, v)
    w.api.branch_from_prod()
    semantic_edit(w.config_dir(cid) / CONFIG_FILE)
    w.push(branch=DEV)

    summary = w.diff(branch=DEV)["summary"]
    assert summary["added"] == 0
    assert w.push(branch=DEV)["status"] == "no_changes"
    assert len(w.api.get(DEV, v.component_id, cid)["rows"]) == 2


def test_row_created_on_dev_is_recorded_under_the_dev_entry(tmp_path: Path) -> None:
    v = VARIANTS["rows"]
    w, cid = _seeded(tmp_path, v)
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
    w, cid = _seeded(tmp_path, v)
    w.api.branch_from_prod()
    shutil.rmtree(w.config_dir(cid))
    assert w.push(branch=DEV, force=True)["deleted"] == 1

    assert (v.component_id, cid) not in w.api.remote[DEV]
    assert len(_entries(w, cid, PROD)) == 1
    assert w.diff()["summary"]["deleted"] == 1  # still pending for production


# -- push --branch DEV onto a dev branch that lacks the config ---------------


def test_promote_create_keeps_the_prod_id_in_main(tmp_path: Path) -> None:
    v = VARIANTS["rows"]
    w, cid = _seeded(tmp_path, v)
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
    w, cid = _seeded(tmp_path, v)
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
