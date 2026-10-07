"""RLS condition primitives: shape constants, structural validation, preview.

Pure functions: no HTTP, no ``ConfigStore`` -- trivially unit-testable,
mirroring ``flow_validation.py``'s split for the same reason.

The condition/rules shape mirrored here is the one defined in
``keboola-mcp-server``'s RFC (``feature_spec/rls_query_tool/RFC.md``, "Rule
storage" section) -- kbagent never invents its own shape, it authors exactly
what the enforcement engine (that repo's ``rls.py::_compile_primitive``,
sqlglot-based) expects to read back from the metastore.

``compile_condition_preview`` below is a **preview** for ``--dry-run`` /
confirmation display, not the enforcement engine -- that lives in
``keboola-mcp-server`` (sqlglot-based, a different repo). It follows the
enforcement's rendering where an admin could be misled otherwise: columns
quoted per dialect (the enforcement matches them case-exactly), booleans as
``TRUE``/``FALSE``. ``null`` comparisons never reach it: the enforcement would
render ``col = NULL`` (matches nothing), so validation refuses them. Adding
sqlglot as a kbagent dependency just for this string is not warranted.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

# The condition operators of the policy schema, checked locally so a typo gets a specific error
# even when the live schema is unavailable. COMPARISON_SQL also renders the preview.
COMPARISON_SQL = {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
RLS_COMPARISON_OPS = frozenset(COMPARISON_SQL)
RLS_MEMBERSHIP_OPS = frozenset({"in", "not_in"})
RLS_NULLNESS_OPS = frozenset({"is_null", "is_not_null"})
RLS_CONDITION_OPS = RLS_COMPARISON_OPS | RLS_MEMBERSHIP_OPS | RLS_NULLNESS_OPS


# The metastore's `rls-policy` schema (RFC) restricts `dialect` to these two
# -- the same pair `rewrite_query()` on the enforcement side never transpiles
# between (predicates are pinned to one workspace dialect per policy).
class Dialect(StrEnum):
    """The policy schema's ``dialect`` enum (the CLI ``--dialect`` choice and the REST field)."""

    SNOWFLAKE = "snowflake"
    BIGQUERY = "bigquery"


RLS_DIALECTS: tuple[str, ...] = tuple(Dialect)

# The enforcement's principal pattern (mcp-server ``rls.py``): no whitespace or control characters. A
# principal that fails it is not ignored there -- it refuses EVERY query in the project.
_PRINCIPAL_RE = re.compile(r"^[^\s\x00-\x1f\x7f]+$")

# How each dialect quotes an identifier. The enforcement compiles columns quoted (case-exact), so the
# preview must too -- an unquoted preview would hide a case mismatch the real filter does not forgive.
_QUOTE = {"snowflake": '"', "bigquery": "`"}


def _is_true_sentinel(condition: dict[Any, Any]) -> bool:
    """Exactly ``{"true": true}``. ``{"true": false}`` (or ``0``/extra keys) would otherwise read as an
    always-true policy -- the opposite of what an author who typed ``false`` meant. ``is True`` because
    ``1 == True`` in Python."""
    return len(condition) == 1 and condition["true"] is True


def _is_scalar(value: Any) -> bool:
    """A literal the primitives allow: string, number or boolean -- never an object or array."""
    return isinstance(value, str | int | float | bool)


