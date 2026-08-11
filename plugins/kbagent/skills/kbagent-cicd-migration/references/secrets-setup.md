# GitHub secrets / variables / environments setup

The legacy CLI-based-sync-demo split config across **repo secrets**, **repo
variables**, and **GitHub Environments**. The kbagent model is simpler: one token
secret per project, the stack URL baked from each manifest, environments only for
push approval.

## Migration table

| Legacy (kbc demo) | Type | kbagent (new) | Type | Notes |
|---|---|---|---|---|
| `secrets.KBC_SAPI_TOKEN_L0` | secret | `secrets.KBC_TOKEN_L0` | secret | One per project; injected as `KBC_TOKEN` |
| `secrets.KBC_SAPI_TOKEN_L1` | secret | `secrets.KBC_TOKEN_L1` | secret | |
| `vars.KBC_SAPI_HOST` | variable | *(baked from manifest `apiHost`)* | — | Override per project in the generated `env:` block if you use a non-default stack |
| `vars.KBC_PROJECT_ID_L0/L1` | variable | *(from `.keboola/manifest.json`)* | — | No longer a CI variable |
| `vars.KBC_BRANCH_ID_L0/L1` | variable | *(from `.keboola/branch-mapping.json`)* | — | Only if using git-branching mode |
| Environments `prod` / `dev` | environment | Environments `prod` / `dev` | environment | **Keep** — used for push approval gating |

## Setup with `gh`

```bash
REPO=<owner>/<repo>

# One Storage API token per project (use environment-scoped secrets for prod):
gh secret set KBC_TOKEN_L0 --repo "$REPO"        # paste project 9996 token
gh secret set KBC_TOKEN_L1 --repo "$REPO"        # paste project 9997 token

# Environments for approval gating:
gh api -X PUT "repos/$REPO/environments/dev"
gh api -X PUT "repos/$REPO/environments/prod"
```

Then in the GitHub UI (or via the environments API):
1. Scope `KBC_TOKEN_*` for production projects to the **prod** environment --
   but only for `kbagent-push.yml`'s `push` job, which is the only generated
   job that declares `environment: prod`. `kbagent-validate.yml`'s `validate`
   job runs on every `pull_request` with **no** `environment:` key (it's
   read-only: `sync diff` + `sync push --dry-run`, never a real write), so an
   environment-scoped secret is invisible to it and every PR-time diff/dry-run
   against a prod project would fail auth. If you scope a prod token to the
   `prod` environment, either (a) keep an unscoped copy of that same secret
   available to `validate` too, or (b) accept that `validate` simply won't run
   its dry-run check against prod-scoped projects until the push step (which
   still gets full approval gating). Do not scope the secret and expect
   `validate` to see it -- that combination silently breaks PR-time checks.
2. Add **required reviewers** to the `prod` environment so `kbagent push` to prod
   blocks on manual approval (this replaces the demo's environment gating).
3. Optionally restrict the `prod` environment to the `main` branch.

## Why no token in config.json
`kbagent` can read a committed `.kbagent/config.json` with multiple project
aliases, but that file stores tokens — unsafe to commit. In CI we instead set
`KBAGENT_PROJECT_FROM_ENV=1` + `KBC_TOKEN` + `KBC_STORAGE_API_URL` per step, so the
token exists only as a masked GitHub secret in the runner's env, never on disk.
This is the direct, safer analog of the demo's per-project `KBC_SAPI_TOKEN_*`
secret model.

## Security guardrails
- Do **not** commit `.kbagent/config.json` with tokens (the new CLI auto-writes a
  `.gitignore` for its config dir — `ConfigStore._ensure_gitignore`).
- Do **not** pass `--allow-plaintext-on-encrypt-failure` in CI.
- Prefer environment-scoped secrets + required reviewers for any lane that pushes
  to a production project.
