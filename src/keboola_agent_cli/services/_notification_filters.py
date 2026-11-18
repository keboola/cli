"""Notification subscription ``filters[]``: parse, validate, merge (issue #989).

One place for every caller that hands kbagent a filter list: the CLI's
``--filter 'FIELD<OP>VALUE'`` strings, the ``serve`` REST bodies and direct
SDK use all end in :func:`normalize_filters`, so the three accept exactly the
same filters.

Local checks are syntax only -- the field path shape and the operator set the
Notification Service declares (``field`` matches ``^[\\w-]+(\\.[\\w-]+)*$``,
``operator`` one of ``==``, ``<``, ``>``, ``<=``, ``>=``, omitted = ``==``).
Whether a field exists in the event's payload, and whether the value fits it,
is the service's call: it validates both against the event's data schema and
answers 400 for an unknown field. A per-event field list here would only drift.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, cast

from ..errors import ErrorCode, KeboolaApiError

# The operators the service accepts. ``=`` is a CLI/REST convenience for ``==``.
FILTER_OPERATORS = ("==", "<", ">", "<=", ">=")
_EQUALITY = "=="
_OPERATOR_ALIASES = {"=": _EQUALITY}

_FIELD_PATTERN = r"[\w-]+(?:\.[\w-]+)*"
_FIELD = re.compile(_FIELD_PATTERN)
# The field charset holds no operator character, so the FIRST operator splits
# the expression; the value after it is kept byte-for-byte. Two-character
# operators come before their one-character prefixes in the alternation.
_EXPRESSION = re.compile(rf"^({_FIELD_PATTERN})(==|<=|>=|=|<|>)(.*)$", re.DOTALL)

FILTER_FORM = "FIELD<OP>VALUE with OP one of ==, =, <=, >=, <, > (e.g. 'averageDuration>=60')"

_ALLOWED_KEYS = frozenset({"field", "operator", "value"})


def _invalid(message: str) -> KeboolaApiError:
    return KeboolaApiError(message=message, error_code=ErrorCode.INVALID_ARGUMENT)


def parse_filter_expression(expr: str) -> dict[str, Any]:
    """Parse one ``--filter`` expression into ``{field, operator, value}``.

    The value is always a string, for every operator: the service's numeric
    leaves accept numeric strings and compare them as numbers, and a string
    keeps an id such as ``00123`` intact. ``=`` is normalized to ``==``.

    Raises:
        KeboolaApiError: ``INVALID_ARGUMENT`` naming ``--filter`` when the
            expression has no operator or the field is not a dotted path.
    """
    match = _EXPRESSION.match(expr)
    if match is None:
        raise _invalid(f"Invalid --filter '{expr}'. Expected {FILTER_FORM}.")
    field_path, operator, value = match.groups()
    return {
        "field": field_path,
        "operator": _OPERATOR_ALIASES.get(operator, operator),
        "value": value,
    }


def parse_filter_expressions(exprs: list[str] | None) -> list[dict[str, Any]] | None:
    """Parse every ``--filter`` value; ``None`` (option not given) stays ``None``."""
    if exprs is None:
        return None
    return [parse_filter_expression(expr) for expr in exprs]


def _valid_value(value: object) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    return isinstance(value, float) and math.isfinite(value)


def normalize_filters(raw: list[Any] | None) -> list[dict[str, Any]]:
    """Validate caller-supplied filters and return them in wire shape.

    Each entry is a dict with ``field`` (a dotted path), an optional
    ``operator`` and a REQUIRED ``value`` (string, number, boolean or null).
    An absent operator stays absent (the service reads it as ``==``); ``=`` is
    sent as ``==``. ``None`` and ``[]`` both mean "no extra filters".

    Raises:
        KeboolaApiError: ``INVALID_ARGUMENT`` for anything else -- a non-list,
            a non-dict entry, an extra key, a missing value, a list/dict/NaN/
            infinite value, an unknown operator, or a malformed field.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _invalid("filters must be a list of {field, operator, value} objects.")
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise _invalid(
                f"Invalid filter {item!r}: expected an object {{field, operator, value}}."
            )
        # Keys of any type: a direct caller may pass ``{7: 2}``.
        extra = sorted(repr(key) for key in item if key not in _ALLOWED_KEYS)
        if extra:
            raise _invalid(f"Invalid filter {item!r}: unknown key(s) {', '.join(extra)}.")
        field_path = item.get("field")
        if not isinstance(field_path, str) or not _FIELD.fullmatch(field_path):
            raise _invalid(
                f"Invalid filter field {field_path!r}: expected a dotted path like 'job.component.id'."
            )
        if "value" not in item:
            raise _invalid(f"Filter on '{field_path}' has no value.")
        value = item["value"]
        if not _valid_value(value):
            raise _invalid(
                f"Filter on '{field_path}' has an unusable value {value!r}: use a string, number, boolean or null."
            )
        entry: dict[str, Any] = {"field": field_path}
        if "operator" in item and item["operator"] is not None:
            raw_operator = item["operator"]
            # Type first: an unhashable operator (a list) must not reach the dict lookup.
            operator = (
                _OPERATOR_ALIASES.get(raw_operator, raw_operator)
                if isinstance(raw_operator, str)
                else None
            )
            if operator not in FILTER_OPERATORS:
                raise _invalid(
                    f"Filter on '{field_path}' has an unknown operator {item['operator']!r}: use one of ==, =, <, <=, >, >=."
                )
            entry["operator"] = operator
        entry["value"] = value
        result.append(entry)
    return result


