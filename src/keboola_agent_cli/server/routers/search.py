"""Cross-project search."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/search", tags=["search"])


@router.get("")
def search(
    query: str,
    project: list[str] | None = Query(None),
    type: list[str] | None = Query(None),
    search_type: str = "textual",
    limit: int = 50,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return registry.search.search(
        query=query,
        aliases=project,
        item_types=type,
        search_type=search_type,
        limit=limit,
    )
