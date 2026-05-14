# Agent Tasks Workflow -- Schedule AI agents inside `kbagent serve`

Cron-scheduled, manual, or chained tasks that run inside a long-lived
`kbagent serve` process. Three action flavours: `mcp_tool`, `cli_command`,
`ai_agent`. Persisted run history with cost/token accounting and live SSE
replay. Available since v0.40.0.

The Web UI (`kbagent serve --ui`, sidebar "Agent Tasks") is the primary
authoring surface. Everything below is the REST API the UI talks to --
use it from scripts, other AI agents, or `kbagent http` subprocesses.

## Prerequisites

```bash
# One-time install with server extras
uv tool install --with 'keboola-agent-cli[server]' \
  'git+https://github.com/padak/keboola_agent_cli'

# Run (keep this terminal open; scheduler dies when the server stops)
kbagent serve --ui
# Prints URL + bearer token on startup.
```

Two ways to call the REST API:

- **From an interactive shell**: export `KBAGENT_SERVE_URL` (the printed URL)
  and `KBAGENT_SERVE_TOKEN` (the printed bearer) yourself, then use
  `kbagent http ...` or curl.
- **From a scheduled task subprocess**: both env vars are auto-injected by
  the scheduler into `cli_command` and `ai_agent` action types. You can
  call back into the same server without re-authentication.

All endpoints below are relative to the server URL. The bearer token is
sent as `Authorization: Bearer <token>` by `kbagent http`.

## The three action flavours

Every task has an `action` block with `type` + `params`:

```json
// type: "mcp_tool" -- call any keboola-mcp-server tool
{
  "type": "mcp_tool",
  "params": {
    "tool": "get_jobs",
    "project": "padak",
    "input": {"status": "error", "limit": 50}
  }
}

// type: "cli_command" -- spawn kbagent <argv>; subprocess inherits
// KBAGENT_CONFIG_DIR + KBAGENT_SERVE_URL + KBAGENT_SERVE_TOKEN.
{
  "type": "cli_command",
  "params": {
    "argv": ["job", "list", "--project", "padak", "--status", "error"],
    "timeout": 300
  }
}

// type: "ai_agent" -- spawn an AI CLI with a prompt; subprocess gets the
// same env injection, so the agent can call kbagent http get/post/...
// against the live serve instead of forking fresh CLI processes.
{
  "type": "ai_agent",
  "params": {
    "cli": "claude",
    "prompt": "Check last night's failed jobs and post a summary.",
    "extra_args": ["--print"],
    "timeout": 600
  }
}
```

`cli` accepts `claude`, `codex`, or `gemini`. The chosen CLI must be on
the server's `PATH`.

## CRUD over REST

### List tasks

```bash
kbagent http get /agents
# Returns: array of AgentTask objects (id, name, cron, manual, enabled,
# action, trigger, created_at, last_run_at, next_run_at).
```

### Create a CRON-scheduled task

```bash
kbagent http post /agents --body '{
  "name": "Storage Cleanup Advisor",
  "description": "Weekly orphan-table scan + savings estimate",
  "cron": "0 6 * * 1",
  "manual": false,
  "enabled": true,
  "action": {
    "type": "ai_agent",
    "params": {
      "cli": "claude",
      "prompt": "You are an unattended Storage Cleanup Advisor for project padak. Scan tables not referenced for > 90 days, estimate monthly Snowflake savings ($23/TB), and write a Markdown report grouped by safe-to-delete / candidate-to-archive / verify-first.",
      "timeout": 900
    }
  }
}'
```

### Create a manual-only task (triggered by hand or by another task)

```bash
kbagent http post /agents --body '{
  "name": "Job Repair Agent",
  "description": "Remediation agent triggered after Failed-Jobs Triage",
  "cron": "0 * * * *",
  "manual": true,
  "action": {
    "type": "ai_agent",
    "params": {
      "cli": "claude",
      "prompt": "You are a remediation agent. Read KBAGENT_UPSTREAM_RUN_ID via the kbagent HTTP API and propose fixes for the failed jobs identified upstream."
    }
  }
}'
```

`manual: true` skips the cron loop entirely -- the task only fires via
`POST /agents/{id}/run` or as a chained downstream. Keep `cron` set so
the operator can flip back to scheduled mode without losing the schedule.

