"""Lineage endpoints (cross-project sharing graph + deep column lineage)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/lineage", tags=["lineage"])


class LineageBuild(BaseModel):
    directory: str
    output: str
    use_ai: bool = False
    refresh: bool = False


class LineageQuery(BaseModel):
    load: str
    upstream: str | None = None
    downstream: str | None = None
    project: str | None = None
    depth: int = 10
    format: str = "text"


@router.get("/edges")
def edges(
    project: list[str] | None = Query(None),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Cross-project bucket-sharing edges (LineageService)."""
    return registry.lineage.get_lineage(aliases=project)


@router.post("/build")
def build(body: LineageBuild, registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    """Build deep column-level lineage and write JSON cache to ``output``.

    Auto-creates the working directory if missing -- it's empty before the
    first ``sync pull`` anyway, so 404'ing on absence was just user-hostile.
    """
    directory = Path(body.directory).resolve()
    output = Path(body.output).resolve()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot create working directory '{body.directory}': {exc}",
        ) from exc
    output.parent.mkdir(parents=True, exist_ok=True)

    if body.refresh:
        registry.sync.pull_all(base_dir=directory)
    result = registry.deep_lineage.build_lineage(directory, generate_ai_tasks=body.use_ai)
    try:
        with output.open("w") as fh:
            json.dump(result, fh, indent=2)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Cannot write '{body.output}': {exc}") from exc
    # Tag the result with the resolved output path so the React UI can
    # auto-load it without re-typing.
    return {**result, "output_path": str(output)}


@router.post("/show")
def show(body: LineageQuery, registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    """Query a built lineage graph (upstream / downstream walk)."""
    load_path = Path(body.load)
    if not load_path.exists():
        raise HTTPException(status_code=400, detail=f"Lineage cache '{body.load}' not found.")
    graph = registry.deep_lineage.load_from_cache(load_path)
    if body.upstream:
        return registry.deep_lineage.query_upstream(
            graph, body.upstream, body.project or "", body.depth
        )
    if body.downstream:
        return registry.deep_lineage.query_downstream(
            graph, body.downstream, body.project or "", body.depth
        )
    raise HTTPException(status_code=400, detail="Provide --upstream or --downstream.")


@router.get("/info")
def info(
    load: str = Query(...), registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    load_path = Path(load)
    if not load_path.exists():
        raise HTTPException(status_code=400, detail=f"Lineage cache '{load}' not found.")
    graph = registry.deep_lineage.load_from_cache(load_path)
    return graph.summary()
