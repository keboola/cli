# Data App Workflow -- Streamlit / Flask / Node Lifecycle

Data apps in Keboola are deployed from a git repo into a managed container
that auto-suspends after idle. Two API surfaces own them:

| Layer | What it owns |
|---|---|
| **Storage API** (`keboola.data-apps` config) | git block, encrypted secrets, slug, runtime size, name, description |
| **Data Science API** (`/apps`) | deployment record: state, desiredState, url, configVersion |

`kbagent data-app` orchestrates both, plus the project's Encryption API for
git PATs. The CLI encapsulates the documented footguns so callers cannot
hit them; see "Gotchas encoded" below.

## Storage access: `runtime.workspace.enabled` (read this first)

Any data app that reads Storage needs `runtime.workspace.enabled: true` in
its `keboola.data-apps` configuration. That flag -- and nothing else -- is
what makes the platform provision the app's ephemeral workspace and inject:

| Env var | Purpose |
|---|---|
| `WORKSPACE_ID` | the provisioned workspace the app queries through |
| `QUERY_SERVICE_URL` | Query Service endpoint for reads/writes |
| `KBC_WORKSPACE_MANIFEST_PATH` | path to the workspace manifest in the container |

**Since 0.87.0 `data-app create` sets it by default.** Pass
`--no-workspace` only for an app that never touches Storage.

```jsonc
// what --workspace (default) writes -- a SIBLING of backend
"runtime": {
  "backend":   { "size": "small" },
  "workspace": { "enabled": true }
}
```

**Why this has its own section:** before 0.87.0 kbagent never wrote the flag
and offered no option to. An app missing it **deploys successfully** --
`state=running`, `desiredState=running`, health probe green, `setup_sh`
completed -- and cannot read a row. **The platform emits no diagnostic**, so
the only reliable check is the config itself:

```bash
kbagent --json config detail --project P --component-id keboola.data-apps \
  --config-id <cfg> | jq '.data.configuration.runtime'
```

An app that checks its own env may log something like `Missing env vars:
WORKSPACE_ID (or KBC_WORKSPACE_MANIFEST_PATH)` -- that phrasing is the ai-kit
DuckDB-cache template's, and an app built from anything else may log something
different or nothing at all. An empty `data-app logs` grep therefore does not
rule this out.

An app that does *not* catch the failure crash-loops behind the health probe
instead, which is at least visible. Silently serving zeros is the dangerous
outcome, and it is the one you get from the DuckDB-cached read-only dashboard
pattern -- the `dataapp-developer` skill's default.

**Retrofitting an app created before 0.87.0** (or by any other path):

```bash
kbagent config update --project P --component-id keboola.data-apps \
  --config-id <cfg> --merge --set 'runtime.workspace.enabled=true'
kbagent data-app deploy --project P --app-id <ID> --wait   # pins the LATEST version
```

The redeploy is required -- a config change alone does not reach the running
container.

> Not gated on any project feature. The `data-apps-storage-workspace` flag is
> being enabled everywhere and then removed, so the config option is the sole
> control. kbagent deliberately does **not** implement MCP's legacy
> `parameters.dataApp.secrets.WORKSPACE_ID` fallback.

## Quick recipes

### Public-repo Streamlit app from scratch (no auth gate)

```bash
kbagent --json data-app create \
  --project prod \
  --name "Hello Streamlit" \
  --slug hello-streamlit \
  --git-repo https://github.com/streamlit/streamlit-example \
  --git-public \
  --auth public \
  --wait
```

Three calls under the hood: `POST /apps` (mint id + configId) → `PUT
Storage config` (full body with git block + parameters.id back-pointer) →
`PATCH /apps {desiredState=running, configVersion, restartIfRunning=true}`.
The `--wait` flag polls until `state == running` (writeup §8 pitfall #1
encoded: a transient `state == stopped` during initial deploy is *not*
treated as terminal).

### Private-repo simpleAuth app

