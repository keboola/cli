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

### Task tracker

| # | Task | Status |
|---|---|---|
| 10 | Persistent progress doc + audit existing branch | in_progress |
| 11 | Backend: Playbook Pydantic model | pending |
| 12 | Backend: YAML storage with 0600 perms | pending |
| 13 | Backend: FastAPI router | pending |
| 14 | Tests: model + storage + router | pending |
| 15 | Frontend: sidebar entry + state.tsx PageId | pending |
| 16 | Frontend: Playbooks library page | pending |
| 17 | Sample data + make check + commit | pending |

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
