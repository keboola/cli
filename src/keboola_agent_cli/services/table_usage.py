"""Resolve which configurations reference a storage table.

Backs ``kbagent storage tables --include-usage``, the native equivalent of
keboola-mcp-server's ``get_tables(include_usage=True)``. That tool is a
config-based search restricted to the storage mapping scopes, so this module
does the same: it scans the component listing (which already carries every
configuration body and row) and reports the input/output mappings that name a
given table id.

Only the mapping scopes count. A table id that merely appears inside a
transformation's SQL is not a mapping reference -- it is text that happens to
match -- and reporting it would make "what breaks if I drop this table?"
answer with false positives.

The scan is a pure function over an already-fetched listing so that the
matching rules stay testable without any HTTP.
"""

from __future__ import annotations

import logging
from typing import Any

from ..errors import KeboolaApiError

logger = logging.getLogger(__name__)

# Configuration-body scopes that constitute a real table reference, mapped to
# the key holding the table id inside each mapping entry.
_MAPPING_SCOPES: tuple[tuple[str, str, str], ...] = (
    ("storage.input", "input", "source"),
    ("storage.output", "output", "destination"),
)


def _mapping_table_ids(configuration: Any) -> list[tuple[str, str]]:
    """Return (scope, table_id) for every storage mapping entry in a body.

    Tolerates malformed bodies: a configuration whose ``storage`` is not a
    dict, or whose ``tables`` is not a list, simply contributes nothing.
    """
    if not isinstance(configuration, dict):
        return []
    storage = configuration.get("storage")
    if not isinstance(storage, dict):
        return []

    found: list[tuple[str, str]] = []
    for scope, mapping_key, id_key in _MAPPING_SCOPES:
        mapping = storage.get(mapping_key)
        if not isinstance(mapping, dict):
            continue
        tables = mapping.get("tables")
        if not isinstance(tables, list):
            continue
        for entry in tables:
            if not isinstance(entry, dict):
                continue
            table_id = entry.get(id_key)
            if isinstance(table_id, str) and table_id:
                found.append((scope, table_id))
    return found


def collect_table_usage(
    components: list[dict[str, Any]],
    table_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Map each requested table id to the configurations that reference it.

    Args:
        components: A component listing including configuration bodies and
            rows (``client.list_components_with_configs``).
        table_ids: Table ids to look for. Matching is case-insensitive and on
            the whole id -- Keboola configs routinely spell one logical table
            several ways (issue #569), while a substring match would report
            ``in.c-main.orders`` as a use of ``in.c-main.order``.

    Returns:
        ``{table_id: [reference, ...]}`` keyed by the id exactly as passed in,
        with an empty list for tables nothing references. Each reference is
        ``{component_id, component_name, config_id, config_name, row_id,
        scope}``; ``row_id`` is None for a root configuration. A configuration
        that names the same table several times within one scope is reported
        once for that scope.
    """
    if not table_ids:
        return {}

    # Normalised id -> the caller's spelling, so the result keys round-trip.
    wanted = {table_id.lower(): table_id for table_id in table_ids}
    usage: dict[str, list[dict[str, Any]]] = {table_id: [] for table_id in table_ids}
    # (requested_id, component_id, config_id, row_id, scope) already recorded.
    seen: set[tuple[str, str, str, str | None, str]] = set()

    for component in components:
        component_id = component.get("id", "")
        component_name = component.get("name", "")
        for config in component.get("configurations", []) or []:
            config_id = str(config.get("id", ""))
            config_name = config.get("name", "")
            bodies: list[tuple[str | None, Any]] = [(None, config.get("configuration"))]
            for row in config.get("rows", []) or []:
                if isinstance(row, dict):
                    bodies.append((str(row.get("id", "")) or None, row.get("configuration")))

            for row_id, body in bodies:
                for scope, table_id in _mapping_table_ids(body):
                    requested = wanted.get(table_id.lower())
                    if requested is None:
                        continue
                    key = (requested, component_id, config_id, row_id, scope)
                    if key in seen:
                        continue
                    seen.add(key)
                    usage[requested].append(
                        {
                            "component_id": component_id,
                            "component_name": component_name,
                            "config_id": config_id,
                            "config_name": config_name,
                            "row_id": row_id,
                            "scope": scope,
                        }
                    )

    return usage


def fetch_usage_components(
    client: Any,
    alias: str,
    branch_id: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch the component listing the usage scan needs; never raises.

    The scan is an add-on to a table listing. A project whose components are
    unreadable (a bucket-scoped token, a transient 5xx) must still return its
    tables -- with an empty ``used_by`` rather than a failed command -- so a
    failure here degrades to "no usage known", not "no tables".
    """
    try:
        return client.list_components_with_configs(branch_id=branch_id)
    except KeboolaApiError as exc:
        logger.debug(
            "usage scan for '%s' failed (%s): %s; surfacing empty used_by",
            alias,
            exc.error_code,
            exc.message,
        )
        return []
