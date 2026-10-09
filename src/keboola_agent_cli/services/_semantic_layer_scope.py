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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..errors import ErrorCode, KeboolaApiError

if TYPE_CHECKING:
    from ..config_store import ConfigStore
    from ..metastore_client import MetastoreClient, ObjectScope, SemanticType

DEFAULT_REQUEST_LIST_LIMIT = 50


def parse_target_projects(values: list[str] | None) -> list[str]:
    """Flatten repeatable and comma-separated ``--target-project`` values, de-duplicated."""
    seen: dict[str, None] = {}
    for value in values or []:
        for part in value.split(","):
            if part.strip():
                seen.setdefault(part.strip())
    return list(seen)


def resolve_target_project_ids(
    config_store: ConfigStore, owner_alias: str, targets: list[str] | None
) -> list[int]:
    """Resolve ``--target-project`` values (alias or numeric project ID) to project IDs.

    A registered alias wins; otherwise an all-digit value is taken as a project
    ID, so a target does not have to be registered in kbagent. An alias must be
    on the owner project's stack -- a project ID is only meaningful there, and
    an ID cannot be checked, so it is trusted as being on that stack. Any bad
    value is ``INVALID_ARGUMENT`` (a usage error) naming the option.
    """
    owner = config_store.get_project(owner_alias)
    ids: list[int] = []
    for target in parse_target_projects(targets):
        project = config_store.get_project(target)
        if project is None:
            if not target.isdigit():
                raise KeboolaApiError(
                    message=(
                        f"--target-project {target!r} is neither a registered project alias "
                        "nor a numeric project ID. See `kbagent project list`."
                    ),
                    error_code=ErrorCode.INVALID_ARGUMENT,
                )
            ids.append(int(target))
            continue
        if owner is not None and project.stack_url.rstrip("/") != owner.stack_url.rstrip("/"):
            raise KeboolaApiError(
                message=(
                    f"--target-project {target!r} is on a different stack than {owner_alias!r}; "
                    "a project ID only means the same project on its own stack."
                ),
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
        if project.project_id is None:
            raise KeboolaApiError(
                message=(
                    f"--target-project {target!r} has no numeric project_id on record "
                    "(re-run `kbagent project refresh` or `project add`)."
                ),
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
        ids.append(project.project_id)
    return list(dict.fromkeys(ids))


def item_scope(item: dict[str, Any]) -> tuple[ObjectScope, list[int] | None]:
    """``(scope, target_project_ids)`` from a raw item's ``meta`` block (``project`` if absent)."""
    meta = item.get("meta") or {}
    return meta.get("scope", "project"), meta.get("targetProjectIds")


def inherited_scope(
    client: MetastoreClient, model_uuid: str
) -> tuple[ObjectScope, list[int] | None]:
    """The scope a child item takes when ``--scope`` is omitted: its model's own."""
    scope, targets = item_scope(client.get_item("semantic-model", model_uuid))
    return scope, (targets or None) if scope == "targeted" else None


@dataclass(frozen=True)
class NewItemScope:
    """The scope a copy command (`import`, `promote`) gives the items it creates.

    ``inherited`` is True when the caller did not pass ``--scope`` and the scope is the target
    model's own; :func:`post_child` then explains a 403 on an inherited ``organization`` scope.
    """

    scope: ObjectScope = "project"
    target_project_ids: list[int] | None = None
    inherited: bool = True


def post_child(
    client: MetastoreClient,
    item_type: SemanticType,
    *,
    name: str,
    data: dict[str, Any],
    scope: ObjectScope,
    target_project_ids: list[int] | None,
    inherited: bool,
    remedy: str,
) -> dict[str, Any]:
    """POST a child item at ``scope``; explain a 403 on an inherited ``organization`` scope.

    The metastore ACL lets only an organization admin create at ``organization`` scope. When the
    caller did not choose that scope (``inherited``), the bare 403 does not say why, so the error
    names the model's scope and ``remedy`` (the caller-specific way out).
    """
    try:
        return client.post_item(
            item_type, name=name, data=data, scope=scope, target_project_ids=target_project_ids
        )
    except KeboolaApiError as exc:
        if not (
            inherited and scope == "organization" and exc.error_code == ErrorCode.ACCESS_DENIED
        ):
            raise
        raise KeboolaApiError(
            message=(
                f"{exc.message} The model is {scope!r}-scoped, so this child inherits that "
                f"scope; creating at it needs an organization-admin token. {remedy}"
            ),
            status_code=exc.status_code,
            error_code=exc.error_code,
            retryable=False,
        ) from exc


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


def _merge_targets(
    item: dict[str, Any], caller_project_id: int | None, add: list[int], remove: list[int]
) -> list[int]:
    """Apply an add/remove delta to the item's grants, refusing when they cannot be read.

    The server returns ``meta.targetProjectIds`` only to the owning project (and
    omits it when the list is empty), so a non-owner (an org admin from
    elsewhere) would read ``None``, apply the delta to an empty list, and PUT it
    -- silently replacing every grant. The owner check is on ``meta.projectId``,
    never on the list being empty: an owner with no grants is a valid merge base.
    """
    if caller_project_id is None:
        raise KeboolaApiError(
            message=(
                "This project's own ID is unknown, so ownership of the item cannot be checked. "
                "Run `kbagent project refresh` (or `project add`) first."
            ),
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
    meta = item.get("meta") or {}
    owner_id = meta.get("projectId")
    if owner_id is not None and owner_id != caller_project_id:
        raise KeboolaApiError(
            message=(
                "This project does not own the item, so its current target projects cannot "
                "be read and add/remove would overwrite them. Use `scope set --target-project` "
                "to replace the whole list from the owning project's view."
            ),
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
    return sorted((set(meta.get("targetProjectIds") or []) | set(add)) - set(remove))


def set_target_projects(
    client: MetastoreClient,
    item_type: SemanticType,
    item_id: str,
    *,
    replace: list[int] | None = None,
    add: list[int] | None = None,
    remove: list[int] | None = None,
    caller_project_id: int | None = None,
) -> dict[str, Any]:
    """Update a targeted-scope item's grants: replace the list, or merge an add/remove delta.

    ``replace`` is the server's native replace-only PUT (``[]`` clears). A merge
    is read-modify-write and not atomic against a concurrent grant change.
    """
    if replace is not None:
        new_ids = sorted(set(replace))
    else:
        current = client.get_item(item_type, item_id)
        new_ids = _merge_targets(current, caller_project_id, add or [], remove or [])
    client.put_target_projects(item_type, item_id, new_ids)
    return item_status(client.get_item(item_type, item_id))


# The metastore's 400 when the item's STORED schema version does not support the scope (go-monorepo
# `ErrScopeNotSupported`): elevation and an elevation request are checked against that stored
# version, and items created before kbagent 0.97.0 pinned `schemaVersion` 1.0.0 (project scope only).
_SCOPE_NOT_SUPPORTED = "not supported for object type"


def _raise_if_old_schema(
    client: MetastoreClient, item_type: SemanticType, item_id: str, exc: KeboolaApiError
) -> None:
    """Re-raise the bare "scope not supported" 400 as an error that names the cause and the way out."""
    if exc.status_code != 400 or _SCOPE_NOT_SUPPORTED not in exc.message:
        return
    version = (client.get_item(item_type, item_id).get("meta") or {}).get("schemaVersion")
    raise KeboolaApiError(
        message=(
            f"{item_type} {item_id!r} cannot be made organization-wide: it is stored at schema "
            f"version {version}, which supports only project scope. Create it again to store it at "
            "the current schema version: `semantic-layer export` the model, delete it, create it "
            "again and `import` the export. Item names are unique per project, so delete the old "
            "model before the import."
        ),
        status_code=exc.status_code,
        error_code=exc.error_code,
        retryable=False,
    ) from exc


def request_elevation(
    client: MetastoreClient, item_type: SemanticType, item_id: str
) -> dict[str, Any]:
    """Flag an item as awaiting an org-admin's step-up decision.

    Re-reads the item instead of rendering the endpoint's own response:
    the ``scope-elevation-request`` (and ``PATCH``) response bodies omit
    ``meta.targetProjectIds``, so reporting them directly prints
    ``target_project_ids: null`` for an item whose grants are actually
    intact. Same re-read pattern as :func:`set_target_projects`.
    """
    try:
        client.request_scope_elevation(item_type, item_id)
    except KeboolaApiError as exc:
        _raise_if_old_schema(client, item_type, item_id, exc)
        raise
    return item_status(client.get_item(item_type, item_id))


def withdraw_elevation(
    client: MetastoreClient, item_type: SemanticType, item_id: str
) -> dict[str, Any]:
    """Clear a pending elevation request. Re-reads for the reason in :func:`request_elevation`."""
    client.withdraw_scope_elevation(item_type, item_id)
    return item_status(client.get_item(item_type, item_id))


def elevate_to_organization(
    client: MetastoreClient, item_type: SemanticType, item_id: str, *, dry_run: bool = False
) -> dict[str, Any]:
    """Step an item up to organization scope (one-way). ``dry_run`` only reports the plan."""
    if dry_run:
        status = item_status(client.get_item(item_type, item_id))
        return {**status, "dry_run": True, "would_set_scope": "organization"}
    try:
        client.elevate_to_organization(item_type, item_id)
    except KeboolaApiError as exc:
        _raise_if_old_schema(client, item_type, item_id, exc)
        raise
    return item_status(client.get_item(item_type, item_id))


def list_pending_elevations(
    client: MetastoreClient,
    item_type: SemanticType,
    *,
    limit: int = DEFAULT_REQUEST_LIST_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """One page of elevation requests; ``has_more`` comes from fetching one row past ``limit``."""
    items = client.list_organization_items(
        item_type, pending_elevation_only=True, limit=limit + 1, offset=offset
    )
    return {
        "items": [item_status(i) for i in items[:limit]],
        "limit": limit,
        "offset": offset,
        "has_more": len(items) > limit,
    }
