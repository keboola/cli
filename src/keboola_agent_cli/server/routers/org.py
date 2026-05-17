"""Organization bulk-onboarding endpoints (require manage token)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_manage_token, get_registry

router = APIRouter(prefix="/org", tags=["org"])

# Every endpoint in this router requires BOTH the standard bearer token AND
# the per-request Manage API token. Declaring it here means Swagger UI's
# "Authorize" dialog will list both schemes so users can paste each one
# once instead of guessing why a request 401s.
_NEEDS_MANAGE_TOKEN: dict[str, Any] = {"security": [{"BearerAuth": [], "ManageToken": []}]}


class OrgSetup(BaseModel):
    stack_url: str
    org_id: int | None = None
    project_ids: list[int] | None = None
    token_description: str = "kbagent"
    dry_run: bool = False
    token_expires_in: int | None = None


class OrgRefresh(BaseModel):
    aliases: list[str] | None = None
    refresh_all: bool = False
    token_description: str = "kbagent"
    dry_run: bool = False
    force: bool = False
    token_expires_in: int | None = None


@router.post("/setup", summary="Onboard org / project list", openapi_extra=_NEEDS_MANAGE_TOKEN)
def setup(
    body: OrgSetup,
    manage_token: str | None = Depends(get_manage_token),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Bulk-register every project in an organization (or a fixed project ID list).

    Issues a fresh storage token per project via the Manage API and persists
    each project under a slug-style alias. Idempotent: existing aliases that
    already point at the same project ID are skipped. Mirrors
    `kbagent org setup`.
    """
    if not manage_token:
        raise HTTPException(
            status_code=401,
            detail="Missing X-Manage-Token header. Org setup requires a manage token.",
        )
    return registry.org.setup_organization(
        stack_url=body.stack_url,
        manage_token=manage_token,
        org_id=body.org_id,
        token_description=body.token_description,
        dry_run=body.dry_run,
        token_expires_in=body.token_expires_in,
        project_ids=body.project_ids,
    )


@router.post("/refresh", summary="Re-issue storage tokens", openapi_extra=_NEEDS_MANAGE_TOKEN)
def refresh(
    body: OrgRefresh,
    manage_token: str | None = Depends(get_manage_token),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Refresh the storage token for one alias, several aliases, or every project.

    Useful after rotating a Manage API token or when a per-project token is
    near expiry. Pass `refresh_all=true` to refresh every persisted alias,
    or a list of aliases to scope the refresh. Mirrors
    `kbagent project refresh --project` and `kbagent project refresh --all`.
    """
    if not manage_token:
        raise HTTPException(
            status_code=401,
            detail="Missing X-Manage-Token header. Refresh requires a manage token.",
        )
    return registry.org.refresh_tokens(
        manage_token=manage_token,
        aliases=body.aliases,
        refresh_all=body.refresh_all,
        token_description=body.token_description,
        dry_run=body.dry_run,
        force=body.force,
        token_expires_in=body.token_expires_in,
    )
