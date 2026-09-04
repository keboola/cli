"""Bearer token auth middleware for kbagent serve.

Generates a single token at startup; clients must send it as
``Authorization: Bearer <token>`` on every request. Public paths
(``/docs``, ``/openapi.json``, ``/health/auth-info``) are allowed
without auth so the UI can bootstrap.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from ..errors import ErrorCode

logger = logging.getLogger(__name__)

PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/docs",
        "/redoc",
        "/openapi.json",
        "/health/ping",
    }
)


@dataclass(frozen=True, slots=True)
class AuthSettings:
    """Bearer-auth configuration for the FastAPI app."""

    token: str
    header_name: str = "authorization"


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests without a matching ``Authorization: Bearer <token>`` header.

    Token comparison uses :func:`hmac.compare_digest` to mitigate timing attacks.
    Public paths (docs, openapi, ping) are unauthenticated so the UI can
    discover the server without first knowing the token.
    """

    def __init__(self, app: ASGIApp, settings: AuthSettings) -> None:
        super().__init__(app)
        self._token = settings.token
        self._header = settings.header_name

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith(("/docs", "/redoc")):
            return await call_next(request)

        # Single-process UI mode (`kbagent serve --ui`): the SPA shell
        # (index.html, /assets/*, favicons, client-side route URLs) is
        # served unauthenticated so the browser can boot. The HttpOnly
        # ``kbagent_session`` cookie set on ``GET /`` then carries auth
        # on every same-origin API call (see the cookie branch below).
        # ``is_ui_public`` is set on app.state by ``_install_ui`` only when
        # ``--ui`` is enabled; in pure-API mode it is absent and this skip
        # never fires.
        is_ui_public = getattr(request.app.state, "is_ui_public", None)
        if callable(is_ui_public) and is_ui_public(request.method, path):
            return await call_next(request)

        header = request.headers.get(self._header, "")
        scheme, _, value = header.partition(" ")
        # Browser fallback: in single-process UI mode the SPA reaches the
        # API on the same origin via ``credentials: "include"``, so the
        # browser attaches a HttpOnly ``kbagent_session`` cookie set by
        # ``GET /`` (see ``server.__init__._install_ui``). We accept it
        # only when no Authorization header was present, so scripted callers
        # (``kbagent http``, curl, BFF) keep the header path -- the token
        # therefore never lands in URLs / access logs and never in any
        # JS-readable surface.
        if not value:
            cookie_token = request.cookies.get("kbagent_session", "")
            if cookie_token:
                value = cookie_token
                scheme = "bearer"
        if scheme.lower() != "bearer" or not value:
            return JSONResponse(
                status_code=401,
                content={
                    "status": "error",
                    "error": {
                        "code": str(ErrorCode.UNAUTHORIZED),
                        "message": "Missing Bearer token. Set Authorization header.",
                    },
                },
            )
        if not hmac.compare_digest(value, self._token):
            return JSONResponse(
                status_code=401,
                content={
                    "status": "error",
                    "error": {
                        "code": str(ErrorCode.UNAUTHORIZED),
                        "message": "Invalid Bearer token.",
                    },
                },
            )
        return await call_next(request)


def install_auth(app: FastAPI, settings: AuthSettings) -> None:
    """Attach the auth middleware to a FastAPI app."""
    app.add_middleware(BearerAuthMiddleware, settings=settings)
