"""Sync group 3: rows and deletes (D1-D9, risks 1-12).

Every test runs the real ``SyncService`` against the branch-aware ``FakeApi``
of the random walk (same seeded world, same helpers): a local delete is
three-way and covers its rows, rows added on the remote are reported, a row's
identity survives a re-target, a promote push sees what the source tree
deleted, and ignoring a component never deletes local work (#927).

Seeded world (see test_sync_random_walk.py): keboola.ex-http 1000 "Name tk6z"
(value tk1z; rows "Row tk2z"/1001, "Row tk4z"/1002), keboola.ex-http 1003
"Name tk13z" (rows "Row tk9z"/1004, "Row tk11z"/1005), snowflake 1006
"Name tk16z", snowflake 1007, python-transformation-v2 1008, custom-python app
1009. The dev branch starts as a copy of production.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import CONFIG_HASH_VERSION_KEY
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services._sync_models import DELETED_ROWS_KEY, RETIRED_ROWS_KEY
from keboola_agent_cli.services._sync_stale import (
    IGNORED_COMPONENT_REASON,
    REMOTE_DELETED_REASON,
)
from keboola_agent_cli.services.project_service import ProjectService
from keboola_agent_cli.sync.config_format import api_config_to_local
from keboola_agent_cli.sync.diff_engine import config_hash
from keboola_agent_cli.sync.manifest import load_manifest, save_manifest
from test_sync_random_walk import (
    DEV,
    HTTP,
    HTTP_1,
    HTTP_2,
    PROD,
    SQL,
    SQL_1,
    Sim,
    _changes,
    _dir,
    _pull,
    _push,
    _world,
    read_yaml,
    run_walk,
    write_yaml,
)

HTTP_1_DIR = "extractor/keboola.ex-http/name-tk6z"
HTTP_2_DIR = "extractor/keboola.ex-http/name-tk13z"
SQL_1_DIR = "transformation/keboola.snowflake-transformation/name-tk16z"
FORCE_DOES_NOT_OVERRIDE = "--force enables deletions but does not override conflicts"


def _diff(sim: Sim, branch: int | None = None) -> dict[str, Any]:
    return sim.svc.diff(alias="prod", project_root=sim.root, branch_override=branch)


def _row(sim: Sim, branch: int, key: tuple[str, str], name: str) -> dict[str, Any]:
    (row,) = [r for r in sim.api.remote[branch][key]["rows"] if r["name"] == name]
    return row


def _drop_row(sim: Sim, branch: int, key: tuple[str, str], name: str) -> str:
    """Delete the row *name* of *key* on the remote (UI); return its id."""
    row = _row(sim, branch, key, name)
    sim.api.remote[branch][key]["rows"].remove(row)
    return row["id"]


def _entry(sim: Sim, config_id: str, branch: int = PROD) -> Any:
    manifest = load_manifest(sim.root)
    (entry,) = [
        c for c in manifest.configurations if c.id == config_id and (c.branch_id or PROD) == branch
    ]
    return entry


def _set_ignored(sim: Sim, *components: str) -> None:
    manifest = load_manifest(sim.root)
    manifest.ignored_components = list(components)
    save_manifest(sim.root, manifest)


def _spy_row_deletes(sim: Sim, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every row DELETE the push sends (FakeApi binds side effects per client)."""
    calls: list[str] = []
    real = sim.api._delete_row

    def spy(component_id: str, config_id: str, row_id: str, **kw: Any) -> None:
        calls.append(f"{component_id}/{config_id}/rows/{row_id}")
        real(component_id, config_id, row_id, **kw)

    monkeypatch.setattr(sim.api, "_delete_row", spy)
    return calls


def _conflict_details(diff: dict[str, Any], config_id: str) -> list[str]:
    return [
        line
        for c in diff["changes"]
        if c["change_type"] == "conflict" and c["config_id"] == config_id
        for line in c["details"]
    ]


# ===========================================================================
# D1-D3: a local delete is three-way; a config delete covers its rows
# ===========================================================================