```bash
export GITHUB_PAT_DATAAPP=ghp_xxxxxxxxxxxxxxxxxxxx

kbagent --json data-app create \
  --project prod \
  --name "Internal Dashboard" \
  --slug internal-dashboard \
  --git-repo https://github.com/myorg/dashboard \
  --git-username myuser \
  --git-pat-env GITHUB_PAT_DATAAPP \
  --auth password \
  --wait
```

`--git-pat-env` is the recommended PAT input mode -- the plaintext token
never appears in argv. The service encrypts it under THIS project's KMS via
the Encryption API before writing it to Storage. `--auth password` (the
default) auto-mints a 20-character hex simpleAuth password; retrieve it
with:

```bash
kbagent data-app password --project prod --app-id <ID>
# Manage token: interactive prompt by default (since v0.29.0); for CI add
# --allow-env-manage-token alongside KBC_MANAGE_API_TOKEN. Storage token
# is read from .kbagent/config.json as usual.
```

The simpleAuth password CANNOT be rotated (writeup §11.2). To change it,
delete and recreate the app.

### Roll out a new code version (no Storage edit)

```bash
git push origin main             # the app's configured branch
kbagent data-app deploy --project prod --app-id 12345678 --wait
```

`deploy` reads the latest Storage config version and PATCHes the §9 trio.
The runner clones the configured git ref at container start, so a fresh
`git push` is picked up by the next deploy without any Storage edit.

### Roll out a new config (e.g. change size or auto-suspend)

```bash
kbagent --json config update \
  --project prod \
  --component-id keboola.data-apps \
  --config-id 01abcdefghijklmnopqrstuvwxyz \
  --set 'runtime.backend.size="medium"' --merge

kbagent data-app deploy --project prod --app-id 12345678 --wait
```

`config update` bumps the Storage version; `data-app deploy` reads the
latest and pins the deployment to it. Without the deploy step, the running
container keeps the OLD config (the deploy-record `configVersion` does not
auto-advance when Storage advances -- writeup §9 mental model).

### Wake an auto-suspended app

```bash
kbagent data-app start --project prod --app-id 12345678 --wait
```

