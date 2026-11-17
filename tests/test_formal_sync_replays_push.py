"""Replays of the formal sync model's push interleavings (#827, findings A, A', B, C, M).

Each test re-enacts one counterexample of the formal sync model against the
REAL ``SyncService`` -- real ``diff`` / ``push`` on a real working tree under
``tmp_path`` -- and fakes only the Storage API client
(``tests/formal_sync_fake.py``).

How an interleaving is forced:

* ``api.on(method, "before" | "after", fn)`` runs ``fn`` once, right before or
  right after the fake applies ``method``. ``fn`` can raise ``Crash`` (process
  death, escapes ``except Exception``), raise ``timeout_error()`` (the request
  was applied, the response was lost), edit the working tree (a human in the
  editor), or run a whole second ``sync push`` (a second process on the same
  dir).
* Where the interesting point sits between two local steps with no API call
  in between, the module-level function is wrapped with ``monkeypatch`` so the
  event fires at exactly that call.

Every test runs for a plain ``sync push`` and for ``sync push --force``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from formal_sync_fake import Crash, FakeStorage, Workspace, timeout_error
from keboola_agent_cli.constants import CONFIG_FILENAME
from keboola_agent_cli.services import _sync_push_create, _sync_push_update, sync_service


@pytest.fixture
def api() -> FakeStorage:
    return FakeStorage()


@pytest.fixture(params=[False, True], ids=["push", "push_force"])
def ws(
    request: pytest.FixtureRequest, tmp_config_dir: Path, tmp_path: Path, api: FakeStorage
) -> Workspace:
    return Workspace(tmp_config_dir, tmp_path / "repo", api, push_force=bool(request.param))


def _pulled(ws: Workspace, api: FakeStorage, name: str = "Orders") -> tuple[str, Path]:
    """One remote SQL transformation, init + pull. Returns (config id, its dir)."""
    cid = api.add(name, ["SELECT 1"])
    ws.init_and_pull()
    return cid, ws.dir_of(cid)


def _crash_at_first_save_after_create(monkeypatch: pytest.MonkeyPatch, api: FakeStorage) -> None:
    """Die at the first manifest save that follows a create POST, wherever it lives."""
    for module in (sync_service, _sync_push_create):
        real = module.save_manifest

        def guarded(*a: Any, _real: Any = real, **kw: Any) -> None:
            if any(c[0] == "create_config" for c in api.calls):
                raise Crash("killed before the manifest recorded the create")
            _real(*a, **kw)

        monkeypatch.setattr(module, "save_manifest", guarded)


def _crash(*_: Any) -> None:
    raise Crash("killed")


def _lose_response(*_: Any) -> None:
    raise timeout_error()


def _rows_of(api: FakeStorage, cid: str, name: str) -> list[dict[str, Any]]:
    return [r for r in api.configs[cid]["rows"] if r["name"] == name]


# =========================================================================== A


def test_a_lost_create_response_then_rerun_does_not_duplicate(
    ws: Workspace, api: FakeStorage
) -> None:
    """Finding A: a lost POST response, then a re-run, leaves one remote config."""
    _, d1 = _pulled(ws, api)
    ws.new_local_config(d1, "Orders Copy")

    api.on("create_config", "after", _lose_response)
    first = ws.push()
    assert first["errors"], "the lost response must surface as an error"
    assert len(api.live_named("Orders Copy")) == 1

    ws.push()

    ids = [c["id"] for c in api.live_named("Orders Copy")]
    assert len(ids) == 1, f"duplicate remote configs for one local dir: {ids}"


def test_a_crash_after_create_then_rerun_does_not_duplicate(
    ws: Workspace, api: FakeStorage
) -> None:
    """Finding A: the process dies after the POST, before the writeback; the re-run creates nothing."""
    _, d1 = _pulled(ws, api)
    ws.new_local_config(d1, "Orders Copy")

    api.on("create_config", "after", _crash)
    with pytest.raises(Crash):
        ws.push()
    assert len(api.live_named("Orders Copy")) == 1

    ws.push()

    assert len(api.live_named("Orders Copy")) == 1


# =========================================================================== A'


def test_a_prime_tracked_config_trashed_in_ui_is_not_recreated(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding A': a tracked config trashed in the UI is not re-created, crash or no crash."""
    cid, _d1 = _pulled(ws, api)
    api.ui_trash(cid)

    _crash_at_first_save_after_create(monkeypatch, api)
    first = ws.push()  # no create is planned, so nothing crashes
    monkeypatch.undo()

    assert not any(c[0] == "create_config" for c in api.calls), first
    assert api.live_named("Orders") == [], first


