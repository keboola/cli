"""SQL transformation endpoints (create / show / edit blocks and codes).

Mirrors the ``kbagent transformation`` command group backed by
:class:`keboola_agent_cli.services.transformation_service.TransformationService`
(issue #396). ``ValueError`` from the service (empty SQL, invalid ops) maps
to HTTP 400 -- the REST equivalent of the CLI's VALIDATION_ERROR exit path.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/transformations", tags=["transformations"])


class TransformationCreate(BaseModel):
    name: str
    sql: str
    created_tables: list[str] | None = None
    component_id: str | None = None
    description: str = ""
    branch_id: int | None = None
    dry_run: bool = False


class TransformationEdit(BaseModel):
    change_description: str
    ops: list[dict[str, Any]] = []
    component_id: str | None = None
    storage: dict[str, Any] | None = None
    branch_id: int | None = None
    dry_run: bool = False


@router.post("/{project}", summary="Create a SQL transformation")
def create(
    project: str,
    body: TransformationCreate,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Create a SQL transformation from a SQL script. Mirrors `kbagent transformation create`.

    The SQL is split into one statement per script element; each entry in
    ``created_tables`` is mapped to ``out.c-<derived-bucket>.<table>`` in the
    output mapping. ``component_id`` defaults to the project's backend
    (Snowflake / BigQuery).
    """
    try:
        return registry.transformation.create(
            project,
            name=body.name,
            sql=body.sql,
            created_tables=body.created_tables,
            component_id=body.component_id,
            description=body.description,
            branch_id=body.branch_id,
            dry_run=body.dry_run,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{project}/{config_id}", summary="Show a SQL transformation's block tree")
def show(
    project: str,
    config_id: str,
    component_id: str | None = None,
    branch_id: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Render the block/code tree with positional IDs (b0, b0.c0, ...).

    Mirrors `kbagent transformation show`. When ``component_id`` is omitted
    the known SQL transformation components are tried until the
    configuration is found.
    """
    return registry.transformation.show(
        project,
        config_id=config_id,
        component_id=component_id,
        branch_id=branch_id,
    )


@router.patch("/{project}/{config_id}", summary="Edit a SQL transformation with operations")
def edit(
    project: str,
    config_id: str,
    body: TransformationEdit,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Apply a batch of block/code operations. Mirrors `kbagent transformation edit`.

    ``ops`` entries use the IDs from the show route; ``storage``, when set,
    replaces ``configuration.storage`` wholesale. ``ops`` may be empty when
    only ``storage`` is being replaced.
    """
    try:
        return registry.transformation.edit(
            project,
            config_id=config_id,
            ops=body.ops,
            change_description=body.change_description,
            component_id=body.component_id,
            storage=body.storage,
            branch_id=body.branch_id,
            dry_run=body.dry_run,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
