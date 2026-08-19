"""Notification subscription audit endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", summary="List notification subscriptions")
def list_subscriptions(
    project: list[str] | None = Query(None),
    event: str | None = None,
    component_id: str | None = None,
    config_id: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """List Flow Notifications-tab recipients. Mirrors `kbagent notification list`.

    `project_wide_excluded` in the response counts the filter-less
    subscriptions that `component_id` / `config_id` dropped -- they fire for
    every job, so a caller answering "who gets paged" must not treat a
    filtered list as complete.
    """
    return registry.notification.list_subscriptions(
        aliases=project,
        event=event,
        component_id=component_id,
        config_id=config_id,
    )


@router.get("/{project}/{subscription_id}", summary="Get subscription detail")
def detail(
    project: str,
    subscription_id: str,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """One subscription including its raw filters. Mirrors `kbagent notification detail`."""
    return registry.notification.get_subscription_detail(
        alias=project, subscription_id=subscription_id
    )
