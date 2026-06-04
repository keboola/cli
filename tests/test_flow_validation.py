"""Unit tests for conditional-flow validation (services/flow_validation.py).

Pure functions, no HTTP, no ConfigStore. The structural JSON Schema is no
longer bundled -- it is passed in explicitly (the service fetches it live from
the stack). These tests supply a compact representative Draft7 schema.
"""

from __future__ import annotations

from keboola_agent_cli.services.flow_validation import (
    find_unreachable_phases,
    validate_conditional_flow,
)

# A compact representative conditional-flow JSON Schema. It exercises the
# structural constraints the tests care about (ids are strings, task.type is an
# enum) without reproducing the full upstream schema -- which now lives on the
# stack and is fetched at runtime, never bundled.
_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["phases", "tasks"],
    "properties": {
        "phases": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id"],
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "next": {"type": "array"},
                },
            },
        },
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "phase", "task"],
                "properties": {
                    "id": {"type": "string"},
                    "phase": {"type": "string"},
                    "enabled": {"type": "boolean"},
                    "task": {
                        "type": "object",
                        "required": ["type"],
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["job", "notification", "variable"],
                            }
                        },
                    },
                },
            },
        },
    },
}


def _valid_phases():
    return [
        {
            "id": "extract",
            "name": "Extract",
            "next": [
                {
                    "id": "t1",
                    "goto": "transform",
                    "condition": {
                        "type": "operator",
                        "operator": "ANY_TASKS_IN_PHASE",
                        "phase": "extract",
                        "operands": [],
                    },
                },
                {"id": "t2", "goto": None},  # default transition (no condition)
            ],
        },
        {"id": "transform", "name": "Transform"},
    ]


def _valid_tasks():
    return [
        {
            "id": "task-1",
            "name": "Run extractor",
            "phase": "extract",
            "enabled": True,
            "task": {
                "type": "job",
                "componentId": "keboola.ex-http",
                "configId": "123",
                "mode": "run",
            },
        },
        {
            "id": "task-2",
            "name": "Run transform",
            "phase": "transform",
            "task": {
                "type": "job",
                "componentId": "keboola.snowflake-transformation",
                "configId": "456",
                "mode": "run",
            },
        },
    ]


# ── schema parameter behaviour ────────────────────────────────────────────


def test_valid_flow_has_no_errors_with_schema():
    assert validate_conditional_flow(_valid_phases(), _valid_tasks(), _SCHEMA) == []


def test_valid_flow_has_no_errors_without_schema():
    # Semantic-only path (no structural validation) -- still valid.
    assert validate_conditional_flow(_valid_phases(), _valid_tasks()) == []
    assert validate_conditional_flow(_valid_phases(), _valid_tasks(), None) == []


def test_structural_error_bad_task_type_with_schema():
    tasks = _valid_tasks()
    tasks[0]["task"]["type"] = "nonsense"
    errors = validate_conditional_flow(_valid_phases(), tasks, _SCHEMA)
    assert errors  # at least one structural error reported
    assert any("task" in e.lower() for e in errors)


def test_structural_error_not_reported_without_schema():
    # Same bad task type, but no schema => structural check is skipped.
    tasks = _valid_tasks()
    tasks[0]["task"]["type"] = "nonsense"
    errors = validate_conditional_flow(_valid_phases(), tasks, None)
    # No structural complaint about the bad type; semantic checks still pass.
    assert errors == []


# ── semantic checks (always run, schema or not) ───────────────────────────


def test_duplicate_phase_ids():
    phases = _valid_phases()
    phases[1]["id"] = "extract"  # collide with phase[0]
    errors = validate_conditional_flow(phases, _valid_tasks())
    assert any("duplicate phase id" in e.lower() for e in errors)


def test_duplicate_task_ids():
    tasks = _valid_tasks()
    tasks[1]["id"] = "task-1"
    errors = validate_conditional_flow(_valid_phases(), tasks)
    assert any("duplicate task id" in e.lower() for e in errors)


def test_task_references_missing_phase():
    tasks = _valid_tasks()
    tasks[0]["phase"] = "ghost"
    errors = validate_conditional_flow(_valid_phases(), tasks)
    assert any("ghost" in e and "phase" in e.lower() for e in errors)


