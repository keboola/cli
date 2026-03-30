"""Conversion between Keboola API JSON and local _config.yml format.

The local format is a human-friendly YAML structure that "promotes" deeply
nested configuration keys (parameters, storage.input, storage.output,
processors) to the top level and adds a ``_keboola`` metadata block.
"""

from __future__ import annotations

import copy
from typing import Any

from ..constants import CONFIG_YML_VERSION


def _normalize_scripts(parameters: Any) -> Any:
    """Normalize script arrays in transformation parameters.

    The Keboola API inconsistently returns code scripts as either:
    - ``["line1", "line2", ...]`` (per-line array)
    - ``["full\\ncode\\nwith\\nnewlines"]`` (single multiline string)

    This normalizes to single-string-per-code-block format so that local
    merge (which produces single-string via ``_lines_to_script``) and
    remote data compare identically.  The Keboola transformation runner
    treats each array element as a separate executable statement, so each
    CODE block must be a single joined string.
    """
    if not isinstance(parameters, dict):
        return parameters
    params = copy.deepcopy(parameters)
    for block in params.get("blocks", []):
        if not isinstance(block, dict):
            continue
        for code in block.get("codes", []):
            if not isinstance(code, dict):
                continue
            scripts = code.get("script")
            if isinstance(scripts, list) and scripts:
                # Flatten everything into individual lines first.
                all_lines: list[str] = []
                for s in scripts:
                    if isinstance(s, str) and "\n" in s:
                        all_lines.extend(s.split("\n"))
                    else:
                        all_lines.append(s)
                # Strip trailing whitespace per line (YAML roundtrip
                # strips it) and remove trailing empty lines.
                all_lines = [line.rstrip() for line in all_lines]
                while all_lines and all_lines[-1] == "":
                    all_lines.pop()
                # Join into a single string per code block.
                code["script"] = ["\n".join(all_lines)] if all_lines else []
    return params


# ---------------------------------------------------------------------------
# Component type mapping
# ---------------------------------------------------------------------------

COMPONENT_TYPE_MAP: dict[str, str] = {
    "extractor": "extractor",
    "writer": "writer",
    "transformation": "transformation",
    "application": "application",
    "other": "other",
}

# Orchestrator-like components that have special handling
ORCHESTRATOR_COMPONENTS: set[str] = {"keboola.orchestrator", "keboola.flow"}


def classify_component_type(api_type: str) -> str:
    """Map an API component type string to its filesystem directory name.

    Falls back to ``"other"`` for unknown types.
    """
    return COMPONENT_TYPE_MAP.get(api_type, "other")


# ---------------------------------------------------------------------------
# API -> local _config.yml
# ---------------------------------------------------------------------------


def api_config_to_local(
    component_id: str, config_data: dict[str, Any], config_id: str
) -> dict[str, Any]:
    """Convert an API configuration response to the local ``_config.yml`` structure.

    Transformation rules:
    - ``version``: always ``CONFIG_YML_VERSION``
    - ``name``, ``description``: taken from the top-level API response
    - ``configuration.parameters`` -> ``parameters``
    - ``configuration.storage.input`` -> ``input``
    - ``configuration.storage.output`` -> ``output``
    - ``configuration.processors`` -> ``processors``
    - ``_keboola``: ``{component_id, config_id}``

    Any remaining keys inside ``configuration`` that are not explicitly
    promoted are preserved under a ``_configuration_extra`` key so that
    round-tripping does not lose data.
    """
    configuration: dict[str, Any] = config_data.get("configuration") or {}

    local: dict[str, Any] = {
        "version": CONFIG_YML_VERSION,
        "name": config_data.get("name", ""),
        "description": config_data.get("description", ""),
    }

    # Promote well-known nested keys
    if "parameters" in configuration:
        local["parameters"] = _normalize_scripts(configuration["parameters"])

    storage: dict[str, Any] = configuration.get("storage") or {}
    if "input" in storage:
        local["input"] = storage["input"]
    if "output" in storage:
        local["output"] = storage["output"]

    if "processors" in configuration:
        local["processors"] = configuration["processors"]

    # Preserve any extra keys that we do not explicitly promote
    promoted_keys = {"parameters", "storage", "processors"}
    extras = {k: v for k, v in configuration.items() if k not in promoted_keys}
    if extras:
        local["_configuration_extra"] = extras

    # Keboola metadata footer
    local["_keboola"] = {
        "component_id": component_id,
        "config_id": config_id,
    }

    return local


# ---------------------------------------------------------------------------
# local _config.yml -> API
# ---------------------------------------------------------------------------


def local_config_to_api(
    config_yml: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """Convert a local ``_config.yml`` dict back to API format.

    Returns:
        A tuple of ``(name, description, configuration_dict)`` suitable for
        an API create/update call.
    """
    name: str = config_yml.get("name", "")
    description: str = config_yml.get("description", "")

    configuration: dict[str, Any] = {}

    if "parameters" in config_yml:
        configuration["parameters"] = config_yml["parameters"]

    # Re-nest input/output under storage
    storage: dict[str, Any] = {}
    if "input" in config_yml:
        storage["input"] = config_yml["input"]
    if "output" in config_yml:
        storage["output"] = config_yml["output"]
    if storage:
        configuration["storage"] = storage

    if "processors" in config_yml:
        configuration["processors"] = config_yml["processors"]

    # Merge back any extras that were preserved during api->local conversion
    extras: dict[str, Any] = config_yml.get("_configuration_extra", {})
    for key, value in extras.items():
        configuration.setdefault(key, value)

    return name, description, configuration


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def api_row_to_local(row_data: dict[str, Any], component_id: str) -> dict[str, Any]:
    """Convert an API configuration row to a local row ``_config.yml``.

    Follows the same promotion rules as :func:`api_config_to_local`.
    """
    configuration: dict[str, Any] = row_data.get("configuration") or {}

    local: dict[str, Any] = {
        "version": CONFIG_YML_VERSION,
        "name": row_data.get("name", ""),
        "description": row_data.get("description", ""),
    }

    if "parameters" in configuration:
        local["parameters"] = configuration["parameters"]

    storage: dict[str, Any] = configuration.get("storage") or {}
    if "input" in storage:
        local["input"] = storage["input"]
    if "output" in storage:
        local["output"] = storage["output"]

    if "processors" in configuration:
        local["processors"] = configuration["processors"]

    promoted_keys = {"parameters", "storage", "processors"}
    extras = {k: v for k, v in configuration.items() if k not in promoted_keys}
    if extras:
        local["_configuration_extra"] = extras

    local["_keboola"] = {
        "component_id": component_id,
        "row_id": row_data.get("id", ""),
    }

    return local


def local_row_to_api(
    row_yml: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """Convert a local row ``_config.yml`` back to API format.

    Returns:
        A tuple of ``(name, description, configuration_dict)``.
    """
    # Reuse the same logic -- the structure is identical
    return local_config_to_api(row_yml)
