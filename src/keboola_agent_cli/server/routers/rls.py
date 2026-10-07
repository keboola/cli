"""Row-level security endpoints -- the REST mirror of ``kbagent rls *``.

1:1 with the CLI group (CONTRIBUTING: every non-terminal command has a
route), except ``rls setup``: it is a genuinely terminal-only interactive
wizard (a checkbox table picker + guided condition prompts), the same
carve-out ``auth register-projects``' picker uses. Its data source (listing
a project's tables) is already covered by the existing ``storage`` router;
its write (create one policy per selected table) is the same
``POST /rls/{project}`` route below.

:func:`build_policy_router` also builds the ``cls`` router: the two groups
differ only in the service and the rule shape, which the service validates.

**Every route enforces the permission policy** (``Depends(require_permission)``)
-- RLS is security-sensitive enough (admin-class writes) that
CLI-gates-but-REST-doesn't would be a real hole, mirroring
``merge_requests.py`` rather than the (ungated) ``notifications.py``. A
create at ``organization`` scope is also checked as
``<group>.create --scope organization`` (destructive), like the CLI flag.

The ``GET /{project}/schema`` route is registered before
``GET /{project}/{policy_id}`` so ``schema`` is never shadowed by the
parameterized route -- Starlette matches path operations in registration
order.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ...errors import ErrorCode, KeboolaApiError
from ...permissions import PermissionEngine
from ...services._rls_condition import Dialect
from ...services.rls_service import PolicyScope
from ..dependencies import (
    ServiceRegistry,
    get_permission_engine,
    get_registry,
    require_permission,
)

# -- Bodies --------------------------------------------------------------------------------------


class PolicyCreate(BaseModel):
    """``target_projects`` takes aliases or project IDs, like the semantic-layer bodies.

    ``target_projects`` with ``scope: organization`` is refused by the service (400).
    """

    table_id: str
    rules: list[dict[str, Any]]
    dialect: Dialect | None = None  # None = the project backend
    scope: PolicyScope = PolicyScope.TARGETED
    target_projects: list[str | int] | None = None
    dry_run: bool = False


class PolicyUpdate(BaseModel):
    """Partial update: an omitted field is unchanged; ``target_projects: []`` revokes every grant."""

    table_id: str | None = None
    dialect: Dialect | None = None
    rules: list[dict[str, Any]] | None = None
    target_projects: list[str | int] | None = None
    dry_run: bool = False


def build_policy_router(group: Literal["rls", "cls"]) -> APIRouter:
    """The ``/rls`` or ``/cls`` router."""
    label = group.upper()
    router = APIRouter(prefix=f"/{group}", tags=[group])

    def perm(operation: str) -> Any:
        return Depends(require_permission(f"{group}.{operation}"))

    def service(registry: ServiceRegistry) -> Any:
        return getattr(registry, group)

    @router.get("/{project}", summary=f"List {label} policies", dependencies=[perm("list")])
    def list_policies(
        project: str, registry: ServiceRegistry = Depends(get_registry)
    ) -> dict[str, Any]:
        """Policies visible to a project. Mirrors `kbagent <group> list`."""
        return service(registry).list_policies(project)

    @router.get(
        "/{project}/schema",
        summary=f"Fetch the live {group}-policy JSON Schema",
        dependencies=[perm("schema")],
    )
    def get_schema(
        project: str, registry: ServiceRegistry = Depends(get_registry)
    ) -> dict[str, Any]:
        """Live schema from the metastore -- no offline bundled snapshot."""
        fetch = service(registry).fetch_schema(project)
        if fetch.schema is None:
            raise KeboolaApiError(
                message=f"Could not fetch the {group}-policy schema: {fetch.reason}",
                status_code=404,
                error_code=ErrorCode.NOT_FOUND,
                retryable=False,
            )
        return {"format": "json-schema", "source": "live", "schema": fetch.schema}

    @router.get(
        "/{project}/{policy_id}", summary=f"Get one {label} policy", dependencies=[perm("detail")]
    )
    def get_policy(
        project: str, policy_id: str, registry: ServiceRegistry = Depends(get_registry)
    ) -> dict[str, Any]:
        """Full rule set for one policy. Mirrors `kbagent <group> detail`."""
        return service(registry).get_policy(project, policy_id)

    @router.post("/{project}", summary=f"Create a {label} policy", dependencies=[perm("create")])
    def create_policy(
        project: str,
        body: PolicyCreate,
        registry: ServiceRegistry = Depends(get_registry),
        engine: PermissionEngine = Depends(get_permission_engine),
    ) -> dict[str, Any]:
        """Create one policy for one table at `targeted` (default) or `organization` scope.

        Never `project` scope (the schema does not support it). Mirrors `kbagent <group> create`.
        """
        if body.scope == PolicyScope.ORGANIZATION:
            engine.check_or_raise(f"{group}.create --scope organization")
        return service(registry).create_policy(
            project,
            table=body.table_id,
            rules=body.rules,
            dialect=body.dialect,
            scope=body.scope,
            target_projects=body.target_projects,
            dry_run=body.dry_run,
        )

    @router.patch(
        "/{project}/{policy_id}", summary=f"Update a {label} policy", dependencies=[perm("update")]
    )
    def update_policy(
        project: str,
        policy_id: str,
        body: PolicyUpdate,
        registry: ServiceRegistry = Depends(get_registry),
    ) -> dict[str, Any]:
        """Partial update: an omitted field keeps its current value. Mirrors `kbagent <group> update`."""
        return service(registry).update_policy(
            project,
            policy_id,
            table=body.table_id,
            dialect=body.dialect,
            rules=body.rules,
            target_projects=body.target_projects,
            dry_run=body.dry_run,
        )

    @router.delete(
        "/{project}/{policy_id}",
        summary=f"Delete a {label} policy",
        dependencies=[perm("delete")],
    )
    def delete_policy(
        project: str,
        policy_id: str,
        dry_run: bool = False,
        registry: ServiceRegistry = Depends(get_registry),
    ) -> dict[str, Any]:
        """Delete a policy (``?dry_run=true`` only shows it). Mirrors `kbagent <group> delete`."""
        return service(registry).delete_policy(project, policy_id, dry_run=dry_run)

    return router


router = build_policy_router("rls")
