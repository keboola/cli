"""Tests for the Phase-1 Playbook Pydantic model.

Targets the slice described in `docs/agent-studio-progress.md`: the
minimal-but-non-breaking shape (id / name / description / revision /
enabled / status / timestamps + opaque placeholders for
connections/skills/plugins/triggers).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from keboola_agent_cli.agent_studio.models.playbook import (
    Playbook,
    PlaybookSummary,
)


def _ts() -> datetime:
    return datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)


def test_playbook_minimal_required_fields() -> None:
    pb = Playbook(
        id="abc123",
        name="Cross-source CRM Cleanup",
        created_at=_ts(),
        updated_at=_ts(),
    )
    assert pb.revision == 1
    assert pb.enabled is True
    assert pb.status == "draft"
    assert pb.connections == []
    assert pb.skills == []
    assert pb.plugins == []
    assert pb.triggers == []
    assert pb.description is None


def test_playbook_rejects_revision_below_one() -> None:
    with pytest.raises(ValidationError):
        Playbook(
            id="abc",
            name="X",
            revision=0,
            created_at=_ts(),
            updated_at=_ts(),
        )


def test_playbook_status_enum_rejects_garbage() -> None:
    with pytest.raises(ValidationError):
        Playbook(
            id="abc",
            name="X",
            status="utterly-broken",  # type: ignore[arg-type]
            created_at=_ts(),
            updated_at=_ts(),
        )


def test_playbook_accepts_known_statuses() -> None:
    for status_value in (
        "draft",
        "scheduled",
        "running",
        "blocked",
        "waiting_for_approval",
        "done",
        "failed",
        "cancelled",
    ):
        pb = Playbook(
            id="abc",
            name="X",
            status=status_value,  # type: ignore[arg-type]
            created_at=_ts(),
            updated_at=_ts(),
        )
        assert pb.status == status_value


def test_playbook_carries_opaque_trigger_dicts() -> None:
    """Phase-1 stores raw trigger configs without typing them; the
    later slice that introduces the Trigger model has to keep this
    forward-compatible."""

    pb = Playbook(
        id="abc",
        name="X",
        triggers=[
            {"type": "cron", "config": {"expression": "0 6 * * 1"}},
            {"type": "manual", "config": {}},
        ],
        created_at=_ts(),
        updated_at=_ts(),
    )
    assert len(pb.triggers) == 2
    assert pb.triggers[0]["type"] == "cron"


def test_summary_projects_required_fields() -> None:
    pb = Playbook(
        id="abc",
        name="X",
        description="A description.",
        revision=3,
        enabled=False,
        status="scheduled",
        connections=["keboola.connection", "slack"],  # dropped in summary
        created_at=_ts(),
        updated_at=_ts(),
    )
    summary = PlaybookSummary.from_playbook(pb)
    assert summary.id == "abc"
    assert summary.name == "X"
    assert summary.description == "A description."
    assert summary.revision == 3
    assert summary.enabled is False
    assert summary.status == "scheduled"
    # The summary intentionally omits connections / skills / plugins /
    # triggers — library callers must not depend on them.
    assert not hasattr(summary, "connections")
