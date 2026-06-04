# Flow Workflow (Conditional Flows / keboola.flow)

> **Since v0.57.0:** the `flow` command group targets **`keboola.flow`
> (Conditional Flows) ONLY**. `keboola.orchestrator` support was dropped and
> `--component-id` was removed from every subcommand. The old `dependsOn`
> phase-DAG template is **invalid**. IDs are **strings**.

Conditional Flows model orchestration as phases connected by **transitions**
(`next[].goto`) with optional **conditions**, plus typed **tasks**.

## Core concepts

- **Phase**: `{id (string), name, next?: [...], retry?, description?}`. The
  **entry phase** is the first item in `phases[]`.
- **Transition** (`next[]` item): `{id, goto, condition?, name?}`. `goto` is a
  phase id **or `null`** (= end the flow). A phase whose `next[]` contains any
  conditional transition **MUST end with a default** (condition-less) transition.
- **Task**: `{id (string), name, phase, enabled?, task: {...}}`. `task.type` is
  one of `job` / `notification` / `variable`. Every phase must have **≥1 enabled
  task**.
- **Condition**: a recursive grammar (`const`/`phase`/`task`/`variable`/
  `operator`/`function`/`array`). Operators: `AND`/`OR` (≥1 operand),
  `EQUALS`/`NOT_EQUALS`/`GREATER_THAN`/`LESS_THAN`/`INCLUDES`/`CONTAINS`
  (exactly 2 operands), `ALL_TASKS_IN_PHASE`/`ANY_TASKS_IN_PHASE` (require a
  `phase` field). Functions `COUNT`/`DATE` take exactly 1 operand.
- **Schedule**: stored as a `keboola.scheduler` config whose
  `target.componentId` is `keboola.flow`; not part of the flow body itself.

## Quick start: validate-before-push loop

```bash
# 1. See the template (string ids, next[].goto, typed tasks) -- offline
kbagent flow schema
# Full JSON Schema (the exact contract) -- fetched live from the stack, needs --project:
kbagent flow schema --full --project PROJECT

# 2. Author flow.yaml
cat > flow.yaml <<'EOF'
phases:
  - id: "extract"
    name: "Extract"
    next:
      # If any task in 'extract' failed, branch to 'notify'.
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
    next:
      - id: "done"
        goto: null            # end the flow
  - id: "notify"
    name: "Notify on failure"
tasks:
  - id: "task-extract"
    name: "Run extractor"
    phase: "extract"
    enabled: true
    task:
      type: job
      componentId: "keboola.ex-db-snowflake"
      configId: "123456"
      mode: run
  - id: "task-transform"
    name: "Run transformation"
    phase: "transform"
    enabled: true
    task:
      type: job
      componentId: "keboola.snowflake-transformation"
      configId: "789012"
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
EOF

# 3. Validate; loop until clean. Pass --project to fetch the LIVE schema from the
#    stack for full structural + semantic validation. Without --project you get
#    semantic-only checks plus a note that structural validation was skipped.
kbagent --json flow validate --file @flow.yaml --project prod

# 4. Create. flow new fetches the live schema and validates on write; a
#    schema-fetch failure degrades to semantic-only + a warning (never blocks).
kbagent --json flow new --project prod --name "Daily ETL" --file @flow.yaml
```

## Conditions cookbook

```yaml
# A task in another phase succeeded:
condition:
  type: task
  task: "task-extract"
  value: "success"

# Logical AND of two checks:
condition:
  type: operator
  operator: AND
  operands:
    - { type: phase, phase: "extract", value: "success" }
    - { type: variable, value: "run_full" }

# Equality (exactly 2 operands):
condition:
  type: operator
  operator: EQUALS
  operands:
    - { type: variable, value: "env" }
    - { type: const, value: "prod" }
```

## List and inspect flows

```bash
# Conditional flows across all projects (legacy orchestrator configs are
# NOT listed -- their count surfaces as legacy_orchestrator_count + a warning).
kbagent --json flow list

# Flows in one project
kbagent --json flow list --project prod

# Full phase/task breakdown (transitions, task-type badges, retry)
kbagent --json flow detail --project prod --flow-id 111

# Flow-centric schedule view -- each row gets inline schedules: [...]
kbagent --json flow list --project prod --with-schedules
```

For schedule-centric fleet-wide discovery, see
[schedule-workflow.md](schedule-workflow.md).

## Update a flow

`flow update --file` is a **full replace** of phases+tasks. Fetch the current
body, merge locally, validate, then push.

```bash
# Rename only
kbagent --json flow update --project prod --flow-id 111 --name "New Name"

# Replace phases/tasks from file (re-validated against the CF schema on write)
kbagent --json flow detail --project prod --flow-id 111 > current.json
# ... merge edits into updated.yaml ...
kbagent --json flow validate --file @updated.yaml --project prod
kbagent --json flow update --project prod --flow-id 111 --file @updated.yaml
```

## Run a flow

Conditional flows execute as a job on the `keboola.flow` component:

```bash
kbagent --json job run --project prod --component-id keboola.flow --config-id 111 --wait
```

## Schedule a flow

`flow schedule` is an upsert (stored as a `keboola.scheduler` config targeting
`keboola.flow`).

```bash
kbagent --json flow schedule --project prod --flow-id 111 --cron "0 6 * * *"
kbagent --json flow schedule --project prod --flow-id 111 \
  --cron "0 8 * * 1-5" --timezone "Europe/Prague" --disabled
kbagent --json flow schedule-remove --project prod --flow-id 111 --yes
```

## Delete a flow

```bash
kbagent --json flow delete --project prod --flow-id 111 --yes
```

## Validation errors

`flow new` / `flow update` validate the body against the **live** conditional-flow
JSON Schema (fetched at runtime from the stack) plus semantic checks; failures
raise `INVALID_FLOW_DEFINITION` with all violation messages joined. Run
`flow validate --file @flow.yaml --project PROJECT` (with `--project` to fetch the
live schema) to see them. Common causes:

- integer ids (ids must be **strings**);
- `next[].goto` targets a non-existent phase (use `null` to end);
- a phase with conditional transitions lacks a trailing default transition;
- a task references an unknown `phase`, or a phase has no enabled task;
- operator/function operand-arity violations.

Unreachable phases are reported as **warnings** (never block a write). `goto`
loops are legal at runtime and are NOT flagged.

### Graceful degradation when the schema can't be fetched

If the live schema fetch fails (network error, or the AI Service returns no
`configurationSchema` for `keboola.flow`), the write is **not** blocked:
structural validation is skipped, the semantic checks still run (the Storage API
does not validate flow configs server-side), and a
`structural schema validation skipped: <reason>` warning is surfaced on the
result. `flow validate` without `--project` behaves the same way and adds a note
explaining there was no schema source.

## Schema source of truth

The conditional-flow JSON Schema is served by the **stack's component registry**
and fetched at runtime via the AI Service `configurationSchema` for
`keboola.flow` -- it is **not bundled or vendored** in the CLI. This guarantees
the validator always matches the stack the user is actually talking to. There is
nothing to re-vendor or pin; `flow schema --full --project PROJECT` prints
whatever the live stack serves.
