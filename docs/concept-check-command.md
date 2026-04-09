# Concept: `kbagent check` - Platform Observability & Anomaly Detection

## Problem Statement

Users have Keboola projects running dozens of components on schedules. They need to
know when something goes wrong — not just "a job failed", but patterns like:

- "Credits consumption doubled this week vs. last week"
- "This extractor is getting slower every day"
- "Data imports dropped 80% on Tuesday — pipeline is broken"
- "Betka from Marketing is firing prompts with 30 iterations, costing $420/day vs usual $15"

Today they write SQL transformations for detection, then manually investigate in GCP
Cost Explorer or Keboola UI. The painful part is the investigation — understanding WHY
something changed.

## Core Idea: Three Phases, AI Only Where Needed

```
┌────────────────────────────────────────────────────────────────┐
│  PHASE 1: DEFINE (human + AI, one-time)                        │
│                                                                │
│  Vojta + Claude Code pick from templates, customize thresholds │
│  → .kbagent/checks/credit-spike.yaml                          │
│  → kbagent check push → Keboola Storage Files (permanent)      │
│                                                                │
│  AI is involved HERE. After this, it's pure automation.        │
└────────────────────────┬───────────────────────────────────────┘
                         │
┌────────────────────────▼───────────────────────────────────────┐
│  PHASE 2: RUN (automated, NO AI, deterministic)                │
│                                                                │
│  kbagent check run --all                                       │
│                                                                │
│  1. Fetch job history + storage events via Keboola APIs        │
│  2. Load into in-memory DuckDB                                 │
│  3. Execute check SQL queries                                  │
│  4. Evaluate pass/fail                                         │
│  5. On failure: produce structured briefing JSON               │
│                                                                │
│  No AI. No cloud compute. No workspaces. Pure SQL on metadata. │
└────────────────────────┬───────────────────────────────────────┘
                         │
                ┌────────┴────────┐
                │  All passed?     │
                │  YES → log, done │
                │  NO ↓            │
                └────────┬────────┘
                         │
┌────────────────────────▼───────────────────────────────────────┐
│  PHASE 3: INVESTIGATE (AI picks up the briefing)               │
│                                                                │
│  The briefing JSON contains:                                   │
│  - What check failed and why (violations data)                 │
│  - Historical context (last 14 days of the metric)             │
│  - Investigation hints (what to look at next)                  │
│  - Related API calls to make for drill-down                    │
│                                                                │
│  WHO investigates (any of these):                              │
│  - Vojta + Claude Code (interactive)                           │
│  - Claude Code /schedule agent (automated)                     │
│  - KAI bot in Keboola (platform-native)                        │
│  - Claude Agent SDK app (custom)                               │
│                                                                │
│  AI reads the briefing and runs further kbagent commands        │
│  to drill down into root cause.                                │
└────────────────────────────────────────────────────────────────┘
```

## Design Principles

- **Checks are code**: YAML files derived from templates, version-controlled, reviewable
- **No AI at runtime**: check execution is deterministic SQL on DuckDB. Zero LLM calls.
- **No cloud compute**: all analysis runs locally on API metadata via DuckDB. No workspaces.
- **One SQL dialect**: DuckDB (PostgreSQL-compatible) regardless of project backend
- **Briefing as handoff**: when a check fails, it produces a self-contained JSON briefing
  that any AI agent can pick up and investigate. The check doesn't know or care WHO investigates.
- **Templates over raw SQL**: users pick from strategy templates, not write SQL from scratch

## Data Sources (all from existing APIs)

Everything `kbagent check` needs is already available through Keboola APIs:

| Data | API endpoint | What it tells us |
|------|-------------|------------------|
| Component jobs | Queue API `/jobs` | runs, duration, status, credits, component, config |
| Storage events | Storage API `/events` | imports, exports, bytes moved, table operations |
| Table metadata | Storage API `/tables` | rowsCount, dataSizeBytes, lastImportDate |
| Token/project stats | Storage API `/tokens/verify` | project info, backend, features |

Data volume is small — thousands of job records, not millions of data rows.
DuckDB handles this in milliseconds with zero setup.

## Strategy Templates

Users don't write SQL. They pick a **strategy template** and set parameters.

### Template: `credit-spike`

Detects when a component's daily credit consumption deviates from its baseline.