def test_goto_references_missing_phase():
    phases = _valid_phases()
    phases[0]["next"][0]["goto"] = "ghost"
    errors = validate_conditional_flow(phases, _valid_tasks())
    assert any("ghost" in e and "goto" in e.lower() for e in errors)


def test_goto_null_is_allowed():
    phases = _valid_phases()
    phases[0]["next"] = [{"id": "x", "goto": None}]
    assert validate_conditional_flow(phases, _valid_tasks()) == []


def test_conditional_transitions_need_default_last():
    phases = _valid_phases()
    # remove the default (last, condition-less) transition, leaving only conditional
    phases[0]["next"] = [phases[0]["next"][0]]
    errors = validate_conditional_flow(phases, _valid_tasks())
    assert any("default" in e.lower() and "transition" in e.lower() for e in errors)


def test_phase_without_enabled_task():
    tasks = _valid_tasks()
    tasks[1]["enabled"] = False  # transform phase now has zero enabled tasks
    errors = validate_conditional_flow(_valid_phases(), tasks)
    assert any("transform" in e and "enabled task" in e.lower() for e in errors)


def _phase_with_condition(condition):
    return [
        {
            "id": "p1",
            "name": "P1",
            "next": [
                {"id": "c", "goto": "p2", "condition": condition},
                {"id": "d", "goto": None},
            ],
        },
        {"id": "p2", "name": "P2"},
    ]


def _tasks_two_phases():
    return [
        {
            "id": "a",
            "name": "A",
            "phase": "p1",
            "task": {"type": "job", "componentId": "c", "configId": "1", "mode": "run"},
        },
        {
            "id": "b",
            "name": "B",
            "phase": "p2",
            "task": {"type": "job", "componentId": "c", "configId": "2", "mode": "run"},
        },
    ]


def _const(v):
    return {"type": "const", "value": v}


def test_equals_requires_two_operands():
    cond = {"type": "operator", "operator": "EQUALS", "operands": [_const("x")]}
    errors = validate_conditional_flow(_phase_with_condition(cond), _tasks_two_phases())
    assert any("EQUALS" in e and "2 operand" in e for e in errors)


def test_and_requires_at_least_one_operand():
    cond = {"type": "operator", "operator": "AND", "operands": []}
    errors = validate_conditional_flow(_phase_with_condition(cond), _tasks_two_phases())
    assert any("AND" in e and "at least 1" in e for e in errors)


def test_function_count_requires_one_operand():
    inner = {"type": "function", "function": "COUNT", "operands": [_const("a"), _const("b")]}
    cond = {"type": "operator", "operator": "AND", "operands": [inner]}
    errors = validate_conditional_flow(_phase_with_condition(cond), _tasks_two_phases())
    assert any("COUNT" in e and "1 operand" in e for e in errors)


def test_valid_equals_two_operands_ok():
    cond = {"type": "operator", "operator": "EQUALS", "operands": [_const("x"), _const("y")]}
    assert validate_conditional_flow(_phase_with_condition(cond), _tasks_two_phases()) == []


# ── reachability (warning-level helper) ────────────────────────────────────


def test_all_phases_reachable():
    assert find_unreachable_phases(_valid_phases()) == []


def test_unreachable_phase_reported():
    phases = [
        {"id": "start", "name": "Start", "next": [{"id": "x", "goto": None}]},
        {"id": "island", "name": "Island"},  # never targeted
    ]
    assert find_unreachable_phases(phases) == ["island"]


def test_goto_loop_is_not_an_error():
    # start -> loop -> start ... legal at runtime, must NOT be flagged
    phases = [
        {"id": "start", "name": "Start", "next": [{"id": "a", "goto": "loop"}]},
        {"id": "loop", "name": "Loop", "next": [{"id": "b", "goto": "start"}]},
    ]
    assert find_unreachable_phases(phases) == []
    assert (
        validate_conditional_flow(
            phases,
            [
                {
                    "id": "t",
                    "name": "T",
                    "phase": "start",
                    "task": {"type": "job", "componentId": "c", "configId": "1", "mode": "run"},
                },
                {
                    "id": "u",
                    "name": "U",
                    "phase": "loop",
                    "task": {"type": "job", "componentId": "c", "configId": "2", "mode": "run"},
                },
            ],
        )
        == []
    )