def validate_condition_ops(condition: Any) -> list[str]:
    """Recursively check every operator name used in ``condition`` is known.

    A defensive pre-check ahead of the (possibly unavailable) live schema
    fetch: an unknown ``op`` typo gets a specific error here instead of a
    generic Draft7 "not valid under any of the given schemas" once it does
    reach structural validation. Returns human-readable error strings.
    """
    errors: list[str] = []
    if not isinstance(condition, dict):
        return [f"condition must be an object, got {type(condition).__name__}"]
    if "true" in condition:
        if not _is_true_sentinel(condition):
            errors.append("'true' condition must be exactly {\"true\": true}")
        return errors
    if "and" in condition or "or" in condition:
        # Exactly one composition key and nothing else: `{"and": [...], "or": [...]}` would otherwise have
        # one branch silently validated and the other dropped, so the policy would no longer be the tree
        # the author submitted.
        if set(condition) not in ({"and"}, {"or"}):
            errors.append(
                f"an 'and'/'or' condition must have exactly one of those keys and no others, got {sorted(condition)}"
            )
            return errors
        key = "and" if "and" in condition else "or"
        clauses = condition[key]
        if not isinstance(clauses, list) or len(clauses) < 2:
            errors.append(f"'{key}' requires at least 2 nested conditions")
            return errors
        for clause in clauses:
            errors.extend(validate_condition_ops(clause))
        return errors
    op = condition.get("op")
    if not isinstance(op, str) or op not in RLS_CONDITION_OPS:
        errors.append(f"unknown condition op {op!r} (expected one of {sorted(RLS_CONDITION_OPS)})")
        return errors
    # The primitive's shape: checked here so a malformed rule fails as INVALID_RLS_POLICY even when the
    # live schema is unavailable, instead of reaching `compile_condition_preview` and crashing it.
    column = condition.get("column")
    if not isinstance(column, str) or not column:
        errors.append(f"condition with op {op!r} needs a non-empty string 'column'")
    if op in RLS_COMPARISON_OPS:
        if "value" not in condition:
            errors.append(f"condition with op {op!r} needs a 'value'")
        elif condition["value"] is None:
            # The enforcement renders `col = NULL`, which matches no row: the principal silently sees nothing.
            errors.append(
                f"condition with op {op!r} cannot compare to null; use op 'is_null' / 'is_not_null'"
            )
        elif not _is_scalar(condition["value"]):
            errors.append(f"condition with op {op!r} needs a string, number or boolean 'value'")
    if op in RLS_MEMBERSHIP_OPS:
        values = condition.get("values")
        if not isinstance(values, list) or not values:
            errors.append(f"condition with op {op!r} needs a non-empty list 'values'")
        elif any(value is None for value in values):
            errors.append(
                f"condition with op {op!r} cannot list null in 'values' (it never matches); use 'is_null'"
            )
        elif not all(_is_scalar(value) for value in values):
            errors.append(f"condition with op {op!r} needs string, number or boolean 'values'")
    # No keys beyond the op's own shape (the schema forbids additional properties): a stray `values` on an
    # `eq`, or a `value` on an `in`, is an authoring mistake that would otherwise be silently ignored.
    allowed = {"column", "op"} | (
        {"value"} if op in RLS_COMPARISON_OPS else {"values"} if op in RLS_MEMBERSHIP_OPS else set()
    )
    if extra := sorted(set(condition) - allowed):
        errors.append(f"condition with op {op!r} has unexpected keys {extra}")
    return errors


def validate_principal_fields(rule: dict[Any, Any], index: int) -> list[str]:
    """Each rule names exactly one of ``principal``/``principals`` (the schema's ``oneOf``).

    Shared by the RLS and CLS (``cls_service``) local checks -- both policy
    types use the identical principal shape.
    """
    # Key PRESENCE decides "exactly one" (a truthiness test would let `{"principal": "a", "principals": []}`
    # through as if `principals` were absent); the VALUE is then checked on its own.
    has_principal = "principal" in rule
    has_principals = "principals" in rule
    if has_principal == has_principals:  # both or neither
        return [f"rules[{index}] must set exactly one of 'principal'/'principals'"]
    if has_principal:
        principal = rule["principal"]
        if not isinstance(principal, str) or not principal:
            return [f"rules[{index}].principal must be a non-empty string"]
        names = [principal]
    else:
        names = rule["principals"]
        if (
            not isinstance(names, list)
            or not names
            or not all(isinstance(name, str) and name for name in names)
        ):
            return [f"rules[{index}].principals must be a non-empty list of non-empty strings"]
    return [
        f"rules[{index}]: principal {name!r} contains whitespace or control characters"
        for name in names
        if not _PRINCIPAL_RE.fullmatch(name)
    ]


def rule_principals(rule: dict[str, Any]) -> list[str]:
    """The principals one rule names (``principal`` or ``principals``), as written."""
    return [rule["principal"]] if "principal" in rule else list(rule.get("principals") or [])


