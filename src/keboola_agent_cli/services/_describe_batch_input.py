"""Shape validation for the `storage describe-batch --from-file` document.

Lives outside ``storage_service.py`` because that module is over its
file-size budget (CONTRIBUTING.md > "File-size budgets"), and because parsing
plus validating user input carries no API orchestration -- it is exactly what
moves out.

The documented contract is three optional top-level mappings (``buckets``,
``tables``, ``columns``), each keyed by ID. A document that got any of those
shapes wrong used to reach the write loop and die on ``.items()`` with an
``AttributeError`` -- a Rich traceback on stdout even under ``--json``
(issue #640). Every check here runs BEFORE the first write, so a malformed
file is rejected whole rather than half-applied, and the message names the
offending key, its actual type, and a copy-pasteable example.

``ValueError`` is the error type the command already maps to
``ErrorCode.INVALID_ARGUMENT`` + exit 2 (see ``commands/_storage_describe.py``),
so raising it here needs no new wiring at the command layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# YAML-flavoured names for the types a section can wrongly be: the author is
# reading their own YAML, not Python, so "string"/"number" beat "str"/"int".
_TYPE_NAMES: dict[type, str] = {
    bool: "boolean",
    dict: "mapping",
    float: "number",
    int: "number",
    list: "list",
    str: "string",
    type(None): "null",
}

# What each top-level section must be, phrased for the error message.
_SECTION_SUBJECTS: dict[str, str] = {
    "buckets": "a mapping of bucket ID to description",
    "tables": "a mapping of table ID to description",
    "columns": "a mapping of table ID to a column mapping",
}

_COLUMN_ENTRY_SUBJECT = "a mapping of column name to description"
_DESCRIPTION_SUBJECT = "a description string"

# One copy-pasteable example per section, appended to every error raised for
# that section so the fix is visible without opening the docs.
_SECTION_EXAMPLES: dict[str, str] = {
    "buckets": "buckets:\n  in.c-sales: All sales data",
    "tables": "tables:\n  in.c-sales.orders: All sales orders",
    "columns": "columns:\n  in.c-sales.orders:\n    order_id: Unique order ID",
}

_TOP_LEVEL_EXAMPLE = "\n".join(_SECTION_EXAMPLES[key] for key in _SECTION_SUBJECTS)


@dataclass(frozen=True)
class DescribeBatchInput:
    """The three validated sections of a describe-batch document.

    Every description is already coerced to ``str`` here, so the write loop
    can hand values straight to the API without re-checking their type.
    """

    buckets: dict[str, str] = field(default_factory=dict)
    tables: dict[str, str] = field(default_factory=dict)
    columns: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        """Number of items across all sections (the progress-callback total)."""
        return len(self.buckets) + len(self.tables) + len(self.columns)


def _type_name(value: Any) -> str:
    """Return ``value``'s type named in YAML vocabulary."""
    return _TYPE_NAMES.get(type(value), type(value).__name__)


def _shape_error(key: str, expected: str, value: Any, example: str) -> ValueError:
    """Build the ValueError for one wrongly-shaped key."""
    return ValueError(
        f"'{key}' must be {expected}, got a {_type_name(value)}. Expected:\n{example}"
    )


def _mapping_section(raw: dict[str, Any], key: str) -> dict[Any, Any]:
    """Return top-level section ``key`` as a mapping (absent/``None`` -> empty)."""
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _shape_error(key, _SECTION_SUBJECTS[key], value, _SECTION_EXAMPLES[key])
    return value


def _description(value: Any, key: str, example: str) -> str:
    """Coerce a scalar description to ``str``, rejecting containers."""
    if isinstance(value, dict | list):
        raise _shape_error(key, _DESCRIPTION_SUBJECT, value, example)
    return str(value)


def _scalar_section(raw: dict[str, Any], key: str) -> dict[str, str]:
    """Validate a ``{id: description}`` section."""
    example = _SECTION_EXAMPLES[key]
    return {
        str(item_id): _description(desc, f"{key}.{item_id}", example)
        for item_id, desc in _mapping_section(raw, key).items()
    }


def _columns_section(raw: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Validate the nested ``{table_id: {column: description}}`` section."""
    example = _SECTION_EXAMPLES["columns"]
    parsed: dict[str, dict[str, str]] = {}
    for table_id, col_map in _mapping_section(raw, "columns").items():
        key = f"columns.{table_id}"
        if not isinstance(col_map, dict):
            raise _shape_error(key, _COLUMN_ENTRY_SUBJECT, col_map, example)
        parsed[str(table_id)] = {
            str(column): _description(desc, f"{key}.{column}", example)
            for column, desc in col_map.items()
        }
    return parsed


def parse_describe_batch_file(from_file: Path) -> DescribeBatchInput:
    """Read and validate a describe-batch YAML file.

    Args:
        from_file: Path to the ``--from-file`` document.

    Returns:
        The validated sections, descriptions coerced to ``str``.

    Raises:
        ValueError: The file is missing, is not valid YAML, is not a mapping
            at the top level, or any section/entry has the wrong shape. The
            command layer maps this to ``INVALID_ARGUMENT`` and exit 2.
    """
    import yaml

    if not from_file.is_file():
        raise ValueError(f"Batch file not found: {from_file}")
    try:
        raw = yaml.safe_load(from_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Batch file is not valid YAML: {exc}") from None
    if raw is None:
        return DescribeBatchInput()
    if not isinstance(raw, dict):
        raise _shape_error(
            from_file.name,
            "a YAML mapping of 'buckets' / 'tables' / 'columns' sections",
            raw,
            _TOP_LEVEL_EXAMPLE,
        )
    return DescribeBatchInput(
        buckets=_scalar_section(raw, "buckets"),
        tables=_scalar_section(raw, "tables"),
        columns=_columns_section(raw),
    )
