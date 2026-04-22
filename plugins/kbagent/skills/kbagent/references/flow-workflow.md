# Flow Workflow

Flows orchestrate Keboola transformations and extractors in a directed acyclic graph (DAG) of phases and tasks. kbagent supports two flow component types: `keboola.orchestrator` (classic) and `keboola.flow` (new format).

## Core concepts

- **Phase**: a named stage with `id` and `dependsOn` (list of upstream phase IDs). Phases with no `dependsOn` run first.
- **Task**: a unit of work referencing a component config, assigned to a phase via `phase` field.
- **Schedule**: stored as a `keboola.scheduler` config that targets the flow; not part of the flow config itself.

## Quick start: create a flow

```bash
# 1. See the template
kbagent flow schema

# 2. Create a simple flow from YAML
cat > flow.yaml <<'EOF'
phases:
  - id: 1
    name: Extract
    dependsOn: []
  - id: 2
    name: Transform
    dependsOn: [1]
tasks:
  - id: 1
    name: Run extractor
    phase: 1
    task:
      mode: run
      componentId: keboola.ex-db-snowflake
      configId: "123456"
  - id: 2
    name: Run transformation
    phase: 2
    task:
      mode: run
      componentId: keboola.snowflake-transformation
      configId: "789012"
EOF

kbagent --json flow new --project prod --name "Daily ETL" --file @flow.yaml
```

## List and inspect flows

```bash
# All flows across all projects
kbagent --json flow list

# Flows in one project
kbagent --json flow list --project prod

# Full phase/task breakdown
kbagent --json flow detail --project prod --flow-id 111
```

## Update a flow

```bash
# Rename only
kbagent --json flow update --project prod --flow-id 111 --name "New Name"

# Replace phases/tasks from file (validates DAG before write)
kbagent --json flow update --project prod --flow-id 111 --file @updated.yaml
```

## Schedule a flow

Schedules are stored as `keboola.scheduler` configs pointing at the flow. kbagent creates one per `flow schedule` call.

```bash
# Daily at 06:00 UTC
kbagent --json flow schedule --project prod --flow-id 111 --cron "0 6 * * *"

# With timezone and disabled state
kbagent --json flow schedule \
  --project prod --flow-id 111 \
  --cron "0 8 * * 1-5" \
  --timezone "Europe/Prague" \
  --disabled

# Remove all schedules (idempotent)
kbagent --json flow schedule-remove --project prod --flow-id 111 --yes
```

## Delete a flow

```bash
kbagent --json flow delete --project prod --flow-id 111 --yes
```

## DAG validation

kbagent validates the phase graph client-side before every create/update:
- Unknown `dependsOn` phase IDs → `INVALID_FLOW_DAG`
- Tasks referencing unknown phase IDs → `INVALID_FLOW_DAG`
- Cycles in the phase graph → `INVALID_FLOW_DAG`

The error carries a list of human-readable violation messages.

## Component IDs

| Component | Use case |
|---|---|
| `keboola.flow` | New projects, preferred for new flows (default for `flow new`) |
| `keboola.orchestrator` | Legacy flows; most existing orchestrations use this (default for `flow detail/update/delete/schedule`) |

Both are fully supported. Use `--component-id` to override the default.
