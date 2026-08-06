---
name: kbagent-cicd-migration
description: >
  Use when migrating an existing kbc (keboola-as-code) GitHub CI/CD pipeline to
  the new kbagent (keboola-agent-cli) sync engine. Covers: converting per-project
  pull/push PR workflows, multi-project repos (e.g. L0/L1 dev->prod promotion),
  branch->environment mapping, GitHub secrets/variables/environments setup, the
  install step (uv tool install instead of downloading a Go binary), and the
  kbc->kbagent command/flag/env-var mapping. Triggers: migrate CI/CD, migrate
  pipeline, kbc to kbagent, port GitHub Actions, CLI-based-sync-demo, kbc pull
  push CI, project-as-code CI migration, replace kbc binary in CI, gitops
  migration, multi-project promotion, dev to prod Keboola, KBC_STORAGE_API_TOKEN
  to KBC_TOKEN, sync push CI, sync pull CI.
---

# kbc -> kbagent CI/CD Migration

Guides a customer through porting a `kbc` GitHub CI/CD pipeline (the
[CLI-based-sync-demo](https://github.com/keboola/CLI-based-sync-demo) shape:
per-project pull/push, multi-project promotion, branch-gated deploys) to the new
`kbagent sync` engine, emitting **clean kbagent-native workflows**.

## Reality check — this is a one-time BREAKING migration, not a command swap

Do **not** tell the user they can just swap `kbc` for `kbagent` in place. Three
hard incompatibilities make this a deliberate cutover (verified against the code):

1. **The on-disk layout/format is different and incompatible.**
   - `kbc` writes per config: `config.json` + `meta.json` + `description.md` (JSON).
   - `kbagent` writes per config: **`_config.yml`** (YAML, with `name`/`description`/
     `parameters` hoisted + a `_configuration_extra` block) + extracted code files
     (`constants.py:425`, `sync/config_format.py`).
   - The first `kbagent sync pull` therefore **rewrites every configuration** into a
     new format. The old `config.json`/`meta.json` files are **not read** by kbagent
     and become orphans that must be deleted. Expect a **massive reformatting diff**.
2. **kbagent sync is an ORCHESTRATOR, not cwd-per-folder.** `kbc pull` runs against
   whatever directory you `cd` into. `kbagent sync pull` *requires* `--project ALIAS`
   (resolved from a central config store) or `--all-projects` (`sync.py:67,495`). In
   CI we bridge this with env-injection: `KBAGENT_PROJECT_FROM_ENV=1` synthesizes a
   project under the reserved alias `__env__`, and every command passes
   `--project __env__ --directory <folder>`.
3. **The two tools cannot co-own the same tree.** Because the source-of-truth files
   differ, you cannot have `kbc push` and `kbagent push` both treating one directory
   as canonical. You must cut over.

### Consequence (this is what the user correctly anticipated)
- The migration is a **one-time conversion commit** on a **dedicated branch**, where
  the JSON tree is replaced by the YAML tree. Reviewing that diff line-by-line is
  impractical; you verify by **behavior** (`sync diff` clean, dry-run push empty), not
  by reading the reformat. Merging it to `main` is effectively a tooling version bump.
- Until cutover, keep the legacy `kbc` workflows; afterwards delete them in the same PR.

> **Live-verified (project 153, GCP europe-west3, 2026-06):**
> - `KBAGENT_PROJECT_FROM_ENV=1` + `--project __env__` works for `init` and `pull`.
> - kbc pulled **143 `config.json` + 187 `meta.json`** (JSON); kbagent pulled
>   **147 `_config.yml`** (YAML). Zero overlap in file format.
> - `sync init --adopt-existing` on a **kbc-produced tree** succeeds, but the very
>   next `sync diff` reports **`0 to create, 0 to update, 136 to delete`** — kbagent
>   does not read kbc's `config.json` at all, so it sees every existing config as a
>   local deletion. **A `sync push --force` here would delete all 136 remote
>   configs.** Adopt-existing adopts only the manifest, NOT the configs.
> - The correct path — adopt → `sync pull --force` (writes `_config.yml`) → `git rm`
>   the orphaned `config.json`/`meta.json` — converged the diff from 136 deletes to
>   ~2 (plus ~9 remote-only scheduler/variables configs that pull/diff treat
>   inconsistently — verify these per project). Both file sets coexist after pull
>   until you delete the kbc files, so the `git rm` step is mandatory, not optional.
>
> **Re-verified (same project 153, kbagent v0.80.0, 2026-08): the 136-delete footgun
> no longer reproduces.** kbagent fixed `adopt-existing` between the original test and
> v0.80.0. Current behavior on a fresh `kbc`-produced tree (137 `config.json` + 181
> `meta.json`):
> - `sync init --adopt-existing` + `sync diff` now reports
>   `added: 0, deleted: 0, remote_only: 9, never_fetched: 110` — **no false deletes.**
>   Configs kbagent hasn't pulled yet show as `never_fetched`, not `deleted`.
> - After `sync pull`, diff converges to `added: 9, deleted: 2, unchanged: 118` — a
>   handful of genuine variable-row drift, not a mass delete. `sync push --dry-run`
>   confirms: "would create 9, update 0, delete 2."
> - The orphaned-file claim **still holds**: 318 `config.json`/`meta.json` files
>   coexisted with 146 new `_config.yml` files after pull. Deleting the orphans
>   produced an *identical* `sync diff` summary before and after, confirming kbagent
>   truly never reads them and the cleanup step is safe (though no longer
>   safety-critical the way the delete-136 scenario was).
>
> **Takeaway:** re-verify the adopt/pull numbers against whichever kbagent version you
> are actually shipping — this mechanic has changed at least once. Don't quote the
> 136-delete figure as current behavior; it's a historical regression, not a standing
> hazard. The `git rm` orphan-cleanup step and the general "verify by behavior, not by
> reading the diff" guidance remain correct regardless of version.
>
> **Operator rule:** even though the mass-delete footgun is currently fixed, still
> never run `sync push` (without `--dry-run`) against an adopted-but-not-yet-pulled
> kbc tree. Always `sync pull` first, confirm `sync diff` is clean, THEN enable the
> push lane — a future regression or a customer on an older kbagent version could
> reintroduce the original failure mode.

> **Root-caused (2026-08, project 153, kbagent v0.80.0): `--adopt-existing` never
> reaches a clean `sync status`, and DO NOT recommend it — use plain `sync init`
> instead (see "Recommended mechanic" below).** After adopt + pull, `sync status`
> still showed `added: 9, deleted: 1` even after the orphan-file cleanup. Traced to
> the manifest, not to remote drift:
> - `sync init --adopt-existing` carries kbc's row paths **verbatim** into the
>   kbagent manifest — `values/{name}` for `keboola.variables`, `codes/{name}` for
>   `keboola.shared-code` (kbc's own naming templates). A subsequent `sync pull`
>   refreshes file *content* at an already-tracked row's existing path but never
>   renames/relocates it to kbagent's own row convention.
> - kbagent's *own* row-path generator, exercised on a genuinely new row, always
>   writes `rows/{name}` — confirmed by a side-by-side test: a **fresh** `sync init`
>   (no adopt) + `sync pull` against the same project produced rows at
>   `.../rows/default-values/_config.yml`, never `.../values/default/`.
> - `_find_untracked_configs` (the scanner behind both `sync status`'s "added" list
>   and `sync push`'s create-plan) only excludes row files by checking for a literal
>   `"rows"` path segment (`sync_service.py`). A row inherited at `values/...` or
>   `codes/...` from adopt doesn't match that check, so **the scanner reports it as a
>   brand-new top-level configuration**, with an empty `config_id` since it isn't
>   actually one. **Pushing it would call `create_config` for a whole new sibling
>   `keboola.variables`/`keboola.shared-code` configuration** (Phase A create path,
>   not the row-update path), not update the row it actually is — a real duplicate-
>   config risk on push, not just a cosmetic status mismatch.
> - Separately, kbc's own manifest.json stores **several unrelated companion
>   `keboola.variables` configs at the same literal bare path `"variables"`**,
>   resolving the true nesting only via a `relations` field kbagent's adopt path
>   ignores entirely. Only one of those collides onto a real file after pull; the
>   rest sit in the manifest with a stale `pull_hash` and no file at their recorded
>   path → phantom `deleted` entries. This is a kbc-schema quirk (relation-based
>   path resolution) that kbagent's manifest model has no equivalent for.
> - **A plain `sync init` (no `--adopt-existing`) + `sync pull` against the same
>   project, verified twice (into an empty dir, and into a dir still containing kbc's
>   untouched `config.json`/`meta.json` tree) produced `added: 0, modified: 0,
>   deleted: 0, unchanged: 119` — a genuinely clean `sync status`.** Plain `init`
>   ignores every foreign file it doesn't recognize; it only requires that
>   `.keboola/manifest.json` not already exist. This is now the recommended
>   mechanic — see below.
>
> **Re-verified end-to-end (2026-08-06, fresh `kbc init` pull of project 153 into
> an empty directory, kbc dev build + kbagent v0.80.0)**, side by side:
> - Plain `sync init` against the as-is kbc tree fails fast and by design:
>   `Error: Manifest already exists at .../.keboola/manifest.json.` — because kbc
>   and kbagent write to the identical path. `rm .keboola/manifest.json` (leaving
>   every `config.json`/`meta.json` untouched), then plain `sync init` + `sync pull`
>   reached `sync status` = "No local changes detected. (119 configurations tracked)"
>   and `sync diff` = "No differences found." — genuinely, provably clean.
> - `sync init --adopt-existing` + `sync pull` against an identical untouched copy
>   of the same tree reproduced the phantom-rows bug exactly as described above:
>   `sync status` reported 9 added / 1 deleted, and `sync diff` planned 9 creates +
>   2 deletes — confirmed to be `keboola.variables`/`keboola.shared-code` rows
>   inherited at kbc's `values/`/`codes/` paths, never relocated to kbagent's own
>   `rows/` convention.
> - **Confirms the recommendation, but the write-up above was missing an
>   operational step:** "plain init, no adopt-existing" cannot be run verbatim
>   against a directory straight out of `kbc pull` — `.keboola/manifest.json` is
>   always already there. The one-line fix, now folded into Step 3b below, is to
>   `rm` that single file (never the `config.json`/`meta.json` tree it sits next
>   to) immediately before the plain `init` call.

### What still carries over unchanged
The *orchestration shell* is CLI-agnostic: manual/scheduled pull that commits state,
PR validation, GitHub-Environment-gated push, branch→env mapping, per-project loops.
Only three mechanics change: **install** (`uv tool install` not a binary download),
**commands/flags** (`kbagent sync ...`, see
[references/command-mapping.md](references/command-mapping.md)), and **auth env vars**
(`KBAGENT_PROJECT_FROM_ENV=1` + `KBC_TOKEN` + `KBC_STORAGE_API_URL`).

## Prerequisites — ask for these before Step 1

Four things this skill cannot infer; get them from the customer/operator first:

- **The repo path.** Where the `kbc`-managed tree (with `.keboola/manifest.json`
  and `.github/workflows/`) actually lives locally — Step 1's `migrate_cicd.py`
  argument.
- **A `kbagent` binary or install.** Either already on `PATH`, or install it now:
  `uv tool install keboola-agent-cli==<ver>` (see Step 2 for version pin) or a
  downloaded standalone binary. No local `kbc` binary is required to *run* the
  migration (kbagent is the only tool that touches the repo from Step 3b on) —
  only to *verify* the "same data, new layout" claim by diffing a `kbc pull`
  against a `kbagent sync pull` of the same project, which is optional.
- **Storage API host + token for each project being converted.** One pair per
  project (`KBC_STORAGE_API_URL` + `KBC_TOKEN`, fed via
  `KBAGENT_PROJECT_FROM_ENV=1`, see "auth env vars" above) — needed for every
  `sync init`/`pull`/`diff`/`push` in Step 3b. Never ask the customer to paste a
  token into chat; read it via the clipboard-secret pattern or point at wherever
  they already store it (a registered `kbagent project`, a CI secret, a
  password manager) and reference it by path/alias, not by value.
- **Which project to convert first.** Always non-prod — confirmed explicitly in
  "How to run this" below, not assumed.

## How to run this — ask the customer, don't auto-pilot

This skill reads like a linear script, and it is tempting to run Steps 1-6
end-to-end without stopping. **Don't.** Every step below that touches the
customer's repo or their live Keboola project is a decision the customer
should make, not one you make for them — this is their production CI/CD and
their production data. Treat the numbered steps as a checklist of decisions to
surface, not a batch job to execute. Concretely, stop and ask before you:

- **Pick the version pin (Step 2).** Don't default to "latest" or to whatever
  version happens to be installed on your machine — ask which lane (prod vs.
  scratch) this repo is, and let the answer decide pinned vs. unpinned.
- **Write anything (`--write` in Step 3).** Show the dry-run output first,
  let the customer review the projects/legacy-files inventory, then ask
  before generating files into their repo.
- **Run `sync init` / `sync pull` against a real project (Step 3b).** This is
  the breaking, one-time conversion — confirm which project to convert first
  (always non-prod), and confirm the customer is fine with the change freeze
  being in effect before you touch anything. Use plain `sync init` (no
  `--adopt-existing`, see Step 3b) and require a genuinely empty `sync status`
  before calling a project converted.
- **Delete any file** — orphaned `config.json`/`meta.json`, or the now-empty
  kbc-only type folders (`app/`, `processor/`, `_shared/`, see the reality
  check above). Show what you're about to delete and why (they are no longer
  read by kbagent) and let the customer say go, especially the first time
  through — don't `git rm` on their behalf and mention it after the fact.
- **Choose the branching model (Step 5).** Single-branch vs. git-branching is
  a workflow/process decision for their team, not a default you pick because
  it's "usually better." Present the decision table and wait for their answer.
- **Enable the push lane / run a real `sync push`.** Dry-run first, always;
  a real push against a customer's project needs their explicit go-ahead
  every time, not just once at the start of the migration.

If you're running this yourself against a live project to verify the skill's
claims (as opposed to guiding a customer), the same discipline still applies:
narrate what you're about to run and why before you run it, rather than
chaining the whole sequence unattended — that's how stale/incorrect claims in
this skill (like the historical "136 to delete" figure) go unnoticed for two
months, and how you end up cleaning up things you didn't realize you'd need to.

## Workflow

### Step 1 — Analyze the existing repo (always start here)
Run the engine in dry-run mode to inventory projects and the legacy CI it replaces:

```bash
python <skill_dir>/scripts/migrate_cicd.py /path/to/repo
```

It prints every project found (one per `.keboola/manifest.json`), each project's
id / stack host / required token secret name, any `ignoredComponents` ("subset of
a project" — see Step 5), and the legacy `kbc` workflow/action files it supersedes.

### Step 2 — Pick a version pin (decide before generating)
- **Pinned (recommended for prod lanes):** `--version 0.58.0` (PyPI, once published)
  or `--git-ref v0.58.0` (git tag, until PyPI exists). Reproducible CI.
- **Unpinned (`keboola-agent-cli`, resolves to latest):** only acceptable for a
  non-prod/scratch lane. Warn the user: unpinned + the current auto-update behavior
  means non-deterministic CI runs.

### Step 3 — Generate the clean workflows
```bash
python <skill_dir>/scripts/migrate_cicd.py /path/to/repo --write \
    --version 0.58.0 --main-branch main --schedule "0 * * * *"
```
Produces:
- `.github/workflows/kbagent-validate.yml` — on PR: `sync diff` + `sync push --dry-run` per project (read-only drift + secret-encryption preflight).
- `.github/workflows/kbagent-pull.yml` — manual + optional cron: `sync pull --force` per project, commits state back.
- `.github/workflows/kbagent-push.yml` — manual, **GitHub-Environment-gated**: `sync push` per project, with an `allow_delete` input.

The legacy `kbc` files are **left in place** — review the new ones, then delete the
old workflows/actions in the same PR.

### Step 3b — Perform the one-time config conversion (dedicated branch)
This is the breaking part. On a fresh migration branch, for each project, convert
the JSON tree to kbagent's YAML tree and remove the orphaned kbc files.

**Recommended mechanic: plain `sync init` — do NOT use `--adopt-existing`.**
`--adopt-existing` carries kbc's row paths (`values/...`, `codes/...`) and its
`relations`-based companion-config paths straight into the kbagent manifest without
translating them to kbagent's own conventions. That's confirmed to leave a
permanently-dirty `sync status` (phantom `added`/`deleted` entries — see the
root-cause note above) and, worse, makes `sync push` create duplicate sibling
configs for the misclassified rows. Plain `init` has none of this baggage: it
creates a brand-new empty manifest and ignores every file it doesn't recognize
(kbc's `config.json`/`meta.json`/legacy folders included) — **except one**: kbc
and kbagent both write their manifest to the exact same path, `.keboola/manifest.json`.
Plain `sync init` refuses to run when that file already exists (`Error: Manifest
already exists at .../.keboola/manifest.json. Use 'sync pull' to update, 'sync
init --adopt-existing' to adopt a kbc-written manifest, or delete .keboola/ to
reinitialize.`) — confirmed live, this is not a hypothetical. **You must delete
kbc's manifest file (only that one file, not the `config.json`/`meta.json` config
tree) before plain `init` will run.** Once it's gone, pointing plain `init` at a
directory that still contains kbc's `config.json`/`meta.json` tree is safe — `pull`
then populates everything through kbagent's own naming/path logic from scratch.

```bash
git checkout -b migrate/kbc-to-kbagent
# Per project (do ONE non-prod project first and verify):
# NOTE: plain init, no --adopt-existing. kbc and kbagent share the same
# manifest path (.keboola/manifest.json), so plain init refuses to run
# until that one file is out of the way -- delete it first.
rm L0/.keboola/manifest.json
KBAGENT_PROJECT_FROM_ENV=1 KBC_TOKEN=$L0_TOKEN KBC_STORAGE_API_URL=https://connection.keboola.com \
  kbagent sync init --project __env__ --directory L0
KBAGENT_PROJECT_FROM_ENV=1 KBC_TOKEN=$L0_TOKEN KBC_STORAGE_API_URL=https://connection.keboola.com \
  kbagent sync pull --project __env__ --directory L0
# Remove orphaned kbc files that kbagent never reads: the config.json/meta.json
# tree, plus the kbc-only type folders (app/, processor/, _shared/) which also
# still hold description.md + code bodies kbagent never reads either -- these
# are NOT empty dirs, "find -empty" is a no-op against them (confirmed live);
# remove the whole subtree. See references/migration-runbook.md.
find L0 -name config.json -o -name meta.json | xargs git rm -q --ignore-unmatch
git rm -rq --ignore-unmatch L0/*/app L0/*/processor L0/*/_shared
git add -A
```
Verify by **behavior**, not by reading the reformat diff: a follow-up
`sync status --directory L0` **must show 0 added, 0 modified, 0 deleted** (not just
"a small handful" — plain init reaches a genuinely empty status; if it doesn't,
something is wrong and you should stop, not push through it), and
`sync diff --project __env__ -d L0` / `sync push --dry-run` must also be clean.
Only then repeat for the remaining projects and the production lane.

**This is a normal commit on a normal branch — never a git-history rewrite.**
It is tempting, once you see how large the reformat diff is, to reach for
`git filter-repo` / an orphan-branch reset / a force-pushed squash of `main` to
make the history "clean." **Don't. This is not something you can deliver to a
real customer:** it invalidates every collaborator's existing clone and open PR,
destroys `git blame`/audit trail across the whole repo (a compliance problem for
regulated customers, not just an inconvenience), and requires a coordinated
force-push that most orgs' branch-protection rules block outright on `main`
anyway. The migration is disruptive enough as an ordinary large commit — do not
compound it with a history rewrite. Treat the "diff is huge, don't review it
line by line, verify by behavior instead" guidance above as the actual answer to
that discomfort, not a rewritten history.

### Step 4 — Set up GitHub secrets, variables, environments
The engine prints exact `gh` commands. The model: **one Storage API token secret
per project** (`KBC_TOKEN_<ALIAS>`), and two **Environments** (`prod`, `dev`) so
prod pushes require approval. See [references/secrets-setup.md](references/secrets-setup.md)
for the full mapping from the old `secrets.KBC_SAPI_TOKEN_*` / `vars.KBC_*` scheme.

### Step 5 — Confirm scope ("subset of a project") and branching
- **Subset:** if a project should only sync part of its config tree, set
  `ignoredComponents` (and/or `allowedBranches`) in that project's
  `.keboola/manifest.json` — `kbagent sync` honors both, exactly like `kbc`.
- **Branching:** the old model used a fixed branch id per env. The new model maps
  git branch → Keboola dev branch via `.keboola/branch-mapping.json` +
  `kbagent sync branch-link`. For PR-based promotion this is usually *better*:
  a PR branch links to a Keboola dev branch, `main` pushes to production. Add
  `--git-branching` to annotate, and walk the user through `branch-link` if they
  want per-PR isolated dev branches. If they want to keep the simple single-branch
  (production) model, leave branch-mapping at the default (null = production).

### Step 6 — Validate before merging
- Open the migration PR; the `kbagent-validate` workflow runs `sync diff` — confirm
  the diff is empty (no unintended drift) against each project.
- Manually run `kbagent-pull` once and confirm the committed state matches what
  `kbc pull` produced (the layout is identical; `git diff` should be tiny — mostly
  YAML vs JSON config-body formatting differences if any).
- Do a `kbagent-push` dry-run (the validate workflow already does this) and read
  the planned changes before the first real gated push.

## Guardrails (state these to the user)
- **Never** add `--allow-plaintext-on-encrypt-failure` to CI push — it silently
  uploads `#`-secrets in cleartext if the Encryption API is down. The generated
  push is fail-closed by design.
- `sync push --force` deletes remote configs removed locally. It is wired to
  the `allow_delete` workflow input (default off). Treat it like the old `--force`.
- Tokens live **only** in GitHub secrets and are injected as env vars per step; the
  generated workflows never write a `config.json` to disk.
- **Never run `--all-projects` in a directory that also holds a flat single-project
  tree.** `--all-projects` (`sync pull --all-projects` / `sync push --all-projects`)
  is hard-coded to a `<directory>/<alias>/` layout for *every* registered project
  alias (`_sync_bulk.py`) — it is not "operate on whatever is in this directory."
  Confirmed live: registering a persistent alias with `kbagent project add --project
  153 ...` (needed for ad-hoc `config update`/`config detail` maintenance work
  outside the CI flow) and then running `sync pull --all-projects` in a directory
  that already had a flat manifest at `./.keboola/manifest.json` silently
  **auto-created a second, separate tree at `./153/`** — `pull_all` auto-inits any
  registered alias with no manifest yet at its expected subpath, it does not detect
  or reuse an existing flat-layout manifest for the same project. `push_all` is
  slightly safer (skips instead of auto-creating) but still expects the same
  subfolder convention. This is why the generated CI workflows never use
  `--all-projects` — every step passes `--project __env__ --directory
  '{directory}'` explicitly (see `migrate_cicd.py`'s `_project_step`). Carry the
  same discipline into any manual/maintenance commands you run outside CI: always
  `--project ALIAS --directory DIR` explicit, never `--all-projects`, in a
  migration repo. If a customer (or you, helping them) already hit this, the
  extra `<alias>/` directory can simply be deleted — it holds a fresh, unrelated
  pull, not anything derived from their real tree.

## Reference material
- [references/migration-runbook.md](references/migration-runbook.md) — **the ordered PR sequence / cutover plan** (pre-flight → conversion PR → start-over). Use this when the user asks "which PRs, what order, how do I cut over."
- [references/branching-model.md](references/branching-model.md) — **how to choose** single-branch (Model A) vs git-branching (Model B), with a decision table.
- [references/command-mapping.md](references/command-mapping.md) — kbc ↔ kbagent commands, flags, env vars.
- [references/secrets-setup.md](references/secrets-setup.md) — secrets/vars/environments migration table + `gh` setup.
- `scripts/migrate_cicd.py` — the analyzer + generator (stdlib only).
