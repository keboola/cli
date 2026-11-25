"""Local validation for the discriminated transition condition tree."""

from __future__ import annotations

from typing import Any

_BINARY_OPERATORS = ("EQUALS", "NOT_EQUALS", "GREATER_THAN", "LESS_THAN", "INCLUDES", "CONTAINS")
_LOGICAL_OPERATORS = ("AND", "OR")
_PHASE_OPERATORS = ("ALL_TASKS_IN_PHASE", "ANY_TASKS_IN_PHASE")
_ANY_PHASE_OPERATOR = "ANY_TASKS_IN_ANY_PHASE"
_FAILURE_OPERATOR = "CONTINUE_ON_FAILURE"
_OPERATORS = (
    *_LOGICAL_OPERATORS,
    *_BINARY_OPERATORS,
    *_PHASE_OPERATORS,
    _ANY_PHASE_OPERATOR,
    _FAILURE_OPERATOR,
)
_TYPES = ("task", "phase", "const", "constant", "variable", "operator", "function", "array")
_STRING_FIELDS = {
    "task": ("task", "value"),
    "phase": ("phase", "value"),
    "variable": ("value",),
    "function": ("function",),
}


def transition_condition_errors(phases: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for i, phase in enumerate(phases):
        for j, transition in enumerate(phase.get("next", [])):
            condition = transition.get("condition")
            if condition is not None:
                errors.extend(_condition_errors(condition, f"phases[{i}].next[{j}].condition"))
    return errors


def _condition_errors(condition: Any, path: str, *, operators_allowed: bool = True) -> list[str]:
    allowed = f"Allowed operators: {', '.join(_OPERATORS)}. See `kbagent flow schema --full`."
    if not isinstance(condition, dict):
        return [f"{path}: 'condition' must be an object, got {type(condition).__name__}. {allowed}"]

    condition_type = condition.get("type")
    operator = condition.get("operator")
    issues: list[str] = []
    operands: list[Any] = []
    if condition_type not in _TYPES:
        issues.append(
            f"unknown condition type {condition_type!r}; expected one of {', '.join(_TYPES)}"
        )
    elif condition_type == "operator" and not operators_allowed:
        issues.append("operator conditions are not valid operands of functions or arrays")
    elif condition_type == "operator":
        if not isinstance(operator, str):
            issues.append("'operator' must be a string")
        elif operator not in _OPERATORS:
            issues.append(f"{operator!r} is not a flow operator")
        else:
            issues.extend(_operator_issues(condition, operator))
    else:
        for key in _STRING_FIELDS.get(condition_type, ()):
            if not isinstance(condition.get(key), str):
                issues.append(f"'{key}' must be a string")
        if condition_type in ("const", "constant") and not isinstance(
            condition.get("value"), str | int | float | bool | list
        ):
            issues.append("'value' must be a string, number, boolean or array")

    takes_operands = condition_type in ("function", "array") or (
        condition_type == "operator" and operator != _FAILURE_OPERATOR
    )
    if takes_operands:
        raw_operands = condition.get("operands")
        if not isinstance(raw_operands, list):
            issues.append("'operands' must be a list")
        else:
            operands = raw_operands
            if (
                condition_type == "operator"
                and operator in _BINARY_OPERATORS
                and len(operands) != 2
            ):
                issues.append(f"requires exactly 2 operands, got {len(operands)}")
            elif condition_type == "operator" and operator in _LOGICAL_OPERATORS and not operands:
                issues.append("requires at least 1 operand")
            elif (
                condition_type == "function"
                and condition.get("function") in ("COUNT", "DATE")
                and len(operands) != 1
            ):
                issues.append(f"requires exactly 1 operand, got {len(operands)}")

    errors: list[str] = []
    if issues:
        name = (
            f"{condition_type!r} operator {operator!r}"
            if condition_type == "operator"
            else f"function {condition.get('function')!r}"
            if condition_type == "function"
            else repr(condition_type)
        )
        errors.append(f"{path}: invalid condition {name}: {'; '.join(issues)}. {allowed}")
    for i, child in enumerate(operands):
        errors.extend(
            _condition_errors(
                child, f"{path}.operands[{i}]", operators_allowed=condition_type == "operator"
            )
        )
    return errors


def _operator_issues(condition: dict[str, Any], operator: str) -> list[str]:
    issues: list[str] = []
    if operator in (*_PHASE_OPERATORS, _FAILURE_OPERATOR):
        phase = condition.get("phase")
        if not isinstance(phase, str):
            issues.append("'phase' must be a string")
        elif operator in _PHASE_OPERATORS and not phase:
            issues.append("'phase' must not be empty")
    if operator == _FAILURE_OPERATOR:
        tasks = condition.get("tasks")
        if not isinstance(tasks, list) or not all(isinstance(task, str) for task in tasks):
            issues.append("'tasks' must be a list of strings")
    return issues
