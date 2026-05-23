"""Tests for Playbook YAML storage.

Uses the shared ``tmp_config_dir`` fixture from ``conftest.py`` so the
filesystem layout matches what the rest of the codebase exercises.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from keboola_agent_cli.agent_studio.models.playbook import Playbook
from keboola_agent_cli.agent_studio.storage import (
    PLAYBOOKS_DIRNAME,
    delete_playbook,
    get_playbook,
    list_playbooks,
    new_playbook_id,
    playbooks_dir,
    save_playbook,
)


def _make_playbook(playbook_id: str = "pb_test", name: str = "X") -> Playbook:
    ts = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    return Playbook(id=playbook_id, name=name, created_at=ts, updated_at=ts)


def test_new_playbook_id_is_hex_uuid() -> None:
    pid = new_playbook_id()
    assert len(pid) == 32
    assert all(c in "0123456789abcdef" for c in pid)


def test_playbooks_dir_created_with_strict_perms(tmp_config_dir: Path) -> None:
    pb_dir = playbooks_dir(tmp_config_dir)
    assert pb_dir == tmp_config_dir / PLAYBOOKS_DIRNAME
    assert pb_dir.is_dir()
    mode = stat.S_IMODE(os.stat(pb_dir).st_mode)
    assert mode == 0o700


def test_save_round_trips_playbook(tmp_config_dir: Path) -> None:
    pb = _make_playbook("pb_round")
    save_playbook(tmp_config_dir, pb)

    loaded = get_playbook(tmp_config_dir, "pb_round")
    assert loaded is not None
    assert loaded.id == pb.id
    assert loaded.name == pb.name
    # ``updated_at`` is stamped at save time so the equality check is
    # by structure, not by full equality with the pre-save instance.
    assert loaded.updated_at >= pb.updated_at


def test_save_writes_0600_permissions(tmp_config_dir: Path) -> None:
    save_playbook(tmp_config_dir, _make_playbook("pb_perms"))
    yaml_path = tmp_config_dir / PLAYBOOKS_DIRNAME / "pb_perms.yaml"
    mode = stat.S_IMODE(os.stat(yaml_path).st_mode)
    assert mode == 0o600


def test_save_is_atomic_via_temp_then_rename(tmp_config_dir: Path) -> None:
    """If a ``.yaml.tmp`` survives, the next save still succeeds and
    no half-written file is left around. The implementation uses
    ``os.replace`` which is atomic on POSIX."""

    save_playbook(tmp_config_dir, _make_playbook("pb_atom", "first"))
    save_playbook(tmp_config_dir, _make_playbook("pb_atom", "second"))

    pb_dir = tmp_config_dir / PLAYBOOKS_DIRNAME
    survivors = sorted(p.name for p in pb_dir.iterdir())
    assert survivors == ["pb_atom.yaml"]
    assert "first" not in (pb_dir / "pb_atom.yaml").read_text()
    assert "second" in (pb_dir / "pb_atom.yaml").read_text()


def test_list_skips_corrupt_yaml(tmp_config_dir: Path) -> None:
    save_playbook(tmp_config_dir, _make_playbook("pb_ok", "Healthy"))
    pb_dir = playbooks_dir(tmp_config_dir)
    (pb_dir / "broken.yaml").write_text("{not valid: yaml: at all")
    (pb_dir / "missing-fields.yaml").write_text(
        yaml.safe_dump({"id": "x"})
    )  # Pydantic should reject — no name, no timestamps.

    summaries = list_playbooks(tmp_config_dir)
    assert [s.id for s in summaries] == ["pb_ok"]


def test_list_sorts_alphabetically(tmp_config_dir: Path) -> None:
    """Stable ordering keeps the library UI from flickering between
    requests when nothing changed on disk."""

    for pid in ("pb_zeta", "pb_alpha", "pb_mid"):
        save_playbook(tmp_config_dir, _make_playbook(pid, pid))
    summaries = list_playbooks(tmp_config_dir)
    assert [s.id for s in summaries] == ["pb_alpha", "pb_mid", "pb_zeta"]


def test_get_returns_none_for_missing(tmp_config_dir: Path) -> None:
    assert get_playbook(tmp_config_dir, "does-not-exist") is None


def test_delete_returns_false_when_missing(tmp_config_dir: Path) -> None:
    assert delete_playbook(tmp_config_dir, "missing") is False


def test_delete_removes_file(tmp_config_dir: Path) -> None:
    save_playbook(tmp_config_dir, _make_playbook("pb_kill"))
    assert delete_playbook(tmp_config_dir, "pb_kill") is True
    assert get_playbook(tmp_config_dir, "pb_kill") is None


def test_save_serialises_datetime_as_iso8601(tmp_config_dir: Path) -> None:
    """YAML on disk must be plain text so users can `cat` it; binary
    pickling of datetimes is a non-starter."""

    save_playbook(tmp_config_dir, _make_playbook("pb_iso"))
    raw = (tmp_config_dir / PLAYBOOKS_DIRNAME / "pb_iso.yaml").read_text()
    assert "2026" in raw  # ISO timestamp is present, regardless of
    # exact format (YAML may emit T-separator or space).


@pytest.fixture
def two_saved_playbooks(tmp_config_dir: Path) -> Path:
    save_playbook(tmp_config_dir, _make_playbook("pb_one", "One"))
    save_playbook(tmp_config_dir, _make_playbook("pb_two", "Two"))
    return tmp_config_dir


def test_round_trip_full_then_summary(two_saved_playbooks: Path) -> None:
    summaries = list_playbooks(two_saved_playbooks)
    assert {s.id for s in summaries} == {"pb_one", "pb_two"}
    full = get_playbook(two_saved_playbooks, "pb_one")
    assert full is not None
    assert full.name == "One"
