# Design: Conditional Flow (`keboola.flow`) support in kbagent — drop `keboola.orchestrator`

**Linear issue:** AJDA-2813 "CF: add support in new CLI"
**Target release:** 0.56.0 (one breaking release)
**Status:** design approved (subagent-driven; decisions recorded below)
**Date:** 2026-06-04

## Design revision (2026-06-04) — schema is fetched live, not bundled

This supersedes decision **D3** (and the related D12 / §4.1 / §4.6 "bundled
schema" wording). The conditional-flow JSON Schema is **no longer
bundled/vendored** in the CLI. It is fetched at **runtime** from the stack's
component registry.

- **Source:** AI Service `/docs/components/keboola.flow` →
  `ComponentDetail.configuration_schema`, via the existing `AiServiceClient`
  (the same path `config new --push` already uses for schema validation).
  Verified live that both the Storage API component index and the AI Service
  serve the full CF schema; the AI Service path was chosen for DI symmetry with
  `component_service` / `config_service`.
- **Why:** a bundled schema drifts the moment upstream changes and forces a
  re-vendor + SHA bump (the original D3). Fetching live guarantees the validator
  always matches the stack the user is actually talking to, removes the private
  upstream repo from the loop, and eliminates the wheel-packaging surface.
- **`flow_validation.py` stays pure:** `validate_conditional_flow(phases, tasks,
  schema=None)` takes the schema as an explicit optional parameter. Structural
  (Draft7) validation runs only when a schema is supplied; the semantic checks
  always run. No network calls in this module.
- **Graceful degradation:** `FlowService.create_flow`/`update_flow` fetch the
  live schema before validating. On fetch failure (network, `KeboolaApiError`,
  or empty/missing schema) the write is **not** blocked — structural validation
  is skipped, semantic checks still run (Storage does not validate flow configs
  server-side), and a `structural schema validation skipped: <reason>` warning is
  surfaced. A real validation error still rejects with `INVALID_FLOW_DEFINITION`.
- **CLI surface:** `flow validate` gains optional `--project ALIAS` (fetch live
  schema → full validation; without it, semantic-only + an explicit note).
  `flow schema --full` now **requires `--project`** (fetches/dumps the live
  schema); plain `flow schema` stays the offline YAML template.

The sections below are kept for historical context; where they say "bundled",
"vendored", "pinned SHA", or `resources/conditional-flow-schema.json`, read the
revision above instead.

## 1. Problem

The `kbagent flow` command group treats `keboola.flow` as if it shared the legacy
`keboola.orchestrator` shape (a `dependsOn`-based phase DAG). In reality
`keboola.flow` **is** the Conditional Flow (CF) component, with a completely
different configuration schema:

- `phases[]` carry `next[]` transitions with optional `condition` objects and a
  `goto` target (another phase id or `null` = end the flow); plus optional
  per-phase `retry`.
- `tasks[]` are typed: `task.type` ∈ {`job`, `notification`, `variable`}; each
  task references its phase via `task.phase`.
- Conditions form a recursive grammar (`const`/`phase`/`task`/`variable`/
  `operator`/`function`/`array`).

Today the CLI:

- `flow schema` prints a `dependsOn` template **labeled** `keboola.flow` — wrong.
- `_validate_dag` (services/flow_service.py:47) validates a `dependsOn` graph
  that does not exist in CF; it passes trivially and checks nothing real
  (`next`/`goto` never validated).
- Defaults are inconsistent: `flow new` defaults to `keboola.flow`, while
  `detail`/`update`/`delete`/`schedule`/`schedule-remove` default to
  `keboola.orchestrator` (a documented gotcha).
- Tests and `flow-workflow.md` cement the wrong contract.

**Goal:** rewrite the flow surface for `keboola.flow` (Conditional Flows) with
the correct schema and validation, and **drop `keboola.orchestrator` support
entirely** (the old CLI is being deprecated).

## 2. Source of truth (grounding)

The CF JSON Schema lives in the **private** repo `keboola/job-queue-daemon`,
file `docs/flow-schema.json` (JSON Schema draft-07, maintained by the engine
that executes conditional flows).

- The public raw URL 404s (private repo); the file is reachable via
  `gh api repos/keboola/job-queue-daemon/contents/docs/flow-schema.json` with
  the maintainer's GitHub credentials.
- **Pinned commit SHA:** `24176de2ec1098e0a4be278815e0ca57a93cc93d`
  (committed 2026-05-26). This SHA is recorded in the loader header comment and
  in the gotchas log.

### Verified schema shapes (read from the pinned schema, 21 KB)

- Top-level `required`: `["phases", "tasks"]`.
- **`phases[]`**: required `["id", "name"]`; props `id, name, retry?, next?, description?`.
  - `next[]`: required `["id", "goto"]`; props `id, name?, condition?, goto`.
  - `goto`: `type: ["string", "null"]` — target phase id or `null` (end flow).
- **`tasks[]`**: required `["id", "name", "task", "phase"]`; props `id, name, phase, task, enabled?`.
  - `task` is a `oneOf` over three typed shapes:
    - `job`: required `type=job, componentId, mode` + `anyOf(configId|configData)`.
    - `notification`: notification task shape.
    - `variable`: variable task shape.
- **IDs are STRINGS, not integers.** `phase.id`, `task.id`, `next.id`,
  `task.phase`, `goto` are all `type: "string"` (or `["string","null"]` for
  `goto`). **This contradicts the issue text and the current code, which assume
  integer phase ids.** The new code, template, fixtures, and docs MUST use
  string ids.
- **Conditions** (`#/definitions/conditionObject` = `oneOf` of):
  - `constantCondition`: `type ∈ {const, constant}`, `value`.
  - `phaseCondition`: `type=phase`, `phase`, `value`.
  - `taskCondition`: `type=task`, `task`, `value`.
  - `variableCondition`: `type=variable`, `value`.
  - `operatorCondition`: `type=operator`, `operator`, `operands`. Two variants:
    - relational/logical: `operator ∈ {AND, OR, EQUALS, NOT_EQUALS, GREATER_THAN, LESS_THAN, INCLUDES, CONTAINS}`.
    - phase-scoped: requires `operator + phase + operands`, `operator ∈ {ALL_TASKS_IN_PHASE, ANY_TASKS_IN_PHASE}`.
  - `functionCondition`: `type=function`, `function ∈ {COUNT, DATE}`, `operands`.
  - `arrayCondition`: `type=array`, `operands`.
- `retryConfiguration`: `retryOn?`, `strategy ∈ {linear}`, `strategyParams?`.

## 3. Decisions (open questions + judgment calls)

These were decided by the implementing subagent (no interactive user); the user
should review them.

| # | Question | Decision | Rationale |
|---|----------|----------|-----------|
| D1 | Feature-gate preflight for CF-disabled projects? | **Error-mapping only** (adopt issue proposal 1). No proactive `conditional_flows` feature check. | YAGNI; one fewer API round-trip; the Storage API already rejects with a clear-enough error we can map. A proactive check would duplicate engine logic and drift. |
| D2 | Legacy-flow UX in `flow list`? | **Warning only** via `legacy_orchestrator_count` (adopt proposal 2). No `--legacy` escape hatch. | YAGNI; orchestrator is deprecated. Surfacing a count tells users why a flow "disappeared" without re-introducing legacy plumbing. |
| D3 | Schema drift vs upstream? | **Pin SHA + gotcha note for v1** (adopt proposal 3). No CI freshness check. | The upstream repo is private, so a CI fetch would need a token in CI; not worth it for v1. A follow-up issue can add a freshness job if drift bites. |
| D4 | **ID type: integer or string?** | **String ids everywhere** (overrides the issue's integer assumption). | Grounded in the pinned schema: all ids are `type: "string"`. Using ints would fail Draft7 validation and produce configs the engine rejects. Recorded as a deviation from the issue text. |
| D5 | Cycle detection over `goto` edges? | **No cycle detection.** Unreachable phases = **warning**, not error. | The issue is explicit: `goto` loops are legal at runtime. Reachability is computed by a forward graph walk from the entry phase. |
| D6 | What is the "entry phase" for reachability? | **The first phase in `phases[]`** (document this). | The schema has no explicit entry marker; engine convention is array order. Keep it simple and documented; reachability is a warning only, so a wrong guess is non-fatal. |
| D7 | `flow validate` exit code on validation failure? | **Exit 2** (usage/validation), matching the issue and existing `VALIDATION_ERROR` convention in flow.py. Exit 0 on success. | Consistent with how `_load_flow_yaml` failures already exit 2. |
| D8 | Does `flow validate` hit the network? | **No.** Pure offline: parse YAML/JSON → schema + semantic validation. No project/branch required, no `--project`. | Lets agents run a tight validate-before-push loop with zero credentials/latency. |
| D9 | `operatorCondition` arity enforcement vs schema. | Enforce as **semantic checks** layered on top of Draft7 structural validation: EQUALS/NOT_EQUALS/GREATER_THAN/LESS_THAN/INCLUDES/CONTAINS = 2 operands; AND/OR ≥ 1; COUNT/DATE (functionCondition) = 1; ALL/ANY_TASKS_IN_PHASE require `phase`. | The schema cannot express per-operator operand counts; these are the issue's explicit arity rules, refined to match the verified enums. |
| D10 | Behavior of `update_flow` validation. | Validation runs on the **merged** result (fetch current body when only one of phases/tasks supplied), preserving today's merge-aware behavior. | Matches issue Phase 2 and current code; avoids validating a half-config. |
| D11 | `flow detail` JSON output. | **Full-body passthrough unchanged.** Only the **human** rendering is rewritten (per-phase transitions, task-type badges, retry). | Stable machine contract; agents already consume the raw body. |
| D12 | `flow schema --full`. | Add `--full` to dump the **bundled JSON schema verbatim**; default prints the YAML template. JSON mode (`--json`) of `--full` returns the parsed schema object. | Agents need the exact contract; humans need a copy-paste template. |
| D13 | Removing `INVALID_FLOW_DAG` from `ErrorCode`. | **Remove** it and add `INVALID_FLOW_DEFINITION`. Grep confirmed references are only in this repo (errors.py, flow_service.py, changelog.py history, tests, docs) — no external wire consumers known. | Per coding-convention note "renaming/removing a code = major bump"; we accept this as part of the single 0.56.0 breaking release and changelog it loudly. |
| D14 | `component_id` on REST models. | **Drop** `component_id` from `FlowCreate`/`FlowUpdate`/`FlowSchedule` and from query params on `detail`/`delete`/`list_schedules`/`remove_schedule`. Keep URL paths stable. | Issue Phase 4; CF is the only component now. |
| D15 | Service signatures. | **Remove** `component_id` from all 8 service methods; hardcode `FLOW_COMPONENT_ID = "keboola.flow"`. Scheduler `target.componentId` is always `keboola.flow`. | Issue Phase 2. Reduces a whole class of "wrong default component" bugs. |
| D16 | `notification` / `variable` task validation depth. | Rely on Draft7 structural validation for their internal shape; semantic layer only checks the cross-cutting rules (unique ids, phase refs, enabled-task-per-phase). | The schema already encodes their structure; re-implementing it in Python would drift. |

## 4. Architecture

Follows the repo's 3-layer design (CLI → service → client) plus a new pure
validation module.

```
commands/flow.py        (LAYER 1) thin Typer: 9 subcommands (8 existing + new `validate`)
services/flow_service.py(LAYER 2) CRUD/schedule; single component; calls validation
services/flow_validation.py (NEW) pure functions: schema load + structural + semantic
src/keboola_agent_cli/resources/conditional-flow-schema.json (NEW) bundled schema
server/routers/flows.py REST mirror (component_id dropped)
```

### 4.1 New module: `services/flow_validation.py`

Pure, dependency-light (only `jsonschema` + `importlib.resources`), **no HTTP,
no ConfigStore** — trivially unit-testable. Public surface:

- `load_conditional_flow_schema() -> dict` — loads the bundled JSON via
  `importlib.resources.files("keboola_agent_cli.resources")`, cached with
  `functools.lru_cache`. Header comment names the upstream repo + pinned SHA.
- `validate_conditional_flow(phases: list[dict], tasks: list[dict]) -> list[str]`
  — returns a flat list of human-readable error strings (empty = valid).
  - **Structural:** build the document `{"phases": phases, "tasks": tasks}` and
    run `jsonschema.Draft7Validator(schema).iter_errors(doc)` — collect **all**
    errors (not first-fail), each rendered with its JSON path.
  - **Semantic** (only runs if structural passed, to avoid cascade noise):
    - unique phase ids; unique task ids;
    - every `task.phase` references an existing phase id;
    - every `next[].goto` is an existing phase id **or** `null`;
    - a phase whose `next[]` contains any conditional transition MUST end with a
      default (condition-less) transition (the last `next` item, `goto` may be a
      phase id or `null`);
    - every phase has ≥1 **enabled** task (`enabled` defaults true);
    - operator/function operand-arity (D9).
- `find_unreachable_phases(phases) -> list[str]` — forward BFS over `next[].goto`
  edges from the entry phase (first phase, D6); phases never visited are
  returned as **warnings** (surfaced separately, never block writes).

`validate_conditional_flow` returns errors; reachability is computed separately
so callers can treat it as a warning. **No cycle detection** (D5).

### 4.2 `services/flow_service.py` changes

- `FLOW_COMPONENT_IDS: tuple` → `FLOW_COMPONENT_ID = "keboola.flow"`.
- Delete `_validate_dag`; import `validate_conditional_flow` /
  `find_unreachable_phases` from `flow_validation`.
- Remove `component_id` param from `list_flows`, `get_flow_detail`,
  `create_flow`, `update_flow`, `delete_flow`, `list_flow_schedules`,
  `set_flow_schedule`, `remove_flow_schedule`. Scheduler `target.componentId` is
  hardcoded to `keboola.flow`.
- `create_flow` / `update_flow`: run `validate_conditional_flow` on the
  (merged, for update) phases+tasks; on errors raise
  `KeboolaApiError(error_code=ErrorCode.INVALID_FLOW_DEFINITION, status_code=400)`
  with all messages joined. Unreachable-phase warnings are attached to the
  returned dict (`warnings: [...]`), not raised.
- `list_flows`: single-component listing; additionally count
  `keboola.orchestrator` configs per project (still a `list_component_configs`
  call, 404 → 0) and return `legacy_orchestrator_count` (per project + total) so
  the CLI can warn (D2). The orchestrator configs themselves are **not** added
  to the `flows` array.
- Error mapping: surface the CF-disabled project error
  (`conditional_flows=false`) as a clear, actionable message (D1).

### 4.3 `commands/flow.py` changes

- Drop `--component-id` from all 8 subcommands; drop `_FLOW_COMPONENT_CHOICES`.
- Replace `_FLOW_SCHEMA` with a CF YAML template (string ids) demonstrating: 2
  phases with a conditional `next` (`ANY_TASKS_IN_PHASE` failure check + default
  transition), a `job` task with `retry`, a `notification` task, a `variable`
  task.
- `flow schema [--full]` (D12): default prints the YAML template; `--full`
  dumps the bundled JSON schema verbatim (rich JSON syntax for humans, parsed
  object for `--json`).
- **New** `flow validate (--file @flow.yaml | -)` (D7, D8): offline validation;
  loads YAML/JSON, calls `validate_conditional_flow` + `find_unreachable_phases`;
  exit 0 if no errors (warnings still printed), exit 2 if errors; `--json` lists
  `{errors: [...], warnings: [...], valid: bool}`.
- `flow detail` human rendering rewrite (D11): per-phase transition list
  (`→ goto [condition summary | default]`), task-type badges
  (`job`/`notification`/`variable`), retry info. JSON unchanged.
- `flow list` human + JSON: surface `legacy_orchestrator_count` as a
  `formatter.warning` line (human) and a key in the JSON payload.

### 4.4 `permissions.py`

Add `"flow.validate": "read"` to the operation registry (next to the other
`flow.*` entries).

### 4.5 REST (`server/routers/flows.py`)

Drop `component_id` from `FlowCreate`/`FlowUpdate`/`FlowSchedule` and from the
query params on `detail`/`delete`/`list_schedules`/`remove_schedule`; drop the
now-unused `DEFAULT_FLOW_COMPONENT` plumbing. Paths unchanged (D14). No `validate`
REST endpoint in v1 (offline CLI-only; can be added later if needed — YAGNI).

### 4.6 Packaging

The bundled JSON must actually ship in the wheel (same class of problem as
`_ui_dist`). Since `conditional-flow-schema.json` lives **inside** the package
tree (`src/keboola_agent_cli/resources/`) and is **not** gitignored, hatchling's
default wheel collection includes it — **no `force-include` needed**. The plan
must nonetheless **verify** this by building the wheel and asserting the JSON is
present (`unzip -l dist/*.whl | grep conditional-flow-schema.json`), and add the
`resources/` dir to the sdist `include` list if not already covered by `src/`
(it is — sdist includes `src/`). A unit test also calls
`load_conditional_flow_schema()` to catch a missing-resource regression.

## 5. Data flow

```
flow new --file @flow.yaml
  → commands/flow._load_flow_yaml → {phases, tasks}
  → FlowService.create_flow
       → flow_validation.validate_conditional_flow (schema + semantic)  [errors → INVALID_FLOW_DEFINITION]
       → flow_validation.find_unreachable_phases (warnings)
       → client.create_config(component_id="keboola.flow", {phases, tasks})
  → result {id, name, warnings?}

flow validate --file @flow.yaml   (offline)
  → _load_flow_yaml → validate_conditional_flow + find_unreachable_phases
  → exit 0 (+warnings) | exit 2 (errors)
```

## 6. Error handling

- Structural + semantic validation failures →
  `INVALID_FLOW_DEFINITION` (replaces `INVALID_FLOW_DAG`), status 400,
  non-retryable, all messages joined.
- CF-disabled project → mapped to an actionable message (D1); E2E skips when a
  project reports `conditional_flows=false`.
- YAML/JSON parse errors → `VALIDATION_ERROR`, exit 2 (existing behavior).
- Reachability issues → **warnings**, never block.

## 7. Testing

- **`tests/test_flow_validation.py` (new):** valid CF fixture; missing default
  transition; unknown `goto`; task → missing phase; phase with no enabled task;
  duplicate phase/task ids; operand-arity violations (each operator class);
  notification + variable task shapes; unreachable-phase warning; `goto` loop is
  **legal** (no error); string-id fixtures only.
- **`tests/test_flow_service.py` (rewrite):** CF payloads, no `component_id`
  args, `INVALID_FLOW_DEFINITION`, `legacy_orchestrator_count`, merge-aware
  update validation. Remove all `dependsOn` fixtures.
- **`tests/test_flow_cli.py` (rewrite):** new `flow validate`, `flow schema
  --full`, dropped `--component-id`, detail human rendering. Remove `dependsOn`.
- **`tests/test_e2e.py`:** full round-trip create → detail → update → schedule →
  schedule-remove → delete + `flow validate` against a CF-enabled project; skip
  with a clear reason when `conditional_flows=false`.
- A unit assertion that `load_conditional_flow_schema()` succeeds (packaging
  regression guard).

## 8. Cleanup sweep

- Delete dead `ORCHESTRATOR_COMPONENTS` from `sync/config_format.py` (verified
  unused repo-wide).
- `services/component_service.py`: `_FLOW_COMPONENT_IDS` /
  `_build_flow_config_yml` still reference orchestrator for the `config new`
  scaffold path — update the flow scaffold to emit a CF skeleton (string ids,
  `phases`/`tasks` with a `job` task) and default component `keboola.flow`;
  remove the orchestrator default.
- Repo-wide `keboola.orchestrator` grep: update comments/help in `context.py`,
  `commands/flow.py`, docs. The sync engine itself is unaffected (flow payload
  round-trips via `_configuration_extra`).

## 9. Docs & plugin sync (convention #17 — all mandatory, silent-drift)

`CLAUDE.md` `## All CLI Commands` flow block; `commands/context.py`
`AGENT_CONTEXT`; `plugins/kbagent/agents/keboola-expert.md` (version gate + tool
matrix); `SKILL.md` + `references/commands-reference.md`; full rewrite of
`references/flow-workflow.md` (CF template, conditions cookbook, validate-before-push
loop, `job run --component-id keboola.flow` to execute); `references/gotchas.md`
new entries tagged `(since v0.56.0)` (orchestrator dropped, `--component-id`
removed, old `dependsOn` template invalid, `INVALID_FLOW_DAG` →
`INVALID_FLOW_DEFINITION`, **string ids**), and mark the old default-component
gotcha resolved; `README.md` if flows mentioned.

## 10. Release

Bump `pyproject.toml` → `0.56.0`; add `changelog.py` entry with an explicit
**breaking-change** callout (orchestrator dropped, `--component-id` removed,
`INVALID_FLOW_DAG` → `INVALID_FLOW_DEFINITION`, CF schema validation, string
ids); `make version-sync`; `make check`; `make test-e2e`.

## 11. Out of scope

- Migration tooling `keboola.orchestrator` → `keboola.flow`.
- A `flow run` command (CF executes via `job run --component-id keboola.flow`).
- CF awareness in `lineage` / `schedule find` beyond existing passthrough.
- A `validate` REST endpoint (offline CLI only for v1).
