"""Programmatic auth (browser login): PKCE + RFC 8628 device authorization.

Keboola Connection can issue a "programmatic session" -- a short-lived access
token (``kbc_at_*``) plus a rotating refresh token (``kbc_rt_*``) obtained via
a browser login instead of a long-lived static Storage API token. Static
tokens remain fully supported and unchanged; this package only adds an
alternative credential source.

Sub-modules (see docs/programmatic-auth-login-plan.md for the full design):

- ``sentinel``: recognise / build the ``kbc-session://`` placeholder stored
  in ``ProjectConfig.token`` for a session-registered project.
- ``models``: wire models for the auth-service API plus the persisted
  ``auth.json`` shape (``StackSession`` / ``AuthState``).
- ``state_store``: ``AuthStateStore``, the on-disk persistence for sessions.
- ``auth_client``, ``pkce``, ``device``, ``environment``, ``token_provider``:
  the HTTP client, browser/device login flows, and the per-stack token cache
  that keeps a session's access token fresh (built by other packages).

This module re-exports only the pieces implemented so far (sentinel helpers,
wire/persisted models, and the state store) and stays import-light: no
``httpx`` client construction and no filelock acquisition happen merely by
importing ``keboola_agent_cli.auth``.
"""

from __future__ import annotations

from .models import (
    AuthProject,
    AuthState,
    AuthUser,
    CliTokenResponse,
    DeviceAuthorization,
    DevicePollResult,
    DevicePollStatus,
    IntrospectResponse,
    RevokeResult,
    StackSession,
)
from .sentinel import (
    is_session_token,
    make_session_token,
    parse_session_project_id,
    require_static_token,
)
from .state_store import AuthStateStore

__all__ = [
    "AuthProject",
    "AuthState",
    "AuthStateStore",
    "AuthUser",
    "CliTokenResponse",
    "DeviceAuthorization",
    "DevicePollResult",
    "DevicePollStatus",
    "IntrospectResponse",
    "RevokeResult",
    "StackSession",
    "is_session_token",
    "make_session_token",
    "parse_session_project_id",
    "require_static_token",
]
