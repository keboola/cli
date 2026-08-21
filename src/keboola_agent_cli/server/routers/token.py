"""Scoped Storage token endpoints -- 1:1 mirror of the `kbagent token` group.

Mint / rotate / revoke a scoped Storage API token in a project. Every operation
uses the per-project Storage API token the service resolves from config (the
acting token must carry ``canManageTokens``); no extra per-request header is
required. A freshly minted / rotated token's secret is returned ONCE in the
response body -- the caller must store it, it is never persisted server-side.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/token", tags=["token"])


class CreateTokenBody(BaseModel):
    description: str
    bucket_write: list[str] | None = None
    bucket_read: list[str] | None = None
    component_access: list[str] | None = None
    can_read_all_file_uploads: bool = False
    expires_in: int | None = None


class TokenIdBody(BaseModel):
    token_id: str


@router.get("/{project}/list", summary="List the project's Storage tokens")
def list_tokens(
    project: str,
    with_last_used: bool = False,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """List the project's tokens without their secrets. Mirrors
    `kbagent token list`. The acting project token must have canManageTokens.

    `with_last_used` mirrors `--with-last-used`: it adds `lastUsed`,
    `lastUsedEvent` and `lastUsedStatus` per token plus an `errors` list, and
    sorts dormant tokens first. Off by default -- it costs one extra Storage
    API call per token.
    """
    return registry.token.list_tokens(alias=project, with_last_used=with_last_used)


@router.post("/{project}/create", summary="Mint a scoped Storage token")
def create_token(
    project: str,
    body: CreateTokenBody,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Mint a scoped token and return it (secret included ONCE). Mirrors
    `kbagent token create`. The acting project token must have canManageTokens.
    """
    return registry.token.create_scoped_token(
        alias=project,
        description=body.description,
        bucket_write=body.bucket_write,
        bucket_read=body.bucket_read,
        component_access=body.component_access,
        can_read_all_file_uploads=body.can_read_all_file_uploads,
        expires_in=body.expires_in,
    )


@router.post("/{project}/delete", summary="Revoke a Storage token (destructive)")
def delete_token(
    project: str,
    body: TokenIdBody,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Revoke a token immediately. Mirrors `kbagent token delete`."""
    return registry.token.delete_token(alias=project, token_id=body.token_id)


@router.post("/{project}/refresh", summary="Rotate a Storage token")
def refresh_token(
    project: str,
    body: TokenIdBody,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Rotate a token: return the new value, invalidate the old. Mirrors
    `kbagent token refresh`.
    """
    return registry.token.refresh_token(alias=project, token_id=body.token_id)
