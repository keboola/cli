"""Rows under a config directory the manifest does not track (group 2, D7-D9).

A new config authored with ``rows/`` is created WITH its rows in one push (D7),
also when a create fails or the push dies half-way and is retried. A directory
adopted by its config id has its rows diffed by id with an unknown base (D8):
a difference is a conflict, never an update, a missing row never a delete,
and registering the config never re-creates its rows. A flow created in the
same push as a row it selects is finished by a later push once that row
exists (D9).

The walk world (``test_sync_random_walk``): ex-http 1000 "Name tk6z" with rows
"Row tk2z"/1001 and "Row tk4z"/1002, ex-http 1003 "Name tk13z" with rows
"Row tk9z"/1004 and "Row tk11z"/1005 (value tk12z); new ids start at 1010.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.services import sync_service as sync_service_module
from keboola_agent_cli.sync.config_format import api_config_to_local
from keboola_agent_cli.sync.diff_engine import config_hash
from keboola_agent_cli.sync.manifest import load_manifest, save_manifest
from test_sync_clone_links import EXTRACTOR, FLOW, SQL
from test_sync_clone_links import World as CloneWorld
from test_sync_random_walk import (
    DEV,
    HTTP,
    HTTP_2,
    PROD,
    Sim,
    _changes,
    _dir,
    _push,
    _world,
    read_yaml,
    write_yaml,
)

HTTP_DIR = "extractor/keboola.ex-http/name-tk6z"
HTTP_2_DIR = "extractor/keboola.ex-http/name-tk13z"


class Crash(BaseException):
    """A process dying mid-push: no ``except Exception`` handler sees it."""


def _author(sim: Sim, name: str, value: str, *, keep_row_ids: bool = False) -> Path:
    """A new config dir copied from ex-http 1000: ids stripped, its own name and row values."""
    src = _dir(sim, HTTP_DIR)
    dst = src.parent / name.lower().replace(" ", "-")
    shutil.copytree(src, dst)
    for yml in dst.rglob("_config.yml"):
        data = read_yaml(yml)
        meta = data.get("_keboola") or {}
        meta.pop("config_id", None)
        if not keep_row_ids:
            meta.pop("row_id", None)
        data["_keboola"] = meta
        if yml.parent == dst:
            data["name"] = name
        else:
            data["parameters"] = {"value": value}
        write_yaml(yml, data)
    return dst


def _created(sim: Sim, branch: int, name: str) -> list[dict[str, Any]]:
    return [cfg for cfg in sim.api.remote[branch].values() if cfg["name"] == name]


def _row_values(cfg: dict[str, Any]) -> list[str]:
    return [r["configuration"]["parameters"]["value"] for r in cfg["rows"]]


def _fail_once(sim: Sim, attribute: str, name: str, error: BaseException) -> None:
    """Make the fake API's create of the config/row called *name* raise *error* once."""
    original = getattr(sim.api, attribute)
    state = {"failed": False}

    def flaky(**kwargs: Any) -> Any:
        if kwargs["name"] == name and not state["failed"]:
            state["failed"] = True
            raise error
        return original(**kwargs)

    setattr(sim.api, attribute, flaky)


# ---------------------------------------------------------------------------
# D7: a new config's rows go up with it
# ---------------------------------------------------------------------------


