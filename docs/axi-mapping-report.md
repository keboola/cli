# AXI → kbagent: Mapping Opportunities Report

> **Source:** <https://github.com/kunchenguid/axi> (AXI = *Agent eXperience Interface*) by Kun Chen.
> **Target:** `keboola-agent-cli` (kbagent) v0.20.6, this repo.
> **Date:** 2026-04-20.
> **Scope:** Identify which AXI design principles apply to kbagent, where the biggest wins are,
> and propose a concrete adoption roadmap. **§2 is a devil's advocate reality check** that
> challenges the optimistic framing of §§3-8 and should be read first.

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
errors, `--hint` codegen, multi-project parallelism, SKILL.md plugin). 5 of the 10 AXI
principles are **not yet implemented** in kbagent, and 3 of those (**TOON, session hooks,
`help[]`**) are -- on paper -- high-ROI token/latency wins that compound across every agent
turn.

**Caveat before reading further:** AXI's claims are validated on browser automation and GitHub
CLI operations -- both very different from kbagent's workload. **§2 (Reality Check)** argues
that the realistic wins for kbagent are smaller, that some AXI principles actively conflict
with kbagent's strengths, and that the adoption priority should be re-ranked from what §§3-8
propose.

This report ranks 12 concrete opportunities, estimates impact and effort, and proposes a 3-phase
adoption plan -- alongside an honest counter-view on whether each item is worth building.

---

## 2. Reality Check: Where kbagent Actually Stands (Devil's Advocate)

Before adopting any AXI principle wholesale, read this section. §§3-8 describe each principle in
its best light; this section pushes back.

### 2.1 AXI's benchmarks are not kbagent's workload

AXI validates its principles on **browser automation** (`chrome-devtools-axi`) and **GitHub CLI
operations** (`gh-axi`). Both workloads are:

- **High-frequency** -- dozens of clicks, navigates, snapshot reads per task.
- **Enumerable-entity-heavy** -- lots of issues, PRs, page elements to list.
- **Shallow shapes** -- an issue = title + state + body + labels.
- **Single-tenant** -- one repo at a time, one browser tab at a time.
- **Stateless between calls** -- each command is an atomic ask.

**kbagent is none of those things:**

- **Low-frequency, stateful operations.** A typical agent session is "describe failing job →
  fetch one config → try a SQL in a workspace → push a fix via sync". That's 5-10 calls, not
  30-50.
- **Bounded cardinality.** An org has 5-50 projects, each with ~50-500 configs. The list
  explosion AXI optimizes for (hundreds of GitHub labels) rarely happens in kbagent.
- **Deep hierarchical data.** A config is a JSON tree with nested `parameters`, `storage`,
  `processors`. Truncation and flat tabular TOON lose information that agents actually need.
- **Multi-tenant by design.** `config list` runs across all projects in parallel -- AXI has no
  such concept.
- **Long-running mutations.** `workspace create`, `sync pull`, `job run --wait` produce
  non-trivial state changes, not lookups.

**Implication:** The 40% token savings AXI measures on list-dominant tasks likely degrades to
**10-15% on a typical kbagent session**. Still real, but not the dramatic "$0.05/task vs
$0.10/task" headline AXI prints.

### 2.2 kbagent already has things AXI's SKILL.md does not cover

AXI's principles are agnostic to these, and their absence in AXI means kbagent should not
abandon them chasing AXI conformance:

- **`--hint client|service` code generation.** Any kbagent command can output runnable Python
  instead of executing. AXI has no equivalent. For longer workflows this is arguably **a
  stronger pattern than TOON**: instead of optimizing per-turn tokens, the agent writes one
  Python script that chains 20 operations with zero per-call JSON overhead.
- **Multi-project parallel execution** (`BaseService._run_parallel`). `config list` across 30
  projects in one call. AXI's conditions are always single-target.
- **Permission firewall + `--read-only`.** Code-level sandbox with three defense layers (policy
  + filesystem `chmod 0400` + Claude settings deny list). AXI has no safety story -- it's a
  trust-the-agent world.
- **Dev branch lifecycle** (`branch create/use/reset/merge`) with persistent active-branch
  state in `config.json`. AXI has no state concept; every invocation is independent.
