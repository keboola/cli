"""Project-wide context search / lookup helpers for :mod:`semantic_layer_service`.

Split out so :class:`SemanticLayerService` stays under the CONTRIBUTING.md
services hard ceiling (1,500 LOC). Each helper opens + closes its own
metastore client via the factory the service injects; the service methods
are 1-line delegators.

Helpers:

- :func:`run_search_context` -- project-wide glob search across semantic-layer
  entity names (mirrors MCP ``search_semantic_context``).
- :func:`run_get_context` -- single fetch by id, irrespective of type (mirrors
  MCP ``get_semantic_context``).
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING, Any

from ..errors import ErrorCode, KeboolaApiError

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..metastore_client import MetastoreClient, SemanticType


# Probed first by :func:`run_get_context`; child types follow the canonical
# iteration order so the sweep is deterministic.
_MODEL_TYPE: SemanticType = "semantic-model"


def _strip_semantic_prefix(wire_type: str) -> str:
    """``"semantic-dataset"`` -> ``"dataset"`` for the CLI surface."""
    return wire_type[len("semantic-") :] if wire_type.startswith("semantic-") else wire_type


def _matches_any_pattern(name: str, patterns: list[str]) -> bool:
    """Case-sensitive ``fnmatch`` against any of the supplied patterns."""
    return any(fnmatch.fnmatchcase(name, pat) for pat in patterns)


def _resolve_search_types(
    type_filter: str | None,
    child_types: tuple[SemanticType, ...],
    type_alias: dict[str, SemanticType],
) -> tuple[SemanticType, ...]:
    """Map the CLI ``--type`` flag to the list of wire types to scan."""
    if type_filter is None or type_filter == "all":
        return child_types
    if type_filter == "model":
        return (_MODEL_TYPE,)
    if type_filter in type_alias:
        return (type_alias[type_filter],)
    allowed = ["all", "model", *sorted(type_alias)]
    raise KeboolaApiError(
        message=f"Invalid --type {type_filter!r}. Must be one of: {', '.join(allowed)}.",
        error_code=ErrorCode.VALIDATION_ERROR,
    )


def run_search_context(
    *,
    open_client: Callable[[], MetastoreClient],
    alias: str,
    child_types: tuple[SemanticType, ...],
    type_alias: dict[str, SemanticType],
    patterns: list[str] | None,
    type_filter: str | None,
    limit: int | None,
) -> dict[str, Any]:
    """Project-wide glob search across semantic-layer entity names.

    Mirrors the upstream ``keboola-mcp-server`` ``search_semantic_context``
    MCP tool. ``patterns`` default to ``["*"]`` and are matched case-
    sensitively against ``attributes.name``; multiple patterns take the
    union. ``type_filter`` ``None`` / ``"all"`` -> every entry in
    ``child_types``; ``"model"`` -> semantic models; any other CLI
    singular narrows to that single wire type via ``type_alias``.

    Returns ``{"project", "contexts", "total_count"}``; each context is
    ``{"id", "type", "name", "description", "attributes"}`` with the
    wire ``"semantic-"`` prefix stripped from ``type`` for CLI ergonomics.
    Raises :data:`ErrorCode.VALIDATION_ERROR` for empty patterns, non-
    positive ``limit``, or an unknown ``type_filter``.

    Opens + closes its own metastore client via ``open_client()`` so the
    service method is a one-line delegator.
    """
    eff_patterns: list[str] = patterns or ["*"]
    if any(not p for p in eff_patterns):
        raise KeboolaApiError(
            message="--pattern values must be non-empty strings",
            error_code=ErrorCode.VALIDATION_ERROR,
        )
    if limit is not None and limit <= 0:
        raise KeboolaApiError(
            message="--limit must be a positive integer",
            error_code=ErrorCode.VALIDATION_ERROR,
        )
    types_to_search = _resolve_search_types(type_filter, child_types, type_alias)

    client = open_client()
    contexts: list[dict[str, Any]] = []
    try:
        for wire_type in types_to_search:
            for item in client.list_items(wire_type):
                attrs = item.get("attributes") or {}
                name = str(attrs.get("name", ""))
                if not _matches_any_pattern(name, eff_patterns):
                    continue
                contexts.append(
                    {
                        "id": item.get("id", ""),
                        "type": _strip_semantic_prefix(wire_type),
                        "name": name,
                        "description": attrs.get("description", ""),
                        "attributes": attrs,
                    }
                )
                if limit is not None and len(contexts) >= limit:
                    break
            if limit is not None and len(contexts) >= limit:
                break
    finally:
        client.close()

    return {"project": alias, "contexts": contexts, "total_count": len(contexts)}


def run_get_context(
    *,
    open_client: Callable[[], MetastoreClient],
    alias: str,
    child_types: tuple[SemanticType, ...],
    context_id: str,
) -> dict[str, Any]:
    """Single-id fetch across every semantic type.

    Probes ``semantic-model`` first then every entry in ``child_types``,
    stopping on the first 200. A 404 on any one type is non-terminal;
    only a full miss raises ``NOT_FOUND``. Non-404 errors (e.g. 500)
    propagate immediately rather than being swallowed by the next probe.

    Returns ``{"project", "id", "type", "name", "description", "attributes"}``
    on hit (type stripped of the ``"semantic-"`` wire prefix). Raises
    :data:`ErrorCode.VALIDATION_ERROR` on empty id, or
    :data:`ErrorCode.NOT_FOUND` after the full sweep.

    Opens + closes its own metastore client via ``open_client()``.
    """
    if not context_id:
        raise KeboolaApiError(
            message="--context-id is required",
            error_code=ErrorCode.VALIDATION_ERROR,
        )

    lookup_order: tuple[SemanticType, ...] = (_MODEL_TYPE, *child_types)
    client = open_client()
    try:
        for wire_type in lookup_order:
            try:
                item = client.get_item(wire_type, context_id)
            except KeboolaApiError as exc:
                if exc.error_code == ErrorCode.NOT_FOUND:
                    continue
                raise
            attrs = item.get("attributes") or {}
            return {
                "project": alias,
                "id": item.get("id", ""),
                "type": _strip_semantic_prefix(wire_type),
                "name": attrs.get("name", ""),
                "description": attrs.get("description", ""),
                "attributes": attrs,
            }
    finally:
        client.close()

    raise KeboolaApiError(
        message=(
            f"Semantic context with id {context_id!r} not found in project "
            f"{alias!r}. Tried: semantic-model + {', '.join(child_types)}."
        ),
        error_code=ErrorCode.NOT_FOUND,
    )
