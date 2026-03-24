"""Path generation from naming templates.

The naming templates (stored in ``manifest.json``) use placeholders like
``{component_type}`` and ``{config_name}`` to produce deterministic
filesystem paths for each configuration.
"""

from __future__ import annotations

import re

from ..constants import SANITIZE_NAME_MAX_LENGTH


def config_path(
    naming_template: str,
    component_type: str,
    component_id: str,
    config_name: str,
) -> str:
    """Apply *naming_template* to generate a filesystem path for a configuration.

    Example template: ``"{component_type}/{component_id}/{config_name}"``
    """
    return naming_template.format(
        component_type=component_type,
        component_id=component_id,
        config_name=sanitize_name(config_name),
    )


def config_row_path(naming_template: str, row_name: str) -> str:
    """Apply *naming_template* to generate a path segment for a config row.

    Example template: ``"rows/{config_row_name}"``
    """
    return naming_template.format(
        config_row_name=sanitize_name(row_name),
    )


def sanitize_name(name: str) -> str:
    """Sanitize *name* for use in filesystem paths.

    Rules:
    - Lowercase the string
    - Replace spaces and non-alphanumeric characters (except hyphens)
      with hyphens
    - Collapse consecutive hyphens
    - Strip leading and trailing hyphens
    - Truncate to ``SANITIZE_NAME_MAX_LENGTH`` characters
    """
    result = name.lower()
    # Replace anything that is not alphanumeric or a hyphen
    result = re.sub(r"[^a-z0-9-]", "-", result)
    # Collapse multiple hyphens
    result = re.sub(r"-{2,}", "-", result)
    # Strip leading/trailing hyphens
    result = result.strip("-")
    # Enforce max length
    return result[:SANITIZE_NAME_MAX_LENGTH]
