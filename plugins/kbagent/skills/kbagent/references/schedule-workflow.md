# Schedule Discovery & Audit Workflow

This guide covers fleet-wide discovery and auditing of cron schedules across one or many Keboola projects. Use `kbagent schedule` when you need to answer questions like *"which flows across all our projects are scheduled on cron triggers?"* without enumerating each project by hand.

For the per-flow schedule CRUD (attach / detach / update a single schedule) see [`flow-workflow.md`](flow-workflow.md). The two topic areas complement each other: `flow schedule` writes individual schedules, `schedule list/detail/find` read them in bulk.

## What is a schedule in Keboola?

Schedules are stored as ordinary Storage API configurations of the `keboola.scheduler` component. Each scheduler config has a `target` (the component+config it triggers) and a `schedule` (cron + timezone + state). There is **no separate scheduler service HTTP client** to talk to -- everything goes through the same Storage API kbagent already uses for configs. A real schedule body looks like:

```json
{
  "configuration": {
    "target": {
      "mode": "run",
      "componentId": "keboola.flow",
      "configurationId": "01kmjawd6w80vn2rgh6yeaa12r"
    },
    "schedule": {
      "cronTab": "0 6 * * *",
      "timezone": "Europe/Prague",
      "state": "enabled"
    }
  }
}
```

Notice that `state` is a **string** (`"enabled"` / `"disabled"`), not a nested object. kbagent handles both shapes, but the canonical form is the string.

## Quick start -- answer "which flows are on cron?"

```bash
# Fleet-wide listing (every project kbagent knows about)
kbagent --json schedule list

# Narrow to a subset of projects
kbagent --json schedule list --project prod --project staging

# Only enabled schedules (exclude paused ones)
kbagent --json schedule list --enabled-only
```

Each row has every field you need for a spreadsheet or dashboard:

```json
{
  "project_alias": "prod",
  "schedule_id": "01kpx6zv0krbp05gh7eb0dzd5y",
  "schedule_name": "1stFlow (Schedule)",
  "parent_component_id": "keboola.flow",
  "parent_config_id": "01kmjawd6w80vn2rgh6yeaa12r",
  "parent_name": "1stFlow",
  "cron": "0 6 * * *",
  "timezone": "Europe/Prague",
  "enabled": true
}
```

`schedule list` returns every `keboola.scheduler` config regardless of its target component, so legacy schedules may still show `"parent_component_id": "keboola.orchestrator"` -- those targets are no longer manageable via `kbagent flow` (orchestrator support was dropped in v0.57.0), but the schedules themselves list and audit normally.

## Inspect a single schedule

```bash
kbagent --json schedule detail \
  --project prod \
  --schedule-id 01kpx6zv0krbp05gh7eb0dzd5y
```

The detail view adds the raw `configuration` body, `version`, `created`, and `change_description`. Orphaned schedules (parent config was deleted after the schedule was created) still return cleanly -- `parent_name` will simply be `""`.

## Audit workflows -- `schedule find`

`schedule find` is the audit command. Two filters are supported and combine with AND:

### `--cron-window "HH:MM-HH:MM"` -- "find the night-time batch jobs"

```bash
# Every schedule that fires between 02:00 and 04:00 across every project
kbagent --json schedule find --cron-window "02:00-04:00"
```

