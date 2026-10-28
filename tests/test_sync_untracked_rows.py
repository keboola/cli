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

from keboola_agent_cli.errors import ErrorCode, KeboolaApiError, SyncConflictError
from keboola_agent_cli.services import sync_service as sync_service_module
from keboola_agent_cli.sync.config_format import api_config_to_local
from keboola_agent_cli.sync.diff_engine import config_hash
from keboola_agent_cli.sync.manifest import load_manifest, save_manifest
from test_sync_bookkeeping_differential import VARIANTS
from test_sync_bookkeeping_differential import World as DiffWorld
from test_sync_clone_links import EXTRACTOR, FLOW, PY, SHARED, SQL, VARS
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


# ---------------------------------------------------------------------------
# Round 13: a staying config that IS the stale dir (an alias) protects it
# ---------------------------------------------------------------------------


def _b_is_an_alias_of_a(sim: Sim) -> Path:
    """Astra's input: A at ``a`` (deleted remotely); B at ``b -> a``; ``a/`` holds B's files."""
    a_dir = _dir(sim, HTTP_DIR)
    shutil.rmtree(a_dir)
    shutil.move(str(_dir(sim, HTTP_2_DIR)), str(a_dir))
    _symlink_or_skip(_dir(sim, HTTP_2_DIR), a_dir)
    del sim.api.remote[PROD][HTTP_1]
    return a_dir


def test_a_live_alias_of_the_stale_dir_keeps_it(tmp_path: Path) -> None:
    """Round 13 P1: B is still on the remote, ``pull --theirs``."""
    sim = _world(tmp_path)
    a_dir = _b_is_an_alias_of_a(sim)
    before = (a_dir / "_config.yml").read_text()

    _pull(sim, theirs=True)

    assert (a_dir / "_config.yml").read_text() == before  # B's files survive


def test_a_kept_alias_of_the_stale_dir_keeps_it(tmp_path: Path) -> None:
    """Round 13 P1: B is deleted remotely too, but kept (edited, a link) -- ``--theirs``
    keeps nothing for being edited, so only the alias rule protects ``a``."""
    sim = _world(tmp_path)
    a_dir = _b_is_an_alias_of_a(sim)
    _edit_b(a_dir)
    del sim.api.remote[PROD][HTTP_2]

    result = _pull(sim, theirs=True)

    assert "tk900z" in (a_dir / "_config.yml").read_text()
    skipped = {d["path"] for d in result["details"] if d.get("action") == "skipped"}
    assert {HTTP_DIR, HTTP_2_DIR} <= skipped


def test_holds_treats_the_same_directory_as_held(tmp_path: Path) -> None:
    from keboola_agent_cli.services import _sync_stale

    a_dir = tmp_path / "a"
    a_dir.mkdir()
    _symlink_or_skip(tmp_path / "b", a_dir)
    assert _sync_stale.holds(a_dir, tmp_path / "b")
    assert _sync_stale.holds(a_dir, a_dir)


# ---------------------------------------------------------------------------
# Devin round on PR #931
# ---------------------------------------------------------------------------


def _crash_once_before_phase_d(monkeypatch: pytest.MonkeyPatch) -> None:
    original = sync_service_module.resolve_run_target_bindings
    state = {"crashed": False}

    def dies_once(*args: Any, **kwargs: Any) -> Any:
        if not state["crashed"]:
            state["crashed"] = True
            raise Crash()
        return original(*args, **kwargs)

    monkeypatch.setattr(sync_service_module, "resolve_run_target_bindings", dies_once)


