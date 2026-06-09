# Migration runbook — kbc → kbagent (PR sequence)

The ordered, low-risk way to cut a repo over. This is a **clean cutover**, not a
coexistence: kbc (`config.json`/`meta.json`) and kbagent (`_config.yml`) cannot both
own the same tree (live-verified — see the SKILL.md reality note). Plan it as one
conversion PR plus housekeeping, then everyone re-branches from the new `main`.

## Answer to "can I transition seamlessly to a new branch?"
No co-existence, but yes a controlled cutover:
1. One **conversion PR** flips the whole repo from JSON→YAML + swaps the workflows.
2. Merge it to `main`.
3. **Delete/redo every old branch** — they carry the incompatible kbc layout and can
   never cleanly merge into the converted `main`.
4. Everyone branches fresh from the new `main` with the new workflows.

## Pre-flight (do once, before any PR)
- [ ] **Announce a change freeze** on the repo + the Keboola projects for the
      conversion window. Any config edit made in the UI between "pull" and "cutover"
      becomes drift you'll chase. Keep it short.
- [ ] **Pick a kbagent version** and pin it (`keboola-agent-cli==X.Y.Z` or
      `git+...@vX.Y.Z`). Never unpinned on a prod lane.
- [ ] **Set GitHub secrets**: one `KBC_TOKEN_<ALIAS>` per project (see
      `references/secrets-setup.md`).
- [ ] **Create GitHub Environments** `dev` + `prod`; add required reviewers to `prod`.
- [ ] **Inventory** with the skill's analyzer (dry-run): confirm every project and the
      legacy files it will replace.
      `python <skill>/scripts/migrate_cicd.py /path/to/repo`

## PR 1 — Conversion (the big one)  →  branch `migrate/kbc-to-kbagent`
Do the projects one at a time; **start with a non-prod project (e.g. `dev`/`L0`)**.

Per project `<DIR>` (with its token in the env):
```bash
export KBAGENT_PROJECT_FROM_ENV=1 KBC_TOKEN=$TOKEN \
       KBC_STORAGE_API_URL=https://<stack>
kbagent sync init --adopt-existing --project __env__ --directory <DIR>
kbagent sync pull --force        --project __env__ --directory <DIR>   # writes _config.yml
# Drop the orphaned kbc files kbagent does not read:
find <DIR> \( -name config.json -o -name meta.json \) -exec git rm -q {} +
# VERIFY BY BEHAVIOR (must be clean before you trust the project):
kbagent sync diff --project __env__ --directory <DIR>
```
Acceptance for each project: `sync diff` shows `0 to create, 0 to update, 0 to delete`.
- If a handful of **scheduler / variables** configs show as "to create" (a known
  wrinkle from the live test), pull again and confirm they settle; if they persist,
  note them in the PR and reconcile manually before enabling push.

Then, still on the same branch:
```bash
# Generate the clean kbagent-native workflows:
python <skill>/scripts/migrate_cicd.py /path/to/repo --write --version X.Y.Z
# Remove the legacy kbc CI (the analyzer listed these):
git rm -r .github/actions/kbc_* .github/workflows/KBC_*.yml   # adjust to your repo
git add -A && git commit -m "Migrate kbc -> kbagent: convert configs + workflows"
```

**Review this PR by behavior, not by diff.** The reformat touches hundreds of files;
reading it line-by-line is pointless. Trust:
- the `kbagent-validate` workflow runs on the PR and `sync diff` is clean per project;
- a `sync push --dry-run` (also in validate) reports no changes.

Merge to `main` once validate is green.

## PR 2 — Housekeeping (optional, after merge)
- [ ] Branch protection on `main`; require the `kbagent-validate` check.
- [ ] Tune the pull schedule cron / push approval reviewers.
- [ ] Update the repo README to the new install + commands.
- [ ] Decide the branching model (next section).

## After merge — start over
- [ ] **Close or recreate every open PR** that was based on the kbc layout. They diff
      against JSON files that no longer exist; rebasing them is not worth it — redo the
      change on a fresh branch from the converted `main`.
- [ ] **Delete stale feature branches** (`git push origin --delete <branch>`).
- [ ] Tell contributors to **re-clone or hard-reset** to the new `main`.
- [ ] First real push: run `kbagent push` (workflow_dispatch) to `dev` first, approve,
      verify in the Keboola UI, then to `prod`.

## Branching model — pick one
| Model | When | How |
|---|---|---|
| **Single-branch (production)** | Each git branch/project maps straight to a production Keboola project (the demo's L0/L1 promotion) | Leave `.keboola/branch-mapping.json` at default (null = production); gate prod pushes via the GitHub Environment |
| **Git-branching (dev isolation)** | You want each PR to deploy to an isolated Keboola dev branch, then merge to prod on merge-to-main | `kbagent sync init --git-branching`; `kbagent sync branch-link --project __env__ --branch-name <pr-branch>` in the PR workflow; `main` maps to production |

For most teams already doing PR-per-change promotion, **git-branching** is the closer
fit and is safer (no direct prod writes from PRs). Migrate to it in PR 2, not PR 1.

## Hard guardrails (repeat to the user)
- **Never** `kbagent sync push` against an adopted-but-not-yet-pulled kbc tree — it
  reports every config as "to delete" (136 in the live test) and `--allow-delete`
  would wipe the project.
- **Never** `--allow-plaintext-on-encrypt-failure` in CI.
- Keep the change freeze until `main` is converted and the first dev push is verified.
