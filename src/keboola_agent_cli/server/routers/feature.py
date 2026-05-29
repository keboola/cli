"""Feature-flag endpoints (stack catalogue / project / user) -- all require a manage token.

1:1 mirror of the `kbagent feature` command group. Every operation hits the
Manage API and therefore needs the per-request Manage token alongside the
standard bearer token, exactly like the `members` and `org` routers.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_manage_token, get_registry

router = APIRouter(prefix="/feature", tags=["feature"])

# Every feature endpoint hits the Manage API, so each needs the per-request
# Manage token alongside the bearer token. Declaring the joint requirement
# here surfaces it as a separate scheme in the Swagger UI "Authorize" dialog.
_NEEDS_MANAGE_TOKEN: dict[str, Any] = {"security": [{"BearerAuth": [], "ManageToken": []}]}


class ProjectFeatureBody(BaseModel):
    feature: str
    dry_run: bool = False


class UserFeatureBody(BaseModel):
    email: str
    feature: str
    dry_run: bool = False


def _require_manage(token: str | None) -> str:
    if not token:
        raise HTTPException(status_code=401, detail="Missing X-Manage-Token header.")
    return token


@router.get("/{project}/list", summary="Stack feature catalogue", openapi_extra=_NEEDS_MANAGE_TOKEN)
def list_features(
    project: str,
    manage_token: str | None = Depends(get_manage_token),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Every feature defined on the stack `project` points at. The alias only
    resolves the stack URL -- the catalogue is stack-wide. Mirrors
    `kbagent feature list`.
    """
    return registry.feature.list_stack_features(
        manage_token=_require_manage(manage_token), alias=project
    )


@router.get(
    "/{project}/project-show",
    summary="Project's assigned features",
    openapi_extra=_NEEDS_MANAGE_TOKEN,
)
def project_show(
    project: str,
    manage_token: str | None = Depends(get_manage_token),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Features assigned to `project`. Mirrors `kbagent feature project-show`."""
    return registry.feature.list_project_features(
        manage_token=_require_manage(manage_token), alias=project
    )


@router.post(
    "/{project}/project-add",
    summary="Enable a feature on a project",
    openapi_extra=_NEEDS_MANAGE_TOKEN,
)
def project_add(
    project: str,
    body: ProjectFeatureBody,
    manage_token: str | None = Depends(get_manage_token),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Enable a feature on `project`. Pass `dry_run=true` to preview. Mirrors
    `kbagent feature project-add`.
    """
    return registry.feature.add_project_feature(
        manage_token=_require_manage(manage_token),
        alias=project,
        feature=body.feature,
        dry_run=body.dry_run,
    )


@router.post(
    "/{project}/project-remove",
    summary="Disable a feature on a project",
    openapi_extra=_NEEDS_MANAGE_TOKEN,
)
def project_remove(
    project: str,
    body: ProjectFeatureBody,
    manage_token: str | None = Depends(get_manage_token),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Disable a feature on `project` (destructive). Pass `dry_run=true` to
    preview. Mirrors `kbagent feature project-remove`.
    """
    return registry.feature.remove_project_feature(
        manage_token=_require_manage(manage_token),
        alias=project,
        feature=body.feature,
        dry_run=body.dry_run,
    )


@router.get(
    "/{project}/user-show",
    summary="User's assigned features",
    openapi_extra=_NEEDS_MANAGE_TOKEN,
)
def user_show(
    project: str,
    email: str,
    manage_token: str | None = Depends(get_manage_token),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Features assigned to `email` on the alias's stack. Mirrors
    `kbagent feature user-show`.
    """
    return registry.feature.list_user_features(
        manage_token=_require_manage(manage_token), alias=project, email=email
    )


@router.post(
    "/{project}/user-add",
    summary="Enable a feature on a user",
    openapi_extra=_NEEDS_MANAGE_TOKEN,
)
def user_add(
    project: str,
    body: UserFeatureBody,
    manage_token: str | None = Depends(get_manage_token),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Enable a feature on a user. Pass `dry_run=true` to preview. Mirrors
    `kbagent feature user-add`.
    """
    return registry.feature.add_user_feature(
        manage_token=_require_manage(manage_token),
        alias=project,
        email=body.email,
        feature=body.feature,
        dry_run=body.dry_run,
    )


@router.post(
    "/{project}/user-remove",
    summary="Disable a feature on a user",
    openapi_extra=_NEEDS_MANAGE_TOKEN,
)
def user_remove(
    project: str,
    body: UserFeatureBody,
    manage_token: str | None = Depends(get_manage_token),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Disable a feature on a user (destructive). Pass `dry_run=true` to
    preview. Mirrors `kbagent feature user-remove`.
    """
    return registry.feature.remove_user_feature(
        manage_token=_require_manage(manage_token),
        alias=project,
        email=body.email,
        feature=body.feature,
        dry_run=body.dry_run,
    )
