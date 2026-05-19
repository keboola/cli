"""HTTP surface for Agent Studio Playbooks.

Read-mostly router for Phase 1: the React UI lists Playbooks, opens
one, and (optionally) creates a new draft. Run, delete-all, and
revision endpoints arrive in later slices.

The path prefix is ``/v1/agent-studio/playbooks`` per `docs/agents-v2.md`
§ 19.2. Auth is the same bearer-token model as every other router —
no special handling here.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from ...agent_studio.models.playbook import Playbook, PlaybookSummary
from ...agent_studio.storage import (
    delete_playbook,
    get_playbook,
    list_playbooks,
    new_playbook_id,
    now,
    save_playbook,
)
from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(
    prefix="/v1/agent-studio/playbooks",
    tags=["agent-studio"],
)


@router.get("", summary="List Playbooks (library projection)")
def list_route(
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, list[PlaybookSummary]]:
    """Lightweight projection used by the Playbook Library page.

    Returns ``{"playbooks": [...]}`` so the React side can extend the
    envelope with paging metadata later without a breaking change.
    """

    summaries = list_playbooks(registry.config_store.config_dir)
    return {"playbooks": summaries}


@router.get("/{playbook_id}", summary="Get one Playbook with full body")
def get_route(
    playbook_id: str,
    registry: ServiceRegistry = Depends(get_registry),
) -> Playbook:
    playbook = get_playbook(registry.config_store.config_dir, playbook_id)
    if playbook is None:
        raise HTTPException(status_code=404, detail="Playbook not found.")
    return playbook


@router.post(
    "",
    summary="Create a new Playbook draft",
    status_code=status.HTTP_201_CREATED,
)
def create_route(
    payload: dict[str, Any],
    registry: ServiceRegistry = Depends(get_registry),
) -> Playbook:
    """Mint a server-side ID + timestamps, then persist.

    The client may pass ``id`` / ``created_at`` / ``updated_at`` and
    we will still overwrite them — those are server-controlled. This
    mirrors how ``POST /v1/storage/buckets`` treats ``id`` in the
    Storage API surface.
    """

    timestamp = now()
    payload = {
        **payload,
        "id": new_playbook_id(),
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    try:
        playbook = Playbook.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return save_playbook(registry.config_store.config_dir, playbook)


@router.delete(
    "/{playbook_id}",
    summary="Delete a Playbook",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_route(
    playbook_id: str,
    registry: ServiceRegistry = Depends(get_registry),
) -> None:
    if not delete_playbook(registry.config_store.config_dir, playbook_id):
        raise HTTPException(status_code=404, detail="Playbook not found.")
