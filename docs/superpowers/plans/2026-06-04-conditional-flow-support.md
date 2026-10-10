# Conditional Flow (`keboola.flow`) Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite the `kbagent flow` surface to support `keboola.flow` (Conditional Flows) with correct schema-backed validation, and drop `keboola.orchestrator` support entirely, shipping as a single breaking release 0.57.0.

**Architecture:** Follows the repo's 3-layer design (commands → services → client). A new pure-function module `services/flow_validation.py` loads a bundled copy of the upstream CF JSON Schema (`resources/conditional-flow-schema.json`) and performs structural (jsonschema Draft7) + semantic validation. `FlowService` hardcodes the single component `keboola.flow`, drops every `component_id` parameter, and calls the validator on create/update. The REST router mirror drops `component_id`.

**Tech Stack:** Python 3.12, Typer, Pydantic 2.x, `jsonschema>=4.20` (already a dependency), `importlib.resources`, hatchling packaging, pytest + `typer.testing.CliRunner`.

**Design spec:** `docs/superpowers/specs/2026-06-04-conditional-flow-support-design.md`

> **Design revision (2026-06-04) — schema fetched live, not bundled.** Supersedes
> the "bundled schema" approach above (spec decision D3). The CF JSON Schema is
> **not** vendored into the wheel; it is fetched at runtime from the stack's
> component registry via `AiServiceClient` → `ComponentDetail.configuration_schema`
> (AI Service `/docs/components/keboola.flow`), the same path `config new --push`
> uses. Verified live that the stack serves the full schema. `flow_validation.py`
> stays a pure module: `validate_conditional_flow(phases, tasks, schema=None)`
> runs structural Draft7 checks only when a schema is passed; the semantic checks
> always run. `FlowService` fetches the live schema before validating and degrades
> gracefully on fetch failure (skip structural, keep semantic, surface a
> `structural schema validation skipped` warning — never block the write, since
> Storage does not validate flow configs server-side). `flow validate` gains
> `--project` (live schema → full validation; otherwise semantic-only + a note);
> `flow schema --full` now requires `--project`. The bundle
> (`resources/conditional-flow-schema.json`, `load_conditional_flow_schema()`),
> the `importlib.resources` loader, and any hatchling packaging for it are
> removed. Where tasks below say "bundled"/"vendored"/"pinned SHA", apply this
> revision instead.

**Pinned upstream schema:** `keboola/job-queue-daemon` `docs/flow-schema.json` @ commit `24176de2ec1098e0a4be278815e0ca57a93cc93d` (2026-05-26). Private repo — fetch via `gh api`.

> **CRITICAL GROUNDING NOTE — string ids.** Every id in the CF schema
> (`phase.id`, `task.id`, `next.id`, `task.phase`, `goto`) is a JSON **string**
> (`goto` is `string | null`). The original issue text assumed integer phase
> ids; that is WRONG. All templates, fixtures, and validation in this plan use
> string ids. Using ints will fail Draft7 validation.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `src/keboola_agent_cli/resources/__init__.py` | Create | Make `resources` an importable package for `importlib.resources`. |
| `src/keboola_agent_cli/resources/conditional-flow-schema.json` | Create | Verbatim copy of upstream CF JSON Schema (pinned SHA). |
| `src/keboola_agent_cli/services/flow_validation.py` | Create | Pure validation: schema loader + structural + semantic + reachability. No HTTP. |
| `src/keboola_agent_cli/errors.py` | Modify | Replace `INVALID_FLOW_DAG` with `INVALID_FLOW_DEFINITION`. |
| `src/keboola_agent_cli/services/flow_service.py` | Modify | Single component; drop `component_id` params; call new validator. |
| `src/keboola_agent_cli/commands/flow.py` | Modify | Drop `--component-id`; new CF template; `flow schema --full`; new `flow validate`; detail rewrite; legacy-count warning. |
| `src/keboola_agent_cli/permissions.py` | Modify | Add `flow.validate: read`. |
| `src/keboola_agent_cli/server/routers/flows.py` | Modify | Drop `component_id` from models + query params. |
| `src/keboola_agent_cli/sync/config_format.py` | Modify | Delete dead `ORCHESTRATOR_COMPONENTS`. |
| `src/keboola_agent_cli/services/component_service.py` | Modify | CF scaffold for `config new`; drop orchestrator default. |
| `src/keboola_agent_cli/commands/context.py` | Modify | Update `AGENT_CONTEXT` flow section. |
| `tests/test_flow_validation.py` | Create | Validator unit tests. |
| `tests/test_flow_service.py` | Rewrite | CF service tests; remove `dependsOn`. |
| `tests/test_flow_cli.py` | Rewrite | CF CLI tests; `validate`, `schema --full`. |
| `tests/test_e2e.py` | Modify | CF round-trip + `flow validate`; skip if CF disabled. |
| `CLAUDE.md`, `pyproject.toml`, `changelog.py`, plugin docs | Modify | Docs/version/changelog sync. |

---

## Phase 1 — Bundle the schema

### Task 1: Vendor the upstream CF schema into the package

**Files:**
- Create: `src/keboola_agent_cli/resources/__init__.py`
- Create: `src/keboola_agent_cli/resources/conditional-flow-schema.json`

- [ ] **Step 1: Create the resources package marker**

Create `src/keboola_agent_cli/resources/__init__.py` with a single docstring line:

```python
"""Bundled static resources (JSON schemas) shipped inside the wheel."""
```

- [ ] **Step 2: Fetch the pinned schema verbatim**

Run (the repo is private; `gh` has credentials):

```bash
gh api "repos/keboola/job-queue-daemon/contents/docs/flow-schema.json?ref=24176de2ec1098e0a4be278815e0ca57a93cc93d" \
  --jq '.content' | base64 -d > src/keboola_agent_cli/resources/conditional-flow-schema.json
```

Expected: a ~21 KB JSON file whose top-level keys are `$schema`, `type`,
`required`, `description`, `properties` (`phases`, `tasks`), `definitions`.

- [ ] **Step 3: Verify it parses and has the expected shape**

Run:

```bash
python3 -c "import json; s=json.load(open('src/keboola_agent_cli/resources/conditional-flow-schema.json')); assert s['required']==['phases','tasks']; assert s['properties']['phases']['items']['properties']['id']['type']=='string'; print('OK', len(open('src/keboola_agent_cli/resources/conditional-flow-schema.json').read()), 'bytes')"
```

Expected: `OK <bytes> bytes`

- [ ] **Step 4: Commit**

```bash
git add src/keboola_agent_cli/resources/
git commit -m "feat(flow): vendor conditional-flow JSON schema (pinned SHA 24176de)"
```

---

### Task 2: Schema loader (`flow_validation.load_conditional_flow_schema`)

**Files:**
- Create: `src/keboola_agent_cli/services/flow_validation.py`
- Test: `tests/test_flow_validation.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_flow_validation.py`:

```python
"""Unit tests for conditional-flow validation (services/flow_validation.py).

Pure functions, no HTTP, no ConfigStore.
"""

from __future__ import annotations

from keboola_agent_cli.services.flow_validation import load_conditional_flow_schema


def test_load_schema_ships_in_package():
    schema = load_conditional_flow_schema()
    assert schema["required"] == ["phases", "tasks"]
    # ids are strings, not integers (grounding guard)
    assert schema["properties"]["phases"]["items"]["properties"]["id"]["type"] == "string"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_flow_validation.py::test_load_schema_ships_in_package -v`
Expected: FAIL — `ModuleNotFoundError: keboola_agent_cli.services.flow_validation`

- [ ] **Step 3: Write the loader**

Create `src/keboola_agent_cli/services/flow_validation.py`:

