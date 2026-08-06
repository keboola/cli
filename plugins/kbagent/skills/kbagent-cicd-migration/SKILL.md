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
     (`constants.py`'s `CONFIG_FILENAME`, `sync/config_format.py`).
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

> **Current finding (project 153, kbagent v0.80.0, live-verified 2026-08-06) — use
> plain `sync init`, never `--adopt-existing`, for the conversion:**
> `--adopt-existing` carries kbc's row paths (`values/{name}` for
> `keboola.variables`, `codes/{name}` for `keboola.shared-code`) straight into the
> kbagent manifest without translating them to kbagent's own `rows/{name}`
> convention. kbagent's untracked-config scanner only recognizes a literal `rows/`
> path segment, so it reports those inherited rows as brand-new top-level configs
> — confirmed live: `sync status`/`sync diff` stayed at 9 added / 1-2 deleted even
> after the orphan-file cleanup, and pushing would `create_config` duplicate
> siblings instead of updating the rows they actually are. Plain `sync init`
> avoids this: it starts a genuinely empty manifest and lets `sync pull` populate
> every path through kbagent's own naming logic from scratch — confirmed to reach
> a fully clean `sync status` ("No local changes detected") and `sync diff` ("No
> differences found") against the same project, 119 tracked configs.
>
> One operational catch: kbc and kbagent write their manifest to the identical
> path, `.keboola/manifest.json`, so plain `sync init` refuses to run
> ("Manifest already exists...") until that one file — not the
> `config.json`/`meta.json` config tree next to it — is deleted first. Folded
> into Step 3b below.
>
> Never run `sync push` (without `--dry-run`) against an adopted-but-not-yet-
> pulled kbc tree on any kbagent version — always `sync pull` and confirm a clean
> `sync diff` first.

### What still carries over unchanged
The *orchestration shell* is CLI-agnostic: manual/scheduled pull that commits state,
PR validation, GitHub-Environment-gated push, branch→env mapping, per-project loops.
Only three mechanics change: **install** (`uv tool install` not a binary download),
**commands/flags** (`kbagent sync ...`, see
[references/command-mapping.md](references/command-mapping.md)), and **auth env vars**
(`KBAGENT_PROJECT_FROM_ENV=1` + `KBC_TOKEN` + `KBC_STORAGE_API_URL`).

**The per-project directory layout is also untouched.** Whatever top-level folder
name each project already uses in the repo — a numeric project id (`9086/`), a
promotion label (`L0/`, `L1/`), or a flat single-project repo (no per-project
folder at all) — kbagent keeps it exactly as-is: `migrate_cicd.py` discovers every
project by walking for `.keboola/manifest.json` and reuses that folder's existing
path verbatim in every generated step (`--directory '{p.directory}'`); it never
renames, moves, or re-derives the folder from the alias. Inside that folder the
branch subdirectory (`main/`, etc.) and the `storage/`-adjacent component-type
folders are unchanged too — only the file format one level below (`config.json`
→ `_config.yml`) changes. **Do not** use `kbagent sync --all-projects` to "adopt"
this layout — it enforces its own `<directory>/<alias>/` convention (see the
guardrail below) and would rename/duplicate the tree; the generated CI never
uses it for exactly this reason.

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
- **Auth for each project being converted — two different answers for CI vs. the
  local conversion step.** The generated CI workflows (Step 3/4) always need a
  static per-project Storage API token secret (`KBC_TOKEN_<ALIAS>`,
  `KBAGENT_PROJECT_FROM_ENV=1`) — `kbagent auth login` is browser-based and
  cannot run unattended on a GitHub Actions runner, so there is no login-based
  alternative for CI. For the **local, interactive** one-time conversion in Step
  3b, though, a raw token is not the only option: if the operator already has
  (or runs) `kbagent auth login` + `auth register-projects`, they get a
  registered alias with a session token and can run `kbagent sync init/pull
  --project <alias> --directory <DIR>` directly — no `KBAGENT_PROJECT_FROM_ENV`/
  `KBC_TOKEN` env-injection needed, since that dance exists specifically to
  bridge CI's no-persisted-config environment. Either way, never ask the
  customer to paste a token into chat; read it via the clipboard-secret pattern
  or point at wherever they already store it and reference it by path/alias.

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
chaining the whole sequence unattended — that's how stale/incorrect claims go
unnoticed in a skill like this one, and how you end up cleaning up things you
didn't realize you'd need to.

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

**Recommended mechanic: plain `sync init` — do NOT use `--adopt-existing`** (see
the reality-check note above for why: inherited row paths make `sync status`
permanently dirty and risk `sync push` creating duplicate configs). The exact
per-project command sequence — including the required `rm .keboola/manifest.json`
prep step, the acceptance criteria, and why it's an ordinary commit on an ordinary
branch (never a git-history rewrite, even though the reformat diff is huge) — is
maintained once in
[references/migration-runbook.md](references/migration-runbook.md) ("PR 1 —
Conversion"); follow it verbatim rather than re-deriving the steps here.

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
  a PR branch links to a Keboola dev branch, `main` pushes to production. This
  is a per-project runtime choice, not something the generator needs to know
  about — run `kbagent sync init --git-branching` and walk the user through
  `branch-link` if they want per-PR isolated dev branches (see
  [references/branching-model.md](references/branching-model.md)). If they
  want to keep the simple single-branch (production) model, leave
  branch-mapping at the default (null = production) and skip this entirely.

### Step 6 — Validate before merging
- Open the migration PR; the `kbagent-validate` workflow runs `sync diff` — confirm
  the diff is empty (no unintended drift) against each project.
- Manually run `kbagent-pull` once **against the already-converted tree** and
  confirm it's a no-op: `git diff` should be empty (or near-empty). This checks
  that nothing drifted between the conversion commit and now — it is not the
  same comparison as the one-time JSON→YAML conversion diff in Step 3b, which
  is expected to touch every config file.
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
