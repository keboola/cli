"""Health, version, changelog, and doctor endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ... import __version__
from ...changelog import CHANGELOG, get_changelog
from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(tags=["health"])


@router.get("/health/ping", summary="Liveness check")
def ping() -> dict[str, Any]:
    """Unauthenticated liveness check."""
    return {"status": "ok", "version": __version__}


@router.get("/health/auth-info", summary="Show authentication scheme")
def auth_info() -> dict[str, Any]:
    """Public info about authentication scheme (no secrets disclosed)."""
    return {
        "scheme": "bearer",
        "header": "Authorization",
        "note": (
            "Send 'Authorization: Bearer <token>'. The token is printed to "
            "stdout when 'kbagent serve' starts."
        ),
    }


@router.get("/version", summary="Show kbagent versions")
def version(registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    """Versions of kbagent, MCP server, and Python."""
    return registry.version.get_versions()


@router.get("/changelog", summary="List release notes")
def changelog(limit: int | None = None) -> dict[str, Any]:
    """Return release entries; pass ``?limit=N`` for the latest N."""
    items = get_changelog(limit=limit) if limit is not None and limit > 0 else dict(CHANGELOG)
    return {
        "entries": [{"version": v, "highlights": notes} for v, notes in items.items()],
    }


@router.get("/doctor", summary="Run health diagnostics")
def doctor(registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    """Run kbagent doctor health checks."""
    return registry.doctor.run_checks()
