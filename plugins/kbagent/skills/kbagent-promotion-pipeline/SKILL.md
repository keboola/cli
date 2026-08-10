---
name: kbagent-promotion-pipeline
description: >
  Use when setting up a from-scratch GitHub Actions pipeline that promotes
  Keboola configurations from a SOURCE project (e.g. dev) to a DESTINATION
  project (e.g. prod) using kbagent sync -- one GitHub repo covering the
  whole org, main branch as the reviewable source of truth. Covers: PR-based
  promotion (pull from source opens a PR, merging pushes to destination),
  cross-project diff before merge, multi-pipeline repos (several independent
  source/destination pairs in one repo), and GitHub secrets/environment
  setup. Triggers: promote config between projects, dev to prod pipeline,
  source project destination project, propagate changes between Keboola
  projects, kbagent promotion workflow, cross-project sync GitHub Actions,
  set up project promotion CI/CD.
---

# kbagent Source -> Destination Promotion Pipeline

Generates a **from-scratch** GitHub Actions setup (not a migration -- see
[kbagent-cicd-migration](../kbagent-cicd-migration/SKILL.md) for porting an
existing `kbc` repo) that promotes Keboola configuration changes from a named
**source project** to a named **destination project**, with a human-reviewed
PR gate in between.

## The mechanic

kbagent's `sync` targets one registered project alias per invocation
(`--project ALIAS`) -- it has no "this git branch is bound to that project"
magic the way some kbc-era setups do. This skill builds the promotion loop
directly out of that primitive, using one shared directory per pipeline and
two Storage API tokens (source, destination):