### Edit a task (PATCH)

```bash
kbagent http patch /agents/<task_id> --body '{"enabled": false}'
# Any AgentTask field can be patched; omit fields you don't want to touch.
```

### Run a task on demand

```bash
# Fire-and-forget: response returns the run_id immediately, scheduler
# captures output to disk.
kbagent http post /agents/<task_id>/run --body '{}'

# Live stream (SSE): each step (stdout / stderr / tool calls for ai_agent)
# arrives as an event. Useful for the UI's "Run live" button.
kbagent http post /agents/<task_id>/run/stream --body '{}'

# Pass a runtime prompt override (ai_agent only):
kbagent http post /agents/<task_id>/run --body '{
  "prompt": "Focus only on the snowflake-transformation component today."
}'
```

### Inspect run history

```bash
kbagent http get /agents/<task_id>/runs
# Returns: most recent runs (id, started_at, ended_at, status, summary,
# error). summary is null for cli_command/mcp_tool; populated for ai_agent.

kbagent http get /agents/<task_id>/runs/<run_id>
# Returns: full AgentRun including output payload.

kbagent http get /agents/<task_id>/runs/<run_id>/events
# Returns: per-step event timeline (the same JSONL persisted at
# agent_runs/<task_id>/<run_id>.jsonl). Used for replay in the UI.
```

### Delete a task

```bash
kbagent http delete /agents/<task_id>
# Run history is preserved (the agent_runs/ directory is not touched).
```

## Chained triggers

The `trigger` field on a task chains a downstream task after the upstream
finishes. The downstream's subprocess inherits `KBAGENT_UPSTREAM_TASK_ID`
and `KBAGENT_UPSTREAM_RUN_ID` env vars, so an `ai_agent` downstream can
read the upstream run via `kbagent http get /agents/$UP/runs/$RUN`:

```bash
# Upstream: Failed-Jobs Triage (cron 08:00 daily)
kbagent http post /agents --body '{
  "name": "Failed Jobs Triage",
  "cron": "0 8 * * *",
  "action": {"type": "ai_agent", "params": {"cli": "claude", "prompt": "..."}},
  "trigger": {"on": "success", "task_id": "<job-repair-agent-id>"}
}'
```

`on` accepts `success`, `error`, or `always`. Triage success → Repair runs.

## Helper endpoints

```bash
# Validate a cron expression (also returns the next 5 fire times)
kbagent http get "/agents/cron/preview?cron=0+6+*+*+1"

# Dry-run an action without persisting a task (UI's "Test run" button)
kbagent http post /agents/test --body '{
  "action": {"type": "cli_command", "params": {"argv": ["project", "list"]}}
}'

# AI-assisted prompt improvement (streams suggestions via SSE)
kbagent http post /agents/prompt/improve/stream --body '{
  "prompt": "Check storage usage"
}'
```

## Discovery

When in doubt, fetch the OpenAPI:

```bash
kbagent http get /openapi.json
```

The full server contract (paths, request/response schemas, every endpoint
the UI uses) lives there. This is the authoritative source -- this file
documents the agent-tasks subset.

## Lifecycle gotchas

- **Scheduler dies with the server.** Closing the `kbagent serve` terminal
  stops the cron loop. Run history persists on disk (`~/.config/keboola-agent-cli/agent_runs/`),
  but scheduled triggers are paused until the server restarts.
- **`manual: true` tasks ignore `cron`** entirely. Their schedule is
  preserved for the operator's reference but never fires automatically.
- **`ai_agent` subprocesses get a privileged env**. `KBAGENT_SERVE_TOKEN`
  lets them call back into the same server, so an agent prompt with
  `kbagent http delete /agents/...` can wipe other tasks. Sandbox AI
  prompts accordingly; the same boundary you would draw around any agent
  with shell access applies here.
- **One run per task at a time.** Starting a second run while one is
  active returns HTTP 409. Use `POST /agents/{id}/run/stream` from the UI
  to follow the active run instead.
- **Cost reporting needs an AI CLI that emits structured output.** Token
  totals + per-tool counts come from claude / codex / gemini stream-json;
  raw `cli_command` runs have `summary: null` (just stdout / stderr / exit).
