"""Health, version, changelog, and doctor endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

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


@router.get("/version", summary="Show the kbagent version")
def version(registry: ServiceRegistry = Depends(get_registry)) -> dict[str, Any]:
    """Version of kbagent."""
    return registry.version.get_versions()


@router.get("/ui-config", summary="Web UI bootstrap configuration")
def ui_config(request: Request) -> dict[str, Any]:
    """Non-secret switches the web UI reads at boot.

    ``banner`` is ``kbagent serve``'s ``--no-banner`` inverted: false tells the
    SPA to suppress its unsolicited "What's new" popup. A user who explicitly
    asks for the popup (the command palette's "What's new" action) still gets
    it -- the flag governs what appears uninvited, not what the user requests.

    Delivered as an endpoint rather than injected into ``index.html``:

    * There is no injection point to extend. The one that used to exist
      (``window.__KBAGENT_TOKEN``) was deliberately removed in favour of the
      HttpOnly cookie -- see :func:`..app._install_ui` -- and
      ``test_serve_ui.py`` asserts it stays gone.
    * Injection would only cover ``GET /`` and ``GET /index.html``. The SPA
      shell is ALSO served by the StaticFiles ``html=True`` fallback for any
      unmatched path, and that copy would carry no config -- so a deep link
      would silently re-enable the very popup the operator suppressed. For a
      suppression flag, failing open is the wrong direction.

    Reading from ``app.state`` keeps this correct however the shell was
    served, and matches how the SPA already gets ``/version``.
    """
    return {"banner": bool(getattr(request.app.state, "ui_banner", True))}


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
