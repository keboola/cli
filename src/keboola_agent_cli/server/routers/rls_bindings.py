"""Policy-binding endpoints -- the REST mirror of ``kbagent rls bind|unbind|bindings|bindings-sweep``.

Included in the app BEFORE the ``rls`` router: ``GET /rls/{project}/{policy_id}`` would otherwise
capture ``GET /rls/{project}/bindings`` (Starlette matches routes in registration order).

``stack_id`` is a required field here: the service never reads the environment (the CLI's
``KBAGENT_STACK_ID`` fallback is a CLI option default). A sweep with ``dry_run: false`` deletes, so
it is also checked as ``rls.bindings-sweep --yes`` (destructive), like the CLI flag.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ...permissions import PermissionEngine
from ..dependencies import (
    ServiceRegistry,
    get_permission_engine,
    get_registry,
    require_permission,
)

router = APIRouter(prefix="/rls", tags=["rls"])


class BindingPut(BaseModel):
    """The policies bound to the workspace; both lists are replaced, at least one is non-empty."""

    stack_id: str
    rls_policies: list[str] = Field(default_factory=list)
    cls_policies: list[str] = Field(default_factory=list)
    dry_run: bool = False


class BindingSweep(BaseModel):
    """``dry_run: false`` deletes the orphaned bindings (``rls bindings-sweep --yes``)."""

    older_than_days: int = Field(default=30, ge=0)
    dry_run: bool = True


@router.get(
    "/{project}/bindings",
    summary="List the policy bindings of a project",
    dependencies=[Depends(require_permission("rls.bindings"))],
)
def list_bindings(
    project: str,
    workspace_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Bindings with whether each workspace still exists. Mirrors `kbagent rls bindings`."""
    return registry.policy_binding.list_bindings(project, workspace_id)


@router.post(
    "/{project}/bindings/sweep",
    summary="Find (and delete) orphaned policy bindings",
    dependencies=[Depends(require_permission("rls.bindings-sweep"))],
)
def sweep_bindings(
    project: str,
    body: BindingSweep,
    registry: ServiceRegistry = Depends(get_registry),
    engine: PermissionEngine = Depends(get_permission_engine),
) -> dict[str, Any]:
    """Reports by default; ``dry_run: false`` deletes. Mirrors `kbagent rls bindings-sweep`."""
    if not body.dry_run:
        engine.check_or_raise("rls.bindings-sweep --yes")
    return registry.policy_binding.sweep(
        project, older_than_days=body.older_than_days, delete=not body.dry_run
    )


@router.put(
    "/{project}/bindings/{workspace_id}",
    summary="Bind policies to a governed workspace",
    dependencies=[Depends(require_permission("rls.bind"))],
)
def bind_workspace(
    project: str,
    workspace_id: int,
    body: BindingPut,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Create or replace the binding of one workspace. Mirrors `kbagent rls bind`."""
    return registry.policy_binding.bind(
        project,
        workspace_id,
        stack_id=body.stack_id,
        rls_policies=body.rls_policies,
        cls_policies=body.cls_policies,
        dry_run=body.dry_run,
    )


@router.delete(
    "/{project}/bindings/{workspace_id}",
    summary="Delete the policy binding of a workspace",
    dependencies=[Depends(require_permission("rls.unbind"))],
)
def unbind_workspace(
    project: str,
    workspace_id: int,
    stack_id: str,
    dry_run: bool = False,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """The workspace then gets every policy of the project. Mirrors `kbagent rls unbind`."""
    return registry.policy_binding.unbind(project, workspace_id, stack_id=stack_id, dry_run=dry_run)
