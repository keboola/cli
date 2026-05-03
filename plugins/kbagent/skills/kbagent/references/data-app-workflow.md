# Data App Workflow -- Streamlit / Flask / Node Lifecycle

Data apps in Keboola are deployed from a git repo into a managed container
that auto-suspends after idle. Two API surfaces own them:

| Layer | What it owns |
|---|---|
| **Storage API** (`keboola.data-apps` config) | git block, encrypted secrets, slug, runtime size, name, description |
| **Data Science API** (`/apps`) | deployment record: state, desiredState, url, configVersion |

`kbagent data-app` orchestrates both, plus the project's Encryption API for
git PATs. The CLI encapsulates four documented footguns so callers cannot
hit them; see "Gotchas encoded" below.

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
# Manage token: interactive prompt by default (since v0.28.0); for CI add
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

## When to use what

| Goal | Command |
|---|---|
| Inventory: "what data apps does this project have?" | `data-app list` |
| Inspect one: "is this app running? what's its URL?" | `data-app detail --app-id N` |
| Bring a new app online from a git repo | `data-app create` (encrypts + PUTs + deploys) |
| Roll out new code already pushed to git | `data-app deploy --app-id N` |
| Roll out a new Storage config | `config update` (any field) → `data-app deploy` |
| Wake an auto-suspended app | `data-app start --app-id N` |
| Pause a running app temporarily | `data-app stop --app-id N` |
| Read the simpleAuth password | `data-app password --app-id N` (needs Manage token) |
| Tear it all down | `data-app delete --app-id N` (cascades to Storage config) |

## What this command group deliberately does NOT cover

- **Reading the build / runtime log** — the Data Science API does not
  expose Terminal Logs as JSON; only the Keboola UI ("Terminal Log" tab)
  shows them. If `data-app deploy --wait` exits with
  `DATA_APP_BUILD_FAILED`, the next step is to open the UI link surfaced
  in the error message.
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
| `POST` | `encryption.<stack>/encrypt` | `data-app create` step 2 (private repo) |
| `PUT` | `connection.<stack>/v2/storage/.../keboola.data-apps/configs/{id}` | `data-app create` step 3, also `config update` |
| `GET` | `connection.<stack>/v2/storage/.../keboola.data-apps/configs/{id}` | `data-app detail` (latest version), `data-app deploy` (read latest) |
