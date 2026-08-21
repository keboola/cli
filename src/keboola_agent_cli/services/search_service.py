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
import re
from typing import Any

from ..constants import GLOBAL_SEARCH_FEATURE
from ..errors import ConfigError, KeboolaApiError
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
    # data-app reuses the "configuration" API type; results are post-filtered
    # to component_id == "keboola.data-apps" in `_normalise_item` so users
    # asking for --type data-app do not get every other configuration too.
    "data-app": ["configuration"],
    "transformation": ["transformation"],
}

# Component ID used to identify data-app configurations after post-filtering.
DATA_APP_COMPONENT_ID = "keboola.data-apps"

# All API types for unfiltered search.
ALL_API_TYPES: list[str] = [
    "bucket",
    "table",
    "flow",
    "transformation",
    "configuration",
    "configuration-row",
]


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
        regex: bool = False,
        scopes: list[str] | None = None,
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
                         config JSON bodies via ConfigService. Both modes match
                         case-insensitively.
            limit: Maximum number of results per project (textual only).
            regex: When True (textual only), the query is run as a
                   case-insensitive whole-term regular expression over entity
                   names (Storage API ``mode=regex``).
            scopes: Dot-notation prefixes (config-based only) that narrow a hit
                    to parts of the configuration body, e.g. ``parameters`` or
                    ``storage.input``. Written relative to the configuration
                    itself; the ``configuration.`` / ``rows[N].configuration.``
                    wrapper is normalised away. Multiple scopes are OR-ed. A
                    configuration whose every match falls outside all scopes
                    drops out of the results entirely.

        Returns:
            Dict with keys:
            - ``"results"``: list of result dicts
            - ``"errors"``: list of per-project error dicts
            - ``"stats"``: dict with ``projects_searched`` and ``results_found``

        Raises:
            ConfigError: When ``regex=True`` is combined with
                         ``search_type="config-based"`` (regex exists only on
                         the global-search endpoint), or when ``scopes`` is
                         combined with textual search (there is no config body
                         to scope into). Validated here so every caller (CLI,
                         REST ``/search``) inherits the checks.
        """
        if regex and search_type == "config-based":
            raise ConfigError(
                "Regex mode is only supported with textual search "
                "(config-based search does not support regex)."
            )

        if scopes and search_type != "config-based":
            raise ConfigError(
                "Scopes are only supported with config-based search "
                "(textual search matches entity names, not configuration bodies)."
            )

        projects = self.resolve_projects(aliases)

        if search_type == "config-based":
            return self._search_config_based(query, projects, item_types, scopes)

        return self._search_textual(query, projects, item_types, limit, regex=regex)

    # ── Textual search (global-search endpoint) ────────────────────────────

    def _search_textual(
        self,
        query: str,
        projects: dict[str, ProjectConfig],
        item_types: list[str] | None,
        limit: int,
        regex: bool = False,
    ) -> dict[str, Any]:
        """Fan out textual search across projects in parallel."""
        api_types = _resolve_api_types(item_types)

        def worker(
            alias: str, project: ProjectConfig
        ) -> tuple[str, list[dict[str, Any]], bool] | tuple[str, dict[str, str]]:
            return self._search_project_textual(
                alias, project, query, api_types, limit, item_types=item_types, regex=regex
            )

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
        item_types: list[str] | None = None,
        regex: bool = False,
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

            # Pre-flight: refuse fast on stacks/projects without the global-search
            # feature instead of letting a raw 404/403 confuse the caller.
            if not client.has_feature(GLOBAL_SEARCH_FEATURE):
                return (
                    alias,
                    {
                        "project_alias": alias,
                        "error_code": "FEATURE_NOT_ENABLED",
                        "message": (
                            f"Project does not have the '{GLOBAL_SEARCH_FEATURE}' feature "
                            "enabled. Use --search-type config-based for body scanning, or "
                            "ask a Keboola admin to enable the feature."
                        ),
                    },
                )

            raw = client.global_search(
                query=query,
                project_id=project_id,
                types=api_types if api_types else None,
                limit=limit,
                regex=regex,
            )
            items = raw.get("items", [])
            results = [_normalise_item(alias, item) for item in items]

            # Post-filter when --type data-app was requested without --type config:
            # data-app maps to the same API type as config, so without filtering
            # users would receive every configuration in the project.
            if item_types and "data-app" in item_types and "config" not in item_types:
                results = [
                    r
                    for r in results
                    if r.get("type") != "configuration"
                    or r.get("component_id") == DATA_APP_COMPONENT_ID
                ]

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
        scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Delegate to ConfigService for JSON-body search across projects.

        Translates user-facing ``item_types`` to ConfigService's
        ``component_type`` filter where possible, then reformats the result
        to the unified search output shape.

        Matching is case-insensitive, mirroring the textual mode of the same
        command (issue #569). Keboola configs routinely spell one logical table
        several ways -- a mixed-case row name against an upper-case physical
        table id in ``storage.input.tables[].source`` -- so a case-sensitive
        body scan answers "is this referenced anywhere?" with a false no.
        ``kbagent config search`` remains the case-sensitive-by-default surface
        for callers that need exact matching (it has its own ``--ignore-case``).
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
            ignore_case=True,
        )

        # Re-shape matches into the unified results format, narrowing each
        # hit to the requested scopes. Filtering here (rather than inside the
        # JSON walk) keeps ConfigService's own `config search` surface -- which
        # has no scope concept -- untouched.
        results = []
        for m in raw.get("matches", []):
            locations = _filter_locations_by_scopes(m.get("match_locations", []), scopes)
            if not locations:
                continue
            results.append(
                {
                    "project_alias": m["project_alias"],
                    "type": "configuration",
                    "id": m["config_id"],
                    "name": m["config_name"],
                    "description": m.get("description", ""),
                    "component_id": m.get("component_id"),
                    "match_count": len(locations),
                    "match_locations": locations,
                    "matched_columns": [],
                }
            )

        return {
            "results": results,
            "errors": raw.get("errors", []),
            "stats": {
                "projects_searched": raw["stats"]["projects_searched"],
                "results_found": len(results),
            },
        }


# ── Helpers ────────────────────────────────────────────────────────────────

_CONFIG_BODY_PREFIX = re.compile(r"^(?:rows\[\d+\]\.)?configuration\.")


def _strip_config_body_prefix(location: str) -> str | None:
    """Return a match location relative to the configuration body, or None.

    ConfigService reports absolute paths into the whole config record
    (``configuration.parameters.host``, ``rows[0].configuration.storage...``),
    while a scope is written the way it appears in the configuration itself
    (``parameters``, ``storage.input``). Locations outside any configuration
    body -- ``name``, ``description``, ``id`` -- have no relative form and
    return None, so they never satisfy a scope.
    """
    stripped, replaced = _CONFIG_BODY_PREFIX.subn("", location, count=1)
    return stripped if replaced else None


def _filter_locations_by_scopes(
    locations: list[str],
    scopes: list[str] | None,
) -> list[str]:
    """Keep the locations that fall under at least one scope.

    An empty/absent ``scopes`` keeps everything. Matching is on whole path
    segments, so ``storage.in`` does not match ``storage.input``.
    """
    if not scopes:
        return list(locations)

    kept: list[str] = []
    for location in locations:
        relative = _strip_config_body_prefix(location)
        if relative is None:
            continue
        if any(
            relative == scope or relative.startswith((f"{scope}.", f"{scope}[")) for scope in scopes
        ):
            kept.append(location)
    return kept


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
        # Only on `table` items matched via a column name; [] otherwise (DMD-1717).
        "matched_columns": item.get("matchedColumns") or [],
    }
