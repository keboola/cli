# Choosing a branching model

kbagent supports two ways to map your git repo to Keboola branches. Pick one up
front — it changes how `sync init` is run and what `push` touches.

## The two models

### Model A — Single-branch / production-direct
- Each project directory maps to **one Keboola project's production branch**.
- `.keboola/branch-mapping.json` stays at its default (`null` = production).
- `sync pull` / `sync push` read and write that project's production branch directly.
- Promotion across environments (dev → prod project) is a **git merge** between
  project directories/repos, then a `push` to the next project (the demo's L0→L1
  shape).

**Choose A when:**
- You're experimenting, or driving a single dev project locally instead of kbc.
- Your "environments" are *separate Keboola projects* (L0/L1), not dev branches.
- You want the simplest mental model and fewest moving parts.

**Trade-off:** a `push` writes straight to the production branch of that project —
there is no isolated staging copy inside Keboola. Review happens in git, not in KBC.

### Model B — Git-branching (Keboola dev-branch isolation)
- `sync init --git-branching` creates `.keboola/branch-mapping.json`.
- Each **git branch** links to a **Keboola development branch** (an isolated server-
  side copy) via `kbagent sync branch-link --branch-name <git-branch>`.
- Work on a PR branch → `push` lands in its Keboola dev branch (safe, isolated);
  merge to `main` → `push` lands in production.
- `kbagent sync branch-status` shows the mapping; `branch-unlink` detaches.

**Choose B when:**
- Multiple people open PRs against the same project and you want each change tested
  in isolation inside Keboola before it hits production.
- You already use Keboola's development-branches feature.
- You want PRs to never write production directly.

**Trade-off:** more lifecycle to manage (create/link/unlink dev branches, clean them
up), and the mapping file is per-clone state.

## Decision shortcut

| Your situation | Model |
|---|---|
| "I just want to manipulate one project with kbagent instead of kbc" | **A** (start here) |
| Separate dev/prod **projects** promoted by git merge (L0/L1) | **A** |
| PR-per-change, multiple contributors, want isolated server-side testing | **B** |
| You rely on Keboola development branches today | **B** |

You can start on **A** and adopt **B** later: run `sync init --git-branching` and
`branch-link` when you actually need per-PR isolation. Moving A→B is additive (it adds
a mapping file); it does not require re-converting the config tree.

## How the model shows up in commands

```bash
# Model A (production-direct) — nothing special:
kbagent sync pull  --project <alias> -d <dir>
kbagent sync push  --project <alias> -d <dir>

# Model B (git-branching):
kbagent sync init  --git-branching --project <alias> -d <dir>
git checkout -b feature/x
kbagent sync branch-link   --project <alias> -d <dir> --branch-name feature/x
kbagent sync pull/push     --project <alias> -d <dir>     # now targets the dev branch
kbagent sync branch-status --project <alias> -d <dir>
```
