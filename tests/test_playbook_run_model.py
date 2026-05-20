"""Tests for the Phase-1 PlaybookRun model."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from keboola_agent_cli.agent_studio.models.playbook_run import PlaybookRun


def _ts() -> datetime:
    return datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)


def test_minimal_required_fields() -> None:
    run = PlaybookRun(
        id="r1",
        playbook_id="p1",
        playbook_revision=1,
        started_at=_ts(),
    )
    assert run.status == "queued"
    assert run.ended_at is None
    assert run.summary is None
    assert run.objective_override is None


def test_rejects_revision_below_one() -> None:
    with pytest.raises(ValidationError):
        PlaybookRun(
            id="r1",
            playbook_id="p1",
            playbook_revision=0,
            started_at=_ts(),
        )


def test_status_enum_rejects_garbage() -> None:
    with pytest.raises(ValidationError):
        PlaybookRun(
            id="r1",
            playbook_id="p1",
            playbook_revision=1,
            status="exploded",  # type: ignore[arg-type]
            started_at=_ts(),
        )


def test_carries_optional_objective_override() -> None:
    run = PlaybookRun(
        id="r1",
        playbook_id="p1",
        playbook_revision=2,
        started_at=_ts(),
        objective_override="Only run on yesterday's deductions, not the full backlog.",
    )
    assert run.objective_override is not None
    assert "yesterday" in run.objective_override
