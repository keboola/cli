# AXI → kbagent: Mapping Opportunities Report

> **Source:** <https://github.com/kunchenguid/axi> (AXI = *Agent eXperience Interface*) by Kun Chen.
> **Target:** `keboola-agent-cli` (kbagent) v0.20.6, this repo.
> **Date:** 2026-04-20.
> **Scope:** Identify which AXI design principles apply to kbagent, where the biggest wins are,
> and propose a concrete adoption roadmap.

---

## 1. Executive Summary

AXI is not a tool -- it is a **manifesto of 10 design principles** plus a small TypeScript SDK and
two benchmarks (GitHub, browser). The benchmarks show that an AXI-compliant CLI beats both
plain `gh` and MCP tooling on **cost (~50%), reliability (100% vs 86%), latency (~10-15%)**
across 425 + 490 runs.

The author's thesis is one sentence:

> *"Recent debate has framed this as 'MCP vs. CLI', but the real question is neither **which**
> protocol nor **which** transport, but rather **what design principles** make an agent-tool
> interface effective. The answer is principled design, not protocol choice."*

kbagent already aligns with AXI on several dimensions (JSON contract, exit codes, structured
errors, `--hint` codegen, multi-project parallelism, SKILL.md plugin). But 5 of the 10 AXI
principles are **not yet implemented** in kbagent, and 3 of those are **high-ROI token/latency
wins** that compound across every agent turn.

This report ranks 12 concrete opportunities, estimates impact and effort, and proposes a 3-phase
adoption plan.

---

## 2. Compliance Matrix: AXI 10 Principles vs kbagent

Legend: `[OK]` = already aligned, `[PARTIAL]` = partial alignment, `[GAP]` = not implemented.

| # | AXI Principle | kbagent Status | Notes |
|---|---|---|---|
| 1 | Token-efficient output (TOON) | `[GAP]` | Uses JSON (verbose) + Rich. ~40% token overhead vs TOON. |
| 2 | Minimal default schemas (3-4 fields) | `[PARTIAL]` | Lists often return 6-10+ fields; no `--fields` opt-in. |
| 3 | Content truncation with `--full` escape hatch | `[GAP]` | No default truncation of config JSONs, job logs, descriptions. |
| 4 | Pre-computed aggregates (`count: X of Y total`, derived status) | `[PARTIAL]` | Paginated results don't expose totals; no derived fields in lists. |
| 5 | Definitive empty states | `[PARTIAL]` | JSON shape is consistent (`"data": []`), but no human hint like *"0 jobs found in project X"*. |
| 6 | Structured errors & exit codes + idempotent mutations + errors on stdout | `[PARTIAL]` | Exit codes OK; errors on stderr *and* stdout in JSON mode; mutations mostly NOT idempotent. |
| 7 | Ambient context via session hooks (self-installing) | `[GAP]` | `kbagent context` is pull-only; nothing pushes dashboard to agent session start. |
| 8 | Content-first home (no args = live data, not `--help`) | `[GAP]` | `kbagent` (no args) opens REPL; `kbagent --help` is usage text. No ambient project state. |
| 9 | Contextual disclosure (`help[]` suggestions on every output) | `[GAP]` | No next-step suggestions appended to outputs. |
| 10 | Consistent way to get help (home view with bin + description, per-subcommand `--help`) | `[PARTIAL]` | Per-subcommand `--help` via Typer is strong; home view missing. |

**Summary:** 3 OK-ish, 4 PARTIAL, 3 outright GAP. The three biggest gaps (1, 7, 9) are also the
three that AXI's benchmarks show deliver the largest cost/latency wins per turn.

---

## 3. High-Priority Opportunities (Do First)

### 3.1 TOON output format (Principle 1) -- BIGGEST WIN

**Opportunity:** Add `--format toon` (or make TOON the default for `--json`-style machine reads)
to kbagent. AXI's `@toon-format/toon` encoder flattens JSON into:

