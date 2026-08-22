"""Trash-safety helpers for configuration delete / restore (issue: double-delete purge).

The Storage API gives ``DELETE .../configs/{id}`` two different meanings
depending on state: on a LIVE configuration it is a soft delete into the
trash, but on a configuration ALREADY in the trash it is a permanent purge --
versions, rows and metadata gone, no restore. That second meaning is exactly
what a retrying agent triggers: request times out after the server already
trashed the config, the client retries, and the retry destroys it for good.

These helpers give the service layer a way to never issue that second DELETE:
look the configuration up first, and when it is not live, consult the trash
listing to answer "already trashed" or "does not exist" explicitly.

Kept out of ``config_service.py`` because that module is over its size budget
(``make loc-check``): the service methods stay thin and delegate here.
"""

from __future__ import annotations

from typing import Any

from ..errors import KeboolaApiError

# States a delete/restore attempt can find the configuration in.
STATE_LIVE = "live"
STATE_TRASHED = "trashed"
STATE_MISSING = "missing"


def locate_config(
    client: Any,
    component_id: str,
    config_id: str,
    branch_id: int | None,
) -> str:
    """Answer whether a configuration is live, in the trash, or absent.

    A direct ``GET .../configs/{id}`` answers 404 for BOTH a trashed and a
    never-existed configuration, so a 404 alone cannot drive the delete
    decision -- the trash listing is what separates the two.
    """
    try:
        client.get_config_detail(component_id, config_id, branch_id=branch_id)
        return STATE_LIVE
    except KeboolaApiError as exc:
        if exc.status_code != 404:
            raise
    trashed = client.list_deleted_configs(component_id=component_id, branch_id=branch_id)
    if any(str(cfg.get("id")) == str(config_id) for cfg in trashed):
        return STATE_TRASHED
    return STATE_MISSING


def already_trashed_result(
    alias: str,
    component_id: str,
    config_id: str,
    branch_id: int | None,
) -> dict[str, Any]:
    """The refusal envelope for a delete aimed at an already-trashed config.

    Status ``already_in_trash`` (not ``deleted``) so a caller inspecting the
    result sees that THIS invocation changed nothing -- while an agent
    blindly checking the exit code still gets the idempotent success it
    expects from a retry.
    """
    return {
        "status": "already_in_trash",
        "project_alias": alias,
        "component_id": component_id,
        "config_id": config_id,
        "branch_id": branch_id,
        "message": (
            f"Configuration '{component_id}/{config_id}' is already in the trash; "
            "not deleting again (a second DELETE would purge it permanently). "
            "Use 'kbagent config restore' to bring it back."
        ),
    }


def shape_trash_entry(config: dict[str, Any], component_id: str | None) -> dict[str, Any]:
    """One uniform row for ``config trash-list`` output."""
    current = config.get("currentVersion") or {}
    return {
        "component_id": config.get("component_id") or component_id,
        "config_id": config.get("id"),
        "name": config.get("name"),
        "version": config.get("version"),
        "deleted_change_description": current.get("changeDescription"),
        "deleted_at": current.get("created"),
    }
