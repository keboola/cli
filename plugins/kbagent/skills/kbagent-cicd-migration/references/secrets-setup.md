# GitHub secrets / variables / environments setup

The legacy CLI-based-sync-demo split config across **repo secrets**, **repo
variables**, and **GitHub Environments**. The kbagent model is simpler: one token
secret per project, the stack URL baked from each manifest, environments only for
push approval.

## Which credential goes in `KBC_TOKEN_<ALIAS>`: a PAT, not a raw Storage token

**Recommended (kbagent v0.81.0+): a Personal Access Token minted via
`kbagent auth pat-create`**, not a Storage API token copy-pasted from the UI.
The one-time setup, once per project being migrated:

```bash
kbagent auth login --stack <stack-url>              # once per stack; opens a browser
kbagent auth pat-create --name "ci-<alias>" --project-id <project-id>
#   ^ prompts for your current TOTP code, prints the PAT exactly once
```

Store *that* PAT as `KBC_TOKEN_<ALIAS>` -- it is a drop-in for the env-injection
model the generated workflows already use (`KBAGENT_PROJECT_FROM_ENV=1` +
`KBC_TOKEN`): kbagent recognizes the `kbc_pat_...` prefix and sends it as
`Authorization: Bearer` automatically, so **nothing about the generated YAML
changes** -- only how the secret's value was obtained. `--project-id` scopes the
PAT to exactly that one project (least privilege for a one-secret-per-project
setup); omit it only if the CI pipeline is deliberately meant to reach every
project the signed-in user can access. Add `--read-only` for the `validate`
workflow's dry-run-only secret if you want to split read vs. write credentials
per project instead of reusing one PAT for both.

This is *better* than the demo's raw `KBC_SAPI_TOKEN_*` model, not just a port
of it: a PAT is scoped, has an expiry you control (`--ttl-days`), and is
revocable independently of the account's password (`kbagent auth pat-revoke`)
-- rotate a compromised CI secret without touching anything else the account
can do.

**Fallback: a raw Storage API token**, pasted from Project Settings > API
Tokens in the Keboola UI, if `auth pat-create` isn't available on your kbagent
version or your account can't complete the browser-login + TOTP step-up. This
still works exactly as before (`X-StorageApi-Token`), just without the
scoping/expiry/independent-revocation benefits above.

## Migration table

| Legacy (kbc demo) | Type | kbagent (new) | Type | Notes |
|---|---|---|---|---|
| `secrets.KBC_SAPI_TOKEN_L0` | secret | `secrets.KBC_TOKEN_L0` | secret | One per project; injected as `KBC_TOKEN`; value is a PAT (`kbagent auth pat-create`), not a copy-pasted Storage token |
| `secrets.KBC_SAPI_TOKEN_L1` | secret | `secrets.KBC_TOKEN_L1` | secret | |
| `vars.KBC_SAPI_HOST` | variable | *(baked from manifest `apiHost`)* | — | Override per project in the generated `env:` block if you use a non-default stack |
| `vars.KBC_PROJECT_ID_L0/L1` | variable | *(from `.keboola/manifest.json`)* | — | No longer a CI variable |
| `vars.KBC_BRANCH_ID_L0/L1` | variable | *(from `.keboola/branch-mapping.json`)* | — | Only if using git-branching mode |
| Environments `prod` / `dev` | environment | Environments `prod` / `dev` | environment | **Keep** — used for push approval gating |

## Setup with `gh`

```bash
REPO=<owner>/<repo>

# Mint a scoped PAT per project (see above), then store each as a secret
# (use environment-scoped secrets for prod):
kbagent auth pat-create --name "ci-L0" --project-id 9996   # copy the printed token
gh secret set KBC_TOKEN_L0 --repo "$REPO"                  # paste that PAT, not the account's Storage token
kbagent auth pat-create --name "ci-L1" --project-id 9997
gh secret set KBC_TOKEN_L1 --repo "$REPO"

# Environments for approval gating:
gh api -X PUT "repos/$REPO/environments/dev"
gh api -X PUT "repos/$REPO/environments/prod"
```

Then in the GitHub UI (or via the environments API):
1. Scope `KBC_TOKEN_*` for production projects to the **prod** environment.
2. Add **required reviewers** to the `prod` environment so `kbagent push` to prod
   blocks on manual approval (this replaces the demo's environment gating).
3. Optionally restrict the `prod` environment to the `main` branch.

## Why no token in config.json
`kbagent` can read a committed `.kbagent/config.json` with multiple project
aliases, but that file stores tokens — unsafe to commit. In CI we instead set
`KBAGENT_PROJECT_FROM_ENV=1` + `KBC_TOKEN` + `KBC_STORAGE_API_URL` per step, so the
token exists only as a masked GitHub secret in the runner's env, never on disk.
This is the direct, safer analog of the demo's per-project `KBC_SAPI_TOKEN_*`
secret model -- now backed by a PAT instead of a raw copy-pasted token.

## Security guardrails
- Do **not** commit `.kbagent/config.json` with tokens (the new CLI auto-writes a
  `.gitignore` for its config dir — `ConfigStore._ensure_gitignore`).
- Do **not** pass `--allow-plaintext-on-encrypt-failure` in CI.
- Prefer environment-scoped secrets + required reviewers for any lane that pushes
  to a production project.
- If a `KBC_TOKEN_*` secret leaks, revoke just that PAT (`kbagent auth pat-revoke
  PAT_ID`) and mint a replacement -- this is the whole reason to prefer a PAT
  over a raw Storage token: revocation doesn't touch anything else the account
  can do.