```
configs[3]{id,name,component_id}:
  123,CRM pull,keboola.ex-db-mysql
  124,Sales push,keboola.wr-snowflake
  125,ML scoring,keboola.python-transformation-v2
```

vs. today's JSON (per kbagent contract):

```json
{
  "status": "ok",
  "data": {
    "configs": [
      {"id": "123", "name": "CRM pull", "component_id": "keboola.ex-db-mysql"},
      {"id": "124", "name": "Sales push", "component_id": "keboola.wr-snowflake"},
      {"id": "125", "name": "ML scoring", "component_id": "keboola.python-transformation-v2"}
    ]
  }
}
```

**Impact:** AXI benchmark measured ~40% fewer output tokens on list-heavy workloads. For kbagent,
`config list` across 50+ projects (common in `org setup` scenarios) easily emits 100 KB+ of JSON;
TOON would cut that to ~60 KB. Across a full agent conversation this compounds through cache
write pricing.

**Effort:** Medium.
- Find Python TOON implementation (Perplexity check: `toon-format` on PyPI mirrors
  `@toon-format/toon` on npm, v0.2.x). If missing, encoder is ~200 LOC (spec at
  <https://github.com/toon-format/spec>).
- Add new output mode in `src/keboola_agent_cli/output.py:OutputFormatter`.
- Reuse existing `data: {...}` contract -- encode only on output boundary (SKILL.md pattern from
  AXI: *"keep internal logic on JSON"*).

**Risk:** Low. Agents request it via `--format toon` (opt-in first); `--json` stays for backward
compat. Don't break humans; they use Rich anyway.

**Migration path:**
1. Add `--format json|toon|rich` (default keeps `--json → json`, TTY → rich).
2. Ship as 0.21.0; update SKILL.md to recommend `--format toon` for agents.
3. Benchmark against current `--json` over a representative agent session; expect ~30-40% token
   drop on list workloads, ~10% on single-record reads.

### 3.2 Pre-computed aggregates in lists (Principle 4)

**Opportunity:** Every list-style output should include `count` and `total` fields so agents
don't paginate just to discover counts.

**Today (abbreviated):**
```json
{"status": "ok", "data": {"configs": [...30 items...]}}
```

**Target:**
```json
{"status": "ok", "data": {"configs": [...30 items...], "count": 30, "total": 847, "has_more": true}}
```

**Impact:** AXI benchmark case `list_labels`: CLI agent 0/5 passed (couldn't determine if all
labels were seen), AXI 5/5. kbagent has the same risk for `config list`, `job list`,
`storage tables`, `storage files`, `branch metadata-list`. In multi-project mode, add
per-project totals too: `"totals": {"proj-a": 42, "proj-b": 18}`.

**Derived fields for lists (Principle 4 sub-pattern):**

- `job list` → inline `duration`, `retries`, `has_logs` instead of needing `job detail` follow-ups.
- `config list` → `last_used_at`, `broken` (true if latest 3 jobs all failed), `row_count`.
- `storage tables` → `row_count`, `size_mb`, `last_import_date`.
- `branch list` → `last_modified`, `ahead_of_main` count.

**Effort:** Medium. Mostly plumbing in `services/*_service.py` to fetch totals (Storage API
exposes these cheaply via `?exclude=components` style queries). Derived fields may need 1 extra
API call per list -- measure whether it's worth the extra RTT vs. token savings.

### 3.3 Contextual disclosure -- `help[]` on every output (Principle 9)

**Opportunity:** Every kbagent JSON/TOON response adds a `help` array with 2-3 templated
next-command suggestions.

**Example (after `kbagent job list --project A --status error`):**
```json
{
  "status": "ok",
  "data": {
    "jobs": [{"id": "5810", "status": "error", "component_id": "keboola.snowflake-transformation"}],
    "count": 1,
    "help": [
      "kbagent --json job detail --project A --job-id 5810",
      "kbagent --json config detail --project A --component-id keboola.snowflake-transformation --config-id <id>",
      "kbagent --json workspace from-transformation --project A --component-id <component> --config-id <id>"
    ]
  }
}
```

**Rules (stolen directly from AXI SKILL.md):**

- **Relevant** -- *after an error job, suggest `job detail` and workspace debugging.*
- **Actionable** -- *every suggestion is a complete command carrying forward disambiguating
  flags (`--project`, `--branch`).*
- **Parameterized** -- *use `<id>` placeholders rather than guessing concrete values.*
- **Self-contained detail views** -- *detail views don't need suggestions.*
- **On errors, resolve the error** -- *suggest the fix command, not `--help`.*

**Impact:** AXI case study says *"contextual suggestions guide agent"* delivers a decisive win
on multi-step investigations. For kbagent, the debug flow (`job detail` → `workspace
from-transformation` → `workspace query` → `sync push`) is exactly the pattern `help[]` unlocks.

**Effort:** Medium-high. Need a `HelpSuggestion` registry keyed by command + response shape. Can
start with high-value commands (`job list`, `config search`, `job detail`, `lineage show`) and
expand.

**Source of truth:** reuse `hints/` registry metadata. Every command already knows its likely
follow-ups.

### 3.4 Content-first home view (Principle 8)

**Opportunity:** Running `kbagent` with no args (and no TTY → no REPL) prints a live project
dashboard, not help text.

**Current behavior:** `kbagent` on non-TTY exits with usage; on TTY it opens REPL.

**AXI pattern:**
```
$ tasks
tasks[3]{id,title,status}:
  1,Fix auth bug,open
  ...
help[2]:
  Run `tasks view <id>` to see full details
  ...
```

**kbagent adaptation:**
```
$ kbagent
bin: ~/.local/bin/kbagent
description: Unified CLI for multiple Keboola projects, optimized for AI agents.
projects[4]{alias,active_branch,status,failing_jobs_24h}:
  keboola-main,main,ok,0
  keboola-dev,fix-etl,ok,0
  clientA-prod,main,auth-expired,3
  clientA-dev,feature-x,ok,1

help[3]:
  Run `kbagent config list` to see configurations across all projects
  Run `kbagent job list --status error --limit 10` to triage failing jobs
  Run `kbagent project status --project clientA-prod` to diagnose auth failure
```

**Impact:** On session start, an agent sees the ambient state of every registered project
*without calling any tool*. Combined with Principle 7 (session hooks, below), this gives the
agent decisive situational awareness in its first turn.

**Effort:** Low. Reuses `project status` (already parallel). Add `--quick` mode that skips
expensive per-project API calls and reads from last-cached status.

**Caveat:** Must be cheap (<3s). If `project status` across 30 projects takes 10s, make dashboard
read from a daemon-maintained cache (see §3.5).

### 3.5 Session hooks / ambient context (Principle 7) -- MOST INNOVATIVE

**Opportunity:** On first invocation in a Claude Code / Codex / Cursor workspace, kbagent
self-installs a `SessionStart` hook into the agent's settings. Every new session then
auto-executes `kbagent --format toon` → dashboard appears as initial context before the agent
makes its first tool call.

**AXI implementation reference (`axi-sdk-js/src/hooks.ts`):**

- `installSessionStartHooks()` writes to:
  - Claude Code: `~/.claude/settings.json`, `hooks.SessionStart[]`
  - Codex: `~/.codex/hooks.json` + `~/.codex/config.toml` (adds `[features] codex_hooks = true`)
- `computeSessionStartHookUpdate()` is pure-function (deep-clone + idempotent merge).
- Uses **absolute executable paths** (from `sys.executable` + `sys.argv[0]` → `shutil.which`).
- Every run re-validates the path; if the binary moved (re-install, virtualenv switch), it
  **heals** the hook → *"self-install becomes self-heal."*
- **Rejects dev entrypoints** (e.g., `.py` path containing `src/` uninstalled, test runner
  subprocess paths) to avoid polluting hooks while developing the tool itself.

**kbagent adaptation:**

- `kbagent init --session-hook` command (opt-in; never write to config without explicit consent).
- Reuse `kbagent doctor --fix` pattern for idempotent install.
- Hook runs `kbagent --format toon --quick` (the `--quick` flag is the cached home view, see §3.4).
- Budget: the hook must emit <2 KB of tokens on average. `--quick` with 20 projects = ~1 KB TOON.

**Impact:** Game-changing for agent UX. Claude Code user who opens a kbagent-managed workspace
immediately sees:
- Which projects are registered.
- Which have failing jobs in the last 24h.
- Which are on a dev branch (not main).
- Suggested next commands based on state.

No first "what do I have?" probe needed.

**Effort:** High (correctness + cross-platform + idempotency matters). Worth it.

**Risk:** Security. Any self-installing hook is a policy decision. Gate behind explicit
`init --session-hook --yes` with a confirmation message describing exactly what is written to
which config file. Document in `docs/session-hooks.md`. Respect
`KBAGENT_DISABLE_SESSION_HOOKS=1`.

---

## 4. Medium-Priority Opportunities

### 4.1 Content truncation with escape hatch (Principle 3)

**Opportunity:** Long string fields (config JSON blobs, descriptions, job logs, Python/SQL code
in `config detail`) default to truncated preview + escape hatch.

**Example:**
```json
{"data": {
  "config": {
    "id": "123",
    "name": "ML scoring",
    "configuration": "{\"parameters\":{\"script\":\"import pandas as pd\\n... (truncated, 8432 chars total) ...\"}}",
    "help": [
      "kbagent --json config detail --project A --config-id 123 --full"
    ]
  }
}}
```

**Rules (from AXI):**
- Never omit large fields entirely -- always include preview.
- 500-1500 chars is the sweet spot (AXI uses 500).
- Suggest `--full` only when truncation actually happened.

**Targets in kbagent:**

- `config detail` → `configuration` (JSON blob), `description`.
- `job detail` → `result.*` long strings, `params`, `status.message`.
- `component detail` → `configurationSchema`, `rootConfigurationExamples`.
- `branch metadata-get` → `value` (when > 500 chars).

**Impact:** Avoids 20 KB+ outputs on single reads. Agent can request `--full` when it needs the
blob (e.g., for parsing).

**Effort:** Low-medium. Add `_truncate()` helper in `output.py`. Wire through `--full` flag
in commands that read large blobs.

### 4.2 Idempotent mutations (Principle 6 sub-rule)

**Opportunity:** Mutations that land on already-correct state return `status: ok` with a
`no_op: true` marker, not an error.

**Examples:**

- `kbagent branch delete --branch-id X` when branch already deleted → `{"status": "ok",
  "data": {"branch_id": "X", "no_op": true}}` with exit 0.
- `kbagent config update --set parameters.query="NEW"` when value already "NEW" → no-op.
- `kbagent sharing share --bucket-id X --type ORG` when already shared ORG → no-op.
- `kbagent storage delete-table` when table not found AND `--force` → no-op (treat 404 as success).

**Currently:** These return errors, which makes agents defensively check before every mutation
(wasted tokens).

**Impact:** AXI SKILL.md: *"Don't error when the desired state already exists... acknowledge
and move on with exit code 0."* Equivalent for kbagent saves the probe calls.

**Effort:** Medium -- per-command logic. Compare intended state vs current, short-circuit.

### 4.3 Definitive empty states (Principle 5)

**Opportunity:** Empty lists include explicit counts + contextual message.

**Example:**
```json
{"status": "ok", "data": {"jobs": [], "count": 0, "filter": {"status": "error", "project": "A"},
  "message": "0 error jobs in project A in the last 50 runs",
  "help": ["kbagent --json job list --project A (remove --status filter)",
           "kbagent --json job list --status processing"]}}
```

**Impact:** Agents see `"count": 0` + message → confidently skip (don't retry with different
flags just to verify).

**Effort:** Low. Already have the data; just enrich response shape.

### 4.4 Errors on stdout (Principle 6 sub-rule)

**Opportunity:** In `--json`/`--format toon` modes, write errors to stdout (agents read stdout);
keep stderr for diagnostics only.

**Current:** kbagent writes JSON error to stdout (check `commands/_helpers.py`), but
`OutputFormatter.print_error` routing is uneven. Verify for every command.

**Impact:** Low (probably already works). Audit + enforce in tests.

**Effort:** Low. Add `test_all_commands_emit_errors_to_stdout()` test.

### 4.5 Minimal default list schemas + `--fields` opt-in (Principle 2)

**Opportunity:** Lists return 3-4 essential fields by default. Agents opt into more via
`--fields id,name,component_id,description`.

**Current:** `kbagent config list` returns `id`, `name`, `component_id`, `configuration` (full
JSON!), `description`, `metadata`, `created`, `changed`, etc. -- often 10+ fields.

**Target:**

- Default: `id`, `name`, `component_id`, `is_disabled`.
- `--fields ...` opt-in for more.
- `--fields '*'` keeps current full shape.

**Impact:** Compounds with TOON for massive list-scan savings. On 50 projects × 30 configs =
1500 rows, default slim schema is ~60 KB → 20 KB (67% savings).

**Effort:** Medium. Every list command needs a default field whitelist + `--fields` flag. Add
test for every list command to assert default schema size.

### 4.6 Home view identifier format (Principle 10)

**Opportunity:** The home view from §3.4 starts with:

```
bin: ~/.local/bin/kbagent
description: Unified CLI for Keboola projects, optimized for AI agents.
```

**Why:** AXI philosophy -- *"When the agent forgets which tool it just ran, the home view
reminds it."* Collapsing `$HOME` (AXI has `collapseHomeDirectory()`) prevents leaking usernames
across agent sessions.

**Effort:** Trivial. Add `bin:` + `description:` lines to home view.

---

## 5. Lower-Priority Opportunities (Consider Later)

### 5.1 Benchmark harness (AXI's bench-github / bench-browser)

AXI ships a full benchmark harness (`bench-github/`, `bench-browser/`) that:
- Runs N tasks × M conditions (AXI vs gh vs MCP) × K repeats.
- Parses Claude/Codex `--output-format stream-json` for token usage, turn count, command count,
  errors.
- Uses **LLM-as-judge** (separate Claude call) to rate trajectory quality.
- Retries with exponential backoff; hard-kills orphan processes.
- Emits aggregate + per-task reports (md tables).

**kbagent adaptation:** Build `bench/` directory comparing:
- `kbagent` vs `kbc` CLI vs `keboola-mcp-server` direct vs generic shell scripts using `curl`.
- Tasks: "list configs using a parameter X", "find all jobs that failed today", "rotate all
  tokens in org", "debug failing SQL transformation", "lineage: what depends on table X".
- Judge: Claude/Codex rates trajectory (did it succeed? extra calls? errors?).

**Impact:** Marketing artifact + continuous evidence that kbagent design choices are correct.
AXI's benchmarks are a huge part of its credibility.

**Effort:** High (weeks). Best done as its own subproject.

### 5.2 Lifecycle capture (session-end hook enrichment)

AXI SKILL.md: *"Use session-end hooks to capture what happened (transcripts, files touched,
specs referenced) so future session-start context gets richer over time."*