```python
"""Conditional-flow (keboola.flow) validation.

Pure functions: no HTTP, no ConfigStore -- trivially unit-testable.

Schema source of truth: keboola/job-queue-daemon docs/flow-schema.json
Pinned commit SHA: 24176de2ec1098e0a4be278815e0ca57a93cc93d (2026-05-26).
The bundled copy lives in keboola_agent_cli/resources/conditional-flow-schema.json.
When the upstream schema changes, re-vendor the file and bump the SHA above and
in references/gotchas.md (no CI freshness check in v1 -- upstream repo is private).
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any


@lru_cache(maxsize=1)
def load_conditional_flow_schema() -> dict[str, Any]:
    """Load the bundled conditional-flow JSON Schema (draft-07)."""
    text = (
        resources.files("keboola_agent_cli.resources")
        .joinpath("conditional-flow-schema.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(text)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_flow_validation.py::test_load_schema_ships_in_package -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/keboola_agent_cli/services/flow_validation.py tests/test_flow_validation.py
git commit -m "feat(flow): add conditional-flow schema loader"
```

---

### Task 3: Verify the JSON ships in the built wheel

**Files:** none (packaging verification only)

- [ ] **Step 1: Build the wheel**

Run:

```bash
uv build --wheel 2>&1 | tail -5
```

Expected: a `dist/keboola_cli-0.55.0-*.whl` (version bumps later).

- [ ] **Step 2: Assert the schema is inside the wheel**

Run:

```bash
unzip -l dist/*.whl | grep conditional-flow-schema.json
```

Expected: one line listing `keboola_agent_cli/resources/conditional-flow-schema.json`.
If MISSING: the file lives inside the package tree and is not gitignored, so the
hatchling default wheel target (`packages = ["src/keboola_agent_cli"]`) should
include it. If it is absent, add a `force-include` entry in `pyproject.toml`
under `[tool.hatch.build.targets.wheel.force-include]`:
`"src/keboola_agent_cli/resources" = "keboola_agent_cli/resources"`, rebuild,
and re-verify. (The unit test in Task 2 already guards the runtime path.)

- [ ] **Step 3: Clean up build artifacts**

Run: `rm -rf dist build`

---

## Phase 2 — Error code

### Task 4: Replace `INVALID_FLOW_DAG` with `INVALID_FLOW_DEFINITION`

**Files:**
- Modify: `src/keboola_agent_cli/errors.py:99-101`

- [ ] **Step 1: Confirm there are no external wire consumers**

Run:

```bash
grep -rn "INVALID_FLOW_DAG" src/ tests/ plugins/ docs/
```

Expected: references only in `errors.py`, `services/flow_service.py`,
`changelog.py` (historical), `tests/test_flow_service.py`, and docs — all
in-repo. These are all updated by later tasks.

- [ ] **Step 2: Edit the enum**

In `src/keboola_agent_cli/errors.py`, replace:

```python
    # Flow (new in 0.22.0)
    INVALID_FLOW_DAG = "INVALID_FLOW_DAG"
    SCHEDULE_DELETE_FAILED = "SCHEDULE_DELETE_FAILED"
```

with:

```python
    # Flow (new in 0.22.0)
    SCHEDULE_DELETE_FAILED = "SCHEDULE_DELETE_FAILED"
    # Conditional-flow validation (replaces INVALID_FLOW_DAG; since 0.57.0)
    INVALID_FLOW_DEFINITION = "INVALID_FLOW_DEFINITION"
```

- [ ] **Step 3: Verify import resolves**

Run: `uv run python -c "from keboola_agent_cli.errors import ErrorCode; print(ErrorCode.INVALID_FLOW_DEFINITION)"`
Expected: `INVALID_FLOW_DEFINITION`

- [ ] **Step 4: Commit**

```bash
git add src/keboola_agent_cli/errors.py
git commit -m "feat(flow): add INVALID_FLOW_DEFINITION error code (replaces INVALID_FLOW_DAG)"
```

---

## Phase 3 — Validation logic (TDD)

> All tasks in this phase add tests + code to `tests/test_flow_validation.py` and
> `src/keboola_agent_cli/services/flow_validation.py`. Define a shared valid
> fixture first (Task 5), then layer one rule per task.

### Task 5: Valid CF fixture + structural validation entrypoint

**Files:**
- Modify: `src/keboola_agent_cli/services/flow_validation.py`
- Test: `tests/test_flow_validation.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_flow_validation.py`:

```python
from keboola_agent_cli.services.flow_validation import validate_conditional_flow


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


def test_valid_flow_has_no_errors():
    assert validate_conditional_flow(_valid_phases(), _valid_tasks()) == []


def test_structural_error_bad_task_type():
    tasks = _valid_tasks()
    tasks[0]["task"]["type"] = "nonsense"
    errors = validate_conditional_flow(_valid_phases(), tasks)
    assert errors  # at least one structural error reported
    assert any("task" in e.lower() for e in errors)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_flow_validation.py -k "valid_flow or structural_error_bad_task" -v`
Expected: FAIL — `validate_conditional_flow` not defined.

- [ ] **Step 3: Implement structural validation + semantic dispatch shell**

Append to `src/keboola_agent_cli/services/flow_validation.py`:

```python
import jsonschema


def _structural_errors(phases: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> list[str]:
    """Run Draft7 validation against the bundled schema, collecting ALL errors."""
    schema = load_conditional_flow_schema()
    document = {"phases": phases, "tasks": tasks}
    validator = jsonschema.Draft7Validator(schema)
    errors: list[str] = []
    for err in sorted(validator.iter_errors(document), key=lambda e: list(e.path)):
        path = "/".join(str(p) for p in err.path) or "(root)"
        errors.append(f"Schema error at {path}: {err.message}")
    return errors


def validate_conditional_flow(
    phases: list[dict[str, Any]], tasks: list[dict[str, Any]]
) -> list[str]:
    """Validate a conditional-flow body. Returns a flat list of error strings
    (empty == valid). Reachability is computed separately as a warning -- call
    ``find_unreachable_phases`` for that. NO cycle detection: goto loops are
    legal at runtime.

    Structural (Draft7) errors are returned first; semantic checks only run when
    the structure is sound (avoids cascade noise from a malformed document).
    """
    structural = _structural_errors(phases, tasks)
    if structural:
        return structural
    return _semantic_errors(phases, tasks)


def _semantic_errors(phases: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> list[str]:
    """Placeholder; rules added incrementally in later tasks."""
    return []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_flow_validation.py -k "valid_flow or structural_error_bad_task" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/keboola_agent_cli/services/flow_validation.py tests/test_flow_validation.py
git commit -m "feat(flow): structural Draft7 validation for conditional flows"
```

---

### Task 6: Semantic — unique ids, task→phase refs, goto refs

**Files:**
- Modify: `src/keboola_agent_cli/services/flow_validation.py` (`_semantic_errors`)
- Test: `tests/test_flow_validation.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_flow_validation.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_flow_validation.py -k "duplicate or missing_phase or goto" -v`
Expected: FAIL — `_semantic_errors` returns `[]`.

- [ ] **Step 3: Implement the rules**

Replace `_semantic_errors` in `src/keboola_agent_cli/services/flow_validation.py`:

```python
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

    return errors
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_flow_validation.py -k "duplicate or missing_phase or goto" -v`
Expected: PASS (all 5)

- [ ] **Step 5: Commit**

```bash
git add src/keboola_agent_cli/services/flow_validation.py tests/test_flow_validation.py
git commit -m "feat(flow): semantic id/reference validation"
```

---

### Task 7: Semantic — default transition + enabled-task-per-phase

**Files:**
- Modify: `src/keboola_agent_cli/services/flow_validation.py` (`_semantic_errors`)
- Test: `tests/test_flow_validation.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_flow_validation.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_flow_validation.py -k "default_last or without_enabled" -v`
Expected: FAIL

- [ ] **Step 3: Implement the rules**

Append inside `_semantic_errors` (before `return errors`):

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_flow_validation.py -k "default_last or without_enabled" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/keboola_agent_cli/services/flow_validation.py tests/test_flow_validation.py
git commit -m "feat(flow): default-transition + enabled-task semantic checks"
```

---

### Task 8: Semantic — operator/function operand arity

**Files:**
- Modify: `src/keboola_agent_cli/services/flow_validation.py`
- Test: `tests/test_flow_validation.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_flow_validation.py`:

```python
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
    cond = {"type": "function", "function": "COUNT", "operands": [_const("a"), _const("b")]}
    errors = validate_conditional_flow(_phase_with_condition(cond), _tasks_two_phases())
    assert any("COUNT" in e and "1 operand" in e for e in errors)


