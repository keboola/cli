# Agent Tasks Workflow (CLI)

Cron-scheduled, manual, or chained tasks defined locally on disk via the
`kbagent agent` CLI. Same on-disk format the in-process scheduler inside
`kbagent serve` reads, so a CLI-created task fires on its cron as soon as
the server is running. Available since v0.42.0.

For the **REST API** form (what `kbagent serve` exposes for AI-agent
subprocesses to call back into) see
[agent-tasks-rest-workflow](agent-tasks-rest-workflow.md). Both surfaces
share the AgentTask / AgentAction schema -- pick whichever fits the
caller.

## When to use which surface

- **`kbagent agent ...` (this file)** -- authoring from a terminal, CI
  pipelines, scripts, or one-shot manual runs. Works offline; the
  scheduler still needs `kbagent serve` running for the cron loop, but
  CRUD + ad-hoc runs do not.
- **`kbagent http <verb> /agents...`** -- from inside a scheduled
  `ai_agent` subprocess that wants to call back into its own serve
  (env vars `KBAGENT_SERVE_URL` + `KBAGENT_SERVE_TOKEN` are auto-injected).
- **Web UI** (`kbagent serve --ui`, sidebar "Agent Tasks") -- preferred
  for human authoring; calls the same REST endpoints.

## Prerequisites

```bash
# Install (no extras required for the agent CLI; croniter is core since 0.42.0)
uv tool install 'git+https://github.com/keboola/cli'

# Optional: start serve so the cron loop actually fires the tasks
# (the CLI commands work without it, but tasks won't auto-run on schedule).
kbagent serve --ui
```

## Three action flavours

Every task carries an `action` envelope with `type` + `params`:

```jsonc
// type: "mcp_tool" -- call any keboola-mcp-server tool
{ "type": "mcp_tool",
  "params": { "tool": "get_jobs", "project": "padak",
              "input": {"status": "error", "limit": 50} } }

// type: "cli_command" -- spawn `kbagent <argv>` subprocess
{ "type": "cli_command",
  "params": { "argv": ["job", "list", "--project", "padak", "--status", "error"],
              "timeout": 300 } }

// type: "ai_agent" -- spawn an AI CLI (claude/codex/gemini) with a prompt
{ "type": "ai_agent",
  "params": { "cli": "claude",
              "prompt": "Summarise last night's failed jobs.",
              "extra_args": ["--print"],
              "timeout": 600 } }
```

`cli` accepts `claude`, `codex`, or `gemini`. The chosen CLI must be on
the server's `PATH` when the task fires (cron or `agent run`).

## Common workflows

### Create + inspect + run

The convenience flags cover the typical single-action case. For complex
payloads, use `--from-file PATH|-` with the full JSON envelope.

```bash
# (1) Scheduled cli_command -- daily 06:00 health check
kbagent agent create \
  --name "Daily Health Check" \
  --description "Verify project tokens + list error jobs" \
  --cron "0 6 * * *" \
  --type cli_command \
  --argv doctor

# (2) Manual ai_agent -- ad-hoc storage cleanup advisor
kbagent agent create \
  --name "Storage Cleanup Advisor" \
  --manual \
  --type ai_agent \
  --cli claude \
  --prompt "You are an unattended Storage Cleanup Advisor for project padak. Scan tables not referenced for > 90 days, estimate monthly Snowflake savings ($23/TB), and write a Markdown report grouped by safe-to-delete / candidate-to-archive / verify-first." \
  --timeout 900

# (3) From a JSON file (preferred for prompts > a few lines)
kbagent agent create \
  --name "Weekly Triage" \
  --cron "0 8 * * 1" \
  --from-file @task.action.json

# List + show what was registered
kbagent agent list
kbagent agent show <task_id>
```

> **ID forms (since v0.44.0):** every subcommand that takes a task/run ID
> accepts it positionally (`agent show <task_id>`) or via a named flag
> (`--id` / `--task-id`, plus `--run-id` for `run-detail` / `run-events`) --
> matching the rest of the CLI (`--job-id`, `--config-id`, ...). Examples
> below use the positional form for brevity.

### Run-on-demand (blocking + streaming)