1. **Pull** (`kbagent-promote-pull.yml`, manual + optional schedule) runs
   `sync pull --project __env__ --directory <scratch-dir> --force` against
   the **source** project's token into a throwaway scratch directory, merges
   its *content* (everything except `.keboola/manifest.json`) into the
   tracked `<dir>`, then opens (or updates) **one PR** against `main` with
   the combined diff via
   [`peter-evans/create-pull-request`](https://github.com/peter-evans/create-pull-request)
   (using a PAT, not the default token -- see
   [references/secrets-setup.md](references/secrets-setup.md)).
2. **Validate** (`kbagent-promote-validate.yml`, on the PR) runs
   `sync push --dry-run --project __env__ --directory <dir>` against the
   **destination** project's token, for every pipeline touched by the PR --
   this is the cross-project diff: *if this PR merges, here is exactly what
   changes in the destination project.* Read this before approving.
3. **Push** (`kbagent-promote-push.yml`, on push to `main`) runs, in a
   **separate job per pipeline**, `sync push --project __env__ --directory
   <dir>` against the **destination** project's token, each job gated by the
   `prod` GitHub Environment (add required reviewers there -- every job run
   gets its own separate approval, so approving one pipeline never approves
   another).

`main` therefore always represents "the last thing approved and pushed to
every destination project" -- the reviewable source of truth the whole repo
is built around. A promotion is: pull opens a PR -> validate shows the
destination-side diff -> a human approves and merges -> push ships it.

**Why pull goes through a scratch directory instead of pulling `<dir>`
directly:** `<dir>`'s `.keboola/manifest.json` is bootstrapped once from the
*destination* project (Step 4) and must stay bound to the destination's
config IDs forever after -- that ID mapping is what lets `sync push`
recognize "this is config X I already created" instead of creating a
duplicate every time. Pulling straight into `<dir>` with the *source*
token would overwrite that manifest with the source project's IDs, and the
next push would then fail to match anything in the destination and create
duplicates on every single promotion cycle. Pulling into a scratch
directory and merging only the content keeps the destination's ID mapping
stable while still picking up the source's changes.

Every step uses `KBAGENT_PROJECT_FROM_ENV=1` + the reserved `--project __env__`
alias (kbagent's headless/CI auth model) -- no token is ever written to
`config.json` or committed to the repo. See
[references/env-injection.md](references/env-injection.md) if you need the
background on why this exists.

## One repo, multiple independent pipelines

A single repo can host several unrelated promotion pipelines (e.g. one per
data source, or one per business unit) -- each is a
`{name, directory, source_stack_url, dest_stack_url}` tuple, all generated
into the same three workflow files as extra per-pipeline steps. Use `--config
pipelines.json` (a JSON list of these tuples) for more than one; the
single-pipeline CLI flags (`--name`/`--directory`/`--source-stack-url`/
`--dest-stack-url`) are a shortcut for exactly one.

## How to run this -- ask the customer, don't auto-pilot

Same discipline as every other skill that touches a customer's live
Keboola projects and their CI/CD: **stop and ask** before you:
- Pick the version pin (Step 2) -- prod vs. scratch lane changes the answer.
- Run `--write` (Step 3) -- show the dry-run inventory first.
- Perform the one-time bootstrap (Step 4) against a real destination
  project -- confirm which project is genuinely production before seeding
  `main` from it.
- Set up secrets/environments (Step 5) -- these are the customer's
  credentials, not yours to generate blindly.

## Workflow

### Step 1 -- Gather the pipeline definition(s)
For each pipeline: a name, the directory to sync, and the source + destination
projects' stack URLs (usually the same stack, different project ids -- the
project id itself comes from the token, not a CLI flag). Ask for a config
file up front if there's more than one pipeline; it's much easier to review
as a single JSON list than to re-run the generator repeatedly.

### Step 2 -- Pick a version pin (decide before generating)
Same guidance as the migration skill: `--version X.Y.Z` (PyPI) pinned for a
prod lane, unpinned only for a scratch/experiment repo.

### Step 3 -- Generate the workflows (dry-run first)
```bash
# Inspect what would be generated:
python <skill_dir>/scripts/generate_promotion_pipeline.py /path/to/repo \
    --name SALESFORCE --directory salesforce \
    --source-stack-url connection.keboola.com \
    --dest-stack-url connection.keboola.com

# Then, once reviewed, write the files:
python <skill_dir>/scripts/generate_promotion_pipeline.py /path/to/repo --write \
    --config pipelines.json --version X.Y.Z --schedule "0 6 * * 1"
```
Produces `.github/workflows/kbagent-promote-{pull,validate,push}.yml` and
prints the exact `gh secret set` / `gh api` commands for Step 5.

### Step 4 -- Bootstrap `main` from the destination project (one-time, per pipeline)
`main` should start out representing what's *already live* in the
destination project, not an empty tree -- otherwise the first promotion PR
would show every single config as "new," which is both wrong and a scary
first review. Locally, with the destination project's token:
```bash
export KBAGENT_PROJECT_FROM_ENV=1 KBC_TOKEN=<dest-token> KBC_STORAGE_API_URL=<dest-stack-url>
kbagent sync pull --project __env__ --directory <dir>
git add <dir> && git commit -m "Bootstrap <dir> from destination project" && git push
```
(`sync pull` auto-initializes when no manifest exists yet -- no separate
`sync init` needed.) Do this directly on `main`, not through a PR -- there
is nothing to review yet, it's just establishing the starting baseline. This
step is what binds `<dir>`'s manifest to the destination project's config
IDs -- see "Why pull goes through a scratch directory" above for why that
binding must never be overwritten by a later source pull.

### Step 5 -- Set up GitHub secrets and the `prod` environment
Two Storage API token secrets per pipeline (`KBC_TOKEN_<NAME>_SOURCE`,
`KBC_TOKEN_<NAME>_DEST`), one repo-wide `PROMOTION_PR_TOKEN` (a PAT --
the default `GITHUB_TOKEN` cannot open a PR that triggers `validate`), and
the `prod` GitHub Environment with required reviewers -- the generator
prints the exact `gh` commands. See
[references/secrets-setup.md](references/secrets-setup.md).

### Step 6 -- Run a promotion end-to-end
1. Trigger `kbagent-promote-pull.yml` (`workflow_dispatch`, or wait for the
   schedule) -- it opens/updates the `promote/update` PR against `main`.
2. Read the `kbagent-promote-validate.yml` check's dry-run output on that
   PR -- confirm it matches what you expect to land in each destination
   project.
3. Merge the PR. `kbagent-promote-push.yml` fires; each pipeline is its own
   job, each waiting for its own `prod` environment approval, then pushing
   to that pipeline's destination project.

## Guardrails (state these to the user)
- **Never** add `--allow-plaintext-on-encrypt-failure` to the push workflow --
  it silently uploads `#`-secrets in cleartext if the Encryption API is down.
- **Never promote `#`-secret values through `sync pull`/`sync push` across
  projects** -- source-encrypted ciphertext cannot be decrypted by the
  destination project. Set destination secrets independently, directly on
  the destination project. See
  [references/secrets-setup.md](references/secrets-setup.md#-secrets-do-not-promote-across-projects).
- The `prod` environment's required-reviewer gate applies to `push`-triggered
  jobs too, not just `workflow_dispatch` -- confirm the reviewers are actually
  configured, since a repo without them makes the "gate" a no-op. Each
  pipeline gets its own job/approval (see secrets-setup.md), but every job
  still needs those reviewers configured to mean anything.
- Add a **required status check** for `validate` on `main`'s branch
  protection -- without it, a PR can merge even if validate never ran or
  failed, silently degrading "PR-gated" to "PR-gated only if someone
  happened to wait for the check."
- One PR covers every pipeline pulled in that run (`branch: promote/update`).
  If pipelines are unrelated and reviewed by different people, consider
  splitting them into separate repos or separate pull workflows instead of
  forcing one combined review. (Push is already split per-pipeline; only
  pull's PR is still combined.)
- `peter-evans/create-pull-request` is a third-party action -- pin it to a
  full commit SHA (not just `@v7`) for a security-sensitive prod pipeline,
  and mention this to the customer rather than silently leaving the tag pin.
- It needs a `PROMOTION_PR_TOKEN` PAT, not the default `GITHUB_TOKEN` --
  otherwise the `validate` check never triggers on the PR it opens. See
  [references/secrets-setup.md](references/secrets-setup.md).

## Reference material
- [references/secrets-setup.md](references/secrets-setup.md) -- GitHub secrets/environment setup with `gh` commands.
- [references/env-injection.md](references/env-injection.md) -- why `KBAGENT_PROJECT_FROM_ENV`/`__env__` exists and how it differs from a registered `project add`.
- `scripts/generate_promotion_pipeline.py` -- the generator (stdlib only).