def duplicate_principals(
    rules: list[dict[str, Any]], taken: dict[str, str] | None = None
) -> list[str]:
    """Principals named twice on one table, case-folded -- the enforcement refuses every query then.

    ``taken`` maps an already-used case-folded principal to where it is used (another policy on the
    same table), so the same check covers duplicates inside ``rules`` and across policies.
    """
    seen = dict(taken or {})
    errors: list[str] = []
    for index, rule in enumerate(rules):
        for name in rule_principals(rule):
            folded = name.casefold()
            if folded in seen:
                errors.append(
                    f"rules[{index}]: principal {name!r} already has a rule in {seen[folded]}"
                )
            else:
                seen[folded] = f"rules[{index}]"
    return errors


def table_key(table: str, dialect: str) -> str:
    """How the enforcement compares table keys: case-insensitive on Snowflake, exact on BigQuery."""
    return table.lower() if dialect == "snowflake" else table


def validate_rules_local(rules: Any) -> list[str]:
    """Semantic checks on ``rules`` that hold regardless of schema availability.

    Runs even when the live schema fetch degraded (see
    ``RlsService.fetch_schema``) -- the one check this module can still make
    with no network access at all: each rule names exactly one of
    ``principal``/``principals`` (the RFC's ``oneOf``, which a Draft7
    validator would otherwise be the only thing checking) and every
    ``condition`` uses a known operator (:func:`validate_condition_ops`).
    Returns human-readable error strings (empty = valid).
    """
    errors: list[str] = []
    if not isinstance(rules, list) or not rules:
        return ["'rules' must be a non-empty array"]
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            errors.append(f"rules[{index}] must be an object")
            continue
        errors.extend(validate_principal_fields(rule, index))
        condition = rule.get("condition")
        if condition is None:
            errors.append(f"rules[{index}] is missing 'condition'")
            continue
        errors.extend(f"rules[{index}].{err}" for err in validate_condition_ops(condition))
    return errors


def compile_condition_preview(condition: dict[str, Any], dialect: str) -> str:
    """Render ``condition`` as a human-readable ``WHERE``-clause-shaped string.

    Preview only -- see module docstring. Raises ``ValueError`` on a
    condition shape this renderer doesn't recognize (callers should already
    have run it through :func:`validate_condition_ops` / structural
    validation first; this function stays strict rather than silently
    rendering something misleading).
    """
    if "true" in condition:
        if not _is_true_sentinel(condition):
            raise ValueError(f"'true' condition must be exactly {{\"true\": true}}: {condition!r}")
        return "TRUE"
    if "and" in condition or "or" in condition:
        if len(condition) != 1:
            raise ValueError(
                f"an 'and'/'or' condition must have exactly one of those keys and no others: {condition!r}"
            )
        if "and" in condition:
            return " AND ".join(
                f"({compile_condition_preview(c, dialect)})" for c in condition["and"]
            )
        return " OR ".join(f"({compile_condition_preview(c, dialect)})" for c in condition["or"])

    column = condition.get("column")
    op = condition.get("op")
    if not column or op is None:
        raise ValueError(f"unrecognized condition shape: {condition!r}")

    quote = _QUOTE.get(dialect, '"')
    column = f"{quote}{column}{quote}"
    if op in COMPARISON_SQL:
        return f"{column} {COMPARISON_SQL[op]} {_preview_literal(condition.get('value'))}"
    if op in RLS_MEMBERSHIP_OPS:
        values = condition.get("values") or []
        rendered = ", ".join(_preview_literal(v) for v in values)
        keyword = "IN" if op == "in" else "NOT IN"
        return f"{column} {keyword} ({rendered})"
    if op == "is_null":
        return f"{column} IS NULL"
    if op == "is_not_null":
        return f"{column} IS NOT NULL"
    raise ValueError(f"unrecognized condition op: {op!r}")


def _preview_literal(value: Any) -> str:
    """Render one scalar literal for the preview string. Not SQL-injection-safe

    on purpose -- this output is never executed, only displayed for human
    review (see module docstring).
    """
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, bool):  # before the generic str(): Python's `True` is SQL's TRUE
        return "TRUE" if value else "FALSE"
    if value is None:
        return "NULL"
    return str(value)
