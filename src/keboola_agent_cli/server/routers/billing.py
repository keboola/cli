"""PAYG billing endpoints (credit balance).

Read-only by design: the upstream billing service on ``billing.{stack}``
exposes a ``POST /credits`` that triggers a REAL automatic top-up (real
money). This router -- and the client/service layers it delegates to --
only ever issue GET requests. Do not add a write endpoint here.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/credits", summary="PAYG credit balance across projects")
def credits(
    project: list[str] | None = Query(None),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """PAYG credit balance per project. Mirrors `kbagent billing credits`.

    Balance only -- purchase history and Stripe invoice IDs are not
    reachable with a project token (issue #594) and have no endpoint here.
    Read-only: the upstream billing service's `POST /credits` performs a
    real-money automatic top-up and is deliberately not exposed.
    """
    return registry.billing.get_credits(aliases=project)