Distinct from `deploy`: `start` does NOT bump the deployed configVersion.
It is the cheap restart for an app the platform parked due to
`autoSuspendAfterSeconds` of inactivity (writeup §8 pitfall #2). Hitting
the app's URL also auto-wakes it (cold-boot ~30-60s).

### Rollback to an older config version

```bash
kbagent data-app deploy --project prod --app-id 12345678 \
  --config-version 5 --wait
```

`--config-version` pins the deployment to a specific Storage version
(rollback). Subsequent deploys without the flag will jump back to the
latest.

### Pre-flight repo validation (since v0.29.0)

```bash
kbagent data-app validate-repo \
  --git-repo https://github.com/myorg/dashboard \
  --git-branch main \
  --git-pat-env GITHUB_PAT_DATAAPP \
  --type python-js
```

Walks the repo via the GitHub Contents + Trees API and emits
BLOCKING / WARN / OK per check (Golden-Rule structure, no `pip install`
in `setup.sh`, `requires-python` consistency, nginx/app port match,
etc.). Each check carries a citation back to the help-doc anchor
(<https://help.keboola.com/data-apps/python-js/>). Run before
`data-app create` so you don't burn a deploy cycle on a misconfigured
repo. Public repos: drop `--git-pat-env` and use `--git-public`. Total
GitHub call budget per run is ≤5 (1 tree + ≤4 contents) regardless of repo size, so the
60/hour unauth limit rarely fires; pass a PAT for CI loops.

### Inspect the deployed-from git repo (since v0.63.3)

```bash
# Clone URLs + whether the repo is managed by Keboola:
kbagent data-app git-repo --project prod --app-id 12345678
#   ssh_url / https_url / is_managed_git_repo
```

This reads the repo *the app is actually deployed from*, via the
sandboxes-service (`GET /apps/{id}/git-repo`). It complements `validate-repo`,
which inspects an arbitrary repo URL via the GitHub API *before* you create an
app; `git-repo` inspects the repo of an *existing* app server-side.

**Precondition:** the app must have been **deployed at least once**. The git
block is synced from the Storage config into the Data Science app record at
deploy time, so a fresh `--no-deploy` app returns 409 "no Git repository
configured". Run `data-app deploy` first.

### Manage git credentials for a managed repo (since v0.63.3)

```bash
# List credentials of a MANAGED git repo (the secret is never returned):
kbagent data-app git-credentials --project prod --app-id 12345678

# Mint an HTTP token (the one-time secret is printed once, never again):
kbagent data-app git-credentials-create \
  --project prod --app-id 12345678 \
  --type http_token --permissions readOnly --name ci-readonly --yes

# Register an SSH public key instead:
kbagent data-app git-credentials-create \
  --project prod --app-id 12345678 \
  --type ssh_key --permissions readWrite --public-key-file ./deploy_key.pub
```

Credential management applies **only to managed git repos**
(`app.managedGitRepoId` set, provisioned in the UI or via
`data-app create --use-managed-git-repo`). Apps created via
`data-app create --git-repo <url>` are **external**, so `git-credentials-create`
returns 409 "no managed Git repository" for them. Both credential commands also
need an **admin** storage token (`CanManageAppRepoCredentials`), unlike `git-repo`
above which needs only the ordinary project storage token.

### Create an app on a Keboola-MANAGED git repo (since v0.65.0)

```bash
# 1. Provision the app + an EMPTY Keboola-hosted repo (no external URL).
#    Writes no git block; forces --no-deploy (nothing to run yet).
kbagent --json data-app create \
  --project prod --name "My App" --slug my-app \
  --use-managed-git-repo --type python-js --auth public

# 2. Mint a readWrite HTTP token (one-time secret, admin storage token needed).
#    this token authenticates your push in step 3 -- nothing else; the deploy
#    uses the platform's injected credentials.
kbagent --json data-app git-credentials-create \
  --project prod --app-id 12345678 \
  --type http_token --permissions readWrite --name deploy --yes

# 3. Read the managed repo URL and push your code to its default branch (main).
kbagent --json data-app git-repo --project prod --app-id 12345678   # -> https_url
#    The token from step 2 authenticates as the username (Gitea-style), or as
#    the password with any username:
git push "https://<token>@<managed-host>/keboola/app-12345678.git" HEAD:main

# 4. Deploy. The platform injects the clone credentials at deploy time, so no
#    credential is wired into the config.
kbagent data-app deploy --project prod --app-id 12345678 --wait
```

`--use-managed-git-repo` is mutually exclusive with `--git-repo` and every
`--git-*`/PAT flag (managed repos carry no credentials in the config). This flow
is **verified working** -- a tic-tac-toe app deployed and serves from a
Keboola-managed repo with no credential wiring.

**No credential wiring is needed.** The platform injects the `git clone`
credentials at deploy time (the sandboxes-service `testManagedGitRepo.sh`
contract), so `data-app deploy` on a pure managed repo deploys straight from
`app.managedGitRepoId`. If a deploy ever reverts to stopped, diagnose it with
`data-app runs` (`failure_reason` + `startup_logs`).

### Manage app-runtime secrets (since v0.29.0)

```bash
# Set two secrets at once. Plaintext values; the CLI encrypts under
# THIS project's KMS via the Encryption API before writing to Storage.
kbagent --json data-app secrets-set \
  --project prod --app-id 12345678 \
  --secret '#ANTHROPIC_API_KEY=sk-ant-...' \
  --secret '#my-database-url=postgres://...'

# Then redeploy so the running container picks up the new env. The
# JSON envelope from secrets-set carries a `next_step` field with the
# exact command; suppress it with --no-hint-next for scripted callers.
kbagent data-app deploy --project prod --app-id 12345678 --wait

# Inspect what's set -- lists BOTH encrypted (#) secrets and plain env-var
# values; never echoes the encrypted ciphertext in full:
kbagent data-app secrets-list --project prod --app-id 12345678
# -> #ANTHROPIC_API_KEY -> env ANTHROPIC_API_KEY
# -> #my-database-url   -> env MY_DATABASE_URL
# -> ADMIN_EMAILS       -> env ADMIN_EMAILS   (plain, unencrypted)

# Read one key. Leading '#' is OPTIONAL (since v0.43.9). Encrypted secret
# -> metadata only (NEVER decrypts). Plain value -> the literal value:
kbagent data-app secrets-get --project prod --app-id 12345678 --key '#ANTHROPIC_API_KEY'  # metadata only
kbagent data-app secrets-get --project prod --app-id 12345678 --key ADMIN_EMAILS          # shows value

# Remove (idempotent -- absent keys exit 0 with removed=0). '#' optional:
kbagent data-app secrets-remove --project prod --app-id 12345678 \
  --key '#my-database-url' --key ADMIN_EMAILS --yes
```

The runtime exposes each secret as an env var with `#` stripped, `-`
replaced with `_`, and uppercased
(<https://help.keboola.com/data-apps/python-js/>). `secrets-set` does
read-modify-write at the service layer (Storage `merge=True` is
shallow at the top level only and would clobber siblings nested under
`parameters.dataApp.secrets`); every untouched key in the config body
is preserved bit-identical. Encryption is per-project KMS, fail-closed:
if the Encryption API does not return a `KBC::Project*` ciphertext,
the command aborts with `ENCRYPTION_FAILED` and Storage is never
written. Setting a key whose derived env-var name collides with the
runtime-injected set (`KBC_TOKEN`, `KBC_URL` for sure; more TODO
follow-up) emits a stderr WARN -- the platform value silently shadows
yours at runtime.

## Gotchas encoded in the CLI (so you don't have to think about them)

1. **§9 redeploy contract** — `data-app deploy` always sends the
   `{desiredState=running, configVersion, restartIfRunning=true}` trio
   together. Sending just `desiredState=running` would silently pin to the
   empty-shell v2 from `POST /apps`; the runner then errors
   `dataApp.git.repository is required in /data/config.json` (writeup §9).

2. **Per-project KMS encryption** — `data-app create` re-encrypts the PAT
   under the target project's KMS via the Encryption API. Pre-encrypted
   PATs (`--git-pat-encrypted KBC::Project...`) MUST already be encrypted
   under the same project; ciphertext does not cross projects (writeup §8
   row 1). The service refuses to write plaintext if the encryption step
   does not return a project-scoped ciphertext.

3. **Cleanup-in-finally** — if the Storage PUT or initial deploy fails
   after the `POST /apps` shell was created, the orphan shell is deleted
   automatically. Pass `--keep-on-failure` to preserve it for forensics.

4. **Transient `state == stopped` during initial deploy** — the platform
   transitions `created → stopped → starting → running` when the deploy
   starts. The CLI's poll loop refuses to treat `stopped` as terminal
   while `desiredState == running`. Naive callers that exit on `stopped`
   would falsely report a failure (writeup §8 row 1).

5. **Auto-injected `parameters.id`** — after `POST /apps`, the platform
   writes the numeric app id into the Storage config's `parameters.id`. The
   service preserves it on every subsequent update. Stripping it breaks
   the URL minting and produces inconsistent state.

6. **Storage access on by default** (since 0.87.0) — `data-app create`
   writes `runtime.workspace.enabled: true` unless you pass
   `--no-workspace`. Omitting it produces an app that deploys, reports
   `running`, passes its health probe and reads nothing. See "Storage
   access" at the top of this file.

## When to use what

| Goal | Command |
|---|---|
| Inventory: "what data apps does this project have?" | `data-app list` |
| Inspect one: "is this app running? what's its URL?" | `data-app detail --app-id N` |
| Bring a new app online from a git repo | `data-app create` (encrypts + PUTs + deploys; Storage access ON by default since 0.87.0) |
| Grant Storage access to an app that lacks it | `config update --merge --set 'runtime.workspace.enabled=true'` → `data-app deploy` |
| Roll out new code already pushed to git | `data-app deploy --app-id N` |
| Roll out a new Storage config | `config update` (any field) → `data-app deploy` |
| Wake an auto-suspended app | `data-app start --app-id N` |
| Pause a running app temporarily | `data-app stop --app-id N` |
| Read the simpleAuth password | `data-app password --app-id N` (needs Manage token) |
| Set or rotate app-runtime secrets | `data-app secrets-set --app-id N --secret '#KEY=VAL'` then `data-app deploy --wait` |
| Inspect what's set (secrets + plain env vars) | `data-app secrets-list --app-id N` (metadata only, never decrypts) |
| Read one key | `data-app secrets-get --app-id N --key KEY` (`#` optional; encrypted → metadata only, plain → value) |
| Remove a key | `data-app secrets-remove --app-id N --key KEY --yes` (`#` optional; idempotent) |
| Pre-flight a repo before create | `data-app validate-repo --git-repo URL` (GitHub-only, python-js for now) |
| Triage a stuck deploy or runtime crash | `data-app logs --app-id N [--lines N \| --since ISO8601]` (since 0.43.8; plain-text container log tail, default `--lines 500`, `--lines 0` for full buffer) |
| Tear it all down | `data-app delete --app-id N` (cascades to Storage config) |

## What this command group deliberately does NOT cover

- **Auto-log-dump on deploy failure** — `data-app deploy --wait` does
  not yet auto-call `data-app logs` when the job ends in
  `DATA_APP_BUILD_FAILED` / `DATA_APP_DEPLOY_TIMEOUT`. After such a
  failure, run `kbagent data-app logs --project P --app-id N` manually
  to fetch the container log tail (since 0.43.8; before that the only
  path was opening the Keboola UI's "Terminal Log" tab at
  https://help.keboola.com/data-apps/terminal-log-tab/). Tracked as a
  follow-up.
- **Updating size / auto-suspend / git settings** — those live on the
  Storage config body, not the deployment record. Use
  `kbagent config update --component-id keboola.data-apps --config-id ID
  --set 'runtime.backend.size="medium"' --merge` then `data-app deploy`.
  `PATCH /apps {config:{...}}` is silently dropped by the API (writeup §8
  row 3).
- **Rotating the simpleAuth password** — not supported by the API. To
  change the password, delete and recreate the app (writeup §11.2).

## Endpoints used

| HTTP | Path | When |
|---|---|---|
| `POST` | `data-science.<stack>/apps` | `data-app create` step 1 |
| `GET` | `data-science.<stack>/apps` | `data-app list` |
| `GET` | `data-science.<stack>/apps/{id}` | `data-app detail`, poll loop |
| `PATCH` | `data-science.<stack>/apps/{id}` | `data-app deploy / start / stop` |
| `DELETE` | `data-science.<stack>/apps/{id}` | `data-app delete` (cascades to Storage) |
| `GET` | `data-science.<stack>/apps/{id}/password` | `data-app password` (needs Manage) |
| `GET` | `data-science.<stack>/apps/{id}/logs/tail` | `data-app logs` (since 0.43.8; `lines` / `since` mutex) |
| `GET` | `data-science.<stack>/apps/{id}/runs` | `data-app runs` (since 0.65.0; deployment attempts + failure_reason / startup_logs) |
| `POST` | `encryption.<stack>/encrypt` | `data-app create` step 2 (private repo) |
| `PUT` | `connection.<stack>/v2/storage/.../keboola.data-apps/configs/{id}` | `data-app create` step 3, also `config update` |
| `GET` | `connection.<stack>/v2/storage/.../keboola.data-apps/configs/{id}` | `data-app detail` (latest version), `data-app deploy` (read latest) |
