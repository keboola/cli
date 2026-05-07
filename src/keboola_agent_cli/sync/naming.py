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

    All template inputs are passed through sanitizers so that an API-controlled
    ``component_id`` like ``"../../etc"`` cannot escape the sync workspace
    (issue #269 sec-01/sec-07). Legitimate component IDs (e.g.
    ``keboola.python-transformation-v2``) preserve their dots and hyphens.
    """
    return naming_template.format(
        component_type=sanitize_path_segment(component_type),
        component_id=sanitize_path_segment(component_id),
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


def sanitize_path_segment(token: str) -> str:
    """Sanitize an API-supplied token for use as a single path segment.

    Stricter-than-``sanitize_name`` defense against path traversal: rejects
    ``/``, ``\\``, parent-directory references (``..``), and other directory
    separators while preserving the dots, hyphens, and underscores commonly
    found in legitimate component IDs (e.g. ``keboola.ex-db-mysql``,
    ``kds-team.app-custom-python``). Returns ``"_"`` if the input would
    sanitize to empty so the resulting path always has a non-empty segment
    (issue #269 sec-01).
    """
    # Replace path separators and whitespace with a single hyphen
    result = re.sub(r"[/\\\s]", "-", token)
    # Replace any run of 2+ dots (which would form parent refs) with
    # a single underscore. Single dots are preserved for legitimate IDs.
    result = re.sub(r"\.{2,}", "_", result)
    # Strip leading dots that could be reintroduced as ``./...`` traversal
    # if the template happens to put us at a directory boundary.
    result = result.lstrip(".")
    # Collapse repeated hyphens introduced by the substitutions
    result = re.sub(r"-{2,}", "-", result).strip("-")
    return result or "_"
