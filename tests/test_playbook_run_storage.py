"""Tests for PlaybookRun YAML storage.

Lives next to ``test_playbook_storage.py`` rather than inside it so a
failure in the run storage doesn't drag the playbook storage suite
down with it (and vice versa).
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from keboola_agent_cli.agent_studio.models.playbook_run import PlaybookRun
from keboola_agent_cli.agent_studio.storage import (
    RUNS_DIRNAME,
    get_run,
    list_runs,
    runs_dir,
    save_run,
)


def _ts(offset_minutes: int = 0) -> datetime:
    base = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    return base + timedelta(minutes=offset_minutes)


def _make_run(
    run_id: str = "r1",
    playbook_id: str = "p1",
    started_offset: int = 0,
) -> PlaybookRun:
    return PlaybookRun(
        id=run_id,
        playbook_id=playbook_id,
        playbook_revision=1,
        status="done",
        started_at=_ts(started_offset),
        ended_at=_ts(started_offset),
        summary="stub",
    )


def test_runs_dir_created_with_strict_perms(tmp_config_dir: Path) -> None:
    rd = runs_dir(tmp_config_dir)
    assert rd == tmp_config_dir / RUNS_DIRNAME
    assert rd.is_dir()
    mode = stat.S_IMODE(os.stat(rd).st_mode)
    assert mode == 0o700


def test_save_round_trips_run(tmp_config_dir: Path) -> None:
    saved = save_run(tmp_config_dir, _make_run("r_round"))
    loaded = get_run(tmp_config_dir, "r_round")
    assert loaded is not None
    assert loaded.id == saved.id
    assert loaded.playbook_id == saved.playbook_id


def test_save_writes_0600_permissions(tmp_config_dir: Path) -> None:
    save_run(tmp_config_dir, _make_run("r_perms"))
    yaml_path = tmp_config_dir / RUNS_DIRNAME / "r_perms.yaml"
    mode = stat.S_IMODE(os.stat(yaml_path).st_mode)
    assert mode == 0o600


def test_list_runs_returns_newest_first(tmp_config_dir: Path) -> None:
    save_run(tmp_config_dir, _make_run("r_old", started_offset=0))
    save_run(tmp_config_dir, _make_run("r_mid", started_offset=5))
    save_run(tmp_config_dir, _make_run("r_new", started_offset=10))

    runs = list_runs(tmp_config_dir)
    assert [r.id for r in runs] == ["r_new", "r_mid", "r_old"]


def test_list_runs_filters_by_playbook_id(tmp_config_dir: Path) -> None:
    save_run(tmp_config_dir, _make_run("r_a", playbook_id="p_a"))
    save_run(tmp_config_dir, _make_run("r_b", playbook_id="p_b"))
    save_run(
        tmp_config_dir,
        _make_run("r_a2", playbook_id="p_a", started_offset=10),
    )

    only_a = list_runs(tmp_config_dir, playbook_id="p_a")
    assert {r.id for r in only_a} == {"r_a", "r_a2"}
    assert all(r.playbook_id == "p_a" for r in only_a)

    only_b = list_runs(tmp_config_dir, playbook_id="p_b")
    assert [r.id for r in only_b] == ["r_b"]


def test_get_run_returns_none_for_missing(tmp_config_dir: Path) -> None:
    assert get_run(tmp_config_dir, "ghost") is None


def test_list_runs_skips_corrupt_yaml(tmp_config_dir: Path) -> None:
    save_run(tmp_config_dir, _make_run("r_ok"))
    (runs_dir(tmp_config_dir) / "broken.yaml").write_text("{not valid: yaml: at all")
    runs = list_runs(tmp_config_dir)
    assert [r.id for r in runs] == ["r_ok"]
