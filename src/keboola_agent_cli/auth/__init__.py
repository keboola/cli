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

This package deliberately re-exports nothing. Importing
``keboola_agent_cli.auth`` executes this module, so a re-export of
``state_store`` would pull ``filelock`` into every process that merely reaches a
sentinel helper -- including the static-token path, which never touches
``auth.json``. Import the submodule you need (``from ..auth.sentinel import
is_session_token``) and the cost stays proportional to what you use.
"""

from __future__ import annotations