def test_valid_equals_two_operands_ok():
    cond = {"type": "operator", "operator": "EQUALS", "operands": [_const("x"), _const("y")]}
    assert validate_conditional_flow(_phase_with_condition(cond), _tasks_two_phases()) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_flow_validation.py -k "operand or two_operands or one_operand or at_least_one" -v`
Expected: FAIL

- [ ] **Step 3: Implement arity checks**

Append to `src/keboola_agent_cli/services/flow_validation.py` (module-level
constants + a walker), and call it from `_semantic_errors`:

```python
# Operand arity per operator (semantic; the schema cannot express these counts).
_BINARY_OPERATORS = frozenset(
    {"EQUALS", "NOT_EQUALS", "GREATER_THAN", "LESS_THAN", "INCLUDES", "CONTAINS"}
)
_VARIADIC_MIN1_OPERATORS = frozenset({"AND", "OR"})
_PHASE_SCOPED_OPERATORS = frozenset({"ALL_TASKS_IN_PHASE", "ANY_TASKS_IN_PHASE"})
_UNARY_FUNCTIONS = frozenset({"COUNT", "DATE"})


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
```

Then add inside `_semantic_errors` (before `return errors`):

```python
    # Condition operand arity (recursive).
    for phase in phases:
        for transition in phase.get("next", []):
            cond = transition.get("condition")
            if cond is not None:
                errors.extend(_condition_arity_errors(cond))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_flow_validation.py -k "operand or two_operands or one_operand or at_least_one" -v`
Expected: PASS (all 4)

- [ ] **Step 5: Commit**

```bash
git add src/keboola_agent_cli/services/flow_validation.py tests/test_flow_validation.py
git commit -m "feat(flow): condition operand-arity validation"
```

---

### Task 9: Reachability warnings (`find_unreachable_phases`)

**Files:**
- Modify: `src/keboola_agent_cli/services/flow_validation.py`
- Test: `tests/test_flow_validation.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_flow_validation.py`:

```python
from keboola_agent_cli.services.flow_validation import find_unreachable_phases


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_flow_validation.py -k "reachable or unreachable or goto_loop" -v`
Expected: FAIL — `find_unreachable_phases` not defined.

- [ ] **Step 3: Implement reachability (forward BFS from first phase)**

Append to `src/keboola_agent_cli/services/flow_validation.py`:

```python
from collections import deque


