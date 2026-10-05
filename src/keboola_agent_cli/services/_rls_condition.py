"""RLS condition primitives: shape constants, structural validation, preview.

Pure functions: no HTTP, no ``ConfigStore`` -- trivially unit-testable,
mirroring ``flow_validation.py``'s split for the same reason.

The condition/rules shape mirrored here is the one defined in
``keboola-mcp-server``'s RFC (``feature_spec/rls_query_tool/RFC.md``, "Rule
storage" section) -- kbagent never invents its own shape, it authors exactly
what the enforcement engine (that repo's ``rls.py::_compile_primitive``,
sqlglot-based) expects to read back from the metastore.

``compile_condition_preview`` below is a **preview convenience only**: a
small recursive pure-Python string renderer used for ``--dry-run`` /
confirmation display before a write. It is deliberately NOT the enforcement
engine and never will be -- the actual compiler that decides what SQL a
query gets rewritten to lives in ``keboola-mcp-server`` (a different repo,
sqlglot-based). Drift between the two renderings is acceptable here because
this string is only ever shown to an admin for sanity-checking, never
executed. Adding sqlglot as a kbagent dependency just to produce this
confirmation string would be a heavier dependency than the six condition
shapes below warrant.
"""

from __future__ import annotations

from typing import Any

import jsonschema

# The RFC's `condition` schema supports these operators; kept as a frozenset
# so the CLI/service layer can validate an op before it ever reaches the
# structural (schema) check, giving a clearer error for the common typo case.
RLS_COMPARISON_OPS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte"})
RLS_MEMBERSHIP_OPS = frozenset({"in", "not_in"})
RLS_NULLNESS_OPS = frozenset({"is_null", "is_not_null"})
RLS_CONDITION_OPS = RLS_COMPARISON_OPS | RLS_MEMBERSHIP_OPS | RLS_NULLNESS_OPS

# The metastore's `rls-policy` schema (RFC) restricts `dialect` to these two
# -- the same pair `rewrite_query()` on the enforcement side never transpiles
# between (predicates are pinned to one workspace dialect per policy).
RLS_DIALECTS: tuple[str, ...] = ("snowflake", "bigquery")

_COMPARISON_SQL = {
    "eq": "=",
    "ne": "!=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}


def validate_policy_structural(policy: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Draft7-validate a candidate ``rls-policy`` body against a live-fetched schema.

    ``policy`` is the full candidate body (``{"table", "dialect", "rules"}``).
    Defense in depth -- the metastore backend validates on write too, per the
    RFC. Returns human-readable error strings (empty = valid); never raises
    and never reaches the network -- ``schema`` must already be fetched by
    the caller (see ``RlsService.fetch_schema``).
    """
    validator = jsonschema.Draft7Validator(schema)
    errors: list[str] = []
    for err in sorted(validator.iter_errors(policy), key=lambda e: list(e.path)):
        path = "/".join(str(p) for p in err.path) or "(root)"
        errors.append(f"Schema error at {path}: {err.message}")
    return errors


def _is_true_sentinel(condition: dict[Any, Any]) -> bool:
    """Exactly ``{"true": true}``. ``{"true": false}`` (or ``0``/extra keys) would otherwise read as an
    always-true policy -- the opposite of what an author who typed ``false`` meant. ``is True`` because
    ``1 == True`` in Python."""
    return len(condition) == 1 and condition["true"] is True


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
    if op not in RLS_CONDITION_OPS:
        errors.append(f"unknown condition op {op!r} (expected one of {sorted(RLS_CONDITION_OPS)})")
        return errors
    # The primitive's shape: checked here so a malformed rule fails as INVALID_RLS_POLICY even when the
    # live schema is unavailable, instead of reaching `compile_condition_preview` and crashing it.
    column = condition.get("column")
    if not isinstance(column, str) or not column:
        errors.append(f"condition with op {op!r} needs a non-empty string 'column'")
    if op in RLS_COMPARISON_OPS and "value" not in condition:
        errors.append(f"condition with op {op!r} needs a 'value'")
    if op in RLS_MEMBERSHIP_OPS:
        values = condition.get("values")
        if not isinstance(values, list) or not values:
            errors.append(f"condition with op {op!r} needs a non-empty list 'values'")
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
        return []
    principals = rule["principals"]
    if (
        not isinstance(principals, list)
        or not principals
        or not all(isinstance(name, str) and name for name in principals)
    ):
        return [f"rules[{index}].principals must be a non-empty list of non-empty strings"]
    return []


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

    if op in _COMPARISON_SQL:
        return f"{column} {_COMPARISON_SQL[op]} {_preview_literal(condition.get('value'))}"
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
    if value is None:
        return "NULL"
    return str(value)
