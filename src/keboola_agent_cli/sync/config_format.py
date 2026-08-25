"""Conversion between Keboola API JSON and local _config.yml format.

The local format is a human-friendly YAML structure that "promotes" deeply
nested configuration keys (parameters, storage.input, storage.output,
processors) to the top level and adds a ``_keboola`` metadata block.
"""

from __future__ import annotations

import copy
from typing import Any

import yaml

from ..constants import CONFIG_YML_VERSION
from .code_extraction import canonical_sql_script, is_sql_transformation_component


def _iter_codes(parameters: dict[str, Any]) -> Any:
    """Yield every ``blocks[].codes[]`` dict in a transformation's parameters."""
    for block in parameters.get("blocks", []):
        if not isinstance(block, dict):
            continue
        for code in block.get("codes", []):
            if isinstance(code, dict):
                yield code


def _join_script(scripts: list[Any]) -> list[str]:
    """Collapse a code's ``script[]`` into a single joined string.

    The non-SQL shape: Python / R / custom-app components carry ONE code
    body per code block, and the push side (``_lines_to_script`` without
    ``is_sql``) produces exactly one element, so both sides agree.
    """
    all_lines: list[str] = []
    for s in scripts:
        if isinstance(s, str) and "\n" in s:
            all_lines.extend(s.split("\n"))
        else:
            all_lines.append(s)
    # Strip trailing whitespace per line (YAML roundtrip strips it) and
    # remove trailing empty lines.
    all_lines = [line.rstrip() for line in all_lines]
    while all_lines and all_lines[-1] == "":
        all_lines.pop()
    return ["\n".join(all_lines)] if all_lines else []


def _normalize_scripts(parameters: Any, component_id: str) -> Any:
    """Normalize script arrays in transformation parameters.

    The Keboola API inconsistently returns code scripts as either:
    - ``["line1", "line2", ...]`` (per-line array)
    - ``["full\\ncode\\nwith\\nnewlines"]`` (single multiline string)

    The normalization is COMPONENT-AWARE (issue #686). Both sides of every
    hash comparison must agree on what ``script[]`` looks like, and the push
    side (``code_extraction._lines_to_script``) has split SQL on statement
    boundaries since PR #120:

    - **SQL transformations** (:func:`is_sql_transformation_component`):
      one element per executable statement, via
      :func:`canonical_sql_script` -- each existing element split
      independently and flattened, never joined first.
    - **Everything else**: joined into a single string per code block,
      matching ``_lines_to_script``'s non-SQL branch.

    Until #686 this function always collapsed to one element, which no SQL
    transformation with two or more statements could ever match -- the
    permanent ``~ REMOTE MODIFIED`` phantom drift after every ``sync push``.
    """
    if not isinstance(parameters, dict):
        return parameters
    params = copy.deepcopy(parameters)
    is_sql = is_sql_transformation_component(component_id)
    for code in _iter_codes(params):
        scripts = code.get("script")
        if isinstance(scripts, list) and scripts:
            code["script"] = canonical_sql_script(scripts) if is_sql else _join_script(scripts)
    return params


def _normalize_scripts_legacy(parameters: Any) -> Any:
    """Pre-#686 normalization: collapse every code's ``script[]`` into one.

    Kept for ONE purpose: recomputing the hash a pre-#686 kbagent would have
    stored, so a manifest entry without ``config_hash_version`` can be
    recognised as "in sync apart from the script shape" instead of being
    re-classified as drift on the first run after the upgrade. It is never
    used to produce data that is written anywhere -- only to compare against
    an already-stored hash, and always from the RAW API config.
    """
    if not isinstance(parameters, dict):
        return parameters
    params = copy.deepcopy(parameters)
    for code in _iter_codes(params):
        scripts = code.get("script")
        if isinstance(scripts, list) and scripts:
            code["script"] = _join_script(scripts)
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

# Row-bearing components whose `configuration` top-level keys do NOT fit the
# standard `parameters` / `storage` / `processors` shape. For these, the
# non-standard keys (e.g. `values` for variables, `code_content` for shared-code)
# are hoisted to the top level of the local YAML instead of being hidden inside
# `_configuration_extra`, so that humans and agents can edit them directly
# (matching `kbc push` convention — FIIA scaffold kit relies on this).
ROW_HOIST_COMPONENTS: set[str] = {"keboola.variables", "keboola.shared-code"}

# Top-level keys in the local row YAML that are never part of the API
# `configuration` body. Used by `local_row_to_api` to separate editable payload
# keys from local metadata when the component is in ROW_HOIST_COMPONENTS.
_ROW_LOCAL_RESERVED_KEYS: frozenset[str] = frozenset(
    {
        "version",
        "name",
        "description",
        "is_disabled",
        "parameters",
        "input",
        "output",
        "processors",
        "_configuration_extra",
        "_keboola",
    }
)


def classify_component_type(api_type: str) -> str:
    """Map an API component type string to its filesystem directory name.

    Falls back to ``"other"`` for unknown types.
    """
    return COMPONENT_TYPE_MAP.get(api_type, "other")