def find_unreachable_phases(phases: list[dict[str, Any]]) -> list[str]:
    """Return ids of phases not reachable from the entry phase (first in the
    list) by following next[].goto edges. WARNING-level only -- never blocks a
    write. Returns ids in the order they appear in ``phases``.
    """
    if not phases:
        return []
    by_id = {str(p.get("id")): p for p in phases}
    entry = str(phases[0].get("id"))
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
                queue.append(str(goto))
    return [str(p.get("id")) for p in phases if str(p.get("id")) not in reachable]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_flow_validation.py -k "reachable or unreachable or goto_loop" -v`
Expected: PASS (all 3)

- [ ] **Step 5: Run the whole validation suite**

Run: `uv run pytest tests/test_flow_validation.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/keboola_agent_cli/services/flow_validation.py tests/test_flow_validation.py
git commit -m "feat(flow): unreachable-phase reachability warnings (no cycle detection)"
```

---

## Phase 4 — Service layer

### Task 10: Rewrite `FlowService` to single-component + new validation

**Files:**
- Modify: `src/keboola_agent_cli/services/flow_service.py`
- Test: `tests/test_flow_service.py` (rewrite)

> This task replaces `_validate_dag`, removes all `component_id` params, hardcodes
> `FLOW_COMPONENT_ID`, adds `legacy_orchestrator_count` to `list_flows`, and wires
> in `validate_conditional_flow` + `find_unreachable_phases`.

- [ ] **Step 1: Rewrite the service test file (failing)**

Replace `tests/test_flow_service.py` entirely. Key tests (full file — keep the
`_mock_config_store` / `_make_flow_service` helpers from the existing file, which
do NOT change):

```python
"""Unit tests for FlowService (conditional flows only)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.services.flow_service import FLOW_COMPONENT_ID, FlowService


def _mock_config_store(projects: dict) -> MagicMock:
    cs = MagicMock()
    config = MagicMock()
    config.projects = {
        alias: MagicMock(stack_url=v["url"], token=v["token"], active_branch_id=None)
        for alias, v in projects.items()
    }
    config.max_parallel_workers = 10
    cs.load.return_value = config
    cs.get_project.side_effect = lambda alias: config.projects.get(alias)
    return cs


def _make_flow_service(mock_client: MagicMock, projects: dict | None = None) -> FlowService:
    if projects is None:
        projects = {"prod": {"url": "https://connection.keboola.com", "token": "tok"}}
    cs = _mock_config_store(projects)
    return FlowService(config_store=cs, client_factory=lambda url, token: mock_client)


def _valid_body():
    phases = [
        {"id": "p1", "name": "P1", "next": [{"id": "n", "goto": None}]},
    ]
    tasks = [
        {
            "id": "t1",
            "name": "T1",
            "phase": "p1",
            "task": {"type": "job", "componentId": "c", "configId": "1", "mode": "run"},
        },
    ]
    return phases, tasks


def test_component_id_constant():
    assert FLOW_COMPONENT_ID == "keboola.flow"


def test_create_flow_rejects_invalid_definition():
    client = MagicMock()
    svc = _make_flow_service(client)
    # task references a phase that does not exist -> semantic error
    phases = [{"id": "p1", "name": "P1", "next": [{"id": "n", "goto": None}]}]
    tasks = [
        {
            "id": "t1",
            "name": "T1",
            "phase": "ghost",
            "task": {"type": "job", "componentId": "c", "configId": "1", "mode": "run"},
        }
    ]
    with pytest.raises(KeboolaApiError) as exc:
        svc.create_flow(alias="prod", name="F", phases=phases, tasks=tasks)
    assert exc.value.error_code == ErrorCode.INVALID_FLOW_DEFINITION


def test_create_flow_uses_keboola_flow_component():
    client = MagicMock()
    client.create_config.return_value = {"id": "999", "name": "F"}
    svc = _make_flow_service(client)
    phases, tasks = _valid_body()
    result = svc.create_flow(alias="prod", name="F", phases=phases, tasks=tasks)
    assert client.create_config.call_args.kwargs["component_id"] == "keboola.flow"
    assert result["id"] == "999"


def test_list_flows_reports_legacy_orchestrator_count():
    client = MagicMock()

    def list_configs(component_id, branch_id=None):
        if component_id == "keboola.flow":
            return [{"id": "1", "name": "CF"}]
        if component_id == "keboola.orchestrator":
            return [{"id": "9", "name": "Old"}, {"id": "10", "name": "Old2"}]
        return []

    client.list_component_configs.side_effect = list_configs
    svc = _make_flow_service(client)
    result = svc.list_flows(aliases=["prod"])
    assert result["legacy_orchestrator_count"] == 2
    assert all(f["component_id"] == "keboola.flow" for f in result["flows"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_flow_service.py -v`
Expected: FAIL — `FLOW_COMPONENT_ID` not importable / signatures still take `component_id`.

- [ ] **Step 3: Edit `flow_service.py` — constants + imports + delete `_validate_dag`**

In `src/keboola_agent_cli/services/flow_service.py`:

Replace the import block and the `FLOW_COMPONENT_IDS` line:

```python
from ..errors import ErrorCode, KeboolaApiError
from ..models import ProjectConfig
from .base import BaseService
from .flow_validation import find_unreachable_phases, validate_conditional_flow
```

```python
FLOW_COMPONENT_ID = "keboola.flow"
LEGACY_FLOW_COMPONENT_ID = "keboola.orchestrator"
SCHEDULER_COMPONENT_ID = "keboola.scheduler"
```

Delete the entire `_validate_dag` function (services/flow_service.py:47-96).

- [ ] **Step 4: Edit `list_flows` — single component + legacy count**

Replace the `worker` body's flow-collection loop and the return assembly. The
worker now lists ONLY `keboola.flow`, plus a separate count of
`keboola.orchestrator` configs:

```python
def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
    client = self._client_factory(project.stack_url, project.token)
    effective_branch = branch_id or project.active_branch_id
    try:
        flows: list[dict[str, Any]] = []
        try:
            configs = client.list_component_configs(FLOW_COMPONENT_ID, branch_id=effective_branch)
        except KeboolaApiError as exc:
            if exc.error_code == "NOT_FOUND":
                configs = []
            else:
                raise
        for cfg in configs:
            flow_row: dict[str, Any] = {
                "project_alias": alias,
                "component_id": FLOW_COMPONENT_ID,
                "config_id": str(cfg.get("id", "")),
                "name": cfg.get("name", ""),
                "description": cfg.get("description", ""),
                "is_disabled": cfg.get("isDisabled", False),
            }
            if with_schedules:
                flow_row["schedules"] = []
            flows.append(flow_row)

        # Count (do not list) legacy orchestrator configs so the CLI can warn.
        try:
            legacy = client.list_component_configs(
                LEGACY_FLOW_COMPONENT_ID, branch_id=effective_branch
            )
            legacy_count = len(legacy)
        except KeboolaApiError as exc:
            if exc.error_code == "NOT_FOUND":
                legacy_count = 0
            else:
                raise

        if with_schedules and flows:
            schedules_by_parent = _collect_schedules_by_parent(client, effective_branch)
            for flow_row in flows:
                key = (flow_row["component_id"], flow_row["config_id"])
                flow_row["schedules"] = schedules_by_parent.get(key, [])

        return (alias, flows, legacy_count)
    except KeboolaApiError as exc:
        return (
            alias,
            {"project_alias": alias, "error_code": exc.error_code, "message": exc.message},
        )
    except Exception as exc:
        return (
            alias,
            {"project_alias": alias, "error_code": "UNEXPECTED_ERROR", "message": str(exc)},
        )
    finally:
        client.close()


successes, errors = self._run_parallel(projects, worker)

all_flows: list[dict[str, Any]] = []
legacy_total = 0
for _, flows, legacy_count in successes:
    all_flows.extend(flows)
    legacy_total += legacy_count
all_flows.sort(key=lambda f: (f["project_alias"], f["name"].lower()))
errors.sort(key=lambda e: e.get("project_alias", ""))

return {
    "flows": all_flows,
    "errors": errors,
    "legacy_orchestrator_count": legacy_total,
}
```

Update the `list_flows` docstring Returns section to mention
`legacy_orchestrator_count` and that only `keboola.flow` rows are returned.

- [ ] **Step 5: Edit the remaining methods — drop `component_id`, hardcode**

For each method below, remove the `component_id` parameter and replace every use
of `component_id` with `FLOW_COMPONENT_ID`:

- `get_flow_detail(self, alias, config_id, branch_id=None)` — call
  `client.get_config_detail(FLOW_COMPONENT_ID, config_id, ...)`; set
  `detail["component_id"] = FLOW_COMPONENT_ID`.
- `create_flow(self, alias, name, description="", phases=None, tasks=None, branch_id=None)`:
  replace the `if phases: dag_errors = _validate_dag(...)` block with:

  ```python
  phases = phases or []
  tasks = tasks or []

  definition_errors = validate_conditional_flow(phases, tasks)
  if definition_errors:
      raise KeboolaApiError(
          message="Flow definition is invalid: " + "; ".join(definition_errors),
          status_code=400,
          error_code=ErrorCode.INVALID_FLOW_DEFINITION,
          retryable=False,
      )
  warnings = [
      f"Phase '{pid}' is unreachable from the entry phase" for pid in find_unreachable_phases(phases)
  ]

  configuration: dict[str, Any] = {"phases": phases, "tasks": tasks}
  ```

  Call `client.create_config(component_id=FLOW_COMPONENT_ID, ...)`; add
  `result["warnings"] = warnings` before returning.

- `update_flow(self, alias, config_id, name=None, description=None, phases=None, tasks=None, branch_id=None)`:
  use `FLOW_COMPONENT_ID` in `get_config_detail` / `update_config`. Replace the
  `_validate_dag` block with validation on the merged body:

  ```python
              merged_phases = phases if phases is not None else current_body.get("phases", [])
              merged_tasks = tasks if tasks is not None else current_body.get("tasks", [])
              definition_errors = validate_conditional_flow(merged_phases, merged_tasks)
              if definition_errors:
                  raise KeboolaApiError(
                      message="Flow definition is invalid: " + "; ".join(definition_errors),
                      status_code=400,
                      error_code=ErrorCode.INVALID_FLOW_DEFINITION,
                      retryable=False,
                  )
              configuration = dict(current_body)
              configuration["phases"] = merged_phases
              configuration["tasks"] = merged_tasks
  ```

- `delete_flow(self, alias, config_id, branch_id=None)` — `FLOW_COMPONENT_ID` in
  `delete_config`; set `"component_id": FLOW_COMPONENT_ID` in the return dict.
- `list_flow_schedules(self, alias, config_id, branch_id=None)` — filter
  `target.componentId == FLOW_COMPONENT_ID`; return `"component_id": FLOW_COMPONENT_ID`.
- `set_flow_schedule(self, alias, config_id, cron_tab, timezone="UTC", enabled=True, schedule_name=None, branch_id=None)`
  — `get_config_detail(FLOW_COMPONENT_ID, ...)` for the name; the scheduler
  `target.componentId` is `FLOW_COMPONENT_ID`; match existing by
  `FLOW_COMPONENT_ID`; return `"component_id": FLOW_COMPONENT_ID`.
- `remove_flow_schedule(self, alias, config_id, branch_id=None)` — filter by
  `FLOW_COMPONENT_ID`; return `"component_id": FLOW_COMPONENT_ID`.

- [ ] **Step 6: Run the service tests**

Run: `uv run pytest tests/test_flow_service.py -v`
Expected: PASS (all). If any reference `_validate_dag` or `_count_phases_tasks`,
keep `_count_phases_tasks` (still used) and ensure no test imports `_validate_dag`.

- [ ] **Step 7: Commit**

```bash
git add src/keboola_agent_cli/services/flow_service.py tests/test_flow_service.py
git commit -m "feat(flow): single-component FlowService with conditional-flow validation"
```

---

## Phase 5 — CLI layer

### Task 11: CF template + `flow schema --full`

**Files:**
- Modify: `src/keboola_agent_cli/commands/flow.py:30-85` (`_FLOW_SCHEMA`, `_FLOW_COMPONENT_CHOICES`), `flow_schema` command
- Test: `tests/test_flow_cli.py`

- [ ] **Step 1: Write the failing CLI tests**

In the rewritten `tests/test_flow_cli.py` (see Task 12 for the file header /
runner fixture), add:

```python
def test_flow_schema_default_is_conditional_template(runner, app):
    result = runner.invoke(app, ["flow", "schema"])
    assert result.exit_code == 0
    assert "next:" in result.stdout
    assert "goto" in result.stdout
    assert "dependsOn" not in result.stdout  # legacy template gone


def test_flow_schema_full_dumps_json_schema(runner, app):
    result = runner.invoke(app, ["flow", "schema", "--full"])
    assert result.exit_code == 0
    assert "$schema" in result.stdout or "draft-07" in result.stdout


def test_flow_schema_full_json_mode(runner, app):
    result = runner.invoke(app, ["--json", "flow", "schema", "--full"])
    assert result.exit_code == 0
    import json as _json

    payload = _json.loads(result.stdout)
    assert payload["schema"]["required"] == ["phases", "tasks"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_flow_cli.py -k "schema" -v`
Expected: FAIL.

- [ ] **Step 3: Replace the template and `flow schema` command**

In `src/keboola_agent_cli/commands/flow.py`:

Delete `_FLOW_COMPONENT_CHOICES = [...]`.

Replace `_FLOW_SCHEMA` with the CF template (string ids):

```python
_FLOW_SCHEMA = """\
# kbagent flow schema -- keboola.flow (Conditional Flow) configuration format
#
# Create with: kbagent flow new --project ALIAS --name "My Flow" --file @flow.yaml
# Update with: kbagent flow update --project ALIAS --flow-id ID --file @flow.yaml
# Validate offline: kbagent flow validate --file @flow.yaml
# Full JSON schema: kbagent flow schema --full
#
# IDs are STRINGS. goto is a phase id or null (= end the flow).