```yaml
# .kbagent/checks/credit-spike.yaml
apiVersion: kbagent/v1
kind: Check
metadata:
  name: credit-spike
  description: "Alert when any component burns >50% more credits than usual"
  severity: warning
  tags: [credits, cost-monitoring]

spec:
  project: my-project
  template: credit-spike
  params:
    lookback_days: 14          # baseline period
    threshold_pct: 50          # alert if >50% above average
    min_daily_credits: 0.5     # ignore components with negligible usage
    group_by: component_id     # or: [component_id, config_id] for finer grain
```

The template expands to DuckDB SQL internally. Users never see or edit the SQL
unless they want to (via `template: custom`).

### Template: `job-slowdown`

Detects jobs running significantly slower than their historical median.

```yaml
apiVersion: kbagent/v1
kind: Check
metadata:
  name: etl-slowdown
  description: "Alert when any job takes >3x its median runtime"
  severity: warning

spec:
  project: my-project
  template: job-slowdown
  params:
    lookback_days: 30
    slowdown_factor: 3         # alert if duration > 3x median
    min_duration_seconds: 60   # ignore short jobs
    status_filter: [success]   # only compare successful runs
```

### Template: `import-volume-drop`

Detects when data import volume drops compared to the same day last week.

```yaml
apiVersion: kbagent/v1
kind: Check
metadata:
  name: tuesday-imports
  description: "Alert when daily import volume drops >50% vs same day last week"
  severity: critical

spec:
  project: my-project
  template: import-volume-drop
  params:
    lookback_weeks: 4          # compare against 4-week average for same weekday
    drop_threshold_pct: 50     # alert if volume dropped >50%
    compare_by: weekday        # seasonal comparison (Mon vs Mon, Tue vs Tue)
```

### Template: `error-rate`

Detects when component error rates exceed a threshold.

```yaml
apiVersion: kbagent/v1
kind: Check
metadata:
  name: error-rate
  description: "Alert when any component has >20% error rate over 24h"
  severity: critical

spec:
  project: my-project
  template: error-rate
  params:
    window_hours: 24
    threshold_pct: 20
    min_jobs: 5                # ignore components with <5 runs (noisy)
```

### Template: `data-freshness`

Detects when tables haven't been updated within their expected schedule.

```yaml
apiVersion: kbagent/v1
kind: Check
metadata:
  name: stale-tables
  description: "Alert when key tables are >2x their usual update interval behind"
  severity: warning

spec:
  project: my-project
  template: data-freshness
  params:
    staleness_factor: 2        # alert if gap > 2x the usual interval
    tables:                    # specific tables to monitor (optional: all if omitted)
      - in.c-billing.daily_costs
      - in.c-crm.contacts
```

### Template: `custom`

For power users who want raw SQL.

```yaml
apiVersion: kbagent/v1
kind: Check
metadata:
  name: my-custom-check
  description: "Custom check with raw DuckDB SQL"
  severity: info

spec:
  project: my-project
  template: custom
  data_sources: [jobs, storage_events, table_metadata]  # what to fetch from API
  query: |
    -- DuckDB SQL. Available tables: jobs, storage_events, table_metadata
    -- Query must return rows that VIOLATE the rule (empty = pass)
    SELECT component_id, daily_credits, avg_credits, pct_change
    FROM (
      WITH daily AS (
        SELECT component_id, DATE_TRUNC('day', start_time) as day,
               SUM(credits_consumed) as daily_credits
        FROM jobs
        WHERE start_time >= CURRENT_DATE - INTERVAL '14 days'
        GROUP BY 1, 2
      ),
      stats AS (
        SELECT component_id, AVG(daily_credits) as avg_credits
        FROM daily WHERE day < CURRENT_DATE - INTERVAL '1 day'
        GROUP BY 1
      )
      SELECT d.component_id, d.daily_credits, s.avg_credits,
             ROUND((d.daily_credits - s.avg_credits) /
                   NULLIF(s.avg_credits, 0) * 100, 1) as pct_change
      FROM daily d JOIN stats s USING (component_id)
      WHERE d.day = CURRENT_DATE - INTERVAL '1 day'
    )
    WHERE pct_change > 50

  # Expect: query returns 0 rows = pass, 1+ rows = fail
  expect:
    type: empty
```

