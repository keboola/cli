"""Lineage endpoints (cross-project sharing graph + deep column lineage).

The ``/browser`` family mirrors what ``kbagent lineage server`` does as a
standalone CLI subcommand -- HTML browser + data.json + query API +
Mermaid renderer -- but mounted into the FastAPI app so the React UI can
``<iframe>`` it instead of duplicating the rendering logic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse
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


@router.get("/edges", summary="List cross-project lineage edges")
def edges(
    project: list[str] | None = Query(None),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Cross-project bucket-sharing edges (LineageService)."""
    return registry.lineage.get_lineage(aliases=project)


@router.post("/build", summary="Build deep column-level lineage")
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


@router.post("/show", summary="Query a built lineage graph")
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


@router.get("/info", summary="Show lineage cache summary")
def info(
    load: str = Query(...), registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Return a summary of nodes/edges in a built lineage cache. Mirrors `kbagent lineage info`."""
    load_path = Path(load)
    if not load_path.exists():
        raise HTTPException(status_code=400, detail=f"Lineage cache '{load}' not found.")
    graph = registry.deep_lineage.load_from_cache(load_path)
    return graph.summary()


# ── Browser surface ───────────────────────────────────────────────
# Reuses the HTML template + handler logic from `kbagent lineage server`
# (commands/lineage.py) so we don't have a second copy of the renderer.
# The CLI command serves them on a fresh http.server thread; here we
# re-host them inside FastAPI so the React UI can iframe the result.


def _rewrite_browser_html(load_path: str) -> str:
    """Return the lineage browser HTML with fetch URLs rewritten.

    The original template (from commands/lineage.py) hard-codes
    ``/data.json`` / ``/api/query`` / ``/api/mermaid`` paths because
    ``kbagent lineage server`` is the only thing serving the request.
    Inside FastAPI we live under ``/lineage/...`` and the BFF proxies
    that under ``/api/lineage/...`` for the browser. Rewrite the three
    fetch endpoints so the same HTML works inside an iframe.

    The ``load`` query param is baked into every rewritten URL so the
    browser never has to know which JSON cache to read -- one HTML
    response = one cache file.
    """
    from ...commands.lineage import _LINEAGE_HTML_TEMPLATE

    enc = quote(load_path, safe="")
    return (
        _LINEAGE_HTML_TEMPLATE
        # /data.json -> /api/lineage/data?load=...
        .replace('"/data.json"', f'"/api/lineage/data?load={enc}"')
        # /api/query?node=X... -> /api/lineage/walk?load=...&node=X...
        .replace('"/api/query?node="', f'"/api/lineage/walk?load={enc}&node="')
        # /api/mermaid?node=X... -> /api/lineage/mermaid?load=...&node=X...
        .replace('"/api/mermaid?node="', f'"/api/lineage/mermaid?load={enc}&node="')
    )


@router.get("/browser", response_class=HTMLResponse, summary="Open lineage browser UI")
def browser(load: str = Query(...)) -> HTMLResponse:
    """Serve the interactive lineage browser HTML for a JSON cache file."""
    load_path = Path(load)
    if not load_path.exists():
        raise HTTPException(status_code=400, detail=f"Lineage cache '{load}' not found.")
    return HTMLResponse(content=_rewrite_browser_html(load))


@router.get("/data", summary="Return raw lineage JSON")
def data(load: str = Query(...)) -> Any:
    """Raw lineage JSON, served verbatim from disk for the browser."""
    load_path = Path(load)
    if not load_path.exists():
        raise HTTPException(status_code=400, detail=f"Lineage cache '{load}' not found.")
    try:
        raw = load_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Cannot read '{load}': {exc}") from exc
    # Hand the parsed dict back so FastAPI sets the right content-type and
    # the browser fetch().json() call works without surprises.
    return json.loads(raw)


@router.get("/walk", summary="Walk lineage graph from a node")
def walk(
    load: str = Query(...),
    node: str = Query(...),
    direction: str = Query("downstream"),
    depth: int = Query(3, ge=1, le=20),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Walk the lineage graph upstream / downstream from a node FQN.

    Used by the in-browser query bar (``/api/query`` in the standalone
    server). Mirrors the CLI ``lineage show`` semantics but is GET-only
    so the HTML's ``fetch()`` calls work without preflight.
    """
    load_path = Path(load)
    if not load_path.exists():
        raise HTTPException(status_code=400, detail=f"Lineage cache '{load}' not found.")
    graph = registry.deep_lineage.load_from_cache(load_path)
    if direction == "upstream":
        return registry.deep_lineage.query_upstream(graph, node, "", depth)
    return registry.deep_lineage.query_downstream(graph, node, "", depth)


@router.get("/mermaid", response_class=PlainTextResponse, summary="Render lineage as Mermaid")
def mermaid(
    load: str = Query(...),
    node: str = Query(...),
    direction: str = Query("downstream"),
    depth: int = Query(3, ge=1, le=20),
    view: str = Query("flow"),
    columns: str = Query("false"),
    registry: ServiceRegistry = Depends(get_registry),
) -> PlainTextResponse:
    """Return a Mermaid diagram for the requested upstream/downstream walk."""
    from ...services.deep_lineage_service import DeepLineageService

    load_path = Path(load)
    if not load_path.exists():
        raise HTTPException(status_code=400, detail=f"Lineage cache '{load}' not found.")
    graph = registry.deep_lineage.load_from_cache(load_path)

    if direction == "upstream":
        result = registry.deep_lineage.query_upstream(graph, node, "", depth)
    else:
        result = registry.deep_lineage.query_downstream(graph, node, "", depth)

    if "error" in result:
        return PlainTextResponse("graph LR\n  error[" + result["error"].replace('"', "'") + "]")
    edges = result.get("edges", [])
    show_cols = columns == "true"
    if view == "er":
        code = DeepLineageService.render_er_diagram(edges, graph, node, show_columns=show_cols)
    else:
        code = DeepLineageService.render_mermaid(
            edges, graph, direction, node, show_columns=show_cols, warnings=result.get("warnings")
        )
    return PlainTextResponse(code)