def test_pending_flow_never_points_at_a_row_deleted_on_the_remote(
    tmp_path: Path, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Devin 4400587856: a created row deleted remotely since is no resolved target."""
    world = CloneWorld(tmp_path, tmp_config_dir)
    _crash_once_before_phase_d(monkeypatch)
    with pytest.raises(Crash):
        world.clone()
    dead_id = _orders_row_id(world)
    rows = world.target(EXTRACTOR)["rows"]
    rows[:] = [r for r in rows if r["id"] != dead_id]

    second = world.clone()

    assert dead_id not in _flow_task(world, 14).get("configRowIds", [])
    assert _flow_task(world, 14)["configRowIds"] == ["exrow-1"]
    assert any(e["error_code"] == ErrorCode.LINK_UNRESOLVED for e in second["errors"])
    assert _flow_entry_metadata(world).get("link_pending") is True


def test_force_pull_preflight_finds_a_moved_row_edited_on_both_sides(tmp_path: Path) -> None:
    """Devin 4400588073: the preflight follows a moved row by id, like pull itself."""
    sim = _world(tmp_path)
    rows = _rows_of_1000(sim)
    shutil.move(str(rows / "row-tk2z"), str(rows / "moved"))
    _set_value(rows / "moved", "tk900z")
    remote_row = next(r for r in sim.api.remote[PROD][HTTP_1]["rows"] if r["id"] == "1001")
    remote_row["configuration"]["parameters"]["value"] = "tk901z"

    with pytest.raises(SyncConflictError):
        _pull(sim, force=True)

    assert read_yaml(rows / "moved" / "_config.yml")["parameters"]["value"] == "tk900z"


def _entry_metadata(world: CloneWorld, component_id: str, name: str) -> dict[str, Any]:
    config_id = world.target(component_id, name)["id"]
    (entry,) = [
        c
        for c in load_manifest(world.clone_dir).configurations
        if c.component_id == component_id and c.id == config_id
    ]
    return entry.metadata


def test_variables_link_after_a_failed_values_row_is_finished_by_the_next_push(
    tmp_path: Path, tmp_config_dir: Path
) -> None:
    """Devin 4400588270: the transformation stays pending until the values row exists."""
    world = CloneWorld(tmp_path, tmp_config_dir)
    world.tgt_api.fail_row_names.add("Default values")

    first = world.clone()
    assert any(e["change_type"] == "variable_link" for e in first["errors"])
    assert world.target(SQL, "SQL vars only")["configuration"]["variables_values_id"] == "vrow-ref"
    assert _entry_metadata(world, SQL, "SQL vars only").get("link_pending") is True

    world.tgt_api.fail_row_names.clear()
    second = world.clone()

    variables = world.target(VARS)
    (values_row,) = variables["rows"]
    for component_id, name in ((SQL, "SQL vars only"), (SQL, "SQL step"), (PY, "Py step")):
        configuration = world.target(component_id, name)["configuration"]
        assert configuration["variables_id"] == variables["id"]
        assert configuration["variables_values_id"] == values_row["id"]
        assert "link_pending" not in _entry_metadata(world, component_id, name)
    assert second["errors"] == []
    assert world.clone()["status"] == "no_changes"


def test_shared_code_link_after_a_failed_row_is_finished_by_the_next_push(
    tmp_path: Path, tmp_config_dir: Path
) -> None:
    """Devin 4400588270: shared-code rows are finished the same way."""
    world = CloneWorld(tmp_path, tmp_config_dir)
    world.tgt_api.fail_row_names.add("helpers")

    first = world.clone()
    assert any(e["change_type"] == "shared_code_link" for e in first["errors"])
    assert _entry_metadata(world, PY, "Py step").get("link_pending") is True

    world.tgt_api.fail_row_names.clear()
    second = world.clone()

    shared = world.target(SHARED, "Shared python")
    (row,) = shared["rows"]
    configuration = world.target(PY, "Py step")["configuration"]
    assert configuration["shared_code_id"] == shared["id"]
    assert configuration["shared_code_row_ids"] == [row["id"]]
    assert f"{{{{{row['id']}}}}}" in str(configuration["parameters"])
    assert "link_pending" not in _entry_metadata(world, PY, "Py step")
    assert second["errors"] == []
    assert world.clone()["status"] == "no_changes"


def test_a_fully_bound_clone_leaves_no_transformation_pending(
    tmp_path: Path, tmp_config_dir: Path
) -> None:
    """Devin 4400588270: a fully bound clone leaves no transformation pending."""
    world = CloneWorld(tmp_path, tmp_config_dir)
    assert world.clone()["errors"] == []
    for component_id, name in ((SQL, "SQL vars only"), (SQL, "SQL step"), (PY, "Py step")):
        assert "link_pending" not in _entry_metadata(world, component_id, name)
    assert world.clone()["status"] == "no_changes"


# ---------------------------------------------------------------------------
# Astra on 5da2a090: recovery starts from the remote (#929 D32)
# ---------------------------------------------------------------------------


def test_pending_transformation_keeps_a_link_edited_on_the_remote(
    tmp_path: Path, tmp_config_dir: Path
) -> None:
    """Astra P1: a link changed in the UI before the retry is not a source id -- it survives."""
    world = CloneWorld(tmp_path, tmp_config_dir)
    world.tgt_api.fail_row_names.add("Default values")
    world.clone()
    remote = world.target(SQL, "SQL vars only")["configuration"]
    remote["variables_id"] = "ui-vars"
    remote["variables_values_id"] = "ui-row"
    world.tgt_api.fail_row_names.clear()

    world.clone()

    configuration = world.target(SQL, "SQL vars only")["configuration"]
    assert configuration["variables_id"] == "ui-vars"
    assert configuration["variables_values_id"] == "ui-row"
    assert "link_pending" not in _entry_metadata(world, SQL, "SQL vars only")
    # The transformations nobody edited are still finished.
    (values_row,) = world.target(VARS)["rows"]
    assert world.target(PY, "Py step")["configuration"]["variables_values_id"] == values_row["id"]


def test_pending_flow_never_writes_a_dead_row_only_its_remote_names(
    tmp_path: Path, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra P1: local task names A, remote names B, B's mapped row is deleted -- never written."""
    world = CloneWorld(tmp_path, tmp_config_dir)
    _crash_once_before_phase_d(monkeypatch)
    with pytest.raises(Crash):
        world.clone()
    variables = world.target(VARS)
    (dead_row,) = variables["rows"]
    variables["rows"] = []
    remote_task = next(t for t in world.target(FLOW)["configuration"]["tasks"] if t["id"] == 14)
    remote_task["task"].update(
        {"componentId": VARS, "configId": "var-ref", "configRowIds": ["vrow-ref"]}
    )

    second = world.clone()

    assert dead_row["id"] not in str(world.target(FLOW)["configuration"])
    assert _flow_task(world, 14)["configRowIds"] == ["vrow-ref"]
    assert any(e["error_code"] == ErrorCode.LINK_UNRESOLVED for e in second["errors"])
    assert _flow_entry_metadata(world).get("link_pending") is True


def test_unreadable_link_target_is_never_written(
    tmp_path: Path, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra P1: a 503 on the target read is no proof it exists -- no rewrite, still pending."""
    world = CloneWorld(tmp_path, tmp_config_dir)
    _crash_once_before_phase_d(monkeypatch)
    with pytest.raises(Crash):
        world.clone()
    # The clients are MagicMock(wraps=...) proxies that bind the fake's methods on first
    # use, so the fault is injected through the fake's state, never by patching a method.
    world.tgt_api.unreadable_components.add(EXTRACTOR)

    second = world.clone()

    assert _flow_task(world, 14)["configId"] == "ex-ref"
    assert _flow_task(world, 14)["configRowIds"] == ["exrow-1"]
    (error,) = second["errors"]  # the failed read is the push's one error for it
    assert (error["change_type"], error["error_code"]) == ("link_target", ErrorCode.API_ERROR)
    assert _flow_entry_metadata(world).get("link_pending") is True


def test_link_repaired_in_the_ui_clears_pending_without_a_write(
    tmp_path: Path, tmp_config_dir: Path
) -> None:
    """Astra P2: the remote names a live row now -- pending clears, nothing is PUT."""
    world = CloneWorld(tmp_path, tmp_config_dir)
    world.tgt_api.fail_row_names.add("Default values")
    world.clone()
    variables = world.target(VARS)
    variables["rows"].append(
        {"id": "live-row", "name": "UI values", "description": "", "configuration": {"values": []}}
    )
    transformation = world.target(SQL, "SQL vars only")
    transformation["configuration"]["variables_id"] = variables["id"]
    transformation["configuration"]["variables_values_id"] = "live-row"
    world.tgt_api.update_calls.clear()

    world.clone()

    assert [c for c in world.tgt_api.update_calls if c["config_id"] == transformation["id"]] == []
    assert world.target(SQL, "SQL vars only")["configuration"]["variables_values_id"] == "live-row"
    assert "link_pending" not in _entry_metadata(world, SQL, "SQL vars only")


# ---------------------------------------------------------------------------
# Astra on 531fffeb
# ---------------------------------------------------------------------------


def test_a_live_row_selected_in_the_ui_is_never_remapped(
    tmp_path: Path, tmp_config_dir: Path
) -> None:
    """Astra P1: B lists both "r" and "new-r", (B, "r") -> "new-r" is mapped, the remote selects "r"."""
    world = CloneWorld(tmp_path, tmp_config_dir)
    world.tgt_api.fail_row_names.add("Default values")
    world.clone()
    remote = world.target(SQL, "SQL vars only")["configuration"]
    remote["variables_id"] = world.target(VARS)["id"]
    remote["variables_values_id"] = "vrow-ref"
    # A row with the id "vrow-ref" is added in the UI right after the push
    # creates "Default values" -- the row the mapping (B, "vrow-ref") names.
    world.tgt_api.rows_added_with["Default values"] = {
        "id": "vrow-ref",
        "name": "UI values",
        "description": "",
        "configuration": {"values": []},
    }
    world.tgt_api.fail_row_names.clear()

    world.clone()

    assert len(world.target(VARS)["rows"]) == 2
    assert world.target(SQL, "SQL vars only")["configuration"]["variables_values_id"] == "vrow-ref"
    assert "link_pending" not in _entry_metadata(world, SQL, "SQL vars only")


def test_a_flow_selects_the_row_of_a_new_untracked_config_by_its_placeholder(
    tmp_path: Path,
) -> None:
    """Astra P1: new dir "ex-ref" with row "orders-ref", a new flow selects ex-ref/["orders-ref"].

    The copied row id of the same new dir is scoped to it: a flow selecting
    the ORIGINAL config's row by that id keeps it (S2).
    """
    variant = VARIANTS["rows"]
    w = DiffWorld(tmp_path)
    cid = w.api.seed(variant)
    w.api.types["keboola.flow"] = "other"
    w.init()
    w.pull()
    authored = _copy_config(w, cid, "Authored")
    data = read_yaml(authored / "_config.yml")
    data["_keboola"]["config_id"] = "ex-ref"
    write_yaml(authored / "_config.yml", data)
    first_row, copied_row = sorted(authored.glob("rows/*/_config.yml"))[:2]
    row = read_yaml(first_row)
    row["_keboola"]["row_id"] = "orders-ref"
    write_yaml(first_row, row)
    copied_id = str(read_yaml(copied_row)["_keboola"]["row_id"])
    version = read_yaml(w.config_dir(cid) / "_config.yml")["version"]
    _write_flow(w, "Nightly", _job(variant.component_id, "ex-ref", ["orders-ref"]), version)
    _write_flow(w, "Daily", _job(variant.component_id, cid, [copied_id]), version)

    w.push()

    (created,) = [c for c in w.api.remote[PROD].values() if c["name"] == "Authored"]
    flows = {
        c["name"]: c["configuration"]["tasks"][0]["task"]
        for (comp, _), c in w.api.remote[PROD].items()
        if comp == "keboola.flow"
    }
    nightly, daily = flows["Nightly"], flows["Daily"]
    assert (nightly["configId"], nightly["configRowIds"]) == (
        created["id"],
        [_by_name(created, row["name"])],
    )
    assert (daily["configId"], daily["configRowIds"]) == (cid, [copied_id])


def test_an_unreadable_variables_target_is_one_error_per_push(
    tmp_path: Path, tmp_config_dir: Path
) -> None:
    """Astra P2: three pending transformations name a variables config that answers 503."""
    world = CloneWorld(tmp_path, tmp_config_dir)
    world.tgt_api.fail_row_names.add("Default values")
    world.clone()
    world.tgt_api.unreadable_components.add(VARS)

    second = world.clone()

    link_errors = [
        e for e in second["errors"] if e["change_type"] in ("link_target", "variable_link")
    ]
    (error,) = link_errors
    assert (error["change_type"], error["config_id"]) == ("link_target", world.target(VARS)["id"])
    for component_id, name in ((SQL, "SQL vars only"), (SQL, "SQL step"), (PY, "Py step")):
        assert _entry_metadata(world, component_id, name).get("link_pending") is True


# ---------------------------------------------------------------------------
# Astra on 2902addd: untracked placeholders
# ---------------------------------------------------------------------------


def _seed_variables(w: DiffWorld, config_id: str, row_names: list[str]) -> None:
    w.api.types[VARS] = "other"
    w.api.remote[PROD][(VARS, config_id)] = {
        "id": config_id,
        "name": f"Vars {config_id}",
        "description": "",
        "configuration": {"variables": [{"name": "x", "type": "string"}]},
        "rows": [
            {
                "id": f"{config_id}-{i}" if i else "existing-prod-row",
                "name": name,
                "description": "",
                "configuration": {"values": [{"name": "x", "value": str(i)}]},
                "isDisabled": False,
            }
            for i, name in enumerate(row_names)
        ],
        "isDisabled": False,
    }


def _new_config_from(
    w: DiffWorld, config_id: str, name: str, file_id: str, row_ids: list[str] | None = None
) -> Path:
    """A new untracked dir copied from a pulled config: its own name, file id and row ids."""
    src = w.config_dir(config_id)
    dst = src.parent / name.lower().replace(" ", "-")
    shutil.copytree(src, dst)
    data = read_yaml(dst / "_config.yml")
    data["name"] = name
    if file_id:
        data["_keboola"]["config_id"] = file_id
    else:
        data["_keboola"].pop("config_id", None)
    write_yaml(dst / "_config.yml", data)
    for row_file, row_id in zip(
        sorted(dst.glob("rows/*/_config.yml")), row_ids or [], strict=False
    ):
        row = read_yaml(row_file)
        row["_keboola"]["row_id"] = row_id
        write_yaml(row_file, row)
    return dst


def _new_transformation(w: DiffWorld, template_id: str, links: dict[str, str]) -> None:
    tx = _new_config_from(w, template_id, "New step", "")
    data = read_yaml(tx / "_config.yml")
    data.setdefault("_configuration_extra", {}).update(links)
    write_yaml(tx / "_config.yml", data)


def _remote_named(w: DiffWorld, name: str) -> dict[str, Any]:
    (config,) = [c for c in w.api.remote[PROD].values() if c["name"] == name]
    return config


def test_a_new_transformation_keeps_its_link_to_an_existing_variables_config(
    tmp_path: Path,
) -> None:
    """Astra P1: existing-prod-vars/-row stay; an unrelated new-vars-ref is no fallback target."""
    w = DiffWorld(tmp_path)
    _seed_variables(w, "existing-prod-vars", ["Default"])
    sql_id = w.api.seed(VARIANTS["sql"], name="Template")
    w.init()
    w.pull()
    _new_config_from(w, "existing-prod-vars", "Unrelated vars", "new-vars-ref", ["new-row-ref"])
    _new_transformation(
        w,
        sql_id,
        {"variables_id": "existing-prod-vars", "variables_values_id": "existing-prod-row"},
    )

    w.push()

    configuration = _remote_named(w, "New step")["configuration"]
    assert configuration["variables_id"] == "existing-prod-vars"
    assert configuration["variables_values_id"] == "existing-prod-row"


def test_a_copy_never_takes_the_placeholder_of_a_created_config(tmp_path: Path) -> None:
    """Astra P1: "ex-ref" was created as the original; a copy still carrying it is no placeholder."""
    w = DiffWorld(tmp_path)
    cid = w.api.seed(VARIANTS["extractor"])
    w.api.types["keboola.flow"] = "other"
    w.init()
    w.pull()
    original = _new_config_from(w, cid, "Original", "ex-ref")
    held = tmp_path / "held-copy"
    shutil.copytree(original, held)  # the copy, taken before the original is created
    w.push()
    shutil.copytree(held, original.parent / "copy")
    data = read_yaml(original.parent / "copy" / "_config.yml")
    data["name"] = "Copy"
    write_yaml(original.parent / "copy" / "_config.yml", data)
    version = read_yaml(w.config_dir(cid) / "_config.yml")["version"]
    _write_flow(w, "Nightly", _job("keboola.ex-http", "ex-ref", []), version)

    w.push()

    (flow,) = [c for (comp, _), c in w.api.remote[PROD].items() if comp == "keboola.flow"]
    assert (
        flow["configuration"]["tasks"][0]["task"]["configId"] == _remote_named(w, "Original")["id"]
    )


def test_two_new_dirs_with_one_free_id_link_neither(tmp_path: Path) -> None:
    """Astra P1: two new dirs claim "dup-ref" -- neither maps, and the push says so."""
    w = DiffWorld(tmp_path)
    cid = w.api.seed(VARIANTS["extractor"])
    w.api.types["keboola.flow"] = "other"
    w.init()
    w.pull()
    _new_config_from(w, cid, "First", "dup-ref")
    _new_config_from(w, cid, "Second", "dup-ref")
    version = read_yaml(w.config_dir(cid) / "_config.yml")["version"]
    _write_flow(w, "Nightly", _job("keboola.ex-http", "dup-ref", []), version)

    result = w.push()

    (flow,) = [c for (comp, _), c in w.api.remote[PROD].items() if comp == "keboola.flow"]
    assert flow["configuration"]["tasks"][0]["task"]["configId"] == "dup-ref"
    ambiguous = [x for x in result.get("warnings", []) if x.get("code") == "PLACEHOLDER_AMBIGUOUS"]
    assert len(ambiguous) == 2
    for name in ("First", "Second"):
        new_id = _remote_named(w, name)["id"]
        (entry,) = [c for c in load_manifest(w.root).configurations if c.id == new_id]
        assert "created_from_id" not in entry.metadata


def test_a_new_transformation_selects_one_of_two_rows_of_a_new_variables_config(
    tmp_path: Path,
) -> None:
    """Astra P1: new-vars-ref has row-one-ref and row-two-ref; the transformation picks row two."""
    w = DiffWorld(tmp_path)
    _seed_variables(w, "existing-prod-vars", ["Row one", "Row two"])
    sql_id = w.api.seed(VARIANTS["sql"], name="Template")
    w.init()
    w.pull()
    _new_config_from(
        w, "existing-prod-vars", "New vars", "new-vars-ref", ["row-one-ref", "row-two-ref"]
    )
    _new_transformation(
        w, sql_id, {"variables_id": "new-vars-ref", "variables_values_id": "row-two-ref"}
    )

    w.push()

    new_vars = _remote_named(w, "New vars")
    configuration = _remote_named(w, "New step")["configuration"]
    assert configuration["variables_id"] == new_vars["id"]
    assert configuration["variables_values_id"] == _by_name(new_vars, "Row two")
    entry = next(
        c
        for c in load_manifest(w.root).configurations
        if c.id == _remote_named(w, "New step")["id"]
    )
    assert "link_pending" not in entry.metadata


def test_a_mapped_row_of_another_parent_does_not_resolve_a_link() -> None:
    """Astra P2: A has only row-a, B's mapped row is row-b; A/row-b stays unresolved."""
    from keboola_agent_cli.services._sync_link_pending import LinkMaps
    from keboola_agent_cli.services._sync_link_recovery import resolve_links

    maps = LinkMaps(
        rows={("B", "src-b"): "row-b"},
        target_rows={(VARS, "A"): {"row-a"}, (VARS, "B"): {"row-b"}},
    )

    links = resolve_links({"variables_id": "A", "variables_values_id": "row-b"}, maps)

    assert links.pending
    assert links.variables_values_id is None


# ---------------------------------------------------------------------------
# Astra on 36c758bd: claims decide, also for failed creates and rows
# ---------------------------------------------------------------------------


def _with_second_variables(components: list[dict[str, Any]]) -> None:
    from test_sync_clone_links import _component_entry, _config, _row

    _component_entry(components, VARS)["configurations"].append(
        _config(
            "vars-B-ref",
            "Other variables",
            {"variables": [{"name": "other", "type": "string"}]},
            rows=[
                _row("vrow-B-ref", "Other values", {"values": [{"name": "other", "value": "1"}]})
            ],
        )
    )


def test_a_failed_variables_create_never_redirects_its_transformation(
    tmp_path: Path, tmp_config_dir: Path
) -> None:
    """Astra P1: A ("var-ref") fails, B is created -- T keeps naming A, the retry binds it."""
    world = CloneWorld(tmp_path, tmp_config_dir, reference=_with_second_variables)
    world.tgt_api.fail_config_names.add("Variables")

    world.clone()

    configuration = world.target(SQL, "SQL vars only")["configuration"]
    assert configuration["variables_id"] == "var-ref"
    assert _entry_metadata(world, SQL, "SQL vars only").get("link_pending") is True

    world.tgt_api.fail_config_names.clear()
    world.clone()

    variables = world.target(VARS, "Variables")
    configuration = world.target(SQL, "SQL vars only")["configuration"]
    assert configuration["variables_id"] == variables["id"]
    assert configuration["variables_values_id"] == variables["rows"][0]["id"]


def test_a_restored_row_copy_never_takes_a_created_rows_placeholder(tmp_path: Path) -> None:
    """Astra P1: R1 was created from "row-ref"; a copy still carrying it is no placeholder."""
    w = DiffWorld(tmp_path)
    _seed_variables(w, "existing-prod-vars", ["Default"])
    sql_id = w.api.seed(VARIANTS["sql"], name="Template")
    w.init()
    w.pull()
    rows = w.config_dir("existing-prod-vars") / "rows"
    (template,) = sorted(rows.glob("*/_config.yml"))
    row = read_yaml(template)
    row["name"] = "First"
    row["_keboola"]["row_id"] = "row-ref"
    (rows / "first").mkdir()
    write_yaml(rows / "first" / "_config.yml", row)
    held = tmp_path / "held-row"
    shutil.copytree(rows / "first", held)  # saved before the row is created
    w.push()
    (first,) = [
        r for r in w.api.get(PROD, VARS, "existing-prod-vars")["rows"] if r["name"] == "First"
    ]
    shutil.copytree(held, rows / "restored")
    copy_row = read_yaml(rows / "restored" / "_config.yml")
    copy_row["name"] = "Restored"
    copy_row["values"] = [{"name": "x", "value": "copy"}]
    write_yaml(rows / "restored" / "_config.yml", copy_row)
    _new_transformation(
        w, sql_id, {"variables_id": "existing-prod-vars", "variables_values_id": "row-ref"}
    )

    w.push()

    assert _remote_named(w, "New step")["configuration"]["variables_values_id"] == first["id"]


def test_two_new_rows_with_one_placeholder_map_neither(tmp_path: Path) -> None:
    """Astra P1: two rows of a new config carry "dup-row" -- neither maps, the push says so."""
    w = DiffWorld(tmp_path)
    _seed_variables(w, "existing-prod-vars", ["Row one", "Row two"])
    sql_id = w.api.seed(VARIANTS["sql"], name="Template")
    w.init()
    w.pull()
    _new_config_from(w, "existing-prod-vars", "New vars", "new-vars-ref", ["dup-row", "dup-row"])
    _new_transformation(
        w, sql_id, {"variables_id": "new-vars-ref", "variables_values_id": "dup-row"}
    )

    result = w.svc.push(alias="prod", project_root=w.root)

    configuration = _remote_named(w, "New step")["configuration"]
    assert configuration["variables_values_id"] == "dup-row"
    ambiguous = [x for x in result.get("warnings", []) if x.get("code") == "PLACEHOLDER_AMBIGUOUS"]
    assert len(ambiguous) == 2
    new_vars = _remote_named(w, "New vars")
    (entry,) = [c for c in load_manifest(w.root).configurations if c.id == new_vars["id"]]
    assert all("created_from_id" not in (r.metadata or {}) for r in entry.rows)


# ---------------------------------------------------------------------------
# Astra on fb5fea19: failed creates keep their claims
# ---------------------------------------------------------------------------


def _entry_of(w: DiffWorld, name: str) -> dict[str, Any]:
    config_id = _remote_named(w, name)["id"]
    (entry,) = [c for c in load_manifest(w.root).configurations if c.id == config_id]
    return entry.metadata


def test_a_link_to_a_failed_untracked_create_is_finished_by_the_next_push(tmp_path: Path) -> None:
    """Astra P1: A ("a-ref") fails, B and T are created -- T stays pending, then links to A."""
    w = DiffWorld(tmp_path)
    _seed_variables(w, "existing-prod-vars", ["Default"])
    sql_id = w.api.seed(VARIANTS["sql"], name="Template")
    w.init()
    w.pull()
    _new_config_from(w, "existing-prod-vars", "A vars", "a-ref", ["a-row"])
    _new_config_from(w, "existing-prod-vars", "B vars", "b-ref", ["b-row"])
    _new_transformation(w, sql_id, {"variables_id": "a-ref", "variables_values_id": "a-row"})
    _fail_once(w, "_create", "A vars", KeboolaApiError("refused", 500, ErrorCode.API_ERROR))

    w.svc.push(alias="prod", project_root=w.root)

    assert _remote_named(w, "New step")["configuration"]["variables_id"] == "a-ref"
    assert _entry_of(w, "New step").get("link_pending") is True

    w.push()

    a_vars = _remote_named(w, "A vars")
    configuration = _remote_named(w, "New step")["configuration"]
    assert configuration["variables_id"] == a_vars["id"]
    assert configuration["variables_values_id"] == a_vars["rows"][0]["id"]
    assert "link_pending" not in _entry_of(w, "New step")


def test_an_explicit_values_row_whose_create_failed_is_never_redirected(tmp_path: Path) -> None:
    """Astra P1: T selects "prod-ref"; that row fails, "test-ref" is created -- no redirect."""
    w = DiffWorld(tmp_path)
    _seed_variables(w, "existing-prod-vars", ["Prod", "Test"])
    sql_id = w.api.seed(VARIANTS["sql"], name="Template")
    w.init()
    w.pull()
    _new_config_from(w, "existing-prod-vars", "New vars", "new-vars-ref", ["prod-ref", "test-ref"])
    _new_transformation(
        w, sql_id, {"variables_id": "new-vars-ref", "variables_values_id": "prod-ref"}
    )
    _fail_once(w, "_create_row", "Prod", KeboolaApiError("refused", 500, ErrorCode.API_ERROR))

    w.svc.push(alias="prod", project_root=w.root)

    assert _remote_named(w, "New step")["configuration"]["variables_values_id"] == "prod-ref"
    assert _entry_of(w, "New step").get("link_pending") is True

    w.push()

    new_vars = _remote_named(w, "New vars")
    configuration = _remote_named(w, "New step")["configuration"]
    assert configuration["variables_id"] == new_vars["id"]
    assert configuration["variables_values_id"] == _by_name(new_vars, "Prod")


def test_an_ambiguous_values_row_is_never_redirected_when_one_create_fails(
    tmp_path: Path,
) -> None:
    """Astra P1: two rows carry "dup-row", one fails -- the other is no fallback target."""
    w = DiffWorld(tmp_path)
    _seed_variables(w, "existing-prod-vars", ["Row one", "Row two"])
    sql_id = w.api.seed(VARIANTS["sql"], name="Template")
    w.init()
    w.pull()
    _new_config_from(w, "existing-prod-vars", "New vars", "new-vars-ref", ["dup-row", "dup-row"])
    _new_transformation(
        w, sql_id, {"variables_id": "new-vars-ref", "variables_values_id": "dup-row"}
    )
    _fail_once(w, "_create_row", "Row one", KeboolaApiError("refused", 500, ErrorCode.API_ERROR))

    w.svc.push(alias="prod", project_root=w.root)

    assert _remote_named(w, "New step")["configuration"]["variables_values_id"] == "dup-row"
    assert _entry_of(w, "New step").get("link_pending") is True


# ---------------------------------------------------------------------------
# Astra on ac852fa0: partial pushes keep what the plan decided
# ---------------------------------------------------------------------------


def test_an_ambiguous_row_placeholder_stays_ambiguous_after_a_partial_push(
    tmp_path: Path,
) -> None:
    """Astra P1: Prod and Test carry "dup-row", Test fails -- the retry never binds T to Test."""
    w = DiffWorld(tmp_path)
    _seed_variables(w, "existing-prod-vars", ["Prod", "Test"])
    sql_id = w.api.seed(VARIANTS["sql"], name="Template")
    w.init()
    w.pull()
    _new_config_from(w, "existing-prod-vars", "New vars", "a-ref", ["dup-row", "dup-row"])
    _new_transformation(w, sql_id, {"variables_id": "a-ref", "variables_values_id": "dup-row"})
    _fail_once(w, "_create_row", "Test", KeboolaApiError("refused", 500, ErrorCode.API_ERROR))
    w.svc.push(alias="prod", project_root=w.root)

    second = w.svc.push(alias="prod", project_root=w.root)

    new_vars = _remote_named(w, "New vars")
    assert sorted(r["name"] for r in new_vars["rows"]) == ["Prod", "Test"]
    assert _remote_named(w, "New step")["configuration"]["variables_values_id"] == "dup-row"
    assert _entry_of(w, "New step").get("link_pending") is True
    assert any(x.get("code") == "PLACEHOLDER_AMBIGUOUS" for x in second.get("warnings", []))


def test_a_fallback_parent_is_finished_once_its_row_exists(tmp_path: Path) -> None:
    """Astra P1: T names unclaimed-parent/row-ref; the fallback picks A, row-ref fails first."""
    w = DiffWorld(tmp_path)
    _seed_variables(w, "existing-prod-vars", ["Default"])
    sql_id = w.api.seed(VARIANTS["sql"], name="Template")
    w.init()
    w.pull()
    _new_config_from(w, "existing-prod-vars", "A vars", "a-ref", ["row-ref"])
    _new_transformation(
        w, sql_id, {"variables_id": "unclaimed-parent", "variables_values_id": "row-ref"}
    )
    _fail_once(w, "_create_row", "Default", KeboolaApiError("refused", 500, ErrorCode.API_ERROR))
    w.svc.push(alias="prod", project_root=w.root)
    assert _entry_of(w, "New step").get("link_pending") is True

    w.push()

    a_vars = _remote_named(w, "A vars")
    configuration = _remote_named(w, "New step")["configuration"]
    assert configuration["variables_id"] == a_vars["id"]
    assert configuration["variables_values_id"] == a_vars["rows"][0]["id"]
    metadata = _entry_of(w, "New step")
    assert "link_pending" not in metadata
    assert "fallback_links" not in metadata


# ---------------------------------------------------------------------------
# Astra on 413e9287: fallback choices are validated and never bypass ambiguity
# ---------------------------------------------------------------------------


def _fallback_over_ambiguous_rows(tmp_path: Path) -> DiffWorld:
    """New A ("a-ref") with Prod and Test both carrying "dup-row"; T names unclaimed-parent/dup-row."""
    w = DiffWorld(tmp_path)
    _seed_variables(w, "existing-prod-vars", ["Prod", "Test"])
    sql_id = w.api.seed(VARIANTS["sql"], name="Template")
    w.init()
    w.pull()
    _new_config_from(w, "existing-prod-vars", "A vars", "a-ref", ["dup-row", "dup-row"])
    _new_transformation(
        w, sql_id, {"variables_id": "unclaimed-parent", "variables_values_id": "dup-row"}
    )
    return w


@pytest.mark.parametrize("pull_between", [False, True], ids=["push", "pull-then-push"])
def test_a_fallback_choice_deleted_on_the_remote_is_never_written(
    tmp_path: Path, pull_between: bool
) -> None:
    """Astra P1: the fallback chose A, A is deleted remotely -- no PUT names A, T stays pending.

    With a plain pull in between (Astra's sequence), A has no manifest entry
    any more; the recorded choice is read all the same.
    """
    w = _fallback_over_ambiguous_rows(tmp_path)
    w.svc.push(alias="prod", project_root=w.root)
    assert _entry_of(w, "New step").get("fallback_links")
    a_id = _remote_named(w, "A vars")["id"]
    del w.api.remote[PROD][(VARS, a_id)]
    if pull_between:
        w.pull()
        assert not [c for c in load_manifest(w.root).configurations if c.id == a_id]

    w.svc.push(alias="prod", project_root=w.root)

    configuration = _remote_named(w, "New step")["configuration"]
    assert configuration["variables_id"] == "unclaimed-parent"
    assert a_id not in str(configuration)
    metadata = _entry_of(w, "New step")
    assert metadata.get("link_pending") is True
    assert metadata.get("fallback_links")


def test_a_fallback_parent_never_resolves_an_ambiguous_row(tmp_path: Path) -> None:
    """Astra P1: Prod fails, Test survives -- the ambiguous "dup-row" does not become Test."""
    w = _fallback_over_ambiguous_rows(tmp_path)
    _fail_once(w, "_create_row", "Prod", KeboolaApiError("refused", 500, ErrorCode.API_ERROR))

    w.svc.push(alias="prod", project_root=w.root)

    assert [r["name"] for r in _remote_named(w, "A vars")["rows"]] == ["Test"]
    configuration = _remote_named(w, "New step")["configuration"]
    assert configuration["variables_values_id"] == "dup-row"
    assert _entry_of(w, "New step").get("link_pending") is True
