"""Project members endpoints (invite/list/remove/role) -- all require manage token."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_manage_token, get_registry

router = APIRouter(prefix="/members", tags=["members"])

# Member endpoints all hit the Manage API, so every operation needs the
# per-request Manage token alongside the standard bearer token. Declaring
# the joint requirement here surfaces it as a separate scheme in the
# Swagger UI "Authorize" dialog.
_NEEDS_MANAGE_TOKEN: dict[str, Any] = {"security": [{"BearerAuth": [], "ManageToken": []}]}


class InviteOne(BaseModel):
    email: str
    role: str
    reason: str | None = None
    dry_run: bool = False


class CancelInvitation(BaseModel):
    email: str
    invitation_id: int | None = None


class RemoveMember(BaseModel):
    email: str


class SetRole(BaseModel):
    email: str
    role: str


def _require_manage(token: str | None) -> str:
    if not token:
        raise HTTPException(status_code=401, detail="Missing X-Manage-Token header.")
    return token


def _serialize(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    return value


@router.get("/{project}", summary="List members", openapi_extra=_NEEDS_MANAGE_TOKEN)
def list_members(
    project: str,
    include_pending: bool = False,
    manage_token: str | None = Depends(get_manage_token),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Active members of `project`. Pass `include_pending=true` to also include
    invitations that have not yet been accepted. Mirrors
    `kbagent project member-list`.
    """
    return registry.member.list_members(
        manage_token=_require_manage(manage_token),
        alias=project,
        include_pending=include_pending,
    )


@router.get(
    "/{project}/invitations",
    summary="List pending invitations",
    openapi_extra=_NEEDS_MANAGE_TOKEN,
)
def list_invitations(
    project: str,
    manage_token: str | None = Depends(get_manage_token),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Outstanding invitations for `project`. Mirrors
    `kbagent project invitation-list`.
    """
    return registry.member.list_invitations(
        manage_token=_require_manage(manage_token), alias=project
    )


@router.post("/{project}/invite", summary="Invite a single user", openapi_extra=_NEEDS_MANAGE_TOKEN)
def invite(
    project: str,
    body: InviteOne,
    manage_token: str | None = Depends(get_manage_token),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Send a single invitation. Pass `dry_run=true` to validate without
    actually sending. Mirrors `kbagent project invite --email --role`.
    """
    return registry.member.invite(
        manage_token=_require_manage(manage_token),
        alias=project,
        email=body.email,
        role=body.role,
        reason=body.reason,
        dry_run=body.dry_run,
    )


@router.post(
    "/{project}/invitations/cancel",
    summary="Cancel invitation",
    openapi_extra=_NEEDS_MANAGE_TOKEN,
)
def cancel_invitation(
    project: str,
    body: CancelInvitation,
    manage_token: str | None = Depends(get_manage_token),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Revoke an outstanding invitation. Identified by email (the API picks
    the right invitation), or pin a specific `invitation_id`. Mirrors
    `kbagent project invitation-cancel`.
    """
    return registry.member.cancel_invitation(
        manage_token=_require_manage(manage_token),
        alias=project,
        email=body.email,
        invitation_id=body.invitation_id,
    )


@router.post("/{project}/remove", summary="Remove member", openapi_extra=_NEEDS_MANAGE_TOKEN)
def remove(
    project: str,
    body: RemoveMember,
    manage_token: str | None = Depends(get_manage_token),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Revoke a user's access to the project. Mirrors
    `kbagent project member-remove`.
    """
    return registry.member.remove_member(
        manage_token=_require_manage(manage_token),
        alias=project,
        email=body.email,
    )


@router.post(
    "/{project}/set-role",
    summary="Change member role",
    openapi_extra=_NEEDS_MANAGE_TOKEN,
)
def set_role(
    project: str,
    body: SetRole,
    manage_token: str | None = Depends(get_manage_token),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Promote / demote a member. Role is one of `admin`, `guest`,
    `readOnly`, `share`. Mirrors `kbagent project member-set-role`.
    """
    return registry.member.set_member_role(
        manage_token=_require_manage(manage_token),
        alias=project,
        email=body.email,
        role=body.role,
    )