## The Briefing (check failure output)

When a check fails, `kbagent check run` produces a **briefing** — structured JSON
designed to be consumed by an AI agent (or a human) for investigation.

The briefing is self-contained: it has everything needed to understand the problem
and start investigating, without requiring the reader to re-fetch data.

```json
{
  "briefing_version": "1",
  "generated_at": "2026-03-29T07:00:12Z",
  "project": {
    "alias": "billing-prod",
    "id": 12345,
    "stack": "connection.north-europe.azure.keboola.com"
  },
  "check": {
    "name": "credit-spike",
    "description": "Alert when any component burns >50% more credits than usual",
    "severity": "warning",
    "template": "credit-spike",
    "params": {
      "lookback_days": 14,
      "threshold_pct": 50
    }
  },
  "result": {
    "status": "fail",
    "violations_count": 2,
    "violations": [
      {
        "component_id": "keboola.ex-google-bigquery-v2",
        "config_id": "894231",
        "yesterday_credits": 42.5,
        "baseline_avg_credits": 8.3,
        "pct_change": 412.0,
        "z_score": 4.8
      },
      {
        "component_id": "keboola.python-transformation-v2",
        "config_id": "901122",
        "yesterday_credits": 15.2,
        "baseline_avg_credits": 9.1,
        "pct_change": 67.0,
        "z_score": 2.1
      }
    ]
  },
  "context": {
    "metric_history": [
      {"date": "2026-03-15", "component_id": "keboola.ex-google-bigquery-v2", "credits": 8.1},
      {"date": "2026-03-16", "component_id": "keboola.ex-google-bigquery-v2", "credits": 7.9},
      {"date": "2026-03-27", "component_id": "keboola.ex-google-bigquery-v2", "credits": 9.2},
      {"date": "2026-03-28", "component_id": "keboola.ex-google-bigquery-v2", "credits": 42.5}
    ],
    "recent_config_changes": [
      {
        "component_id": "keboola.ex-google-bigquery-v2",
        "config_id": "894231",
        "config_name": "BQ Marketing Extract",
        "changed_at": "2026-03-27T14:30:00Z",
        "changed_by": "betka@company.com"
      }
    ]
  },
  "investigation_hints": [
    "Config 894231 was modified 1 day before the spike — check what changed",
    "Credits jumped from ~8 to 42 — a 5x increase, likely a query/table scope change",
    "Use: kbagent --json job list --project billing-prod --component-id keboola.ex-google-bigquery-v2 --limit 50",
    "Use: kbagent --json config detail --project billing-prod --component-id keboola.ex-google-bigquery-v2 --config-id 894231"
  ]
}
```

Key properties of the briefing:
- **Self-contained**: includes the violation data AND the historical context
- **Actionable hints**: suggests specific `kbagent` commands for drill-down
- **Config change correlation**: automatically cross-references with recent config changes
- **AI-ready**: any LLM can read this and start investigating immediately

## CLI Commands

```bash
# === Define (with AI assistance) ===

# List available templates
kbagent check templates

# Create from template (AI helps pick params)
kbagent check create --template credit-spike --project billing-prod
# → interactive: AI suggests thresholds based on project's actual data
# → saves to .kbagent/checks/credit-spike.yaml

# Validate a check definition
kbagent check validate .kbagent/checks/credit-spike.yaml

# === Sync with Keboola ===

# Push checks to Keboola Storage Files (permanent, tagged)
kbagent check push [--name NAME | --all] --project ALIAS
# Tags: kbagent:check, kbagent:check:<name>

# Pull checks from Keboola
kbagent check pull [--project ALIAS]

# List checks (local + remote)
kbagent check list [--project ALIAS] [--remote]

# === Run (no AI, deterministic) ===

# Run specific check
kbagent check run --name credit-spike
# exit 0 = pass, exit 1 = fail (with briefing on stdout)

# Run all checks for a project
kbagent check run --all [--project ALIAS]

# JSON output (for automation / AI consumption)
kbagent --json check run --all
# → {"status": "ok", "data": {"total": 5, "passed": 4, "failed": 1,
#    "checks": [...], "briefings": [<briefing JSONs for failures>]}}

# Store results in Keboola for audit trail
kbagent check run --all --store-results

# === History ===

# View past results
kbagent check history [--name NAME] [--limit 10]
```

