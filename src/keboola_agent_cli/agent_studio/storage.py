"""YAML-backed persistence for Agent Studio Playbooks.

Each Playbook lives in its own ``<id>.yaml`` under
``<config_dir>/playbooks/`` with ``0600`` permissions, matching the
pattern in `config_store.py` (tokens get the same treatment because
the same dir holds AI CLI credentials).

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
from pydantic import ValidationError

from .models.playbook import Playbook, PlaybookSummary

PLAYBOOKS_DIRNAME = "playbooks"
FILE_MODE = 0o600


def playbooks_dir(config_dir: Path) -> Path:
    """Resolve the on-disk Playbooks directory, creating it if missing.

    The directory itself is mode ``0700`` so per-file ``0600`` is not
    bypassed by a chmod-ed enclosing dir.
    """

    path = config_dir / PLAYBOOKS_DIRNAME
    path.mkdir(mode=0o700, exist_ok=True, parents=True)
    return path


def new_playbook_id() -> str:
    """Random UUID4 hex; short, URL-safe, human-typable in CLI."""

    return uuid.uuid4().hex


def now() -> datetime:
    """UTC timestamp helper — keeps test fixtures deterministic via
    monkeypatch."""

    return datetime.now(tz=UTC)


def list_playbooks(config_dir: Path) -> list[PlaybookSummary]:
    """Return library projections for every readable Playbook.

    Files that fail to parse are silently skipped — they show up in the
    server log instead of taking down the library page. The library is
    a read-only surface and a corrupt YAML is a degraded-state
    condition we want to *see*, not a fatal one.
    """

    directory = playbooks_dir(config_dir)
    summaries: list[PlaybookSummary] = []
    for yaml_path in sorted(directory.glob("*.yaml")):
        playbook = _safe_load(yaml_path)
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
    return _safe_load(path)


def save_playbook(config_dir: Path, playbook: Playbook) -> Playbook:
    """Write a Playbook to disk with ``0600`` perms.

    ``updated_at`` is stamped on every save; the caller is expected to
    have already filled ``created_at`` for new records (the router
    enforces this).
    """

    playbook = playbook.model_copy(update={"updated_at": now()})

    path = playbooks_dir(config_dir) / f"{playbook.id}.yaml"
    # Serialise via Pydantic so ``datetime`` -> ISO 8601 happens for free.
    # ``model_dump(mode="json")`` returns JSON-compatible types which
    # PyYAML then writes as YAML scalars.
    payload = playbook.model_dump(mode="json")

    # Atomic-ish write: tmp file + rename. Same pattern as ConfigStore
    # to avoid leaving a partial YAML on disk when the process is
    # killed mid-write.
    tmp_path = path.with_suffix(".yaml.tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, sort_keys=False, allow_unicode=True)
    os.chmod(tmp_path, FILE_MODE)
    os.replace(tmp_path, path)
    return playbook


def delete_playbook(config_dir: Path, playbook_id: str) -> bool:
    """Remove the Playbook file. ``False`` when nothing was deleted."""

    path = playbooks_dir(config_dir) / f"{playbook_id}.yaml"
    if not path.is_file():
        return False
    path.unlink()
    return True


def _safe_load(path: Path) -> Playbook | None:
    """Parse one YAML file; ``None`` on any malformed input.

    We deliberately swallow ``yaml.YAMLError`` and Pydantic
    ``ValidationError`` here: the library view should still render
    when one Playbook out of fifty has a broken field. Look at
    ``kbagent serve``'s log for the per-file error.
    """

    try:
        with open(path, encoding="utf-8") as fh:
            payload = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return Playbook.model_validate(payload)
    except ValidationError:
        return None
