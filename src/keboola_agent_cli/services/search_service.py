"""Search service — cross-project item search using the Storage API global-search endpoint.

Provides textual (name-based) search across buckets, tables, configurations,
flows, data apps, and other Keboola item types. Supports multi-project fan-out
via BaseService._run_parallel() with per-project error accumulation.

The ``config-based`` search type is delegated to ``ConfigService.search_configs()``
(which scans full configuration JSON bodies for the query string) and is included
here for a unified ``search`` command surface.
"""

from __future__ import annotations

import logging
from typing import Any

from ..errors import KeboolaApiError
from ..models import ProjectConfig
from .base import BaseService, sanitize_unexpected_error
from .config_service import ConfigService

logger = logging.getLogger(__name__)

# Mapping from user-facing --type values to Storage API ``types[]`` values.
# The API accepts a strict set; we expose a friendlier subset as CLI options.
USER_TYPE_TO_API_TYPES: dict[str, list[str]] = {
    "bucket": ["bucket"],
    "table": ["table"],
    "config": ["configuration"],
    "flow": ["flow"],
    "data-app": ["configuration"],  # data-apps are configurations of keboola.data-apps component
    "transformation": ["transformation"],
}

# All API types for unfiltered search.
ALL_API_TYPES: list[str] = [
    "bucket",
    "table",
    "flow",
    "transformation",
    "configuration",
    "configuration-row",
]

# Feature flag required for global-search endpoint.
GLOBAL_SEARCH_FEATURE = "global-search"


class SearchService(BaseService):
    """Service for cross-project textual search using the Storage API global-search endpoint.

    Supports:
    - Textual search: uses ``GET /v2/storage/global-search`` (name-based, fast).
    - Config-based search: delegates to ConfigService for body scanning (slower).

    Multi-project fan-out runs in parallel via ``_run_parallel()``. A failing
    project is recorded in ``errors`` without stopping others.
    """

    def search(
        self,
        query: str,
        aliases: list[str] | None = None,
        item_types: list[str] | None = None,
        search_type: str = "textual",
        limit: int = 50,
    ) -> dict[str, Any]:
        """Search for items across one or more projects.

        Args:
            query: Search string to match against item names (textual) or
                   config bodies (config-based).
            aliases: Project aliases to search. ``None`` means all projects.
            item_types: Optional list of user-facing type names. Supported:
                        ``bucket``, ``table``, ``config``, ``flow``,
                        ``data-app``, ``transformation``.
                        ``None`` or empty means search all types.
            search_type: ``"textual"`` (default) uses the fast global-search
                         endpoint (name-based). ``"config-based"`` scans full
                         config JSON bodies via ConfigService.
            limit: Maximum number of results per project (textual only).

        Returns:
            Dict with keys:
            - ``"results"``: list of result dicts
            - ``"errors"``: list of per-project error dicts
            - ``"stats"``: dict with ``projects_searched`` and ``results_found``
        """
        projects = self.resolve_projects(aliases)

        if search_type == "config-based":
            return self._search_config_based(query, projects, item_types)

        return self._search_textual(query, projects, item_types, limit)

    # ── Textual search (global-search endpoint) ────────────────────────────

    def _search_textual(
        self,
        query: str,
        projects: dict[str, ProjectConfig],
        item_types: list[str] | None,
        limit: int,
    ) -> dict[str, Any]:
        """Fan out textual search across projects in parallel."""
        api_types = _resolve_api_types(item_types)

        def worker(
            alias: str, project: ProjectConfig
        ) -> tuple[str, list[dict[str, Any]], bool] | tuple[str, dict[str, str]]:
            return self._search_project_textual(alias, project, query, api_types, limit)

        successes, errors = self._run_parallel(projects, worker)

        all_results: list[dict[str, Any]] = []
        for _alias, results, _ok in successes:
            all_results.extend(results)

        all_results.sort(key=lambda r: (r["project_alias"], r["type"], r["id"]))
        errors.sort(key=lambda e: e.get("project_alias", ""))

        return {
            "results": all_results,
            "errors": errors,
            "stats": {
                "projects_searched": len(successes),
                "results_found": len(all_results),
            },
        }

    def _search_project_textual(
        self,
        alias: str,
        project: ProjectConfig,
        query: str,
        api_types: list[str],
        limit: int,
    ) -> tuple[str, list[dict[str, Any]], bool] | tuple[str, dict[str, str]]:
        """Worker: run textual search against a single project (thread-safe)."""
        client = self._client_factory(project.stack_url, project.token)
        try:
            # Resolve project_id via token verify; cached within client lifetime.
            token_info = client.verify_token()
            project_id = token_info.project_id
            if project_id is None:
                return (
                    alias,
                    {
                        "project_alias": alias,
                        "error_code": "CONFIG_ERROR",
                        "message": "Could not determine project ID from token info.",
                    },
                )

            raw = client.global_search(
                query=query,
                project_id=project_id,
                types=api_types if api_types else None,
                limit=limit,
            )
            items = raw.get("items", [])
            results = [_normalise_item(alias, item) for item in items]
            return alias, results, True

        except KeboolaApiError as exc:
            return (
                alias,
                {
                    "project_alias": alias,
                    "error_code": exc.error_code or "API_ERROR",
                    "message": exc.message,
                },
            )
        except Exception as exc:
            logger.debug("Unexpected error searching project '%s': %s", alias, exc)
            return (
                alias,
                {
                    "project_alias": alias,
                    "error_code": "UNEXPECTED_ERROR",
                    "message": sanitize_unexpected_error(exc),
                },
            )

    # ── Config-based search (full body scan) ───────────────────────────────

    def _search_config_based(
        self,
        query: str,
        projects: dict[str, ProjectConfig],
        item_types: list[str] | None,
    ) -> dict[str, Any]:
        """Delegate to ConfigService for JSON-body search across projects.

        Translates user-facing ``item_types`` to ConfigService's
        ``component_type`` filter where possible, then reformats the result
        to the unified search output shape.
        """
        # Map item_types to a component_type filter for ConfigService.
        component_type = _item_types_to_component_type(item_types)

        config_service = ConfigService(
            config_store=self._config_store,
            client_factory=self._client_factory,
        )
        aliases = list(projects.keys()) if projects else None
        raw = config_service.search_configs(
            query=query,
            aliases=aliases,
            component_type=component_type,
        )

        # Re-shape matches into the unified results format.
        results = [
            {
                "project_alias": m["project_alias"],
                "type": "configuration",
                "id": m["config_id"],
                "name": m["config_name"],
                "description": m.get("description", ""),
                "component_id": m.get("component_id"),
                "match_count": m.get("match_count", 0),
                "match_locations": m.get("match_locations", []),
            }
            for m in raw.get("matches", [])
        ]

        return {
            "results": results,
            "errors": raw.get("errors", []),
            "stats": {
                "projects_searched": raw["stats"]["projects_searched"],
                "results_found": len(results),
            },
        }


