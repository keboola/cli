"""Cross-project search."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from ...services.base import resolve_fanout_aliases
from ..dependencies import ALL_PROJECTS_HELP, ServiceRegistry, get_registry

router = APIRouter(prefix="/search", tags=["search"])


@router.get("", summary="Search across projects")
def search(
    query: str,
    project: list[str] | None = Query(None),
    type: list[str] | None = Query(None),
    search_type: str = "textual",
    limit: int = 50,
    regex: bool = False,
    scope: list[str] | None = Query(None),
    all_projects: bool = Query(False, description=ALL_PROJECTS_HELP),
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Search tables, buckets, configs and flows across one or more projects. Mirrors `kbagent search`."""
    aliases = resolve_fanout_aliases(registry.config_store, project, all_projects)
    return registry.search.search(
        query=query,
        aliases=aliases,
        item_types=type,
        search_type=search_type,
        limit=limit,
        regex=regex,
        scopes=scope or [],
    )
