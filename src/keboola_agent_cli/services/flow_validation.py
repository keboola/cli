"""Conditional-flow (keboola.flow) validation.

Pure functions: no HTTP, no ConfigStore -- trivially unit-testable.

The caller supplies the stack's live JSON Schema (Draft7) when available.
Local condition and semantic checks run even without a live schema.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from ..json_utils import draft7_errors
from ._flow_conditions import transition_condition_errors
from .flow_run_selection import first_phase_ids, normalize_id


def with_inferred_job_task_types(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize implicit jobs on a copy, leaving explicitly typed tasks untouched."""
    normalized = []
    for task in tasks:
        inner = task.get("task")
        if (
            isinstance(inner, dict)
            and "type" not in inner
            and ("componentId" in inner or "component_id" in inner)
        ):
            job = {**inner, "type": "job"}
            if "component_id" in job:
                job.setdefault("componentId", job.pop("component_id"))
            task = {**task, "task": job}
        normalized.append(task)
    return normalized


def _semantic_errors(phases: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []

    # Unique phase ids
    phase_ids: list[str] = [normalize_id(p.get("id")) for p in phases]
    seen: set[str] = set()
    for pid in phase_ids:
        if pid in seen:
            errors.append(f"Duplicate phase id '{pid}'")
        seen.add(pid)
    valid_phase_ids = set(phase_ids)

    # Unique task ids
    seen_tasks: set[str] = set()
    for task in tasks:
        tid = normalize_id(task.get("id"))
        if tid in seen_tasks:
            errors.append(f"Duplicate task id '{tid}'")
        seen_tasks.add(tid)

    # task.phase references an existing phase
    for task in tasks:
        ref = normalize_id(task.get("phase"))
        if ref not in valid_phase_ids:
            errors.append(f"Task '{task.get('id', '?')}' references unknown phase '{ref}'")

    # next[].goto is an existing phase id or null
    for phase in phases:
        for transition in phase.get("next", []):
            goto = transition.get("goto")
            if goto is not None and normalize_id(goto) not in valid_phase_ids:
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
    enabled_by_phase: dict[str, int] = {normalize_id(p.get("id")): 0 for p in phases}
    for task in tasks:
        if task.get("enabled", True):
            enabled_by_phase[normalize_id(task.get("phase"))] = (
                enabled_by_phase.get(normalize_id(task.get("phase")), 0) + 1
            )
    for phase in phases:
        pid = normalize_id(phase.get("id"))
        if enabled_by_phase.get(pid, 0) == 0:
            errors.append(f"Phase '{pid}' has no enabled task")

    # Exactly one first phase: the job queue refuses any other flow ("Flow
    # must have exactly one first step"), whole run or partial.
    roots = first_phase_ids(phases)
    if len(roots) != 1:
        found = ", ".join(roots) if roots else "none"
        errors.append(
            "The flow must have exactly one first phase (a phase no transition goes to); "
            f"found: {found}"
        )

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

    Check the condition tree before Draft7 validation so malformed conditions
    get specific hints instead of a generic union error. With no live schema,
    local condition and semantic checks still validate the definition.
    """
    shape = _shape_errors(phases, tasks)
    if shape:
        return shape
    condition_errors = transition_condition_errors(phases)
    if condition_errors:
        return condition_errors
    if schema:
        structural = draft7_errors(
            {"phases": phases, "tasks": with_inferred_job_task_types(tasks)}, schema
        )
        if structural:
            return structural
    if not phases:
        # Checked here, not in the schema: a flow with no phases runs nothing,
        # and `flow new --definition-file empty.yaml` used to create one.
        return ["Flow definition has no phases"]
    return _semantic_errors(phases, tasks)


def _shape_errors(phases: Any, tasks: Any) -> list[str]:
    """The lists the semantic checks iterate are lists of objects.

    Checked before the schema, so a malformed document gets an error instead
    of a crash also when no schema could be fetched.
    """
    errors: list[str] = []
    for name, items in (("phases", phases), ("tasks", tasks)):
        if not isinstance(items, list):
            errors.append(f"'{name}' must be a list, got {type(items).__name__}")
        elif not all(isinstance(item, dict) for item in items):
            errors.append(f"Every entry of '{name}' must be an object")
    if errors:
        return errors
    for phase in phases:
        nexts = phase.get("next", [])
        if not isinstance(nexts, list) or not all(isinstance(t, dict) for t in nexts):
            errors.append(f"Phase '{phase.get('id', '?')}': 'next' must be a list of objects")
    # Ids the daemon compares as strings: a list, an object, a boolean or a
    # missing id cannot name a phase or a task.
    for kind, items, keys in (("Phase", phases, ("id",)), ("Task", tasks, ("id", "phase"))):
        for item in items:
            for key in keys:
                value = item.get(key)
                if isinstance(value, bool) or not isinstance(value, str | int | float):
                    errors.append(
                        f"{kind} '{item.get('id', '?')}': '{key}' must be a string or a "
                        f"number, got {type(value).__name__}"
                    )
    for task in tasks:
        if "enabled" in task and not isinstance(task["enabled"], bool):
            errors.append(
                f"Task '{task.get('id', '?')}': 'enabled' must be true or false, "
                f"got {type(task['enabled']).__name__}"
            )
    return errors


def find_unreachable_phases(phases: list[dict[str, Any]]) -> list[str]:
    """Return ids of phases not reachable from the entry phase (the one no
    transition goes to) by following next[].goto edges. WARNING-level only -- never blocks a
    write. Returns ids in the order they appear in ``phases``.
    """
    if not phases or _shape_errors(phases, []):
        return []
    # The entry is the phase no transition goes to, as the job queue starts
    # it -- not the first one listed. No single entry is an error of its own.
    roots = first_phase_ids(phases)
    if len(roots) != 1:
        return []
    by_id = {normalize_id(p.get("id")): p for p in phases}
    entry = roots[0]
    reachable: set[str] = set()
    queue: deque[str] = deque([entry])
    while queue:
        pid = queue.popleft()
        if pid in reachable or pid not in by_id:
            continue
        reachable.add(pid)
        for transition in by_id[pid].get("next", []):
            goto = transition.get("goto")
            if goto is not None:
                queue.append(normalize_id(goto))
    return [normalize_id(p.get("id")) for p in phases if normalize_id(p.get("id")) not in reachable]
