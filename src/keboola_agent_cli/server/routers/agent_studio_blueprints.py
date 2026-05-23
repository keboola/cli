"""HTTP surface for the Agent Studio Blueprint catalogue.

Read-only catalogue (`GET`) plus a fork action that mints a new
Playbook from a Blueprint. Path prefix ``/v1/agent-studio/blueprints``
per `docs/agents-v2.md` § 19.2.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from ...agent_studio.blueprints_catalog import get_blueprint, list_blueprints
from ...agent_studio.models.blueprint import Blueprint
from ...agent_studio.models.playbook import Playbook
from ...agent_studio.storage import new_id, now, save_playbook
from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(
    prefix="/v1/agent-studio/blueprints",
    tags=["agent-studio"],
)


@router.get("", summary="List Blueprint templates")
def list_route(category: str | None = None) -> dict[str, list[Blueprint]]:
    """The catalogue, optionally filtered by ``?category=Data Cleanup``.

    The catalogue is a static in-code seed in Phase 1, so this never
    touches disk and needs no project context.
    """

    return {"blueprints": list_blueprints(category)}


@router.get("/{blueprint_id}", summary="Get one Blueprint")
def get_route(blueprint_id: str) -> Blueprint:
    blueprint = get_blueprint(blueprint_id)
    if blueprint is None:
        raise HTTPException(status_code=404, detail="Blueprint not found.")
    return blueprint


@router.post(
    "/{blueprint_id}/fork",
    summary="Fork a Blueprint into a new Playbook",
    status_code=status.HTTP_201_CREATED,
)
def fork_route(
    blueprint_id: str,
    registry: ServiceRegistry = Depends(get_registry),
) -> Playbook:
    """Mint a new draft Playbook prefilled from the Blueprint.

    Phase 1 copies name / description / connections / skills / plugins.
    The SOP, budget, and approval policy are not yet part of the
    Playbook shape, so the forked Playbook starts as a ``draft`` the
    user fills in — exactly the "fork one to get a working Playbook in
    seconds" promise from `docs/mockups/02-blueprints-catalog.png`,
    minus the parts the model can't carry yet.
    """

    blueprint = get_blueprint(blueprint_id)
    if blueprint is None:
        raise HTTPException(status_code=404, detail="Blueprint not found.")

    timestamp = now()
    playbook = Playbook(
        id=new_id(),
        name=blueprint.name,
        description=blueprint.description,
        connections=list(blueprint.connections),
        skills=list(blueprint.skills),
        plugins=list(blueprint.plugins),
        status="draft",
        created_at=timestamp,
        updated_at=timestamp,
    )
    return save_playbook(registry.config_store.config_dir, playbook)
