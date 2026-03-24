"""Conversion between Keboola API JSON and local _config.yml format.

The local format is a human-friendly YAML structure that "promotes" deeply
nested configuration keys (parameters, storage.input, storage.output,
processors) to the top level and adds a ``_keboola`` metadata block.
"""

from __future__ import annotations

from typing import Any

from ..constants import CONFIG_YML_VERSION

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
    configuration: dict[str, Any] = config_data.get("configuration", {})

    local: dict[str, Any] = {
        "version": CONFIG_YML_VERSION,
        "name": config_data.get("name", ""),
        "description": config_data.get("description", ""),
    }

    # Promote well-known nested keys
    if "parameters" in configuration:
        local["parameters"] = configuration["parameters"]

    storage: dict[str, Any] = configuration.get("storage", {})
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
    configuration: dict[str, Any] = row_data.get("configuration", {})

    local: dict[str, Any] = {
        "version": CONFIG_YML_VERSION,
        "name": row_data.get("name", ""),
        "description": row_data.get("description", ""),
    }

    if "parameters" in configuration:
        local["parameters"] = configuration["parameters"]

    storage: dict[str, Any] = configuration.get("storage", {})
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
