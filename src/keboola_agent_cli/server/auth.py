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
        if path in PUBLIC_PATHS or path.startswith("/docs") or path.startswith("/redoc"):
            return await call_next(request)

        # Single-process UI mode (`kbagent serve --ui`): the SPA shell
        # (index.html, /assets/*, favicons, client-side route URLs) is
        # served unauthenticated so the browser can boot. The injected
        # ``window.__KBAGENT_TOKEN`` then carries auth on every API call.
        # ``is_ui_public`` is set on app.state by ``_install_ui`` only when
        # ``--ui`` is enabled; in pure-API mode it is absent and this skip
        # never fires.
        is_ui_public = getattr(request.app.state, "is_ui_public", None)
        if callable(is_ui_public) and is_ui_public(request.method, path):
            return await call_next(request)

        header = request.headers.get(self._header, "")
        scheme, _, value = header.partition(" ")
        # EventSource fallback: in single-process UI mode the SPA passes the
        # token as ``?_kbagent_token=...`` because ``EventSource`` cannot
        # carry custom request headers. We accept it iff the header is empty.
        # The header path is preferred for everything else (scripts, fetch,
        # ssePost) so the token never lands in server access logs by default.
        if not value:
            qs_token = request.query_params.get("_kbagent_token", "")
            if qs_token:
                value = qs_token
                scheme = "bearer"
        if scheme.lower() != "bearer" or not value:
            return JSONResponse(
                status_code=401,
                content={
                    "status": "error",
                    "error": {
                        "code": "UNAUTHORIZED",
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
                        "code": "UNAUTHORIZED",
                        "message": "Invalid Bearer token.",
                    },
                },
            )
        return await call_next(request)


def install_auth(app: FastAPI, settings: AuthSettings) -> None:
    """Attach the auth middleware to a FastAPI app."""
    app.add_middleware(BearerAuthMiddleware, settings=settings)
