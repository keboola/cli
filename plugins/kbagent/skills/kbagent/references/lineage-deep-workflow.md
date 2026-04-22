# Lineage Workflow -- Column-Level Lineage Analysis

Lineage deep builds a full dependency graph from sync'd project data on disk.
It detects table-level and column-level relationships by parsing config files,
SQL code, and optionally using AI for ambiguous mappings.

## Prerequisites

Projects must be sync'd to disk before building lineage:

```bash
# Pull all projects (or a specific one)
kbagent sync pull --all-projects
```

The sync'd directory structure is the input for lineage analysis.

### Supported layouts

`lineage build` auto-detects both layouts produced by `sync pull`:

| Source command | Layout | Example manifest path |
|----------------|--------|------------------------|
| `sync pull --project X` | **Flat** -- CWD *is* the project | `./.keboola/manifest.json` |
| `sync pull --all-projects` | **Nested** -- one subdir per project | `./<alias>/.keboola/manifest.json` |

Pass the directory that *contains* the manifest (flat) or the parent of all
project subdirs (nested). When the build finds zero projects it emits a
warning with a hint rather than silently returning an empty graph.

## Build lineage

```bash
# Build from sync'd data and save to cache file
kbagent lineage build -d /path/to/sync-dir -o lineage.json
```

This scans all project directories, parses configs, and writes the full
lineage graph to `lineage.json` for fast subsequent queries.

## Query from cache

Once built, query the graph without re-scanning:

```bash
# Show downstream dependencies of a table
kbagent lineage show -l lineage.json --downstream "my-project:in.c-main.users"

# Show upstream dependencies
kbagent lineage show -l lineage.json --upstream "my-project:out.c-analytics.report"
```

## Column-level detail

```bash
# Show column-level mappings for all tables in the result
kbagent lineage show -l lineage.json --downstream "my-project:in.c-main.users" --columns

# Trace a single column through the lineage
kbagent lineage show -l lineage.json --downstream "my-project:in.c-main.users" -c user_id
```

## Refresh in one step

Combine sync pull + rebuild into a single command:

```bash
kbagent lineage build -d /path/to/sync-dir -o lineage.json --refresh
```

This runs `sync pull` first, then rebuilds the lineage graph.

## AI-enhanced analysis (2-step flow)

For ambiguous SQL or Python code where deterministic parsing cannot resolve
column mappings, use the `--ai` flag in a 2-step workflow:

**Step 1: Generate AI task file**

```bash
kbagent lineage build -d /path/to/sync-dir -o lineage.json --ai
```

This writes `.lineage_ai_tasks.json` alongside the output file, containing
tasks for each code block that needs AI analysis. Each task includes the
source code, context, and a code hash for cache invalidation.

**Step 2: AI agent processes the tasks**

An AI agent reads `.lineage_ai_tasks.json`, analyzes each code block, and
writes results back to `.lineage_ai_results.json`.

**Step 3: Re-build to apply AI results**

```bash
kbagent lineage build -d /path/to/sync-dir -o lineage.json --ai
```

On re-build, the CLI reads `.lineage_ai_results.json` and merges AI-derived
column mappings into the lineage graph. Tasks whose code hash has not changed
reuse existing results; only modified code needs re-analysis.

## Node identifiers

Tables are identified by fully-qualified names (FQN):

| Format | Example | Notes |
|--------|---------|-------|
| Full FQN | `my-project:in.c-main.users` | Always unambiguous |
| Table only | `in.c-main.users` | Auto-resolves; warns if ambiguous across projects |

Use the full FQN (`project-alias:bucket_id.table_name`) when multiple
projects contain tables with the same name.

## Detection methods

| Method | Source | Type |
|--------|--------|------|
| `input_mapping` | Config input mapping definitions | Deterministic |
| `output_mapping` | Config output mapping definitions | Deterministic |
| `sql_tokenizer` | SQL parsing for Snowflake table references | Deterministic |
| `bucket_sharing` | Cross-project sharing from `kbagent lineage show` | Deterministic |
| `sql_ai` | AI analysis of SQL code | Requires `--ai` |
| `python_ai` | AI analysis of Python code | Requires `--ai` |

## Inspect graph contents

Use `lineage info` to see what's in a cached lineage graph without running
a full query:

```bash
kbagent lineage info -l lineage.json
```

This shows per-project breakdown, table counts, and most connected nodes.

## Output formats

The `--format` option controls how `lineage show` renders the result:

```bash
# Default text output
kbagent lineage show -l lineage.json --downstream "my-project:in.c-main.users"

# Mermaid diagram (paste into mermaid.live or GitHub markdown)
kbagent lineage show -l lineage.json --downstream "my-project:in.c-main.users" --format mermaid

# ER diagram (entity-relationship style with column details)
kbagent lineage show -l lineage.json --downstream "my-project:in.c-main.users" --format er --columns

# Self-contained HTML page with interactive diagram
kbagent lineage show -l lineage.json --downstream "my-project:in.c-main.users" --format html
```

Formats: `text` (default), `mermaid`, `html`, `er`.

## Interactive lineage browser

Start a local web server for interactive lineage exploration:

```bash
kbagent lineage server -l lineage.json
kbagent lineage server -l lineage.json --port 8080 --host 0.0.0.0
```

Opens a sidebar-based node picker with mermaid/ER diagram rendering and
export capabilities. Useful for exploring large graphs visually.

## Key details

- **Non-sync'd projects**: cross-project references to projects not in the sync
  directory appear as `unknown-{project_id}` in the graph
- **AI task/result files**: `.lineage_ai_tasks.json` and `.lineage_ai_results.json`
  are keyed by code hash -- only changed code needs re-analysis on subsequent runs
- **`--refresh`**: runs sync pull before rebuilding, so the graph reflects the
  latest remote state
- **Cache file**: the `-o` / `-l` JSON file is the single source of truth for
  queries; rebuild it when project configs change
