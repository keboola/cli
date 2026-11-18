"""Row writes behind a parent version check (#827 follow-up, R1-R4).

The Storage API row endpoints take no ``If-Match``. Before the first row work
of a parent config, push reads the parent and compares its version with the
one it expects: the diff's (``parent_remote_version``), or our own PUT's when
Phase A updated the parent. Anything it cannot prove unchanged -- a different
version, a PUT verified only by reading it back after a 412, a missing
version, a failed read -- blocks the parent's rows: nothing is written for
them and their files and manifest entries stay as they are.

Real ``SyncService`` on a real tree; only the Storage API is faked
(``tests/formal_sync_fake.py``: every row write raises the parent's version).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from formal_sync_fake import Crash, FakeStorage, Workspace, not_found, precondition_failed
from keboola_agent_cli.constants import CONFIG_FILENAME
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError


@pytest.fixture
def api() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def ws(tmp_config_dir: Path, tmp_path: Path, api: FakeStorage) -> Workspace:
    return Workspace(tmp_config_dir, tmp_path / "repo", api)


def _with_rows(api: FakeStorage, name: str = "Orders") -> str:
    cid = api.add(name, ["SELECT 1"])
    for rid, table in (("500", "a"), ("501", "b")):
        api.configs[cid]["rows"].append(
            {
                "id": rid,
                "name": f"Row {table}",
                "description": "",
                "configuration": {"parameters": {"table": table}},
            }
        )
    return cid


def _row_files(ws: Workspace, cid: str) -> list[Path]:
    return sorted((ws.dir_of(cid) / "rows").rglob(CONFIG_FILENAME))


def _edit_rows(ws: Workspace, cid: str, value: str) -> None:
    for path in _row_files(ws, cid):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["parameters"] = {"table": value}
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _remote_tables(api: FakeStorage, cid: str) -> list[str]:
    return [r["configuration"]["parameters"]["table"] for r in api.configs[cid]["rows"]]


def _row_entries(ws: Workspace, cid: str) -> list[dict[str, Any]]:
    entry = ws.entry(cid)
    assert entry is not None
    return [r.model_dump() for r in entry.rows]


def _gets(api: FakeStorage, cid: str) -> int:
    return sum(1 for call in api.calls if call == ("get_config_detail", cid))


def _row_errors(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in result["errors"] if e.get("parent_config_id") or e.get("parent_path")]


def _snapshot(ws: Workspace, cid: str) -> tuple[list[bytes], list[dict[str, Any]]]:
    return [p.read_bytes() for p in _row_files(ws, cid)], _row_entries(ws, cid)


# ---------------------------------------------------------------------------
# R1: the diff records the parent's version on every row change
# ---------------------------------------------------------------------------


def test_diff_records_the_parent_version_on_row_changes(ws: Workspace, api: FakeStorage) -> None:
    cid = _with_rows(api)
    ws.init_and_pull()
    _edit_rows(ws, cid, "z")
    rows = [c for c in ws.diff()["changes"] if c.get("is_row")]
    assert len(rows) == 2
    assert {c["parent_remote_version"] for c in rows} == {api.configs[cid]["version"]}


# ---------------------------------------------------------------------------
# R3: unchanged parent -> one read, rows written
# ---------------------------------------------------------------------------


def test_rows_of_an_unchanged_parent_are_written_after_one_check(
    ws: Workspace, api: FakeStorage
) -> None:
    cid = _with_rows(api)
    ws.init_and_pull()
    _edit_rows(ws, cid, "z")
    api.calls.clear()

    result = ws.push()

    assert result["errors"] == [], result
    assert _remote_tables(api, cid) == ["z", "z"]
    assert _gets(api, cid) == 1  # once per parent: our own row writes raise its version


def test_rows_of_a_parent_this_push_updated_are_written(ws: Workspace, api: FakeStorage) -> None:
    """Expected = the version our own PUT answered with."""
    cid = _with_rows(api)
    ws.init_and_pull()
    ws.edit_description(ws.dir_of(cid), "new text")
    _edit_rows(ws, cid, "z")

    result = ws.push()

    assert result["errors"] == [], result
    assert api.configs[cid]["description"] == "new text"
    assert _remote_tables(api, cid) == ["z", "z"]


# ---------------------------------------------------------------------------
# R3: a change in Keboola after the diff blocks the rows (risk 9)
# ---------------------------------------------------------------------------


def test_a_ui_row_edit_after_the_diff_blocks_the_rows(ws: Workspace, api: FakeStorage) -> None:
    cid = _with_rows(api)
    ws.init_and_pull()
    _edit_rows(ws, cid, "z")
    before = _snapshot(ws, cid)
    # The UI edits a row right after push's diff read the listing.
    api.on("list_components_with_configs", "after", lambda: api.ui_edit_row(cid))

    result = ws.push()

    errors = _row_errors(result)
    assert len(errors) == 2, result
    assert {e["error_code"] for e in errors} == {ErrorCode.SYNC_CONFLICT}
    assert all(e["parent_config_id"] == cid for e in errors)
    assert "changed in Keboola" in errors[0]["message"]
    assert _remote_tables(api, cid) == ["a", "b"]
    assert _snapshot(ws, cid) == before


def test_a_412_verified_by_readback_blocks_the_rows(ws: Workspace, api: FakeStorage) -> None:
    """Risk 5: our PUT landed, its answer was lost, a UI row edit followed, the retry got 412.

    The read-back matches every field push sent, so the config update counts
    as done -- but that proves nothing about the rows, so none is written.
    """
    cid = _with_rows(api)
    ws.init_and_pull()
    ws.edit_description(ws.dir_of(cid), "new text")
    _edit_rows(ws, cid, "z")
    before = _snapshot(ws, cid)

    def lost_answer_then_row_edit(config_id: str) -> None:
        api.ui_edit_row(cid)
        version = api.configs[cid]["version"]
        raise precondition_failed(config_id, version, version - 2)

    api.on("update_config", "after", lost_answer_then_row_edit)

    result = ws.push()

    assert api.configs[cid]["description"] == "new text"
    errors = _row_errors(result)
    assert len(errors) == 2, result
    assert {e["error_code"] for e in errors} == {ErrorCode.SYNC_CONFLICT}
    assert "reading it back" in errors[0]["message"]
    assert _remote_tables(api, cid) == ["a", "b"]
    assert _snapshot(ws, cid) == before


# ---------------------------------------------------------------------------
# R3: fail closed (risk 6)
# ---------------------------------------------------------------------------


def test_versions_missing_on_both_sides_block_the_rows(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = _with_rows(api)
    ws.init_and_pull()
    _edit_rows(ws, cid, "z")
    real_view = api._view

    def no_version(cfg: dict[str, Any]) -> dict[str, Any]:
        view = real_view(cfg)
        view.pop("version", None)
        return view

    monkeypatch.setattr(api, "_view", no_version)

    result = ws.push()

    errors = _row_errors(result)
    assert len(errors) == 2, result
    assert {e["error_code"] for e in errors} == {ErrorCode.SYNC_CONFLICT}
    assert _remote_tables(api, cid) == ["a", "b"]


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (
            KeboolaApiError("Internal error", status_code=500, error_code=ErrorCode.API_ERROR),
            ErrorCode.API_ERROR,
        ),
        (not_found("config"), ErrorCode.NOT_FOUND),
    ],
    ids=["5xx", "404"],
)
def test_a_failed_parent_read_blocks_the_rows_with_its_own_code(
    ws: Workspace,
    api: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    failure: KeboolaApiError,
    code: ErrorCode,
) -> None:
    cid = _with_rows(api)
    ws.init_and_pull()
    _edit_rows(ws, cid, "z")
    before = _snapshot(ws, cid)

    def failing(*_: Any, **__: Any) -> dict[str, Any]:
        raise failure

    monkeypatch.setattr(api, "get_config_detail", failing)

    result = ws.push()

    errors = _row_errors(result)
    assert len(errors) == 2, result
    assert {e["error_code"] for e in errors} == {code}
    assert _remote_tables(api, cid) == ["a", "b"]
    assert _snapshot(ws, cid) == before


# ---------------------------------------------------------------------------
# R3: before any bookkeeping of the rows (risk 7)
# ---------------------------------------------------------------------------


def test_a_blocked_adopted_parent_is_not_registered(ws: Workspace, api: FakeStorage) -> None:
    """The check precedes adoption: a blocked parent gains no manifest entry."""
    cid = _with_rows(api)
    ws.init_and_pull()
    config_dir = ws.dir_of(cid)
    template = _row_files(ws, cid)[0]
    manifest = ws.manifest()
    manifest.configurations = [c for c in manifest.configurations if c.id != cid]
    from keboola_agent_cli.sync.manifest import save_manifest

    save_manifest(ws.root, manifest)
    new_row = config_dir / "rows" / "row-new"
    shutil.copytree(template.parent, new_row)
    data = yaml.safe_load((new_row / CONFIG_FILENAME).read_text(encoding="utf-8"))
    data["name"] = "Row new"
    data["parameters"] = {"table": "new"}
    data["_keboola"].pop("row_id", None)
    (new_row / CONFIG_FILENAME).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    api.on("list_components_with_configs", "after", lambda: api.ui_edit_row(cid))

    result = ws.push()

    assert any(e["error_code"] == ErrorCode.SYNC_CONFLICT for e in _row_errors(result)), result
    assert ws.entry(cid) is None
    assert len(api.configs[cid]["rows"]) == 2


def test_rows_of_a_parent_created_in_this_push_need_no_check(
    ws: Workspace, api: FakeStorage
) -> None:
    template_cid = _with_rows(api)
    ws.init_and_pull()
    new_dir = ws.new_local_config(ws.dir_of(template_cid), "Fresh")
    for path in (new_dir / "rows").rglob(CONFIG_FILENAME):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data.get("_keboola", {}).pop("row_id", None)
        data.get("_keboola", {}).pop("config_id", None)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    result = ws.push()

    assert result["errors"] == [], result
    (fresh,) = api.live_named("Fresh")
    assert len(fresh["rows"]) == 2


# ---------------------------------------------------------------------------
# R4: rows grouped by parent
# ---------------------------------------------------------------------------


def test_rows_are_grouped_by_parent_in_first_seen_order() -> None:
    from keboola_agent_cli.services._sync_push_rows import group_by_parent

    def row(parent: str, rid: str) -> dict[str, Any]:
        return {"component_id": "c", "parent_config_id": parent, "config_id": rid}

    ordered = group_by_parent([row("1", "a"), row("2", "b"), row("1", "c"), row("2", "d")])
    assert [r["config_id"] for r in ordered] == ["a", "c", "b", "d"]


# ---------------------------------------------------------------------------
# Review round 1 (items 2, 4, 5)
# ---------------------------------------------------------------------------


def _crash(*_: Any) -> None:
    raise Crash("killed before the manifest recorded the create")


def test_a_blocked_row_says_push_again_and_a_second_push_writes_it(
    ws: Workspace, api: FakeStorage
) -> None:
    """Item 4: a parent edited after the diff blocks the rows; a plain re-push resolves it.

    The next diff reads the new version, so the check passes and the row is
    written. The message says so and never sends the user to `pull --theirs`.
    """
    cid = _with_rows(api)
    ws.init_and_pull()
    _edit_rows(ws, cid, "z")
    api.on("list_components_with_configs", "after", lambda: api.ui_describe(cid, "ui text"))

    first = ws.push()

    errors = _row_errors(first)
    assert len(errors) == 2, first
    message = errors[0]["message"]
    assert "kbagent sync push` again" in message
    assert "--theirs" not in message
    assert _remote_tables(api, cid) == ["a", "b"]
    second = ws.push()
    assert _row_errors(second) == [], second
    assert _remote_tables(api, cid) == ["z", "z"]
    assert api.configs[cid]["description"] == "ui text"


def test_a_readback_blocked_row_says_push_again(ws: Workspace, api: FakeStorage) -> None:
    """Item 4: the read-back case gets the same advice (usually our own repeated PUT)."""
    cid = _with_rows(api)
    ws.init_and_pull()
    ws.edit_description(ws.dir_of(cid), "new text")
    _edit_rows(ws, cid, "z")

    def lost_answer(config_id: str) -> None:
        version = api.configs[cid]["version"]
        raise precondition_failed(config_id, version, version - 1)

    api.on("update_config", "after", lost_answer)
    errors = _row_errors(ws.push())

    assert len(errors) == 2
    assert "kbagent sync push` again" in errors[0]["message"]
    assert "--theirs" not in errors[0]["message"]


def _new_config_with_rows(ws: Workspace, api: FakeStorage) -> tuple[str, Path]:
    """A pulled config with two rows, and a new local config dir copied from it (no ids)."""
    template_cid = _with_rows(api)
    ws.init_and_pull()
    new_dir = ws.new_local_config(ws.dir_of(template_cid), "Fresh")
    for path in (new_dir / "rows").rglob(CONFIG_FILENAME):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data.get("_keboola", {}).pop("row_id", None)
        data.get("_keboola", {}).pop("config_id", None)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return template_cid, new_dir


def test_a_recovered_create_is_checked_before_its_rows(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 2: a config an earlier push created (answer lost) is not "created here".

    The next push recovers it by reading it at version 1; an edit made after
    that read must block its rows, like for any existing config.
    """
    _new_config_with_rows(ws, api)
    api.on("create_config", "after", _crash)
    with pytest.raises(Crash):
        ws.push()
    (fresh,) = api.live_named("Fresh")
    real_detail = api.get_config_detail
    edited: list[str] = []

    def edit_after_the_recovery_read(*args: Any, **kwargs: Any) -> dict[str, Any]:
        answer = real_detail(*args, **kwargs)
        config_id = str(kwargs.get("config_id", args[1] if len(args) > 1 else ""))
        if config_id == fresh["id"] and not edited:
            edited.append(fresh["id"])
            api.ui_describe(fresh["id"], "edited in the UI")
        return answer

    monkeypatch.setattr(api, "get_config_detail", edit_after_the_recovery_read)
    result = ws.push()

    errors = _row_errors(result)
    assert len(errors) == 2, result
    assert {e["error_code"] for e in errors} == {ErrorCode.SYNC_CONFLICT}
    assert api.configs[fresh["id"]]["rows"] == []


def test_rows_of_a_failed_new_config_carry_the_create_error_code(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 5: not PARENT_CONFIG_NOT_TRACKED -- the parent create's own code."""
    _new_config_with_rows(ws, api)

    def refused(*_: Any, **__: Any) -> dict[str, Any]:
        raise KeboolaApiError("Internal error", status_code=500, error_code=ErrorCode.API_ERROR)

    monkeypatch.setattr(api, "create_config", refused)
    result = ws.push()

    rows = [e for e in result["errors"] if e["change_type"] == "added" and e.get("parent_path")]
    rows = rows or [e for e in result["errors"] if "Row" in e["message"]]
    assert len(rows) == 2, result
    assert {e["error_code"] for e in rows} == {ErrorCode.API_ERROR}
    assert all("API_ERROR" in e["message"] for e in rows)
