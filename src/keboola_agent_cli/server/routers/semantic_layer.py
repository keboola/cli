"""Semantic-layer endpoints — 18 routes covering 30 CLI subcommands.

Mirrors the per-subcommand surface of
:class:`keboola_agent_cli.services.semantic_layer_service.SemanticLayerService`.
The 15 ``add.*``/``edit.*``/``remove.*`` CLI leaves collapse into three
``{kind}``-parameterized routes (``POST/PUT/DELETE /items/{kind}``) for
RESTful semantics; per-kind Pydantic body validation happens inside the
handler. This is a deliberate departure from the CONTRIBUTING.md "1:1
endpoint per command" guideline — the parameterization preserves
discoverability while keeping the route count manageable.

Pattern: Pydantic body models for routes that the CLI flags with multiple
options, query parameters for read endpoints. ``--yes`` is implicit on
every REST destructive call (the body / DELETE request IS the confirmation).
"""

from __future__ import annotations

import contextlib
import json as _json
import tempfile
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from ...errors import ErrorCode
from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/semantic-layer", tags=["semantic-layer"])


# Closed set of remove/edit/add kinds, mirroring the CLI surface.
ItemKind = Literal["metric", "dataset", "relationship", "constraint", "glossary"]


# ── Pydantic body models ───────────────────────────────────────────


class ModelCreate(BaseModel):
    project: str
    name: str
    description: str = ""
    sql_dialect: str = "Snowflake"


class DiffRequest(BaseModel):
    """Diff body — exactly one of project_a/file_a and one of project_b/file_b."""

    project_a: str | None = None
    project_b: str | None = None
    model_a: str | None = None
    model_b: str | None = None
    file_a: dict[str, Any] | None = None
    file_b: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _exactly_one_per_side(cls, data: Any) -> Any:
        # Pydantic v2 calls this with the raw input dict in "before" mode.
        if not isinstance(data, dict):
            return data
        left = (data.get("project_a") is not None, data.get("file_a") is not None)
        right = (data.get("project_b") is not None, data.get("file_b") is not None)
        if sum(left) != 1:
            raise ValueError("Exactly one of project_a or file_a must be set.")
        if sum(right) != 1:
            raise ValueError("Exactly one of project_b or file_b must be set.")
        return data


class AddMetric(BaseModel):
    project: str
    model: str | None = None
    name: str
    sql: str
    dataset: str
    description: str = ""


class AddDataset(BaseModel):
    project: str
    model: str | None = None
    name: str
    table_id: str
    description: str = ""
    grain: str = ""
    primary_key: list[str] | None = None
    deep_fields: bool = False


class AddRelationship(BaseModel):
    project: str
    model: str | None = None
    name: str
    from_: str = Field(alias="from")
    to: str
    on: str
    type_: str = Field(alias="type", default="left")

    model_config = {"populate_by_name": True}


class AddConstraint(BaseModel):
    project: str
    model: str | None = None
    name: str
    constraint_type: str
    rule: str
    metrics: list[str]
    severity: str = "warning"


class AddGlossary(BaseModel):
    project: str
    model: str | None = None
    term: str
    definition: str = ""


class EditMetric(BaseModel):
    project: str
    model: str | None = None
    new_name: str | None = None
    new_sql: str | None = None
    new_dataset: str | None = None
    new_description: str | None = None


class EditDataset(BaseModel):
    project: str
    model: str | None = None
    new_name: str | None = None
    new_description: str | None = None
    new_grain: str | None = None


class EditRelationship(BaseModel):
    project: str
    model: str | None = None
    new_name: str | None = None
    new_from: str | None = None
    new_to: str | None = None
    new_on: str | None = None
    new_type: str | None = None


class EditConstraint(BaseModel):
    project: str
    model: str | None = None
    new_name: str | None = None
    new_rule: str | None = None
    new_constraint_type: str | None = None
    new_severity: str | None = None
    new_metrics: list[str] | None = None


class EditGlossary(BaseModel):
    project: str
    model: str | None = None
    new_term: str | None = None
    new_definition: str | None = None


class ImportRequest(BaseModel):
    project: str
    model: str | None = None
    snapshot: dict[str, Any]
    types: list[str] | None = None
    dry_run: bool = False
    overwrite: bool = False


class PromoteRequest(BaseModel):
    from_project: str
    to_project: str
    from_model: str | None = None
    to_model: str | None = None
    types: list[str] | None = None
    dry_run: bool = False