# =========================================================================== B


def test_b_row_create_crash_before_save_then_rerun_does_not_duplicate_row(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding B: a new row is POSTed once, also when the process dies before the final save."""
    cid = api.add("Orders", ["SELECT 1"])
    api.configs[cid]["rows"].append(
        {
            "id": "500",
            "name": "Existing Row",
            "description": "",
            "configuration": {"parameters": {"table": "a"}},
        }
    )
    ws.init_and_pull()
    d1 = ws.dir_of(cid)
    existing = [p.parent for p in (d1 / "rows").rglob(CONFIG_FILENAME)]
    assert len(existing) == 1

    new_row = d1 / "rows" / "new-row"
    new_row.mkdir()
    data = ws.read_yaml(existing[0])
    data["name"] = "New Row"
    for meta_key in ("row_id", "config_id"):
        (data.get("_keboola") or {}).pop(meta_key, None)
    ws.write_yaml(new_row, data)

    monkeypatch.setattr(sync_service, "save_manifest", _crash)
    with pytest.raises(Crash):
        ws.push()
    monkeypatch.undo()
    assert len(_rows_of(api, cid, "New Row")) == 1

    ws.push()

    rows = _rows_of(api, cid, "New Row")
    assert len(rows) == 1, f"re-run POSTed the row again: rows {[r['id'] for r in rows]}"


# =========================================================================== C


def test_c_concurrent_pushes_double_delete_never_purges(ws: Workspace, api: FakeStorage) -> None:
    """Finding C: a second concurrent DELETE never purges the config the first trashed."""
    _, d1 = _pulled(ws, api)
    shutil.rmtree(d1)

    second: dict[str, Any] = {}

    def second_process_pushes(*_: Any) -> None:
        second["result"] = ws.push()

    api.on("delete_config", "before", second_process_pushes)
    ws.push()

    if "result" in second:  # only push --force sends a DELETE
        assert second["result"]["deleted"] == 1
    assert api.purged == [], f"second concurrent DELETE purged config(s) {api.purged}"


# =========================================================================== M


def test_m_edit_during_put_is_not_overwritten_by_writeback(ws: Workspace, api: FakeStorage) -> None:
    """Finding M (b): a local edit saved while the PUT is in flight survives on disk.

    Push sent v21; the editor saved v22 during the PUT. The writeback is
    skipped (``LOCAL_EDIT_DURING_PUSH``) and the manifest records what was
    sent, so the next diff shows v22 as a local change.
    """
    cid, d1 = _pulled(ws, api)
    ws.edit_description(d1, "v21")

    api.on("update_config", "before", lambda *_: ws.edit_description(d1, "v22"))
    result = ws.push()

    assert result["updated"] == 1, result
    assert api.configs[cid]["description"] == "v21"
    assert ws.read_yaml(d1)["description"] == "v22"
    codes = [w.get("code") for w in result.get("warnings", [])]
    assert codes == ["LOCAL_EDIT_DURING_PUSH"], result
    assert ws.change_of(cid) == "modified"
    ws.push()
    assert api.configs[cid]["description"] == "v22"


def test_m_edit_between_writeback_and_stamp_is_reported(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding M (a): an edit right after the writeback is never stamped as synced."""
    cid, d1 = _pulled(ws, api)
    ws.edit_description(d1, "v21")

    original = _sync_push_update.writeback_after_push
    fired: list[bool] = []

    def writeback_then_editor_saves(*args: Any, **kwargs: Any) -> Any:
        written = original(*args, **kwargs)
        if not fired:
            fired.append(True)
            ws.edit_description(d1, "v22")
        return written

    monkeypatch.setattr(_sync_push_update, "writeback_after_push", writeback_then_editor_saves)
    result = ws.push()
    monkeypatch.undo()
    assert result["updated"] == 1
    assert fired
    assert ws.read_yaml(d1)["description"] == "v22"
    assert api.configs[cid]["description"] == "v21"

    assert ws.change_of(cid) == "modified", "local edit (v22, remote v21) is not reported"
