# GitHub secrets / environment setup

Each pipeline needs **two** Storage API token secrets -- one for the source
project, one for the destination project -- plus one shared `prod`
GitHub Environment used for push approval gating across every pipeline.

## Per pipeline

| Secret | Used by | Project |
|---|---|---|
| `KBC_TOKEN_<NAME>_SOURCE` | `kbagent-promote-pull.yml` | Source (e.g. dev) |
| `KBC_TOKEN_<NAME>_DEST` | `kbagent-promote-validate.yml`, `kbagent-promote-push.yml` | Destination (e.g. prod) |

`<NAME>` is the pipeline's `name`, uppercased and sanitized to
`[A-Za-z0-9_]` (the generator's `Pipeline.label` property) -- it always
matches what `generate_promotion_pipeline.py` prints in its secrets report,
so copy-paste from there rather than re-deriving it by hand.

## Setup with `gh`

```bash
REPO=<owner>/<repo>

# Per pipeline (repeat for each):
gh secret set KBC_TOKEN_SALESFORCE_SOURCE --repo "$REPO"   # paste the dev project's token
gh secret set KBC_TOKEN_SALESFORCE_DEST --repo "$REPO"     # paste the prod project's token

# Shared push-approval environment (once per repo):
gh api -X PUT "repos/$REPO/environments/prod"
```

Then in the GitHub UI (or via the environments API):
1. Add **required reviewers** to the `prod` environment. This is what
   actually makes `kbagent-promote-push.yml` block on approval -- the
   `environment: prod` line in the generated workflow is a no-op without
   reviewers configured.
2. Optionally restrict the `prod` environment to the `main` branch only.
3. Scope the `*_DEST` secrets to the `prod` environment if your org's policy
   requires environment-scoped secrets (recommended for genuinely
   production-facing tokens).

## Why no token in config.json

kbagent can read a committed `.kbagent/config.json` with registered project
aliases, but that file stores tokens on disk -- unsafe to commit. Every
generated workflow step instead sets `KBAGENT_PROJECT_FROM_ENV=1` +
`KBC_TOKEN` + `KBC_STORAGE_API_URL` for that one step only, so the token
exists solely as a masked GitHub secret in the runner's environment, never
written to a file.

## Security guardrails

- Do **not** commit `.kbagent/config.json` with tokens.
- Do **not** pass `--allow-plaintext-on-encrypt-failure` in CI.
- Prefer environment-scoped `*_DEST` secrets and required reviewers for any
  pipeline whose destination is a genuinely production project.
- Pin `peter-evans/create-pull-request` to a full commit SHA, not just a
  version tag, for a prod-adjacent pipeline (third-party action supply-chain
  hygiene).