def test_dry_run_lists_the_rows_of_a_new_config(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    _author(sim, "Name tk901z", "tk911z")
    result = sim.svc.push(alias="prod", project_root=sim.root, dry_run=True)
    rows = [c for c in result["changes"] if c.get("is_row")]
    assert [c["change_type"] for c in rows] == ["added", "added"]
    assert {c["parent_path"] for c in rows} == {"extractor/keboola.ex-http/name-tk901z"}
    assert {c["parent_config_id"] for c in rows} == {""}


def test_two_new_configs_get_their_own_rows(tmp_path: Path) -> None:
    """Risk 5: both parents have the empty id until created; rows go by directory."""
    sim = _world(tmp_path)
    _author(sim, "Name tk901z", "tk911z")
    _author(sim, "Name tk902z", "tk912z")

    result = _push(sim)

    assert result["errors"] == []
    assert result["created"] == 6
    (first,) = _created(sim, PROD, "Name tk901z")
    (second,) = _created(sim, PROD, "Name tk902z")
    assert _row_values(first) == ["tk911z", "tk911z"]
    assert _row_values(second) == ["tk912z", "tk912z"]
    assert _changes(sim) == []


@pytest.mark.parametrize("keep_row_ids", [False, True], ids=["fresh-rows", "copied-row-ids"])
def test_failed_row_create_is_retried_alone(tmp_path: Path, keep_row_ids: bool) -> None:
    """Risk 6: parent and first row created, second fails; the retry creates only it."""
    sim = _world(tmp_path)
    _author(sim, "Name tk901z", "tk911z", keep_row_ids=keep_row_ids)
    _fail_once(
        sim,
        "_create_row",
        "Row tk4z",
        KeboolaApiError("Storage refused the row", 500, ErrorCode.API_ERROR),
    )

    first = _push(sim)
    assert len(first["errors"]) == 1
    assert first["created"] == 2

    second = _push(sim)
    assert second["errors"] == []
    assert second["created"] == 1
    (cfg,) = _created(sim, PROD, "Name tk901z")
    assert sorted(r["name"] for r in cfg["rows"]) == ["Row tk2z", "Row tk4z"]
    assert _changes(sim) == []


def test_push_that_dies_after_the_parent_create_resumes_without_duplicates(
    tmp_path: Path,
) -> None:
    """Risk 6 (crash): the parent's create checkpoint is all that survives."""
    sim = _world(tmp_path)
    _author(sim, "Name tk901z", "tk911z")
    _fail_once(sim, "_create_row", "Row tk2z", Crash())

    with pytest.raises(Crash):
        _push(sim)
    assert len(_created(sim, PROD, "Name tk901z")) == 1

    retry = _push(sim)
    assert retry["errors"] == []
    (cfg,) = _created(sim, PROD, "Name tk901z")
    assert sorted(r["name"] for r in cfg["rows"]) == ["Row tk2z", "Row tk4z"]
    assert _changes(sim) == []


def test_rows_of_a_parent_that_was_not_created_wait_for_it(tmp_path: Path) -> None:
    """A failed parent create skips its rows with a dependency error; the retry does both."""
    sim = _world(tmp_path)
    _author(sim, "Name tk901z", "tk911z")
    _fail_once(
        sim,
        "_create",
        "Name tk901z",
        KeboolaApiError("Storage refused the config", 500, ErrorCode.API_ERROR),
    )

    first = _push(sim)
    row_errors = [e for e in first["errors"] if e.get("error_code") == "PARENT_CONFIG_NOT_TRACKED"]
    assert len(row_errors) == 2
    assert _created(sim, PROD, "Name tk901z") == []

    second = _push(sim)
    assert second["errors"] == []
    (cfg,) = _created(sim, PROD, "Name tk901z")
    assert len(cfg["rows"]) == 2


def test_new_config_with_rows_reaches_dev_in_one_promote_push(tmp_path: Path) -> None:
    """Risk 10: ``push --branch DEV`` from main/ creates the rows in the same run."""
    sim = _world(tmp_path)
    _author(sim, "Name tk901z", "tk911z")
    result = _push(sim, DEV)
    assert result["errors"] == []
    (cfg,) = _created(sim, DEV, "Name tk901z")
    assert _row_values(cfg) == ["tk911z", "tk911z"]
    assert _push(sim, DEV)["status"] == "no_changes"


# ---------------------------------------------------------------------------
# D8: an adopted directory's rows
# ---------------------------------------------------------------------------


def _forget(sim: Sim, key: tuple[str, str]) -> None:
    """The manifest loses the entry; the directory keeps its config id."""
    manifest = load_manifest(sim.root)
    manifest.configurations = [c for c in manifest.configurations if (c.component_id, c.id) != key]
    save_manifest(sim.root, manifest)


def _row_file(sim: Sim, row_dir: str) -> Path:
    return _dir(sim, f"{HTTP_2_DIR}/rows/{row_dir}") / "_config.yml"


def test_edited_row_of_an_adopted_config_is_a_conflict_not_an_update(tmp_path: Path) -> None:
    """Risk 8: no baseline says which side changed it."""
    sim = _world(tmp_path)
    _forget(sim, HTTP_2)
    row = _row_file(sim, "row-tk11z")
    row.write_text(row.read_text().replace("tk12z", "tk900z"), encoding="utf-8")

    assert ("conflict", "1003") in _changes(sim)
    result = _push(sim)

    assert result.get("skipped")
    rows = sim.api.remote[PROD][HTTP_2]["rows"]
    assert [r["configuration"]["parameters"]["value"] for r in rows] == ["tk10z", "tk12z"]


def test_deleted_rows_of_an_adopted_config_are_not_deleted(tmp_path: Path) -> None:
    """Risk 14: no row baseline, so a missing row is no intentional delete."""
    sim = _world(tmp_path)
    _forget(sim, HTTP_2)
    shutil.rmtree(_dir(sim, f"{HTTP_2_DIR}/rows"))

    changes = _changes(sim)
    assert ("conflict", "1003") in changes
    assert not any(kind == "deleted" for kind, _ in changes)
    _push(sim, force=True)
    assert len(sim.api.remote[PROD][HTTP_2]["rows"]) == 2


def test_registering_an_adopted_config_registers_its_rows(tmp_path: Path) -> None:
    """D8: the scaffold-style adoption (base_config_hash) push never re-creates rows."""
    sim = _world(tmp_path)
    _forget(sim, HTTP_2)
    config_file = _dir(sim, HTTP_2_DIR) / "_config.yml"
    data = read_yaml(config_file)
    remote = sim.api.remote[PROD][HTTP_2]
    data["_keboola"]["base_config_hash"] = config_hash(api_config_to_local(HTTP, remote, "1003"))
    data["parameters"]["value"] = "tk900z"
    write_yaml(config_file, data)

    first = _push(sim)
    assert (first["updated"], first["created"]) == (1, 0)
    (entry,) = [c for c in load_manifest(sim.root).configurations if c.id == "1003"]
    assert sorted(r.id for r in entry.rows) == ["1004", "1005"]
    assert {r.metadata.get("unknown_base") for r in entry.rows} == {"adopted"}

    assert _push(sim)["status"] == "no_changes"
    assert len(remote["rows"]) == 2


def test_new_row_under_an_unchanged_adopted_config_is_created_once(tmp_path: Path) -> None:
    """D8: the adopted parent is registered (it equals the remote), then the row created."""
    sim = _world(tmp_path)
    _forget(sim, HTTP_2)
    new_row = _dir(sim, f"{HTTP_2_DIR}/rows/row-tk903z")
    shutil.copytree(_dir(sim, f"{HTTP_2_DIR}/rows/row-tk9z"), new_row)
    data = read_yaml(new_row / "_config.yml")
    data["name"] = "Row tk903z"
    data["parameters"] = {"value": "tk904z"}
    data["_keboola"].pop("row_id")
    write_yaml(new_row / "_config.yml", data)

    first = _push(sim)
    assert first["errors"] == []
    assert first["created"] == 1
    rows = sim.api.remote[PROD][HTTP_2]["rows"]
    assert [r["name"] for r in rows] == ["Row tk9z", "Row tk11z", "Row tk903z"]
    assert _push(sim)["status"] == "no_changes"
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# D9: a flow created with a row that failed is finished by a later push
# ---------------------------------------------------------------------------


def _flow_task(world: CloneWorld, task_id: int) -> dict[str, Any]:
    tasks = world.target(FLOW)["configuration"]["tasks"]
    return next(t for t in tasks if t["id"] == task_id)["task"]


def _flow_entry_metadata(world: CloneWorld) -> dict[str, Any]:
    (entry,) = [c for c in load_manifest(world.clone_dir).configurations if c.component_id == FLOW]
    return entry.metadata


def _orders_row_id(world: CloneWorld) -> str:
    return next(r["id"] for r in world.target(EXTRACTOR)["rows"] if r["name"] == "orders")


def test_flow_selecting_a_failed_row_is_finished_by_the_next_push(
    tmp_path: Path, tmp_config_dir: Path
) -> None:
    """Risk 7: the reference stays pending until the row exists, no edit needed."""
    world = CloneWorld(tmp_path, tmp_config_dir)
    world.tgt_api.fail_row_names.add("orders")

    first = world.clone()
    assert any(e["error_code"] == ErrorCode.LINK_UNRESOLVED for e in first["errors"])
    assert _flow_task(world, 14)["configRowIds"] == ["exrow-1"]
    assert _flow_entry_metadata(world).get("link_pending") is True

    world.tgt_api.fail_row_names.clear()
    second = world.clone()

    assert second["errors"] == []
    assert _flow_task(world, 14)["configRowIds"] == [_orders_row_id(world)]
    assert "link_pending" not in _flow_entry_metadata(world)
    assert world.clone()["status"] == "no_changes"


def test_flow_links_survive_a_push_that_dies_before_the_backfill(
    tmp_path: Path, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D9: the create checkpoints carry what the next push needs to finish the links."""
    world = CloneWorld(tmp_path, tmp_config_dir)
    original = sync_service_module.resolve_run_target_bindings
    state = {"crashed": False}

    def dies_once(*args: Any, **kwargs: Any) -> Any:
        if not state["crashed"]:
            state["crashed"] = True
            raise Crash()
        return original(*args, **kwargs)

    monkeypatch.setattr(sync_service_module, "resolve_run_target_bindings", dies_once)
    with pytest.raises(Crash):
        world.clone()
    assert _flow_task(world, 10)["configId"] == "tr1-ref"

    second = world.clone()

    assert second["errors"] == []
    assert _flow_task(world, 10)["configId"] == world.target(SQL, "SQL step")["id"]
    assert _flow_task(world, 14)["configRowIds"] == [_orders_row_id(world)]
    assert world.clone()["status"] == "no_changes"
