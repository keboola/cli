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
>   local deletion. **A `sync push --allow-delete` here would delete all 136 remote
>   configs.** Adopt-existing adopts only the manifest, NOT the configs.
> - The correct path — adopt → `sync pull --force` (writes `_config.yml`) → `git rm`
>   the orphaned `config.json`/`meta.json` — converged the diff from 136 deletes to
>   ~2 (plus ~9 remote-only scheduler/variables configs that pull/diff treat
>   inconsistently — verify these per project). Both file sets coexist after pull
>   until you delete the kbc files, so the `git rm` step is mandatory, not optional.
>
> **Operator rule:** never run `sync push` against an adopted-but-not-yet-pulled kbc
> tree. Always `sync pull --force` first, confirm `sync diff` is clean, THEN enable
> the push lane.

### What still carries over unchanged
The *orchestration shell* is CLI-agnostic: manual/scheduled pull that commits state,
PR validation, GitHub-Environment-gated push, branch→env mapping, per-project loops.
Only three mechanics change: **install** (`uv tool install` not a binary download),
**commands/flags** (`kbagent sync ...`, see
[references/command-mapping.md](references/command-mapping.md)), and **auth env vars**
(`KBAGENT_PROJECT_FROM_ENV=1` + `KBC_TOKEN` + `KBC_STORAGE_API_URL`).

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
the JSON tree to kbagent's YAML tree and remove the orphaned kbc files:

```bash
git checkout -b migrate/kbc-to-kbagent
# Per project (do ONE non-prod project first and verify):
KBAGENT_PROJECT_FROM_ENV=1 KBC_TOKEN=$L0_TOKEN KBC_STORAGE_API_URL=https://connection.keboola.com \
  kbagent sync init --adopt-existing --project __env__ --directory L0
KBAGENT_PROJECT_FROM_ENV=1 KBC_TOKEN=$L0_TOKEN KBC_STORAGE_API_URL=https://connection.keboola.com \
  kbagent sync pull --project __env__ --directory L0
# Remove orphaned kbc JSON files that kbagent no longer reads:
find L0 -name config.json -o -name meta.json | xargs git rm --cached --ignore-unmatch
git add -A
```
Verify by **behavior**, not by reading the reformat diff: a follow-up
`sync diff --project __env__ -d L0` must be clean and `sync push --dry-run` empty.
Only then repeat for the remaining projects and the production lane.

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
- `sync push --allow-delete` deletes remote configs removed locally. It is wired to
  the `allow_delete` workflow input (default off). Treat it like the old `--force`.
- Tokens live **only** in GitHub secrets and are injected as env vars per step; the
  generated workflows never write a `config.json` to disk.

## Reference material
- [references/migration-runbook.md](references/migration-runbook.md) — **the ordered PR sequence / cutover plan** (pre-flight → conversion PR → start-over). Use this when the user asks "which PRs, what order, how do I cut over."
- [references/branching-model.md](references/branching-model.md) — **how to choose** single-branch (Model A) vs git-branching (Model B), with a decision table.
- [references/command-mapping.md](references/command-mapping.md) — kbc ↔ kbagent commands, flags, env vars.
- [references/secrets-setup.md](references/secrets-setup.md) — secrets/vars/environments migration table + `gh` setup.
- `scripts/migrate_cicd.py` — the analyzer + generator (stdlib only).
