"""YAML-backed persistence for Agent Studio Playbooks + PlaybookRuns.

Each Playbook lives in its own ``<id>.yaml`` under
``<config_dir>/playbooks/`` and each PlaybookRun under
``<config_dir>/runs/``, both with ``0600`` permissions. The two dirs
are siblings of ``config.json``, matching how ``ConfigStore`` writes
tokens (the same dir holds AI CLI credentials).

This module is intentionally I/O-only — it does **not** import from
the FastAPI layer. The router calls these functions inside its
handlers. That separation keeps storage testable without spinning up
the HTTP stack and lets the future CLI surface (`kbagent playbook
list`, etc.) reuse the same primitives.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from .models.playbook import Playbook, PlaybookSummary
from .models.playbook_run import PlaybookRun

PLAYBOOKS_DIRNAME = "playbooks"
RUNS_DIRNAME = "runs"
FILE_MODE = 0o600


# ── shared helpers ──────────────────────────────────────────────────


def new_id() -> str:
    """Random UUID4 hex; short, URL-safe, human-typable in CLI."""

    return uuid.uuid4().hex


# Back-compat alias retained for tests that target the original name.
new_playbook_id = new_id


def now() -> datetime:
    """UTC timestamp helper — keeps test fixtures deterministic via
    monkeypatch."""

    return datetime.now(tz=UTC)


def _dir(config_dir: Path, name: str) -> Path:
    """Resolve a sibling directory under the config dir, creating it
    with mode ``0700`` so per-file ``0600`` is not bypassed by a
    chmod-ed enclosing dir."""

    path = config_dir / name
    path.mkdir(mode=0o700, exist_ok=True, parents=True)
    return path


def _atomic_write_yaml(path: Path, payload: dict) -> None:
    """Tmp-file + rename pattern. ConfigStore uses the same dance to
    avoid leaving a partial YAML on disk when the process is killed
    mid-write."""

    tmp_path = path.with_suffix(".yaml.tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, sort_keys=False, allow_unicode=True)
    os.chmod(tmp_path, FILE_MODE)
    os.replace(tmp_path, path)


def _safe_load[T: BaseModel](path: Path, model: type[T]) -> T | None:
    """Parse one YAML file into the given Pydantic model. ``None`` on
    any malformed input: an unreadable file, broken YAML, or
    ``ValidationError`` from Pydantic.

    Swallowing these errors here is deliberate — the library view
    should still render when one Playbook out of fifty has a broken
    field. Look at ``kbagent serve``'s log for the per-file error.
    """

    try:
        with open(path, encoding="utf-8") as fh:
            payload = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return model.model_validate(payload)
    except ValidationError:
        return None


# ── Playbook CRUD ───────────────────────────────────────────────────


def playbooks_dir(config_dir: Path) -> Path:
    return _dir(config_dir, PLAYBOOKS_DIRNAME)


def list_playbooks(config_dir: Path) -> list[PlaybookSummary]:
    """Return library projections for every readable Playbook."""

    directory = playbooks_dir(config_dir)
    summaries: list[PlaybookSummary] = []
    for yaml_path in sorted(directory.glob("*.yaml")):
        playbook = _safe_load(yaml_path, Playbook)
        if playbook is not None:
            summaries.append(PlaybookSummary.from_playbook(playbook))
    return summaries


def get_playbook(config_dir: Path, playbook_id: str) -> Playbook | None:
    """Read one Playbook by ID. ``None`` when not found / unparseable.

    The caller (FastAPI router) translates ``None`` into a 404; we do
    not raise here so the storage layer stays HTTP-agnostic.
    """

    path = playbooks_dir(config_dir) / f"{playbook_id}.yaml"
    if not path.is_file():
        return None
    return _safe_load(path, Playbook)


def save_playbook(config_dir: Path, playbook: Playbook) -> Playbook:
    """Write a Playbook to disk with ``0600`` perms.

    ``updated_at`` is stamped on every save; the caller is expected to
    have already filled ``created_at`` for new records (the router
    enforces this).
    """

    playbook = playbook.model_copy(update={"updated_at": now()})
    path = playbooks_dir(config_dir) / f"{playbook.id}.yaml"
    _atomic_write_yaml(path, playbook.model_dump(mode="json"))
    return playbook


def delete_playbook(config_dir: Path, playbook_id: str) -> bool:
    """Remove the Playbook file. ``False`` when nothing was deleted."""

    path = playbooks_dir(config_dir) / f"{playbook_id}.yaml"
    if not path.is_file():
        return False
    path.unlink()
    return True


# ── PlaybookRun CRUD ────────────────────────────────────────────────


def runs_dir(config_dir: Path) -> Path:
    return _dir(config_dir, RUNS_DIRNAME)


def list_runs(config_dir: Path, *, playbook_id: str | None = None) -> list[PlaybookRun]:
    """Return every readable PlaybookRun, optionally filtered to one
    Playbook. Sorted newest-first by ``started_at`` so the UI can
    truncate to "last 5" without resorting.
    """

    directory = runs_dir(config_dir)
    runs: list[PlaybookRun] = []
    for yaml_path in directory.glob("*.yaml"):
        run = _safe_load(yaml_path, PlaybookRun)
        if run is None:
            continue
        if playbook_id is not None and run.playbook_id != playbook_id:
            continue
        runs.append(run)
    runs.sort(key=lambda r: r.started_at, reverse=True)
    return runs


def get_run(config_dir: Path, run_id: str) -> PlaybookRun | None:
    path = runs_dir(config_dir) / f"{run_id}.yaml"
    if not path.is_file():
        return None
    return _safe_load(path, PlaybookRun)


def save_run(config_dir: Path, run: PlaybookRun) -> PlaybookRun:
    """Write a PlaybookRun to disk with ``0600`` perms.

    Unlike ``save_playbook`` we do not re-stamp ``ended_at`` — the
    run lifecycle owns that field, not the storage layer.
    """

    path = runs_dir(config_dir) / f"{run.id}.yaml"
    _atomic_write_yaml(path, run.model_dump(mode="json"))
    return run
