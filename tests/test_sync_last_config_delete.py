"""Deleting the last config of a pulled dev-branch tree is a delete (customer feedback 3.1).

``sync init`` + ``sync pull --branch DEV`` into an empty folder, the dev branch
holds one config; the user deletes that config's directory and runs
``sync push --branch DEV --force``. The dev tree then holds no ``_config.yml``,
and push used to fall back to reading ``main/`` (the #929 promote path): the
config became an orphan "tracked on another branch" -- naming the synced branch
itself -- and nothing was deleted. Each test drives the real ``SyncService``
over the in-memory API of the bookkeeping differential test.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from keboola_agent_cli.sync.branch_scope import REASON_OTHER_BRANCH
from keboola_agent_cli.sync.manifest import load_manifest
from test_sync_bookkeeping_differential import DEV, PROD, VARIANTS, World


def _dev_checkout(tmp_path: Path, configs: int = 1) -> tuple[World, list[str]]:
    """A dev branch with *configs* configs, pulled alone into an empty folder."""
    w = World(tmp_path)
    variant = VARIANTS["extractor"]
    ids = [w.api.seed(variant, name=f"Dev {i}", branch=DEV) for i in range(configs)]
    w.api.seed(variant, name="Production only", branch=PROD)
    w.init()
    w.pull(branch=DEV)
    return w, ids


def _dev_keys(w: World) -> set[str]:
    return {cid for (_comp, cid) in w.api.remote[DEV]}


def test_plain_push_holds_back_the_deleted_last_config(tmp_path: Path) -> None:
    w, (cid,) = _dev_checkout(tmp_path)
    shutil.rmtree(w.config_dir(cid, DEV))

    result = w.push(branch=DEV)

    assert [d["config_id"] for d in result["skipped_deletions"]] == [cid]
    assert not result.get("orphaned")
    assert _dev_keys(w) == {cid}


def test_force_push_deletes_the_last_config_of_the_dev_branch(tmp_path: Path) -> None:
    w, (cid,) = _dev_checkout(tmp_path)
    shutil.rmtree(w.config_dir(cid, DEV))

    result = w.push(branch=DEV, force=True)

    assert result["status"] == "pushed"
    assert not result.get("orphaned")
    assert _dev_keys(w) == set()
    manifest = load_manifest(w.root)
    assert [c.id for c in manifest.configurations] == []


def test_diff_reports_the_deleted_last_config(tmp_path: Path) -> None:
    w, (cid,) = _dev_checkout(tmp_path)
    shutil.rmtree(w.config_dir(cid, DEV))

    result = w.diff(branch=DEV)

    assert [(c["config_id"], c["change_type"]) for c in result["changes"]] == [(cid, "deleted")]
    assert not result.get("orphaned")


def test_deleting_one_of_two_configs_still_works(tmp_path: Path) -> None:
    w, (gone, kept) = _dev_checkout(tmp_path, configs=2)
    shutil.rmtree(w.config_dir(gone, DEV))

    w.push(branch=DEV, force=True)

    assert _dev_keys(w) == {kept}


def test_a_dev_branch_without_a_tree_still_promotes_main(tmp_path: Path) -> None:
    """#929: ``push --branch DEV`` with no ``dev/`` tree reads ``main/``."""
    w = World(tmp_path)
    variant = VARIANTS["extractor"]
    w.api.seed(variant, name="Orders", branch=PROD)
    w.init()
    w.pull()

    assert w.source_tree(DEV) == w.root / "main"
    w.push(branch=DEV)

    assert [cfg["name"] for cfg in w.api.remote[DEV].values()] == ["Orders"]


def test_orphan_hint_never_calls_the_synced_branch_another_branch(tmp_path: Path) -> None:
    """With the whole ``dev/`` folder gone push reads ``main/``; the hint says why."""
    w, (cid,) = _dev_checkout(tmp_path)
    shutil.rmtree(w.root / "dev")

    result = w.push(branch=DEV, force=True)

    (record,) = [o for o in result["orphaned"] if o["config_id"] == cid]
    assert record["reason"] == REASON_OTHER_BRANCH
    assert "not the branch being synced" not in record["hint"]
    assert "the branch being synced" in record["hint"]
    assert f"sync pull --branch {DEV}" in record["hint"]
    assert _dev_keys(w) == {cid}
