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

Branch trees *(since 0.99.1)*: each configuration is read from the branch tree
its manifest entry belongs to (`branchId` missing / 0 / unknown -> the
default tree, `branches[0].path`), not from a hardcoded `main/`. When one
manifest spans several branch trees only the default tree is scanned and the
build `warnings` say how many configurations from other trees were skipped
(lineage node ids are not branch-qualified, so mixing snapshots would silently
connect the wrong configs). Config rows are read relative to their
configuration directory, the way `sync pull` writes them.

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

The text output is a real tree *(since 0.99.1)*: every node sits under the node
it is actually connected to. A node reached a second time is printed as a leaf
with `(see above)`. Lines start with the node type (`[table]`,
`[transformation]`, ...) and the edge meaning: `reads`, `writes` or `shares`
(bucket sharing); `[AI]` marks an edge detected by AI code analysis (the JSON
keeps `detection` unchanged). A configuration with AI-recorded external systems
prints them as `external: Slack (write)`, even when it has no table edges.

Traversal is breadth-first: an edge's `depth` is its shortest distance from the
queried node, and nothing within `--depth` is lost because a longer path
reached a node first (before 0.99.1 a shorter second path to an already visited
node dropped that node's descendants).

## Column-level detail

```bash
# Show column-level mappings for all tables in the result
kbagent lineage show -l lineage.json --downstream "my-project:in.c-main.users" --columns
```

In the text tree `--columns` prints the mapping (or column list) of each EDGE
under that edge's line, including a `(see above)` repeat -- two configs reading
the same table each show their own mapping (since 0.99.1). Columns of a
configuration reading one table through several input mappings are merged into
one edge. AI-recorded `external:` systems print under every configuration in
the tree (at its first occurrence), not only under the queried node.

```bash
# Trace a single column through the lineage
kbagent lineage show -l lineage.json --downstream "my-project:in.c-main.users" --column user_id
```

`--column` *(since 0.99.1)* is a hop-by-hop trace, not a filter:

- The focus must be a **table**; a configuration focus is a usage error (exit 2).
  `--column` works with text and `--json` only; with `--format mermaid|html|er` it is
  a usage error (exit 2) instead of being silently ignored.
- **Upstream** from `T.C`: the producer config of `T`, then its input edges whose
  `column_mapping` has key `C` (case-insensitive), continuing at that input
  table with the mapped (possibly renamed) input column.
- **Downstream** from `T.C`: each consumer whose mapping takes `T.C`, continuing
  with the output column it produces into the one output table carrying it.
- Bucket-sharing edges keep the column name. Depth counts graph edges
  (table -> config -> table = 2).
- Mappings come from `lineage build --ai` (`col_map`). Where no mapping is known
  the trace **stops and says so** instead of guessing: text prints `column
  mapping unknown beyond this point`; JSON lists `terminals[]` entries
  `{node, column, reason}` with `reason` one of `column_mapping_unknown`,
  `ambiguous_output_table` (the config writes several tables with that column
  name; `candidates` lists them) or `output_table_unknown`.
- JSON edges carry `source_column` / `target_column` and only the relevant
  mapping entry; the result has `column`. The legacy `column_filter` key is
  removed (since vNEXT).

Before 0.99.1 `--column` kept every edge without column info and dropped edges whose
column list lacked the name, so unrelated branches stayed and real hops vanished.

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

Each task's `code_file` is the configuration's real file on disk
(`transform.sql` / `code.py` in the scanned branch tree; since 0.99.1 also right
for the flat layout and for aliases that differ from the directory name). A
configuration whose code file does not exist gets no task; it is listed in
`ai_status.ai_tasks_missing_code_file` (`{config_fqn, code_file}`).

Result entry format (one per `config_fqn`, keep the task's `_code_hash`):

```json
{
  "config_fqn": "project:component/config_id",
  "_code_hash": "hash from task",
  "inputs": [{"pid": 123, "bucket": "in.c-x", "table": "y", "columns": ["a"]}],
  "extra_inputs": [{"table_id": "in.c-x.z", "evidence": "code line"}],
  "outputs": [{"table": "out.c-x.result", "columns": ["b"]}],
  "col_map": [{"out": "b", "in_table": "in.c-x.y", "in_col": "a", "transform": "direct"}],
  "external": [{"system": "Slack", "op": "write"}]
}
```

Rules *(since 0.99.1)*:

- `outputs[]` become `writes` edges (detection `ai`); before 0.99.1 they were
  silently ignored. `table` should be the full table id (`out.c-bucket.table`).
  A local name is resolved through the config's output mapping (its `source`,
  else a unique destination table name). An ambiguous name, a
  project-qualified one, or one not in the output mapping is NOT guessed: it is
  reported in `ai_status.ai_outputs_unresolved` (`{config_fqn, table, reason,
  candidates}`, reason `ambiguous` | `cross_project_not_supported` |
  `not_in_output_mapping`). When the edge already exists from the output
  mapping, the AI columns are merged into it (`ai_columns_merged`) instead of
  adding a duplicate.
- `external[]` records systems outside Keboola Storage (`system` non-empty,
  `op` `read` | `write`); they show in the text tree and as dashed nodes in
  diagrams.
- `col_map[].in_table` must be the full input table id (or its
  `alias:`-qualified form, or an input-mapping destination of that config that
  maps to exactly one source). Empty or ambiguous values are skipped and
  counted in `ai_col_map_skipped` -- before 0.99.1 a substring match could
  attach a mapping to the wrong input table. A config that reads the same
  table under two input-mapping aliases is NOT ambiguous: the mapping goes
  onto every reads edge of that one source.
- A result whose `_code_hash` differs from the configuration's current code
  hash is **stale** (the code changed after the agent answered) and is NOT
  applied -- no inputs, outputs, external systems or column mappings. It is
  listed in `ai_status.ai_results_stale` (config FQNs; human build output
  prints the count) and the same build re-emits a task for it. A result with
  no `_code_hash` is applied as before, so always echo the task's hash.
- `ai_status` reports `ai_edges_added` (reads + writes), `ai_reads_added`,
  `ai_writes_added`, `ai_columns_merged`, `ai_external_added`,
  `ai_col_map_skipped`, `ai_outputs_unresolved`, `ai_results_stale`,
  `ai_tasks_missing_code_file`.

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
| `sql_tokenizer` / `sql_tokenizer_cross_project` | SQL parsing for Snowflake table references | Deterministic |
| `bucket_sharing` | Cross-project bucket sharing (`kbagent sharing edges`) | Deterministic |
| `ai` / `ai_cross_project` | AI analysis of SQL / Python code | Requires `--ai` |

Detection says *how* an edge was found; `edge_type` (`reads`, `writes`,
`cross_project_share`) says what it means, and that is what the text tree and
the diagrams label edges with.

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

# ER diagram (entity-relationship style; always lists table columns)
kbagent lineage show -l lineage.json --downstream "my-project:in.c-main.users" --format er

# Self-contained HTML page with interactive diagram
kbagent lineage show -l lineage.json --downstream "my-project:in.c-main.users" --format html
```

Formats: `text` (default), `mermaid`, `html`, `er`.

Diagrams *(since 0.99.1)*:

- Flowcharts are always `graph LR` -- data flows left to right in every view
  (before 0.99.1 upstream views were drawn right-to-left). The queried node has
  a thick border (`focus` class).
- Edges are labelled `reads` / `writes` / `shares`; AI-detected edges are dashed
  with an `AI` badge (`-.->|writes · AI|`).
- Table nodes show the table name, then its bucket; configs show their name and
  component id. The project alias is prefixed only when the diagram spans more
  than one project. ER labels are unchanged.
- AI-recorded external systems render as dashed stadium nodes
  (`system -> config` for read, `config -> system` for write).
- `--columns` now also reaches the flowcharts (column lists in table nodes);
  `--format html --columns` saves to `lineage_<dir>_<node>_columns.html`, so it does
  not overwrite the plain diagram.
- The ER view (`--format er`, and the `lineage server` ER tab) always lists each
  table's columns as attributes -- before 0.99.1 it showed only a rows/cols
  count unless `--columns` was given; `--columns` no longer changes it.
  External systems render as `ext:<system>` entities related to the tables
  their config touches (or to the config itself when it touches none).
- `--format html` defaults to `--depth 3` (a depth-10 diagram of a real project
  renders unreadably small); an explicit `--depth` wins, and `--json` as well
  as every other format keep the default of 10.

## Interactive lineage browser

Start a local web server for interactive lineage exploration:

```bash
kbagent lineage server -l lineage.json
kbagent lineage server -l lineage.json --port 8080 --host 0.0.0.0
```

Opens a sidebar-based node picker with mermaid/ER diagram rendering and
export capabilities. Useful for exploring large graphs visually.

"Both" directions *(since 0.99.1)* are computed server-side as one deduplicated
union and rendered once (`direction=both` on `/api/query` + `/api/mermaid`, and
on `kbagent serve`'s `GET /lineage/walk` + `GET /lineage/mermaid`). The browser
used to merge two rendered diagrams, which corrupted the ER view. A
configuration with only external-system annotations now renders instead of
showing "no dependencies".

## Key details

- **Non-sync'd projects**: cross-project references to projects not in the sync
  directory appear as `unknown-{project_id}` in the graph
- **AI task/result files**: `.lineage_ai_tasks.json` and `.lineage_ai_results.json`
  are keyed by code hash -- only changed code needs re-analysis on subsequent runs
- **`--refresh`**: runs sync pull before rebuilding, so the graph reflects the
  latest remote state
- **Cache file**: the `-o` / `-l` JSON file is the single source of truth for
  queries; rebuild it when project configs change
