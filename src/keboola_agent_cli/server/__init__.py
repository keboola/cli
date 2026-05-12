"""FastAPI HTTP server for kbagent.

Wraps all services as REST endpoints. Designed to be consumed by the
web/backend Node.js BFF (which in turn serves the React UI in web/frontend).

Key design choices:
- Bearer token auth (random secret generated at startup, printed to stdout)
- Localhost-only by default (bind 127.0.0.1)
- Per-request X-Manage-Token header for write ops requiring manage token
- Reuses existing services with their existing dict return shapes
- SSE for streaming: job log tail, branch reset progress, kai chat
"""

from __future__ import annotations

import logging
import secrets

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__
from ..config_store import ConfigStore, resolve_config_dir
from ..errors import ConfigError, KeboolaApiError
from .auth import AuthSettings, install_auth
from .dependencies import ServiceRegistry, install_registry
from .routers import (
    branches,
    components,
    configs,
    data_apps,
    encrypt,
    flows,
    health,
    jobs,
    kai,
    lineage,
    mcp,
    members,
    org,
    projects,
    schedules,
    search,
    sharing,
    storage,
    workspaces,
)

logger = logging.getLogger(__name__)


def _format_error(message: str, error_code: str, *, http_status: int = 400) -> JSONResponse:
    """Render a kbagent-style error envelope at the given HTTP status."""
    return JSONResponse(
        status_code=http_status,
        content={
            "status": "error",
            "error": {
                "code": error_code,
                "message": message,
            },
        },
    )


def create_app(
    *,
    config_dir: str | None = None,
    auth_token: str | None = None,
    cors_origins: list[str] | None = None,
) -> FastAPI:
    """Build and configure the FastAPI application.

    Args:
        config_dir: Override config directory (matches kbagent --config-dir).
        auth_token: Bearer token clients must send. If None, generates one.
        cors_origins: Allowed CORS origins. Default: localhost dev ports.

    Returns:
        Configured FastAPI app ready for uvicorn.
    """
    resolved_token = auth_token or secrets.token_urlsafe(32)

    app = FastAPI(
        title="kbagent serve",
        description=(
            "HTTP API surface for kbagent. Wraps all kbagent CLI commands as "
            "REST endpoints. Designed for the kbagent web UI (web/backend + "
            "web/frontend) but consumable by any HTTP client."
        ),
        version=__version__,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins
        or [
            "http://localhost:5173",  # Vite dev default
            "http://localhost:8000",  # Node BFF
            "http://127.0.0.1:5173",
            "http://127.0.0.1:8000",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    install_auth(app, AuthSettings(token=resolved_token))

    resolved_dir, source = resolve_config_dir(cli_config_dir=config_dir)
    config_store = ConfigStore(config_dir=resolved_dir, source=source)
    registry = ServiceRegistry(config_store=config_store)
    install_registry(app, registry)

    @app.exception_handler(ConfigError)
    async def _config_error_handler(_request, exc: ConfigError):
        return _format_error(str(exc), "CONFIG_ERROR", http_status=400)

    @app.exception_handler(KeboolaApiError)
    async def _api_error_handler(_request, exc: KeboolaApiError):
        code = getattr(exc, "error_code", "API_ERROR")
        msg = getattr(exc, "message", str(exc)) or str(exc)
        return _format_error(msg, code, http_status=502)

    @app.exception_handler(StarletteHTTPException)
    async def _starlette_handler(_request, exc: StarletteHTTPException):
        return _format_error(exc.detail or "HTTP error", "HTTP_ERROR", http_status=exc.status_code)

    @app.exception_handler(Exception)
    async def _generic_handler(_request, exc: Exception):
        logger.exception("Unhandled error: %s", exc)
        return _format_error(str(exc) or repr(exc), "INTERNAL_ERROR", http_status=500)

    app.include_router(health.router)
    app.include_router(projects.router)
    app.include_router(members.router)
    app.include_router(configs.router)
    app.include_router(components.router)
    app.include_router(storage.router)
    app.include_router(jobs.router)
    app.include_router(branches.router)
    app.include_router(workspaces.router)
    app.include_router(flows.router)
    app.include_router(schedules.router)
    app.include_router(lineage.router)
    app.include_router(sharing.router)
    app.include_router(data_apps.router)
    app.include_router(mcp.router)
    app.include_router(kai.router)
    app.include_router(encrypt.router)
    app.include_router(search.router)
    app.include_router(org.router)

    app.state.auth_token = resolved_token
    return app


__all__ = ["create_app"]
