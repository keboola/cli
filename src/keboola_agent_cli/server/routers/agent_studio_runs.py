"""HTTP surface for Agent Studio Playbook runs.

The runs router lives separately from
``agent_studio_playbooks`` because its prefix is different
(``/v1/agent-studio/runs`` vs. ``/v1/agent-studio/playbooks``) and
the path that *starts* a run still belongs on the Playbook (its
URL says "run this Playbook"). Both paths land on the same on-disk
``runs/`` directory through ``agent_studio.storage``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ...agent_studio.models.playbook_run import PlaybookRun
from ...agent_studio.storage import get_run, list_runs
from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(
    prefix="/v1/agent-studio/runs",
    tags=["agent-studio"],
)


@router.get("", summary="List PlaybookRuns")
def list_route(
    playbook_id: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, list[PlaybookRun]]:
    """Newest-first list of every PlaybookRun on disk.

    Pass ``?playbook_id=<id>`` to scope to one Playbook's run history
    (this is what the detail Drawer uses). Returns ``{"runs": [...]}``
    so the envelope can grow paging metadata later without a breaking
    change.
    """

    runs = list_runs(registry.config_store.config_dir, playbook_id=playbook_id)
    return {"runs": runs}


@router.get("/{run_id}", summary="Get one PlaybookRun")
def get_route(
    run_id: str,
    registry: ServiceRegistry = Depends(get_registry),
) -> PlaybookRun:
    run = get_run(registry.config_store.config_dir, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="PlaybookRun not found.")
    return run
