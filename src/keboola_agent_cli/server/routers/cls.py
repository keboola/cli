"""Column-level security endpoints -- the REST mirror of ``kbagent cls *``.

1:1 with the CLI group. Same shape and rules as the ``rls`` router (every
route permission-gated; ``GET /{project}/schema`` registered before
``GET /{project}/{policy_id}`` so it is never shadowed), over the
``cls-policy`` object type: each rule is ``{principal|principals,
visible_columns}``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ...errors import ErrorCode, KeboolaApiError
from ..dependencies import ServiceRegistry, get_registry, require_permission

router = APIRouter(prefix="/cls", tags=["cls"])


def _perm(operation: str) -> Any:
    return Depends(require_permission(f"cls.{operation}"))


# -- Bodies --------------------------------------------------------------------------------------


class ClsPolicyCreate(BaseModel):
    table: str
    dialect: str
    rules: list[dict[str, Any]]
    target_project_ids: list[str] | None = None
    dry_run: bool = False


class ClsPolicyUpdate(BaseModel):
    table: str | None = None
    dialect: str | None = None
    rules: list[dict[str, Any]] | None = None
    target_project_ids: list[str] | None = None
    dry_run: bool = False


# -- Reads -----------------------------------------------------------------------------------


@router.get("/{project}", summary="List CLS policies", dependencies=[_perm("list")])
def list_policies(
    project: str, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """List column-level security policies visible to a project. Mirrors `kbagent cls list`."""
    return registry.cls.list_policies(project)


@router.get(
    "/{project}/schema",
    summary="Fetch the live cls-policy JSON Schema",
    dependencies=[_perm("schema")],
)
def get_schema(project: str, registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    """Live schema from the metastore -- no offline bundled snapshot. Mirrors `kbagent cls schema`."""
    fetch = registry.cls.fetch_schema(project)
    if fetch.schema is None:
        raise KeboolaApiError(
            message=f"Could not fetch the cls-policy schema: {fetch.reason}",
            status_code=404,
            error_code=ErrorCode.NOT_FOUND,
            retryable=False,
        )
    return {"format": "json-schema", "source": "live", "schema": fetch.schema}


@router.get("/{project}/{policy_id}", summary="Get one CLS policy", dependencies=[_perm("detail")])
def get_policy(
    project: str, policy_id: str, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Full rule set for one policy. Mirrors `kbagent cls detail`."""
    return registry.cls.get_policy(project, policy_id)


# -- Writes ----------------------------------------------------------------------------------


@router.post("/{project}", summary="Create a CLS policy", dependencies=[_perm("create")])
def create_policy(
    project: str, body: ClsPolicyCreate, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Create one policy for one table -- always `organization`/`targeted` scope,

    never `project` scope (no such option exists on this body). Mirrors
    `kbagent cls create`.
    """
    return registry.cls.create_policy(
        project,
        table=body.table,
        dialect=body.dialect,
        rules=body.rules,
        target_project_ids=body.target_project_ids,
        dry_run=body.dry_run,
    )


@router.put("/{project}/{policy_id}", summary="Update a CLS policy", dependencies=[_perm("update")])
def update_policy(
    project: str,
    policy_id: str,
    body: ClsPolicyUpdate,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Fetch-then-merge: an omitted field keeps its current value, it is never

    silently blanked. Mirrors `kbagent cls update`.
    """
    return registry.cls.update_policy(
        project,
        policy_id,
        table=body.table,
        dialect=body.dialect,
        rules=body.rules,
        target_project_ids=body.target_project_ids,
        dry_run=body.dry_run,
    )


@router.delete(
    "/{project}/{policy_id}", summary="Delete a CLS policy", dependencies=[_perm("delete")]
)
def delete_policy(
    project: str, policy_id: str, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Delete a policy, un-protecting its columns. Mirrors `kbagent cls delete`."""
    return registry.cls.delete_policy(project, policy_id)