- **GitOps sync workflow** (`sync pull/push` with 3-way diff, auto-encryption of `#`-prefixed
  secrets). AXI has no "codify platform state as files" pattern.
- **MCP bridge** (`kbagent tool list/call` wraps keboola-mcp-server). AXI positions itself
  **against** MCP; kbagent offers both.
- **Plugin marketplace + 17-file reference library** under `plugins/kbagent/skills/kbagent/
  references/`. AXI's SKILL.md is one 260-line file.

**Implication:** Blindly adopting all 10 AXI principles would displace or clutter capabilities
that AXI simply never envisioned.

### 2.3 Per-principle honest value reassessment

Re-rating each principle by *(realistic kbagent win) ÷ (implementation + maintenance cost)*:

| # | Principle | AXI claim | Realistic kbagent win | Verdict |
|---|---|---|---|---|
| 1 | TOON output | 40% token savings on list workloads | **10-15% on typical sessions.** kbagent sessions are mutation-heavy; list reads are a minority. Deep JSON (configs) doesn't flatten well into TOON's tabular shape. | **Defer.** Large implementation cost (write or vend Python encoder, dual-format tests, SKILL.md updates, `context` output rework). Measure first. |
| 2 | Minimal default schemas + `--fields` | Smaller list outputs | **Solid win, low cost.** `config list` returning full `configuration` JSON blob by default is real bloat today. | **Ship.** Tackle `configuration` field first -- biggest single offender. |
| 3 | Content truncation with `--full` | Avoid 20 KB single-read outputs | **Solid win, low cost.** `config detail.configuration`, `job detail.result.*`, `component detail.configurationSchema` are real offenders. | **Ship.** Start with 3 worst fields. |
| 4 | Pre-computed aggregates (`count`, `total`, derived fields) | Eliminates follow-up calls | **Partial.** `total` often costs an extra API call. Derived per-item fields (`last_run_status`, `row_count`) cost 1 call per item or per list = real latency hit. | **Ship `count` + `total` + `has_more`.** Skip per-item derived fields until we measure their ROI. |
| 5 | Definitive empty states | Prevents false-negative retries | **Marginal.** kbagent filter semantics are documented in SKILL.md. Agents don't retry our empty lists. | **Skip** unless telemetry shows actual retries. |
| 6 | Idempotent mutations | Fewer probe reads | **Good for cheap cases** (404 → no-op). **Expensive for state-diff cases** -- checking "is this already set?" often costs a read = double API traffic. | **Ship for cheap cases** (branch delete, sharing re-share, storage delete with `--force`). Skip for costly ones. |
| 7 | Ambient context via session hooks | Agent sees state in turn 1 | **Questionable.** Session-start hooks fire on **every** Claude Code session -- even when the agent isn't touching Keboola. That's pure token overhead for 90% of non-kbagent conversations. Our `CLAUDE.md` + SKILL.md plugin already provide entry points when relevant. | **Opt-in only.** Never default-on. Gate behind explicit `kbagent init --session-hook`. |
| 8 | Content-first home view (`kbagent` no-args = dashboard) | Agent sees live data without calling a tool | **UX conflict.** `kbagent` on TTY opens REPL today -- humans rely on that. Dual behavior (TTY → REPL, non-TTY → dashboard) is fine but not a "wow". | **Move to `kbagent doctor`** or a dedicated `kbagent dashboard`. Don't hijack bare `kbagent`. |
| 9 | Contextual `help[]` suggestions | Agent guided to next step | **Real but smaller than AXI's win.** kbagent already has SKILL.md decision table + `kbagent context`. `help[]` is a third copy of the same information, maintained in a third place -- risk of drift. | **Ship selectively** on commands where next step is genuinely ambiguous (`job detail` → debug workspace, `config search`, `lineage show`). Do not spray every command. |
| 10 | Consistent home view identifier | Tool self-identifies (`bin:` + `description:`) | **Trivial to add.** | **Ship** alongside whatever home view lands. |

