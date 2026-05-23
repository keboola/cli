"""Playbook entity — the top-level unit of Agent Studio.

A Playbook bundles an SOP, a chosen set of Connections/Skills/Plugins,
the secrets it may touch (Logins), the events that launch it
(Triggers), a Budget cap, and an Approval policy. See `docs/agents-v2.md`
§ 5 for the mental model and § 7 for the full Pydantic surface this
file gradually fills in.

Phase 1 ships a deliberately minimal shape: enough fields to render
the Playbook Library card from `docs/mockups/01-playbooks-library.png`
backed by real YAML, *without* committing the format to the heavier
Budget / Approval / Tool Broker substructures we have not yet
implemented. Those land in their own slices and are added as optional
nested models so existing on-disk YAMLs keep parsing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# Statuses extend `server/agents_store.py:AgentRun` per `docs/agents-v2.md`
# § 23 Migration. The enum lives next to the Playbook model because the
# run state is read by every UI surface that lists Playbooks. Phase 1
# only emits ``draft`` and ``scheduled``; live-run statuses (running /
# waiting_for_approval / done / failed / cancelled) come online when the
# run loop wires up.
PlaybookRunStatus = Literal[
    "draft",
    "scheduled",
    "queued",
    "running",
    "blocked",
    "waiting_for_approval",
    "reviewing",
    "done",
    "failed",
    "cancelled",
]


class Playbook(BaseModel):
    """One workflow definition — persisted as YAML.

    The fields below mirror the *required* subset of `docs/agents-v2.md`
    § 7 ``Playbook``. Fields the PRD lists but Phase 1 cannot yet
    enforce (``budget``, ``approval_policy``, full ``sop`` shape) are
    intentionally absent — adding them as ``None``-defaulted attributes
    later is a non-breaking change because we never serialise ``None``
    keys.
    """

    id: str = Field(..., description="UUID4 hex string; client-generated.")
    name: str = Field(..., description="Human-readable Playbook name.")
    description: str | None = Field(
        default=None,
        description="Short blurb shown on the Library card under the title.",
    )
    revision: int = Field(default=1, ge=1, description="Active revision number.")
    enabled: bool = Field(default=True, description="Whether the Playbook may run.")

    # Phase-1 placeholders. The full shapes are defined per § 7 of the
    # v2 PRD but we accept opaque lists here so on-disk YAMLs can carry
    # forward-looking data without breaking the loader. Later slices
    # narrow these to concrete typed models.
    connections: list[str] = Field(
        default_factory=list,
        description="Connection IDs this Playbook is allowed to touch.",
    )
    skills: list[str] = Field(
        default_factory=list,
        description="Skill IDs staged into the run context.",
    )
    plugins: list[str] = Field(
        default_factory=list,
        description="Plugin IDs (bundles of connections + skills + tools).",
    )
    triggers: list[dict] = Field(
        default_factory=list,
        description="Raw trigger configs; typed in Phase 2.",
    )

    # Cosmetic / library-card fields. ``status`` is denormalised onto
    # the Playbook (rather than computed from the latest PlaybookRun)
    # so the library does not have to fan out N queries per page load.
    # When the run loop lands it updates this field as a side effect.
    status: PlaybookRunStatus = Field(
        default="draft",
        description="Current high-level state shown on the Library card.",
    )

    created_at: datetime
    updated_at: datetime


class PlaybookSummary(BaseModel):
    """Lighter projection used by the library list endpoint.

    The full Playbook (with triggers / connections / etc.) is loaded
    only when the user opens a single card. Listing 50 Playbooks
    should not pay for 50x full deserialisation.
    """

    id: str
    name: str
    description: str | None = None
    revision: int
    enabled: bool
    status: PlaybookRunStatus
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_playbook(cls, playbook: Playbook) -> PlaybookSummary:
        return cls(
            id=playbook.id,
            name=playbook.name,
            description=playbook.description,
            revision=playbook.revision,
            enabled=playbook.enabled,
            status=playbook.status,
            created_at=playbook.created_at,
            updated_at=playbook.updated_at,
        )