phases:
  - id: "extract"
    name: "Extract"
    next:
      # Conditional transition: if any task in 'extract' failed, go to 'notify'.
      - id: "on-failure"
        goto: "notify"
        condition:
          type: operator
          operator: ANY_TASKS_IN_PHASE
          phase: "extract"
          operands: []
      # Default transition (NO condition) -- MUST be last.
      - id: "default"
        goto: "transform"
  - id: "transform"
    name: "Transform"
    retry:
      strategy: linear
      strategyParams:
        delaySeconds: 60
      retryOn: ["error"]
    next:
      - id: "done"
        goto: null
  - id: "notify"
    name: "Notify on failure"

tasks:
  - id: "task-extract"
    name: "Run HTTP extractor"
    phase: "extract"
    enabled: true
    task:
      type: job
      componentId: "keboola.ex-http"
      configId: "123456789"
      mode: run
      retry:
        strategy: linear
        strategyParams:
          delaySeconds: 30
        retryOn: ["error"]
  - id: "task-transform"
    name: "Run transformation"
    phase: "transform"
    enabled: true
    task:
      type: job
      componentId: "keboola.snowflake-transformation"
      configId: "987654321"
      mode: run
  - id: "task-notify"
    name: "Email the team"
    phase: "notify"
    enabled: true
    task:
      type: notification
      title: "Flow failed"
      message: "The extract phase reported a failure."
      recipients:
        - channel: email
          address: "team@example.com"
  - id: "task-setvar"
    name: "Set a flow variable"
    phase: "extract"
    enabled: true
    task:
      type: variable
      name: "run_date"
      value: "2026-01-01"
"""
```

Replace the `flow_schema` command:

```python
@flow_app.command("schema")
def flow_schema(
    ctx: typer.Context,
    full: bool = typer.Option(
        False, "--full", help="Dump the full bundled JSON Schema verbatim instead of the template."
    ),
) -> None:
    """Print the conditional-flow YAML template, or --full for the JSON Schema."""
    formatter = get_formatter(ctx)
    if full:
        from ..services.flow_validation import load_conditional_flow_schema

        schema = load_conditional_flow_schema()
        if formatter.json_mode:
            formatter.output({"format": "json-schema", "schema": schema})
        else:
            import json as _json

            from rich.syntax import Syntax

            formatter.console.print(
                Syntax(_json.dumps(schema, indent=2), "json", theme="monokai", line_numbers=False)
            )
        return

    if formatter.json_mode:
        formatter.output(
            {
                "format": "yaml",
                "description": "keboola.flow (Conditional Flow) configuration schema",
                "schema": _FLOW_SCHEMA,
            }
        )
    else:
        from rich.syntax import Syntax

        formatter.console.print(Syntax(_FLOW_SCHEMA, "yaml", theme="monokai", line_numbers=False))
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_flow_cli.py -k "schema" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/keboola_agent_cli/commands/flow.py tests/test_flow_cli.py
git commit -m "feat(flow): conditional-flow template + 'flow schema --full'"
```

---

### Task 12: `flow validate` command + drop `--component-id` everywhere

**Files:**
- Modify: `src/keboola_agent_cli/commands/flow.py` (all 8 subcommands + new `validate`)
- Modify: `src/keboola_agent_cli/permissions.py:278-286`
- Test: `tests/test_flow_cli.py` (rewrite)

- [ ] **Step 1: Write the rewritten CLI test file header + validate tests (failing)**

Replace `tests/test_flow_cli.py`. File header / fixtures:

```python
"""CLI tests for the flow command group (conditional flows)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app as _app


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def app():
    return _app
```

Add validate tests (offline, no project needed):

```python
_VALID_FLOW_YAML = """
phases:
  - id: "p1"
    name: "P1"
    next:
      - id: "n"
        goto: null
tasks:
  - id: "t1"
    name: "T1"
    phase: "p1"
    enabled: true
    task:
      type: job
      componentId: "keboola.ex-http"
      configId: "1"
      mode: run
"""


def test_flow_validate_valid(runner, app, tmp_path):
    f = tmp_path / "flow.yaml"
    f.write_text(_VALID_FLOW_YAML)
    result = runner.invoke(app, ["flow", "validate", "--file", f"@{f}"])
    assert result.exit_code == 0


def test_flow_validate_invalid_exit_2(runner, app, tmp_path):
    bad = _VALID_FLOW_YAML.replace('phase: "p1"', 'phase: "ghost"')
    f = tmp_path / "bad.yaml"
    f.write_text(bad)
    result = runner.invoke(app, ["--json", "flow", "validate", "--file", f"@{f}"])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["valid"] is False
    assert payload["errors"]


