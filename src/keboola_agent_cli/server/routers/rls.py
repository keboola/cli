"""Row-level security endpoints -- the REST mirror of ``kbagent rls *``.

1:1 with the CLI group (CONTRIBUTING: every non-terminal command has a
route), except ``rls setup``: it is a genuinely terminal-only interactive
wizard (a checkbox table picker + guided condition prompts), the same
carve-out ``auth register-projects``' picker uses. Its data source (listing
a project's tables) is already covered by the existing ``storage`` router;
its write (create one policy per selected table) is the same
``POST /rls/{project}`` route below.

**Every route enforces the permission policy** (``Depends(require_permission)``)
-- RLS is security-sensitive enough (org-admin-only authorship) that
CLI-gates-but-REST-doesn't would be a real hole, mirroring
``merge_requests.py`` rather than the (ungated) ``notifications.py``.

The ``GET /{project}/schema`` route is registered before
``GET /{project}/{policy_id}`` so ``schema`` is never shadowed by the
parameterized route -- Starlette matches path operations in registration
order.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ...errors import ErrorCode, KeboolaApiError
from ..dependencies import ServiceRegistry, get_registry, require_permission

router = APIRouter(prefix="/rls", tags=["rls"])


def _perm(operation: str) -> Any:
    return Depends(require_permission(f"rls.{operation}"))


# -- Bodies --------------------------------------------------------------------------------------


class RlsPolicyCreate(BaseModel):
    table: str
    dialect: str
    rules: list[dict[str, Any]]
    target_project_ids: list[str] | None = None
    dry_run: bool = False


class RlsPolicyUpdate(BaseModel):
    table: str | None = None
    dialect: str | None = None
    rules: list[dict[str, Any]] | None = None
    target_project_ids: list[str] | None = None
    dry_run: bool = False


# -- Reads -----------------------------------------------------------------------------------


@router.get("/{project}", summary="List RLS policies", dependencies=[_perm("list")])
def list_policies(
    project: str, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """List row-level security policies visible to a project. Mirrors `kbagent rls list`."""
    return registry.rls.list_policies(project)


@router.get(
    "/{project}/schema",
    summary="Fetch the live rls-policy JSON Schema",
    dependencies=[_perm("schema")],
)
def get_schema(project: str, registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    """Live schema from the metastore -- no offline bundled snapshot. Mirrors `kbagent rls schema`."""
    fetch = registry.rls.fetch_schema(project)
    if fetch.schema is None:
        raise KeboolaApiError(
            message=f"Could not fetch the rls-policy schema: {fetch.reason}",
            status_code=404,
            error_code=ErrorCode.NOT_FOUND,
            retryable=False,
        )
    return {"format": "json-schema", "source": "live", "schema": fetch.schema}


@router.get("/{project}/{policy_id}", summary="Get one RLS policy", dependencies=[_perm("detail")])
def get_policy(
    project: str, policy_id: str, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Full rule set for one policy. Mirrors `kbagent rls detail`."""
    return registry.rls.get_policy(project, policy_id)


# -- Writes ----------------------------------------------------------------------------------


@router.post("/{project}", summary="Create an RLS policy", dependencies=[_perm("create")])
def create_policy(
    project: str, body: RlsPolicyCreate, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Create one policy for one table -- always `organization`/`targeted` scope,

    never `project` scope (no such option exists on this body). Mirrors
    `kbagent rls create`.
    """
    return registry.rls.create_policy(
        project,
        table=body.table,
        dialect=body.dialect,
        rules=body.rules,
        target_project_ids=body.target_project_ids,
        dry_run=body.dry_run,
    )


@router.put(
    "/{project}/{policy_id}", summary="Update an RLS policy", dependencies=[_perm("update")]
)
def update_policy(
    project: str,
    policy_id: str,
    body: RlsPolicyUpdate,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Fetch-then-merge: an omitted field keeps its current value, it is never

    silently blanked. Mirrors `kbagent rls update`.
    """
    return registry.rls.update_policy(
        project,
        policy_id,
        table=body.table,
        dialect=body.dialect,
        rules=body.rules,
        target_project_ids=body.target_project_ids,
        dry_run=body.dry_run,
    )


@router.delete(
    "/{project}/{policy_id}", summary="Delete an RLS policy", dependencies=[_perm("delete")]
)
def delete_policy(
    project: str, policy_id: str, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Delete a policy, un-protecting its table. Mirrors `kbagent rls delete`."""
    return registry.rls.delete_policy(project, policy_id)
