# Agent Studio — Implementation Progress

> **Status as of 2026-05-19**: Phase 1 scaffold in progress on branch
> `feat/personal-ai-agents`. Cross-session continuity tracker — update
> at every meaningful commit so a new chat session can pick up without
> reading scrollback.

## Where the canonical docs live

| What | Where |
|---|---|
| Product PRD (v2) | [`docs/agents-v2.md`](agents-v2.md) |
| v1 PRD (superseded) | [`docs/agents.md`](agents.md) |
| Critical review of v1 | [`docs/agents-review.md`](agents-review.md) |
| NERD UI design system | [`docs/agent-studio-design-system.md`](agent-studio-design-system.md) |
| UI mockups (light primary, dark backup) | [`docs/mockups/`](mockups/) |
| ADR 0001 product boundary | [`docs/adr/0001-agent-office-product-boundary.md`](adr/0001-agent-office-product-boundary.md) |
| **This file (progress tracker)** | [`docs/agent-studio-progress.md`](agent-studio-progress.md) |

## Done so far (May 2026 session)

### Documentation + design

- ✅ `docs/agents-v2.md` written — Playbook-first PRD, addresses every
  blocking finding from `agents-review.md` (budget caps in MVP, scoped
  per-run JWTs, stable API contract, body_hash, 5s undo, untrusted
  wrapping, etc.).
- ✅ `docs/agent-studio-design-system.md` rewritten as NERD UI
  specification — light mode primary, dark secondary, single source of
  truth for visual contract.
- ✅ `docs/mockups/` — 6 light primary mockups (conditioning approach
  via Playwright + nano-banana edit mode) + 6 dark secondary backups.
  README documents the regen workflow.
- ✅ v2 PRD updates from Klint customer-validated workflow:
  - §9.3 `xlsx-renderer` added to first-party tools
  - §18 6th Solution `product-cost-allocation` (Finance Ops) with detail spec
  - §21 Phase 2 promoted basic view scoping (`created_by` + `allowed_users`) from Phase 5
  - §21 Phase 1 acceptance criterion includes Klint scenario
  - §24 Open Question #5 split (view scoping = Phase 2 ✓, approval routing = Phase 5+)
  - §26 Appendix E "Deployment Patterns" added

### Code

- ✅ `web/frontend/index.html` — anti-FOUC bootstrap defaults to light
  (`prefers-color-scheme: dark` users still get dark).

## Phase 1 scaffold — the first vertical slice

**Goal**: User opens `kbagent serve --ui`, clicks "Playbooks" in
sidebar, sees a library of Playbook cards loaded from YAML files on
disk. No run logic yet, no Tool Broker yet — just the data shape +
persistence + UI integration end-to-end.

### Implementation plan

```
src/keboola_agent_cli/agent_studio/
  __init__.py
  models/
    __init__.py
    playbook.py        # Pydantic Playbook + Step + Trigger etc.
  storage.py            # YAML load/save in ~/.config/.../playbooks/
                        # with 0600 perms
  sample_playbooks/     # Two ready-to-explore YAMLs

src/keboola_agent_cli/server/routers/
  agent_studio_playbooks.py   # /v1/agent-studio/playbooks router

# wired into src/keboola_agent_cli/server/__init__.py:create_app

tests/
  test_playbook_model.py
  test_playbook_storage.py
  test_playbook_router.py

web/frontend/src/
  state.tsx             # add "playbooks" PageId
  layout/Sidebar.tsx    # add Playbooks under AI / Tools
  App.tsx               # add Playbooks route
  pages/Playbooks.tsx   # library page
```

### Task tracker — Phase 1 scaffold (shipped)

| # | Task | Status | Lands in commit |
|---|---|---|---|
| 10 | Persistent progress doc + audit existing branch | ✅ done | `docs(agent-studio): v2 PRD + NERD UI ...` |
| 11 | Backend: Playbook Pydantic model | ✅ done | `feat(agent-studio): Phase 1 scaffold` |
| 12 | Backend: YAML storage with 0600 perms | ✅ done | `feat(agent-studio): Phase 1 scaffold` |
| 13 | Backend: FastAPI router | ✅ done | `feat(agent-studio): Phase 1 scaffold` |
| 14 | Tests: model + storage + router | ✅ done | `feat(agent-studio): Phase 1 scaffold` |
| 15 | Frontend: sidebar entry + state.tsx PageId | ✅ done | `feat(agent-studio): Phase 1 scaffold` |
| 16 | Frontend: Playbooks library page | ✅ done | `feat(agent-studio): Phase 1 scaffold` |
| 17 | Sample data + make check + commit | ✅ done — sample data skipped (TwoPathEmpty is the on-ramp) | `feat(agent-studio): Phase 1 scaffold` |

**Verification snapshot:**
- 27 new tests under `tests/test_playbook_*.py`, all green.
- `ruff check`, `ruff format --check`, `ty check` clean on changed files.
- `npx tsc --noEmit` clean on frontend.
- `git log --oneline feat/personal-ai-agents ^main` shows the two
  Phase 1 commits at the tip.

