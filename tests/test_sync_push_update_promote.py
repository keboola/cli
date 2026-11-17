"""Conditional update and push receipt on the promote path and for ``pyproject.toml`` (#827).

Driven through the real ``SyncService`` over the branch-aware in-memory API
of the bookkeeping differential test, which honours ``If-Match`` on PUT.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.services import _sync_promote_remap, _sync_writeback
from keboola_agent_cli.sync.manifest import load_manifest
from test_sync_bookkeeping_differential import (
    CONFIG_FILE,
    DEV,
    PROD,
    VARIANTS,
    _seeded,
    semantic_edit,
)
from test_sync_promote_branch_entries import _seed_flow_world

FLOW = ("keboola.flow", "cfg-flow")


def _spy_versions(w: Any, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    sent: list[Any] = []
    real = w.api._update

    def spy(*args: Any, **kwargs: Any) -> dict:
        sent.append(kwargs.get("expected_version"))
        return real(*args, **kwargs)

    monkeypatch.setattr(w.api, "_update", spy)
    return sent


# ------------------------------------------------------------------------ R5


def test_promote_update_sends_the_target_branch_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v = VARIANTS["extractor"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    w.api.branch_from_prod()
    w.api.remote[PROD][(v.component_id, cid)]["version"] = 3
    w.api.remote[DEV][(v.component_id, cid)]["version"] = 7
    semantic_edit(w.config_dir(cid) / CONFIG_FILE)

    (change,) = [c for c in w.diff(branch=DEV)["changes"] if c["config_id"] == cid]
    sent = _spy_versions(w, monkeypatch)
    w.push(branch=DEV)

    assert change["remote_version"] == 7
    assert sent == [7]


def test_promote_update_refuses_a_dev_edit_made_after_the_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v = VARIANTS["extractor"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    w.api.branch_from_prod()
    dev = w.api.remote[DEV][(v.component_id, cid)]
    semantic_edit(w.config_dir(cid) / CONFIG_FILE)
    real = w.api._update

    def ui_edits_dev_first(*args: Any, **kwargs: Any) -> dict:
        dev["name"] = "edited on the dev branch"
        dev["version"] = dev.get("version", 1) + 1
        return real(*args, **kwargs)

    monkeypatch.setattr(w.api, "_update", ui_edits_dev_first)
    result = w.svc.push(alias="prod", project_root=w.root, branch_override=DEV)

    assert [e["error_code"] for e in result["errors"]] == ["SYNC_CONFLICT"], result
    assert dev["name"] == "edited on the dev branch"
    assert w.api.remote[PROD][(v.component_id, cid)]["name"] == "Orders"


# ------------------------------------------------------------------------ R6


def test_a_config_whose_update_failed_is_not_rebound_later_in_the_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D4: a ``rebind_pending`` of an earlier push is not acted on after the update's 412."""
    f = _seed_flow_world(tmp_path, flow_first=True)
    w = f.world
    comp = VARIANTS["extractor"].component_id
    w.api.branch_from_prod()
    del w.api.remote[DEV][(comp, f.extractor_id)]
    flow_file = w.config_dir(f.flow_id) / CONFIG_FILE
    semantic_edit(flow_file)
    real_create = w.api._create

    def extractor_create_fails(*args: Any, **kwargs: Any) -> dict:
        if kwargs.get("component_id") == comp:
            raise KeboolaApiError("create refused", 500, ErrorCode.API_ERROR)
        return real_create(*args, **kwargs)

    monkeypatch.setattr(w.api, "_create", extractor_create_fails)
    first = w.svc.push(alias="prod", project_root=w.root, branch_override=DEV)
    monkeypatch.undo()
    assert first["errors"]

    def pending_of_flow() -> Any:
        manifest = load_manifest(w.root)
        return [
            c.metadata.get("rebind_pending")
            for c in manifest.configurations
            if c.id == f.flow_id and c.metadata.get("rebind_pending")
        ]

    pending_before = pending_of_flow()
    assert pending_before  # the flow still names the source extractor on dev

    dev_flow = w.api.remote[DEV][FLOW]
    dev_flow["version"] = 4
    semantic_edit(flow_file)
    real_update = w.api._update
    flow_puts: list[Any] = []

    def ui_edits_flow_then_put(*args: Any, **kwargs: Any) -> dict:
        if kwargs.get("component_id") == "keboola.flow":
            flow_puts.append(kwargs.get("expected_version"))
            if kwargs.get("expected_version") == 4:
                dev_flow["name"] = "edited on the dev branch"
                dev_flow["version"] = 5
        return real_update(*args, **kwargs)

    monkeypatch.setattr(w.api, "_update", ui_edits_flow_then_put)
    result = w.svc.push(alias="prod", project_root=w.root, branch_override=DEV)

    assert [e["error_code"] for e in result["errors"]] == ["SYNC_CONFLICT"], result
    assert flow_puts == [4]  # the refused PUT; no rebind PUT after it
    assert dev_flow["name"] == "edited on the dev branch"
    assert dev_flow["configuration"]["tasks"][0]["task"]["configId"] == f.extractor_id
    assert pending_of_flow() == pending_before


# ------------------------------------------------------------------------ R13