The matcher is an **hour-field approximation**: it parses only the third cron field (hour). That is sufficient for operational audits (*"which jobs are running overnight?"*) and keeps the CLI dependency-free. Cron expressions whose hour field is `*` (every hour) are treated as **not confined** to any bounded window -- they do not match, even if minute restrictions would technically limit them. See the [gotchas](gotchas.md#schedule-find-cron-window-is-an-hour-field-approximation) for the full rationale.

### `--not-run-since N` -- "find schedules whose parent hasn't run in a while"

```bash
# Schedules whose parent config has no job in the last 90 days (or never ran)
kbagent --json schedule find --not-run-since 90
```

The service fetches the latest job for each parent (single `list_jobs(limit=1)` call per unique parent config) and compares against the `startTime` / `createdTime`. A parent with no job history at all counts as stale -- that is usually what you want when auditing abandoned schedules.

**Branch-awareness caveat:** the Queue API has no branch parameter, so `schedule find --branch <DEV_ID> --not-run-since N` still compares against *production* jobs. Dev branches with freshly-deployed configs will appear stale even if they ran on main moments ago. If you need per-branch job history, drop `--not-run-since` and script the Queue API with `job list` scoping.

### Combined filters

```bash
# Night-time schedules that also haven't run in the last 30 days
kbagent --json schedule find \
  --cron-window "00:00-05:00" \
  --not-run-since 30
```

### No filters -- base listing with unevaluated audit columns

```bash
# Same rows as 'schedule list' plus two audit columns left null
kbagent --json schedule find
```

Every response row from `schedule find` always carries `last_run_at` and `matches_cron_window`, but they are populated **only when the corresponding filter is active**:

| Filter state | `last_run_at` | `matches_cron_window` |
|---|---|---|
| No filters | `null` | `null` |
| `--cron-window ...` | `null` | `true` / `false` |
| `--not-run-since N` | ISO timestamp / `null` | `null` |
| Both filters | ISO timestamp / `null` | `true` / `false` |

This keeps the response schema stable (JSON consumers can rely on the keys being present) while avoiding N extra Queue API calls per project just to populate columns nobody asked for. If you want `last_run_at` populated on every row *without* applying a staleness filter, pass `--not-run-since 0` -- that fires the lookup for every row (caveat: N extra API calls per project) but includes every row in the result.

LLM/agent callers: do not treat `matches_cron_window: true` as an affirmative signal unless you also see `filters.cron_window` populated in the response envelope -- otherwise the column was never evaluated.

## `flow list --with-schedules` -- flow-centric view

If you prefer the **flow-centric** view (flows with their schedules attached as an inline list) use `flow list --with-schedules`:

```bash
kbagent --json flow list --with-schedules --project prod
```

Each flow row gains a `schedules: [{schedule_id, cron, timezone, enabled}, ...]` key. Flows without a schedule get `schedules: []`. Under the hood this is **one** extra `list_component_configs(keboola.scheduler)` call per project, not per flow -- the join happens in memory by `(parent_component_id, parent_config_id)`.

Prefer `schedule list` when you care about schedules (e.g. "how many cron-triggered jobs run between 2-4am across 14 projects?"). Prefer `flow list --with-schedules` when you care about flows (e.g. "list every flow in prod and tell me which ones are triggered on cron").

## Error handling and multi-project semantics

- All three commands fan out across projects in parallel. Per-project errors are collected into `data.errors[]` without aborting -- one broken token doesn't blind you to the rest of the fleet.
- `schedule detail` on a single project follows the standard single-project write/detail pattern: `KeboolaApiError` exits with the standard code (1/3/4), `ConfigError` exits 5.
- `schedule find` validates `--cron-window` and `--not-run-since` at the service boundary and raises `ConfigError` (exit 5) on malformed input before any API calls.
- **`_fetch_latest_job_ts` silently returns `None` on Queue API errors.** This is intentional for the audit use-case (`None` = stale, matching the "never ran" semantics), but it means a permission issue on Queue API for one project is invisible in the `errors[]` envelope -- every schedule in that project will just look stale. If you audit results show a suspiciously uniform "never ran" cluster in one project, run `kbagent job list --project <alias>` to check whether the token even has Queue API read access.

## Payload cost -- one big API call per project

Under the hood `schedule list` and `schedule find` each call `list_components_with_configs(branch_id=...)` once per project. That endpoint returns **every** component's configs + rows + full configuration bodies, not just `keboola.scheduler`. For a 50-component x 5-config project this is 250 configurations on the wire per project, just to extract a handful of schedules and parent names.

Why not hit `list_component_configs("keboola.scheduler")` instead (much smaller response)? Because we'd still need `get_config_detail` per unique parent to resolve `parent_name`, which turns the O(projects) round-trip into O(projects + unique-parents). For projects with a large `keboola.scheduler` surface this is usually worse, not better; for projects with many components but few schedules it is faster but more complex. The current implementation optimises for **call count** at the cost of **payload size**.

Practical implications:

- Audits against 14 projects with moderate component counts finish in a few seconds -- no action needed.
- If you hit memory pressure (tens of thousands of configs per project, unusual) consider splitting the audit per-project via `--project X --project Y` calls.
- `flow list --with-schedules` uses the *lighter* `list_component_configs("keboola.scheduler")` path precisely because it only needs the scheduler bodies (flows are already enumerated separately) -- so prefer that view when you don't need the parent-name join that only scheduler-side audits require.

## Permissions

All three schedule commands are categorized as **read** in `permissions.py`:

```
schedule.list   -> read
schedule.detail -> read
schedule.find   -> read
```

They are safe to run under `--deny-writes` and `--deny-destructive`.

## When to use which tool

| Question | Command |
|---|---|
| "How many cron schedules exist in project X?" | `kbagent schedule list --project X` |
| "Which configs across all 14 projects run at 03:00?" | `kbagent schedule find --cron-window "03:00-03:00"` |
| "Which scheduled parents haven't run in 90 days?" | `kbagent schedule find --not-run-since 90` |
| "What schedules are attached to this flow?" | `kbagent flow list --with-schedules --project X` (flow-centric) or `kbagent schedule list --project X` then grep |
| "Full detail for schedule SC1 including parent name?" | `kbagent schedule detail --project X --schedule-id SC1` |
| "Add a new cron schedule to flow F" | `kbagent flow schedule --project X --flow-id F --cron "0 6 * * *"` (see [flow-workflow.md](flow-workflow.md)) |
| "Remove every schedule attached to flow F" | `kbagent flow schedule-remove --project X --flow-id F --yes` |