## Next slices (Phase 1 continuation, in priority order)

These are the things v2 PRD § 21 Phase 1 lists that the scaffold
**didn't** ship. Order is "what unblocks the most downstream work".

1. **Playbook detail drawer** in the UI — ✅ done
   (`feat(agent-studio): Playbook detail Drawer + two-step delete`,
   commit `5a85b47`). PlaybookCard is clickable, opens a right-side
   Drawer fed by `GET /v1/agent-studio/playbooks/{id}`, shows
   description + connections + skills + plugins + triggers (formatted
   JSON) + timestamps. Delete button uses a two-step confirm modal
   so destructive clicks are deliberate. New Playbook auto-opens its
   drawer to land the user on what they just produced.

2. **Run loop** — partial:
   - 2.a ✅ done (`feat(agent-studio): PlaybookRun stub …`). New
     `PlaybookRun` Pydantic model + YAML storage under
     `<config_dir>/runs/` + 3 endpoints
     (POST `/v1/agent-studio/playbooks/{id}/run` stub,
     GET `/v1/agent-studio/runs[?playbook_id=X]`,
     GET `/v1/agent-studio/runs/{run_id}`). Drawer gained a Run
     button and a Recent Runs section (truncated to 5, "+ N earlier"
     marker for the future Past Jobs tab). Backend stub marks runs
     `done` immediately with a clear "stub" summary — proves the
     data flow end-to-end without real execution.
   - 2.b **(next)**: tie Playbook execution into the existing
     `server/agent_runner.py` scheduler. `AgentRun` gains the new
     statuses (`blocked` / `waiting_for_approval` / `reviewing`) per
     § 23 migration. `PlaybookRun` becomes a thin specialisation of
     `AgentRun` so we get the SSE stream + cost tracking for free.

3. **Tool Broker primitives**: registry + risk-class enum + scoped
   per-run JWTs. No UI yet — the foundation that the Approval queue
   and the budget enforcer need.

4. **Budget enforcer**: hook into `server/pricing.py`, evaluate after
   every tool call, transition the run to `waiting_for_approval` or
   `failed` per the policy's `on_breach`.

5. **Approval queue + body_hash + 5s undo**: § 14 of the PRD. The UI
   side is the centered modal mocked in
   `docs/mockups/05-approval-modal.png`.

6. **Untrusted-content wrapping**: wrap every untrusted tool output in
   `<untrusted source="...">...</untrusted>` before passing back to
   the LLM. System-prompt invariant.

7. **Skill loader** for built-in skills under `plugins/kbagent/skills/`.

8. **Connection auto-discovery**: synthesise Connection YAMLs from the
   user's existing Keboola component configs.

9. **`data-cleanup` native plugin**: the Phase 1 use case (a) target.

Once 1–9 land, Phase 1 acceptance criteria from § 21 are met:
"User creates Playbook from `data-cleanup` template, runs it, hits
HITL pause at ER rules, approves, budget respected, final report +
lineage map in workspace."

## Branch state when this slice started

`git status` snapshot (existing uncommitted work from this branch
prior to the Phase 1 scaffold):

- Modified (pre-existing on branch):
  `Makefile`, `web/frontend/index.html`, `web/frontend/package.json`,
  `web/frontend/package-lock.json`, `web/frontend/src/App.tsx`,
  `web/frontend/src/layout/Sidebar.tsx`, `web/frontend/src/state.tsx`
- Untracked (this session's doc work, expected to be committed):
  `docs/agents-v2.md`, `docs/agent-studio-design-system.md`,
  `docs/mockups/`, `docs/agent-studio-progress.md` (this file),
  `plugins/.../build-app-over-kbagent-serve.md`, `scripts/dump_openapi.py`,
  `web/frontend/src/api/generated.ts`, `web/frontend/src/api/openapi.json`,
  `web/frontend/src/api/types.ts`, `web/frontend/src/apps/`,
  `web/frontend/src/vite-env.d.ts`

`web/frontend/src/apps/` is a **dynamic app registry** with
`app:<slug>` page IDs — pre-existing infrastructure for user-contributed
apps. Agent Studio Playbooks is a **builtin** page (first-class
feature), not an app, so we add to `BuiltinPageId` not `apps/_registry`.

## Commit strategy

- Frequent commits at logical chunks (model done; storage done; router
  done; tests passing; frontend wired).
- Conventional commit prefix `feat(agent-studio):` or `chore(agent-studio):`.
- **NO** `Co-Authored-By` line per user's CLAUDE.md.
- **NO** AI attribution footer in PR description.
- Run `ruff check && ruff format --check && ty check` on changed files
  before every commit (`make check` does all of it including pytest).

## How to resume in a new session

1. Open this file.
2. Check the "Task tracker" table — find first `pending` or
   `in_progress` row.
3. Use `git log --oneline feat/personal-ai-agents ^main` to see what's
   already shipped.
4. `git status` to see in-flight changes.
5. Continue from the next pending task.

If the task tracker is stale relative to git history, trust the git
history and update this file.
