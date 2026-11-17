"""``sync push`` of a modified config: conditional PUT and local-file guards (#827, items 4 and 8).

Driven through the real ``SyncService`` over the stateful Storage API fake
(``formal_sync_fake``). Each concurrent actor -- a human in the Keboola UI, an
editor saving a file, the transport repeating a PUT -- is injected at the
exact point with a one-shot hook or a wrapped function.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from formal_sync_fake import CONFIG_FILENAME, FakeStorage, Workspace
from keboola_agent_cli.services import _sync_push_update
from keboola_agent_cli.services._sync_push_update import (
    SentUpdate,
    holds_sent,
    local_edit_warning,
)

HTTP = "keboola.ex-http"


@pytest.fixture
def api() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def ws(tmp_config_dir: Path, tmp_path: Path, api: FakeStorage) -> Workspace:
    return Workspace(tmp_config_dir, tmp_path / "repo", api)


def _pulled(ws: Workspace, api: FakeStorage, **kw: Any) -> tuple[str, Path]:
    cid = api.add("Orders", ["SELECT 1"], **kw)
    ws.init_and_pull()
    return cid, ws.dir_of(cid)


def _codes(result: dict[str, Any]) -> list[str]:
    return [e.get("error_code", "") for e in result["errors"]]


# --------------------------------------------------------------------- D1, D2


def test_diff_records_the_version_of_a_modified_config_and_push_sends_it(
    ws: Workspace, api: FakeStorage
) -> None:
    cid, d = _pulled(ws, api)
    api.configs[cid]["version"] = 7
    ws.edit_description(d, "local")

    (change,) = [c for c in ws.diff()["changes"] if c["config_id"] == cid]
    result = ws.push()

    assert change["change_type"] == "modified"
    assert change["remote_version"] == 7
    assert result["updated"] == 1, result
    assert api.update_kwargs == [{"expected_version": 7}]


def test_a_listing_without_version_sends_no_precondition(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid, d = _pulled(ws, api)
    real_view = api._view

    def without_version(cfg: dict[str, Any]) -> dict[str, Any]:
        view = real_view(cfg)
        view.pop("version", None)
        return view

    monkeypatch.setattr(api, "_view", without_version)
    ws.edit_description(d, "local")

    (change,) = [c for c in ws.diff()["changes"] if c["config_id"] == cid]
    result = ws.push()

    assert "remote_version" not in change
    assert result["updated"] == 1, result
    assert api.update_kwargs == [{}]
    assert ws.change_of(cid) == "unchanged"


# --------------------------------------------------------------------- D3, R1


def test_412_when_only_the_enabled_state_differs_is_a_conflict(
    ws: Workspace, api: FakeStorage
) -> None:
    """R1: push re-enables (line removed); a row edit raises the version; the config stays disabled."""
    cid, d = _pulled(ws, api, is_disabled=True)
    data = ws.read_yaml(d)
    assert data["is_disabled"] is True
    del data["is_disabled"]
    ws.write_yaml(d, data)

    api.on("update_config", "before", lambda _cid: api.ui_edit_row(cid))
    result = ws.push()

    assert _codes(result) == ["SYNC_CONFLICT"], result
    assert api.configs[cid]["isDisabled"] is True
    assert ("get_config_detail", cid) in api.calls


# --------------------------------------------------------------------- D3, R2


def test_412_with_only_an_encrypted_value_changed_is_a_conflict(
    ws: Workspace, api: FakeStorage
) -> None:
    """R2: the comparison uses the exact ciphertext sent, never config_hash's placeholder."""
    cid = api.add(
        "Api",
        component_id=HTTP,
        configuration={"parameters": {"#token": "KBC::ProjectSecure::AAA", "url": "a"}},
    )
    ws.init_and_pull()
    d = ws.dir_of(cid)
    data = ws.read_yaml(d)
    data["parameters"]["url"] = "b"
    ws.write_yaml(d, data)

    def ui_sets_same_url_other_secret(_cid: str) -> None:
        cfg = api.configs[cid]
        cfg["configuration"] = {"parameters": {"#token": "KBC::ProjectSecure::BBB", "url": "b"}}
        cfg["version"] += 1

    api.on("update_config", "before", ui_sets_same_url_other_secret)
    result = ws.push()

    assert _codes(result) == ["SYNC_CONFLICT"], result
    assert api.configs[cid]["configuration"]["parameters"]["#token"] == "KBC::ProjectSecure::BBB"


def test_holds_sent_treats_an_empty_object_and_an_empty_list_alike() -> None:
    sent = SentUpdate("n", "", {"parameters": {}, "storage": {"input": {}}}, None)
    remote = {
        "name": "n",
        "description": "",
        "configuration": {"parameters": [], "storage": {"input": []}},
    }
    assert holds_sent(remote, sent)
    assert not holds_sent({**remote, "description": "x"}, sent)
    assert not holds_sent({**remote, "name": "m"}, sent)
    assert holds_sent({**remote, "isDisabled": True}, sent)  # state not sent: not compared
    assert not holds_sent(
        {**remote, "isDisabled": True}, SentUpdate("n", "", sent.configuration, False)
    )


# --------------------------------------------------------------------- D3, R4


