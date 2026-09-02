"""Conditional-flow (keboola.flow) validation.

Pure functions: no HTTP, no ConfigStore -- trivially unit-testable.

The structural JSON Schema (Draft7) is NOT bundled. It is fetched at runtime
from the stack's component registry (AI Service ``configurationSchema`` for
``keboola.flow``) by the caller and passed into ``validate_conditional_flow``
as an explicit parameter. When no schema is available (offline, fetch failure,
empty schema) structural validation is skipped and only the semantic checks
run -- this module never reaches the network itself.
"""

from __future__ import annotations

from collections import deque
from typing import Any

import jsonschema

# Operand arity per operator (semantic; the schema cannot express these counts).
_BINARY_OPERATORS = frozenset(
    {"EQUALS", "NOT_EQUALS", "GREATER_THAN", "LESS_THAN", "INCLUDES", "CONTAINS"}
)
_VARIADIC_MIN1_OPERATORS = frozenset({"AND", "OR"})
_PHASE_SCOPED_OPERATORS = frozenset({"ALL_TASKS_IN_PHASE", "ANY_TASKS_IN_PHASE"})
_UNARY_FUNCTIONS = frozenset({"COUNT", "DATE"})


def _structural_errors(
    phases: list[dict[str, Any]], tasks: list[dict[str, Any]], schema: dict[str, Any]
) -> list[str]:
    """Run Draft7 validation against the supplied schema, collecting ALL errors."""
    document = {"phases": phases, "tasks": tasks}
    validator = jsonschema.Draft7Validator(schema)
    errors: list[str] = []
    for err in sorted(validator.iter_errors(document), key=lambda e: list(e.path)):
        path = "/".join(str(p) for p in err.path) or "(root)"
        errors.append(f"Schema error at {path}: {err.message}")
    return errors


def _condition_arity_errors(condition: Any) -> list[str]:
    """Recursively check operator/function operand arity."""
    if not isinstance(condition, dict):
        return []
    errors: list[str] = []
    ctype = condition.get("type")
    operands = condition.get("operands", [])

    if ctype == "operator":
        op = condition.get("operator")
        if op in _BINARY_OPERATORS and len(operands) != 2:
            errors.append(f"Operator '{op}' requires exactly 2 operands, got {len(operands)}")
        elif op in _VARIADIC_MIN1_OPERATORS and len(operands) < 1:
            errors.append(f"Operator '{op}' requires at least 1 operand, got {len(operands)}")
        elif op in _PHASE_SCOPED_OPERATORS and not condition.get("phase"):
            errors.append(f"Operator '{op}' requires a 'phase' field")
    elif ctype == "function":
        fn = condition.get("function")
        if fn in _UNARY_FUNCTIONS and len(operands) != 1:
            errors.append(f"Function '{fn}' requires exactly 1 operand, got {len(operands)}")

    for child in operands:
        errors.extend(_condition_arity_errors(child))
    return errors


