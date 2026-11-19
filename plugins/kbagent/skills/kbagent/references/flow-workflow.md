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
kbagent --json flow validate --definition-file flow.yaml --project prod

# 4. Create. flow new fetches the live schema and validates on write; a
#    schema-fetch failure degrades to semantic-only + a warning (never blocks).
kbagent --json flow new --project prod --name "Daily ETL" --definition-file flow.yaml
```

A definition you pass must hold `phases` and/or `tasks` only, and the flow it
leaves must have at least one phase (`flow update` keeps the side you leave out). An empty file, a `null` list, an unknown top-level key (`task:` instead
of `tasks:`) or the whole `flow detail --json` output is refused with
`VALIDATION_ERROR` (exit 2) and nothing is written. `flow new` WITHOUT
`--definition`/`--definition-file` creates an empty flow on purpose; fill it
with one `flow update` that carries the phases and their tasks together.

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
kbagent --json flow list --all-projects

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

`flow update --definition-file` is a **full replace** of phases+tasks. Fetch the current
body, merge locally, validate, then push.

```bash
# Rename only
kbagent --json flow update --project prod --flow-id 111 --name "New Name"

# Replace phases/tasks from file (re-validated against the CF schema on write)
kbagent --json flow detail --project prod --flow-id 111 > current.json
# ... merge edits into updated.yaml ...
kbagent --json flow validate --definition-file updated.yaml --project prod
kbagent --json flow update --project prod --flow-id 111 --definition-file updated.yaml
```

## Run a flow

*(since vNEXT)* `job run --component-id keboola.flow` refuses a flow the job
queue would refuse, like `flow run` (see Validation below). Prefer `flow run`:
it also checks a partial selection and reports every task.

*(`flow run` since 1.4.0; on older kbagent only the whole flow can run, via
`job run --project P --component-id keboola.flow --config-id ID`.)*

Running a flow starts real jobs that write data and spend credits. Treat it
like any other execution: show the user what will run and wait for an explicit
"run it" (see [safe-write-workflow](safe-write-workflow.md)). Never chain a run
right after `flow update`.

### Pick the right kind of run

| The user wants to | Use | Conditions evaluated? |
|---|---|---|
| Run the flow as production would | `flow run --project P --flow-id ID` (no selector) | yes |
| Re-run one downstream part after an edit or a failure | `flow run --project P --flow-id ID --from-phase-id PH` | **no** |
| Run a few named tasks, in any phases | `flow run --project P --flow-id ID --task-id T --task-id T2` | **no** |
| Test that the transition conditions route correctly | whole flow, ideally on a dev branch (`branch create` + `--branch ID`) | yes |

A partial run **cannot** test conditions or notification routing: the server
ignores every condition in it. Say so when a user asks to "test the new phase's
conditions" with a partial run; a partial run proves only that the selected
tasks themselves run.

### Steps

```bash
# 1. Find the phase and task ids (strings) of the flow
kbagent --json flow detail --project prod --flow-id 111
# 2. Preview: reads the flow once, creates nothing
kbagent --json flow run --project prod --flow-id 111 --from-phase-id transform --dry-run
# 3. Show the user data.selection + warnings[]; wait for an explicit yes
# 4. Run and wait
kbagent --json flow run --project prod --flow-id 111 --from-phase-id transform --wait
# Single tasks, any phase:
kbagent --json flow run --project prod --flow-id 111 --task-id task-a --task-id task-b --wait
# Whole flow, conditions evaluated:
kbagent --json flow run --project prod --flow-id 111 --wait
```

`--task-id` and `--from-phase-id` can be combined; the selection is their
union, de-duplicated. The Queue job carries the list as `onlyFlowTaskIds`, the
same mechanism as the Keboola UI's "Run selected tasks".

### What a partial run does (tell the user before running)

This is the Queue daemon's behaviour ("Selected Tasks Run" in the
job-queue-daemon flow documentation), not kbagent's; kbagent only sends the
task list.

- **Conditions are not evaluated.** The server chains the phases holding
  selected tasks one after another, from the flow's first phase. Phases needed
  only to reach a selected phase run empty.
- **Every selected task runs, even after a failure.** A failed task does not
  stop the run.
- **Tasks you select on both arms of an if/else both run**, one after the other.
- **Only the selected tasks run.** Unselected tasks of every type (job,
  variable, notification) do not run at all: no alert is sent unless its task is selected, and variables that an
  unselected variable task would set are not set (jobs fall back to their
  component's own variable values).
- **The flow job's status is the status of its last phase**, so it can say
  `success` while a selected task failed. Never report a partial run as
  successful from the flow status alone: read `failed_task_ids` (see Result).

### `--from-phase-id` follows default transitions only

`--from-phase-id P` selects P plus the phases reached by following each phase's
**default transition** (the `next[]` entry without a `condition` key) until
`goto: null`. It never follows a conditional transition, because in a partial
run the server would run that branch unconditionally: following every `goto`
would, for example, send the failure e-mail although nothing failed.

- Phases with an enabled task that are reachable from that path only through a
  conditional transition (failure handling, notifications, retries), and that
  hold none of the tasks you selected with `--task-id`, are named in a
  `CONDITIONAL_PHASES_SKIPPED` warning. Add their tasks with `--task-id`
  only when the user wants them to run.
- `condition: null` or `condition: {}` counts as a condition, not a default.
- A phase with conditional transitions but no default ends the path there.
- More than one default transition is a malformed flow: `INVALID_ARGUMENT`.
  A transition (default or not) to an unknown phase breaks every run:
  `INVALID_FLOW_DEFINITION` (see Validation).
- It does not mean "as if everything succeeded": a success condition can lead
  somewhere else than the default transition.

### Validation (before anything is created)

A partial run fails with `INVALID_ARGUMENT`, exit 2 (HTTP 400 over serve),
before any job is created, when:

- an id is blank (`--task-id " "` is never read as "no selector", so it can
  never turn into a whole-flow run) or contains a comma. These are checked
  first and the message names the option, not every value;
- a task id or the phase id is unknown; a task given by id is disabled (on the
  `--from-phase-id` path disabled tasks are skipped and listed in
  `skipped_disabled_task_ids`); a selected task's phase does not exist; nothing
  is left to run. These are listed together in one message;
- the `--from-phase-id` path meets a malformed default transition (see above).
- *(since 1.4.0)* the job queue would refuse the selection: a phase with a
  selected task cannot be reached from the flow's first phase, or the phases
  the run needs reach each other (a loop: a partial run cannot enter a phase
  twice). kbagent runs the job queue's own check, so a `--dry-run` that
  succeeds is a run the queue accepts.

*(since 1.4.0)* Every run, whole or partial, `--dry-run` included, fails with
`INVALID_FLOW_DEFINITION`, exit 1, before any job is created when the flow
itself is one the job queue refuses: a repeated phase id or task id (also a
disabled one), a transition to a phase the flow does not define, or not
exactly one first phase (a phase no transition goes to; a transition back to
the first phase leaves none). Fix the flow; `flow validate` does not check
all of these. Before 1.4.0 the dry run said yes and the job failed after it
was created.

A cycle on the default path that stays inside one phase (a phase retrying
itself) is not an error: kbagent stops there and sets
`selection.default_path_cycle: true`. A whole-flow run checks only the flow
rules above; its conditions decide the rest.

### Result

- `data.selection`: `task_ids`, `from_phase_id`, `phases` (which phases hold
  selected tasks; **not** the order the daemon runs them in),
  `skipped_disabled_task_ids`, `default_path_cycle`.
- `warnings[]` in the `--json` envelope: `PARTIAL_RUN_IGNORES_CONDITIONS` on
  every partial run whose selection was valid (also when the job then fails or
  times out; not on a validation error or a failed flow read),
  `CONDITIONAL_PHASES_SKIPPED`, `SELECTED_TASK_FAILED`.
- With `--wait` on a partial run: `task_results` (`task_id`, `phase_id`,
  `status`, `job_id`) for every selected task, and `failed_task_ids` (status not
  `success`/`warning`, or `missing` when the task is absent from the job's
  `result.tasks`).
- The job id: `data.id` when the job was created (also with `--wait`);
  `error.details.job_id` on every error after the job was created:
  `QUEUE_JOB_FAILED`, a timeout, and a failure while polling (connection
  error, HTTP 5xx, exhausted retries -- these keep their own code and exit
  code, with `retryable: false`). A `--dry-run` creates no job and has no id.
- A whole-flow `--dry-run` lists the enabled tasks as tasks that **could** run;
  the conditions decide which do.

| Exit | Meaning | What to do |
|---|---|---|
| 0 | `--dry-run`; job created (no `--wait`); partial run where every selected task succeeded; whole-flow run that finished in any status but `error` (check `data.status`: `warning` / `terminated` also exit 0) | report the status, not just "done" |
| 1 | `SELECTED_TASK_FAILED` (payload has `failed_task_ids`), or `QUEUE_JOB_FAILED` (the flow job ended `error`; `error.details` has `job_id`, and on a partial run usually `task_results` / `failed_task_ids`; they are missing when kbagent could not read the failed job) | `job detail --project P --job-id` of the failed task's `job_id`; when a task has no `job_id` (status `missing`, or the result did not carry one), read the flow job itself |
| 2 | invalid selection or options | fix the ids from `flow detail` |
| 4 | `--timeout` reached, or polling lost the connection (`CONNECTION_ERROR`, `TIMEOUT`, `RETRY_EXHAUSTED`): the job **keeps running** (`retryable: false`, `error.details.job_id`) | `job detail --project P --job-id ID`; do **not** start it again |
| 6 | permission denied | see "Variables, branch, permissions" below |

### Retries create duplicate runs

`flow run` has no `--idempotency-key`: every call that passes validation (and is not `--dry-run`) creates a new job. After a
timeout or a lost response, look the job up (`job list --project P --component-id
keboola.flow --config-id ID`, `job detail --project P --job-id ID`) before running again. For a
replay-safe whole-flow run use `job run --project P --component-id
keboola.flow --config-id ID --idempotency-key KEY`: the key is remembered only
in this machine's config directory, and a key whose earlier job failed (or is
gone) starts a new job, so it prevents a duplicate of a running or successful
run, not every repeat. To re-run only what failed, pass the
previous run's `failed_task_ids` as `--task-id` (the Queue's own "re-run failed
tasks of job X" is not exposed).

### Variables, branch, permissions

- Linked variables are resolved like `job run`: the values row the flow
  configuration names (`variables_values_id`), else the first values row;
  override with `--variable-values-id ID`, or skip with `--no-variables`.
- The run goes to the active branch (`branch use`) unless `--branch ID` is
  given; `--branch 0` is production. The selection is read from the same branch
  the job runs on. An edit of the flow between that read and the Queue running
  the job is not detected.
- Permission key `flow.run` (write), and it also needs `job.run`: `flow run`
  creates a Queue job, so a policy that denies `job.run` (or `flow.run`, or
  `cli:write` / `--deny-writes`) blocks it, `--dry-run` included (exit 6,
  HTTP 403). An allow-list policy (`--mode deny`) must allow both keys.
- Over `kbagent serve`: `POST /flows/{project}/{flow_id}/run` with body
  `task_ids`, `from_phase_id`, `branch_id`, `variable_values_id`,
  `no_variables`, `dry_run` and query `wait`, `timeout`. `task_ids: []` is HTTP
  400 (`null` = no selector); unknown fields or query parameters are 422;
  warnings come in the `Kbagent-Warnings` header.

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

The delete moves the flow to the trash. A repeated delete answers
`status: "already_in_trash"` (exit 0) and sends nothing, so retrying after a
timeout is safe. Undo with `config restore --component-id keboola.flow`.

## Validation errors

`flow new` / `flow update` validate the body against the **live** conditional-flow
JSON Schema (fetched at runtime from the stack) plus semantic checks; failures
raise `INVALID_FLOW_DEFINITION` with all violation messages joined. Run
`flow validate --definition-file flow.yaml --project PROJECT` (with `--project` to fetch the
live schema) to see them. Common causes:

- integer ids (ids must be **strings**);
- `next[].goto` targets a non-existent phase (use `null` to end);
- a phase with conditional transitions lacks a trailing default transition;
- the definition has no phases (`phases: []`, or a `tasks`-only `flow new`);
- `phases` / `tasks` / `next` is not a list of objects; an `id` or task `phase`
  is not a string or number; `enabled` is not `true`/`false`; a `condition` is
  not an object (since vNEXT);
- the flow has not exactly one first phase, a phase no transition goes to
  (since vNEXT; the job queue runs no other flow);
- a task references an unknown `phase`, or a phase has no enabled task;
- operator/function operand-arity violations.

Unreachable phases are reported as **warnings** (never block a write), counted
from the first phase (the one no transition goes to, since vNEXT). `goto`
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

## Flows in a sync / CI deploy pipeline

In a synced `_config.yml`, a flow's `phases` and `tasks` belong under
`_configuration_extra:`, where `sync pull` writes them. Top-level `phases:` /
`tasks:` are dropped by `sync push` without a message up to 1.0.0rc4 (an error
*(since 1.0.0)*, #941). See [gitops-cicd-workflow.md](gitops-cicd-workflow.md)
pitfalls 2.3 and 3.5.