class RetryingStorage(FakeStorage):
    """The transport repeats a PUT whose response was lost, with the same If-Match."""

    retried = False

    def update_config(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        response = super().update_config(*args, **kwargs)
        if not self.retried:
            self.retried = True
            return super().update_config(*args, **kwargs)  # 412: our own write landed
        return response


def test_a_retried_put_after_a_lost_response_is_verified_and_stamped(
    tmp_config_dir: Path, tmp_path: Path
) -> None:
    api = RetryingStorage()
    ws = Workspace(tmp_config_dir, tmp_path / "repo", api)
    cid, d = _pulled(ws, api)
    ws.edit_description(d, "local")

    result = ws.push()

    assert api.retried
    assert result["errors"] == [], result
    assert result["updated"] == 1
    assert api.configs[cid]["description"] == "local"
    assert ("get_config_detail", cid) in api.calls
    assert ws.change_of(cid) == "unchanged"  # the baseline was stamped


def test_a_conflict_leaves_the_file_the_manifest_and_later_phases_alone(
    ws: Workspace, api: FakeStorage
) -> None:
    cid, d = _pulled(ws, api)
    ws.edit_description(d, "local")
    before_file = (d / CONFIG_FILENAME).read_bytes()
    before_entry = ws.entry(cid)

    api.on("update_config", "before", lambda _cid: api.ui_edit(cid, ["SELECT 'ui'"]))
    result = ws.push()

    assert _codes(result) == ["SYNC_CONFLICT"], result
    assert "version 1" in result["errors"][0]["message"]
    assert (d / CONFIG_FILENAME).read_bytes() == before_file
    assert ws.entry(cid) == before_entry
    assert api.updates_of(cid) == 0
    assert api.script(cid) == ["SELECT 'ui'"]


# --------------------------------------------------------------------- D6, R9


def test_a_file_edited_and_restored_while_push_reads_it_is_not_pushed(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R9 (A -> B -> A): the bytes push would send (B) are not the bytes it hashed first (A)."""
    cid, d = _pulled(ws, api)
    ws.edit_description(d, "A")
    content_a = (d / CONFIG_FILENAME).read_bytes()
    entry_before = ws.entry(cid)
    real = _sync_push_update.capture_local_input

    def editor_flips_during_read(*args: Any, **kwargs: Any) -> Any:
        ws.edit_description(d, "B")
        captured = real(*args, **kwargs)
        (d / CONFIG_FILENAME).write_bytes(content_a)
        return captured

    monkeypatch.setattr(_sync_push_update, "capture_local_input", editor_flips_during_read)
    result = ws.push()

    assert _codes(result) == ["SYNC_LOCAL_FILE_CHANGED"], result
    assert api.updates_of(cid) == 0
    assert api.update_kwargs == []
    assert (d / CONFIG_FILENAME).read_bytes() == content_a
    assert ws.entry(cid) == entry_before


def test_a_companion_edited_while_push_reads_it_is_not_pushed(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid, d = _pulled(ws, api)
    ws.edit_sql(d, "SELECT 2;\n")
    real = _sync_push_update.capture_local_input

    def edit_then_capture(*args: Any, **kwargs: Any) -> Any:
        ws.edit_sql(d, "SELECT 3;\n")
        return real(*args, **kwargs)

    monkeypatch.setattr(_sync_push_update, "capture_local_input", edit_then_capture)
    result = ws.push()

    assert _codes(result) == ["SYNC_LOCAL_FILE_CHANGED"], result
    assert api.updates_of(cid) == 0
    monkeypatch.undo()
    assert ws.push()["updated"] == 1  # a re-run pushes the current file
    assert api.script(cid) == ["SELECT 3;"]


# --------------------------------------------------------------------- D7, R11


def test_removing_is_disabled_during_the_put_is_kept_and_pushed_next(
    ws: Workspace, api: FakeStorage
) -> None:
    """R11: the disabled baseline is the one push read; the removed line re-enables next time."""
    cid, d = _pulled(ws, api, is_disabled=True)
    ws.edit_description(d, "local")

    def editor_removes_the_line(_cid: str) -> None:
        data = ws.read_yaml(d)
        del data["is_disabled"]
        ws.write_yaml(d, data)

    api.on("update_config", "before", editor_removes_the_line)
    result = ws.push()

    assert result["updated"] == 1, result
    assert [w["code"] for w in result["warnings"]] == ["LOCAL_EDIT_DURING_PUSH"]
    assert "is_disabled" not in ws.read_yaml(d)
    assert api.configs[cid]["isDisabled"] is True
    assert ws.change_of(cid) == "modified"
    second = ws.push()
    assert second["errors"] == [], second
    assert api.configs[cid]["isDisabled"] is False


def test_an_edit_during_the_put_of_a_config_with_secrets_says_plaintext_stays() -> None:
    """R7: the warning names the plain-text secrets the skipped writeback left."""
    warning = local_edit_warning(HTTP, "1", Path("main/x"), secrets=True)
    assert warning["code"] == "LOCAL_EDIT_DURING_PUSH"
    assert "plain text" in warning["message"]
    assert "plain text" not in local_edit_warning(HTTP, "1", Path("x"), secrets=False)["message"]


def test_an_edit_saved_right_after_the_writeback_is_not_stamped(
    ws: Workspace, api: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D8: the stamp is the hash of the bytes push wrote, never a re-read."""
    cid, d = _pulled(ws, api)
    ws.edit_sql(d, "SELECT 2;\n")
    real = _sync_push_update.writeback_after_push

    def writeback_then_sql_edit(*args: Any, **kwargs: Any) -> Any:
        written = real(*args, **kwargs)
        ws.edit_sql(d, "SELECT 3;\n")
        return written

    monkeypatch.setattr(_sync_push_update, "writeback_after_push", writeback_then_sql_edit)
    result = ws.push()
    monkeypatch.undo()

    assert result["updated"] == 1, result
    assert api.script(cid) == ["SELECT 2;"]
    assert ws.change_of(cid) == "modified"
    entry = copy.deepcopy(ws.entry(cid))
    assert entry is not None
    assert entry.metadata["pull_hash"] == ws.service()._file_hash(d / CONFIG_FILENAME)
