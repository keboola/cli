"""Agent Studio runtime — Playbooks, Skills, Connections, Tool Broker.

Phase 1 ships only the Playbook entity + YAML persistence + a read-only
HTTP surface so the React UI can render a Playbook library backed by
real disk state. Everything else from
`docs/agents-v2.md` § 21 Phase 1 (Tool Broker, scoped JWTs, budget
enforcer, approval queue, untrusted wrapping, skill loader,
connection auto-discovery) lands in subsequent vertical slices.

The module is optional: nothing in core `kbagent` imports from
`agent_studio` directly. The server wires the router on startup only
when the module is present, mirroring the "Agent Studio is an optional
extra" rule from ADR 0001.
"""

from __future__ import annotations

from .models.playbook import Playbook, PlaybookSummary

__all__ = ["Playbook", "PlaybookSummary"]
