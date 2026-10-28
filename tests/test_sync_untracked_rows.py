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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.services import sync_service as sync_service_module
from keboola_agent_cli.sync.config_format import api_config_to_local
from keboola_agent_cli.sync.diff_engine import config_hash
from keboola_agent_cli.sync.manifest import load_manifest, save_manifest
from test_sync_bookkeeping_differential import VARIANTS
from test_sync_bookkeeping_differential import World as DiffWorld
from test_sync_clone_links import EXTRACTOR, FLOW, SQL
from test_sync_clone_links import World as CloneWorld
from test_sync_random_walk import (
    DEV,
    HTTP,
    HTTP_1,
    HTTP_2,
    PROD,
    Sim,
    _changes,
    _dir,
    _pull,
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


def _fail_once(sim: Any, attribute: str, name: str, error: BaseException) -> None:
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


def test_copied_row_ids_of_a_new_config_are_no_source_identity(tmp_path: Path) -> None:
    """S2 (replaces R9): a copied row id under a new config is a placeholder."""
    sim = _world(tmp_path)
    _author(sim, "Name tk901z", "tk911z", keep_row_ids=True)
    result = sim.svc.push(alias="prod", project_root=sim.root, dry_run=True)
    rows = [c for c in result["changes"] if c.get("is_row")]
    assert len(rows) == 2
    assert all(not c.get("source_row_id") and not c["config_id"] for c in rows)


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


# ---------------------------------------------------------------------------
# Round 2: R2, R3, R5, R6
# ---------------------------------------------------------------------------


def test_new_config_rows_survive_a_dev_pull_and_a_theirs_pull(tmp_path: Path) -> None:
    """R2 (walk seed 668): a row push created carries its id, so a later pull finds it.

    Without ``_keboola.row_id`` in the row file, ``pull --theirs`` after a
    ``pull --branch DEV`` wrote the rows into second directories and diff
    listed the first ones as new rows -- duplicates on the next push.
    """
    sim = _world(tmp_path)
    _author(sim, "Name tk901z", "tk911z")
    assert _push(sim)["created"] == 3
    _pull(sim, DEV)
    _pull(sim, theirs=True)

    assert _changes(sim) == []
    assert _push(sim)["status"] == "no_changes"
    (cfg,) = _created(sim, PROD, "Name tk901z")
    assert len(cfg["rows"]) == 2
    rows_dir = _dir(sim, "extractor/keboola.ex-http/name-tk901z/rows")
    assert len(list(rows_dir.iterdir())) == 2


def test_remotely_deleted_row_of_an_adopted_config_is_not_recreated(tmp_path: Path) -> None:
    """R5: a row file carrying an id the remote no longer has is ``remote_deleted``."""
    sim = _world(tmp_path)
    _forget(sim, HTTP_2)
    rows = sim.api.remote[PROD][HTTP_2]["rows"]
    rows[:] = [r for r in rows if r["id"] != "1005"]

    assert ("remote_deleted", "1003") in _changes(sim)
    result = _push(sim)

    assert result["created"] == 0
    assert [r["id"] for r in sim.api.remote[PROD][HTTP_2]["rows"]] == ["1004"]


def test_pending_link_recovery_keeps_a_remote_edit_of_the_flow(
    tmp_path: Path, tmp_config_dir: Path
) -> None:
    """R3: only the references of the remote config are rewritten, never the local file sent."""
    world = CloneWorld(tmp_path, tmp_config_dir)
    world.tgt_api.fail_row_names.add("orders")
    world.clone()
    flow = world.target(FLOW)
    flow["configuration"]["phases"][0]["name"] = "Edited in the UI"
    world.tgt_api.fail_row_names.clear()

    second = world.clone()

    assert second["errors"] == []
    flow = world.target(FLOW)
    assert flow["configuration"]["phases"][0]["name"] == "Edited in the UI"
    assert _flow_task(world, 14)["configRowIds"] == [_orders_row_id(world)]
    assert "link_pending" not in _flow_entry_metadata(world)
    # The local file is untouched: diff shows the remote change for a pull.
    diff = world.service.diff(alias="target", project_root=world.clone_dir)
    assert any(
        c["change_type"] == "remote_modified" and c["component_id"] == FLOW for c in diff["changes"]
    )


@dataclass(frozen=True)
class FlowOverRow:
    """A pulled ex-http config with an untracked row selected by a new flow."""

    world: DiffWorld
    config_id: str
    component_id: str


def _write_flow(w: DiffWorld, name: str, task: dict[str, Any], version: Any) -> None:
    flow_dir = w.root / "main" / "other" / "keboola.flow" / name.lower()
    flow_dir.mkdir(parents=True)
    write_yaml(
        flow_dir / "_config.yml",
        {
            "version": version,
            "name": name,
            "description": "",
            "_configuration_extra": {"tasks": [{"id": 1, "name": "t", "task": task}]},
            "_keboola": {"component_id": "keboola.flow"},
        },
    )


def _job(component_id: str, config_id: str, rows: list[str]) -> dict[str, Any]:
    return {
        "type": "job",
        "componentId": component_id,
        "configId": config_id,
        "configRowIds": rows,
        "mode": "run",
    }


def _flow_over_new_row(tmp_path: Path) -> FlowOverRow:
    variant = VARIANTS["rows"]
    w = DiffWorld(tmp_path)
    cid = w.api.seed(variant)
    w.api.types["keboola.flow"] = "other"
    w.init()
    w.pull()
    parent = w.config_dir(cid)
    (template,) = sorted(parent.glob("rows/*/_config.yml"))[:1]
    row = read_yaml(template)
    row["name"] = "Row New"
    row["_keboola"]["row_id"] = "row-R"  # a copied id the flow below selects
    (parent / "rows" / "row-new").mkdir()
    write_yaml(parent / "rows" / "row-new" / "_config.yml", row)
    _write_flow(w, "Nightly", _job(variant.component_id, cid, ["row-R"]), row["version"])
    return FlowOverRow(world=w, config_id=cid, component_id=variant.component_id)


def _assert_flow_selects_the_new_row(case: FlowOverRow) -> None:
    w = case.world
    new_row = next(
        r
        for r in w.api.get(PROD, case.component_id, case.config_id)["rows"]
        if r["name"] == "Row New"
    )
    (flow,) = [c for (comp, _), c in w.api.remote[PROD].items() if comp == "keboola.flow"]
    assert flow["configuration"]["tasks"][0]["task"]["configRowIds"] == [new_row["id"]]


def test_untracked_row_selected_by_a_new_flow_survives_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R6: the row's checkpoint records the id the flow selects it by."""
    case = _flow_over_new_row(tmp_path)
    w = case.world
    original = sync_service_module.resolve_run_target_bindings
    state = {"crashed": False}

    def dies_once(*args: Any, **kwargs: Any) -> Any:
        if not state["crashed"]:
            state["crashed"] = True
            raise Crash()
        return original(*args, **kwargs)

    monkeypatch.setattr(sync_service_module, "resolve_run_target_bindings", dies_once)
    with pytest.raises(Crash):
        w.svc.push(alias="prod", project_root=w.root)

    w.push()

    _assert_flow_selects_the_new_row(case)


def test_failed_row_create_under_an_existing_config_keeps_the_flow_pending(
    tmp_path: Path,
) -> None:
    """S3: the planned row is a dependency; the push that creates it sets the link."""
    case = _flow_over_new_row(tmp_path)
    w = case.world
    _fail_once(w, "_create_row", "Row New", KeboolaApiError("refused", 500, ErrorCode.API_ERROR))

    first = w.svc.push(alias="prod", project_root=w.root)
    assert any(e.get("error_code") == ErrorCode.LINK_UNRESOLVED for e in first["errors"])
    (flow_entry,) = [
        c for c in load_manifest(w.root).configurations if c.component_id == "keboola.flow"
    ]
    assert flow_entry.metadata.get("link_pending") is True

    w.push()

    _assert_flow_selects_the_new_row(case)


def _copy_config(w: DiffWorld, config_id: str, name: str) -> Path:
    """Copy a config dir to a new one: config id removed, row ids KEPT (copied)."""
    src = w.config_dir(config_id)
    dst = src.parent / name.lower()
    shutil.copytree(src, dst)
    data = read_yaml(dst / "_config.yml")
    data["_keboola"].pop("config_id")
    data["name"] = name
    write_yaml(dst / "_config.yml", data)
    return dst


def _by_name(cfg: dict[str, Any], name: str) -> str:
    return next(r["id"] for r in cfg["rows"] if r["name"] == name)


def test_copied_row_ids_under_a_new_config_are_linked_by_path(tmp_path: Path) -> None:
    """S2 (Astra's sequence): copy, promote, push to production, promote a flow over P/RP."""
    variant = VARIANTS["rows"]
    w = DiffWorld(tmp_path)
    cid = w.api.seed(variant)
    w.api.types["keboola.flow"] = "other"
    w.api.branch_from_prod()
    w.init()
    w.pull()
    _copy_config(w, cid, "Copied")
    w.push(branch=DEV)  # the copy on DEV: ND with rows RD
    w.push()  # the copy on production: P with rows RP
    (prod_copy,) = [c for c in w.api.remote[PROD].values() if c["name"] == "Copied"]
    (dev_copy,) = [c for c in w.api.remote[DEV].values() if c["name"] == "Copied"]
    version = read_yaml(w.config_dir(cid) / "_config.yml")["version"]
    _write_flow(
        w,
        "Nightly",
        _job(variant.component_id, prod_copy["id"], [_by_name(prod_copy, "Row 1")]),
        version,
    )

    w.push(branch=DEV)

    (flow,) = [c for (comp, _), c in w.api.remote[DEV].items() if comp == "keboola.flow"]
    task = flow["configuration"]["tasks"][0]["task"]
    assert (task["configId"], task["configRowIds"]) == (
        dev_copy["id"],
        [_by_name(dev_copy, "Row 1")],
    )


def test_copied_row_id_does_not_make_the_original_row_pending(tmp_path: Path) -> None:
    """S2: a reference to the ORIGINAL config's row R is not a planned create."""
    variant = VARIANTS["rows"]
    w = DiffWorld(tmp_path)
    cid = w.api.seed(variant)
    w.api.types["keboola.flow"] = "other"
    w.api.branch_from_prod()
    w.init()
    w.pull()
    _copy_config(w, cid, "Copied")
    original_row = _by_name(w.api.get(PROD, variant.component_id, cid), "Row 1")
    version = read_yaml(w.config_dir(cid) / "_config.yml")["version"]
    _write_flow(w, "Nightly", _job(variant.component_id, cid, [original_row]), version)

    w.push(branch=DEV)

    (flow,) = [c for (comp, _), c in w.api.remote[DEV].items() if comp == "keboola.flow"]
    task = flow["configuration"]["tasks"][0]["task"]
    assert (task["configId"], task["configRowIds"]) == (cid, [original_row])
    (entry,) = [
        c
        for c in load_manifest(w.root).configurations
        if c.component_id == "keboola.flow" and c.branch_id == DEV
    ]
    assert "rebind_pending" not in entry.metadata


def test_registering_an_adopted_config_keeps_a_remotely_deleted_row_identity(
    tmp_path: Path,
) -> None:
    """T3 (Astra's sequence): adopt, R gone remotely, a new row -- two pushes, no POST of R."""
    sim = _world(tmp_path)
    _forget(sim, HTTP_2)
    rows = sim.api.remote[PROD][HTTP_2]["rows"]
    rows[:] = [r for r in rows if r["id"] != "1005"]  # R deleted on the remote
    new_row = _dir(sim, f"{HTTP_2_DIR}/rows/row-tk903z")
    shutil.copytree(_dir(sim, f"{HTTP_2_DIR}/rows/row-tk9z"), new_row)
    data = read_yaml(new_row / "_config.yml")
    data["name"] = "Row tk903z"
    data["_keboola"].pop("row_id")
    write_yaml(new_row / "_config.yml", data)

    first = _push(sim)
    assert first["created"] == 1  # the new row, under the registered parent
    (entry,) = [c for c in load_manifest(sim.root).configurations if c.id == "1003"]
    assert "1005" in {r.id for r in entry.rows}

    second = _push(sim)

    assert second["created"] == 0
    assert ("remote_deleted", "1003") in _changes(sim)
    assert [r["name"] for r in sim.api.remote[PROD][HTTP_2]["rows"]] == ["Row tk9z", "Row tk903z"]


def test_two_row_dirs_carrying_one_deleted_row_id_are_never_posted(tmp_path: Path) -> None:
    """Round 5 P1 (Astra's input): rows/a and rows/b carry R, R is gone on the remote."""
    sim = _world(tmp_path)
    _forget(sim, HTTP_2)
    rows = sim.api.remote[PROD][HTTP_2]["rows"]
    rows[:] = [r for r in rows if r["id"] != "1005"]  # R deleted on the remote
    shutil.copytree(
        _dir(sim, f"{HTTP_2_DIR}/rows/row-tk11z"), _dir(sim, f"{HTTP_2_DIR}/rows/row-copy")
    )
    new_row = _dir(sim, f"{HTTP_2_DIR}/rows/row-tk903z")  # makes push register the parent
    shutil.copytree(_dir(sim, f"{HTTP_2_DIR}/rows/row-tk9z"), new_row)
    data = read_yaml(new_row / "_config.yml")
    data["name"] = "Row tk903z"
    data["_keboola"].pop("row_id")
    write_yaml(new_row / "_config.yml", data)

    for push_number in (1, 2):
        diff = sim.svc.diff(alias="prod", project_root=sim.root)
        conflicts = [
            c for c in diff["changes"] if c.get("is_row") and c["change_type"] == "conflict"
        ]
        assert sorted(c["path"] for c in conflicts) == ["rows/row-copy", "rows/row-tk11z"]
        assert all("rows/row-copy" in c["details"][0] for c in conflicts)
        assert all("rows/row-tk11z" in c["details"][0] for c in conflicts)
        result = _push(sim)
        assert result["created"] == (1 if push_number == 1 else 0)

    (entry,) = [c for c in load_manifest(sim.root).configurations if c.id == "1003"]
    assert "1005" not in {r.id for r in entry.rows}
    assert [r["name"] for r in sim.api.remote[PROD][HTTP_2]["rows"]] == ["Row tk9z", "Row tk903z"]


# ---------------------------------------------------------------------------
# Round 6: resolving an ambiguous row id never creates the row again
# ---------------------------------------------------------------------------


def _ambiguous_adopted_row(sim: Sim, *, delete_remotely: bool) -> None:
    """Astra's input: adopted 1003, rows/row-tk11z and rows/row-copy both carry 1005."""
    _forget(sim, HTTP_2)
    if delete_remotely:
        rows = sim.api.remote[PROD][HTTP_2]["rows"]
        rows[:] = [r for r in rows if r["id"] != "1005"]
    shutil.copytree(
        _dir(sim, f"{HTTP_2_DIR}/rows/row-tk11z"), _dir(sim, f"{HTTP_2_DIR}/rows/row-copy")
    )
    new_row = _dir(sim, f"{HTTP_2_DIR}/rows/row-tk903z")  # makes push register the parent
    shutil.copytree(_dir(sim, f"{HTTP_2_DIR}/rows/row-tk9z"), new_row)
    data = read_yaml(new_row / "_config.yml")
    data["name"] = "Row tk903z"
    data["_keboola"].pop("row_id")
    write_yaml(new_row / "_config.yml", data)


def _remove_row_id(sim: Sim, row_dir: str) -> None:
    path = _dir(sim, f"{HTTP_2_DIR}/rows/{row_dir}") / "_config.yml"
    data = read_yaml(path)
    data["_keboola"].pop("row_id")
    write_yaml(path, data)


def test_deleting_one_carrier_of_a_deleted_row_id_never_recreates_the_row(tmp_path: Path) -> None:
    """Round 6 P1 (Astra's sequence, 3 pushes): the dir left with R IS row R."""
    sim = _world(tmp_path)
    _ambiguous_adopted_row(sim, delete_remotely=True)

    assert _push(sim)["created"] == 1  # only the row without an id
    shutil.rmtree(_dir(sim, f"{HTTP_2_DIR}/rows/row-copy"))
    assert ("remote_deleted", "1003") in _changes(sim)
    assert _push(sim)["created"] == 0
    assert _push(sim)["created"] == 0

    assert [r["name"] for r in sim.api.remote[PROD][HTTP_2]["rows"]] == ["Row tk9z", "Row tk903z"]


def test_removing_a_carriers_row_id_makes_only_that_dir_a_new_row(tmp_path: Path) -> None:
    """Round 6 P1: the other resolution path -- the dir left with R is still never POSTed."""
    sim = _world(tmp_path)
    _ambiguous_adopted_row(sim, delete_remotely=True)

    assert _push(sim)["created"] == 1
    _remove_row_id(sim, "row-copy")
    assert _push(sim)["created"] == 1  # rows/row-copy, now a new row
    assert _push(sim)["created"] == 0

    rows = sim.api.remote[PROD][HTTP_2]["rows"]
    assert len(rows) == 3
    assert "1005" not in {r["id"] for r in rows}
    assert ("remote_deleted", "1003") in _changes(sim)


def test_resolving_an_ambiguity_while_the_row_still_exists_leaves_no_duplicate(
    tmp_path: Path,
) -> None:
    """Round 6 P1: R still on the remote -- the dir left with R is row R, unchanged."""
    sim = _world(tmp_path)
    _ambiguous_adopted_row(sim, delete_remotely=False)

    assert _push(sim)["created"] == 1
    shutil.rmtree(_dir(sim, f"{HTTP_2_DIR}/rows/row-copy"))
    assert _changes(sim) == []
    assert _push(sim)["created"] == 0
    assert _push(sim)["created"] == 0

    names = [r["name"] for r in sim.api.remote[PROD][HTTP_2]["rows"]]
    assert names == ["Row tk9z", "Row tk11z", "Row tk903z"]


def test_the_ambiguity_conflict_says_what_to_do(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    _ambiguous_adopted_row(sim, delete_remotely=True)
    diff = sim.svc.diff(alias="prod", project_root=sim.root)
    [detail, *_] = next(c for c in diff["changes"] if c["change_type"] == "conflict")["details"]
    assert "Delete every directory that is not row 1005" in detail
    assert "remove the 'row_id' line" in detail


# ---------------------------------------------------------------------------
# Round 7: carriers of a tracked row id -- never deleted, never forgotten
# ---------------------------------------------------------------------------


def _rows_of_1000(sim: Sim) -> Path:
    return _dir(sim, f"{HTTP_DIR}/rows")


def _row_value(sim: Sim, key: tuple[str, str], row_id: str) -> str:
    row = next(r for r in sim.api.remote[PROD][key]["rows"] if r["id"] == row_id)
    return row["configuration"]["parameters"]["value"]


def _set_value(row_dir: Path, value: str) -> None:
    data = read_yaml(row_dir / "_config.yml")
    data["parameters"]["value"] = value
    write_yaml(row_dir / "_config.yml", data)


def test_ambiguous_carriers_of_a_tracked_row_never_delete_it(tmp_path: Path) -> None:
    """Round 7 P1 (Astra): original copied to a and b, original removed, push --force."""
    sim = _world(tmp_path)
    rows = _rows_of_1000(sim)
    shutil.copytree(rows / "row-tk2z", rows / "a")
    shutil.copytree(rows / "row-tk2z", rows / "b")
    shutil.rmtree(rows / "row-tk2z")

    changes = sim.svc.diff(alias="prod", project_root=sim.root)["changes"]
    assert not [c for c in changes if c["change_type"] == "deleted"]
    assert sorted(c["path"] for c in changes if c["change_type"] == "conflict") == [
        "rows/a",
        "rows/b",
    ]
    result = _push(sim, force=True)

    assert (result["created"], result["deleted"]) == (0, 0)
    assert [r["id"] for r in sim.api.remote[PROD][HTTP_1]["rows"]] == ["1001", "1002"]


def test_every_carrier_conflicts_also_an_edited_tracked_one(tmp_path: Path) -> None:
    """Round 7 P2 (Astra): edited tracked row + an untracked copy -- no update."""
    sim = _world(tmp_path)
    rows = _rows_of_1000(sim)
    _set_value(rows / "row-tk2z", "tk900z")
    shutil.copytree(rows / "row-tk2z", rows / "copy")

    changes = sim.svc.diff(alias="prod", project_root=sim.root)["changes"]
    row_changes = [c for c in changes if c.get("is_row")]
    assert sorted((c["change_type"], c["path"]) for c in row_changes) == [
        ("conflict", "rows/copy"),
        ("conflict", "rows/row-tk2z"),
    ]
    _push(sim)
    assert _row_value(sim, HTTP_1, "1001") == "tk3z"


def test_moved_row_dir_has_no_baseline_until_a_pull_records_it(tmp_path: Path) -> None:
    """Round 7: a moved, edited row is a conflict -- after a pull, an ordinary edit."""
    sim = _world(tmp_path)
    rows = _rows_of_1000(sim)
    shutil.move(str(rows / "row-tk2z"), str(rows / "moved"))
    _set_value(rows / "moved", "tk900z")

    assert ("conflict", "1000") in _changes(sim)
    _push(sim)
    assert _row_value(sim, HTTP_1, "1001") == "tk3z"

    _pull(sim)  # tracks the row at its new path, keeping the local edit
    assert ("modified", "1000") in _changes(sim)
    _push(sim)
    assert _row_value(sim, HTTP_1, "1001") == "tk900z"


@pytest.mark.parametrize("edited", [False, True], ids=["unedited", "edited"])
def test_pull_follows_a_moved_row_whose_remote_row_was_deleted(
    tmp_path: Path, edited: bool
) -> None:
    """Round 7 P1 (Astra): move, delete remotely, plain pull, push -- never POSTed."""
    sim = _world(tmp_path)
    rows = _rows_of_1000(sim)
    shutil.move(str(rows / "row-tk2z"), str(rows / "moved"))
    if edited:
        _set_value(rows / "moved", "tk900z")
    remote_rows = sim.api.remote[PROD][HTTP_1]["rows"]
    remote_rows[:] = [r for r in remote_rows if r["id"] != "1001"]

    _pull(sim)

    assert (rows / "moved").exists() is edited
    if edited:
        assert ("remote_deleted", "1000") in _changes(sim)
    assert _push(sim)["created"] == 0
    assert [r["id"] for r in sim.api.remote[PROD][HTTP_1]["rows"]] == ["1002"]


def test_pull_keeps_ambiguous_carriers_of_a_remotely_deleted_row(tmp_path: Path) -> None:
    """Round 7 P1: the id is never forgotten while dirs carry it."""
    sim = _world(tmp_path)
    rows = _rows_of_1000(sim)
    shutil.copytree(rows / "row-tk2z", rows / "a")
    shutil.copytree(rows / "row-tk2z", rows / "b")
    shutil.rmtree(rows / "row-tk2z")
    remote_rows = sim.api.remote[PROD][HTTP_1]["rows"]
    remote_rows[:] = [r for r in remote_rows if r["id"] != "1001"]

    _pull(sim)

    assert (rows / "a").exists() and (rows / "b").exists()
    (entry,) = [c for c in load_manifest(sim.root).configurations if c.id == "1000"]
    assert "1001" in {r.id for r in entry.rows}
    result = _push(sim, force=True)
    assert (result["created"], result["deleted"]) == (0, 0)
    assert [r["id"] for r in sim.api.remote[PROD][HTTP_1]["rows"]] == ["1002"]


# ---------------------------------------------------------------------------
# Round 8: a remote PARENT delete never erases row work
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("edited", [True, False], ids=["edited", "unedited"])
def test_remote_config_delete_and_a_moved_row(tmp_path: Path, edited: bool) -> None:
    """Round 8 P1 (Astra): rename rows/row-tk2z to rows/moved, edit it, delete 1000, pull."""
    sim = _world(tmp_path)
    rows = _rows_of_1000(sim)
    shutil.move(str(rows / "row-tk2z"), str(rows / "moved"))
    if edited:
        _set_value(rows / "moved", "tk900z")
    del sim.api.remote[PROD][HTTP_1]

    result = _pull(sim)

    config_dir = _dir(sim, HTTP_DIR)
    assert config_dir.exists() is edited
    if edited:
        assert (rows / "moved" / "_config.yml").read_text().count("tk900z") == 1
        assert any(
            d.get("action") == "skipped" and d.get("path") == HTTP_DIR for d in result["details"]
        )
        assert _push(sim)["created"] == 0
        assert HTTP_1 not in sim.api.remote[PROD]


def test_remote_config_delete_keeps_a_new_row_dir(tmp_path: Path) -> None:
    """Round 8 P1: a row dir no tracked row stands for is user content without a baseline."""
    sim = _world(tmp_path)
    rows = _rows_of_1000(sim)
    shutil.copytree(rows / "row-tk4z", rows / "new-row")
    data = read_yaml(rows / "new-row" / "_config.yml")
    data["name"] = "Row tk905z"
    data["_keboola"].pop("row_id")
    write_yaml(rows / "new-row" / "_config.yml", data)
    del sim.api.remote[PROD][HTTP_1]

    _pull(sim)

    assert (rows / "new-row" / "_config.yml").exists()
    assert _push(sim)["created"] == 0


# ---------------------------------------------------------------------------
# Round 9: the parent-delete guard sees every row layout and unknown bases
# ---------------------------------------------------------------------------


def _world_with_row_naming(tmp_path: Path, template: str) -> Sim:
    sim = Sim(tmp_path)
    sim.seed_remote()
    sim.svc.init_sync(alias="prod", project_root=sim.root)
    manifest = load_manifest(sim.root)
    manifest.naming.config_row = template
    save_manifest(sim.root, manifest)
    _pull(sim)
    return sim


@pytest.mark.parametrize(
    ("template", "row_dir"),
    [
        ("records/{config_row_name}", "records/row-tk2z"),  # Astra's input
        ("rows/group/{config_row_name}", "rows/group/row-tk2z"),  # nested
    ],
    ids=["records", "nested"],
)
def test_remote_config_delete_keeps_an_edited_row_in_a_custom_layout(
    tmp_path: Path, template: str, row_dir: str
) -> None:
    """Round 9 P1: the edited row is checked at its manifest path, whatever the naming."""
    sim = _world_with_row_naming(tmp_path, template)
    row = _dir(sim, f"{HTTP_DIR}/{row_dir}")
    assert (row / "_config.yml").exists()
    _set_value(row, "tk900z")
    del sim.api.remote[PROD][HTTP_1]

    _pull(sim)

    assert "tk900z" in (row / "_config.yml").read_text()


@pytest.mark.parametrize(
    ("template", "row_dir", "new_dir"),
    [
        ("rows/{config_row_name}", "rows/row-tk2z", "rows/moved"),  # Astra's input
        ("rows/group/{config_row_name}", "rows/group/row-tk2z", "rows/group/moved"),
    ],
    ids=["rows", "nested"],
)
def test_remote_config_delete_keeps_a_moved_unknown_base_row(
    tmp_path: Path, template: str, row_dir: str, new_dir: str
) -> None:
    """Round 9 P1: an adopted row (unknown base, empty pull_hash), moved, parent deleted."""
    sim = _world_with_row_naming(tmp_path, template)
    _set_value(_dir(sim, f"{HTTP_DIR}/{row_dir}"), "tk900z")
    manifest = load_manifest(sim.root)
    for entry in manifest.configurations:
        for row in entry.rows if entry.id == "1000" else []:
            if row.id == "1001":
                row.metadata = {"pull_hash": "", "pull_config_hash": "", "unknown_base": "adopted"}
    save_manifest(sim.root, manifest)
    moved = _dir(sim, f"{HTTP_DIR}/{new_dir}")
    shutil.move(str(_dir(sim, f"{HTTP_DIR}/{row_dir}")), str(moved))
    del sim.api.remote[PROD][HTTP_1]

    _pull(sim)

    assert "tk900z" in (moved / "_config.yml").read_text()


# ---------------------------------------------------------------------------
# Round 10: ambiguous carriers keep the parent; a nested config is not work
# ---------------------------------------------------------------------------


def test_remote_config_delete_keeps_ambiguous_carriers_of_a_row(tmp_path: Path) -> None:
    """Round 10 P1 (Astra): unchanged copies a and b of row 1001, original removed."""
    sim = _world(tmp_path)
    rows = _rows_of_1000(sim)
    shutil.copytree(rows / "row-tk2z", rows / "a")
    shutil.copytree(rows / "row-tk2z", rows / "b")
    shutil.rmtree(rows / "row-tk2z")
    del sim.api.remote[PROD][HTTP_1]

    result = _pull(sim)

    assert (rows / "a").exists() and (rows / "b").exists()
    assert any(
        d.get("action") == "skipped" and d.get("path") == HTTP_DIR for d in result["details"]
    )


def _nest_config_b_in_a(sim: Sim) -> Path:
    """Astra's A/B input: config 1003 (B) at ``<A>/child`` inside config 1000 (A)."""
    child = _dir(sim, f"{HTTP_DIR}/child")
    shutil.move(str(_dir(sim, HTTP_2_DIR)), str(child))
    manifest = load_manifest(sim.root)
    for entry in manifest.configurations:
        if entry.id == "1003":
            entry.path = f"{HTTP_DIR}/child"
    save_manifest(sim.root, manifest)
    del sim.api.remote[PROD][HTTP_1]
    del sim.api.remote[PROD][HTTP_2]
    return child


@pytest.mark.parametrize("force", [False, True], ids=["pull", "pull-force"])
def test_a_nested_config_is_not_work_of_its_outer_config(tmp_path: Path, force: bool) -> None:
    """Round 10 P2: both clean and deleted remotely -- both swept, no conflict."""
    sim = _world(tmp_path)
    child = _nest_config_b_in_a(sim)

    result = _pull(sim, force=force)  # --force must not raise a false conflict

    assert not child.exists()
    assert not _dir(sim, HTTP_DIR).exists()
    removed = {d["path"] for d in result["details"] if d.get("action") == "removed"}
    assert {HTTP_DIR, f"{HTTP_DIR}/child"} <= removed


def test_a_genuine_untracked_row_next_to_a_nested_config_keeps_the_outer(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    child = _nest_config_b_in_a(sim)
    new_row = _rows_of_1000(sim) / "new-row"
    shutil.copytree(_rows_of_1000(sim) / "row-tk4z", new_row)
    data = read_yaml(new_row / "_config.yml")
    data["_keboola"].pop("row_id")
    write_yaml(new_row / "_config.yml", data)

    _pull(sim)

    assert (new_row / "_config.yml").exists()  # A kept for its new row
    assert not child.exists()  # B, clean, swept


def _edit_b(child: Path) -> None:
    data = read_yaml(child / "_config.yml")
    data["parameters"]["value"] = "tk900z"
    write_yaml(child / "_config.yml", data)


def test_an_outer_config_holding_a_kept_nested_one_is_kept_whole(tmp_path: Path) -> None:
    """Round 11: A (clean, deleted) holds B (edited, kept): A is kept and reported."""
    sim = _world(tmp_path)
    child = _nest_config_b_in_a(sim)
    _edit_b(child)

    result = _pull(sim)

    assert "tk900z" in (child / "_config.yml").read_text()
    assert (_dir(sim, HTTP_DIR) / "_config.yml").exists()
    [kept] = [d for d in result["details"] if d.get("path") == HTTP_DIR]
    assert kept["action"] == "skipped"
    assert f"{HTTP_DIR}/child" in kept["reason"]
    assert "1000" in {c.id for c in load_manifest(sim.root).configurations}

    shutil.rmtree(child)  # the user drops B; the next pull removes both
    _pull(sim)
    assert not _dir(sim, HTTP_DIR).exists()


def test_containment_is_judged_by_the_filesystem_not_the_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 11: on a case-insensitive filesystem ``Extractor/a`` IS ``extractor/a``."""
    from keboola_agent_cli.services import _sync_stale

    def case_insensitive(a: Any, b: Any) -> bool:
        return str(a).casefold() == str(b).casefold()

    monkeypatch.setattr(_sync_stale.os.path, "samefile", case_insensitive)
    outer = Path("/tree/main/extractor/keboola.ex-http/a")
    assert _sync_stale.contains(outer, Path("/tree/main/Extractor/keboola.ex-http/a/child"))
    assert not _sync_stale.contains(outer, Path("/tree/main/extractor/keboola.ex-http/ab"))
    assert _sync_stale.holds(outer, Path("/tree/main/Extractor/keboola.ex-http/a/child"))
    assert not _sync_stale.holds(outer, Path("/tree/main/extractor/keboola.ex-http/ab"))


def _case_insensitive(path: Path) -> bool:
    probe = path / "CaseProbe"
    probe.mkdir()
    try:
        return (path / "caseprobe").exists()
    finally:
        probe.rmdir()


def test_a_nested_config_spelled_in_another_case_keeps_its_outer(tmp_path: Path) -> None:
    """Round 11: the real filesystem variant -- only where it is case-insensitive."""
    if not _case_insensitive(tmp_path):
        pytest.skip("case-sensitive filesystem")
    sim = _world(tmp_path)
    child = _nest_config_b_in_a(sim)
    _edit_b(child)
    manifest = load_manifest(sim.root)
    for entry in manifest.configurations:
        if entry.id == "1003":
            entry.path = f"{HTTP_DIR}/CHILD"
    save_manifest(sim.root, manifest)

    _pull(sim)

    assert "tk900z" in (child / "_config.yml").read_text()


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symbolic links here")


def test_a_symlinked_outer_config_dir_deletes_nothing_in_its_target(tmp_path: Path) -> None:
    """Round 11 P1 (Astra): the config dir is a symlink; the remote deletes the config."""
    sim = _world(tmp_path)
    config_dir = _dir(sim, HTTP_DIR)
    target = tmp_path / "elsewhere"
    shutil.move(str(config_dir), str(target))
    _symlink_or_skip(config_dir, target)
    before = sorted(p.relative_to(target) for p in target.rglob("*"))
    del sim.api.remote[PROD][HTTP_1]

    result = _pull(sim)

    assert config_dir.is_symlink()
    assert sorted(p.relative_to(target) for p in target.rglob("*")) == before
    [kept] = [d for d in result["details"] if d.get("path") == HTTP_DIR]
    assert kept["action"] == "skipped"


def test_a_kept_nested_config_that_is_a_symlink_is_never_unlinked(tmp_path: Path) -> None:
    """Round 11: B is a symlink inside A; B is kept (edited), so A is kept whole."""
    sim = _world(tmp_path)
    child = _nest_config_b_in_a(sim)
    target = tmp_path / "b-elsewhere"
    shutil.move(str(child), str(target))
    _symlink_or_skip(child, target)
    _edit_b(target)

    _pull(sim)

    assert child.is_symlink()
    assert "tk900z" in (target / "_config.yml").read_text()
    assert (_dir(sim, HTTP_DIR) / "_config.yml").exists()


# ---------------------------------------------------------------------------
# Round 12: two-pass sweep; no ownership decided through links
# ---------------------------------------------------------------------------


def test_a_config_reached_through_an_alias_link_keeps_its_real_parent(tmp_path: Path) -> None:
    """Round 12 P1 (Astra): B's manifest path is ``alias -> <A>/child``, B edited."""
    sim = _world(tmp_path)
    child = _dir(sim, f"{HTTP_DIR}/child")
    shutil.move(str(_dir(sim, HTTP_2_DIR)), str(child))
    alias = _dir(sim, "extractor/keboola.ex-http/alias")
    _symlink_or_skip(alias, child)
    manifest = load_manifest(sim.root)
    for entry in manifest.configurations:
        if entry.id == "1003":
            entry.path = "extractor/keboola.ex-http/alias"
    save_manifest(sim.root, manifest)
    _edit_b(child)
    del sim.api.remote[PROD][HTTP_1]
    del sim.api.remote[PROD][HTTP_2]

    result = _pull(sim)

    assert "tk900z" in (child / "_config.yml").read_text()
    assert (_dir(sim, HTTP_DIR) / "_config.yml").exists()
    kept = {d["path"] for d in result["details"] if d.get("action") == "skipped"}
    assert HTTP_DIR in kept


def test_a_symlinked_nested_config_first_in_the_manifest_survives_theirs(tmp_path: Path) -> None:
    """Round 12 P2 (Astra): B (a link under A) listed before A, ``pull --theirs``."""
    sim = _world(tmp_path)
    child = _nest_config_b_in_a(sim)
    target = tmp_path / "b-elsewhere"
    shutil.move(str(child), str(target))
    _symlink_or_skip(child, target)
    manifest = load_manifest(sim.root)
    manifest.configurations.sort(key=lambda c: c.id != "1003")  # B before A
    save_manifest(sim.root, manifest)

    result = _pull(sim, theirs=True)

    assert child.is_symlink() and (target / "_config.yml").exists()
    assert (_dir(sim, HTTP_DIR) / "_config.yml").exists()
    skipped = {d["path"] for d in result["details"] if d.get("action") == "skipped"}
    assert {HTTP_DIR, f"{HTTP_DIR}/child"} <= skipped
    ids = {c.id for c in load_manifest(sim.root).configurations}
    assert {"1000", "1003"} <= ids  # the manifest matches what stays on disk


def test_any_symlink_inside_a_clean_stale_config_keeps_it(tmp_path: Path) -> None:
    """Round 12: kbagent does not decide ownership through links."""
    sim = _world(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("mine", encoding="utf-8")
    _symlink_or_skip(_rows_of_1000(sim) / "link", outside)
    del sim.api.remote[PROD][HTTP_1]

    result = _pull(sim)

    assert (_dir(sim, HTTP_DIR) / "_config.yml").exists()
    assert (outside / "keep.txt").read_text() == "mine"
    [kept] = [d for d in result["details"] if d.get("path") == HTTP_DIR]
    assert kept["action"] == "skipped"
