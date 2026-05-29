"""Developer Portal reads (list/get).

Only the read surface is exposed over REST. The write commands
(`create`, `patch`, `upload-icon`, `publish`, `deprecate`) are deliberately
CLI-only: they require a human to type a random confirmation code on a real
TTY, which has no meaning over HTTP. Identity management
(`identity add/edit/remove/...`) is likewise CLI-only because it handles
login credentials that must not travel over this API. See
`plugins/kbagent/skills/kbagent/references/dev-portal-workflow.md`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..dependencies import ServiceRegistry, get_registry

router = APIRouter(prefix="/dev-portal", tags=["dev-portal"])


def _resolve_identity(registry: ServiceRegistry, identity: str | None) -> str:
    """Resolve the identity alias: explicit query param, else the configured default."""
    if identity:
        return identity
    default = registry.dev_portal.current_identity()
    if not default:
        raise HTTPException(
            status_code=400,
            detail=(
                "No Developer Portal identity selected. Pass ?identity=<alias>, "
                "or set a default via `kbagent dev-portal identity use <alias>`."
            ),
        )
    return default


@router.get("/apps", summary="List Developer Portal apps for a vendor")
def list_apps(
    vendor: str,
    identity: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> list[dict[str, Any]]:
    """List all apps for a vendor. Mirrors `kbagent dev-portal list --vendor`."""
    alias = _resolve_identity(registry, identity)
    return registry.dev_portal.list_apps(alias, vendor)


@router.get("/apps/{app}", summary="Get one Developer Portal app")
def get_app(
    app: str,
    identity: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Full portal entry for one app. `app` is VENDOR.APP_ID, e.g. keboola.ex-foo.

    Mirrors `kbagent dev-portal get --app`.
    """
    if "." not in app:
        raise HTTPException(
            status_code=400,
            detail=f"app must be in VENDOR.APP_ID form (e.g. keboola.ex-foo), got: {app!r}",
        )
    vendor, _ = app.split(".", 1)
    alias = _resolve_identity(registry, identity)
    return registry.dev_portal.get_app(alias, vendor, app)
