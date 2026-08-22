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

Empty is not malformed
----------------------
A section that is *absent*, ``None`` (a bare ``buckets:`` key), or an EMPTY
container (``[]``, ``''``, ``{}``) is an empty section, silently skipped. The
pre-validation code reached the same outcome through ``raw.get(key) or {}``,
and a file whose sections are generated (a templating step that emitted no
rows) must keep being a no-op rather than an exit-2 failure. Only a NON-EMPTY
wrong shape, and any other scalar (``false``, ``0``, a non-empty string), is
an error -- those carry content that would be silently dropped. The same rule
applies one level down to a table's column mapping.

A ``None`` *description*, on the other hand, is an error: ``str(None)`` used
to write the literal text "None" onto the object. An empty mapping has nothing
to say; a described object with no description is a mistake.

``ValueError`` is the error type the command already maps to
``ErrorCode.INVALID_ARGUMENT`` + exit 2 (see ``commands/_storage_describe.py``),
so raising it here needs no new wiring at the command layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from ..yaml_input import yaml_type_name


class _Section(NamedTuple):
    """What one top-level section must be, and how to write it correctly.

    Subject and example live together so an added or reworded section cannot
    end up quoting one section's rule beside another's example.
    """

    subject: str
    example: str


_SECTIONS: dict[str, _Section] = {
    "buckets": _Section(
        subject="a mapping of bucket ID to description",
        example="buckets:\n  in.c-sales: All sales data",
    ),
    "tables": _Section(
        subject="a mapping of table ID to description",
        example="tables:\n  in.c-sales.orders: All sales orders",
    ),
    "columns": _Section(
        subject="a mapping of table ID to a column mapping",
        example="columns:\n  in.c-sales.orders:\n    order_id: Unique order ID",
    ),
}

_COLUMN_ENTRY_SUBJECT = "a mapping of column name to description"
_DESCRIPTION_SUBJECT = "a description string"
_TOP_LEVEL_SUBJECT = "a YAML mapping of 'buckets' / 'tables' / 'columns' sections"
_TOP_LEVEL_EXAMPLE = "\n".join(section.example for section in _SECTIONS.values())


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


def _shape_error(key: str, expected: str, value: Any, example: str) -> ValueError:
    """Build the ValueError for one wrongly-shaped key."""
    return ValueError(
        f"'{key}' must be {expected}, got a {yaml_type_name(value)}. Expected:\n{example}"
    )


def _is_empty(value: Any) -> bool:
    """True for the values that mean "nothing here": None and empty containers.

    Deliberately not plain falsiness: ``False`` and ``0`` are content the
    author typed, so they stay errors rather than being silently skipped.
    """
    return value is None or (isinstance(value, dict | list | str) and len(value) == 0)


def _mapping(value: Any, key: str, subject: str, example: str) -> dict[Any, Any]:
    """Return ``value`` as a mapping; empty stands in for an omitted section."""
    if _is_empty(value):
        return {}
    if not isinstance(value, dict):
        raise _shape_error(key, subject, value, example)
    return value


def _description(value: Any, key: str, example: str) -> str:
    """Coerce a scalar description to ``str``, rejecting containers and null."""
    if value is None:
        raise ValueError(
            f"'{key}' has no description (empty value); write a non-empty string. "
            f"Expected:\n{example}"
        )
    if isinstance(value, dict | list):
        raise _shape_error(key, _DESCRIPTION_SUBJECT, value, example)
    return str(value)


def _coerced_key(key: Any, seen: dict[str, Any], prefix: str, example: str) -> str:
    """Return ``str(key)``, refusing one that another key already claimed.

    YAML types keys, kbagent addresses objects by string: ``1:`` and ``"1":``
    are two distinct YAML keys that both coerce to ``"1"``, and the second
    would silently overwrite the first -- a description the author wrote and
    never saw applied.
    """
    coerced = str(key)
    if coerced in seen:
        raise ValueError(
            f"'{prefix}' has two entries that both resolve to the ID '{coerced}' "
            f'(YAML keys of different types, e.g. `1:` and `"1":`); keep one. '
            f"Expected:\n{example}"
        )
    return coerced


def _scalar_section(raw: dict[str, Any], key: str) -> dict[str, str]:
    """Validate a ``{id: description}`` section."""
    section = _SECTIONS[key]
    parsed: dict[str, str] = {}
    for item_id, desc in _mapping(raw.get(key), key, section.subject, section.example).items():
        coerced = _coerced_key(item_id, parsed, key, section.example)
        parsed[coerced] = _description(desc, f"{key}.{coerced}", section.example)
    return parsed


def _columns_section(raw: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Validate the nested ``{table_id: {column: description}}`` section.

    A table whose column mapping is empty is dropped, not written: there is
    nothing to say about it, and an empty write is a pointless API roundtrip.
    """
    section = _SECTIONS["columns"]
    example = section.example
    parsed: dict[str, dict[str, str]] = {}
    for table_id, col_map in _mapping(
        raw.get("columns"), "columns", section.subject, example
    ).items():
        table_key = _coerced_key(table_id, parsed, "columns", example)
        key = f"columns.{table_key}"
        columns: dict[str, str] = {}
        for column, desc in _mapping(col_map, key, _COLUMN_ENTRY_SUBJECT, example).items():
            coerced = _coerced_key(column, columns, key, example)
            columns[coerced] = _description(desc, f"{key}.{coerced}", example)
        if columns:
            parsed[table_key] = columns
    return parsed


def parse_describe_batch_file(from_file: Path) -> DescribeBatchInput:
    """Read and validate a describe-batch YAML file.

    Args:
        from_file: Path to the ``--from-file`` document.

    Returns:
        The validated sections, descriptions coerced to ``str``. Absent,
        ``None`` and empty sections come back empty.

    Raises:
        ValueError: The file is missing, is not valid YAML, is not a mapping
            at the top level, or any section/entry has a wrong non-empty
            shape, a null description, or a duplicate coerced key. The command
            layer maps this to ``INVALID_ARGUMENT`` and exit 2.
    """
    import yaml

    if not from_file.is_file():
        raise ValueError(f"Batch file not found: {from_file}")
    try:
        raw = yaml.safe_load(from_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Batch file is not valid YAML: {exc}") from None
    if _is_empty(raw):
        return DescribeBatchInput()
    if not isinstance(raw, dict):
        raise _shape_error(from_file.name, _TOP_LEVEL_SUBJECT, raw, _TOP_LEVEL_EXAMPLE)
    return DescribeBatchInput(
        buckets=_scalar_section(raw, "buckets"),
        tables=_scalar_section(raw, "tables"),
        columns=_columns_section(raw),
    )