# ── Helpers ────────────────────────────────────────────────────────────────


def _resolve_api_types(item_types: list[str] | None) -> list[str]:
    """Translate user-facing type names to Storage API type values.

    Args:
        item_types: List of user-facing type names, or None for all.

    Returns:
        Deduplicated list of API type strings. Empty list means "all types".
    """
    if not item_types:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for t in item_types:
        for api_type in USER_TYPE_TO_API_TYPES.get(t, [t]):
            if api_type not in seen:
                seen.add(api_type)
                result.append(api_type)
    return result


def _item_types_to_component_type(item_types: list[str] | None) -> str | None:
    """Map user-facing item types to a ConfigService component_type filter.

    Returns None (no filter) for types that do not map to a specific
    component type (e.g. bucket, table) or when no filter is given.
    """
    if not item_types:
        return None
    types_set = set(item_types)
    if types_set == {"transformation"}:
        return "transformation"
    return None


def _normalise_item(alias: str, item: dict[str, Any]) -> dict[str, Any]:
    """Normalise a raw global-search API item into the unified result shape.

    Args:
        alias: Project alias this result came from.
        item: Raw item dict from the Storage API response.

    Returns:
        Normalised result dict with consistent keys across all item types.
    """
    full_path = item.get("fullPath", {})
    return {
        "project_alias": alias,
        "type": item.get("type", ""),
        "id": item.get("id", ""),
        "name": item.get("name", ""),
        "description": full_path.get("description", ""),
        "component_id": item.get("componentId"),
        "project_id": item.get("projectId"),
        "project_name": item.get("projectName", ""),
    }
