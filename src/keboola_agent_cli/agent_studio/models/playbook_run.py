"""One execution of a Playbook.

In v2 PRD § 7 ``PlaybookRun`` carries a denormalised cost ledger, an
SSE event log path, a workspace path, an approval log path, etc. The
Phase 1 stub omits all of that — its only job is to prove the
"library card -> Run button -> new run record appears" data flow
end-to-end. Slice 2.b replaces the stub with a real subprocess
invocation tied to ``server/agent_runner.py``; the new fields land
then.

Persisting runs separately from Playbooks (rather than as a list
nested inside the Playbook YAML) is deliberate per § 7: it lets us
GC / archive old runs without rewriting the Playbook, and it
mirrors how ``AgentRun`` lives next to ``AgentTask`` in
``server/agents_store.py``.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .playbook import PlaybookRunStatus


class PlaybookRun(BaseModel):
    """One execution attempt of one Playbook revision.

    Phase 1 ships only the fields needed to render the Recent Runs
    list in the Playbook detail Drawer. Cost, token, workspace, and
    SSE-log fields arrive in slice 2.b.
    """

    id: str = Field(..., description="UUID4 hex; server-issued.")
    playbook_id: str = Field(..., description="ID of the Playbook this run came from.")
    playbook_revision: int = Field(
        ..., ge=1, description="Revision of the Playbook at run-start time."
    )
    status: PlaybookRunStatus = Field(default="queued")
    started_at: datetime
    ended_at: datetime | None = None
    summary: str | None = Field(
        default=None,
        description=(
            "Short human note about the run -- 'stub completed', "
            "'aborted by user', etc. The Phase-1 stub fills this with "
            "a fixed string so the UI has something to display."
        ),
    )
    objective_override: str | None = Field(
        default=None,
        description=(
            "Optional one-off objective passed at run-start. Mirrors "
            "the AgentTask 'ai_agent' manual-run pattern; lets the "
            "operator tell the Playbook to focus on a single thing."
        ),
    )
