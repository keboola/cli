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
        return errors
    if "and" in condition or "or" in condition:
        clauses = condition.get("and", condition.get("or"))
        if not isinstance(clauses, list) or len(clauses) < 2:
            key = "and" if "and" in condition else "or"
            errors.append(f"'{key}' requires at least 2 nested conditions")
            return errors
        for clause in clauses:
            errors.extend(validate_condition_ops(clause))
        return errors
    op = condition.get("op")
    if op not in RLS_CONDITION_OPS:
        errors.append(f"unknown condition op {op!r} (expected one of {sorted(RLS_CONDITION_OPS)})")
    return errors


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
        principals_value = rule.get("principals")
        has_principal = bool(rule.get("principal"))
        has_principals = isinstance(principals_value, list) and bool(principals_value)
        if has_principal == has_principals:  # both or neither
            errors.append(f"rules[{index}] must set exactly one of 'principal'/'principals'")
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
        return "TRUE"
    if "and" in condition:
        clauses = condition["and"]
        return " AND ".join(f"({compile_condition_preview(c, dialect)})" for c in clauses)
    if "or" in condition:
        clauses = condition["or"]
        return " OR ".join(f"({compile_condition_preview(c, dialect)})" for c in clauses)

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
