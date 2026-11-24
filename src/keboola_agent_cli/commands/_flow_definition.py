"""Loading the ``--definition`` / ``--definition-file`` of ``flow new/update/validate``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
import yaml

from ..errors import ErrorCode
from ..output import OutputFormatter
from ._helpers import structured_input

# The top-level keys of a flow definition document (`flow schema`).
_DEFINITION_KEYS = ("phases", "tasks")


def load_flow_definition(inline: str | None, file: Path | None) -> dict[str, Any] | None:
    """The flow definition of ``--definition`` (inline YAML/JSON) or ``--definition-file``.

    ``None`` when neither is given; ``-`` as the file is stdin (spec rule I1).

    A definition that is given must carry one: an empty value, an empty or
    comments-only file, a YAML ``null`` and a mapping with neither ``phases``
    nor ``tasks`` are refused. Before, they created a flow with no phases, and
    ``flow update --name X`` with such a file only renamed the flow.
    """
    if inline is None and file is None:
        return None
    parsed = structured_input(inline, file, label="--definition", allow_yaml=True)
    if parsed is None:
        raise ValueError(
            "the definition is empty. Pass a YAML/JSON object with 'phases' and 'tasks' "
            "('kbagent flow schema' shows the format); omit --definition and "
            "--definition-file to create an empty flow."
        )
    if not isinstance(parsed, dict):
        raise ValueError(
            f"Flow definition must be a YAML/JSON object (mapping), got {type(parsed).__name__}"
        )
    unknown = [str(key) for key in parsed if key not in _DEFINITION_KEYS]
    if unknown or not parsed:
        found = ", ".join(repr(str(key)) for key in parsed) or "none"
        raise ValueError(
            "a flow definition has only the keys 'phases' and 'tasks' (keys found: "
            f"{found}). 'kbagent flow schema' shows the format."
        )
    nulls = [key for key in _DEFINITION_KEYS if key in parsed and parsed[key] is None]
    if nulls:
        raise ValueError(
            f"'{nulls[0]}' is null. Give a list, or leave the key out to keep the current value."
        )
    return parsed


def load_definition_or_exit(
    formatter: OutputFormatter, inline: str | None, file: Path | None
) -> dict[str, Any] | None:
    """``load_flow_definition``; a definition that cannot be used is VALIDATION_ERROR, exit 2."""
    try:
        return load_flow_definition(inline, file)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        formatter.error(
            message=f"Cannot load flow definition: {exc}", error_code=ErrorCode.VALIDATION_ERROR
        )
        raise typer.Exit(code=2) from None