```bash
# Blocking: print the AgentRun record once the action finishes.
kbagent agent run <task_id>

# Live event stream (one line per event in human mode, NDJSON in --json).
kbagent agent run <task_id> --stream

# Manual tasks accept ad-hoc runtime input merged with the persisted prompt.
kbagent agent run <task_id> --runtime-prompt "Focus only on prod-snowflake-etl."

# Or full JSON merge (mcp_tool / cli_command also supported).
kbagent agent run <task_id> --runtime-input '{"prompt": "Today only."}'
```

### Update + disable + delete

```bash
# Toggle enabled/disabled (cron loop respects --enabled, --disabled).
kbagent agent update <task_id> --disabled

# Flip a cron task to manual (preserves cron for later re-enable).
kbagent agent update <task_id> --manual

# Change the cron expression (next_run_at is recomputed automatically).
kbagent agent update <task_id> --cron "0 5 * * 1-5"

# Remove a chained downstream trigger.
kbagent agent update <task_id> --clear-trigger

# Permanent deletion (run history on disk is preserved).
kbagent agent delete <task_id> --yes
```

### Run history

```bash
# Most-recent first, default limit 50.
kbagent agent runs <task_id>

# A single run record (status, summary, output, error).
kbagent agent run-detail <task_id> <run_id>

# Per-event timeline -- only ai_agent runs from v0.10+ carry one.
kbagent agent run-events <task_id> <run_id>
```

### Test an action before saving

```bash
# Same dispatcher as a real run, but nothing is written to agents.json.
kbagent agent test --type cli_command --argv project --argv list

# With live streaming for ai_agent previews.
kbagent agent test --type ai_agent --cli claude --prompt "Hello" --stream
```

### Cron preview + prompt helper

```bash
# Validate a cron expression and see the next 5 firings (UTC).
kbagent agent cron-preview --cron "0 6 * * 1" --count 5

# Polish a plain-English goal into a polished single-shot prompt.
kbagent agent prompt-improve \
  --goal "Audit last week's failed jobs and post a Slack-friendly summary" \
  --cli claude --project padak --stream
```

## Chained triggers

`--trigger-task-id` chains a downstream task. The downstream's
subprocess inherits `KBAGENT_UPSTREAM_TASK_ID` + `KBAGENT_UPSTREAM_RUN_ID`
env vars so an `ai_agent` downstream can read the upstream run via
`kbagent http get /agents/$UP/runs/$RUN` (only available when the
downstream runs inside `kbagent serve`).

```bash
# Repair runs only when the triage upstream succeeds.
kbagent agent create \
  --name "Failed Jobs Triage" \
  --cron "0 8 * * *" \
  --type ai_agent --cli claude --prompt "..." \
  --trigger-task-id <repair-task-id> \
  --trigger-on success
```

`--trigger-on` accepts `success` (default), `error`, or `always`.

## Output modes

- **Human** -- Rich tables, panels, syntax-highlighted JSON for action
  payloads.
- **`--json`** -- canonical envelope (`{"status": "ok", "data": ...}`)
  for scripted use. Errors land as `{"status": "error", "error":
  {"code": ..., "message": ...}}` with stable codes (`NOT_FOUND`,
  `CONFIG_ERROR`, `VALIDATION_ERROR`, `MISSING_PARAMETER`).
- **`--stream`** -- per-event output, one line per event. NDJSON in
  `--json` mode (each event is a self-contained JSON object).

## Lifecycle gotchas

- **The cron loop only runs inside `kbagent serve`.** The CLI commands
  read/write the same `agents.json`, but cron-firing requires the live
  scheduler. Tasks created via CLI sit dormant until serve starts.
- **`agent run` does not consult cron.** It dispatches immediately and
  fans out the chained downstream (if any) regardless of cron schedule.
- **One run per task at a time.** While a task is running, a second
  `agent run` errors out (the runner refuses concurrent dispatch).
- **`--runtime-prompt` is ai_agent only.** For cli_command use
  `--runtime-input '{"argv": ["--extra", "flag"]}'`; for mcp_tool use
  `--runtime-input '{"key": "value"}'`. Merge semantics match the REST
  endpoint exactly (`merge_runtime_input` is the shared helper).
- **Cron expressions are UTC.** No per-task timezone override (yet);
  use `cron-preview` to translate.