def test_force_push_keeps_a_config_whose_row_changed_remotely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Risk 1: the config dir is deleted locally, ONE row is edited in the UI.

    The parent's own hash is unchanged, but its DELETE would delete the row
    edit: nothing is deleted and the parent (and every row) is a conflict.
    """
    sim = _world(tmp_path)
    shutil.rmtree(_dir(sim, HTTP_1_DIR))
    _row(sim, PROD, HTTP_1, "Row tk4z")["configuration"]["parameters"]["value"] = "tk900z"
    deletes = _spy_row_deletes(sim, monkeypatch)

    diff = _diff(sim)
    result = _push(sim, force=True)

    assert HTTP_1 in sim.api.remote[PROD]
    assert len(sim.api.remote[PROD][HTTP_1]["rows"]) == 2
    assert deletes == []
    assert result["deleted"] == 0
    assert ("conflict", "1000") in _changes(sim)
    assert diff["summary"]["deleted"] == 0
    assert any(FORCE_DOES_NOT_OVERRIDE in line for line in _conflict_details(diff, "1000"))


def test_force_push_keeps_a_config_with_a_row_added_remotely(tmp_path: Path) -> None:
    """Risk 2: delete the parent locally, add a row in the UI, push --force -> blocked."""
    sim = _world(tmp_path)
    shutil.rmtree(_dir(sim, HTTP_1_DIR))
    sim.api.remote[PROD][HTTP_1]["rows"].append(sim._remote_row("Row extra"))

    diff = _diff(sim)
    _push(sim, force=True)

    assert HTTP_1 in sim.api.remote[PROD]
    assert [r["row_name"] for r in diff["remote_only_rows"]] == ["Row extra"]
    assert ("conflict", "1000") in _changes(sim)


def test_force_push_keeps_a_config_whose_row_was_deleted_remotely(tmp_path: Path) -> None:
    """D2 (a remote row DELETE is a remote change of the config's rows too):
    deleting the config would discard what the remote did since the pull."""
    sim = _world(tmp_path)
    shutil.rmtree(_dir(sim, HTTP_1_DIR))
    _drop_row(sim, PROD, HTTP_1, "Row tk2z")

    _push(sim, force=True)

    assert HTTP_1 in sim.api.remote[PROD]
    assert ("conflict", "1000") in _changes(sim)


def test_force_push_deletes_an_unchanged_config_once_with_its_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Risk 3 + was FINDING force-delete-row-errors: one DELETE, no row DELETEs,
    no errors; the dry run counts the config once and names its rows."""
    sim = _world(tmp_path)
    shutil.rmtree(_dir(sim, HTTP_2_DIR))
    deletes = _spy_row_deletes(sim, monkeypatch)

    dry = _push(sim, force=True, dry_run=True)
    result = _push(sim, force=True)

    assert dry["summary"]["deleted"] == 1
    (planned,) = [c for c in dry["changes"] if c["change_type"] == "deleted"]
    assert planned["config_id"] == "1003"
    assert sorted(r["config_id"] for r in planned["covered_rows"]) == ["1004", "1005"]
    assert HTTP_2 not in sim.api.remote[PROD]
    assert (result["deleted"], result["errors"]) == (1, [])
    assert deletes == []
    assert all(c.id != "1003" for c in load_manifest(sim.root).configurations)
    assert _changes(sim) == []


def test_plain_push_holds_back_the_config_and_its_rows_as_one_deletion(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    shutil.rmtree(_dir(sim, HTTP_2_DIR))

    result = _push(sim)

    (held,) = result["skipped_deletions"]
    assert held["config_id"] == "1003"
    assert len(held["covered_rows"]) == 2
    assert HTTP_2 in sim.api.remote[PROD]


def test_force_push_deletes_a_config_with_a_legacy_baseline(tmp_path: Path) -> None:
    """Risk 3: an entry written before #686 (no config_hash_version, legacy
    script-shape hash) still proves an unchanged remote."""
    sim = _world(tmp_path)
    manifest = load_manifest(sim.root)
    (entry,) = [c for c in manifest.configurations if c.id == "1006"]
    raw = sim.api.remote[PROD][SQL_1]
    entry.metadata["pull_config_hash"] = config_hash(
        api_config_to_local(SQL, raw, "1006", legacy_scripts=True)
    )
    entry.metadata.pop(CONFIG_HASH_VERSION_KEY, None)
    save_manifest(sim.root, manifest)
    shutil.rmtree(_dir(sim, SQL_1_DIR))

    result = _push(sim, force=True)

    assert SQL_1 not in sim.api.remote[PROD]
    assert (result["deleted"], result["errors"]) == (1, [])


def test_force_push_keeps_a_config_without_a_baseline(tmp_path: Path) -> None:
    """D1: no recorded baseline is no proof -- a conflict, never a DELETE."""
    sim = _world(tmp_path)
    manifest = load_manifest(sim.root)
    (entry,) = [c for c in manifest.configurations if c.id == "1006"]
    entry.metadata["pull_config_hash"] = ""
    save_manifest(sim.root, manifest)
    shutil.rmtree(_dir(sim, SQL_1_DIR))

    diff = _diff(sim)
    _push(sim, force=True)

    assert SQL_1 in sim.api.remote[PROD]
    assert any("no trustworthy baseline" in line for line in _conflict_details(diff, "1006"))


def test_force_push_keeps_a_row_without_a_baseline(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    manifest = load_manifest(sim.root)
    (entry,) = [c for c in manifest.configurations if c.id == "1000"]
    (row,) = [r for r in entry.rows if r.id == "1002"]
    row.metadata["pull_config_hash"] = ""
    save_manifest(sim.root, manifest)
    shutil.rmtree(_dir(sim, f"{HTTP_1_DIR}/rows/row-tk4z"))

    _push(sim, force=True)

    assert any(r["id"] == "1002" for r in sim.api.remote[PROD][HTTP_1]["rows"])


def test_force_push_deletes_an_unchanged_row_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rows-only delete keeps its own DELETE."""
    sim = _world(tmp_path)
    shutil.rmtree(_dir(sim, f"{HTTP_1_DIR}/rows/row-tk4z"))
    deletes = _spy_row_deletes(sim, monkeypatch)

    result = _push(sim, force=True)

    assert deletes == ["keboola.ex-http/1000/rows/1002"]
    assert (result["deleted"], result["errors"]) == (1, [])


@pytest.mark.parametrize(
    "error",
    [
        KeboolaApiError("Access denied", 403, ErrorCode.ACCESS_DENIED),
        KeboolaApiError("Read timed out", 0, ErrorCode.TIMEOUT),
    ],
    ids=["403", "timeout"],
)
def test_failed_config_delete_deletes_no_rows_and_keeps_the_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: KeboolaApiError
) -> None:
    """Risk 6: the parent DELETE fails -> no row DELETE, manifest entry and rows kept."""
    sim = _world(tmp_path)
    shutil.rmtree(_dir(sim, HTTP_2_DIR))
    before = _entry(sim, "1003")
    deletes = _spy_row_deletes(sim, monkeypatch)

    def refuse(component_id: str, config_id: str, branch_id: int | None = None) -> None:
        raise error

    monkeypatch.setattr(sim.api, "_delete", refuse)
    result = _push(sim, force=True)

    assert deletes == []
    assert len(sim.api.remote[PROD][HTTP_2]["rows"]) == 2
    assert [(e["change_type"], e["config_id"]) for e in result["errors"]] == [("deleted", "1003")]
    assert result["deleted"] == 0
    after = _entry(sim, "1003")
    assert [r.id for r in after.rows] == [r.id for r in before.rows]
    assert after.metadata == before.metadata


def test_retry_after_a_partial_force_push(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed parent delete is retried by the next push; nothing done twice."""
    sim = _world(tmp_path)
    shutil.rmtree(_dir(sim, HTTP_1_DIR))
    shutil.rmtree(_dir(sim, HTTP_2_DIR))
    deletes = _spy_row_deletes(sim, monkeypatch)
    real = sim.api._delete
    failed: list[str] = []

    def flaky(component_id: str, config_id: str, branch_id: int | None = None) -> None:
        if config_id == "1000" and not failed:
            failed.append(config_id)
            raise KeboolaApiError("Server error", 503, ErrorCode.API_ERROR)
        real(component_id, config_id, branch_id)

    monkeypatch.setattr(sim.api, "_delete", flaky)
    first = _push(sim, force=True)
    second = _push(sim, force=True)

    assert [e["config_id"] for e in first["errors"]] == ["1000"]
    assert first["deleted"] == 1
    assert (second["deleted"], second["errors"]) == (1, [])
    assert HTTP_1 not in sim.api.remote[PROD] and HTTP_2 not in sim.api.remote[PROD]
    assert deletes == []
    assert _changes(sim) == []


def test_retry_after_a_delete_whose_answer_was_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The DELETE reached the API but the push saw a timeout: the entry is kept
    (unknown outcome), and the next push finds nothing left to delete."""
    sim = _world(tmp_path)
    shutil.rmtree(_dir(sim, HTTP_2_DIR))
    real = sim.api._delete

    def lost_answer(component_id: str, config_id: str, branch_id: int | None = None) -> None:
        real(component_id, config_id, branch_id)
        raise KeboolaApiError("Read timed out", 0, ErrorCode.TIMEOUT)

    monkeypatch.setattr(sim.api, "_delete", lost_answer)
    first = _push(sim, force=True)
    monkeypatch.setattr(sim.api, "_delete", real)
    second = _push(sim, force=True)

    assert [e["config_id"] for e in first["errors"]] == ["1003"]
    assert (second["status"], second["errors"]) == ("no_changes", [])
    assert _changes(sim) == []


# ===========================================================================
# D5: rows added on the remote are reported, never pushed
# ===========================================================================


def test_remote_added_row_is_listed_pulled_with_a_detail_and_never_pushed(
    tmp_path: Path,
) -> None:
    """Risk 12: ``remote_only_rows[]`` + a pull detail record + summary counts."""
    sim = _world(tmp_path)
    row = sim._remote_row("Row extra")
    sim.api.remote[PROD][HTTP_1]["rows"].append(row)

    diff = _diff(sim)
    assert diff["changes"] == []
    assert diff["remote_only_rows"] == [
        {
            "component_id": HTTP,
            "parent_config_id": "1000",
            "row_id": row["id"],
            "row_name": "Row extra",
        }
    ]
    assert diff["summary"]["remote_only_rows"] == 1
    for force in (False, True):
        assert _push(sim, force=force)["status"] == "no_changes"
    assert len(sim.api.remote[PROD][HTTP_1]["rows"]) == 3

    pulled = _pull(sim)

    (detail,) = [d for d in pulled["details"] if d.get("row_id") == row["id"]]
    assert detail["action"] == "new"
    assert detail["path"] == f"{HTTP_1_DIR}/rows/row-extra"
    assert _diff(sim)["remote_only_rows"] == []


def test_pull_reports_a_remote_row_edit_it_writes(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    _row(sim, PROD, HTTP_1, "Row tk4z")["configuration"]["parameters"]["value"] = "tk900z"

    pulled = _pull(sim)

    (detail,) = [d for d in pulled["details"] if d.get("row_id") == "1002"]
    assert detail["action"] == "updated"


def test_summary_unchanged_counts_configs_not_rows(tmp_path: Path) -> None:
    """Risk 12: two row edits of one config never make ``unchanged`` shrink (or go negative)."""
    sim = _world(tmp_path)
    for row in ("row-tk2z", "row-tk4z"):
        sim._local_edit_slot(_dir(sim, HTTP_1_DIR), f"row:{row}")

    summary = _diff(sim)["summary"]

    assert summary["modified"] == 2
    assert summary["unchanged"] == 6


def _cli_diff(tmp_path: Path, diff_result: dict[str, Any], *json_flag: str) -> str:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-55555-fakeTestTokenDoNotUseXXXXXXXX",
            project_name="prod",
            project_id=1234,
        ),
    )
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as store_cls,
        patch("keboola_agent_cli.cli.ProjectService") as project_cls,
        patch("keboola_agent_cli.cli.SyncService") as sync_cls,
    ):
        store_cls.return_value = store
        project_cls.return_value = ProjectService(config_store=store)
        sync_cls.return_value.diff.return_value = diff_result
        result = CliRunner().invoke(
            app,
            [*json_flag, "sync", "diff", "--project", "prod", "--directory", str(tmp_path)],
        )
    assert result.exit_code == 0, result.output
    return result.output


REMOTE_ONLY_ROW_DIFF = {
    "changes": [],
    "remote_only": [],
    "remote_only_rows": [
        {
            "component_id": HTTP,
            "parent_config_id": "1000",
            "row_id": "2001",
            "row_name": "Row extra",
        }
    ],
    "summary": {"added": 0, "modified": 0, "deleted": 0, "unchanged": 6, "remote_only_rows": 1},
}


def test_cli_diff_renders_remote_only_rows(tmp_path: Path) -> None:
    """Risk 12: human output lists the row instead of saying "in sync"."""
    output = _cli_diff(tmp_path, REMOTE_ONLY_ROW_DIFF)

    assert "No differences found" not in output
    assert "Remote-only rows (1 new" in output
    assert "NEW ROW keboola.ex-http/1000/Row extra" in output


def test_cli_diff_json_carries_remote_only_rows(tmp_path: Path) -> None:
    data = json.loads(_cli_diff(tmp_path, REMOTE_ONLY_ROW_DIFF, "--json"))["data"]

    assert data["remote_only_rows"] == REMOTE_ONLY_ROW_DIFF["remote_only_rows"]


# ===========================================================================
# D6: a row's identity survives a re-target (walk seed 210)
# ===========================================================================


@pytest.mark.parametrize("force", [False, True], ids=["pull", "pull-force"])
def test_seed_210_retarget_does_not_recreate_a_remotely_deleted_row(
    tmp_path: Path, force: bool
) -> None:
    """Risk 5: `pull --branch DEV` -> row deleted on production -> `pull (--force)`
    -> `push` used to CREATE the deleted row again (main/ kept its dir, the
    re-target had dropped its manifest entry)."""
    sim = _world(tmp_path)
    _pull(sim, DEV)
    assert RETIRED_ROWS_KEY in load_manifest(sim.root).branches[0].metadata
    _drop_row(sim, PROD, HTTP_2, "Row tk11z")

    _pull(sim, force=force)
    result = _push(sim)

    assert not _dir(sim, f"{HTTP_2_DIR}/rows/row-tk11z").exists()
    assert result["created"] == 0
    assert [r["name"] for r in sim.api.remote[PROD][HTTP_2]["rows"]] == ["Row tk9z"]
    assert _changes(sim) == []
    # The pulled tree's record is consumed by the pull that rebuilt its configs.
    assert RETIRED_ROWS_KEY not in load_manifest(sim.root).branches[0].metadata


def test_seed_210_edited_row_is_kept_and_never_recreated(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    _pull(sim, DEV)
    row_file = _dir(sim, f"{HTTP_2_DIR}/rows/row-tk11z") / "_config.yml"
    row_file.write_text(row_file.read_text().replace("tk12z", "tk900z"), encoding="utf-8")
    _drop_row(sim, PROD, HTTP_2, "Row tk11z")

    pulled = _pull(sim)
    result = _push(sim, force=True)

    assert "tk900z" in row_file.read_text()
    assert any(d.get("reason") == REMOTE_DELETED_REASON for d in pulled["details"])
    assert ("remote_deleted", "1003") in _changes(sim)
    assert result["created"] == 0
    assert len(sim.api.remote[PROD][HTTP_2]["rows"]) == 1


def test_seed_210_retired_rows_still_on_the_remote_keep_the_rediscovery_rule(
    tmp_path: Path,
) -> None:
    """Round 2 regression guard: a retired baseline is deletion provenance only.

    A row still on the remote, edited in main/ while the manifest pointed at
    DEV, keeps the #929 rediscovery rule (D15, D22): kept with an UNKNOWN base,
    a conflict, never pushed over the remote. Round 1 seeded it with the old
    baseline and made it a pushable edit."""
    sim = _world(tmp_path)
    _pull(sim, DEV)
    row_file = _dir(sim, f"{HTTP_2_DIR}/rows/row-tk11z") / "_config.yml"
    row_file.write_text(row_file.read_text().replace("tk12z", "tk900z"), encoding="utf-8")

    _pull(sim)
    _push(sim, force=True)

    assert ("conflict", "1003") in _changes(sim)
    assert _row(sim, PROD, HTTP_2, "Row tk11z")["configuration"]["parameters"]["value"] == ("tk12z")
    (row,) = [r for r in _entry(sim, "1003").rows if r.id == "1005"]
    assert row.metadata.get("unknown_base") == "adopted"


def test_retired_rows_are_pruned_only_when_no_carrier_survives(tmp_path: Path) -> None:
    """A retired record goes once its config is in no manifest entry AND no row
    dir on disk carries its row ids -- never while a carrier survives."""
    sim = _world(tmp_path)
    _pull(sim, DEV)
    del sim.api.remote[DEV][HTTP_2]
    _pull(sim, DEV)  # 1003 is in no manifest entry now; main/ still carries its rows
    assert all(c.id != "1003" for c in load_manifest(sim.root).configurations)
    retired = load_manifest(sim.root).branches[0].metadata[RETIRED_ROWS_KEY]
    assert f"{HTTP}/1003/1005" in retired

    shutil.rmtree(_dir(sim, HTTP_2_DIR))
    _pull(sim, DEV)

    retired = load_manifest(sim.root).branches[0].metadata.get(RETIRED_ROWS_KEY) or {}
    assert not [k for k in retired if k.startswith(f"{HTTP}/1003/")]
    assert f"{HTTP}/1000/1001" in retired  # 1000 is still tracked (on DEV)


# ===========================================================================
# D7: the promote path sees what the source tree deleted (walk seed 257)
# ===========================================================================


def test_seed_257_promote_reports_a_row_production_deleted(tmp_path: Path) -> None:
    """Risk 10: row deleted on production -> `pull` (main/ drops the row) ->
    `push --branch DEV`: DEV still has the row, main/ does not, and diff was
    silent. Now a held-back deletion; `--force` deletes it (DEV unchanged)."""
    sim = _world(tmp_path)
    row_id = _drop_row(sim, PROD, HTTP_1, "Row tk2z")
    _pull(sim)
    assert row_id in _entry(sim, "1000").metadata[DELETED_ROWS_KEY]

    assert ("deleted", "1000") in _changes(sim, DEV)
    held = _push(sim, DEV)
    assert [d["config_id"] for d in held["skipped_deletions"]] == [row_id]
    assert any(r["id"] == row_id for r in sim.api.remote[DEV][HTTP_1]["rows"])

    forced = _push(sim, DEV, force=True)

    assert forced["errors"] == []
    assert all(r["id"] != row_id for r in sim.api.remote[DEV][HTTP_1]["rows"])
    assert HTTP_1 in sim.api.remote[DEV]


def test_seed_257_promote_keeps_a_dev_row_edited_since(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    row_id = _drop_row(sim, PROD, HTTP_1, "Row tk2z")
    _row(sim, DEV, HTTP_1, "Row tk2z")["configuration"]["parameters"]["value"] = "tk900z"
    _pull(sim)

    assert ("conflict", "1000") in _changes(sim, DEV)
    _push(sim, DEV, force=True)
    assert any(r["id"] == row_id for r in sim.api.remote[DEV][HTTP_1]["rows"])


def test_promote_deletion_without_a_baseline_is_a_conflict(tmp_path: Path) -> None:
    """D7: an association whose baseline is lost is a conflict, never made up."""
    sim = _world(tmp_path)
    row_id = _drop_row(sim, PROD, HTTP_1, "Row tk2z")
    _pull(sim)
    manifest = load_manifest(sim.root)
    (entry,) = [c for c in manifest.configurations if c.id == "1000"]
    entry.metadata[DELETED_ROWS_KEY] = {row_id: ""}
    save_manifest(sim.root, manifest)

    assert ("conflict", "1000") in _changes(sim, DEV)


def test_promote_row_only_on_dev_is_remote_only(tmp_path: Path) -> None:
    """Risk 4: DEV gains a row main/ never had -> a remote-only row, not a deletion."""
    sim = _world(tmp_path)
    row = sim._remote_row("Row on dev")
    sim.api.remote[DEV][HTTP_1]["rows"].append(row)

    diff = _diff(sim, DEV)
    _push(sim, DEV, force=True)

    assert [r["row_id"] for r in diff["remote_only_rows"]] == [row["id"]]
    assert not [c for c in diff["changes"] if c["config_id"] == row["id"]]
    assert any(r["id"] == row["id"] for r in sim.api.remote[DEV][HTTP_1]["rows"])


def test_production_rollback_drops_the_deletion_record(tmp_path: Path) -> None:
    """A row id the remote lists again leaves ``deleted_rows``."""
    sim = _world(tmp_path)
    row = _row(sim, PROD, HTTP_1, "Row tk2z")
    _drop_row(sim, PROD, HTTP_1, "Row tk2z")
    _pull(sim)
    sim.api.remote[PROD][HTTP_1]["rows"].append(row)

    _pull(sim)

    assert row["id"] not in (_entry(sim, "1000").metadata.get(DELETED_ROWS_KEY) or {})


# ===========================================================================
# D8 (#927): ignoring a component never deletes local work
# ===========================================================================


@pytest.mark.parametrize("mode", ["plain", "force", "theirs"])
def test_ignoring_a_component_keeps_an_edited_dir_and_its_entry(tmp_path: Path, mode: str) -> None:
    """Risk 7 / #927: the edited dir and its manifest entry stay; the unedited
    one of the same component is cleaned up. ``--theirs`` deletes nothing kept."""
    sim = _world(tmp_path)
    sim._local_edit_slot(_dir(sim, HTTP_1_DIR), "value")
    before = _entry(sim, "1000")
    _set_ignored(sim, HTTP)

    result = _pull(sim, force=mode == "force", theirs=mode == "theirs")

    assert _dir(sim, HTTP_1_DIR).is_dir()
    (kept,) = [d for d in result["details"] if d["path"] == HTTP_1_DIR]
    assert kept["action"] == "skipped"
    assert kept["reason"] == IGNORED_COMPONENT_REASON.format(component_id=HTTP)
    after = _entry(sim, "1000")
    assert after.metadata == before.metadata
    assert [(r.id, r.metadata) for r in after.rows] == [(r.id, r.metadata) for r in before.rows]
    assert not _dir(sim, HTTP_2_DIR).exists()
    assert all(c.id != "1003" for c in load_manifest(sim.root).configurations)
    assert _changes(sim) == []  # ignored: out of diff and push


def test_ignoring_a_component_keeps_a_dir_without_a_baseline(tmp_path: Path) -> None:
    """Risk 7: no ``pull_hash`` (an adopted config) is not proof of "unchanged"."""
    sim = _world(tmp_path)
    manifest = load_manifest(sim.root)
    (entry,) = [c for c in manifest.configurations if c.id == "1003"]
    entry.metadata["pull_hash"] = ""
    save_manifest(sim.root, manifest)
    _set_ignored(sim, HTTP)

    _pull(sim)

    assert _dir(sim, HTTP_2_DIR).is_dir()
    assert any(c.id == "1003" for c in load_manifest(sim.root).configurations)


def test_ignoring_a_component_keeps_an_edited_row(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    sim._local_edit_slot(_dir(sim, HTTP_2_DIR), "row:row-tk9z")
    _set_ignored(sim, HTTP)

    _pull(sim)

    assert (_dir(sim, HTTP_2_DIR) / "rows/row-tk9z/_config.yml").exists()


def test_unignore_pushes_the_kept_edit_against_its_preserved_baseline(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    sim._local_edit_slot(_dir(sim, HTTP_1_DIR), "value")
    token = read_yaml(_dir(sim, HTTP_1_DIR) / "_config.yml")["parameters"]["value"]
    _set_ignored(sim, HTTP)
    _pull(sim)
    _set_ignored(sim)

    assert ("modified", "1000") in _changes(sim)
    _push(sim)
    assert sim.api.remote[PROD][HTTP_1]["configuration"]["parameters"]["value"] == token


def test_unignore_after_a_remote_edit_is_a_conflict_not_a_new_baseline(tmp_path: Path) -> None:
    """D8: a remote change made while the component was ignored is never accepted
    as the kept dir's baseline."""
    sim = _world(tmp_path)
    sim._local_edit_slot(_dir(sim, HTTP_1_DIR), "value")
    _set_ignored(sim, HTTP)
    _pull(sim)
    sim.api.remote[PROD][HTTP_1]["configuration"]["parameters"]["value"] = "tk901z"
    _pull(sim)
    _set_ignored(sim)

    assert ("conflict", "1000") in _changes(sim)
    _push(sim)
    assert sim.api.remote[PROD][HTTP_1]["configuration"]["parameters"]["value"] == "tk901z"


def test_unignore_of_a_cleaned_component_is_no_local_delete(tmp_path: Path) -> None:
    """A cleaned dir drops its entry too: its absence is no local delete later."""
    sim = _world(tmp_path)
    _set_ignored(sim, HTTP)
    _pull(sim)
    _set_ignored(sim)

    diff = _diff(sim)

    assert not [c for c in diff["changes"] if c["change_type"] == "deleted"]
    assert {r["config_id"] for r in diff["remote_only"]} == {"1000", "1003"}


# ===========================================================================
# The walk's own invariants on the two minimal shapes (steps chosen by hand:
# remote_row_delete picks production for c < 0.8, the config by a, the row by b)
# ===========================================================================

SEED_SHAPES = {
    # pull --branch DEV -> row delete on production (1003, Row tk11z) -> pull --force -> push
    "seed-210": [
        ("pull_dev", 0.5, 0.5, 0.5),
        ("remote_row_delete", 0.9, 0.9, 0.1),
        ("pull_force", 0.5, 0.5, 0.5),
        ("push", 0.5, 0.5, 0.5),
    ],
    # row delete on production (1000, Row tk2z) -> pull -> push --branch DEV (promote)
    "seed-257": [
        ("remote_row_delete", 0.1, 0.1, 0.1),
        ("pull", 0.5, 0.5, 0.5),
        ("push_dev", 0.5, 0.5, 0.5),
    ],
    # local delete of the app dir (1009) -> description edit on production -> push --force
    "force-over-config-edit": [
        ("local_delete", 0.0, 0.0, 0.0),
        ("remote_edit", 0.0, 0.99, 0.1),
        ("push_force", 0.5, 0.5, 0.5),
    ],
    # local delete of 1003 -> edit of its Row tk11z on production -> push --force
    "force-over-row-edit": [
        ("local_delete", 0.25, 0.0, 0.0),
        ("remote_edit", 0.42, 0.99, 0.1),
        ("push_force", 0.5, 0.5, 0.5),
    ],
    # local delete of 1003 -> a row added to it on production -> push --force
    "force-over-row-add": [
        ("local_delete", 0.25, 0.0, 0.0),
        ("remote_row_add", 0.9, 0.0, 0.1),
        ("push_force", 0.5, 0.5, 0.5),
    ],
    # local delete of 1003 -> one of its rows deleted on production -> push --force
    "force-over-row-delete": [
        ("local_delete", 0.25, 0.0, 0.0),
        ("remote_row_delete", 0.9, 0.0, 0.1),
        ("push_force", 0.5, 0.5, 0.5),
    ],
}


@pytest.mark.parametrize("name", sorted(SEED_SHAPES))
def test_walk_shape_holds_every_invariant(name: str) -> None:
    from test_sync_random_walk import MASKS

    violation, log = run_walk(SEED_SHAPES[name], MASKS)
    assert violation is None, f"{name}: {violation}\n" + "\n".join(log)


def test_encrypted_only_remote_edit_is_a_documented_limit(tmp_path: Path) -> None:
    """D9 (documented, not fixed): encrypted values compare through one
    placeholder, so a remote edit of ONLY a ciphertext leaves the delete
    ``deleted``. gotchas.md says so; this pins the behaviour it describes."""
    sim = _world(tmp_path)
    params = sim.api.remote[PROD][HTTP_2]["configuration"]["parameters"]
    params["#token"] = "KBC::ProjectSecure::old"
    _pull(sim)
    params["#token"] = "KBC::ProjectSecure::new"
    shutil.rmtree(_dir(sim, HTTP_2_DIR))

    assert ("deleted", "1003") in _changes(sim)


def test_retired_rows_are_recorded_for_the_tree_a_retarget_leaves(tmp_path: Path) -> None:
    sim = _world(tmp_path)
    _pull(sim, DEV)

    retired = load_manifest(sim.root).branches[0].metadata[RETIRED_ROWS_KEY]

    assert set(retired) >= {f"{HTTP}/1003/1004", f"{HTTP}/1003/1005"}
    assert retired[f"{HTTP}/1003/1005"]["path"] == "rows/row-tk11z"
    assert retired[f"{HTTP}/1003/1005"]["pull_hash"]


def test_kept_ignored_entry_survives_write_yaml_roundtrip(tmp_path: Path) -> None:
    """The kept entry's files stay byte-identical (the sweep never rewrites them)."""
    sim = _world(tmp_path)
    cfg = _dir(sim, HTTP_1_DIR) / "_config.yml"
    data = read_yaml(cfg)
    data["parameters"]["value"] = "tk902z"
    write_yaml(cfg, data)
    text = cfg.read_text()
    _set_ignored(sim, HTTP)

    _pull(sim)

    assert cfg.read_text() == text


# ===========================================================================
# Round 2 (review findings)
# ===========================================================================


def test_ignoring_keeps_a_baseline_free_row_under_a_parent_without_its_file(
    tmp_path: Path,
) -> None:
    """P1 (#927 hole): a parent with no baseline and no ``_config.yml`` must not
    hide a surviving row that has no baseline either -- the row is never
    "proven unchanged", so the ignored sweep keeps it."""
    sim = _world(tmp_path)
    manifest = load_manifest(sim.root)
    (entry,) = [c for c in manifest.configurations if c.id == "1003"]
    entry.metadata = {}
    for row in entry.rows:
        row.metadata = {}
    save_manifest(sim.root, manifest)
    (_dir(sim, HTTP_2_DIR) / "_config.yml").unlink()
    (_dir(sim, HTTP_2_DIR) / "_description.md").unlink(missing_ok=True)
    row_file = _dir(sim, f"{HTTP_2_DIR}/rows/row-tk11z") / "_config.yml"
    row_file.write_text(row_file.read_text().replace("tk12z", "tk900z"), encoding="utf-8")
    _set_ignored(sim, HTTP)

    result = _pull(sim)

    assert "tk900z" in row_file.read_text()
    assert any(d["path"] == HTTP_2_DIR and d["action"] == "skipped" for d in result["details"])
    assert any(c.id == "1003" for c in load_manifest(sim.root).configurations)


def test_promote_shadow_keeps_a_deferred_source_row_deletion(tmp_path: Path) -> None:
    """P2 (D7): a promote update of the parent creates its DEV shadow while the
    row deletion is held back; the shadow must not hide that deletion, so the
    forced retry deletes the dev row."""
    sim = _world(tmp_path)
    row_id = _drop_row(sim, PROD, HTTP_1, "Row tk2z")
    _pull(sim)
    sim._local_edit_slot(_dir(sim, HTTP_1_DIR), "value")

    first = _push(sim, DEV)
    assert first["updated"] == 1
    assert [d["config_id"] for d in first["skipped_deletions"]] == [row_id]
    assert any(
        c.id == "1000" and c.metadata.get("promoted_from")
        for c in load_manifest(sim.root).configurations
        if (c.branch_id or PROD) == DEV
    ), "the update recorded no DEV shadow -- the test premise is gone"

    assert ("deleted", "1000") in _changes(sim, DEV)
    forced = _push(sim, DEV, force=True)

    assert forced["errors"] == []
    assert all(r["id"] != row_id for r in sim.api.remote[DEV][HTTP_1]["rows"])


def test_push_result_carries_each_refused_deletion_with_its_message(tmp_path: Path) -> None:
    """D4: the actionable message reaches the push result, not only diff."""
    sim = _world(tmp_path)
    shutil.rmtree(_dir(sim, SQL_1_DIR))
    params = sim.api.remote[PROD][SQL_1]["configuration"]["parameters"]
    params["blocks"][0]["codes"][0]["script"] = ["SELECT 'tk900z';"]

    for result in (_push(sim, force=True), _push(sim, force=True, dry_run=True), _push(sim)):
        (refused,) = result["refused_deletions"]
        assert (refused["component_id"], refused["config_id"]) == (SQL, "1006")
        assert refused["message"].startswith(f"Cannot delete {SQL}/1006 on branch production")
        assert FORCE_DOES_NOT_OVERRIDE in refused["message"]
        assert "kbagent sync pull" in refused["message"]


def test_push_human_output_prints_the_refusal_message() -> None:
    from unittest.mock import MagicMock

    from keboola_agent_cli.commands._sync_push_render import print_push_skips

    formatter = MagicMock()
    print_push_skips(
        formatter,
        {"refused_deletions": [{"component_id": SQL, "config_id": "1006", "message": "Cannot X"}]},
    )
    printed = " ".join(str(c.args[0]) for c in formatter.err_console.print.call_args_list)
    assert "Cannot X" in printed


def test_remote_row_added_under_an_adopted_config_is_remote_only(tmp_path: Path) -> None:
    """D5: an adopted dir (no manifest entry) says nothing about a remote row no
    local dir carries -- it is a remote-only row, not a false deletion conflict."""
    sim = _world(tmp_path)
    manifest = load_manifest(sim.root)
    manifest.configurations = [c for c in manifest.configurations if c.id != "1003"]
    save_manifest(sim.root, manifest)
    row = sim._remote_row("Row extra")
    sim.api.remote[PROD][HTTP_2]["rows"].append(row)

    diff = _diff(sim)

    assert [r["row_id"] for r in diff["remote_only_rows"]] == [row["id"]]
    assert not [c for c in diff["changes"] if c["config_id"] == row["id"]]
    _push(sim, force=True)
    assert any(r["id"] == row["id"] for r in sim.api.remote[PROD][HTTP_2]["rows"])
