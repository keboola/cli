"""Column-level security service -- business logic for ``kbagent cls``.

The ``cls-policy`` metastore object is the column-level sibling of
``rls-policy``: one object per protected table, ``{table, dialect, rules}``,
where each rule selects identities (``principal``, ``principals`` or IdP
``groups``) and the columns they may see (``visible_columns`` -- an allowlist
projection; masking is not supported). Under schema 1.1.0 an identity several
rules match sees the union of their columns; there is no ``default``.

Everything except the rule shape is identical to :class:`RlsService` (never
``project`` scope, partial (PATCH) update, live-schema validation that
degrades to a warning), so this class only overrides the policy type and the
rule / default hooks. Enforcement happens in ``keboola-mcp-server``'s
``query_data``, not here -- kbagent only authors policies.
"""

from __future__ import annotations

import re
from typing import Any

from ..errors import ErrorCode
from ..metastore_client import SemanticType
from . import _rls_condition
from .rls_service import RlsService

CLS_ITEM_TYPE: SemanticType = "cls-policy"

__all__ = ["CLS_ITEM_TYPE", "ClsService"]

# Same identifier pattern the ``cls-policy`` schema enforces on ``visible_columns`` entries.
_COLUMN_RE = re.compile(r"^[A-Za-z0-9_]+$")


class ClsService(RlsService):
    """Business logic for the ``cls`` command group."""

    item_type: SemanticType = CLS_ITEM_TYPE
    label = "CLS"
    invalid_error_code = ErrorCode.INVALID_CLS_POLICY

    def _local_rule_errors(self, rules: Any) -> list[str]:
        """Principal shape + a non-empty identifier allowlist per rule (schema-independent)."""
        if not isinstance(rules, list) or not rules:
            return ["'rules' must be a non-empty array"]
        errors: list[str] = []
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict):
                errors.append(f"rules[{index}] must be an object")
                continue
            errors.extend(_rls_condition.validate_principal_fields(rule, index))
            columns = rule.get("visible_columns")
            if not isinstance(columns, list) or not columns:
                errors.append(f"rules[{index}] needs a non-empty 'visible_columns' array")
                continue
            errors.extend(
                f"rules[{index}].visible_columns: {col!r} is not a valid column name"
                for col in columns
                if not isinstance(col, str) or not _COLUMN_RE.fullmatch(col)
            )
        return errors

    def _preview_rules(self, rules: list[dict[str, Any]], dialect: str) -> list[dict[str, Any]]:
        """Show each selector's allowed projection (``dialect`` is unused: no SQL is rendered)."""
        return [
            {**_rls_condition.rule_selector(rule), "visible_columns": rule["visible_columns"]}
            for rule in rules
        ]

    @staticmethod
    def _default_errors(default: Any) -> list[str]:
        """A ``cls-policy`` has no ``default``: an identity no rule matches is refused."""
        return [] if default is None else ["'default' is not supported by a CLS policy"]
