"""Notification subscription audit endpoints."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
)

from ...services.base import resolve_fanout_aliases
from ..dependencies import ALL_PROJECTS_HELP, ServiceRegistry, get_registry

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationFilter(BaseModel):
    """One ``filters[]`` entry: the Notification Service's own wire shape (#989).

    ``value`` is required (null is a value). ``operator`` omitted means ``==``;
    ``=`` is accepted and sent as ``==``. Types are strict and NaN/infinity are
    refused, so a REST caller and a ``--filter`` caller reach the same
    validator with the same kinds of values.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    field: StrictStr
    operator: Literal["==", "=", "<", "<=", ">", ">="] | None = None
    value: StrictStr | StrictInt | StrictFloat | StrictBool | None = Field(...)

    def wire(self) -> dict[str, Any]:
        entry: dict[str, Any] = {"field": self.field}
        if self.operator is not None:
            entry["operator"] = self.operator
        entry["value"] = self.value
        return entry


def _wire_filters(filters: list[NotificationFilter] | None) -> list[dict[str, Any]] | None:
    """``None`` stays ``None`` (no filters given); ``[]`` stays an explicit empty list."""
    return None if filters is None else [item.wire() for item in filters]


class NotificationCreate(BaseModel):
    event: str
    channel: str
    address: str
    component_id: str | None = None
    config_id: str | None = None
    branch_id: int | None = None
    expires_at: str | None = None
    # Whole percent (50 = 50 % longer than usual), only for the
    # *-processing-long events. StrictInt: a boolean or "50" is a 422, never
    # coerced; the event and range checks are the service's (HTTP 400).
    overtime_percent: StrictInt | None = None
    # Extra filters on any event payload field, after the dedicated ones.
    filters: list[NotificationFilter] | None = None


class NotificationReplaceRecipient(BaseModel):
    address: str
    channel: str | None = None


class NotificationClone(BaseModel):
    """Body of ``POST /notifications/{project}/{subscription_id}/clone`` (spec V5).

    Every field is an override; an empty body duplicates the subscription.
    """

    model_config = ConfigDict(extra="forbid")

    component_id: str | None = None
    config_id: str | None = None
    branch_id: StrictInt | None = None
    overtime_percent: StrictInt | None = None
    filters: list[NotificationFilter] | None = None
    address: str | None = None
    channel: str | None = None
    expires_at: str | None = None
    clear_expires_at: bool = False
    dry_run: bool = False


def _reject_query_parameters(request: Request) -> None:
    """The clone route takes everything in its JSON body; any query parameter is refused."""
    unknown = sorted(set(request.query_params))
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown query parameter(s): {', '.join(unknown)}. This route takes its "
                "overrides, dry_run included, in the JSON body. The request was refused "
                "and nothing was created."
            ),
        )


@router.get("", summary="List notification subscriptions")
def list_subscriptions(
    project: list[str] | None = Query(None),
    event: str | None = None,
    component_id: str | None = None,
    config_id: str | None = None,
    all_projects: bool = Query(False, description=ALL_PROJECTS_HELP),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """List Flow Notifications-tab recipients. Mirrors `kbagent notification list`.

    `project_wide_excluded` in the response counts the filter-less
    subscriptions that `component_id` / `config_id` dropped -- they fire for
    every job, so a caller answering "who gets paged" must not treat a
    filtered list as complete.
    """
    aliases = resolve_fanout_aliases(registry.config_store, project, all_projects)
    return registry.notification.list_subscriptions(
        aliases=aliases,
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
        overtime_percent=body.overtime_percent,
        filters=_wire_filters(body.filters),
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


@router.post(
    "/{project}/{subscription_id}/clone",
    summary="Create a subscription from an existing one",
    dependencies=[Depends(_reject_query_parameters)],
)
def clone(
    project: str,
    subscription_id: str,
    body: NotificationClone,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Copy a subscription with overrides. Mirrors `kbagent notification clone`."""
    return registry.notification.clone_subscription(
        alias=project,
        subscription_id=subscription_id,
        component_id=body.component_id,
        config_id=body.config_id,
        branch_id=body.branch_id,
        overtime_percent=body.overtime_percent,
        filters=_wire_filters(body.filters),
        address=body.address,
        channel=body.channel,
        expires_at=body.expires_at,
        clear_expires_at=body.clear_expires_at,
        dry_run=body.dry_run,
    )
