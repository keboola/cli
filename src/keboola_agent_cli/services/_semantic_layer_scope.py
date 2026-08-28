"""Scope / target-project / elevation logic for ``semantic-layer scope`` (PSGO-140).

Composes the :class:`MetastoreClient` scope primitives
(``elevate_to_organization``, ``put_target_projects``,
``request_scope_elevation``, ``withdraw_scope_elevation``,
``list_organization_items``) into the operations
:class:`SemanticLayerService` exposes as ``scope_*``. Alias<->numeric
``project_id`` resolution happens here (service layer), not in the client,
following this project's client/service split -- see CLAUDE.md
"Architecture: 3-Layer Design".

Split out (like the other ``_semantic_layer_*.py`` helpers) so the
orchestrator class stays under the CONTRIBUTING.md services LOC ceiling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..errors import ErrorCode, KeboolaApiError

if TYPE_CHECKING:
    from ..config_store import ConfigStore
    from ..metastore_client import MetastoreClient, SemanticType


def resolve_target_project_ids(config_store: ConfigStore, target_projects: list[str]) -> list[int]:
    """Resolve project aliases to numeric Storage project IDs.

    Raises ``NOT_FOUND`` for an unregistered alias, ``CONFIG_ERROR`` for a
    registered project with no numeric ``project_id`` on record (would need
    a ``project refresh``/re-add to populate it -- see
    ``services/org_service.py``, which relies on the same invariant).
    """
    ids: list[int] = []
    for alias in target_projects:
        project = config_store.get_project(alias)
        if project is None:
            raise KeboolaApiError(
                message=f"Unknown project alias {alias!r}. See `kbagent project list`.",
                error_code=ErrorCode.NOT_FOUND,
            )
        if project.project_id is None:
            raise KeboolaApiError(
                message=(
                    f"Project {alias!r} has no numeric project_id on record "
                    "(re-run `kbagent project refresh` or `project add`)."
                ),
                error_code=ErrorCode.CONFIG_ERROR,
            )
        ids.append(project.project_id)
    return ids


def item_status(item: dict[str, Any]) -> dict[str, Any]:
    """Extract the scope/grant/elevation-request fields from a raw item for display."""
    meta = item.get("meta") or {}
    attrs = item.get("attributes") or {}
    return {
        "id": item.get("id"),
        "type": item.get("type"),
        "name": attrs.get("name") or attrs.get("term"),
        "scope": meta.get("scope", "project"),
        "target_project_ids": meta.get("targetProjectIds"),
        "scope_elevation_requested_at": meta.get("scopeElevationRequestedAt"),
        "project_id": meta.get("projectId"),
    }


def grant_target_projects(
    client: MetastoreClient,
    item_type: SemanticType,
    item_id: str,
    *,
    add: list[int] | None = None,
    remove: list[int] | None = None,
    replace: list[int] | None = None,
) -> dict[str, Any]:
    """Update the target-project grant list for a targeted-scope item.

    ``replace`` sends exactly that set, matching the server's native
    replace-only semantics with no extra round trip. ``add``/``remove`` are
    a convenience merge: read the item's current grants, apply the delta,
    then PUT the result. This merge is **not atomic** against a concurrent
    grant change on the same item -- last write wins, same as every other
    read-modify-write in this CLI.
    """
    if replace is not None:
        new_ids = sorted(set(replace))
    else:
        current = client.get_item(item_type, item_id)
        current_ids = set((current.get("meta") or {}).get("targetProjectIds") or [])
        current_ids |= set(add or [])
        current_ids -= set(remove or [])
        new_ids = sorted(current_ids)
    client.put_target_projects(item_type, item_id, new_ids)
    return item_status(client.get_item(item_type, item_id))


def request_elevation(
    client: MetastoreClient, item_type: SemanticType, item_id: str
) -> dict[str, Any]:
    return item_status(client.request_scope_elevation(item_type, item_id))


def withdraw_elevation(
    client: MetastoreClient, item_type: SemanticType, item_id: str
) -> dict[str, Any]:
    return item_status(client.withdraw_scope_elevation(item_type, item_id))


def elevate_to_organization(
    client: MetastoreClient, item_type: SemanticType, item_id: str
) -> dict[str, Any]:
    return item_status(client.elevate_to_organization(item_type, item_id))


def list_pending_elevations(
    client: MetastoreClient,
    item_type: SemanticType,
    *,
    limit: int | None = None,
    offset: int | None = None,
) -> list[dict[str, Any]]:
    items = client.list_organization_items(
        item_type, pending_elevation_only=True, limit=limit, offset=offset
    )
    return [item_status(i) for i in items]