## Runner Options (who calls `check run`)

`kbagent check run` is a plain CLI command with a JSON exit. Anything can call it:

### A) Local cron
```bash
# crontab -e
0 7 * * * kbagent --json check run --all --store-results >> /var/log/kbagent-checks.log
```

### B) Claude Code /schedule
```
Schedule daily at 7:00:
  Run `kbagent --json check run --all`.
  If any check fails, read the briefings and investigate using the suggested
  kbagent commands. Produce a report and send via Slack/email.
```

### C) Keboola component (Python transformation)
```python
import subprocess, json
result = subprocess.run(["kbagent", "--json", "check", "run", "--all"], capture_output=True, text=True)
data = json.loads(result.stdout)
if data["data"]["failed"] > 0:
    # write briefings to output table for downstream notification
    ...
```

### D) KAI bot
KAI calls `kbagent check run`, gets briefings, investigates autonomously, notifies user.

## Storage: Check Definitions & Results

Both stored as **permanent** Storage Files with tags:

```
Check definitions:
  Tags: ["kbagent:check", "kbagent:check:credit-spike"]
  isPermanent: true

Check results (audit trail):
  Tags: ["kbagent:check-result", "kbagent:check:credit-spike", "kbagent:run:2026-03-29"]
  isPermanent: true  (or with TTL for old results)
```

Local cache in `.kbagent/checks/` for fast access and git version control.

## DuckDB as the Engine

All check SQL runs on DuckDB (in-memory, in-process). No external compute.

**Available tables** (auto-populated from API responses):

| DuckDB table | Source | Key columns |
|-------------|--------|-------------|
| `jobs` | Queue API `/jobs` | id, component_id, config_id, config_name, status, duration_seconds, credits_consumed, start_time, end_time, result_message |
| `storage_events` | Storage API `/events` | id, type (tableImport, tableExport, ...), table_id, bytes, created, creator_token_description |
| `table_metadata` | Storage API `/tables` | id, bucket_id, name, rows_count, data_size_bytes, last_import_date, last_change_date |
| `config_versions` | Storage API `/components/*/configs/*/versions` | version, created, creator_token_description, change_description |

Templates generate SQL against these tables. Custom checks can use any of them.

**Why DuckDB:**
- PostgreSQL-compatible SQL with window functions, MEDIAN, PERCENTILE, etc.
- Handles analytical queries on thousands of rows in <100ms
- Single `pip install duckdb` dependency (~12MB), no server
- Same SQL dialect regardless of project backend (Snowflake/BigQuery)
- Perfect for the kind of analysis needed: aggregations, trends, outlier detection

## Implementation Plan

### Phase 1: MVP — templates + run + briefing
- DuckDB dependency
- API data fetchers (jobs, storage events, table metadata)
- 3 templates: `credit-spike`, `error-rate`, `custom`
- `kbagent check run` — fetch data, run in DuckDB, produce briefing
- `kbagent check list` — list local checks
- `kbagent check validate` — parse and validate YAML

### Phase 2: Full templates + persistence
- All 5 templates: add `job-slowdown`, `import-volume-drop`, `data-freshness`
- Storage Files integration (`push`, `pull`, `--store-results`)
- `check history` command
- Config change correlation in briefings

### Phase 3: Assisted creation + plugin
- `kbagent check create` with AI-assisted parameter tuning
- `kbagent check templates` listing with descriptions
- Plugin playbook for investigation patterns
- Integration guide for Claude Code /schedule + KAI

## Open Questions

1. **Data fetch scope**: how many days of job history to fetch? Default 30 days?
   Queue API has pagination — need to handle large projects with many jobs.

2. **Multi-project checks**: should one check span multiple projects?
   E.g., "compare credit usage across prod and dev". MVP: single project.

3. **Notification**: part of check YAML or runner responsibility?
   Leaning toward runner-handles-notification. Check just produces briefing.

4. **Config versions API**: fetching config change history for correlation
   requires extra API calls per component/config. Worth it for briefing quality,
   but may be slow for projects with hundreds of configs. Fetch only for
   components that appear in violations?

5. **DuckDB SQL in templates**: templates generate SQL from params. Should the
   generated SQL be visible/editable by the user? (e.g., `check render --name X`
   to see the SQL that would run). Useful for debugging and trust.