**kbagent adaptation:** On session end, write `~/.config/keboola-agent-cli/session-<id>.jsonl`
with:
- Commands executed + exit codes.
- Projects touched, configs mutated, jobs run.
- Total API calls, duration, credits estimate.

Next session-start hook reads last few session logs → home view gets *"In last 3 sessions:
updated 2 configs in project A, ran 5 jobs, no failures"*.

**Impact:** Agent continuity across sessions; richer dashboards.

**Effort:** Medium. Needs session ID correlation (already have `KBAGENT_CONVERSATION_ID`).

### 5.3 Consistent `--query` flag for filtered views

AXI's `chrome-devtools-axi` Case B example:
```
$ chrome-devtools-axi open "<url>" --query "search"
→ Navigate + filter snapshot to matching lines.
```

**kbagent adaptation:** Add `--query STR` to every list command that does server-side or
in-memory substring filter on name/description/ID, returning only matching rows.

- `kbagent config list --query "snowflake-transformation"` (already have `config search` but
  it's different -- searches *inside* configs).
- `kbagent job list --query "load CRM"`.
- `kbagent storage tables --query "user"`.

**Impact:** Smaller outputs; fewer post-filter grep pipes in agent workflows.

**Effort:** Low. Add kwarg to list services.

### 5.4 Command shape enforcement

AXI's `runAxiCli()` rejects flags before command:
```
$ gh-axi --json issue list
error: Flags must come after the command. Try: gh-axi issue list --json
```

**kbagent conflict:** kbagent has `--json` as a *global* flag that works before or after the
command (Typer default). The rigid AXI rule conflicts with current UX.

**Recommendation:** Skip this. Accept both orders. Document the preferred order in SKILL.md.

### 5.5 Social video / marketing artifact

AXI has `bench-browser/social/` with HyperFrames composition for a race animation visualization
comparing AXI vs CLI vs MCP. It's a marketing artifact tied to the benchmarks.

**kbagent equivalent:** Only relevant if kbagent ships its own benchmark. Skip until §5.1 lands.

---

## 6. Bonus: kbagent-Specific AXI Applications

AXI principles are general. Here are two kbagent-specific opportunities that combine them.

### 6.1 Auto-dashboard on `kbagent doctor`

`kbagent doctor` already exists. Extend it to also emit the home-view dashboard. If `--json` is
set, use TOON; if TTY, use Rich. This is cheap to ship and becomes the canonical "show me
everything" command.

### 6.2 `--hint toon` mode

`kbagent --hint client|service` already generates Python code. Add `--hint toon` that emits the
TOON representation of what the command would return (a kind of schema preview). Useful for
agent authors writing workflows.

### 6.3 Session-hook-aware `sync pull`

If ambient context (§3.5) is in place, `kbagent sync pull` can detect *"project A is on dev
branch `fix-etl`; main has drifted 14 commits ahead; workspace `ws-312` has pending queries"*
and surface it as part of sync output's `help[]`.

---

## 7. Adoption Roadmap (3 Phases)

### Phase 1 -- "Token diet" (v0.21.0, 1-2 weeks)

Quick wins. No breaking changes. Agent cost drops ~30-40% on list workloads.

1. **TOON output** as opt-in `--format toon` (§3.1).
2. **Pre-computed aggregates** (`count`, `total`, `has_more`) in all lists (§3.2).
3. **Definitive empty states** (§4.3).
4. **Minimal default list schemas** + `--fields` opt-in, starting with `config list`,
   `job list`, `storage tables` (§4.5).
5. **Content truncation** on `config detail`, `job detail`, `component detail` with `--full`
   escape hatch (§4.1).

Update SKILL.md rules: *"Always use `--format toon` for agents."*

### Phase 2 -- "Guidance" (v0.22.0, 2-3 weeks)

Agent navigation improvements. Low risk.

6. **`help[]` contextual disclosure** on every command (§3.3). Start with ~15 high-value
   commands.
7. **Content-first home view** (`kbagent` no-args → dashboard in TOON, §3.4).
8. **Home view identifier** (bin + description at top, §4.6).
9. **Idempotent mutations** for `branch delete`, `config update` no-op, `sharing share` re-share
   (§4.2).
10. **Errors on stdout audit + test** (§4.4).

### Phase 3 -- "Ambient context" (v0.23.0 / 0.24.0, 4-6 weeks)

The killer feature. Demands careful UX.

11. **Session hooks self-install** for Claude Code + Codex (§3.5). Gated behind
    `kbagent init --session-hook --yes`. Include self-heal on every run.
12. **Session-end lifecycle capture** (§5.2).
13. **`kbagent bench`** (optional, but if we do it, it validates all of the above, §5.1).

---

## 8. Implementation Notes (the gotchas)

- **TOON Python library availability**: verify before committing to Phase 1. If absent, factor
  in ~200 LOC encoder (spec at <https://toon-format.github.io>).
- **Backward compatibility**: `--json` remains the default for agents; TOON opt-in. After a few
  releases, flip default to TOON once we're confident the ecosystem is ready.
- **Plugin SKILL.md**: every principle change needs SKILL.md updates. Auto-generator
  (`make skill-gen`) already exists -- make sure it understands TOON.
- **`kbagent context`**: output should itself be TOON once TOON is the default. Self-hosting.
- **Tests**: add fixture-based golden-file tests for every list command's default field shape +
  TOON encoding. AXI ships vitest snapshots; we'd use `pytest` + `syrupy`.
- **Benchmark claim verification**: AXI's ~40% savings claim is based on specific workloads
  (browser + GitHub). For kbagent, run a mini-benchmark on `config list` + `job list` during
  Phase 1 to quantify actual savings and include in release notes.

---

## 9. What NOT to Copy

AXI has a few choices that don't fit kbagent:

- **No-interactive-prompts rule** -- kbagent intentionally has REPL for humans. Keep it but
  make sure it's never reachable from non-TTY (it is already).
- **Flags-after-command enforcement** (§5.4) -- breaks our Typer UX.
- **Single top-level home command** -- AXI tools like `tasks` are single-domain. kbagent is
  multi-domain (project, config, job, storage, ...). Home view should be ambient dashboard, not
  *"list tasks"* equivalent.
- **Absolute dominance of TOON** -- we keep Rich for TTY humans. TOON is for `--json`-style
  machine reads.

---

## 10. TL;DR for Humans

1. AXI is a **set of 10 design principles** with benchmarks proving agent-native CLIs beat both
   plain CLIs and MCP on cost, reliability, latency.
2. kbagent already aligns on JSON contract, exit codes, and structured errors.
3. **5 principles are gaps**; the top 3 (**TOON, ambient context, `help[]`**) will measurably
   reduce agent token spend per conversation.
4. **Phase 1 (Token diet)** can ship in 1-2 weeks and delivers ~30-40% token cost drop on list
   workloads without breaking anyone.
5. **Phase 3 (Session hooks)** is the "wow" feature -- agent opens Claude Code in a
   kbagent-managed repo and sees the world in turn 1 without calling any tool.
6. Ship a **`kbagent bench`** eventually. AXI's credibility comes from benchmarks, not
   assertions; ours can too.

---

## References

- <https://github.com/kunchenguid/axi> -- AXI repo
- <https://github.com/kunchenguid/gh-axi> -- GitHub reference AXI tool
- <https://github.com/kunchenguid/chrome-devtools-axi> -- browser reference AXI tool
- `/tmp/axi/.agents/skills/axi/SKILL.md` -- the 10 principles (definitive source)
- `/tmp/axi/packages/axi-sdk-js/src/hooks.ts` -- session hooks reference implementation
- `/tmp/axi/docs/index.html` -- study website with benchmark evidence
- `/tmp/axi/bench-github/published-results/STUDY.md` -- GitHub benchmark full report
- `/tmp/axi/bench-browser/published-results/report.md` -- browser benchmark full report
