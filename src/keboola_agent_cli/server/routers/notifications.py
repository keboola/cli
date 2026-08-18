"""Flow Notification subscription endpoints (issue #600).

Read-only by design: the upstream notification service on
``notification.{stack}`` exposes ``POST`` / ``DELETE
/project-subscriptions``, which change who gets paged when production
breaks. This router -- and the client/service layers it delegates to --
only ever issue GET requests. Do not add a write endpoint here.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", summary="Flow Notification subscriptions across projects")
def list_subscriptions(
    project: list[str] | None = Query(None),
    event: str | None = Query(None),
    component_id: str | None = Query(None),
    config_id: str | None = Query(None),
    branch: int | None = Query(None),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Notification-tab recipients per project. Mirrors `kbagent notification list`.

    `event` is forwarded to the upstream service; `component_id`,
    `config_id` and `branch` match client-side against each subscription's
    own filter fields. The upstream endpoint is not branch-scoped, so
    omitting `branch` returns dev-branch subscriptions alongside production.
    """
    return registry.notification.list_subscriptions(
        aliases=project,
        event=event,
        component_id=component_id,
        config_id=config_id,
        branch_id=branch,
    )
