"""Creator and lifecycle fields of a Storage workspace response."""

from typing import Any


def workspace_provenance_fields(ws: dict[str, Any]) -> dict[str, Any]:
    """Return who created a workspace, when it was last used and its retention dates.

    Every value is passed through as the Storage API reports it. A key the
    stack does not return (an older Connection) is None.
    """
    creator_token = ws.get("creatorToken") or {}
    return {
        "creator_token_id": creator_token.get("id"),
        "creator_token_description": creator_token.get("description"),
        "actor": ws.get("actor"),
        "last_activity_at": ws.get("lastActivityAt"),
        "state": ws.get("state"),
        "warn_at": ws.get("warnAt"),
        "deactivate_at": ws.get("deactivateAt"),
        "delete_at": ws.get("deleteAt"),
    }
