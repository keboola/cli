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

import asyncio
import contextlib
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__
from ..config_store import ConfigStore, resolve_config_dir
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from .agents_store import AgentStore
from .auth import AuthSettings, install_auth
from .dependencies import ServiceRegistry, install_registry
from .routers import (
    agents,
    ai_chat,
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
    semantic_layer,
    sharing,
    storage,
    workspaces,
)

logger = logging.getLogger(__name__)


def _format_error(
    message: str, error_code: ErrorCode | str, *, http_status: int = 400
) -> JSONResponse:
    """Render a kbagent-style error envelope at the given HTTP status.

    ``error_code`` accepts both :class:`ErrorCode` enum members (the canonical
    surface; matches CLI error envelopes byte-for-byte) and raw strings (for
    forward compatibility when a router wraps a third-party exception whose
    code is not yet in the enum). The :class:`ErrorCode` mixes in ``str``, so
    both shapes serialise as plain strings in the JSON body.
    """
    return JSONResponse(
        status_code=http_status,
        content={
            "status": "error",
            "error": {
                "code": str(error_code),
                "message": message,
            },
        },
    )


def create_app(
    *,
    config_dir: str | None = None,
    auth_token: str | None = None,
    cors_origins: list[str] | None = None,
    serve_url: str | None = None,
    ui_dist: str | None = None,
) -> FastAPI:
    """Build and configure the FastAPI application.

    Args:
        config_dir: Override config directory (matches kbagent --config-dir).
        auth_token: Bearer token clients must send. If None, generates one.
        cors_origins: Allowed CORS origins. Default: localhost dev ports.
        serve_url: Self-URL (``http://host:port``) of this server. Stored on
            the registry so agent subprocesses can be told where to call back.
            If None, defaults are still injected into subprocess env using
            ``127.0.0.1:8001`` -- the serve_command default.
        ui_dist: Optional absolute path to a built React ``dist/`` directory
            (the output of ``npm run build`` in ``web/frontend``). When set,
            the FastAPI app additionally:

            1) accepts ``/api/<path>`` as an alias for the bare ``<path>``
               (the BFF used to re-strip this prefix; in single-process mode
               we do it server-side via an ASGI path-rewrite middleware),
            2) mounts the dist directory at ``/`` so static assets and the
               SPA fallback are served by uvicorn directly,
            3) intercepts ``GET /`` to inject ``window.__KBAGENT_TOKEN`` into
               ``index.html`` so the SPA boots already authenticated -- no
               BFF and no manual paste step.

            If the path does not exist, the UI mount is skipped silently and
            a warning is logged so ``--ui`` typos don't break the API path.

    Returns:
        Configured FastAPI app ready for uvicorn.
    """
    resolved_token = auth_token or secrets.token_urlsafe(32)

    @asynccontextmanager
    async def _lifespan(app_: FastAPI):
        # Start the agent scheduler loop in the background once services
        # exist (registry is installed below before include_router calls).
        from .agent_runner import scheduler_loop

        scheduler_task = None
        store = getattr(app_.state, "agent_store", None)
        registry_ = getattr(app_.state, "registry", None)
        if store is not None and registry_ is not None:
            scheduler_task = asyncio.create_task(scheduler_loop(store, registry_))
            logger.info("Agent scheduler task spawned")
        try:
            yield
        finally:
            if scheduler_task is not None:
                scheduler_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await scheduler_task

    app = FastAPI(
        lifespan=_lifespan,  # type: ignore[arg-type]
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
    registry = ServiceRegistry(
        config_store=config_store,
        serve_url=serve_url,
        serve_token=resolved_token,
    )
    install_registry(app, registry)

    app.state.agent_store = AgentStore(resolved_dir)

    from .run_broadcaster import install_broadcaster

    install_broadcaster(app)

    @app.exception_handler(ConfigError)
    async def _config_error_handler(_request, exc: ConfigError):
        return _format_error(str(exc), ErrorCode.CONFIG_ERROR, http_status=400)

    @app.exception_handler(KeboolaApiError)
    async def _api_error_handler(_request, exc: KeboolaApiError):
        code = getattr(exc, "error_code", ErrorCode.API_ERROR)
        msg = getattr(exc, "message", str(exc)) or str(exc)
        return _format_error(msg, code, http_status=502)

    @app.exception_handler(StarletteHTTPException)
    async def _starlette_handler(_request, exc: StarletteHTTPException):
        return _format_error(
            exc.detail or "HTTP error", ErrorCode.HTTP_ERROR, http_status=exc.status_code
        )

    @app.exception_handler(Exception)
    async def _generic_handler(_request, exc: Exception):
        logger.exception("Unhandled error: %s", exc)
        return _format_error(str(exc) or repr(exc), ErrorCode.INTERNAL_ERROR, http_status=500)

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
    app.include_router(ai_chat.router)
    app.include_router(encrypt.router)
    app.include_router(search.router)
    app.include_router(semantic_layer.router)
    app.include_router(org.router)
    app.include_router(agents.router)

    app.state.auth_token = resolved_token

    if ui_dist:
        _install_ui(app, ui_dist=ui_dist, token=resolved_token)

    return app


_SESSION_COOKIE_NAME = "kbagent_session"


def _install_ui(app: FastAPI, *, ui_dist: str, token: str) -> None:
    """Mount the built React SPA at ``/`` and bridge ``/api/*`` to bare routes.

    Three pieces:

    1) **Path-rewrite middleware** for ``/api/<rest>`` -> ``<rest>``. Mounted
       BEFORE auth so the auth header check runs against the rewritten path
       (auth doesn't care about path, but PUBLIC_PATHS exact-matches do).
    2) **Cookie-setting** ``GET /`` and ``GET /index.html``: read the built
       ``index.html``, return it with a ``Set-Cookie: kbagent_session=<token>;
       HttpOnly; SameSite=Strict; Path=/`` header. Public (no auth) so the
       SPA can bootstrap. The browser then attaches the cookie to every
       same-origin REST + SSE request automatically. The token is HttpOnly
       (no JS access -- XSS-resistant), SameSite=Strict (no cross-origin
       sends -- CSRF-resistant), and lives only for the browser session.

       This replaces the older "inject ``window.__KBAGENT_TOKEN`` into a
       ``<script>`` tag" approach. The injected token landed in the JS heap
       (XSS-readable) and the EventSource fallback (``?_kbagent_token=...``
       query param) put it into uvicorn's access log -- both attack surfaces
       are gone with the cookie-only design.
    3) **StaticFiles mount at ``/``** with ``html=True`` so missing paths fall
       through to ``index.html`` for SPA client-side routing.

    The mount is appended *after* all API routers, so any registered route
    (``/projects``, ``/configs``, ``/agents``, ...) wins over a hypothetical
    static file with the same name.
    """
    from pathlib import Path

    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles
    from starlette.middleware.base import BaseHTTPMiddleware

    dist = Path(ui_dist).expanduser().resolve()
    if not dist.exists() or not (dist / "index.html").exists():
        logger.warning(
            "UI dist path %s missing index.html -- skipping UI mount. "
            "Did you run `make web-build`?",
            dist,
        )
        return

    class _ApiAliasMiddleware(BaseHTTPMiddleware):
        """Rewrite ``/api/<path>`` -> ``/<path>`` so the SPA can keep its
        existing ``/api/*`` calls unchanged when the BFF is removed.

        Done in middleware (not via APIRouter prefix) so we can flip a single
        scope key and reuse every existing router unchanged.
        """

        async def dispatch(self, request, call_next):  # type: ignore[override]
            path = request.scope.get("path", "")
            if path.startswith("/api/") or path == "/api":
                stripped = path[4:] or "/"
                request.scope["path"] = stripped
                # raw_path is bytes; some Starlette internals consult it.
                request.scope["raw_path"] = stripped.encode("utf-8")
            return await call_next(request)

    app.add_middleware(_ApiAliasMiddleware)

    # Allow unauthenticated access to the bootstrap HTML so the browser can
    # load it and pick up the session cookie. Static assets (JS/CSS/icons)
    # under /assets/* are also public -- they carry no secrets.
    from .auth import PUBLIC_PATHS as _AUTH_PUBLIC_PATHS  # noqa: F401  (touch to confirm import)

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    async def _serve_ui_index() -> HTMLResponse:
        html = (dist / "index.html").read_text(encoding="utf-8")
        response = HTMLResponse(html)
        # Browser session cookie: HttpOnly + SameSite=Strict + Path=/. No
        # ``Secure`` flag because kbagent serve defaults to plain http on
        # 127.0.0.1; setting Secure would prevent the cookie from ever being
        # set on a localhost dev install. Operators running `--host 0.0.0.0`
        # behind TLS termination should layer Secure via the proxy.
        # ``max_age`` deliberately omitted -> cookie is a session cookie
        # (cleared when the browser closes), matching the token's
        # one-per-process lifecycle.
        response.set_cookie(
            key=_SESSION_COOKIE_NAME,
            value=token,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    # SPA fallback + assets. ``html=True`` makes StaticFiles serve index.html
    # for unknown paths (so /workspaces, /jobs, etc. client-side routes work
    # on direct navigation). The auth middleware will still gate API calls;
    # static files are served before it sees them only because StaticFiles
    # is the LAST mount and middleware runs on the unified scope -- so we
    # widen PUBLIC_PATHS via prefix logic in the auth middleware itself.
    # Quick path: short-circuit auth for non-/api GETs by extending the
    # bypass list at install time.
    _allow_static_through_auth(app)

    app.mount("/", StaticFiles(directory=str(dist), html=True), name="ui")


def _allow_static_through_auth(app: FastAPI) -> None:
    """Mark the SPA's GET-only static surface as auth-public.

    The auth middleware already exempts ``PUBLIC_PATHS`` (docs, openapi,
    health). For the UI mode we also need to let through the SPA shell:
    ``GET /``, ``GET /index.html``, ``GET /assets/*``, favicons, and the
    SPA's client-side routes (which all resolve to index.html via the
    StaticFiles ``html=True`` fallback).

    Implemented by stashing a predicate on ``app.state`` that the auth
    middleware consults; we don't import-cycle by editing the auth module
    here.
    """

    def _is_ui_public(method: str, path: str) -> bool:
        if method != "GET":
            return False
        if path == "/" or path == "/index.html":
            return True
        if path.startswith("/assets/"):
            return True
        if path in {"/favicon.svg", "/favicon.ico", "/manifest.json"}:
            return True
        # Any non-API GET that doesn't look like an API endpoint we own:
        # treat as a SPA route and let StaticFiles serve index.html.
        # API routes always start with a known prefix; everything else
        # falls to the SPA. Use a tight allow-list to avoid leaking auth
        # bypass to API routes that might be added in future.
        api_prefixes = (
            "/projects",
            "/configs",
            "/components",
            "/storage",
            "/jobs",
            "/branches",
            "/workspaces",
            "/flows",
            "/schedules",
            "/lineage",
            "/sharing",
            "/data-apps",
            "/mcp",
            "/kai",
            "/encrypt",
            "/search",
            "/semantic-layer",
            "/org",
            "/agents",
            "/members",
            "/health",
            "/docs",
            "/redoc",
            "/openapi.json",
            "/api/",
        )
        return not any(path == p or path.startswith(p + "/") for p in api_prefixes)

    app.state.is_ui_public = _is_ui_public


__all__ = ["create_app"]
