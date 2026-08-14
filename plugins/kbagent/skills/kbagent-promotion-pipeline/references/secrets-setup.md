# GitHub secrets / environment setup

Each pipeline needs **two** Storage API token secrets -- one for the source
project, one for the destination project -- plus one repo-wide PAT for
opening promotion PRs, plus a single repo-wide `prod` GitHub Environment
shared by every pipeline, used for push approval gating (the environment is
shared, but each pipeline's push job still gets its own separate approval
prompt -- see below).

## Per pipeline

| Secret | Used by | Project |
|---|---|---|
| `KBC_TOKEN_<NAME>_SOURCE` | `kbagent-promote-pull.yml` | Source (e.g. dev) |
| `KBC_TOKEN_<NAME>_DEST` | `kbagent-promote-validate.yml`, `kbagent-promote-push.yml` | Destination (e.g. prod) |

`<NAME>` is the pipeline's `name`, uppercased and sanitized to
`[A-Za-z0-9_]` (the generator's `Pipeline.label` property) -- it always
matches what `generate_promotion_pipeline.py` prints in its secrets report,
so copy-paste from there rather than re-deriving it by hand. The generator
also rejects two pipeline names that sanitize to the same label, so a
secret name never silently collides between two different pipelines.

## Repo-wide

| Secret | Used by | Purpose |
|---|---|---|
| `PROMOTION_PR_TOKEN` | `kbagent-promote-pull.yml` | Opens the promotion PR as a real identity, not the default token |

**Why this PAT is required, not optional:** `peter-evans/create-pull-request`
opens the PR using whatever token it's given. If that's the workflow's
default `GITHUB_TOKEN`, GitHub deliberately suppresses `pull_request`-triggered
workflow runs for PRs opened that way -- `kbagent-promote-validate.yml` would
never fire on the PR, and reviewers would approve blind with no destination-side
diff, silently defeating the whole point of this pipeline. Use a fine-grained
PAT (Contents: write, Pull requests: write, scoped to this repo) or a GitHub
App installation token instead, and set it as `PROMOTION_PR_TOKEN`.

## Setup with `gh`

```bash
REPO=<owner>/<repo>

# Once per repo:
gh secret set PROMOTION_PR_TOKEN --repo "$REPO"   # paste the PAT described above

# Per pipeline (repeat for each):
gh secret set KBC_TOKEN_SALESFORCE_SOURCE --repo "$REPO"   # paste the dev project's token
gh secret set KBC_TOKEN_SALESFORCE_DEST --repo "$REPO"     # paste the prod project's token

# Push-approval environment (once per repo -- every pipeline's push job
# references it, but each job run still gets its own separate approval,
# see "One environment, per-pipeline approval" below):
gh api -X PUT "repos/$REPO/environments/prod"
```

Then in the GitHub UI (or via the environments API):
1. Add **required reviewers** to the `prod` environment. This is what
   actually makes `kbagent-promote-push.yml` block on approval -- the
   `environment: prod` line in the generated workflow is a no-op without
   reviewers configured.
2. Add a **required status check** for the `validate` job on `main`'s branch
   protection rules. Without this, a PR can be merged even if `validate`
   never ran (e.g. the PAT above was missing) or actively failed --
   "PR-gated" is only true if merging is actually blocked on it.
3. Optionally restrict the `prod` environment to the `main` branch only.
4. Scope the `*_DEST` secrets to the `prod` environment if your org's policy
   requires environment-scoped secrets (recommended for genuinely
   production-facing tokens).

## One environment, per-pipeline approval

`kbagent-promote-push.yml` generates **one job per pipeline**, each with
`environment: prod`. GitHub's required-reviewer gate is enforced per job
*run*, not per environment name -- even though every pipeline's job
references the same `prod` environment, each one gets its own separate
"Review deployments" prompt. Approving one pipeline's push does not approve
any other pipeline in the same workflow run, and one pipeline's job failing
does not block or skip the others (they have no `needs:` dependency on each
other).

## Why no token in config.json

kbagent can read a committed `.kbagent/config.json` with registered project
aliases, but that file stores tokens on disk -- unsafe to commit. Every
generated workflow step instead sets `KBAGENT_PROJECT_FROM_ENV=1` +
`KBC_TOKEN` + `KBC_STORAGE_API_URL` for that one step only, so the token
exists solely as a masked GitHub secret in the runner's environment, never
written to a file.

## `#`-secrets do not promote across projects

`sync pull`/`sync push` move plain configuration between projects, but a
config's `#`-prefixed (encrypted) values are stored on disk only as
project-scoped ciphertext (`KBC::ProjectSecure::...`) -- ciphertext encrypted
for the *source* project cannot be decrypted by the *destination* project.
A value that round-trips through this pipeline either fails `sync push`'s
fail-closed encryption check, or -- if something upstream forced plaintext
through -- lands in the destination as an inert string that looks like a
secret but isn't one, a silent outage waiting to happen.

**Never promote `#`-secret values through this pipeline.** Set each
destination project's secrets independently and directly on that project
(`kbagent config variables-set`, `config update`, or `data-app secrets-set`
against the *destination* token) -- only the config's *structure* (which
keys exist) should ever come from a promotion PR, never `#`-secret contents.

## Security guardrails

- Do **not** commit `.kbagent/config.json` with tokens.
- Do **not** pass `--allow-plaintext-on-encrypt-failure` in CI.
- Never promote `#`-secret *values* through `sync pull`/`sync push` across
  projects -- see above.
- Prefer environment-scoped `*_DEST` secrets and required reviewers for any
  pipeline whose destination is a genuinely production project.
- Pin `peter-evans/create-pull-request` to a full commit SHA, not just a
  version tag, for a prod-adjacent pipeline (third-party action supply-chain
  hygiene).