def test_flow_validate_json_valid_lists_warnings(runner, app, tmp_path):
    f = tmp_path / "flow.yaml"
    f.write_text(_VALID_FLOW_YAML)
    result = runner.invoke(app, ["--json", "flow", "validate", "--file", f"@{f}"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["errors"] == []
    assert "warnings" in payload
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_flow_cli.py -k "validate" -v`
Expected: FAIL — no `validate` command.

- [ ] **Step 3: Add the `flow validate` command**

In `src/keboola_agent_cli/commands/flow.py`, add after `flow_schema`:

```python
@flow_app.command("validate")
def flow_validate(
    ctx: typer.Context,
    file: str = typer.Option(
        ...,
        "--file",
        help="YAML/JSON flow definition to validate (@file, -, or inline). Offline -- no API call.",
    ),
) -> None:
    """Validate a conditional-flow definition offline (schema + semantic checks).

    Exit 0 when valid (warnings still printed), exit 2 when there are errors.
    """
    formatter = get_formatter(ctx)
    from ..services.flow_validation import find_unreachable_phases, validate_conditional_flow

    try:
        flow_def = _load_flow_yaml(file)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        formatter.error(
            message=f"Cannot load flow definition: {exc}", error_code=ErrorCode.VALIDATION_ERROR
        )
        raise typer.Exit(code=2) from None

    phases = flow_def.get("phases", [])
    tasks = flow_def.get("tasks", [])
    errors = validate_conditional_flow(phases, tasks)
    warnings = [
        f"Phase '{pid}' is unreachable from the entry phase"
        for pid in find_unreachable_phases(phases)
    ]
    valid = not errors

    if formatter.json_mode:
        formatter.output({"valid": valid, "errors": errors, "warnings": warnings})
    else:
        for w in warnings:
            formatter.warning(w)
        if valid:
            formatter.success("Flow definition is valid.")
        else:
            for e in errors:
                formatter.console.print(f"[red]✗[/red] {escape(e)}")
    if not valid:
        raise typer.Exit(code=2)
```

- [ ] **Step 4: Remove `--component-id` from all 8 subcommands**

Delete the `component_id: str = typer.Option(...)` parameter from `flow_detail`,
`flow_new`, `flow_update`, `flow_delete`, `flow_schedule`, `flow_schedule_remove`
(`flow_list` and `flow_schema` never had it). Update each service call to drop
the `component_id=` kwarg. Replace human-output strings that interpolate
`component_id` with the literal `keboola.flow` (or just the flow id). Examples:

- `flow_detail`: `service.get_flow_detail(alias=project, config_id=flow_id, branch_id=effective_branch)`.
- `flow_new`: `service.create_flow(alias=project, name=name, description=description, phases=phases, tasks=tasks, branch_id=branch)`; success line: `f"Created flow '{...}' [keboola.flow/{result.get('id','')}]{branch_info}"`. Print any `result.get("warnings")` via `formatter.warning`.
- `flow_update`: `service.update_flow(alias=project, config_id=flow_id, name=name, description=description, phases=phases, tasks=tasks, branch_id=branch)`; success line uses `keboola.flow`.
- `flow_delete`: drop `component_id` from `would_delete`, the confirm prompt (`f"Delete flow keboola.flow/{flow_id}?"`), and `service.delete_flow(alias=project, config_id=flow_id, branch_id=branch)`.
- `flow_schedule`: `service.set_flow_schedule(alias=project, config_id=flow_id, cron_tab=cron, ...)`.
- `flow_schedule_remove`: both `list_flow_schedules` and `remove_flow_schedule` calls drop `component_id`; drop it from `would_delete`.

- [ ] **Step 5: Update the `flow list` legacy warning**

In `_format_flows_table`, after the errors loop, add:

```python
    legacy = result.get("legacy_orchestrator_count", 0)
    if legacy:
        formatter.warning(
            f"{legacy} legacy keboola.orchestrator flow(s) are not shown "
            f"(orchestrator support was dropped in 0.57.0; migrate to keboola.flow)."
        )
```

The JSON path already passes `result` straight through, so
`legacy_orchestrator_count` appears in `--json` automatically.

Also drop the `"Component"` column from the table (every row is `keboola.flow`):
remove `"Component"` from `columns` and remove the `f.get("component_id", "")`
cell from each row.

- [ ] **Step 6: Add the permission entry**

In `src/keboola_agent_cli/permissions.py`, in the `# Flow operations` block,
after `"flow.schema": "read",` add:

```python
    "flow.validate": "read",
```

- [ ] **Step 7: Update `flow_app` help string**

Change `flow_app = typer.Typer(help="Manage flows (keboola.orchestrator + keboola.flow)")`
to `flow_app = typer.Typer(help="Manage conditional flows (keboola.flow)")`.

- [ ] **Step 8: Add CLI tests for dropped `--component-id` + detail rendering**

Append to `tests/test_flow_cli.py`:

```python
def test_component_id_flag_removed(runner, app):
    # --component-id is no longer a recognized option on flow detail
    result = runner.invoke(
        app,
        ["flow", "detail", "--project", "x", "--flow-id", "1", "--component-id", "keboola.flow"],
    )
    assert result.exit_code == 2
    assert "No such option" in result.stdout or "no such option" in result.stdout.lower()
```

- [ ] **Step 9: Run the CLI suite**

Run: `uv run pytest tests/test_flow_cli.py -v`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/keboola_agent_cli/commands/flow.py src/keboola_agent_cli/permissions.py tests/test_flow_cli.py
git commit -m "feat(flow): add 'flow validate', drop --component-id, legacy-count warning"
```

---

### Task 13: `flow detail` human rendering rewrite

**Files:**
- Modify: `src/keboola_agent_cli/commands/flow.py` (`_format_flow_detail`)
- Test: `tests/test_flow_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_flow_cli.py` (mock the service via monkeypatch of the
service factory is heavy; instead test the pure formatter through a small unit).
Add a direct unit test of the renderer:

```python
def test_format_flow_detail_renders_transitions_and_badges(capsys):
    from keboola_agent_cli.commands.flow import _format_flow_detail
    from keboola_agent_cli.output import OutputFormatter

    formatter = OutputFormatter(json_mode=False)
    detail = {
        "name": "My CF",
        "id": "100",
        "phases": [
            {
                "id": "p1",
                "name": "Extract",
                "next": [
                    {
                        "id": "c",
                        "goto": "p2",
                        "condition": {
                            "type": "operator",
                            "operator": "ANY_TASKS_IN_PHASE",
                            "phase": "p1",
                            "operands": [],
                        },
                    },
                    {"id": "d", "goto": None},
                ],
            },
            {"id": "p2", "name": "Transform"},
        ],
        "tasks": [
            {
                "id": "t1",
                "name": "Run",
                "phase": "p1",
                "enabled": True,
                "task": {
                    "type": "job",
                    "componentId": "keboola.ex-http",
                    "configId": "9",
                    "mode": "run",
                },
            },
            {
                "id": "t2",
                "name": "Notify",
                "phase": "p2",
                "task": {"type": "notification", "title": "x", "recipients": []},
            },
        ],
    }
    _format_flow_detail(formatter, detail)
    out = capsys.readouterr().out
    assert "Extract" in out and "Transform" in out
    assert "→" in out  # transition arrow
    assert "default" in out.lower()  # condition-less transition labeled
    assert "job" in out and "notification" in out  # task type badges
```

> NOTE: confirm `OutputFormatter(json_mode=False)` is the correct constructor by
> checking `src/keboola_agent_cli/output.py`; adapt the kwarg if the signature
> differs (e.g. positional). The existing `conftest.py` has a formatter fixture
> you may reuse instead.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_flow_cli.py -k "format_flow_detail" -v`
Expected: FAIL (current renderer prints `dependsOn`, no arrows/badges).

- [ ] **Step 3: Rewrite `_format_flow_detail`**

Replace `_format_flow_detail` in `src/keboola_agent_cli/commands/flow.py`:

```python
def _summarize_condition(condition: dict[str, Any] | None) -> str:
    """One-line human summary of a transition condition."""
    if not condition:
        return "default"
    ctype = condition.get("type")
    if ctype == "operator":
        op = condition.get("operator", "?")
        phase = condition.get("phase")
        return f"{op}({phase})" if phase else f"{op}(...)"
    if ctype == "function":
        return f"{condition.get('function', '?')}(...)"
    if ctype in ("const", "constant"):
        return f"const={condition.get('value')!r}"
    return str(ctype)


def _format_flow_detail(formatter: Any, result: dict[str, Any]) -> None:
    formatter.console.print(
        f"\n[bold]{escape(result.get('name', ''))}[/bold]"
        f"  [dim](keboola.flow / {escape(str(result.get('id', '')))})[/dim]"
    )
    if result.get("description"):
        formatter.console.print(f"[dim]{escape(result['description'])}[/dim]")
    if result.get("branch_id"):
        formatter.console.print(f"[dim]Branch: {result['branch_id']}[/dim]")

    phases = result.get("phases", [])
    tasks = result.get("tasks", [])
    if not phases and not tasks:
        formatter.console.print("\n[dim]No phases or tasks defined.[/dim]")
        return

    formatter.console.print(
        f"\n[bold]Phases[/bold] ({len(phases)})  [bold]Tasks[/bold] ({len(tasks)})"
    )

    tasks_by_phase: dict[Any, list[dict[str, Any]]] = {}
    for task in tasks:
        tasks_by_phase.setdefault(str(task.get("phase")), []).append(task)

    _TYPE_COLORS = {"job": "green", "notification": "yellow", "variable": "magenta"}

    for phase in phases:
        pid = str(phase.get("id"))
        retry = " [dim](retry)[/dim]" if phase.get("retry") else ""
        formatter.console.print(
            f"\n  [cyan bold]Phase {escape(pid)}: {escape(phase.get('name', ''))}[/cyan bold]{retry}"
        )
        for transition in phase.get("next", []):
            goto = transition.get("goto")
            target = "END" if goto is None else str(goto)
            summary = _summarize_condition(transition.get("condition"))
            formatter.console.print(f"      [dim]→ {escape(target)} [{escape(summary)}][/dim]")
        for task in tasks_by_phase.get(pid, []):
            t_info = task.get("task") or {}
            ttype = t_info.get("type", "?")
            color = _TYPE_COLORS.get(ttype, "white")
            badge = f"[{color}]{escape(ttype)}[/{color}]"
            detail_str = ""
            if ttype == "job":
                detail_str = f" {escape(str(t_info.get('componentId', '')))}/{escape(str(t_info.get('configId', '')))}"
            elif ttype == "variable":
                detail_str = f" {escape(str(t_info.get('name', '')))}"
            t_retry = " [dim](retry)[/dim]" if t_info.get("retry") else ""
            enabled = "" if task.get("enabled", True) else " [dim](disabled)[/dim]"
            formatter.console.print(
                f"    [{escape(str(task.get('id', '?')))}] {badge} "
                f"{escape(task.get('name', ''))}[dim]{detail_str}[/dim]{enabled}{t_retry}"
            )

    orphan_keys = set(tasks_by_phase.keys()) - {str(p.get("id")) for p in phases}
    for key in sorted(orphan_keys):
        formatter.console.print(f"\n  [yellow]Phase '{key}' (not in phases list)[/yellow]")
        for task in tasks_by_phase.get(key, []):
            formatter.console.print(f"    {escape(task.get('name', str(task)))}")
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_flow_cli.py -k "format_flow_detail" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/keboola_agent_cli/commands/flow.py tests/test_flow_cli.py
git commit -m "feat(flow): rewrite detail rendering for conditional flows"
```

---

## Phase 6 — REST mirror

### Task 14: Drop `component_id` from `server/routers/flows.py`

**Files:**
- Modify: `src/keboola_agent_cli/server/routers/flows.py`
- Test: existing server tests (run to confirm green) — locate with grep below

- [ ] **Step 1: Find server flow tests**

Run: `grep -rln "flows\|/flows\|FlowCreate" tests/ | grep -i serv`
Expected: a server test file (e.g. `tests/test_server*.py`). If a flow-route test
exists, read it to learn the expected request shape before editing.

- [ ] **Step 2: Edit the router**

In `src/keboola_agent_cli/server/routers/flows.py`:

- Delete the `DEFAULT_FLOW_COMPONENT = "keboola.flow"` constant.
- Remove `component_id` from `FlowCreate`, `FlowUpdate`, `FlowSchedule`.
- Remove the `component_id: str = DEFAULT_FLOW_COMPONENT` query param from
  `detail`, `delete`, `list_schedules`, `remove_schedule`.
- Drop the `component_id=...` kwarg from every `registry.flow.*` call (matching
  the new service signatures from Task 10).

Example for `create`:

```python
@router.post("/{project}", summary="Create a new flow")
def create(
    project: str, body: FlowCreate, registry: ServiceRegistry = Depends(get_registry)
) -> dict[str, Any]:
    """Create a new flow configuration. Mirrors `kbagent flow new`."""
    return registry.flow.create_flow(
        alias=project,
        name=body.name,
        description=body.description,
        phases=body.phases,
        tasks=body.tasks,
        branch_id=body.branch_id,
    )
```

Apply the analogous edit to `detail`, `update`, `delete`, `list_schedules`,
`set_schedule`, `remove_schedule`.

- [ ] **Step 3: Run server tests**

Run: `uv run pytest tests/ -k "flow and serv" -v` (and any file found in Step 1)
Expected: PASS. If a server test posted `component_id`, update it to omit the
field.

- [ ] **Step 4: Commit**

```bash
git add src/keboola_agent_cli/server/routers/flows.py tests/
git commit -m "feat(flow): drop component_id from /flows REST surface"
```

---

## Phase 7 — Cleanup sweep

### Task 15: Remove dead `ORCHESTRATOR_COMPONENTS`; CF scaffold for `config new`

**Files:**
- Modify: `src/keboola_agent_cli/sync/config_format.py:70`
- Modify: `src/keboola_agent_cli/services/component_service.py:43,292-298,645-650`
- Test: `tests/test_component_service.py`

- [ ] **Step 1: Confirm `ORCHESTRATOR_COMPONENTS` is dead**

Run: `grep -rn "ORCHESTRATOR_COMPONENTS" src/ tests/`
Expected: only the definition at `config_format.py:70`.

- [ ] **Step 2: Delete it**

Remove the line `ORCHESTRATOR_COMPONENTS: set[str] = {"keboola.orchestrator", "keboola.flow"}`
from `src/keboola_agent_cli/sync/config_format.py` (and its preceding comment if
standalone).

- [ ] **Step 3: Update the flow scaffold in `component_service.py`**

Read `tests/test_component_service.py` first to see what the flow scaffold test
asserts. Then:

- Change `_FLOW_COMPONENT_IDS = ("keboola.orchestrator", "keboola.flow")` to
  `_FLOW_COMPONENT_IDS = ("keboola.flow",)` (or inline `"keboola.flow"` if it
  only feeds `_classify`).
- Rewrite `_build_flow_config_yml` to emit a CF skeleton (string ids):

```python
def _build_flow_config_yml(name: str, component_id: str = "keboola.flow") -> str:
    """Generate a conditional-flow (keboola.flow) configuration YAML skeleton."""
    lines = [
        f'name: "{name}"',
        "description: |",
        "  TODO: describe this flow",
        "phases:",
        '  - id: "phase-1"',
        '    name: "Phase 1"',
        "    next:",
        '      - id: "default"',
        "        goto: null",
        "tasks:",
        '  - id: "task-1"',
        '    name: "Task 1"',
        '    phase: "phase-1"',
        "    enabled: true",
        "    task:",
        "      type: job",
        '      componentId: "keboola.ex-http"',
        '      configId: "TODO"',
        "      mode: run",
    ]
    return "\n".join(lines) + "\n"
```

- In the `config new` builder (`component_service.py:645-650`), update the flow
  branch description to `"Conditional flow (keboola.flow) configuration"` and
  pass `detail.component_id` (now always `keboola.flow` for flow components).

- [ ] **Step 4: Run component service tests**

Run: `uv run pytest tests/test_component_service.py -v`
Expected: PASS. Update any assertion that expected `dependsOn` / orchestrator
output to expect the new CF skeleton.

- [ ] **Step 5: Commit**

```bash
git add src/keboola_agent_cli/sync/config_format.py src/keboola_agent_cli/services/component_service.py tests/test_component_service.py
git commit -m "refactor(flow): drop orchestrator constants; CF scaffold for 'config new'"
```

---

## Phase 8 — E2E

### Task 16: CF round-trip E2E + skip when CF disabled

**Files:**
- Modify: `tests/test_e2e.py` (`TestE2EFlowOperations`, ~line 5101-5200)

- [ ] **Step 1: Read the existing flow E2E block**

Run: `sed -n '5101,5230p' tests/test_e2e.py` — note the `self._run`, `_step`,
and cleanup helpers and the existing assertions.

- [ ] **Step 2: Rewrite the flow E2E to use a CF payload**

Replace the body that creates/updates the flow so it writes a valid CF
definition (string ids, a `job` task) via a temp YAML file and `--file`, and
drops every `--component-id` argument. Add a `flow validate` step (offline). At
the start of `test_flow_crud_and_skip`, detect CF support and skip cleanly:

```python
def test_flow_crud_and_schedule(self, tmp_path: Path) -> None:
    # Skip if the project has conditional flows disabled.
    probe = self._run(
        "flow",
        "new",
        "--project",
        self.alias,
        "--name",
        "cf-probe",
        "--file",
        "@" + str(self._write_cf(tmp_path)),
    )
    if probe.exit_code != 0 and "conditional" in (probe.stdout + probe.stderr).lower():
        pytest.skip("Project reports conditional_flows=false; skipping CF E2E")
    ...
```

Add a helper on the test class:

```python
@staticmethod
def _write_cf(tmp_path: Path) -> Path:
    body = (
        "phases:\n"
        '  - id: "p1"\n'
        '    name: "P1"\n'
        "    next:\n"
        '      - id: "n"\n'
        "        goto: null\n"
        "tasks:\n"
        '  - id: "t1"\n'
        '    name: "T1"\n'
        '    phase: "p1"\n'
        "    enabled: true\n"
        "    task:\n"
        "      type: job\n"
        '      componentId: "keboola.ex-http"\n'
        '      configId: "1"\n'
        "      mode: run\n"
    )
    path = tmp_path / "cf.yaml"
    path.write_text(body, encoding="utf-8")
    return path
```

Ensure the steps cover: schema → validate → new → detail → update → schedule →
schedule-remove → delete. Cleanup tracks created flow ids as before (now always
`keboola.flow`).

- [ ] **Step 3: Run E2E (requires credentials)**

Run: `make test-e2e` (needs `E2E_API_TOKEN` + `E2E_URL`).
Expected: the flow E2E passes against a CF-enabled project, or skips with the
clear reason on a CF-disabled one. If credentials are unavailable in this
environment, note it and defer to CI; do NOT mark the task done without a run.

- [ ] **Step 4: Commit**

```bash
git add tests/test_e2e.py
git commit -m "test(flow): conditional-flow E2E round-trip + CF-disabled skip"
```

---

## Phase 9 — Docs, plugin sync, release

### Task 17: Update `CLAUDE.md` + `AGENT_CONTEXT` flow sections

**Files:**
- Modify: `CLAUDE.md` (`## All CLI Commands` flow block)
- Modify: `src/keboola_agent_cli/commands/context.py:569-598`

- [ ] **Step 1: Update `CLAUDE.md` flow block**

Replace the flow command lines in `## All CLI Commands` to drop `--component-id`,
add `flow validate` and `flow schema --full`:

```
kbagent flow list [--project NAME] [--branch ID] [--with-schedules]
kbagent flow detail --project NAME --flow-id ID [--branch ID]
kbagent flow schema [--full]
kbagent flow validate --file @flow.yaml|-
kbagent flow new --project NAME --name NAME [--description D] [--file @path.yaml|-|JSON] [--branch ID]
kbagent flow update --project NAME --flow-id ID [--name N] [--description D] [--file @path.yaml|-|JSON] [--branch ID]
kbagent flow delete --project NAME --flow-id ID [--branch ID] [--yes]
kbagent flow schedule --project NAME --flow-id ID --cron "0 6 * * *" [--timezone TZ] [--disabled] [--branch ID]
kbagent flow schedule-remove --project NAME --flow-id ID [--branch ID] [--yes]
# Flows are conditional flows (keboola.flow). keboola.orchestrator is NOT supported (dropped 0.57.0).
# Execute a flow with: kbagent job run --project NAME --component-id keboola.flow --config-id ID
```

- [ ] **Step 2: Update `context.py` `AGENT_CONTEXT`**

In `src/keboola_agent_cli/commands/context.py:569-598`, mirror the same changes:
remove `--component-id`, remove "defaults to keboola.orchestrator" notes, replace
"DAG re-validated on write" with "validated against the conditional-flow schema
(INVALID_FLOW_DEFINITION on failure)", add the `flow validate` and
`flow schema --full` entries, and add a note that orchestrator is dropped.

- [ ] **Step 3: Verify context renders**

Run: `uv run kbagent context | grep -A2 "flow validate"`
Expected: the new command appears; no `--component-id` in the flow section.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md src/keboola_agent_cli/commands/context.py
git commit -m "docs(flow): refresh CLAUDE.md + AGENT_CONTEXT for conditional flows"
```

---

### Task 18: Plugin sync — keboola-expert, SKILL, references

**Files:**
- Modify: `plugins/kbagent/agents/keboola-expert.md`
- Modify: `plugins/kbagent/skills/kbagent/SKILL.md`
- Modify: `plugins/kbagent/skills/kbagent/references/commands-reference.md`
- Rewrite: `plugins/kbagent/skills/kbagent/references/flow-workflow.md`
- Modify: `plugins/kbagent/skills/kbagent/references/gotchas.md`

- [ ] **Step 1: keboola-expert.md**

Update the tool-selection matrix / version gate: flows are conditional flows;
`--component-id` removed; add `flow validate`; reference the validate-before-push
loop; note orchestrator dropped in 0.57.0.

- [ ] **Step 2: SKILL.md + commands-reference.md**

Update the flow rows: drop `--component-id`, add `flow validate` and
`flow schema --full`. If the SKILL.md decision table is CI-generated, regenerate
per the repo convention; otherwise hand-edit.

- [ ] **Step 3: Rewrite flow-workflow.md**

Full rewrite around conditional flows: the CF template, a conditions cookbook
(operator/function/phase/task examples with string ids), the
validate-before-push loop (`flow validate` → fix → `flow new`/`flow update`),
and execution via `kbagent job run --component-id keboola.flow --config-id ID`.
Remove all `dependsOn` content.

- [ ] **Step 4: gotchas.md — new entries**

Add, each tagged `(since v0.57.0)`:
- orchestrator support dropped; `flow list` hides legacy flows (shows a count).
- `--component-id` removed from all flow subcommands.
- old `dependsOn` template is invalid; use `phases[].next[].goto` + conditions.
- `INVALID_FLOW_DAG` renamed to `INVALID_FLOW_DEFINITION`.
- **IDs are strings**, not integers.
- bundled schema pinned to job-queue-daemon SHA `24176de…` (re-vendor to update).
Mark the old "flow default-component differs between subcommands" gotcha as
**resolved**.

- [ ] **Step 5: Commit**

```bash
git add plugins/kbagent/
git commit -m "docs(plugin): sync flow surface to conditional flows (0.57.0)"
```

---

### Task 19: README + version bump + changelog + release checks

**Files:**
- Modify: `README.md` (if flows mentioned)
- Modify: `pyproject.toml:3`
- Modify: `src/keboola_agent_cli/changelog.py`

- [ ] **Step 1: README scan**

Run: `grep -n "orchestrator\|flow" README.md`
Update any flow mention to conditional flows; drop `--component-id`.

- [ ] **Step 2: Bump version**

In `pyproject.toml`, change `version = "0.55.0"` to `version = "0.57.0"`.

- [ ] **Step 3: Add changelog entry**

In `src/keboola_agent_cli/changelog.py`, add a `"0.57.0"` key at the TOP of
`CHANGELOG` (newest-first) with a breaking-change callout, e.g.:

```python
    "0.57.0": [
        "BREAKING: `flow` command group now targets conditional flows "
        "(`keboola.flow`) only; `keboola.orchestrator` support is dropped. "
        "`--component-id` removed from every `flow` subcommand and from the "
        "`/flows` REST surface. `flow new`/`flow update` validate payloads "
        "against the bundled conditional-flow JSON schema (phases[].next[].goto "
        "transitions + conditions; job/notification/variable tasks; string ids) "
        "and reject invalid bodies with `INVALID_FLOW_DEFINITION` (replaces "
        "`INVALID_FLOW_DAG`). New `flow validate --file` does offline schema + "
        "semantic checks. `flow schema --full` dumps the JSON schema. `flow list` "
        "hides legacy orchestrator configs and reports `legacy_orchestrator_count`. "
        "Schema pinned to job-queue-daemon@24176de.",
    ],
```

- [ ] **Step 4: Sync plugin version**

Run: `make version-sync`
Expected: `plugin.json` / `marketplace.json` updated to 0.57.0.

- [ ] **Step 5: Full check suite**

Run: `make check`
Expected: ruff lint + format-check + changelog-check + tests all pass.
Fix any `ruff check --fix` / `ruff format` / `ty check` findings inline.

- [ ] **Step 6: E2E (if creds available)**

Run: `make test-e2e`
Expected: PASS or clean CF-disabled skip.

- [ ] **Step 7: Commit**

```bash
git add README.md pyproject.toml src/keboola_agent_cli/changelog.py plugins/kbagent/.claude-plugin/ .claude-plugin/
git commit -m "release: 0.57.0 -- conditional flow support, drop orchestrator"
```

---

## Final verification

- [ ] Run the full unit suite: `uv run pytest tests/ -m "not e2e" -v` — all green.
- [ ] `grep -rn "INVALID_FLOW_DAG\|_validate_dag\|dependsOn\|ORCHESTRATOR_COMPONENTS\|FLOW_COMPONENT_IDS" src/ tests/ plugins/ docs/` returns **no production references** (historical changelog text is acceptable).
- [ ] `grep -rn "\-\-component-id" src/keboola_agent_cli/commands/flow.py src/keboola_agent_cli/server/routers/flows.py` returns nothing.
- [ ] `uv run kbagent flow schema | grep goto` and `uv run kbagent flow schema --full | grep '\$schema'` both succeed.
- [ ] `make check` passes.
