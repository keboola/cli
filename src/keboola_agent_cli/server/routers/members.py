"""Project members endpoints (invite/list/remove/role) -- all require manage token."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_manage_token, get_registry

router = APIRouter(prefix="/members", tags=["members"])


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


@router.get("/{project}")
def list_members(
    project: str,
    include_pending: bool = False,
    manage_token: str | None = Depends(get_manage_token),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.member.list_members(
        manage_token=_require_manage(manage_token),
        alias=project,
        include_pending=include_pending,
    )


@router.get("/{project}/invitations")
def list_invitations(
    project: str,
    manage_token: str | None = Depends(get_manage_token),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.member.list_invitations(
        manage_token=_require_manage(manage_token), alias=project
    )


@router.post("/{project}/invite")
def invite(
    project: str,
    body: InviteOne,
    manage_token: str | None = Depends(get_manage_token),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.member.invite(
        manage_token=_require_manage(manage_token),
        alias=project,
        email=body.email,
        role=body.role,
        reason=body.reason,
        dry_run=body.dry_run,
    )


@router.post("/{project}/invitations/cancel")
def cancel_invitation(
    project: str,
    body: CancelInvitation,
    manage_token: str | None = Depends(get_manage_token),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.member.cancel_invitation(
        manage_token=_require_manage(manage_token),
        alias=project,
        email=body.email,
        invitation_id=body.invitation_id,
    )


@router.post("/{project}/remove")
def remove(
    project: str,
    body: RemoveMember,
    manage_token: str | None = Depends(get_manage_token),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.member.remove_member(
        manage_token=_require_manage(manage_token),
        alias=project,
        email=body.email,
    )


@router.post("/{project}/set-role")
def set_role(
    project: str,
    body: SetRole,
    manage_token: str | None = Depends(get_manage_token),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.member.set_member_role(
        manage_token=_require_manage(manage_token),
        alias=project,
        email=body.email,
        role=body.role,
    )
