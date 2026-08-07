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
- [ ] **Pick a kbagent version** and pin it (`keboola-cli==X.Y.Z` or
      `git+...@vX.Y.Z`). Never unpinned on a prod lane.
- [ ] **Set GitHub secrets**: one `KBC_TOKEN_<ALIAS>` per project, valued with a
      PAT from `kbagent auth pat-create --project-id <id>` (v0.81.0+), not a
      raw Storage token (see `references/secrets-setup.md`).
- [ ] **Create GitHub Environments** `dev` + `prod`; add required reviewers to `prod`.
- [ ] **Inventory** with the skill's analyzer (dry-run): confirm every project and the
      legacy files it will replace.
      `python <skill>/scripts/migrate_cicd.py /path/to/repo`

## PR 1 — Conversion (the big one)  →  branch `migrate/kbc-to-kbagent`
Do the projects one at a time; **start with a non-prod project (e.g. `dev`/`L0`)**.

**Use plain `sync init` — never `--adopt-existing`.** Root-caused on project 153
(kbagent v0.80.0, 2026-08): `--adopt-existing` carries kbc's row paths
(`values/...` for `keboola.variables`, `codes/...` for `keboola.shared-code`) and
kbc's `relations`-based companion-config paths straight into the kbagent manifest
without translating them. That permanently leaves phantom `added`/`deleted`
entries in `sync status` (kbagent's own untracked-row scanner only recognizes a
literal `rows/` path segment; kbc's inherited `values/`/`codes/` rows never match
it), and a `sync push` against those phantom "added" rows would call
`create_config` and create duplicate sibling configs, not update the rows they
actually are. Plain `sync init` has none of this: it starts a brand-new empty
manifest and ignores every file it doesn't recognize, so pointing it at a
directory still full of kbc's `config.json`/`meta.json` tree is safe — the
following `sync pull` populates everything fresh through kbagent's own
naming/path logic, with no inherited kbc paths at all. Verified: a plain
`init`+`pull` against the same project reached `sync status` = `0 added, 0
modified, 0 deleted` — genuinely clean, not "a small acceptable residual."

**One required prep step: delete kbc's manifest file first.** kbc and kbagent
write to the identical path, `<DIR>/.keboola/manifest.json`, and plain `sync init`
refuses to run while that file exists (`Error: Manifest already exists at
.../.keboola/manifest.json. Use 'sync pull' to update, 'sync init
--adopt-existing' ..., or delete .keboola/ to reinitialize.`) — confirmed live,
2026-08-06. This is the ONE file you delete before `init`, not the
`config.json`/`meta.json` config tree — those stay in place and get cleaned up
only after the pull below.

Per project `<DIR>` (with its token in the env):
```bash
export KBAGENT_PROJECT_FROM_ENV=1 KBC_TOKEN=$TOKEN \
       KBC_STORAGE_API_URL=https://<stack>
rm <DIR>/.keboola/manifest.json   # kbc's manifest -- same path kbagent needs
kbagent sync init --project __env__ --directory <DIR>
kbagent sync pull        --project __env__ --directory <DIR>   # writes _config.yml
# Drop the orphaned kbc files kbagent does not read:
find <DIR> \( -name config.json -o -name meta.json \) -exec git rm -q {} +
# VERIFY BY BEHAVIOR (must be genuinely empty before you trust the project):
kbagent sync status --directory <DIR>
kbagent sync diff --project __env__ --directory <DIR>
```
Acceptance for each project: `sync status` shows `0 added, 0 modified, 0 deleted`
**and** `sync diff` shows `0 to create, 0 to update, 0 to delete`. Do not accept
"a handful of leftover entries" as normal and push through it — with plain init
there should be none; if there are, stop and diagnose before moving to the next
project or enabling push.

**Also clean up kbc-only type folders — they are left behind whole, not just
emptied of `config.json`/`meta.json`.** kbc's naming has a finer-grained
component-type taxonomy than kbagent's: kbc buckets configs into `extractor/`,
`writer/`, `transformation/`, `application/`, **`processor/`**, **`app/`**
(data apps), `_shared/` (shared code), `variables/`, `schedules/`. kbagent only
recognizes `extractor` / `writer` / `transformation` / `application` and folds
**everything else — processors, data apps, shared code — into a flat `other/`**
(`COMPONENT_TYPE_MAP` in `sync/config_format.py`; kbagent never applies kbc's
dedicated `dataAppConfig` naming template even though the manifest model still
carries the field for read compatibility). After `sync pull` rewrites those
configs under `other/<component_id>/...`, the old `app/`, `processor/`, and
`_shared/` directories are orphaned — but **they are not empty**: besides
`config.json`/`meta.json` (already removed above), kbc also writes
`description.md` and the code body itself (`code.sql`/`code.py`/`code.txt`/
`code.txt` under `_shared/.../codes/...`) into these folders, none of which
kbagent reads either. A `find -empty -delete` is a no-op against them — confirmed
live, 2026-08-06 (19 leftover files, zero dirs matched `-empty`). Remove the
whole subtree instead, in the same commit as the `config.json`/`meta.json`
cleanup:
```bash
git rm -rq --ignore-unmatch <DIR>/*/app <DIR>/*/processor <DIR>/*/_shared
```
(adjust the glob to your branch layout — kbc nests these under the branch
directory, e.g. `main/app`, `main/processor`, `main/_shared`). Re-run `sync diff`
after — it should be unaffected (these folders were never manifest-tracked;
deleting them doesn't touch any config kbagent knows about); verified live: diff
stayed "No differences found" before and after removing all 19 leftover files.

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

**This whole sequence is an ordinary commit + ordinary PR merge — never a git
history rewrite.** The conversion diff is huge and it's tempting to reach for
`git filter-repo`, an orphan-branch reset, or a force-pushed squash of `main` to
make history "clean." Do not suggest this to a customer: it invalidates every
collaborator's clone and open PR, destroys `git blame`/audit trail across the
*entire* repo (a compliance concern, not just an inconvenience, for regulated
customers), and a coordinated force-push to `main` is exactly what most orgs'
branch-protection rules exist to block. "Close/redo open PRs, delete stale
branches, re-clone" above is already disruptive enough as ordinary git hygiene —
that is the actual answer to "the diff is too big to review," not a rewritten
history.

## Branching model — pick one
See [references/branching-model.md](branching-model.md) for the full decision table
(single-branch vs. git-branching). Migrate to git-branching in PR 2, not PR 1, if
you choose it — it's additive and doesn't require re-converting the config tree.

## Hard guardrails (repeat to the user)
- **Never** `kbagent sync push` (without `--dry-run`) against an adopted-but-not-yet-
  pulled kbc tree. In the original 2026-06 live test this reported every config as
  "to delete" (136) and `--force` would have wiped the project; re-verified on
  kbagent v0.80.0 (2026-08, same project) this no longer happens — `sync diff` now
  reports untouched configs as `never_fetched`, not `deleted`. Treat the old figure as
  a historical regression, not current behavior, but keep the rule: always
  `sync pull` and confirm a clean `sync diff` before the first real push, on any
  kbagent version.
- **Never** `--allow-plaintext-on-encrypt-failure` in CI.
- **Never `--all-projects` in a migration repo directory.** It's hard-coded to
  `<directory>/<alias>/` for every registered project alias and (for pull)
  auto-inits a fresh tree there if none exists — confirmed to silently create a
  second, unrelated `<alias>/` directory alongside an existing flat manifest.
  Always `--project ALIAS --directory DIR` explicit, matching what the generated
  CI already does.
- Keep the change freeze until `main` is converted and the first dev push is verified.
