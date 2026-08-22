"""Shared shape validation for flat ``{id: scalar}`` YAML/JSON input files.

Several commands accept a small user-authored mapping file -- ``sync clone``'s
``--bucket-map`` / ``--variable-values`` / ``--instance-rename`` are the
canonical case. Coercing every value with bare ``str(value)`` silently turns a
nested mapping (one fat-fingered colon away from valid input) into the literal
string ``"{'new': 'in.c-new'}"``, which then lands in the target project as a
"bucket ID". This module rejects non-scalar values (and ``None``) up front,
naming the offending key and its actual type -- same approach as the
``storage describe-batch --from-file`` validation in
``services/_describe_batch_input.py``.

Type names come out in YAML vocabulary ("mapping", "list", "null"), not
Python's -- the author is reading their own YAML file, not a traceback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import ConfigError

_TYPE_NAMES: dict[type, str] = {
    bool: "boolean",
    dict: "mapping",
    float: "number",
    int: "number",
    list: "list",
    str: "string",
    type(None): "null",
}


def yaml_type_name(value: Any) -> str:
    """Return ``value``'s type named in YAML vocabulary."""
    return _TYPE_NAMES.get(type(value), type(value).__name__)


def load_flat_scalar_mapping(path: Path, *, label: str = "input file") -> dict[str, str]:
    """Load a JSON/YAML file as a flat ``{str: str}`` mapping, or raise.

    YAML's loader also parses JSON, so a single path handles both. Scalar
    values (string, number, boolean) are coerced to ``str``; a container or
    ``None`` value raises instead of being stringified.

    Args:
        path: The file to load.
        label: What the file is, for error messages (e.g. "override file").

    Raises:
        ConfigError: The file is missing, is not valid YAML/JSON, is not a
            mapping at the top level, or any value is not a scalar.
    """
    import yaml

    if not path.exists():
        raise ConfigError(f"{label.capitalize()} not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Cannot parse {label} {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(
            f"{label.capitalize()} {path} must contain a JSON/YAML object (mapping), "
            f"got a {yaml_type_name(data)}."
        )
    result: dict[str, str] = {}
    for key, value in data.items():
        if value is None or isinstance(value, dict | list):
            raise ConfigError(
                f"'{key}' in {label} {path} must be a single scalar value "
                f"(string, number or boolean), got a {yaml_type_name(value)}. "
                f"Check the file for a stray colon or missing value."
            )
        result[str(key)] = str(value)
    return result
