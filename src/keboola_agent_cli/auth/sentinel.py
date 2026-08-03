"""Session-token sentinel helpers.

A session-registered project (`kbagent auth login --register-projects`) does
not have a real Storage API token to put in `ProjectConfig.token` -- its
actual credential lives in `auth.json`, keyed by stack URL, and rotates over
time. Instead, `ProjectConfig.token` holds an opaque sentinel string,
`kbc-session://{project_id}`, so `config.json`'s schema (and
`CURRENT_CONFIG_VERSION`) never changes and older kbagent builds keep loading
the file (they just see a project whose "token" happens to look odd).

Every code path that still expects a bearer-style Storage token (not yet
wired through `SessionTokenProvider` / `BearerAuth`) must recognise this
sentinel and fail fast via `require_static_token` instead of sending the
literal sentinel string to an API as if it were a credential.
"""

from __future__ import annotations

from .. import constants
from ..errors import SessionAuthUnsupportedError


def make_session_token(project_id: int) -> str:
    """Build the ``kbc-session://{project_id}`` sentinel stored in ProjectConfig.token."""
    return f"{constants.SESSION_TOKEN_PREFIX}{project_id}"


def is_session_token(token: str) -> bool:
    """True when ``token`` is a session sentinel rather than a static Storage token."""
    return token.startswith(constants.SESSION_TOKEN_PREFIX)


def parse_session_project_id(token: str) -> int | None:
    """Return the project id embedded in a sentinel, or None if not a valid sentinel.

    A sentinel with a non-numeric or empty body (e.g. a hand-edited or
    corrupted ``config.json``) is not a *valid* sentinel for this function --
    it returns None -- but it is still caught by `is_session_token` (prefix
    match only), so guards never mistake it for a usable credential.
    """
    if not is_session_token(token):
        return None
    body = token[len(constants.SESSION_TOKEN_PREFIX) :]
    if not body.isdigit():
        return None
    return int(body)


def require_static_token(token: str, *, feature: str, remedy: str = "") -> None:
    """Fail fast when ``token`` is a session sentinel on a static-token-only path.

    Raises SessionAuthUnsupportedError. Callers pass a human name for the code
    path (e.g. "The MCP subprocess", "kbagent serve", "The importable SDK Client").
    """
    if is_session_token(token):
        raise SessionAuthUnsupportedError(feature, remedy=remedy)