# ---------------------------------------------------------------------------
# API -> local _config.yml
# ---------------------------------------------------------------------------


def api_config_to_local(
    component_id: str,
    config_data: dict[str, Any],
    config_id: str,
    *,
    legacy_scripts: bool = False,
) -> dict[str, Any]:
    """Convert an API configuration response to the local ``_config.yml`` structure.

    Transformation rules:
    - ``version``: always ``CONFIG_YML_VERSION``
    - ``name``, ``description``: taken from the top-level API response
    - ``is_disabled``: from top-level ``isDisabled``, emitted ONLY when true
      (sparse -- absence means enabled, so trees pulled before this field
      existed do not show a spurious diff on every config; issue #467)
    - ``configuration.parameters`` -> ``parameters``
    - ``configuration.storage.input`` -> ``input``
    - ``configuration.storage.output`` -> ``output``
    - ``configuration.processors`` -> ``processors``
    - ``_keboola``: ``{component_id, config_id}``

    Any remaining keys inside ``configuration`` that are not explicitly
    promoted are preserved under a ``_configuration_extra`` key so that
    round-tripping does not lose data.

    Args:
        legacy_scripts: Migration compatibility only (issue #686). When True,
            ``parameters`` is normalized with the pre-#686 collapse
            (:func:`_normalize_scripts_legacy`) so the caller can recompute
            the hash an older kbagent would have stored for this same remote
            config. Never pass it on a path that WRITES the result.
    """
    configuration: dict[str, Any] = config_data.get("configuration") or {}

    local: dict[str, Any] = {
        "version": CONFIG_YML_VERSION,
        "name": config_data.get("name", ""),
        "description": config_data.get("description", ""),
    }
    if config_data.get("isDisabled"):
        local["is_disabled"] = True

    # Promote well-known nested keys
    if "parameters" in configuration:
        local["parameters"] = (
            _normalize_scripts_legacy(configuration["parameters"])
            if legacy_scripts
            else _normalize_scripts(configuration["parameters"], component_id)
        )

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

    Follows the same promotion rules as :func:`api_config_to_local`, with one
    exception: for components in :data:`ROW_HOIST_COMPONENTS`, non-standard
    top-level ``configuration`` keys (e.g. ``values`` for ``keboola.variables``)
    are hoisted directly into the local YAML instead of being wrapped under
    ``_configuration_extra``, so users can edit them naturally.
    """
    configuration: dict[str, Any] = row_data.get("configuration") or {}

    local: dict[str, Any] = {
        "version": CONFIG_YML_VERSION,
        "name": row_data.get("name", ""),
        "description": row_data.get("description", ""),
    }
    if row_data.get("isDisabled"):
        local["is_disabled"] = True

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
        if component_id in ROW_HOIST_COMPONENTS:
            for key, value in extras.items():
                local[key] = value
        else:
            local["_configuration_extra"] = extras

    local["_keboola"] = {
        "component_id": component_id,
        "row_id": row_data.get("id", ""),
    }

    return local


def local_row_to_api(
    row_yml: dict[str, Any],
    component_id: str | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Convert a local row ``_config.yml`` back to API format.

    For components in :data:`ROW_HOIST_COMPONENTS`, hoisted top-level keys
    (outside the reserved set) are pulled back into the API ``configuration``
    body. For all other components this behaves identically to
    :func:`local_config_to_api`.

    Args:
        row_yml: The local row ``_config.yml`` dict.
        component_id: The component id the row belongs to. When provided
            (the caller almost always knows it), it drives the hoist check
            directly. When ``None`` (back-compat), the id is read from the
            file's ``_keboola.component_id``. The explicit form is required
            for fresh-CREATE rows whose scaffold ``_config.yml`` does not yet
            carry a ``_keboola`` block, so that ``keboola.variables`` rows
            still hoist their ``values`` array into the API body (KFR-04).

    Returns:
        A tuple of ``(name, description, configuration_dict)``.
    """
    keboola_meta: dict[str, Any] = row_yml.get("_keboola") or {}
    resolved_component_id: str = component_id or keboola_meta.get("component_id", "")

    name, description, configuration = local_config_to_api(row_yml)

    if resolved_component_id in ROW_HOIST_COMPONENTS:
        for key, value in row_yml.items():
            if key in _ROW_LOCAL_RESERVED_KEYS:
                continue
            configuration.setdefault(key, value)

    return name, description, configuration


# ---------------------------------------------------------------------------
# Canonical _config.yml serialization
# ---------------------------------------------------------------------------


def dump_config_yaml(config_data: dict[str, Any]) -> str:
    """Serialize a local ``_config.yml`` dict with the canonical settings.

    The single source of truth for how sync materializes ``_config.yml``
    content -- ``SyncService._write_config_file`` (pull) and
    ``config new --push --output-dir`` (mirrored-body scaffold, issue #644)
    both call this, so the two paths can never drift in formatting.
    """
    return yaml.dump(
        config_data,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        width=120,
    )