class BuildRequest(BaseModel):
    project: str
    model: str | None = None
    tables: list[str]
    name: str | None = None
    dry_run: bool = False
    keep_on_failure: bool = False
    # Column-type resolution for alias / linked tables (empty Storage
    # metadata). Provide a specific workspace, or leave auto-resolve on (the
    # UI default) to have the server pick a read-only workspace per backend.
    types_workspace_id: int | None = None
    auto_resolve_types: bool = True


class TokenEncryptRequest(BaseModel):
    project: str
    component_id: str


class RefDataSet(BaseModel):
    """Create-or-replace body for ``PUT /reference-data`` (idempotent on dimension)."""

    project: str
    model: str | None = None
    dimension: str
    members: list[dict[str, Any]]
    dataset_id: str | None = None
    description: str | None = None


# ── Routes (14 declarations, in the order from the plan) ────────────


@router.get("/models", summary="List semantic-layer models")
def list_models(
    project: str,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """List every semantic-layer model in a project."""
    return registry.semantic_layer.list_models(project)


@router.post("/models", summary="Create a semantic-layer model")
def create_model(
    body: ModelCreate, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Create a semantic-layer model (POST /repository/semantic-model)."""
    return registry.semantic_layer.create_model(
        alias=body.project,
        name=body.name,
        description=body.description,
        sql_dialect=body.sql_dialect,
    )


@router.delete("/models/{model}", summary="Delete a semantic-layer model")
def delete_model(
    model: str,
    project: str,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Delete a semantic-layer model (--yes implicit on REST)."""
    return registry.semantic_layer.delete_model(alias=project, model_name_or_uuid=model)


@router.get("/show", summary="Show model entities")
def show(
    project: str,
    model: str | None = None,
    type: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Show all entities (datasets, metrics, ...) for a model."""
    return registry.semantic_layer.show_model(
        alias=project, model_name_or_uuid=model, type_filter=type
    )


@router.get("/validate", summary="Validate a semantic-layer model")
def validate(
    project: str,
    model: str | None = None,
    deep: bool = False,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Validate a model — basic checks (always) + Snowflake schema probes (``deep``)."""
    return registry.semantic_layer.validate_model(
        alias=project, model_name_or_uuid=model, deep=deep
    )


@router.get("/search-context", summary="Search semantic contexts by name pattern")
def search_context(
    project: str,
    pattern: list[str] = Query(default=["*"]),
    type: str = "all",
    limit: int | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Project-wide glob search across semantic-layer entities.

    Mirrors ``kbagent semantic-layer search-context``. See the service-layer
    docstring for matching semantics and the returned envelope shape.
    """
    return registry.semantic_layer.search_context(
        alias=project, patterns=pattern, type_filter=type, limit=limit
    )


@router.get("/get-context", summary="Fetch one semantic context by id")
def get_context(
    project: str,
    context_id: str,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Single-entry fetch by id; probes every type until found."""
    return registry.semantic_layer.get_context(alias=project, context_id=context_id)


@router.get("/export", summary="Export model snapshot")
def export(
    project: str,
    model: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Export a model + every child entity as an inline JSON snapshot.

    Unlike the CLI which writes to disk by default, the HTTP route returns
    the snapshot in the response body (no file is written server-side).
    """
    # output_path=None on the service writes to a default file path; we
    # need to avoid that for REST. We bypass via export_model + pop the
    # transient path. Simpler: route through the service and let the
    # caller ignore the `path` field. Using a tmp-dir export keeps the
    # service contract intact while avoiding pollution of the CWD.

    with tempfile.TemporaryDirectory(prefix="kbagent-sl-export-") as tmp:
        out = Path(tmp) / "snapshot.json"
        result = registry.semantic_layer.export_model(
            alias=project, model_name_or_uuid=model, output_path=out
        )
    # Strip the now-deleted tmp path from the wire response (the bytes
    # are already captured in the dict).
    result.pop("path", None)
    return result


@router.post("/diff", summary="Diff two semantic-layer snapshots")
def diff(body: DiffRequest, registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    """Diff two snapshots — project↔project, project↔file, file↔file.

    File-backed sides carry the snapshot inline in the body (``file_a`` /
    ``file_b``). When a file side is set we serialize it to a temp file so
    the existing service contract (``Path``) keeps working.
    """

    def _write_tmp(payload: dict[str, Any]) -> Path:
        # `delete=False` is required because we close the handle before
        # the service reads the file (the open handle would let us read
        # but the service takes a Path, not a file object). Cleanup is
        # done explicitly in the finally below.
        fd, tmp_name = tempfile.mkstemp(suffix=".json", prefix="kbagent-sl-diff-")
        try:
            with open(fd, "w", encoding="utf-8") as fh:
                fh.write(_json.dumps(payload))
        except Exception:
            with contextlib.suppress(OSError):
                Path(tmp_name).unlink()
            raise
        return Path(tmp_name)

    file_a_path: Path | None = None
    file_b_path: Path | None = None
    tmps: list[Path] = []
    try:
        if body.file_a is not None:
            file_a_path = _write_tmp(body.file_a)
            tmps.append(file_a_path)
        if body.file_b is not None:
            file_b_path = _write_tmp(body.file_b)
            tmps.append(file_b_path)

        return registry.semantic_layer.diff(
            project_a=body.project_a,
            project_b=body.project_b,
            model_a=body.model_a,
            model_b=body.model_b,
            file_a=file_a_path,
            file_b=file_b_path,
        )
    finally:
        for p in tmps:
            with contextlib.suppress(OSError):
                p.unlink()


@router.post("/items/{kind}", summary="Add an entity to a model")
def add_item(
    kind: ItemKind,
    body: dict[str, Any],
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Add an entity to a model. ``kind`` selects the entity type.

    Per-kind Pydantic body validation is done downstream by binding the
    raw body to the right model before delegating to the service.
    FastAPI rejects unknown ``kind`` values at the framework layer (422)
    via the :data:`ItemKind` ``Literal`` alias.
    """
    svc = registry.semantic_layer
    if kind == "metric":
        m = AddMetric.model_validate(body)
        return svc.add_metric(
            alias=m.project,
            model_name_or_uuid=m.model,
            name=m.name,
            sql=m.sql,
            dataset=m.dataset,
            description=m.description,
            assume_yes=True,
            is_tty=False,
        )
    if kind == "dataset":
        d = AddDataset.model_validate(body)
        return svc.add_dataset(
            alias=d.project,
            model_name_or_uuid=d.model,
            name=d.name,
            table_id=d.table_id,
            description=d.description,
            grain=d.grain,
            primary_key=d.primary_key,
            deep_fields=d.deep_fields,
        )
    if kind == "relationship":
        r = AddRelationship.model_validate(body)
        return svc.add_relationship(
            alias=r.project,
            model_name_or_uuid=r.model,
            name=r.name,
            from_=r.from_,
            to=r.to,
            on=r.on,
            type_=r.type_,
        )
    if kind == "constraint":
        c = AddConstraint.model_validate(body)
        return svc.add_constraint(
            alias=c.project,
            model_name_or_uuid=c.model,
            name=c.name,
            constraint_type=c.constraint_type,
            rule=c.rule,
            metrics=c.metrics,
            severity=c.severity,
        )
    if kind == "glossary":
        g = AddGlossary.model_validate(body)
        return svc.add_glossary(
            alias=g.project,
            model_name_or_uuid=g.model,
            term=g.term,
            definition=g.definition,
        )
    raise HTTPException(
        status_code=404,
        detail=(
            f"Unknown item kind {kind!r}. "
            f"Must be one of: metric, dataset, relationship, constraint, glossary."
        ),
    )


@router.put("/items/{kind}/{name}", summary="Edit a model entity")
def edit_item(
    kind: ItemKind,
    name: str,
    body: dict[str, Any],
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Edit an entity. ``name`` is the current identifier; new-* fields live in the body.

    For ``kind="glossary"``, ``name`` is the current ``term`` (not a stored
    field called ``name``). FastAPI rejects unknown ``kind`` values at the
    framework layer (422) via the :data:`ItemKind` ``Literal`` alias.
    """
    svc = registry.semantic_layer
    if kind == "metric":
        m = EditMetric.model_validate(body)
        return svc.edit_metric(
            alias=m.project,
            model_name_or_uuid=m.model,
            current_name=name,
            new_name=m.new_name,
            new_sql=m.new_sql,
            new_dataset=m.new_dataset,
            new_description=m.new_description,
            assume_yes=True,
            is_tty=False,
        )
    if kind == "dataset":
        d = EditDataset.model_validate(body)
        return svc.edit_dataset(
            alias=d.project,
            model_name_or_uuid=d.model,
            current_name=name,
            new_name=d.new_name,
            new_description=d.new_description,
            new_grain=d.new_grain,
        )
    if kind == "relationship":
        r = EditRelationship.model_validate(body)
        return svc.edit_relationship(
            alias=r.project,
            model_name_or_uuid=r.model,
            current_name=name,
            new_name=r.new_name,
            new_from=r.new_from,
            new_to=r.new_to,
            new_on=r.new_on,
            new_type=r.new_type,
        )
    if kind == "constraint":
        c = EditConstraint.model_validate(body)
        return svc.edit_constraint(
            alias=c.project,
            model_name_or_uuid=c.model,
            current_name=name,
            new_name=c.new_name,
            new_rule=c.new_rule,
            new_constraint_type=c.new_constraint_type,
            new_severity=c.new_severity,
            new_metrics=c.new_metrics,
        )
    if kind == "glossary":
        g = EditGlossary.model_validate(body)
        return svc.edit_glossary(
            alias=g.project,
            model_name_or_uuid=g.model,
            current_term=name,
            new_term=g.new_term,
            new_definition=g.new_definition,
        )
    raise HTTPException(
        status_code=404,
        detail=(
            f"Unknown item kind {kind!r}. "
            f"Must be one of: metric, dataset, relationship, constraint, glossary."
        ),
    )


@router.delete("/items/{kind}/{name}", summary="Remove a model entity")
def remove_item(
    kind: ItemKind,
    name: str,
    project: str,
    model: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Remove a child entity (``--yes`` implicit on REST).

    For ``kind="glossary"``, ``name`` is the term to remove. FastAPI
    rejects unknown ``kind`` values at the framework layer (422) via the
    :data:`ItemKind` ``Literal`` alias.
    """
    return registry.semantic_layer.remove_item(
        alias=project, model_name_or_uuid=model, kind=kind, name=name
    )


@router.post("/import", summary="Import a snapshot into a project")
def import_snapshot(
    body: ImportRequest, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Replay an inline snapshot into a project. Default: skip on conflict."""
    return registry.semantic_layer.import_snapshot_from_dict(
        body.project,
        snapshot=body.snapshot,
        model_name_or_uuid=body.model,
        types=body.types,
        dry_run=body.dry_run,
        overwrite=body.overwrite,
    )


@router.post("/promote", summary="Promote a model between projects")
def promote(
    body: PromoteRequest, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Promote a model from one project to another (additive + overwrite)."""
    return registry.semantic_layer.promote_model(
        from_project=body.from_project,
        to_project=body.to_project,
        from_model=body.from_model,
        to_model=body.to_model,
        types=body.types,
        dry_run=body.dry_run,
    )


@router.post("/build", summary="Build a model from tables")
def build(body: BuildRequest, registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    """Heuristic greenfield builder — synthesize a model from a list of tables."""
    return registry.semantic_layer.build_model(
        alias=body.project,
        table_ids=body.tables,
        model_name=body.name,
        model_name_or_uuid=body.model,
        dry_run=body.dry_run,
        keep_on_failure=body.keep_on_failure,
        types_workspace_id=body.types_workspace_id,
        auto_resolve_types=body.auto_resolve_types,
    )


@router.post("/token/encrypt", summary="Encrypt storage token for transformation")
def token_encrypt(
    body: TokenEncryptRequest, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Encrypt the project's storage token for transformation ``user_properties``."""
    return registry.semantic_layer.encrypt_token(alias=body.project, component_id=body.component_id)


# ── reference-data (dimension-member records, e.g. a Chart of Accounts) ──


@router.get("/reference-data", summary="List reference-data records")
def list_reference_data(
    project: str,
    model: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """List dimension-member records (summaries; use the by-id route for members)."""
    return registry.semantic_layer.list_reference_data(alias=project, model_name_or_uuid=model)


@router.get("/reference-data/{record_id}", summary="Get one reference-data record")
def get_reference_data(
    record_id: str,
    project: str,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Fetch one record (all members) by UUID."""
    return registry.semantic_layer.get_reference_data(alias=project, record_id=record_id)


@router.put("/reference-data", summary="Create or replace a reference-data record")
def set_reference_data(
    body: RefDataSet, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Create or replace (by model + dimension) a record. Idempotent; PUT semantics."""
    return registry.semantic_layer.set_reference_data(
        alias=body.project,
        model_name_or_uuid=body.model,
        dimension=body.dimension,
        members=body.members,
        dataset_id=body.dataset_id,
        description=body.description,
    )


@router.delete("/reference-data/{record_id}", summary="Delete a reference-data record")
def delete_reference_data(
    record_id: str,
    project: str,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Delete a record by UUID (``--yes`` implicit on REST; server-side soft-delete)."""
    return registry.semantic_layer.delete_reference_data(alias=project, record_id=record_id)


# Re-export the closed set of kinds for tests / docs.
__all__ = ["ErrorCode", "ItemKind", "router"]