**Recap:** Of the 10 principles, only **4** are clear ships (#2, #3, partial #4, #10). **3** are
selective ships (#6, #9, #8-redirected-to-doctor). **3** are either defer/opt-in/skip (#1 TOON,
#5 empty states, #7 session hooks).

Compare this ranking to §3 ("High-Priority Opportunities") below: §3 promotes exactly the three
most questionable items (TOON, session hooks, content-first home) to the top. That's the
optimistic reading. §2 is the realistic one.

### 2.4 Uncomfortable truths about AXI's benchmark evidence

- **Sample size looks big (425 + 490 runs) but covers two domains with two models (Claude +
  Codex).** We don't know how AXI's principles perform on kbagent-adjacent workloads: Salesforce
  CLI, kubectl, stripe CLI, AWS CLI, or multi-tenant SaaS management. AXI's 50% cost advantage
  may be browser- and GitHub-specific.
- **Half of AXI's "2.3× input token advantage vs MCP" comes from not loading 30 tool schemas
  up front.** kbagent has already mitigated that: MCP is one subprocess with per-request
  credentials, and `tool list` is explicit opt-in. We've sidestepped the schema overhead
  without adopting AXI's principles -- so the AXI-over-MCP delta is smaller for kbagent than
  AXI claims for browser workloads.
- **LLM-as-judge benchmarks are weak quality signals.** AXI grades trajectories with a
  Claude/Codex call; the judge shares biases with the executor. This doesn't invalidate the
  cost measurements (those are deterministic token counts), but the "100% reliability" claim is
  judge-dependent.
- **AXI's "ambient context" pattern assumes a single-domain agent session.** When a developer
  opens Claude Code to work on a Python backend that happens to query Keboola occasionally, a
  session-start dashboard is noise, not signal. AXI's benchmark harnesses the agent on
  single-task prompts; real Claude Code usage is mixed-domain.
- **AXI's flagship principle (self-installing hooks) is invasive.** It writes to the user's
  global Claude Code and Codex configs on first invocation. In a security-conscious org, that's
  a non-starter without explicit consent. Our `CLAUDE.md` / SKILL.md pattern is opt-in by
  design (the user has to install the plugin).

### 2.5 Revised priority list (after devil's advocate)

If I re-rank by **(realistic kbagent win) ÷ (cost)** and ignore AXI's headline claims:

1. **Minimal default list schemas + `--fields`** (§5.5) -- high value, low cost, no UX risk.
   Start here.
2. **Content truncation with `--full`** (§5.1) -- same profile. Cheap and obviously correct.
3. **`count` + `total` + `has_more`** on lists (§4.2, half of it) -- cheap when Storage API
   exposes totals; **skip per-item derived fields until measured**.
4. **Selective `help[]`** on 5-10 high-value commands (§4.3) -- do not spray everywhere; risk
   of SKILL.md drift.
5. **Idempotent mutations where free** (§5.2) -- 404 → no-op patterns only.
6. **`kbagent doctor` becomes the dashboard** (§7.1) -- reuse existing command, don't hijack
   bare `kbagent`. Delivers the "home view" win without the UX conflict.
7. ~~TOON output (§4.1)~~ -- **defer**. Build a 30-minute prototype, benchmark against
   real agent traces. If ROI is <20% on a typical session, shelve it.
8. ~~Session hooks (§4.5)~~ -- **opt-in only**. Ship as `kbagent init --session-hook --yes`,
   never default-on. Document the privacy implications.
9. ~~Content-first home view on bare `kbagent`~~ -- **skip**. Use #6 instead.

This is explicitly more conservative than §8 (Adoption Roadmap). The roadmap represents the
AXI-maximalist ambition; §2.5 represents the "ship-what-pays" realistic plan. The two are
compatible: run the realistic plan, measure, then promote items into the ambitious plan if data
supports them.

---

## 3. Compliance Matrix: AXI 10 Principles vs kbagent

Legend: `[OK]` = already aligned, `[PARTIAL]` = partial alignment, `[GAP]` = not implemented.

| # | AXI Principle | kbagent Status | Notes |
|---|---|---|---|
| 1 | Token-efficient output (TOON) | `[GAP]` | Uses JSON (verbose) + Rich. ~40% token overhead vs TOON on paper; realistic kbagent win is smaller (§2.3). |
| 2 | Minimal default schemas (3-4 fields) | `[PARTIAL]` | Lists often return 6-10+ fields; no `--fields` opt-in. |
| 3 | Content truncation with `--full` escape hatch | `[GAP]` | No default truncation of config JSONs, job logs, descriptions. |
| 4 | Pre-computed aggregates (`count: X of Y total`, derived status) | `[PARTIAL]` | Paginated results don't expose totals; no derived fields in lists. |
| 5 | Definitive empty states | `[PARTIAL]` | JSON shape is consistent (`"data": []`), but no human hint like *"0 jobs found in project X"*. |
| 6 | Structured errors & exit codes + idempotent mutations + errors on stdout | `[PARTIAL]` | Exit codes OK; errors on stderr *and* stdout in JSON mode; mutations mostly NOT idempotent. |
| 7 | Ambient context via session hooks (self-installing) | `[GAP]` | `kbagent context` is pull-only; nothing pushes dashboard to agent session start. |
| 8 | Content-first home (no args = live data, not `--help`) | `[GAP]` | `kbagent` (no args) opens REPL; `kbagent --help` is usage text. No ambient project state. |
| 9 | Contextual disclosure (`help[]` suggestions on every output) | `[GAP]` | No next-step suggestions appended to outputs. |
| 10 | Consistent way to get help (home view with bin + description, per-subcommand `--help`) | `[PARTIAL]` | Per-subcommand `--help` via Typer is strong; home view missing. |

**Summary:** 3 OK-ish, 4 PARTIAL, 3 outright GAP. On paper the three gaps (1, 7, 9) deliver the
largest AXI benchmark wins. In practice (§2), the best ROI for kbagent is in the PARTIAL
principles (2, 3, 4), not the GAPs.

---

## 4. High-Priority Opportunities (AXI-Maximalist View)

> **Note:** §2.5 argues most of these should be demoted. Read this section as "the optimistic
> case" and cross-reference §2.3 for the counter-argument.

### 4.1 TOON output format (Principle 1) -- BIGGEST WIN on paper

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

**Impact (optimistic):** AXI benchmark measured ~40% fewer output tokens on list-heavy
workloads. For kbagent, `config list` across 50+ projects (common in `org setup` scenarios)
easily emits 100 KB+ of JSON; TOON would cut that to ~60 KB.

**Impact (realistic -- see §2.3):** Typical kbagent sessions are mutation-heavy, not
list-heavy. Deep JSON (configs with nested `parameters.storage.input.tables[]`) does not
flatten into TOON's tabular shape cleanly. Real session savings: **10-15%**.

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
2. Ship as next release; update SKILL.md to recommend `--format toon` for agents.
3. **Benchmark first** against current `--json` over a representative agent session (see
   §6.1). If ROI <20%, shelve.

### 4.2 Pre-computed aggregates in lists (Principle 4)

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

**Caveat (see §2.3):** Derived per-item fields often require an extra API call per row. In a
multi-project parallel scenario, that multiplies latency. Ship aggregate counts unconditionally;
make derived per-item fields opt-in via `--with-stats`.

**Effort:** Medium. Mostly plumbing in `services/*_service.py` to fetch totals (Storage API
exposes these cheaply via `?exclude=components` style queries).

### 4.3 Contextual disclosure -- `help[]` on every output (Principle 9)

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

**Caveat (see §2.3):** SKILL.md decision table + `kbagent context` + plugin `references/`
already cover "next step" guidance. `help[]` is a third authoring surface. Ship selectively --
only on commands where the next step is genuinely ambiguous.

**Impact:** AXI case study says *"contextual suggestions guide agent"* delivers a decisive win
on multi-step investigations. For kbagent, the debug flow (`job detail` → `workspace
from-transformation` → `workspace query` → `sync push`) is exactly the pattern `help[]`
unlocks.

**Effort:** Medium-high. Need a `HelpSuggestion` registry keyed by command + response shape. Can
start with high-value commands (`job list`, `config search`, `job detail`, `lineage show`) and
expand.

**Source of truth:** reuse `hints/` registry metadata. Every command already knows its likely
follow-ups.

### 4.4 Content-first home view (Principle 8)

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

**Caveat (see §2.3):** REPL is valuable for humans; hijacking bare `kbagent` for a dashboard
creates confusing dual behavior. §7.1 proposes putting this on `kbagent doctor` instead -- same
ambient-context value, no UX collision.

**Effort:** Low. Reuses `project status` (already parallel). Add `--quick` mode that skips
expensive per-project API calls and reads from last-cached status.

**Caveat:** Must be cheap (<3s). If `project status` across 30 projects takes 10s, make
dashboard read from a daemon-maintained cache (see §4.5).

### 4.5 Session hooks / ambient context (Principle 7) -- MOST INNOVATIVE, MOST CONTROVERSIAL

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
- Hook runs `kbagent --format toon --quick` (the `--quick` flag is the cached home view, see §4.4).
- Budget: the hook must emit <2 KB of tokens on average. `--quick` with 20 projects = ~1 KB TOON.

**Impact (optimistic):** Game-changing for agent UX. Claude Code user who opens a kbagent-
managed workspace immediately sees registered projects, failing jobs, active branches, and
suggested next commands.

**Impact (realistic -- see §2.3):** Session-start hooks fire on **every** Claude Code session --
even when the agent is writing Python or editing React components, not touching Keboola. That's
token overhead amortized across many non-kbagent conversations. Our `CLAUDE.md` + SKILL.md
plugin approach already provides discovery when relevant, without polluting unrelated sessions.

**Effort:** High (cross-platform correctness + idempotency + self-heal).

**Risk:** Security + privacy. Any self-installing hook is a policy decision. **Must be opt-in
only.** Gate behind explicit `init --session-hook --yes` with a confirmation message describing
exactly what is written to which config file. Document in `docs/session-hooks.md`. Respect
`KBAGENT_DISABLE_SESSION_HOOKS=1`.

---

## 5. Medium-Priority Opportunities

### 5.1 Content truncation with escape hatch (Principle 3)

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

**§2 verdict:** Ship. One of the clearest high-ROI items.

### 5.2 Idempotent mutations (Principle 6 sub-rule)

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

**§2 verdict:** Ship only for cheap cases (404 → no-op). State-diff cases require an extra read
and may cost more than they save.

### 5.3 Definitive empty states (Principle 5)

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

**§2 verdict:** Marginal. Skip unless telemetry shows actual false-negative retries.

### 5.4 Errors on stdout (Principle 6 sub-rule)

**Opportunity:** In `--json`/`--format toon` modes, write errors to stdout (agents read stdout);
keep stderr for diagnostics only.

**Current:** kbagent writes JSON error to stdout (check `commands/_helpers.py`), but
`OutputFormatter.print_error` routing is uneven. Verify for every command.

**Impact:** Low (probably already works). Audit + enforce in tests.

**Effort:** Low. Add `test_all_commands_emit_errors_to_stdout()` test.

### 5.5 Minimal default list schemas + `--fields` opt-in (Principle 2)

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

**§2 verdict:** Ship first. Highest value-to-cost ratio of any opportunity in this report.

### 5.6 Home view identifier format (Principle 10)

**Opportunity:** The home view from §4.4 starts with:

```
bin: ~/.local/bin/kbagent
description: Unified CLI for Keboola projects, optimized for AI agents.
```

**Why:** AXI philosophy -- *"When the agent forgets which tool it just ran, the home view
reminds it."* Collapsing `$HOME` (AXI has `collapseHomeDirectory()`) prevents leaking usernames
across agent sessions.

**Effort:** Trivial. Add `bin:` + `description:` lines to home view.

---

## 6. Lower-Priority Opportunities (Consider Later)

### 6.1 Benchmark harness (AXI's bench-github / bench-browser)

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

**§2 verdict:** This is arguably the *first* thing to ship -- without it we cannot validate or
refute any of the other principle-adoption bets.

### 6.2 Lifecycle capture (session-end hook enrichment)

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

### 6.3 Consistent `--query` flag for filtered views

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

### 6.4 Command shape enforcement

AXI's `runAxiCli()` rejects flags before command:
```
$ gh-axi --json issue list
error: Flags must come after the command. Try: gh-axi issue list --json
```

**kbagent conflict:** kbagent has `--json` as a *global* flag that works before or after the
command (Typer default). The rigid AXI rule conflicts with current UX.

**Recommendation:** Skip this. Accept both orders. Document the preferred order in SKILL.md.

### 6.5 Social video / marketing artifact

AXI has `bench-browser/social/` with HyperFrames composition for a race animation visualization
comparing AXI vs CLI vs MCP. It's a marketing artifact tied to the benchmarks.

**kbagent equivalent:** Only relevant if kbagent ships its own benchmark. Skip until §6.1 lands.

---

## 7. Bonus: kbagent-Specific AXI Applications

AXI principles are general. Here are three kbagent-specific opportunities that combine them.

### 7.1 Auto-dashboard on `kbagent doctor` (§2's preferred dashboard home)

`kbagent doctor` already exists. Extend it to also emit the home-view dashboard. If `--json`
is set, use TOON (if adopted); if TTY, use Rich. This is cheap to ship and becomes the
canonical "show me everything" command **without conflicting with bare `kbagent` REPL**.

§2.5 recommends this over §4.4 as the way to deliver the "home view" value.

### 7.2 `--hint toon` mode

`kbagent --hint client|service` already generates Python code. Add `--hint toon` that emits the
TOON representation of what the command would return (a kind of schema preview). Useful for
agent authors writing workflows.

### 7.3 Session-hook-aware `sync pull`

If ambient context (§4.5) is in place, `kbagent sync pull` can detect *"project A is on dev
branch `fix-etl`; main has drifted 14 commits ahead; workspace `ws-312` has pending queries"*
and surface it as part of sync output's `help[]`.

---

## 8. Adoption Roadmap -- Two Variants

§§3-7 describe the optimistic AXI-maximalist adoption. §2 argues for a more conservative plan.
Here are both; pick one (or blend).

### 8.A Ambitious roadmap (AXI-maximalist)

**Phase 1 -- "Token diet" (next minor, 1-2 weeks).** Agent cost drops ~30-40% on list workloads.
1. **TOON output** as opt-in `--format toon` (§4.1).
2. **Pre-computed aggregates** (`count`, `total`, `has_more`) in all lists (§4.2).
3. **Definitive empty states** (§5.3).
4. **Minimal default list schemas** + `--fields` opt-in, starting with `config list`,
   `job list`, `storage tables` (§5.5).
5. **Content truncation** on `config detail`, `job detail`, `component detail` with `--full`
   escape hatch (§5.1).

Update SKILL.md rules: *"Always use `--format toon` for agents."*

**Phase 2 -- "Guidance" (next minor after, 2-3 weeks).** Agent navigation improvements.
6. **`help[]` contextual disclosure** on every command (§4.3).
7. **Content-first home view** (`kbagent` no-args → dashboard in TOON, §4.4).
8. **Home view identifier** (bin + description at top, §5.6).
9. **Idempotent mutations** for `branch delete`, `config update` no-op, `sharing share`
   re-share (§5.2).
10. **Errors on stdout audit + test** (§5.4).

**Phase 3 -- "Ambient context" (4-6 weeks).** The killer feature.
11. **Session hooks self-install** for Claude Code + Codex (§4.5). Gated behind
    `kbagent init --session-hook --yes`. Include self-heal on every run.
12. **Session-end lifecycle capture** (§6.2).
13. **`kbagent bench`** (§6.1).

### 8.B Conservative roadmap (devil's-advocate, §2.5 flavor)

**Phase 1 -- "High-confidence wins" (1-2 weeks).** All clear-positive-ROI items.
1. **Minimal default list schemas + `--fields`** (§5.5).
2. **Content truncation with `--full`** (§5.1).
3. **`count` + `total` + `has_more`** on lists, **without** per-item derived fields (§4.2 part
   1 only).
4. **Home view identifier** in whatever dashboard ships (§5.6).

**Phase 2 -- "Measure, then expand" (3-4 weeks).** No new principle adoption without evidence.
5. **Build `kbagent bench`** (§6.1) comparing current kbagent vs kbc CLI vs MCP on 5 realistic
   tasks. Publish token/cost/reliability numbers.
6. **Auto-dashboard on `kbagent doctor`** (§7.1) -- ambient-context win without `kbagent`
   no-args hijacking.
7. **`help[]` on 5-10 high-value commands** (§4.3, selective).
8. **Idempotent mutations -- cheap cases only** (§5.2, 404 → no-op).

**Phase 3 -- "Evidence-gated bets" (conditional).** Only ship if bench shows a measurable win.
9. **TOON output** (§4.1) -- ship only if bench shows >20% session token savings.
10. **Session hooks** (§4.5) -- ship opt-in only, regardless of bench result.

---

## 9. Implementation Notes (the gotchas)

- **TOON Python library availability:** verify before committing to Phase 1 of 8.A (or
  Phase 3 of 8.B). If absent, factor in ~200 LOC encoder (spec at
  <https://toon-format.github.io>).
- **Backward compatibility:** `--json` remains the default for agents; TOON opt-in. Flip the
  default only after a few releases and only if agents ecosystem-wide support the format.
- **Plugin SKILL.md:** every principle change needs SKILL.md updates. Auto-generator
  (`make skill-gen`) already exists -- make sure it understands TOON.
- **`kbagent context`:** output should itself be TOON once TOON is the default. Self-hosting.
- **Tests:** add fixture-based golden-file tests for every list command's default field shape +
  TOON encoding. AXI ships vitest snapshots; we'd use `pytest` + `syrupy`.
- **Benchmark claim verification:** AXI's ~40% savings claim is based on specific workloads
  (browser + GitHub). For kbagent, run a mini-benchmark on `config list` + `job list` **before**
  shipping TOON; don't take the claim on faith. This is §6.1 serving as a gate for §4.1.
- **`help[]` drift:** SKILL.md decision table + references/ + `hints/` + `help[]` = four
  sources of "what to do next" guidance. Auto-generate `help[]` from `hints/` where possible to
  cut down on drift.
- **Session hook consent:** never default-on; interactive one-time confirmation with exact
  details of what gets written where.

---

## 10. What NOT to Copy

AXI has a few choices that don't fit kbagent:

- **No-interactive-prompts rule** -- kbagent intentionally has REPL for humans. Keep it but
  make sure it's never reachable from non-TTY (it is already).
- **Flags-after-command enforcement** (§6.4) -- breaks our Typer UX.
- **Single top-level home command** -- AXI tools like `tasks` are single-domain. kbagent is
  multi-domain (project, config, job, storage, ...). Home view should be ambient dashboard, not
  *"list tasks"* equivalent. Plus, REPL collision on bare `kbagent`.
- **Absolute dominance of TOON** -- we keep Rich for TTY humans. TOON (if adopted) is for
  `--json`-style machine reads only.
- **Default-on self-installing hooks** -- security / privacy boundary; must be opt-in for
  kbagent.

---

## 11. TL;DR for Humans

1. AXI is a **set of 10 design principles** with benchmarks proving agent-native CLIs beat both
   plain CLIs and MCP on cost, reliability, latency -- **on browser and GitHub workloads**
   (§2.1).
2. kbagent already aligns on JSON contract, exit codes, structured errors, and has things AXI
   never considered (`--hint` codegen, multi-project parallel, permission firewall, dev branch,
   GitOps sync) -- §2.2.
3. **5 AXI principles are gaps**; the three biggest (TOON, ambient context, `help[]`) look
   great in AXI benchmarks but likely deliver smaller wins on kbagent's mutation-heavy,
   stateful, multi-tenant workload -- §2.3.
4. **The four clearest kbagent wins** (in descending ROI): minimal default list schemas +
   `--fields` (§5.5), content truncation (§5.1), `count`/`total`/`has_more` on lists (§4.2
   aggregate half only), and a home view on `kbagent doctor` (§7.1). Ship these first. They
   are cheap, obvious, and un-controversial.
5. **Benchmark before TOON.** §4.1 promises 30-40% token savings; §2.3 argues real kbagent
   savings are 10-15%. Ship a `kbagent bench` harness (§6.1) first and let numbers decide.
6. **Session hooks are opt-in only.** §4.5 is AXI's flagship trick; for kbagent, the always-on
   token cost on non-kbagent Claude Code sessions makes it noise more often than signal.
7. **Pick roadmap 8.B (conservative)** if you want evidence before bets. Pick roadmap 8.A
   (ambitious) if you believe AXI's benchmarks generalize to kbagent. §2 argues for 8.B.

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