def test_the_final_promote_stamp_uses_the_receipt_not_the_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D9: an edit saved before the final remap stamp is not absorbed into the baseline."""
    f = _seed_flow_world(tmp_path, flow_first=True)
    w = f.world
    w.api.branch_from_prod()
    del w.api.remote[DEV][(VARIANTS["extractor"].component_id, f.extractor_id)]
    flow_file = w.config_dir(f.flow_id) / CONFIG_FILE
    semantic_edit(flow_file)
    real = _sync_promote_remap.stamp_updated_config
    stamped: list[str] = []

    def editor_saves_then_stamp(*args: Any, **kwargs: Any) -> Any:
        stamped.append(kwargs["config_id"])
        semantic_edit(flow_file)
        return real(*args, **kwargs)

    monkeypatch.setattr(_sync_promote_remap, "stamp_updated_config", editor_saves_then_stamp)
    w.push(branch=DEV)
    monkeypatch.undo()

    assert stamped == [f.flow_id]  # the final rebind resent and stamped the flow
    changes = {c["config_id"]: c["change_type"] for c in w.diff(branch=DEV)["changes"]}
    assert changes.get(f.flow_id) == "modified"


# ------------------------------------------------------------------------ R10


@pytest.mark.parametrize("when", ["before_refresh", "after_refresh"])
def test_a_pyproject_edit_during_the_push_is_not_stamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    """D8: the pyproject.toml stamp is the hash of the bytes push wrote, never a re-read."""
    v = VARIANTS["python"]
    seeded = _seeded(tmp_path, v)
    w, cid = seeded.world, seeded.config_id
    config_dir = w.config_dir(cid)
    pyproject = config_dir / "pyproject.toml"
    assert pyproject.is_file()
    semantic_edit(config_dir / CONFIG_FILE)
    real = _sync_writeback.refresh_pyproject

    def editor_edits_dependencies(*args: Any, **kwargs: Any) -> Any:
        if when == "before_refresh":
            semantic_edit(pyproject)
            return real(*args, **kwargs)
        refreshed = real(*args, **kwargs)
        semantic_edit(pyproject)
        return refreshed

    monkeypatch.setattr(_sync_writeback, "refresh_pyproject", editor_edits_dependencies)
    result = w.push()
    monkeypatch.undo()
    edited = pyproject.read_text(encoding="utf-8")

    assert "originale" in edited  # the edit is on disk
    if when == "before_refresh":
        codes = [x.get("code") for x in result.get("warnings", [])]
        assert "LOCAL_EDIT_DURING_PUSH" in codes, result
    changes = {c["config_id"]: c["change_type"] for c in w.diff()["changes"]}
    assert changes.get(cid) == "modified"


# ------------------------------------------------------------------------ F4


def test_a_promote_update_whose_writeback_failed_is_not_resent_or_stamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4: the PUT landed, the local writeback raised; the final rebind skips the config."""
    from keboola_agent_cli.services import _sync_push_update

    f = _seed_flow_world(tmp_path, flow_first=True)
    w = f.world
    w.api.branch_from_prod()
    del w.api.remote[DEV][(VARIANTS["extractor"].component_id, f.extractor_id)]
    flow_file = w.config_dir(f.flow_id) / CONFIG_FILE
    semantic_edit(flow_file)

    def writeback_fails(*_: Any, **__: Any) -> Any:
        raise PermissionError(13, "Permission denied", str(flow_file))

    monkeypatch.setattr(_sync_push_update, "settle_local_files", writeback_fails)
    flow_puts = _spy_flow_puts(w, monkeypatch)
    result = w.svc.push(alias="prod", project_root=w.root, branch_override=DEV)
    monkeypatch.undo()

    assert [e["config_id"] for e in result["errors"]] == [f.flow_id], result
    assert len(flow_puts) == 1  # Phase A only: no final resend


def _spy_flow_puts(w: Any, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    puts: list[Any] = []
    real = w.api._update

    def spy(*args: Any, **kwargs: Any) -> dict:
        if kwargs.get("component_id") == "keboola.flow":
            puts.append(kwargs.get("configuration"))
        return real(*args, **kwargs)

    monkeypatch.setattr(w.api, "_update", spy)
    return puts


# ------------------------------------------------------------------------ F7


def test_a_link_recovery_stamp_never_absorbs_an_edit_saved_after_its_write(
    tmp_path: Path, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F7: a ``link_pending`` flow rewritten by Phase D is stamped from the bytes written."""
    import yaml

    from keboola_agent_cli.services import _sync_bindings
    from test_sync_clone_links import FLOW
    from test_sync_clone_links import World as CloneWorld

    world = CloneWorld(tmp_path, tmp_config_dir)
    world.tgt_api.fail_row_names.add("orders")
    world.clone()
    (entry,) = [c for c in load_manifest(world.clone_dir).configurations if c.component_id == FLOW]
    assert entry.metadata.get("link_pending") is True
    flow_file = world.clone_dir / "main" / entry.path / CONFIG_FILE
    world.tgt_api.fail_row_names.clear()
    real = _sync_bindings._refresh_binding_hashes
    fired: list[bool] = []

    def editor_saves_then_stamp(*args: Any, **kwargs: Any) -> Any:
        if kwargs["created"].component_id == FLOW and not fired:
            fired.append(True)
            data = yaml.safe_load(flow_file.read_text(encoding="utf-8"))
            data["description"] = "edited after the backfill wrote the file"
            flow_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return real(*args, **kwargs)

    monkeypatch.setattr(_sync_bindings, "_refresh_binding_hashes", editor_saves_then_stamp)
    world.clone()
    monkeypatch.undo()

    assert fired
    diff = world.service.diff(alias="target", project_root=world.clone_dir)
    changes = {c["config_id"]: c["change_type"] for c in diff["changes"] if not c.get("is_row")}
    assert changes.get(entry.id) == "modified", diff
