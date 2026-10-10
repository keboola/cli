"""Notification subscription audit endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationCreate(BaseModel):
    event: str
    channel: str
    address: str
    component_id: str | None = None
    config_id: str | None = None
    branch_id: int | None = None
    expires_at: str | None = None


class NotificationReplaceRecipient(BaseModel):
    address: str
    channel: str | None = None


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


@router.post("/{project}", summary="Create a notification subscription")
def create(
    project: str,
    body: NotificationCreate,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Create a notification subscription and return its audit row."""
    return registry.notification.create_subscription(
        alias=project,
        event=body.event,
        channel=body.channel,
        address=body.address,
        component_id=body.component_id,
        config_id=body.config_id,
        branch_id=body.branch_id,
        expires_at=body.expires_at,
    )


@router.delete("/{project}/{subscription_id}", summary="Delete a notification subscription")
def delete(
    project: str,
    subscription_id: str,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Delete a notification subscription."""
    return registry.notification.delete_subscription(alias=project, subscription_id=subscription_id)


@router.post(
    "/{project}/{subscription_id}/replace-recipient",
    summary="Replace a subscription's recipient",
)
def replace_recipient(
    project: str,
    subscription_id: str,
    body: NotificationReplaceRecipient,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Swap a subscription's recipient (create-then-delete). See service docstring."""
    return registry.notification.replace_subscription_recipient(
        alias=project,
        subscription_id=subscription_id,
        new_address=body.address,
        new_channel=body.channel,
    )
