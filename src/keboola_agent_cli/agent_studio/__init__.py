"""Agent Studio runtime — Playbooks, Skills, Connections, Tool Broker.

Phase 1 ships the Playbook entity + YAML persistence + a read-only
HTTP surface so the React UI can render a Playbook library backed by
real disk state, plus a stub PlaybookRun so the "Run" button in the
detail Drawer has a place to land. Real subprocess execution wires
into ``server/agent_runner.py`` in slice 2.b.

Everything else from `docs/agents-v2.md` § 21 Phase 1 (Tool Broker,
scoped JWTs, budget enforcer, approval queue, untrusted wrapping,
skill loader, connection auto-discovery) lands in subsequent vertical
slices.

The module is optional: nothing in core `kbagent` imports from
`agent_studio` directly. The server wires the router on startup only
when the module is present, mirroring the "Agent Studio is an optional
extra" rule from ADR 0001.
"""

from __future__ import annotations

from .models.playbook import Playbook, PlaybookSummary
from .models.playbook_run import PlaybookRun

__all__ = ["Playbook", "PlaybookRun", "PlaybookSummary"]