def _semantic_errors(phases: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []

    # Unique phase ids
    phase_ids: list[str] = [str(p.get("id")) for p in phases]
    seen: set[str] = set()
    for pid in phase_ids:
        if pid in seen:
            errors.append(f"Duplicate phase id '{pid}'")
        seen.add(pid)
    valid_phase_ids = set(phase_ids)

    # Unique task ids
    seen_tasks: set[str] = set()
    for task in tasks:
        tid = str(task.get("id"))
        if tid in seen_tasks:
            errors.append(f"Duplicate task id '{tid}'")
        seen_tasks.add(tid)

    # task.phase references an existing phase
    for task in tasks:
        ref = str(task.get("phase"))
        if ref not in valid_phase_ids:
            errors.append(f"Task '{task.get('id', '?')}' references unknown phase '{ref}'")

    # next[].goto is an existing phase id or null
    for phase in phases:
        for transition in phase.get("next", []):
            goto = transition.get("goto")
            if goto is not None and str(goto) not in valid_phase_ids:
                errors.append(
                    f"Phase '{phase.get('id', '?')}' transition goto '{goto}' "
                    f"is not an existing phase id (use null to end the flow)"
                )

    # A phase with conditional transitions must end with a default
    # (condition-less) transition.
    for phase in phases:
        nexts = phase.get("next", [])
        if not nexts:
            continue
        has_conditional = any("condition" in t for t in nexts)
        last_is_default = "condition" not in nexts[-1]
        if has_conditional and not last_is_default:
            errors.append(
                f"Phase '{phase.get('id', '?')}' has conditional transitions but "
                f"no default (condition-less) transition as the last next[] item"
            )

    # Every phase must have at least one enabled task.
    enabled_by_phase: dict[str, int] = {str(p.get("id")): 0 for p in phases}
    for task in tasks:
        if task.get("enabled", True):
            enabled_by_phase[str(task.get("phase"))] = (
                enabled_by_phase.get(str(task.get("phase")), 0) + 1
            )
    for phase in phases:
        pid = str(phase.get("id"))
        if enabled_by_phase.get(pid, 0) == 0:
            errors.append(f"Phase '{pid}' has no enabled task")

    # Condition operand arity (recursive).
    for phase in phases:
        for transition in phase.get("next", []):
            cond = transition.get("condition")
            if cond is not None:
                errors.extend(_condition_arity_errors(cond))

    return errors


def validate_conditional_flow(
    phases: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    schema: dict[str, Any] | None = None,
) -> list[str]:
    """Validate a conditional-flow body. Returns a flat list of error strings
    (empty == valid). Reachability is computed separately as a warning -- call
    ``find_unreachable_phases`` for that. NO cycle detection: goto loops are
    legal at runtime.

    Structural (Draft7) validation runs ONLY when ``schema`` is supplied (the
    schema is fetched at runtime from the stack by the caller -- this module is
    pure). When the structure is unsound the structural errors are returned
    first and the semantic checks are skipped, to avoid cascade noise from a
    malformed document.

    The semantic checks (unique ids, task.phase refs, goto refs,
    default-transition rule, enabled-task-per-phase, operator/function arity)
    ALWAYS run -- with or without a schema -- because the Storage API does not
    validate flow configurations server-side.
    """
    if schema:
        structural = _structural_errors(phases, tasks, schema)
        if structural:
            return structural
    return _semantic_errors(phases, tasks)


def reachable_phases(phases: list[dict[str, Any]], start_id: str) -> list[str]:
    """Return the ids of ``start_id`` and every phase reachable from it.

    Walks ``next[].goto`` edges breadth-first; a ``goto`` of ``None`` is the
    flow's END marker and terminates that branch. Ids come back in the order
    they appear in ``phases`` (not in visit order), so callers get a stable,
    flow-ordered selection regardless of the graph shape. An unknown
    ``start_id`` yields an empty list -- callers validate the id themselves so
    they can raise with the list of valid ones.
    """
    by_id = {str(p.get("id")): p for p in phases}
    if start_id not in by_id:
        return []
    seen: set[str] = set()
    queue: deque[str] = deque([start_id])
    while queue:
        pid = queue.popleft()
        if pid in seen or pid not in by_id:
            continue
        seen.add(pid)
        for transition in by_id[pid].get("next", []):
            goto = transition.get("goto")
            if goto is not None:
                queue.append(str(goto))
    return [str(p.get("id")) for p in phases if str(p.get("id")) in seen]


def find_unreachable_phases(phases: list[dict[str, Any]]) -> list[str]:
    """Return ids of phases not reachable from the entry phase (first in the
    list) by following next[].goto edges. WARNING-level only -- never blocks a
    write. Returns ids in the order they appear in ``phases``.
    """
    if not phases:
        return []
    reachable = set(reachable_phases(phases, str(phases[0].get("id"))))
    return [str(p.get("id")) for p in phases if str(p.get("id")) not in reachable]