def operator_of(item: dict[str, Any]) -> str:
    """The effective operator of a wire filter: an omitted one means ``==``."""
    return str(item.get("operator") or _EQUALITY)


def is_equality(item: dict[str, Any]) -> bool:
    return operator_of(item) == _EQUALITY


def check_incoming_filters(
    dedicated: list[tuple[str, str, bool]],
    generic: list[dict[str, Any]],
) -> None:
    """Refuse an incoming filter set the caller cannot have meant.

    Args:
        dedicated: ``(option_name, field, given)`` per dedicated option, e.g.
            ``("--config-id", "job.configuration.id", True)``.
        generic: The normalized ``--filter`` / ``filters`` entries.

    Raises:
        KeboolaApiError: ``INVALID_ARGUMENT`` when a dedicated option and a
            generic filter target the same field (both are named), or when one
            ``(field, operator)`` pair appears twice among the generic filters.
            A range (one field, two operators) is allowed.
    """
    for option, field_path, given in dedicated:
        if given and any(item["field"] == field_path for item in generic):
            raise _invalid(
                f"{option} and --filter {field_path}... both set '{field_path}'. Use one of them."
            )
    seen: set[tuple[str, str]] = set()
    for item in generic:
        key = (item["field"], operator_of(item))
        if key in seen:
            raise _invalid(
                f"--filter sets '{item['field']} {key[1]}' twice. Give each field "
                "and operator once (a range uses two different operators)."
            )
        seen.add(key)


def validate_source_filters(subscription_id: str, source: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a source subscription's filters, refusing a shape a clone cannot copy.

    Unknown fields, an omitted operator and a null value are legal and are
    kept verbatim; ``filters: []`` (a project-wide subscription) is copied as
    empty. A MISSING or null ``filters`` key is refused, not read as ``[]``:
    copying it as "no filters" would create an alert that fires for every
    job. So are a non-list, a non-dict entry, and an entry without a string
    ``field`` or without a ``value`` key.

    Raises:
        KeboolaApiError: ``INVALID_ARGUMENT`` naming the subscription.
    """
    filters = source.get("filters")
    refusal = _invalid(
        f"Subscription {subscription_id} has filters kbagent cannot copy "
        f"({filters!r}). Recreate it with `kbagent notification create` instead."
    )
    if not isinstance(filters, list):
        raise refusal
    result: list[dict[str, Any]] = []
    for item in filters:
        if not (isinstance(item, dict) and isinstance(item.get("field"), str) and "value" in item):
            raise refusal
        result.append(cast(dict[str, Any], item))
    return result


@dataclass
class FilterMerge:
    """The outcome of overlaying incoming filters on a source subscription's filters."""

    filters: list[dict[str, Any]] = field(default_factory=list)
    replaced: list[dict[str, Any]] = field(default_factory=list)
    added: list[dict[str, Any]] = field(default_factory=list)
    kept: list[dict[str, Any]] = field(default_factory=list)


def merge_filters(source: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> FilterMerge:
    """Overlay ``incoming`` on ``source`` by field (grouped replacement).

    Every field that appears in ``incoming`` loses ALL its source filters (any
    operator), then gets its complete incoming group. Source filters on other
    fields are kept as the same dicts, in their order; the incoming groups are
    appended after them in incoming order.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in incoming:
        groups.setdefault(item["field"], []).append(item)

    merge = FilterMerge()
    merge.kept = [item for item in source if item["field"] not in groups]
    for field_path, new in groups.items():
        old = [item for item in source if item["field"] == field_path]
        if old:
            merge.replaced.append({"field": field_path, "old": old, "new": new})
        else:
            merge.added.extend(new)
    merge.filters = merge.kept + [item for new in groups.values() for item in new]
    return merge


__all__ = [
    "FILTER_FORM",
    "FILTER_OPERATORS",
    "FilterMerge",
    "check_incoming_filters",
    "is_equality",
    "merge_filters",
    "normalize_filters",
    "operator_of",
    "parse_filter_expression",
    "parse_filter_expressions",
    "validate_source_filters",
]
