# Gotchas -- Response Parsing and Common Pitfalls

<!--
Versioning convention:
- `## Title (since vX.Y.Z)` -- standalone section header for a behavior that
  was introduced in vX.Y.Z and has not changed since.
- `(updated vX.Y.Z -- closes #N)` -- sub-bullet inside an existing
  `(since v...)` section, marking that the original behavior was refined or
  extended in vX.Y.Z. The "since" tag on the parent header stays at the
  introduction version so version-floor scanning still finds the original
  behavior; the inline `(updated vX.Y.Z)` records when the refinement landed.
-->

## Programmatic auth (browser login) is human-only; sentinel tokens; v1 scope (since v0.80.0)

- **`kbagent auth login` requires a human at a browser (or a device to type a
  code into) -- there is no headless/unattended path for THIS command.**
  Never invoke it from an unattended AI agent task; if a user asks an agent
  to "log in", the agent must tell the user to run `auth login` themselves in
  their own terminal. Session tokens are deliberately not readable through
  the CLI once issued. **`auth login-password` (since v0.84.0, below) is the
  headless counterpart** -- it did not exist when this rule was written and
  does not fall under it.
- **PKCE is the default; the device flow is a fallback, not a mode switch.**
  The CLI tries the browser (PKCE authorization-code) flow first and falls
  back to the RFC 8628 device flow ONLY on a *pre-exchange* failure: no
  usable loopback browser, a callback timeout, or an SSH/container/WSL
  heuristic. `--device-code` forces the device flow unconditionally. Once the
  browser callback succeeds (the authorization code was received), there is
  **no** fallback -- an exchange failure past that point is terminal.
- **A session is USER-scoped, not project-scoped.** One `auth login` covers
  every project the signed-in user can access; which project a given command
  talks to is still chosen the normal way (`--project` / `KBAGENT_PROJECT` /
  pin) and is bound to the request via `X-KBC-ProjectId`, not by re-logging-in.
- **`--register-projects` writes a SENTINEL, not a real token.**
  `config.json`'s `token` field becomes the literal string
  `kbc-session://{project_id}` for a session-registered project alias --
  `config.json`'s schema and `CURRENT_CONFIG_VERSION` are UNCHANGED (still 1).
  Seeing that string in `project list --json` or a raw `config.json` read is
  expected, not a corrupted token. An existing alias already pointing at the
  same project+stack is left untouched (`status: "exists"`). A *suggested*
  alias never collides in the first place -- it is computed against every
  existing `config.json` key and suffixes `-{project_id}` past any clash -- so
  `status: "skipped"` only arises when you force a taken alias with
  `--alias ID=ALIAS`, or request a second alias for an already-registered
  project. An existing entry is never overwritten either way.
- **Registered aliases derive from the project NAME, never the numeric
  project id -- `--project 9840` will never resolve.** `login`'s
  accessible-projects table shows a numeric `id`, but the alias
  `--register-projects` (or `auth register-projects`, or the login picker)
  writes is a slug of the project *name* (e.g. `jirka-bq-sox`), suffixed
  with `-{project_id}` only on a name collision. Passing the raw id as
  `--project` is the single most common mistake right after a first login;
  the fix is `kbagent project list` to see the real alias, not re-running
  login. `kbagent auth register-projects [--stack] [--all] [--project-id ID
  ...] [--alias ID=ALIAS ...] [--yes]` (0.80.0+) registers an EXISTING
  session's projects without re-logging-in -- `--all`/`--project-id` are
  non-interactive and safe for an agent to run once a human has confirmed
  the session is live; omitting both starts an interactive arrow-key +
  spacebar checkbox picker (every not-yet-registered project preselected,
  `a` toggles all, `enter` accepts) followed by a single `Edit aliases?`
  confirm (default no) that only opens a per-project alias prompt if opted
  into -- falls back to the original typed `numbers`/`ranges`/`all`/`none`
  prompt on a piped stdin or a terminal without real interactive
  capabilities. Either form needs a real terminal and is NOT for
  unattended/agent use -- it fails fast in a non-TTY or `--json` context
  instead of hanging on a prompt. Two
  accessible projects sharing the same name each get a distinct suggested
  alias (via the `-{project_id}` suffix) instead of the second one being
  silently skipped, unlike the original 0.80.0 `--register-projects` batch
  path. Collision handling matches `--register-projects` above: never
  overwrites an existing `config.json` entry.
- **v1 wires bearer sessions through the Storage + Manage paths.** Everything
  else recognises the `kbc-session://` sentinel and fails FAST with
  `AUTH_NOT_SUPPORTED_ON_STACK`, naming the static-token fallback, rather than
  silently sending the sentinel string as if it were a real credential. The
  authoritative list is `SESSION_UNSUPPORTED_FEATURES` in
  `services/_auth_registration.py`. `auth login` and `auth register-projects`
  print it and both ship it in `--json` as the additive key
  `session_unsupported_features` (`auth status` does **not** carry it), so read
  it from there instead of reconstructing it from memory:
  `kai`; `semantic-layer` (Metastore); `data-app` (Data Science);
  `stream` (Data Streams); `sharing` unless a master token is in the
  environment; the AI Service paths (`docs query`, `config examples`,
  `config new`, `component detail`/`search`, `flow new`/`update`/`validate`);
  the Scheduler Service paths (`flow schedule`, `flow schedule-remove`);
  and the importable SDK
  (`lib.Client`). If a task needs one of those, register the same project again
  with a static Storage token (`project add --project <alias2> --token ...`)
  instead of fighting the guard.
- **Two surfaces people wrongly expect on that list.** `dev-portal` is
  unaffected -- it authenticates with its own Developer Portal identity
  (`dev-portal identity add`), never a project token, so a session project
  changes nothing there. And `flow` splits: `flow list` / `flow detail` are
  plain Storage calls that work, while `flow new` / `flow update` /
  `flow validate --project` fetch the live schema from the AI Service and so
  fail -- they do NOT degrade to the semantic-only validation they fall back to
  when a schema fetch merely errors.
- **`config new` depends on the flag shape, not just `--no-validate`.** The
  scaffold step calls the AI Service unconditionally, and it runs unless BOTH
  `--push` and `--no-files` are set. So plain `config new` and
  `config new --push` fail on a session project regardless of `--no-validate`.
  Only `config new --push --no-files` skips the scaffold; that shape then still
  hits the AI Service if you pass an explicit `--configuration` without
  `--no-validate`, because body validation is the other AI call. Working
  combination on a session project: `--push --no-files` plus either
  `--no-validate` or no explicit body.
- **`kbagent serve` DOES serve session projects -- it is not on the fail-fast
  list.** It delegates to the same already-guarded services, so the REST API
  and web UI work against a sentinel-token project. Two things follow that are
  easy to get wrong: a session is a USER credential, so anyone holding
  `KBAGENT_SERVE_TOKEN` acts as the signed-in user for every session-backed
  project the server exposes (treat that token as equivalent to the user's own
  credential, or serve static-token projects instead); and when the session
  expires at runtime the server answers **HTTP 401 with
  `error_code: SESSION_EXPIRED`**, which no request payload can fix -- a
  browser login only completes on the host, so a human has to run
  `kbagent auth login` there.
- **`project refresh` and `org setup --refresh` SKIP session projects, and
  `--force` does not override that.** There is no static token to replace: the
  credential lives in `auth.json` and its access token rotates on its own. The
  project appears under `skipped` with that reason instead of raising or being
  silently converted. A deliberate single-project conversion to a static token
  is `project edit --token` (next bullet).
- **`project edit --token` on a session project is allowed, warns, and
  converts.** The project becomes a static-token project, which means
  `auth logout --remove-projects` will no longer clean that alias up -- use
  `project remove` when done with it. The warning text is identical in
  `--dry-run`; in `--json` it arrives in an additive top-level `warnings`
  array, so a consumer must not assume the key is absent.
- **`auth logout --remove-projects` needs the `admin` permission class, the
  bare `auth logout` only `write`** (since v0.80.0). The flag deletes
  `config.json` entries, the same observable effect as `project remove`, so a
  policy denying `cli:admin` blocks the flag while still letting the session be
  ended. `permissions list` shows it as its own row,
  `auth.logout --remove-projects`.
- **To find out which credential mode a project uses, read `auth_mode` --
  never parse the token.** `kbagent project list --json | jq '.data[].auth_mode'`
  (also on `project status` and `project info`, and over HTTP on `/projects`,
  `/projects/status`, `/projects/{alias}/info`). Exactly two values, `session`
  and `static`; the field is **always present and never empty**, including on a
  row synthesized for a project whose connection failed, so branch on it rather
  than testing for absence. The human tables show it as an `Auth` column
  (`project list`: before `Token`; `project status`: after `Status`) and
  `project info` as an `Auth` row above the Token rows. A session project's
  human `Token` cell is a dash -- the sentinel is not a credential and masked
  (`kbc-...9840`) it reads like a truncated real token. **`--json` `token` is
  unchanged** (`mask_token(...)`): that contract did not move, so do not expect
  a dash there. `project current` / `project add` / `project edit` carry no
  `auth_mode` by design -- `current` answers which alias is effective (and can
  report a `KBAGENT_PROJECT` override naming an alias absent from the config),
  the other two are write confirmations.
- **A session refresh that times out is a NETWORK error (exit 4), not a
  session error (exit 3) or a config error (exit 5).** `TIMEOUT` /
  `CONNECTION_ERROR` mean the auth service was slow or unreachable, not that
  the login died -- do not tell the user to log in again. The refresh is a
  single attempt under a short budget by design (a retry would re-present the
  same refresh token, which is exactly the replay the server's grace window
  forgives), so the remedy is simply to re-run the command. Only
  `SESSION_EXPIRED` / `SESSION_NOT_FOUND` mean "log in again".
- **An older (pre-0.80.0) kbagent build has no sentinel awareness at all.** A
  session-registered project opened on an old CLI gets an opaque 401 on a
  plain `X-StorageApi-Token` call; a few consumers that treat the token as
  *data* rather than a header (`semantic-layer token --encrypt`, `kai`,
  `sharing`'s master-token fallback) fail differently still. Upgrade first --
  do not try to work around it.
- **Tokens are plaintext in `auth.json` (0600), a sibling of `config.json`.**
  Deliberate RFC 8628 deviation, same posture as the static tokens already in
  `config.json` (see `docs/programmatic-auth-login-plan.md` section 4.2). CI
  and any headless/unattended runner has two options since v0.84.0: `auth
  login-password` (below) if account credentials for this purpose exist, or
  a static Storage token otherwise -- browser login (`auth login` itself)
  still has no non-interactive path by design.
- **Feature-flagged per stack.** A 404 from any `auth` endpoint means browser
  login is not enabled on that stack (not a wrong URL or a bug) -- the CLI
  reports it as `AUTH_NOT_SUPPORTED_ON_STACK` and suggests a static token.
- See `auth-workflow.md` for the end-to-end login -> register -> status ->
  logout walkthrough and PKCE-vs-device troubleshooting.

## `auth login-password` is the CI-safe, headless exception to "browser login is human-only" (since v0.84.0)

- **Password-grant login, no browser, safe for an unattended agent task**:
  `kbagent auth login-password --email E (--password-stdin | --password P |
  KBC_LOGIN_PASSWORD) [--totp-secret SEED | KBC_LOGIN_TOTP_SECRET]
  [--register-projects]`. This is the deliberate carve-out from every "human
  at a browser" rule above -- an agent MAY run it directly when the task was
  given real account credentials for this purpose, the same way it may run
  any other kbagent command with a supplied secret.
- **`--totp-secret` is the base32 TOTP *seed* (the enrollment secret), never
  a live 6-digit code.** kbagent computes the current code itself
  (stdlib-only RFC 6238); no human ever types a code into this flow. An
  account with WebAuthn/passkey-only MFA cannot be resolved here -- the
  server rejects it and the CLI raises `AUTH_MFA_INVALID` (new error code,
  exit 3, see the table below); fall back to telling the user to run
  `kbagent auth login` themselves.
- **A wrong password reports a plain "invalid email or password", not the
  generic session-oriented `INVALID_TOKEN` wording** -- there is no
  client-level token in this flow, only a submitted credential the server
  rejected. A 403 (SSO-enforced account, or an admin whose MFA this grant
  cannot resolve) surfaces the server's own message and names `auth login`
  as the fallback. A 429 reports when the account's rate-limit window
  resets rather than a bare `API error 429`.
- **The session this produces is stored in `auth.json` exactly like a
  browser-login session** (same `auth_mode: session`, same
  `--register-projects` contract, same v1 scope restrictions above) with one
  privilege difference worth knowing: for an MFA-enabled account it carries
  a live 3-hour sudo window that a browser-login session usually does not
  (see `docs/auth.md`) -- treat the credentials backing it with the same
  care as any other long-lived secret, not as a lesser one than a scoped
  Storage token.

## MCP passthrough is REMOVED (since v0.85.0)

- **`tool call` / `tool list` / `agent --type mcp_tool` / the `/mcp/*` serve
  routes no longer exist** (epic #390 phase 3, deprecated since 0.74.0).
  Proposing any of them produces a usage error, not a warning. Every tool the
  catalog exposed has a native command: the full tool-to-command map is
  `docs/mcp-migration.md` in the kbagent repo -- consult it whenever a user,
  an old script or an old runbook names a tool (`update_config`, `get_configs`,
  `query_data`, ...).
- **`keboola-mcp-server` itself is unaffected** -- it is a separate
  distribution that still works with Claude Desktop / Cursor. What changed is
  that kbagent no longer installs or auto-updates it: keep it fresh with
  `uv tool install --upgrade --prerelease=allow keboola-mcp-server` (the
  pre-release flag is still required -- see #324). `kbagent version` reports
  kbagent only (no `dependencies` key) and `kbagent doctor` has no `--fix`.
- **Persisted `mcp_tool` agent tasks are tombstoned, not deleted.** They
  survive an `agents.json` load/save round-trip untouched, the scheduler skips
  them instead of firing them, a manual `agent run` records an error naming
  the migration guide, `agent list` marks the row, and `kbagent doctor`
  reports them as FAIL (`mcp_tool_tasks`; `details.tasks[]` in `--json` lists
  the task ids -- the native command is NOT in the payload, look it up in
  `docs/mcp-migration.md`).
- **There is NO migration command, by decision.** The old parity map knew
  which native command replaces a tool, but not how to map arguments:
  `mcp_tool` params are the tool's own `input` dict (`componentId`) while the
  CLI takes flags (`--component-id`), and for several tools the shape differs
  too. Guessing the argv of a scheduled WRITE task that then runs unattended
  is the worst place to be wrong.
- **Recipe for migrating one task** (this is the whole procedure):
  1. `kbagent --json doctor` -> `checks[].details.tasks[]` for the affected
     task ids.
  2. Read that task's `action.params.input` from `<config_dir>/agents.json`
     (or `kbagent --json agent show <id>`) -- those are the values to carry over.
  3. Look the tool up in `docs/mcp-migration.md`, then
     `kbagent <native command> --help` -> map each input key to its flag. This
     is the step no tooling does for you; the naming differs per tool.
  4. `kbagent agent create --name ... --cron ... --type cli_command --argv ...`
     with the result, verify it once with `kbagent agent run <new-id>`, then
     `kbagent agent delete <old-id>`.
- **`agent update` CANNOT change a task's action** -- it patches name, cron,
  enabled/manual and the trigger only, so `--type` / `--argv` are not options
  there and exit 2. Migration therefore means create-new + delete-old, which
  has one trap: **the task id changes**. `Trigger.task_id` chains tasks by id,
  so if any OTHER task has `--trigger-task-id <old-id>`, repoint it with
  `kbagent agent update <that-task> --trigger-task-id <new-id>` BEFORE deleting
  the old one, or the chain breaks silently. Check with
  `kbagent --json agent list` and grep the `trigger` blocks for the old id.
- **`tool:read` / `tool:write` / `tool:destructive` permission patterns are
  inert.** A persisted policy still LOADS with them, but they match nothing --
  so a `mode="deny"` policy whose only read allowance was `tool:read` now
  denies everything (fail-closed). Re-run `kbagent permissions set` with
  `cli:read` if that was your setup.

## Native parity commands (since v0.73.0)

- **These commands are the native answers to tools the removed passthrough
  used to expose**: `docs query`, `config examples`, `semantic-layer schema`,
  `component sync-action`, `transformation create|show|edit`, `flow examples`.
  `query_data`'s CLI answer is `workspace query`. **VERSION GATE**: below
  0.73.0 they do not exist. (The passthrough itself is gone since 0.85.0 --
  see the removal section above; `tool:*` permission patterns are inert.)
- **`component sync-action --row-id` merge is SHALLOW**: row
  `parameters`/`storage` top-level keys replace the root's wholesale -- a row
  that sets `parameters.db` replaces the ENTIRE root `db` object, it does not
  deep-merge into it.
- **`transformation edit` ids are positional and renumber** after every
  structural op -- always `transformation show` immediately before `edit`
  (fresh-fetch rule). `--storage` REPLACES `configuration.storage` wholesale.
- **`semantic-layer schema` resolves the default schema VERSION** -- the bare
  metastore endpoint returns only a `{versions: [...]}` listing (the upstream
  keboola-mcp-server tool ships that listing as-is; the CLI fetches the real
  document and reports `schema_version`).
- **`docs query` vs `kai ask`**: `docs query` is documentation-only RAG (any
  token, no feature flag, no project data); `kai ask` sees project data but
  needs the master token + `agent-chat` feature.

## `token` group mints/rotates/revokes SCOPED Storage tokens; secret shown ONCE (since v0.66.0)

- **`kbagent token create --project P --description D [--bucket-write B ...]
  [--bucket-read B ...] [--component-access ID ...] [--can-read-all-file-uploads]
  [--expires-in N]`** mints a NEW Storage token scoped by bucket permissions,
  `token refresh --token-id ID` rotates the secret in place, `token delete
  --token-id ID` revokes. All three back the SDK facade
  (`Client.create_scoped_token` / `refresh_token` / `delete_token`) and the same
  service layer.
- **`create` requires a MASTER (admin) token — `canManageTokens` alone is NOT
  enough.** A non-master token carrying the flag is the Storage API's
  "impossible state" (`CreateTokenVoter` `LogicException` → generic 500, issue
  #599) — exactly the shape `org setup` / `project refresh` mint. Since v0.89.0
  `token create` pre-flights `isMasterToken` and fails fast with
  `MISSING_MASTER_TOKEN` (exit 3) naming the fix (`kbagent project edit
  --project ALIAS --token <MASTER>`).
- **That defect is CREATE-ONLY — `refresh` / `list` / `delete` are NOT
  master-guarded.** `RefreshTokenVoter` lets any token rotate **itself** and a
  `canManageTokens` token rotate another, so rotating a leaked device token
  from an `org setup` project token works and kbagent deliberately does not
  block it (the incident path is the last place to add a credential upgrade
  step). Do not "fix" a failing `refresh` by hunting for a master token — read
  the actual error. A narrowly-scoped device token has neither privilege, so
  you still cannot bootstrap tokens from a token you just minted.
- **`token refresh` does not write the new secret back to `config.json`.** The
  value is printed once, exactly like `create`. Rotating the very token an
  alias uses therefore leaves that alias holding a dead value — follow it with
  `kbagent project edit --project ALIAS --token <NEW>` or the next command
  fails on auth.
- **The secret is a ONE-TIME reveal.** `create` / `refresh` print the token value
  once (human mode: inside a Rich panel; `--json`: the `token` field). It is never
  retrievable again — persist only the `id` (to revoke/refresh later) and `expires`.
  Lost the secret? `token refresh --token-id ID` mints a new one.

## Files-upload permission: `canReadAllFileUploads` gates READING, not the UPLOAD (since v0.66.0)

- **Any valid Storage token can upload its OWN Files.** Uploading is NOT gated by
  `canReadAllFileUploads` or by `componentAccess`. `canReadAllFileUploads` controls
  whether the token may *read/list other tokens'* file uploads — it has nothing to
  do with the upload action itself. `componentAccess` is unrelated to Files entirely
  (it scopes which components' configs the token may touch).
- **To scope a DEVICE token that uploads into a bucket, grant
  `bucketPermissions[<sinkBucket>]=write`** (`token create --bucket-write
  in.c-otlp-<source>`). That write grant on the destination bucket is the
  load-bearing scope — not `--component-access`, not `--can-read-all-file-uploads`.
- Practical enrollment pairing: `stream create-source` provisions the OTLP source +
  its `in.c-otlp-<source_id>` sink bucket, then `token create --bucket-write
  in.c-otlp-<source_id> --expires-in 3600` mints exactly the token the device holds.

## `stream create-source` uses a NORMAL Storage token — there is NO master-token gate (since v0.66.0)

- Creating a Data Streams source (`kbagent stream create-source` /
  `Client.create_stream_source`) authenticates with the ordinary project Storage
  token — **NOT** a master token. There is **no `stream.api.masterTokenRequired`
  error code**; do not code against one or tell a user they need a master token.
- The documented create-time errors are `stream.api.sourceAlreadyExists` (HTTP 409
  — use `stream create-source --if-not-exists` to make it idempotent) and
  `stream.api.resourceLimitReached` (HTTP 422 — the project hit its source quota).

## `sync status` + `doctor` flag plaintext `#`-secrets in synced configs (since v0.55.0)

- **What it catches.** The v0.54.0 fix is forward-looking -- it does NOT
  retroactively encrypt secrets written by older versions. This audit finds
  them: an **in-sync** config/row (local file hash == manifest `pull_hash`)
  whose `#`-prefixed value is still plaintext means the remote holds it
  unencrypted (it passed through the sync baseline unencrypted).
- **`sync status`** adds a `plaintext_secret_warnings` array (`--json`) / red
  block (human) listing affected configs + key *paths* (never the secret
  values). It prints even when there are no local changes.
- **`doctor`** adds a `sync_secrets` check: `warn` (with the affected configs)
  when the current dir is a sync working tree (`.keboola/manifest.json`) holding
  plaintext secrets, else `pass`; `skip` outside a sync tree.
- **Pending edits are NOT flagged.** A locally-edited-but-unpushed config (hash
  != `pull_hash`) is skipped -- a `sync push` on >=0.54.0 encrypts it on write,
  so flagging it would be noise. Only already-synced plaintext is a real leak.
- **Remediation in the warning:** re-push on >=0.54.0 to encrypt AND **rotate**
  the credential -- config version history keeps the old plaintext. Read-only,
  no API call.

## `--change-description` is the version audit line; `--description` is the display name (since v0.77.0, #505)

- **Two different fields, similar names.** `config update` / `config row-update`
  accept both. `--description` overwrites the configuration's **display
  description** (the text shown in the UI, persisted on the config itself).
  `--change-description` writes the **new version's `changeDescription`** -- the
  one-line "why" in version history. Passing the wrong one either clobbers a
  description someone wrote, or buries the reason for the change.
- **Omitting it is safe.** Without the flag kbagent keeps the previous
  auto-generated default verbatim (`Updated metadata + configuration via kbagent
  config update`), so existing callers see no behavior change.
- **`--dry-run` echoes it.** The dry-run result carries `change_description`
  (JSON) / a `changeDescription:` line (human), so the audit text can be
  reviewed before the write -- unlike the version number, which does not exist
  until the PUT lands.
- **Read it back from the config, not a version list.** `config detail` returns
  the current version's `changeDescription` at the top level; there is no
  `config versions` command.
- **Not available on creates.** `config new`, `config row-create`, `flow new`,
  and `flow update` do NOT take the flag -- their client methods have no such
  parameter yet. Only the two update paths are covered.

## `config create/update` + rows auto-encrypt `#`-prefixed secrets before write (since v0.54.0, closes #378)

- **Before v0.54.0 this was a plaintext leak.** `config new --push`,
  `config update`, `config row-create`, and `config row-update` POSTed/PUT the
  configuration JSON straight to Storage **without encrypting** `#`-prefixed
  values. The Storage API stores config JSON verbatim -- it does NOT encrypt
  `#`-values server-side -- so a `#password` / `#api_token` landed readable in
  plaintext in Storage, in every config version, and was re-exposed on every
  read. (The `sync push` and variables paths already encrypted; only the
  interactive config paths skipped it.) Verified live on projects 4214 + 10539.
- **Since v0.54.0** these paths call the Encryption API first, so a read-back
  returns `KBC::ProjectSecure::...` (not the plaintext). Same service layer
  (`ConfigService`) backs the CLI and the `serve` REST config routes, so both
  are covered.
- **Fail-closed by default.** If encryption fails (or the project scope cannot
  be resolved) the write is aborted with `ENCRYPTION_FAILED` rather than
  writing plaintext. Pass `--allow-plaintext-on-encrypt-failure` (named to
  match `sync push`) to downgrade to a warning -- bootstrap/debug only.
- **Dry-run is NOT encrypted on purpose.** `config update --dry-run` shows the
  plaintext value in the diff so it stays readable and deterministic
  (ciphertext is non-deterministic); nothing is written to Storage.
- **`--set` targets a dot-path.** `--set '#password=...'` sets a *top-level*
  `#password` key, not `parameters.#password`. For a nested secret use
  `--set 'parameters.#password=...'` or a full `--configuration`. (Either way
  the value is now encrypted before write.)

## Encryption ciphertext has a different prefix per cloud; Azure was rejected (since v0.86.0, #607)

- **One project-scoped prefix per cloud.** The Encryption API returns
  `KBC::ProjectSecure::` on AWS, `KBC::ProjectSecureGKMS::` on GCP and
  `KBC::ProjectSecureKV::` on Azure (Key Vault). All three mean the same
  thing -- project-bound ciphertext -- and none of them is interchangeable
  across stacks. Never assert on a single literal prefix when checking
  whether a value is encrypted; match the whole family, or just `KBC::`.
- **Before v0.86.0 the Azure variant was missing from the data-app
  whitelist**, so on an Azure stack a *correctly* encrypted value was
  rejected as "not project-scoped" and the write aborted with
  `ENCRYPTION_FAILED` -- before anything reached Storage. Affected:
  `data-app create --git-pat-env / --git-pat-file / --git-pat-encrypted`
  (private repos were impossible on Azure) and `data-app secrets-set`.
  `data-app secrets-list --show-fingerprint` / `secrets-get` also returned an
  empty `fingerprint` + `encryption_prefix` for Azure ciphertext.
- **Fail-closed is unchanged.** Only the three project-scoped prefixes pass;
  look-alikes (`KBC::ProjectSecureKVX::`), the broader `KBC::ComponentSecure*`
  / `KBC::ConfigSecure*` scopes and plaintext are still refused, and plaintext
  is still never written to Storage.
- **The sync side had the same gap (#612).** `sync diff` / `sync status`
  redact ciphertext so it never shows up as a diff; until 0.86.0 the detector
  knew only the AWS-form prefixes, so on GCP/Azure an encrypted value stored
  under a **non-`#` key** was treated as plaintext. It now derives the whole
  family from the platform registry (8 scopes x 3 clouds + 3 legacy ciphers).
  One-time effect: such a config hashes as changed on the first run after the
  upgrade. `#`-prefixed keys were never affected.
- **If you hit `ENCRYPTION_FAILED` on a data app, check the kbagent version
  first** -- on Azure with <= 0.85.0 it is this bug, not a broken token or a
  broken Encryption API. Upgrade rather than reaching for
  `--allow-plaintext-on-encrypt-failure`, which would write the PAT in
  plaintext into Storage.

## `sync push` fresh-CREATE writeback now updates placeholders in place (since v0.47.0)

Before v0.47.0, `kbagent sync push` always **appended** new `ManifestConfiguration`
(and `ManifestConfigRow`) entries to `.keboola/manifest.json` on every CREATE.
The FIIA / scaffold emit pattern — pre-populating manifest entries with
placeholder ids before the first push — therefore produced manifests with
N placeholders + N real entries (= 2N) after one push, and every placeholder
still looked `added` on re-push (spurious duplicates on remote).

Starting in v0.47.0, the create path looks up an existing entry by
`(component_id, path)` and **updates it in place** (id, branch_id, pull_hash,
pull_config_hash refreshed; user-declared `KBC.configuration.*` metadata
preserved). When no placeholder is found, the legacy append path still fires
(so commands like `sync init` followed by direct push of newly-pulled remote
configs are unaffected).

Two follow-on contract notes:

- **Re-push idempotency comes for free.** After the first push the manifest
  holds the real ULID, so the diff engine matches against remote_configs and
  reports `status: no_changes, created: 0`. No more "every fresh-create
  emit doubles the manifest" workaround needed.
- **`KBC.configuration.*` metadata propagates on CREATE.** If a placeholder
  entry's `metadata` dict contains keys starting with `KBC.` (e.g.
  `KBC.configuration.folderName`), they are POSTed to the metadata API
  via `client.set_config_metadata` immediately after the create call.
  Bookkeeping keys (`pull_hash`, `pull_config_hash`, ...) are filtered
  out by the `KBC.` prefix check. `_push_update` does **not** propagate
  metadata — use `kbagent config set-metadata` (or `config set-folder`)
  for that.

If a downstream consumer has been working around the duplication by
post-processing the manifest, drop that workaround. The single-entry
manifest is the new contract.

## `sync push` fresh-CREATE now resolves variable links, hoists row `values`, and `--branch` promotes the default tree (since v0.47.2)

A transformation scaffolded alongside its sibling `keboola.variables` config +
default-values row is now **runnable after a single `sync push`** — no post-push
`config variables-set` workaround needed. Three things changed in the create pass:

- **Row `values` are no longer dropped.** A `keboola.variables` values row whose
  scaffold `_config.yml` has top-level `values: [...]` but no `_keboola` block
  now hoists `values` into the API body (the push callers pass the known
  `component_id` into `local_row_to_api`). Pre-0.47.2 the row was created with an
  empty `configuration.values`.
- **Rows whose parent config was created in the same push now succeed.** Push runs
  in ordered phases (configs first, then rows); a row's `parent_config_id` is
  remapped from the diff-time placeholder to the freshly-assigned ULID before
  `create_config_row`. Pre-0.47.2 this raised `PARENT_CONFIG_NOT_TRACKED`.
- **`variables_id` / `variables_values_id` are rebound to ULIDs.** After the
  variables config + its values row are created, a backfill pass PUTs the
  transformation's corrected `configuration.variables_id` / `variables_values_id`
  (via `update_config`, NOT `set_variables` — that would create a *second*
  variables config), rewrites the local `_configuration_extra`, and refreshes the
  manifest hashes so a re-push is clean. Pre-0.47.2 the remote kept placeholder
  strings and `job run` failed with `Variable configuration "<placeholder>" not
  found`. When the placeholder can't be matched exactly but exactly one
  `keboola.variables` config was created this push, it binds to that one with a
  warning; zero or ambiguous (>1) matches surface a `variable_link` entry in the
  push `errors` array rather than writing a broken link.

`sync push --branch <id>` now **promotes the local default tree** (`main/`) to the
target dev branch when no `<branch_name>/` subtree exists on disk, instead of
erroring with `Config file not found`. Source (where files are read) and target
(where the API writes) are decoupled; API calls still target the branch id. When a
per-branch subtree *does* exist, behaviour is unchanged.

## `sync push` / `sync pull` / `sync diff` accept `--branch <id>` for per-invocation dev-branch targeting (since v0.47.0)

The `--branch` override wins over every other branch source: `manifest.branches[0]`,
`active_branch_id` (set by `kbagent branch use`), and the git-branching
`branch-mapping.json`. Required exactly one `--project` (branch id is per-project).
Useful for targeting a freshly-created dev branch without running `branch use` or
`sync branch-link` first. The override is per-invocation only — it does not persist
to the manifest or to the config store, so subsequent commands without `--branch`
fall back to the normal priority chain.

## `storage create-table --if-not-exists` returns `action: skipped` instead of raising on duplicate display name (since v0.47.0)

Opt-in flag (default `False`, so existing callers are unaffected). When set,
catches the specific `STORAGE_JOB_FAILED` + "already has the same display name"
error from the Storage API, probes `get_table_detail(target_id)`, and returns
`{action: "skipped", skip_reason: "table already exists", table_id: ...}` when
the table really exists at the expected id. A different table that happens to
share the display name still raises (real conflict to resolve). The response
envelope now always carries `action: "created" | "skipped"` so programmatic
callers can branch on outcome. Safe for parallel workers (e.g. FIIA's
8-worker scaffold pattern that previously surfaced ~12 spurious errors per run).

**Skipped envelope reports the ACTUAL existing schema (since v0.47.1, keboola/cli#349).**
When `action == "skipped"`, `columns` / `primary_key` / `name` reflect the
EXISTING table's real schema (read from the `get_table_detail` probe that
confirms the table exists), not the caller's request. The caller's requested
values are preserved under `requested_columns` and `requested_primary_key`, and
`schema_drift: true` flags when the existing table diverges from what was
requested. So the skipped envelope IS a valid discovery mechanism — a caller can
trust `columns` / `primary_key` as the real shape and inspect `schema_drift` to
detect "I hit a pre-existing table with a different shape". (Before v0.47.1 the
skipped envelope re-echoed the request, so older installs must still call
`kbagent storage table-detail` after a skip to get the real shape.)

## `sync push --no-name-drift-warnings` suppresses the cosmetic warnings array (since v0.47.0)

When local directory names diverge from the canonical kbagent naming (e.g.
FIIA's `var-07-fi-daily-date-refresh` pattern), `sync push` normally returns
a `name_drift_warnings: [...]` array on the result envelope. The
`--no-name-drift-warnings` flag drops that field. The underlying detection
still runs, so a future operator who wants to audit can flip the flag off
without losing data.

## `semantic-layer search-context` + `get-context` cover the upstream `search_semantic_context` / `get_semantic_context` parity (since v0.47.0)

`kbagent semantic-layer search-context --project P [--pattern G ...] [--type T] [--limit N]`
is project-wide (not model-scoped). Patterns are **case-sensitive `fnmatch`** against
`attributes.name`, repeatable (union). Default `--type all` searches every CHILD
type (datasets, metrics, relationships, constraints, glossary) and does **not**
include semantic models — pass `--type model` to search those. The response
envelope is `{project, contexts: [{id, type, name, description, attributes}], total_count}`.
The `type` field is the CLI-friendly singular (no `semantic-` prefix on the wire form).

`kbagent semantic-layer get-context --project P --context-id ID` probes
`semantic-model` first then every child type until a 200 lands. A 404 on any
single probe is non-terminal (keeps trying); only a full miss across all 6
types raises `NOT_FOUND` (exit 1). **Non-404 errors propagate immediately**
without continuing the probe — a 500 on the dataset type does not get
swallowed by the subsequent metric probe.

These two subcommands cover the pre-flight pattern FIIA uses to verify a
project's semantic model is populated before kicking off a downstream
pipeline; the previous workaround (a `keboola-mcp-server` entry in `.mcp.json`
solely for these two tools) can be dropped.

## `semantic-layer reference-data` holds a whole dimension as ONE record; `set` is PUT-replace, not append (since v0.55.0)

`semantic-reference-data` stores one record **per dimension** (e.g. a Chart
of Accounts), with the full member list in a `members[]` array — NOT one
record per member. Consequences an agent must internalize:

- **`set` replaces the entire members array.** `kbagent sl reference-data
  set --dimension chart_of_accounts --members-file coa.json` is
  create-or-replace, idempotent on the `dimension`. To add/remove a
  single account you must `get` the record, mutate the array client-side,
  and `set` the whole thing back. There is no per-member endpoint.
- **It uses the metastore's real `PUT`** (revisioned update, `meta.revision`
  increments, history preserved) when a record for that dimension already
  exists — distinct from the DELETE+POST that `edit metric|…` uses.
  A brand-new dimension is `POST`-ed.
- **The envelope `name` is the dimension, unique per project per type, so
  the `set`/`get` lookup is project-wide.** Because the dimension name is the
  project-unique key, `set` finds and PUT-replaces an existing record
  regardless of which `--model` you pass (the resolved model is just stored
  on the record) — it does NOT POST and collide with `ALREADY_EXISTS`. For
  the same reason `get --dimension` needs no `--model` (one dimension name
  per project for this type). `get` rejects passing both `--id` and
  `--dimension` (exit 2).
- **Member field names mirror the `DIM_COA` columns 1:1 (snake_case):**
  `account_code` (required key), `account_name`, `parent_code`, `is_leaf`
  (integer 0/1, not bool), `level_1_code`, `cf_category`, … The metastore
  schema sets `additionalProperties: true` on members, so unknown columns
  are stored but not validated.
- **Deliberately invisible to `build` / `export` / `diff` / cascade /
  `PUSH_ORDER`.** Deleting a model does NOT cascade-delete its
  reference-data records (they are not model children in the snapshot
  sense); `export`/`diff` will not include them. Manage them only through
  the `reference-data` sub-app.
- **JSON Schema cannot enforce cross-member referential integrity** (every
  `parent_code` resolving to some member `account_code`). That stays an
  app/sync-layer concern.

## `workspace list` / `workspace detail` now expose loginType + RO + qs_compatible (since v0.42.0, closes #304)

Before v0.42.0 the Storage workspace endpoint already returned
`connection.loginType` and `readOnlyStorageAccess`, but kbagent discarded
both fields when normalising the response. The only way for a data-app
developer to learn whether a workspace was Query-Service-compatible was to
fire a query and read the failure (`code:
storage.executeQuery.notSupportedLoginType`). v0.42.0 surfaces both fields
plus a derived `qs_compatible: bool`.

**Output shape (JSON):**

```jsonc
{
  "workspaces": [{
    "id": 2950518214,
    "name": "RO",
    "backend": "snowflake",
    "host": "...",
    "database": "sapi_901",
    "warehouse": "KEBOOLA_PROD",
    "schema": "WORKSPACE_...",
    "user": "...",
    "login_type": "snowflake-service-keypair",
    "read_only": true,
    "qs_compatible": true,
    "component_id": "keboola.sandboxes",
    "config_id": "01kj..."
  }],
  "errors": []
}
```

**Compatibility is keyed by (backend, loginType) -- since v0.58.0.** The same
`default` string means opposite things per backend, so there are two whitelists.

Snowflake (`constants.QUERY_SERVICE_COMPATIBLE_LOGIN_TYPES`):

- `snowflake-service-keypair` -- confirmed PASS
- `snowflake-person-sso` -- confirmed PASS
- `snowflake-person-keypair` -- confirmed PASS (since v0.47.1)
- `snowflake-legacy-service` -- explicitly OFF the list (works on
  `connection.keboola.com` but FAILED on GCP us-east4 stack in the
  original #304 incident -- keep it off until cross-stack confirmation)
- `default` on Snowflake (legacy 2016 workspaces) -- confirmed FAIL
  (`JWT token is invalid`)

BigQuery (`constants.QUERY_SERVICE_COMPATIBLE_LOGIN_TYPES_BIGQUERY`):

- `default` on BigQuery -- confirmed PASS (since v0.58.0). Every BigQuery
  workspace carries loginType `default` (the sandbox API exposes no
  Snowflake-style variants for BigQuery), and the Query Service runs SELECTs
  against it -- verified live against project 9621 on `connection.keboola.com`.
  Before v0.58.0, kbagent's whitelist was Snowflake-only, so BigQuery
  workspaces were mislabeled `qs_compatible: false` and hidden by
  `workspace list --qs-compatible`, even though `workspace query` worked.

`qs_compatible: false` does NOT mean "broken"; it means "not on the
confirmed-good whitelist". For an unknown loginType, `workspace list`
renders it as `?` (yellow) in the QS column so callers know the policy
is uncertain rather than confirmed-bad.

## `workspace query`: fast inline results vs `--full` CSV export (since v0.59.0)

By default `workspace query` now reads the result set inline via the Query
Service `GET /api/v1/queries/{job}/{stmt}/results` endpoint (JSON `columns` +
`rows`) instead of materializing a CSV file through the warehouse UNLOAD path
(`.../export?fileType=csv`). The inline path skips the file round-trip, so
interactive queries are markedly faster.

- The inline path is **paginated**: it fetches at most `--limit` rows (default
  500), walking `offset` in pages. When the warehouse has more rows than
  fetched, the statement is marked `truncated: true` (with `total_rows` =
  the full count) and the CLI prints `Showing first N of TOTAL rows. Use --full`.
- The `/results` endpoint enforces **`100 <= pageSize <= 100000`** (a smaller
  `pageSize` 400s with `Invalid pageSize parameter, must be between 100 and
  100000`). kbagent therefore requests a fixed valid page size and trims the
  result to `--limit` locally -- `pageSize` is NOT derived from `--limit`, so a
  `--limit 5` still works (fetches one valid page, returns 5).
- Each statement carries structured `columns`, `rows`, `row_count`,
  `total_rows`, `truncated`, **and** a synthesized `csv_data` string. Parsers
  that read `csv_data` (the pre-0.59.0 shape) keep working unchanged.
  VARIANT/ARRAY/OBJECT (Snowflake) and STRUCT/ARRAY (BigQuery) cells are
  emitted in `csv_data` as compact JSON (`{"k":"v"}`) to match the warehouse
  CSV export, not Python `repr`.
- `--full` opts back into the complete CSV export -- slower (warehouse UNLOAD),
  but **uncapped**. Use it when you need every row, e.g. a bulk extract
  (`workspace query --full --json`). Under `--full` the statement carries only
  `csv_data` (no structured `columns`/`rows`).
- The `kbagent serve` `/workspaces/{p}/{w}/query` REST endpoint defaults to
  `full=True` so the web UI's "Download CSV" stays complete; REST clients can
  pass `full=false` (+ `limit`) in the JSON body to opt into the fast path.

## Snowflake `workspace create` returns `private_key`, not password (since v0.47.1)

Headless `workspace create` on Snowflake requests
`loginType: snowflake-person-keypair`, generates an RSA key pair locally,
passes the public key to the Storage API, and returns the private key once in
the creation envelope:

```jsonc
{
  "backend": "snowflake",
  "user": "KEBOOLA_WORKSPACE_42",
  "password": "",
  "private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
}
```

For successful Snowflake creates, `private_key` is the credential to save and
use for key-pair authentication; `password` remains in the envelope for
backward compatibility but should be treated as empty/unusable. BigQuery
workspaces keep the previous password-based/default backend shape and do not
return `private_key`.

**Filter (data-app pre-selection):**

```bash
kbagent --json workspace list --project prod --qs-compatible
# returns only workspaces with login_type ∈ whitelist AND read_only=true
```

**Branch behaviour (read-command parity with `storage buckets`):**

`workspace list` / `workspace detail` now follow the same pattern as
`storage buckets` / `storage tables` / `config list`: when an alias is
pinned to a dev branch via `branch use`, the production endpoint is used
with an `Info: Using production branch for read (active dev branch X
ignored; pass --branch X to override)` banner. Before v0.42.0 these
commands silently scoped to the pinned branch, returning a different
workspace set than the same alias one shell ago. Pass `--branch ID` to
opt back into the dev-branch endpoint. `--branch` requires exactly one
`--project`.

## `config detail --component-id keboola.sandboxes` now annotates the misleading `parameters.id` (since v0.42.0, closes #304)

The sandbox config's `parameters.id` field (e.g. `1296392806`) looks like
a Storage workspace ID but is actually a sandbox-service-internal handle.
Passing it to `workspace detail --workspace-id 1296392806` returns 404.
The real mapping is the other way around: each Storage workspace exposes
`configurationId` pointing at its sandbox config.

`config detail --component-id keboola.sandboxes --config-id <ID>` now
appends a `sandbox_annotation` block:

```jsonc
{
  // ... original config detail fields unchanged ...
  "sandbox_annotation": {
    "sandbox_service_id": "1296392806",
    "storage_workspace_id": 2950518214,
    "note": "`parameters.id` in a keboola.sandboxes config is the sandbox-service internal ID, NOT the Storage workspace ID. Use `storage_workspace_id` with `kbagent workspace detail --workspace-id ...`."
  }
}
```

When no workspace is currently backed by the sandbox config (orphan
sandbox), `storage_workspace_id` is `null` -- the annotation block still
appears so callers can distinguish "annotation did not run" from "ran but
no workspace found".

**Single-config mode only.** Bulk mode (`--config-id` omitted) skips the
annotation to avoid N+1 (one `list_workspaces` per config). Use
`workspace list --project NAME` as a one-shot lookup instead.

**HTTP / REST parity** (updated v0.43.1 -- closes #312): the annotation
now lives in `ConfigService.get_config_detail()` behind an opt-in
`include_sandbox_annotation: bool = False` parameter, not only in the
CLI command. `GET /configs/{project}/{component_id}/{config_id}` on
`kbagent serve` accepts `?include_sandbox_annotation=true` to switch it
on. Default off so existing programmatic / web UI consumers see the
unchanged shape -- a regression-free upgrade. The CLI command always
opts in to preserve v0.42.0 behavior. If `list_workspaces` fails (rate
limit, transient 5xx), the detail call still succeeds and
`storage_workspace_id` is set to `null` -- the annotation is UX, not a
contract.

## `kbagent job run --mode debug` redirects output to a Storage File, not the destination buckets (since v0.43.6)

`kbagent job run` accepts `--mode run|debug` (default `run`). The flag is
threaded straight into the Queue API job-creation body as `"mode": "..."`;
`run` is the unchanged historical wire shape. `--mode debug` flips the Queue
worker into debug execution: the component starts with the same configuration
and inputs as a normal run, but its output stream is **not** written to the
configured destination buckets. Instead the worker uploads the output bytes
to a Storage File tagged `debug-<jobId>` (you can pull it down with
`kbagent storage file-download --tag debug-<jobId>`). Useful for:

- **Dry-runs against production configs** -- reproduce a failing job exactly
  as it happened in prod without touching downstream tables, and inspect the
  worker's actual output bytes after the fact.
- **Seeding component test fixtures** -- harvest the debug output file as
  ground-truth input for a new VCR recording or a component test case (the
  intended use case that drove this flag).
- **Validating a config change before merging** -- run the new config in
  debug mode, diff the output file against the previous debug file, only
  promote once the diff is acceptable.

The CLI gates the flag with `click.Choice` so a typo like `--mode dry-run`
exits 2 with a Click usage error before any wire call -- it cannot reach the
Queue API and surface as an opaque 422. Service-layer also validates against
`VALID_JOB_MODES` so direct programmatic callers get a
`KeboolaApiError(INVALID_ARGUMENT)` rather than a silent passthrough.

The human-mode `Running ...` banner appends a bold-yellow `mode=debug` chip
when the flag is non-default, so operators see at a glance that a run is
diagnostic, not production. `--json` output shape is unchanged (the mode is
visible on the returned job dict via the Queue API's own response shape).

Before this release the `mode` parameter existed on `KeboolaClient.create_job`
but neither `JobService.run_job` nor `commands/job.py` exposed it, so every
job created via `kbagent` hard-coded `mode: "run"` on the wire.

## Metastore duplicate-name POST returns 409 OR 500 -- both map to `ALREADY_EXISTS` (since v0.43.5)

`MetastoreClient.post_item` normalises **both** the post-go-monorepo-PR#513
HTTP 409 (`"Object with this name already exists in this project"`) and the
legacy HTTP 500 (`"Failed to create meta object"`) into
`ErrorCode.ALREADY_EXISTS` -- the user-facing message and command-layer
exit-code mapping stay the same across stacks during the rollout window.

**Why an AI agent might trip on this:** raw `httpx.post(...)` calls against
`metastore.*.keboola.com/v1/api/repository/...` (instead of going through
`kbagent semantic-layer add ...` / `MetastoreClient`) get the bare HTTP
status. Code that branched on `status_code == 500` to detect duplicates
silently breaks on a 409-enabled stack; code that only handles 409 silently
breaks on a legacy 500 stack. Use `kbagent semantic-layer ...` and let the
client do the mapping.

**Retry behaviour:** 500 is in `RETRYABLE_STATUS_CODES` so the legacy path
costs `MAX_RETRIES` round-trips before the normaliser fires; 409 is not, so
post-fix duplicates resolve in a single round-trip. No code change needed
in callers either way.

## `semantic-layer model delete` cascade-deletes children (since v0.43.4)

`kbagent semantic-layer model delete --project P --model M` used to DELETE
only the parent `semantic-model` row, leaving every dataset / metric /
relationship / constraint / glossary term on the wire pointing at the
now-dead `modelUUID` (issue #306). The orphans were invisible until the next
`build` or `import` hit HTTP 422 `semantic-dataset with name 'X' already
exists in the target model` on a same-named dataset — names are unique
**per project**, not per model.

Since this release the command walks `reversed(PUSH_ORDER)` (constraints →
glossary → relationships → metrics → datasets) and deletes each child via
`client.delete_item` before the parent. `--yes` still skips the
confirmation prompt; the prompt text now warns explicitly that all children
will be deleted.

**Partial failure semantics (matches `push_built_model` rollback envelope):**

- Every child DELETE is wrapped individually; sibling failures do **not**
  abort the cascade.
- If ANY child fails, the parent is **preserved** and a `KeboolaApiError`
  is raised with `details.cascade = {attempted, deleted, failures: [{type,
  id, name, error}], parent_deleted: False, model_uuid}`.
- Re-run `kbagent semantic-layer model delete --project P --model <uuid>`
  after fixing the underlying error to finish the cascade.

**Response envelope changes:**

- New top-level `cascade` block on success: `{attempted, deleted: {datasets,
  metrics, relationships, glossary, constraints}, failures: [], parent_deleted}`.
- Legacy `orphaned_children` top-level key kept for back-compat with the
  shape unchanged, but its **meaning** flips from 'leaked count' to
  'cascaded count'. Happy-path JSON consumers always saw zeros on this key
  before — the only way to populate it was the bug.

**Deprecation:** `orphaned_children` is deprecated as of v0.43.4 and
scheduled for **removal in a future minor release** (not before v0.44.0).
Read `cascade.deleted` instead — it carries the same per-type counts plus
the explicit `attempted` / `parent_deleted` / `failures` fields that
disambiguate happy-path from partial-failure responses. JSON callers
should migrate now; the field name is the only thing that changes.

**Implication for AI agents / scripts:** Scripts that called `model delete`
and then assumed they had to teardown children manually can drop that
follow-up. Scripts that scraped `orphaned_children` to detect the bug now
see the same zeros they always wanted — but should switch to
`cascade.deleted` ahead of v0.42.0.

## Web UI `Kai Chat` is gone — replaced by `Local AI` (since v0.41.9)

The web UI dashboard tile / left-nav entry previously labelled **Kai
Chat** has been replaced by **Local AI** (PR #301, follow-up to #291
closed-wontfix and #288 closed-wontfix). The new tile is backed by
`POST /ai/chat/stream`, a third instance of the same stateless-helper
pattern as `POST /agents/prompt/improve/stream` and
`POST /workspaces/sql/improve/stream`. It spawns the user's local
`claude` / `codex` / `gemini` CLI with a meta-prompt grounding it as
a kbagent co-pilot.

**Why the swap:**

- Kai requires a **master** Storage API token. `kbagent org setup`
  generates non-master tokens by default for security reasons, so any
  project registered via that path had its Kai tile broken.
- Kai is per-project; cross-project work (lineage, migration assistant,
  multi-project comparison) was structurally impossible inside Kai.
- The local AI uses any Storage token kbagent already has AND handles
  multi-project flags natively (`--project NAME`).

**What stays:**

- `POST /kai/chat` and the rest of the `/kai/*` backend endpoints
  remain available for HTTP callers that explicitly want Kai's
  per-project session-state API. Only the dashboard UI tile + left
  nav entry was swapped. `kbagent kai ping|preflight|ask|chat`
  CLI commands are unchanged.

**Implication for AI agents:**

- If your script targets the web UI (e.g. screen-scraping or Playwright
  automation), the page id changed from `kai` to `localai` in
  `UIState.page` and the route from `KaiPage` to `LocalAiPage`. The
  endpoint flipped from `POST /kai/chat` (blocking JSON) to
  `POST /ai/chat/stream` (SSE) -- different wire protocol, different
  envelope.

## Dashboard `▶ run` button on scheduled agents uses BLOCKING `/agents/{id}/run`, NOT the SSE stream (since v0.41.9)

The dashboard's Scheduled agents tile gained an inline `▶ run` button
per row (issue #292). It fires `POST /agents/{task_id}/run` -- the
blocking variant -- and invalidates the `['agents']` query cache on
completion so the row's `last_run_at` + status pill refresh inline.

The Agents PAGE (`/agents`) uses a different code path: when its `▶`
button fires, it opens the Run drawer that streams via
`POST /agents/{task_id}/run/stream` (SSE with late-attach support).

**Pick the right endpoint:**

- Need live tool_use / token-cost / `stream-json` events as they
  arrive? Use `/agents/{id}/run/stream`.
- Just need "fire and forget; tell me when it's done; let me move on"?
  Use `/agents/{id}/run`. This is what the dashboard tile uses.

Both endpoints persist the same `AgentRun` record on disk; the blocking
endpoint returns it once the run completes, the SSE endpoint streams
events and emits a final `done` SSE frame mirroring the same record.

## Semantic-layer constraint `rule` is a STRING, not an object (since v0.41.0)

- The `sl-builder` skill docs (in `04_AI_Kit/ai-kit/`) describe range
  constraints with `ruleExpression: {bounds: {min: 0, max: 100}}` --
  that is **WRONG** against the live metastore. Probed 2026-05-14
  against `e2e-1143`: the API rejects the object shape with HTTP 400
  / `"got object, want string"`.
- The correct shape is a single SQL-ish string expression:
  ```json
  {"name": "revenue_non_negative", "constraintType": "inequality",
   "rule": "value >= 0", "metrics": ["revenue"], "severity": "warning"}
  ```
  Other examples: `"value BETWEEN 0 AND 100"` (range),
  `"value IS NOT NULL"` (equality/existence), `"prev_value <= value"`
  (temporal monotonic).
- The `constraintType` enum is a CLOSED list:
  `inequality | equality | range | composition | exclusion | temporal | conditional`.
  It classifies the SHAPE of the constraint -- the actual expression is
  always a string.
- `kbagent semantic-layer add constraint --rule "..."` enforces the
  string contract at the CLI layer; if a user pastes a `{bounds: ...}`
  object the CLI exits 2 / `VALIDATION_ERROR` with a hint pointing at
  this gotcha.

## Constraint name regex `^[a-z][a-z0-9_]*$` AND the 3-vs-4 severity split (since v0.41.0)

- Constraint NAMES must match `^[a-z][a-z0-9_]*$`: lowercase ASCII,
  digits, underscores; must start with a letter. UPPERCASE, hyphens,
  dots, or leading digits get rejected with HTTP 400.
- The 4-band health convention (`<name>_critical / _warning / _healthy
  / _review`) lives in the NAME SUFFIX. That suffix is what shows up
  downstream in `DIM_METRIC_THRESHOLD` joins on `CODE_CONSTRAINT`
  derivations -- it is **not** the same as the API `severity` field.
- The API `severity` field is a SEPARATE closed 3-value enum
  (`error | warning | info`).
- Typical pairing: a `_critical`-suffixed constraint typically carries
  `severity: "error"`; a `_warning`-suffixed one `severity: "warning"`;
  `_healthy` and `_review` typically carry `severity: "info"`. There is
  no automatic mapping in the API -- the operator sets both
  independently. kbagent's `semantic-layer validate` emits a warning
  when the suffix and the severity drift (e.g. a `_critical`-suffixed
  constraint with `severity: "info"`).
- `kbagent semantic-layer add constraint --severity` only accepts the
  3 API values; the 4-band band lives in `--name` suffix.

## Metric rename auto-cascades through `CODE_METRIC` (since v0.41.0)

- `kbagent semantic-layer edit metric --new-name NEW` does DELETE+POST
  on the metric and ALSO DELETE+POST on every constraint whose
  `metrics[]` referenced the old name (POST new with `metrics[]`
  updated to the new name). The metastore has no PATCH endpoint, so
  every "edit" is a delete-then-create.
- The `CODE_METRIC` derived value (used in downstream SQL joins on
  `DIM_METRIC_THRESHOLD` / `FACT_METRIC_*` lookups) is computed via
  ```python
  re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")
  ```
  Renaming a metric from `revenue_growth` to `revenue_growth_qoq`
  changes `CODE_METRIC` from `REVENUE_GROWTH` to `REVENUE_GROWTH_QOQ`.
  Downstream SQL joining on `CODE_METRIC = 'REVENUE_GROWTH'` silently
  drops the row after the rename.
- kbagent `edit metric` ALWAYS prints the old/new CODE_METRIC values
  and the list of affected constraints, and requires Y/N confirm
  unless `--yes` is set. The CODE_METRIC change line is printed even
  with `--yes` -- treat it as a contract change that needs explicit
  audit of downstream SQL.
- On POST failure (e.g. the new name violates a constraint), the
  service re-POSTs `original_attrs` to restore the pre-edit state
  and reports rollback success/failure explicitly in the response
  envelope's `rollback` field. If rollback itself fails, the model
  is left in a partial state -- surface that to the operator and
  recommend running `semantic-layer validate` immediately.
- **Partial-state envelope signal (updated v0.41.10 -- closes #294)**:
  the cascade has per-item rollback only (each constraint DELETE+POST
  rolls back individually), NOT whole-operation atomicity. If the
  metric rename succeeds but M of N dependent constraints fail to
  repoint, the response envelope sets `partial_state: true` and
  `recovery_hint: "<text pointing at validate + manual re-cascade>"`
  at the TOP level (previously the partial-state condition was
  buried inside `cascaded_constraints[i].status == 'failed'`).
  Human-mode CLI prints a bright red `PARTIAL STATE` banner above
  the per-entry list. Atomic two-phase commit was intentionally NOT
  implemented: the metastore has no PATCH endpoint, so every
  cascade 'stage' is itself a DELETE+POST that can fail; true
  atomicity would require side-staging every cascade item, which is
  disproportionate for a rename. Recovery recipe: `kbagent
  semantic-layer validate` to surface the dangling refs, then
  re-run each failed cascade via `edit constraint --new-metrics ...`.

## Removing a metric corrupts `DIM_METRIC_THRESHOLD` downstream (since v0.41.0)

- `kbagent semantic-layer remove metric --name N` runs a pre-deletion
  scan listing every constraint whose `metrics[]` includes N. Each
  such constraint becomes ORPHANED after the delete: it remains in
  the model but references a metric that no longer exists.
- Downstream impact: the typical Keboola semantic-layer pipeline
  pushes constraints into `DIM_METRIC_THRESHOLD` keyed by
  `CODE_METRIC` derived from the metric name. After an orphan, the
  threshold row points at a non-existent metric -- joins on
  `CODE_METRIC` from `FACT_METRIC_VALUES` silently drop the row (or
  crash on strict joins, depending on the pipeline).
- The orphan warning is ALWAYS printed (even with `--yes`) and lists
  the orphaned constraint names plus their `metrics[]` content.
  Non-TTY invocations without `--yes` refuse with exit 2 -- the
  warning is non-suppressible.
- Recommended recovery: either remove the orphaned constraints FIRST
  (so `metrics[]` shrinks to a list of still-existing metrics), or
  use `edit metric --new-name <archived_*>` for a SOFT-DELETE that
  keeps the constraint refs valid (and the CODE_METRIC alive in
  historical comparisons).

## `semantic-layer build` is a HEURISTIC fallback, not full AI (since v0.41.0)

- The kbagent AI Service client (`ai_client.py`) only exposes
  `get_component_detail` and `suggest_components` as of v0.41.0 --
  no arbitrary-JSON endpoint. So `kbagent semantic-layer build`
  falls back to a DETERMINISTIC heuristic builder that synthesises:
  one dataset per `--tables` entry (FQN auto-derived from
  `tableId`; `fields[]` role-classified via the same PK_/FK_/*_DATE/
  *_DT/numeric-amount-name heuristics as `add dataset --deep-fields`),
  one `COUNT(*)` metric per dataset, one glossary entry per table.
  No relationships, no constraints.
- The response envelope carries `fallback_used: "heuristic"` so
  callers can detect the mode. Treat the output as a "best starting
  scaffold" and immediately follow up with `add metric`, `add
  relationship`, `add constraint` for real business logic.
- The push loop walks ALL FIVE child types in dependency order
  (datasets -> metrics -> relationships -> glossary -> constraints).
  This FIXES the long-standing `sl-build` skill bug where
  `semantic-constraint` was silently dropped from the push loop --
  the skill iterated only 4 of the 5 types.
- The full AI-assisted greenfield wizard (schema discovery, SQL
  analysis, LLM-generated metrics with rich business logic and
  paired range constraints) still lives in the `sl-build` skill in
  `04_AI_Kit/ai-kit/`. Bridge to that skill when the heuristic is
  not enough; the two are interoperable via the same metastore
  contract.
- **Field-type normalization (since v0.41.10)**: warehouse-native
  column types from Storage (`VARCHAR(255)`, `NUMBER(38,2)`,
  `STRING`, `TIMESTAMP_NTZ`, ...) are mapped to the metastore's
  closed lowercase set (`string`, `integer`, `decimal`, `boolean`,
  `date`, `datetime`, `json`) before the model is POSTed. Untyped
  Storage columns (empty `basetype`) default to `string`. Before
  this fix `build` 422'd on every legacy untyped table because the
  metastore rejected the raw warehouse types verbatim.
- **Rollback + `--keep-on-failure` (updated v0.41.10 -- closes #295)**:
  the push loop now tracks every successfully-POSTed child in order
  and, on any subsequent POST failure, walks that list in REVERSE
  PUSH_ORDER calling `client.delete_item` per child. If the model
  itself was created during this call (caller did NOT pass `--model`),
  it is DELETEd last. Each cleanup DELETE is wrapped in its own
  try/except so a partial cleanup failure never masks the original
  error. The wrapped `KeboolaApiError` carries `details.rollback =
  {attempted, posted_children, deleted, failed_deletes,
  model_created_here, model_deleted, model_delete_error, model_uuid}`.
  Before v0.41.10, a build failing mid-push left the model + N
  successful children in the metastore; retry returned
  ALREADY_EXISTS and `model delete` refused while children existed,
  forcing per-child manual teardown. **`--keep-on-failure` flag**
  (mirrors `data-app create --keep-on-failure`) preserves the
  partial state for forensic inspection -- the wrapped error then
  carries `details.rollback.attempted=False, reason='keep_on_failure'`
  instead of running cleanup. When caller passes `--model EXISTING`
  the model itself is NEVER deleted on rollback (only the children
  WE POSTed during this call get torn down).

## `kbagent http` works only inside `kbagent serve` subprocesses (since v0.40.0)

- `kbagent http get/post/patch/delete <PATH>` is a thin self-call client
  against the running `kbagent serve`. It requires both
  `KBAGENT_SERVE_URL` and `KBAGENT_SERVE_TOKEN` env vars; without them it
  refuses with exit code 2 -- the command has **no meaningful target**
  outside a serve subprocess context. Do not try to run it from an
  interactive shell unless you exported these env vars yourself.
- **Auto-injection from `kbagent serve`:** when the scheduler dispatches a
  scheduled agent task (action types `cli_command` and `ai_agent`), the
  subprocess env is overlaid with `KBAGENT_CONFIG_DIR`,
  `KBAGENT_SERVE_URL`, and `KBAGENT_SERVE_TOKEN`. This means:
  - An AI agent (claude / codex / gemini) can call `kbagent http get
    /projects` directly -- it talks to the live serve, sees the same
    Keboola tokens the operator configured, never the global
    `~/.config/keboola-agent-cli/` config.
  - Forking another `kbagent <cmd>` CLI also reads the aligned config
    (via `KBAGENT_CONFIG_DIR`) -- no more "expirovaný token" surprises
    where the child process loaded a different `config.json` than the
    parent serve.
- **Manage-token operations still require human interaction.**
  `KBAGENT_CONFIG_DIR` propagation does NOT bypass the `--allow-env-manage-token`
  default-deny (see entry "Manage token: env var is ignored..."). An AI
  subprocess that hits an expired storage token cannot refresh it
  autonomously -- it must surface the issue and ask for human intervention.
- **Browse the OpenAPI to discover endpoints:** `kbagent http get
  /openapi.json` returns the full schema, which lets the AI pick the
  right route + body shape without hard-coded knowledge.

## `config new --push --output-dir` scaffolds carry the created config's ID and land in the right branch subtree (since vNEXT)

- **Before vNEXT this combo was a duplicate factory** (issue #644): the
  scaffold was written WITHOUT `_keboola.config_id` (the "assigned on first
  push" comment was wrong on this path -- the config already existed), and it
  always landed in the DEFAULT branch's tree even when the config was created
  in a dev branch (`--branch`, or the active branch set by `branch create` /
  `branch use`). The next `sync push` then classified the directory as a new
  config and POSTed a duplicate -- a real incident created 34 duplicates.
- **Since vNEXT** the written `_config.yml` records `_keboola.config_id`
  (double-quoted, so legacy numeric IDs stay YAML strings) and the files are
  written into the subtree of the branch the config was ACTUALLY created in;
  an unregistered branch is added to `manifest.branches` exactly like
  `sync pull --branch` would. If that registration fails, files fall back to
  `branch-{id}/` -- never to the default tree -- with a warning in
  `warnings[]`.
- The follow-up `sync diff` reports the new config as `modified` (placeholder
  scaffold vs the pushed empty body) -- that is the expected "now edit and
  push" state, NOT drift. With an explicit `--configuration` body the local
  directory mirrors the pushed (already encrypted) body instead, materialized
  exactly like `sync pull` would (a transformation body's `parameters.blocks`
  become a REAL `transform.sql`/`transform.py`, a non-empty description
  becomes `_description.md`) -- so the diff is clean and a later push cannot
  regress the remote to TODO placeholders.
- `--json` gains an additive `local_scaffold: {directory, files, config_id}`
  key on this path.
- Agents on kbagent < vNEXT: do NOT recommend `--output-dir` + `--push`
  together; use scaffold-only then `sync push`, or `--push --no-files`.

## `kbagent config new --push` is one-shot remote create; default is scaffold-only (since v0.33.0)

- **Pre-v0.33.0**, `kbagent config new` was scaffold-only -- it wrote
  boilerplate files to `--output-dir` (or stdout) and made **zero API calls**.
  The intended flow was scaffold → edit → `kbagent sync push`. The agent docs
  in `keboola-expert.md` and SKILL.md conflated this with "create config"
  intent, which was wrong if the goal was an API mutation.
- **Since v0.33.0**, `--push` adds a one-shot remote create:
  `kbagent config new --component-id C --name N --project P --push` calls
  `POST /v2/storage/components/C/configs` after the scaffold step. Returns
  the new config ID immediately. `--no-files` skips the filesystem step
  entirely (no scaffold to disk or stdout, only the API POST) -- this is
  the FIIA-style "empty shell, then patch via `config update --set ...`"
  pattern.
- `--push` **requires** `--project` AND a non-empty `--name`. All other
  push-gated flags (`--no-files`, `--description`, `--configuration` /
  `--configuration-file`, `--no-validate`, `--branch`, `--dry-run`) are
  no-ops without `--push` and exit 2 if set independently.
- `--configuration` and `--configuration-file` are mutually exclusive;
  `--no-files` and `--output-dir` are mutually exclusive.
- **`config new --push` accepts EVERY component type**, including
  `keboola.snowflake-transformation` -- the typed CLI wraps the raw Storage
  API directly. (The upstream `keboola-mcp-server` `create_config` tool
  refuses that component and routes to `create_sql_transformation`; kbagent
  never inherited that restriction, and `transformation create` is the
  purpose-built path since 0.73.0.)
- **Schema validation** runs by default whenever `--configuration` /
  `--configuration-file` provide an explicit body. On mismatch the create
  aborts with exit 5 and a list of error paths. If the AI Service has no
  schema for the component or returns an error, validation skips silently
  (the result envelope shows `validation_status: "skipped"`). Use
  `--no-validate` to skip the AI Service call entirely.
- **Empty-shell exception**: when no body is provided (default `{}`),
  validation auto-skips. Component schemas almost always require parameters
  and would reject `{}` -- skipping is the FIIA-pattern-friendly default.
  Passing `--configuration '{}'` explicitly does NOT take the skip path:
  the body is treated as caller-provided and validated, which typically
  fails. Use `--no-validate` to suppress validation entirely.
- **`--push --dry-run`** returns the planned POST body + validation result
  without making the API call (`dry_run: true` in the envelope, exit 0 even
  on validation failure -- dry-run is inspection-only).
- The result envelope on success includes the full Storage API response
  plus `project_alias`, `branch_id`, `validation_status`, and
  `validation_errors` (always present, even if empty). Shape-symmetric with
  `config detail` single-config mode and `config row-create`.
- **Validation reads the `parameters` section, not the whole body (since
  v0.84.1).** A component's `configurationSchema` describes the CONTENTS of
  `parameters`, so error paths are reported as `parameters.<field>`. Sibling
  keys (`storage`, `runtime`, `authorization`) are not covered by the schema
  and never fail validation. **Before v0.84.1 the whole configuration object
  was validated against that schema**, which inverted the result: a correct
  body (`{"parameters": {"db": ...}}`) was rejected with the misleading
  `<root>: 'db' is a required property`, while a body missing the
  `parameters` wrapper validated clean. If you are on <= 0.84.0 and hit that
  `<root>:` error on a body you know is correct, the config is fine and the
  validator is wrong -- upgrade rather than reaching for `--no-validate`,
  which switches off the checking that still works.
- **A body with no `parameters` key is validated as an EMPTY `parameters`
  section (since v0.86.0, issue #605).** So a body that forgot the wrapper --
  the component's own fields sitting at the configuration root -- now fails
  with the schema's required-field errors plus a `hint:` line naming the
  missing wrapper. **Before v0.86.0 it validated `ok` and was POSTed
  verbatim**, producing a live configuration with no `parameters` key, which
  the UI and the component runtime both read as empty (blank boilerplate) even
  though `--push` reported success. The whole-body exemption is now keyed on
  the component (`keboola.flow` / `keboola.orchestrator`, whose `phases` /
  `tasks` genuinely ARE the configuration root), not on the body's shape.
  Always POST the full object: `{"storage": ..., "parameters": {...}}`.

## Flow "Notifications tab" recipients are NOT in the flow config (since v0.86.0)

- **Two different notification mechanisms share the word "notification".** A
  task node of `type: "notification"` lives *inside* the flow's
  `configuration` JSON and has always been visible via `flow detail` /
  `config detail`. The Flow Builder's **Notifications tab** (the bell icon --
  Success / Error / Processing-delay / Warning cards) is backed by a separate
  platform service and is **not** in the configuration at all. Auditing "who
  gets paged when this flow breaks" by reading flow configs answers only half
  the question; `kbagent notification list` answers the other half.
- **Event names are kebab-case, and the field is an open string.**
  `job-failed`, `job-succeeded`, `job-succeeded-with-warning`,
  `job-processing-long`, plus the `phase-job-*` variants -- *not* the
  camelCase `jobFailed` spelling. `--event` is deliberately not validated
  against a fixed list, because the API declares `EventName` as
  `type: string` with no enum. A misspelled event therefore returns an empty
  list, not an error -- if a query comes back empty, re-check the spelling
  before concluding nobody is subscribed.
- **The service IGNORES `?event=`; kbagent narrows client-side**
  (verified live, not from the swagger). A filtered request answers `200` with
  the project's FULL subscription list -- the documented query parameter has
  no effect. kbagent still sends it (so a server-side fix would cost nothing)
  and then filters the rows itself, which is why `--event` behaves correctly
  from the CLI. **Anything talking to the service directly must do the same
  narrowing**, or "who gets paged on failure" silently answers with a superset
  that includes success and processing-delay recipients. Same lesson as
  Storage's `list_files`: a documented query parameter is a statement of
  intent, not of behavior -- check what comes back.
- **Filter fields are dotted paths into the event payload.**
  `job.component.id`, `job.configuration.id`, `branch.id`, `phase.id`,
  `durationOvertimePercentage` -- *not* flat keys like `configurationId` or
  `component`. Only the first four have dedicated output columns; everything
  else rides along in the raw `filters` list (`notification detail` prints it
  verbatim).
- **`--component-id` / `--config-id` filter client-side** -- as does
  `--event` (see above). The endpoint has no server-side filter that works at
  all: kbagent always fetches the project's full subscription list and narrows
  it locally.
- **A subscription with NO filters is project-wide and fires for every job.**
  `filters` is optional in the schema (only `event` and `recipient` are
  required), so a catch-all "page me on any failure" subscription is legal and
  common. `--component-id`/`--config-id` exclude those rows -- but they also
  page for the config being audited, so the count of the ones actually
  **dropped** is reported as `project_wide_excluded` (and a warning in human
  mode). Note `scope` keys off the config filter alone, so a subscription
  filtering only on `job.component.id` is labelled `project-wide` even though
  it is not truly global. Two consequences: a matching `--component-id` keeps
  it (kept rows are never counted), and a dropped one that names a *different*
  component is not counted either -- its own explicit filter proves it can
  never fire for what you audited. Only genuinely ambiguous rows -- those
  missing the constraint you filtered on -- are counted.
  **Never answer "who gets paged for this flow" from a filtered run alone**
  -- add the project-wide rows from an unfiltered run.
- **Webhook recipients carry `url`, email recipients carry `address`.** Both
  render in the single `address` column; a `--json` consumer reading the raw
  API would need to handle both keys.
- **`branch_id` is populated on EVERY subscription, production included** --
  a filled Branch column does NOT mean "dev-branch only" (verified live).
  There is no branch query parameter, but the Flow Builder always writes a
  `branch.id` filter, and for a production subscription that value is the
  **default branch's numeric id** (e.g. `390737` for a project whose `Main`
  branch has that id). To tell a production alert from a dev-branch one you
  must compare against `kbagent branch list` for that project -- presence
  alone tells you nothing. Configuration *names* are resolved against the
  project's active branch, so a flow that exists only in production shows a
  blank `config_name` while a dev branch is active.
- **`config_name` is best-effort.** It is resolved by an exact
  `(component_id, config_id)` match, falling back to a config-ID lookup when
  the subscription has no component filter -- but only when unambiguous. Two
  components sharing a config ID, a deleted parent config, or a failed lookup
  all yield `""` rather than a guess. A blank name never means the
  subscription is inactive.
- Read-only in this release: creating and deleting subscriptions is not
  exposed by the CLI.

## `config clone` duplicates a config whole; cross-project cannot carry secrets (since v0.84.2)

- **Use it instead of rebuilding a body.** Reading `config detail` and
  POSTing back `configuration["parameters"]` drops the siblings
  (`runtime`, `storage`, `authorization`) silently. That is issue #587: a
  dropped `runtime.parallelism` made Keboola fall back to `parallelism: 1`
  and a 65-row writer ran sequentially, 140 min instead of ~60-90.
- **Same project (default): server-side copy.** Goes through
  `POST .../configs/{id}/versions/{version}/create` at the source's current
  version. Verified live: **rows come along** (a 2-row source produced a
  2-row clone) and so do `KBC::` encrypted values, which stay decryptable
  because the project is unchanged.
- **`--set PATH=VALUE` is applied AFTER the copy**, as a normal update on the
  new config. So an override can never be the reason a key went missing --
  the copy is complete first, then edited.
- **Cross-project (`--target-project`) REFUSES on encrypted values.** A
  Keboola ciphertext is scoped to the project it was encrypted in; no other
  project can decrypt it. The clone exits 5 and lists every `KBC::` path
  (parent config AND rows, reported as `rows[N].path`) until each is
  re-supplied via `--secret PATH=VALUE`. Those are encrypted in the **target**
  project on write. Run `--dry-run` first: it reports the paths in
  `missing_secrets` instead of refusing, which is how you learn what to gather.
- **Cross-project copies storage mappings VERBATIM.** Bucket and table IDs are
  not remapped, so an `in.c-main.orders` input still says `in.c-main.orders`
  in the target. Check those buckets exist there; `sync clone` is the command
  that actually remaps.
- `--secret` and `--set` split on the FIRST `=` only, so values may contain
  `=` (base64 padding, connection strings).
- **Row secrets are supplied under their `rows[N].` path** and are routed back
  into that row, not the parent body. Paths for secrets inside a list use a
  plain index segment (`parameters.values.0.value`, not `[0]`) so they can be
  passed straight back to `--secret`.
- **Ciphertext under a plain (non-`#`) key is refused outright.** The
  Encryption API round-trip in this CLI keys off `#` names, so a replacement
  supplied there would be written to the target project in PLAINTEXT.
  Encrypt it yourself for the target with `kbagent encrypt values` and pass
  the ciphertext via `--set`.
- **`--set` is encrypted on both paths.** A `--set 'parameters.db.#password=...'`
  goes through the Encryption API before the write, same as `config update`.
- **`--target-branch` is rejected on a same-project clone** when it differs
  from `--branch`: the server-side copy writes into the source's own branch,
  so honouring it is impossible and writing to the wrong branch silently is
  not acceptable.
- **A cross-project clone is not transactional.** If a row fails mid-copy the
  error names the created configuration id and how many rows landed, so you
  can delete it and re-run.

## `config delete` twice = permanent purge -- kbagent now refuses the second one (since v0.89.0)

The Storage API overloads `DELETE .../configs/{id}`: on a live configuration
it soft-deletes into the trash (restorable), but on a configuration ALREADY in
the trash the same call **purges it permanently** -- versions, rows and
metadata gone, no restore. The classic way to hit this is an agent retry: the
first DELETE times out client-side after the server already trashed the
config, the retry fires, and the retry destroys it for good.

- Since 0.89.0 `config delete` locates the configuration first and never
  sends a DELETE at anything that is not live. A config already in the trash
  answers `status: "already_in_trash"` with **exit 0** (the retry stays
  idempotent for scripts) and a pointer to `config restore`. A config in
  neither place is NOT_FOUND.
- `config restore --project P --component-id C --config-id ID` is the undo;
  `config trash-list` shows what is restorable. Restore brings back versions,
  rows and metadata.
- **On kbagent <= 0.88.x the guard does not exist** -- a repeated
  `config delete` there purges permanently. When driving an older kbagent,
  never blind-retry a delete; check `config list` first.
- Direct API callers: the purge-safe alternative is the dedicated
  `POST .../configs/{id}/purge` endpoint (fails with 400 when the config is
  not in the trash), never a second DELETE.
- **The likeliest second DELETE is not a human retry -- it is the HTTP
  client's own.** `DELETE` is conventionally idempotent, so most transports
  (kbagent's included, before 0.89.0) repeat it after a read timeout or a
  5xx. On this endpoint that automatic repeat IS the purge, and it happens
  before any caller sees a result. If you are writing a script or another
  client against the Storage API, disable transport-level retry for a config
  DELETE specifically; idempotency here is a property of the endpoint, not
  of the method.

## `data-app` JSON output: key for the app's own id is `app_id` (since v0.33.0)

- Every `kbagent --json data-app <subcommand>` envelope emits the
  data-app's own identifier under the key `app_id`. Prior to v0.33.0 the
  same key was named bare `id`, which did not match the `--app-id` input
  flag. Affects `data-app list / detail / create / deploy / start / stop /
  delete / password / secrets-set / secrets-list / secrets-get /
  secrets-remove`. The companion `config_id` key is unchanged.
- Pipe-friendly chain that v0.33.0 enables:
  `kbagent --json data-app list | jq -r '.apps[].app_id' | xargs -I{} kbagent data-app deploy --project P --app-id {}`.
  On pre-v0.33.0 you had to read `.apps[].id` (mismatched the input flag,
  surprised AI agents that templated `.app_id`).
- **What is NOT renamed:** the Storage config back-pointer at
  `parameters.id` inside the configuration body sent TO Storage (writeup
  §5) -- that lives in the Storage config, not in kbagent's output
  envelope. The auth-provider id (`auth_providers[].id == "simpleAuth"`)
  is also unchanged.
- The Data Science API on every Keboola stack we've probed (europe-west3.gcp,
  us-east4.gcp; 2026-05-12) serves camelCase keys on the wire (`id`,
  `configId`, `desiredState`, `configVersion`, ...). kbagent reads those
  camelCase keys directly and emits its own snake_case-ish output keys.
  If a future API shape change introduces snake_case wire keys, this
  helper will need a defensive alias pass -- not yet warranted.

## `project edit --new-alias` does NOT rewrite lineage caches (since v0.31.0)

- `kbagent project edit --project OLD --new-alias NEW` cascades the rename
  through `config.json` (`projects` dict key + `default_project` field if it
  matched OLD) and renames the nested-layout sync directory at
  `<cwd>/<old-alias>/.keboola/manifest.json` to `<cwd>/<new-alias>/`.
  Collision handling appends a `-2` numeric suffix (mirrors `config rename`).
- Lineage caches (`*.lineage.json` files produced by `kbagent lineage build
  --output FILE`) embed the alias inside FQN strings (`<alias>:<table_id>`)
  and are **NOT** auto-updated by the rename. The CLI emits a stderr warning
  when it detects a cache file in the workspace.
- After a rename: rebuild any cached `.lineage.json` with
  `kbagent lineage build --output PATH`. Otherwise downstream lineage queries
  silently reference the old alias.
- Why we don't auto-rewrite: lineage caches can live anywhere on disk
  (committed to git, in a sibling repo, used by external tooling). A partial
  rewrite is worse than no rewrite -- callers must opt in by re-running
  `lineage build`.
- Combined invocations are atomic in the obvious order: `--new-alias` is
  applied first, then `--url` / `--token` mutations target the new alias key.
  So `kbagent project edit --project foo --new-alias bar --token NEW` does
  the rename, then writes the new token under `bar`. If `--new-alias` is
  identical to the current alias, it's a no-op (matches "rename to same name"
  idempotency).

## kbagent no longer installs or updates `keboola-mcp-server` (since v0.85.0)

- From v0.30.1 to v0.84.x, `kbagent` startup and `kbagent update` ran a
  two-stage upgrade: kbagent itself AND `keboola-mcp-server`. **Since v0.85.0
  the MCP stage is gone.** `kbagent update` and the startup hook touch kbagent
  only; `kbagent version` reports kbagent only (no `dependencies` key, no
  Dependencies panel); `kbagent doctor` has no `--fix` (it only ever installed
  the MCP server).
- **If you use `keboola-mcp-server` with Claude Desktop / Cursor, keep it fresh
  yourself**: `uv tool install --upgrade --prerelease=allow keboola-mcp-server`.
  The `--prerelease=allow` opt-in is NOT optional (#324): `keboola-mcp-server
  >= 1.55.0` pins a pre-release-only transitive dep (`toon-format~=0.9.0b1`),
  and without it uv backtracks to a stale v1.32.0 and exits 0 -- a silent
  no-op. `--prerelease=if-necessary` is insufficient: a stable `toon-format`
  0.1.0 exists but violates the pin.
- A stale MCP server used to be invisible from kbagent (#243: a user ran six
  minor versions behind for months). It still is -- but kbagent no longer
  claims to manage it, so check the version in your MCP client, not here.
- **kbagent's own self-update is unchanged**: discovery is fully planned before
  anything is mutated, the reinstall is an exact-version full reinstall
  (`uv tool install --force --reinstall` for uv tools), and it re-executes the
  original command immediately afterwards. A failed or timed-out reinstall
  prints a copy-paste recovery command (#528/#530, v0.76.2).

## `storage swap-tables` is branch-scoped and aliases stay put (since v0.28.0)

- `kbagent storage swap-tables --project P --table-id A --target-table-id B
  --branch <ID>` swaps two tables' physical positions
  (`POST /v2/storage/branch/{branch}/tables/{id}/swap`).
- **branch_id is mandatory, but any branch works -- including the
  default/production branch.** The service refuses with exit 5 /
  `ConfigError` *before* any HTTP call only when neither `--branch` nor an
  active branch (via `branch use`) is set. (The earlier "rejected on
  production" claim was wrong -- verified live 2026-06-01: a default-branch
  swap succeeds and is the supported way to retype a prod table.)
- **Aliases are NOT transferred.** They keep pointing at the same
  physical position, so after the swap they expose the OTHER table's
  data. Plan downstream config rewrites if any aliased consumer relies
  on schema, not data.
- **Dev-branch merge does NOT carry storage schema** (only configs), so a
  swap done inside a dev branch never reaches production via merge. The
  dev branch is a *rehearsal* -- profile the typeless table, build a typed
  rebuild (`<name>_change_log`) via CTAS, swap, and run downstream configs
  against it to prove the typed schema is consumer-safe. Then discard the
  branch and run the real build + swap in the production (default) branch.
  Full procedure: `typify-table-workflow.md`.

## `storage clone-table` materializes a prod table into a dev branch (since v0.52.0)

- `kbagent storage clone-table --project P --table-id T --branch <ID>`
  pulls a production table into a dev branch
  (`POST /v2/storage/branch/{branch}/tables/{id}/pull`, operationName
  `devBranchTablePull` -- the same call the platform issues on a branch's
  first write to a prod table).
- **Why it matters on `storage-branches` projects:** a dev branch reads
  production tables transparently (copy-on-write) until the first write.
  A schema mutation in the branch -- `swap-tables`, dropping a column --
  targets a table that is not yet branch-local, so the Storage API fails
  with a misleading `"bucket ... was not found in the project"`. Run
  `clone-table` first to materialize the table branch-local; the swap /
  drop then succeeds. (Verified live 2026-06-01 on project 10539 with
  storage-branches ON: clone -> in-branch swap succeeds; production left
  untouched.)
- **One-way (default -> branch).** There is no "push branch -> default":
  branch storage is never merged back to production (only configurations
  are). The pull is the only API path between the two table stores.
- Branch is mandatory: the service refuses with exit 5 (`ConfigError`)
  before any HTTP call when neither `--branch` nor an active branch (via
  `branch use`) is set.
- Permission class: `write` (creates a branch-local copy; never deletes).

## Dev-branch merge carries only configurations, NOT storage schema (since v0.52.0, verified 2026-06-01)

- When a dev branch is merged to production, Keboola propagates
  **configuration** changes only. Physical storage tables -- their
  schema, column types, and rows -- are **not** merged back. (Confirmed
  by the storage-branches designer and Keboola's public docs:
  help.keboola.com/tutorial/branches/merge-to-production.)
- Consequence for retyping: a `swap-tables` (or `clone-table`) done inside
  a dev branch stays in the branch. To retype a **production** table you
  run the build + `swap-tables` in the production (default) branch itself.
  The dev branch is only a rehearsal that validates the typed schema
  against downstream configs. Full procedure: `typify-table-workflow.md`.
- The only API path between the two table stores is `clone-table` (pull,
  default -> branch). There is no "push branch -> default".

## `storage truncate-table` preserves schema; endpoint is uniformly async-via-job (since v0.32.0)

- `kbagent storage truncate-table --project P --table-id T [--branch ID]
  [--dry-run] [--yes]` calls
  `DELETE /v2/storage/[branch/{id}/]tables/{id}/rows?allowTruncate=1`
  on the Storage API. The `allowTruncate=1` flag is a safety opt-in
  the API requires whenever no row filter is sent -- omitting it
  returns HTTP 400. kbagent always passes it; do the same in any
  direct `KeboolaClient` script.
- **Do NOT pass `async=true` on this endpoint.** Sibling destructive
  endpoints (`delete_table`, `delete_bucket`) require `async=true`,
  but the row-delete endpoint **rejects** it with HTTP 400
  (`"async: This field was not expected."` -- verified live
  2026-05-11 on connection.europe-west3.gcp.keboola.com). The endpoint
  is inherently async on every branch: it always returns HTTP 202
  with a queued storage job (`operationName: tableRowsDelete`) that
  the client polls via `_wait_for_storage_job` -- same machinery as
  `delete_table`, just without the `async=true` query param.
- **Sub-second on production, longer on dev branches.** Same poll
  loop in both cases; only wall-clock latency differs. From the
  caller's perspective the call always blocks until rows_after=0
  is authoritative on return.
- **Idempotent.** Truncating an empty table is a no-op success
  (`rows_before=0`, `rows_after=0`, `failed=[]`). Safe to retry; safe
  to run as a pre-load step that may or may not have data to clear.
- **What survives:** column definitions, types, primary key,
  descriptions, sharing edges, and every downstream config reference
  (aliases, input/output mappings, transformation refs). What does
  not survive: the rows. Pick `truncate-table` whenever the schema
  contract must hold; pick `delete-table` only when retiring the
  table itself.
- **Propagation.** The Storage API removes the rows immediately on
  the warehouse side -- consumers of an aliased / shared bucket see
  zero rows on the next query, no quiesce window. A downstream
  transformation that started reading the table *just before*
  truncate may see partial state mid-job. Plan re-seed steps so the
  truncate completes before any downstream job picks it up.
- **Permission classification.** `storage.truncate-table` is
  `destructive` -- alongside `delete-table`, `delete-column`,
  `delete-bucket`, `swap-tables`. Schema preservation does not
  downgrade the row-data destruction.

## `data-app create --auth public` writes the canonical noneProxyAuthorization shape (since v0.29.0; fixes v0.27.0 silent HTTP 503)

- **What changed.** v0.27.0's `--auth public` wrote NO `authorization`
  key into the Storage config at all. The Keboola app-proxy refused to
  route to the resulting URL (HTTP 503 / "Service Unavailable") and the
  UI's "Authentication Type" selector showed blank. Operators got a
  silently broken app. v0.29.0 fixes this: `--auth public` now writes
  the canonical `noneProxyAuthorization` shape that the kbc-ui exports
  for the "None" UI option.
- **Exact shape written by 0.29.0:**
  ```json
  {
    "app_proxy": {
      "auth_providers": [],
      "auth_rules": [
        {"type": "pathPrefix", "value": "/", "auth_required": false}
      ]
    }
  }
  ```
- **Authoritative source (public):** keboola/job-queue-job-configuration
  `src/JobDefinition/Configuration/Authorization/AppProxyDefinition.php`
  -- when `auth_required=false`, the `auth` field MUST NOT be set. The
  validator rejects shapes that include `auth` alongside
  `auth_required: false`.
- **Corroborating source (private; Keboola org members only):**
  keboola/ui `apps/kbc-ui/src/scripts/modules/data-apps/constants.ts`
  exports this exact shape as the `noneProxyAuthorization` constant for
  the "None" UI option.
- **Live-validated** end-to-end (HTTP 200 on the resulting URL, no
  auth challenge; UI Authentication tab shows "None" pre-selected).
- **Repairing existing v0.27.0 apps stuck at 503**: re-run
  `kbagent data-app create --auth public ...` to mint a new app, OR
  patch the existing config in-place via
  `kbagent config update --component-id keboola.data-apps --config-id ID --set 'authorization=...'`
  with the shape above. The previous URL stays retired in either case
  (the proxy URL is bound to the deployment record, not the config).
- **`--auth password` behaviour unchanged.** Mints a 20-char hex
  simpleAuth password retrievable via `kbagent data-app password`
  (Manage token required) or visible in the UI's Authentication tab.
- **Other auth providers (OIDC / GitHub OAuth / GitLab OAuth /
  JumpCloud / Auth0)** are NOT yet supported by the CLI's `--auth`
  flag. Use the Keboola UI to configure them after `data-app create`.
  Tracked as a follow-up issue.

## `data-app secrets-*` -- per-project KMS, idempotent remove, never decryptable (since v0.29.0)

- **Encryption is per-project KMS.** `kbagent data-app secrets-set` calls
  the project's Encryption API to wrap each plaintext value before
  writing it to Storage. The resulting `KBC::Project*` ciphertext is
  bound to the project's KMS key; the same ciphertext does NOT decrypt
  in another project. Same fail-closed semantic as `data-app create`'s
  `--git-pat-encrypted`: if the Encryption API does not return a
  project-scoped ciphertext, the command aborts with `ENCRYPTION_FAILED`
  and never writes plaintext to Storage. `--allow-plaintext-on-encrypt-failure`
  is bootstrap/debug only; never use in production.
- **Read-modify-write at the service layer, NOT Storage `merge=True`.**
  The Storage API's `merge=True` flag is shallow at the top level only;
  relying on it would clobber sibling keys nested inside
  `parameters.dataApp.secrets`. The CLI GETs the full config, modifies
  the secrets sub-dict in place, and PUTs the unchanged remainder. Every
  untouched sibling key (under `parameters.dataApp.secrets`,
  `parameters.dataApp` -- slug, git block, id back-pointer, `parameters`
  itself, and the top-level `runtime`/`authorization`/`storage`) is
  preserved bit-identical.
- **`data-app list` hides workspace/sandbox deployments (since v0.43.9).**
  The Data Science `GET /apps` collection returns EVERY deployment in the
  project, not just data apps -- interactive Snowflake/BigQuery
  workspaces (`componentId=keboola.sandboxes`, `type=snowflake`/`bigquery`,
  no name, a `*.snowflakecomputing.com` URL) live in the same collection.
  Before 0.43.9 they showed up as phantom unnamed `(snowflake)` rows that
  do NOT appear in the Apps UI. The command now keeps only
  `componentId == keboola.data-apps` (items missing `componentId` are kept
  defensively); the JSON envelope carries `component_id` per app.
- **`secrets-remove` is idempotent.** Removing a key that isn't set is
  exit 0 with `removed: 0`, `not_found: [<derived env-var name>]`. The
  Storage version is not bumped on a no-op. Do NOT script around this
  with a precondition lookup -- the idempotent path is the contract.
- **`secrets-get` NEVER echoes the decrypted plaintext of an ENCRYPTED
  (`#` / `KBC::`) secret.** The Encryption API has no decrypt endpoint;
  the CLI cannot decrypt under any branch. For an encrypted secret the
  command returns metadata only -- key name, derived env-var name,
  ciphertext fingerprint, encryption prefix, `encrypted: true`,
  `value: null`. NOT_FOUND on an absent key never enumerates sibling
  keys. (Plain unencrypted values ARE returned in full -- see next entry.)
- **`secrets-get` / `secrets-remove` accept keys WITHOUT a leading `#`
  (since v0.43.9).** The `parameters.dataApp.secrets` block holds BOTH
  `#`-prefixed encrypted secrets and plain unencrypted env-var config
  values (e.g. `ADMIN_EMAILS`, `SMTP_HOST`), and `secrets-list`
  enumerates both. Before 0.43.9 `get`/`remove` rejected any key without
  `#` (`Invalid secret key ... Keys must start with '#'`), so a listable
  plain key was neither readable nor removable. Now the `#` is optional on
  these two read/remove commands. `secrets-get` on a PLAIN value returns
  the literal value (`encrypted: false`) -- it is already stored in clear
  and visible via `config detail`, so this leaks nothing new. Lookup is
  exact-match (no `#KEY`<->`KEY` fuzzing). `secrets-set` is UNCHANGED: it
  still requires `#` because it encrypts; to add a plain env var use
  `config update`, not `secrets-set`.
- **Runtime env-var translation rule:** strip `#`, replace `-` with `_`,
  uppercase. Documented at https://help.keboola.com/data-apps/python-js/.
  Examples: `#KBC_TOKEN` -> `KBC_TOKEN`, `#my-api-key` -> `MY_API_KEY`,
  `#anthropic-token` -> `ANTHROPIC_TOKEN`.
- **Setting a reserved-name secret is silently shadowed.** The data-app
  runtime auto-injects a documented set of env vars (canon-confirmed
  floor: `KBC_TOKEN`, `KBC_URL`; runtime almost certainly injects more
  -- TODO follow-up to enumerate exhaustively against a running app).
  Setting `--secret '#KBC_TOKEN=foo'` succeeds (exit 0) but the platform
  value silently shadows yours at runtime; the command emits a stderr
  WARN naming each shadowed key and lists them in
  `shadowed_by_runtime[]` of the JSON envelope.
- **Adding/removing a secret bumps the Storage version, but the running
  container keeps the OLD config until `data-app deploy` runs.** Same
  contract as any other `keboola.data-apps` config edit (see the
  `(since v0.27.0)` entry below). The response includes a `next_step`
  field with the exact redeploy command to run; suppress it with
  `--no-hint-next` for scripted callers.

## `data-app validate-repo` -- pre-flight against the Golden Rule, GitHub-only (since v0.29.0)

- `kbagent data-app validate-repo --git-repo URL` walks the repo via the
  GitHub Contents + Trees API and verifies the documented "Golden Rule"
  layout from https://help.keboola.com/data-apps/python-js/ before
  `data-app create`. Each check emits BLOCKING / WARN / OK with a
  citation back to the help anchor that defines the rule. Runs in ≤5
  GitHub API calls regardless of repo size (one trees-recursive + up
  to four contents fetches), so the 60/hour unauthenticated GitHub
  rate limit is no longer the common-case failure mode.
- **`--type` is restricted to `python-js` in 0.29.0.** Streamlit /
  pure-Python / R / Node-only repos have different layouts (Streamlit
  does not require the `keboola-config/` tree, for instance) and need
  per-type canon citations. Tracked as a follow-up.
- **GitHub-only.** GitLab / Bitbucket support is a follow-up. Calling
  with a non-GitHub URL exits 2 / `INVALID_ARGUMENT`.
- **The `setup.sh` rules read CODE, not comments** *(since v0.88.0)*. Both
  `golden-rule.setup-sh-no-pip` and `golden-rule.setup-sh-uv-sync` strip shell
  comments before matching. Before 0.88.0 they grepped the raw file, which broke
  in both directions: a script whose comment read
  `# always uv sync here, never pip install` was rejected as **BLOCKING** --
  penalising exactly the author who documented the rule -- while a script whose
  only mention of `uv sync` was in a comment **satisfied** the uv-sync rule
  despite installing nothing. If you are advising on a repo validated by an
  older kbagent, a BLOCKING `setup-sh-no-pip` may be about a comment; check the
  file before telling anyone to change what the script runs.
- The stripper is deliberately not a shell parser: it tracks single/double
  quotes, so a `#` inside a string survives, but heredocs and `${...#...}`
  expansions are out of scope. A `pip install` hidden inside a heredoc is not
  detected -- the rule is a pre-flight heuristic, not a guarantee.
- Exit 0 on all checks <= WARN; exit 1 on any BLOCKING. `--strict`
  treats WARNs as failures (exit 1) for CI gating.
- **Reading the build / runtime log** is now available via
  `kbagent data-app logs --project ALIAS --app-id ID [--lines N |
  --since ISO8601]` (since v0.43.8). On `DATA_APP_BUILD_FAILED` /
  `DATA_APP_DEPLOY_TIMEOUT`, fetch the container log tail directly
  from the CLI instead of opening the UI's Terminal Log tab. See
  the `data-app logs` section below for the mutex contract, the
  default-500-lines behavior, and the secret-echo risk (apps that
  print credentials to stdout will leak them into the buffer; the
  envelope is reproduced verbatim with no masking).
  Auto-log-dump on deploy failure (calling `data-app logs` from
  inside `data-app create` / `data-app deploy` when the job ends in
  a failure state) is still tracked as a separate follow-up.

## Manage token: env var is ignored without `--allow-env-manage-token` (since v0.29.0)

- `KBC_MANAGE_API_TOKEN` is no longer auto-resolved on the three
  surfaces that consume it (`kbagent org setup`,
  `kbagent project refresh`, `kbagent data-app password`). Default
  behaviour on 0.29.0+ is **default-deny**: the env var is ignored, a
  TTY hidden-input prompt is shown instead. With no TTY (CI / cron /
  systemd / `< /dev/null`) the resolver exits **2** with the message
  `Error: No manage token available. Run interactively, or pass
  --allow-env-manage-token to read KBC_MANAGE_API_TOKEN from env.`
- To opt in for CI/CD, pass the top-level flag:
  `kbagent --allow-env-manage-token --json org setup ...`. The flag
  belongs in front of the subcommand (it is a top-level option, mirroring
  `--deny-writes`). The flag is session-only -- not persisted, no
  env-var equivalent (intentional; an env-var equivalent would re-create
  the AI-exfiltration hole this default-deny is closing).
- When the env var IS set but the flag IS NOT, you will see a one-shot
  stderr warning `Warning: KBC_MANAGE_API_TOKEN found in environment
  but ignored. Pass --allow-env-manage-token to opt in.`. This is
  informational; the resolver still falls through to the TTY prompt
  (or exits 2 if no TTY). Do NOT suppress this warning by piping stderr
  away -- it tells CI maintainers exactly what to fix.
- The default-deny exists to close the AI-exfiltration risk: any
  subprocess running as the same user (including the AI agent itself)
  inherits env vars, so a manage token in env is reachable by anyone
  who can read `os.environ` or shell out raw `curl`. Default-deny means
  human admin work uses TTY (no env exposure) and CI must explicitly
  say "yes I trust this env" via the flag.
- Storage tokens are unaffected: `KBC_TOKEN` (storage API) keeps
  resolving from env as before.

## `data-app deploy` is required after `config update` -- the running container does NOT auto-pick-up new config versions (since v0.27.0)

- `kbagent config update --component-id keboola.data-apps ...` bumps the
  Storage config version; the deployed container keeps running at the
  OLD version. The Data Science deployment record's `configVersion`
  field is a *pinned pointer* that does not auto-advance when Storage
  advances.
- To roll out the new config, run `kbagent data-app deploy --project P
  --app-id N` (optionally with `--wait`). The CLI reads the latest
  Storage version and `PATCH`es the deployment with the §9 trio
  `{desiredState=running, configVersion, restartIfRunning=true}`.
- **Do NOT** call `PATCH /apps/{id} {desiredState:running}` directly --
  the API silently pins to whatever `configVersion` the deployment
  already had (often the empty shell from `POST /apps`), and the runner
  errors `dataApp.git.repository is required in /data/config.json` with
  no top-level error surfaced. The CLI's `data-app deploy` always sends
  the trio together; sending only `configVersion` returns HTTP 422.
- Same goes for `kbagent data-app start`: it WAKES an auto-suspended app
  at the currently-pinned version. It does NOT roll out new code or
  config -- use `data-app deploy` for that.

## Cross-project KMS ciphertext does NOT decrypt; re-encrypt per project (since v0.27.0)

- The Encryption API's `KBC::Project*` ciphertext is bound to the
  **target project's KMS key**. A `#password` encrypted in project A
  will not decrypt in project B; the Storage API accepts the value but
  the runner fails the `git clone` with "Invalid cipher text for key
  #password" at deploy time.
- `kbagent data-app create` always re-encrypts the plaintext PAT under
  the target project's KMS via the project's Encryption API. Pass the
  PAT via `--git-pat-env VAR` (recommended; no argv leak) or
  `--git-pat-file PATH`. Pre-encrypted ciphertext (`--git-pat-encrypted
  KBC::Project...`) is accepted only when it was encrypted under the
  same project's KMS -- the service refuses to write plaintext if the
  encryption round-trip does not return a project-scoped ciphertext.
- Practical implication: you cannot copy-paste a `KBC::Project*` value
  from one project's `keboola.data-apps` config into another's.

## Transient `state == stopped` during initial data-app deploy is not a failure (since v0.27.0)

- After `data-app create` (or any `data-app deploy --wait`), polling
  may observe `state == stopped` once for ~5-15s before the container
  reaches `running`. This is normal: the platform transitions
  `created → stopped → starting → running` while spinning up the
  runtime. A naive poll that exits on `stopped` would falsely report
  a failure.
- The CLI's `--wait` flag refuses to treat `stopped` as terminal while
  `desiredState == running`. Only `state == running` (success) and
  `state == error` (build failure) and `--timeout` exhaustion are
  terminal in that mode.
- A LATER `state == stopped` (after the app has been running a while)
  is a different beast: it means the platform auto-suspended the
  container after `autoSuspendAfterSeconds` of inactivity. Hit the URL
  to wake it (auto-restart triggers a 30-60s cold boot) or run
  `kbagent data-app start --app-id N`.
## `project invite` "already invited / already member" returns HTTP 400, not 422 (since v0.29.0)

- Re-inviting a user the project already knows about returns HTTP **400** with
  one of two error strings:
  - `"This user has already been invited to this project."` (pending invitation)
  - `"This user is already a member of this project."` (active member)
- `MemberService.invite()` translates both cases to `status="noop"` with
  `note="already_invited"` / `"already_member"` -- they are *not* exit-1
  failures. Bulk runs (`--from-csv`) count them as `noop` in the summary, not
  `failed`.
- The 422 heuristic in pre-v0.29.0 orchestrator scripts (`invite_participants.py:25`)
  is **wrong** for this API. If you write a parallel implementation, key off
  status_code 400 + the substring marker, not 422.

## `project member-set-role` is PATCH, not PUT (since v0.29.0)

- The Manage API role-change endpoint is `PATCH /manage/projects/{id}/users/{userId}`
  with body `{"role": "..."}`. **PUT returns 404** ("resource not found") even
  on a real, currently-active member -- the endpoint shape is PATCH-only.
- The kbagent `ManageClient.update_project_member_role` method emits PATCH;
  any custom code re-implementing the call must do the same.

## `project invite --from-csv` order is not deterministic (since v0.29.0)

- Bulk invitation parallelises via `ThreadPoolExecutor` (default 8 workers).
  The `rows[]` array in the result is in completion order, not CSV order.
- Per-row parsing of `failed_rows` should match by `email`, not by index.
- A failed row never aborts the run -- the executor accumulates results and
  the command exits 0 with `failed > 0` reflected in the JSON summary. Mirror
  the `org setup` partial-success exit semantics.

## `default_bucket` is per-config and only an output prefix (since 0.26.0)

- `kbagent config set-default-bucket` writes
  `configuration.storage.output.default_bucket`. The Storage API uses this
  value as the bucket for any output table whose `destination` is unset.
  Tables that pin `destination: in.c-...` ignore it.
- The setting lives on the configuration, not the project; configs that
  share a destination bucket each need their own value.
- "Clear" leaves an empty `storage.output: {}` if no other keys live there
  -- intentional, mirrors how `set` creates intermediate parents. Storage
  API treats both `output: {}` and a missing `output` as "use the default
  derived bucket name".
- This is the same setting the support article describes as "raw mode":
  https://keboola.atlassian.net/wiki/spaces/SUP/pages/3770155030/.
- UI exposure is tracked under epic
  [KBCP-108](https://keboola.atlassian.net/browse/KBCP-108).
- Validated against three component types (`kds-team.ex-google-cloud-storage`,
  `keboola.ex-cnb-exchange-rates`, `ex-generic-v2`) -- the runner honors the
  setting at job time regardless of whether the component is row-based or
  what `parameters.config.outputBucket` (Generic Extractor's component-internal
  bucket key) says. The Storage `default_bucket` always wins for tables that
  don't pin their own `destination`.

## `config detail` has a bulk mode (since 0.23.0)

- **Omit `--config-id`** to get every configuration under `--component-id`
  as `{"configs": [...], "errors": [...]}`. Each row is tagged with
  `project_alias` and `branch_id`. Shape is identical to `config list`,
  `storage tables`, etc. Use this instead of forking 100 parallel
  `config detail` subprocesses -- one request per project, not per config.
- **Single-config shape unchanged.** Passing `--config-id` returns the
  original flat dict (`.id`, `.name`, `.configuration`, `.rows`, ...) --
  callers that already parse this shape are unaffected.
- **`--config-id` + multiple `--project` is rejected** (exit 2 /
  `INVALID_ARGUMENT`). A single config lives in exactly one project; the
  CLI refuses to guess. Drop `--config-id` for multi-project fan-out.
- **`--branch` requires exactly one `--project`** in both modes (branch
  IDs are per-project; bulk across branches would mix meanings).
- Bulk mode uses the same `list_components_with_configs` call `config search`
  already uses (include=configuration,rows) -- filtering to `--component-id`
  happens in memory. `config list` returns every component's summary;
  `config detail --component-id X` returns every configuration body of
  component X. Different use cases, same underlying endpoint.

## `config list --include-rows` payload size warning (since 0.23.0)

- The default `config list` response is summary-level: just name,
  description, component, last_modified, folder per config. Cheap and fast.
- `--include-rows` switches the service to
  `list_components_with_configs(include=configuration,rows)` so each row
  carries the full `configuration` dict and its `rows` list. Payload
  grows proportionally to configuration complexity -- a project with
  heavy Snowflake writers can easily return 5-10x more bytes. Use only
  when you actually need the bodies (bulk audit dashboards, scripted
  review across many projects). For just finding strings, prefer
  `config search` -- same endpoint, tighter response.

## `config detail --with-state` runtime-state fetch (since 0.23.0)

- The `state` dict on a configuration is mutable runtime data components
  persist between jobs (last sync cursors, auth refresh tokens, OAuth
  intermediate state). It is **not** part of the summary; you have to
  opt in with `--with-state`.
- **Single mode:** adds one dedicated call to `get_config_state`
  (`GET /v2/storage/components/{cid}/configs/{id}` -- the dedicated
  `.../state` resource is not implemented by Storage API; the state
  field rides on the detail response, which is what `get_config_state`
  reads). The returned `state` key always holds the latest snapshot.
- **Bulk mode:** does NOT fan out one HTTP call per config. Instead it
  adds `include=state` to the single `list_components_with_configs`
  call, so a project with 100 configs returns 100 states in one
  request. No N+1. Parallelism bound by `BaseService._run_parallel`'s
  thread pool (default max_parallel_workers = 10; overridable via
  `KBAGENT_MAX_PARALLEL_WORKERS` or `config.json`).
- Most configs return `state: {}` -- this is normal (the component has
  never written state yet, or state was cleared). Treat `{}` as
  "no state", not an error.

## Variables: attach, don't manage (since 0.21.0)

- `keboola.variables` is an implementation detail. Use
  `kbagent config variables-set/get/clear` -- you never need to create,
  list, or link variables configs manually.
- First `variables-set` auto-creates a sibling `keboola.variables` config
  named `<parent-name>-vars` and links the parent. Subsequent sets update
  the same default row.
- **`variables-clear` does NOT delete the backing variables config** -- it
  may be shared across multiple configs. To actually remove it, run
  `kbagent config delete --component-id keboola.variables --config-id <id>`
  after verifying nothing else references it.
- `--var #KEY=plain` -> encrypted via Encryption API before reaching Storage.
  Fail-closed: encryption failure aborts with `ENCRYPTION_FAILED`. Use
  `--allow-plaintext-on-encrypt-failure` only for bootstrap/debug.
- `--replace` drops any existing keys not in the current `--var` set.
  Default is merge.
- Full workflow + response shapes: see
  [variables-workflow.md](variables-workflow.md).

## `job run` auto-resolves variable values (since 0.21.0)

Transformations with linked `keboola.variables` used to run against empty
strings unless the caller hand-wired a `variableValuesId` at the HTTP
layer. `kbagent job run` now auto-resolves it: reads
`configuration.variables_id` from the parent config (root of the
configuration body -- same key `VariablesService` writes), picks
`configuration.variables_values_id` if set, else the first row of the
linked variables config.

- **Override knobs**: `--variable-values-id ROW_ID` pins a specific row
  (CI runs, what-if analysis); `--no-variables` skips resolution
  entirely. Mutually exclusive -- passing both returns exit 2 /
  `INVALID_ARGUMENT` before any API call.
- **`NO_VARIABLE_ROWS`** -- the linked `keboola.variables` config exists
  but has zero rows. Fix:
  `kbagent config variables-set --project X --component-id C --config-id I --var KEY=VALUE`.
- **`MALFORMED_VARIABLES_ROW`** -- Storage API returned a first row
  without a usable `id`. Fails loud rather than silently submitting with
  empty bindings.
- **Empty `--variable-values-id ""`** (or whitespace) rejected at CLI
  layer with `INVALID_ARGUMENT` -- same silent-omission class as the
  above, caught at a different layer.
- JSON response carries `resolvedVariableValuesId` when the resolver
  fired, so callers verify the binding without a second `job detail`
  round-trip.

## Sync: row deploy & manifest v3 (since 0.21.0)

- `sync push` **does** deploy config rows now (previously silently skipped).
  Row changes in the `pushed_details` array carry `"is_row": true` and
  `"parent_config_id": "..."` so you can distinguish them from parent config ops.
- For `keboola.variables` and `keboola.shared-code` rows, the row's
  `configuration` keys are **hoisted** to the top level of the local YAML
  (`values:`, `code_content:`, etc.) -- NOT wrapped under
  `_configuration_extra`. Edit them directly at the top level.
- `.keboola/manifest.json` auto-upgrades from v2 to v3 on the next successful
  pull or push. v3 adds `rows[].metadata` with per-row pull hashes. v2
  manifests still load cleanly; a downgrade to an older kbagent still reads
  the file via `extra="allow"`.
- Encryption failure on a row push raises `ENCRYPTION_FAILED` from the
  service. If it escapes the per-change handler it maps to CLI exit 1
  (general); if caught per-change it lands in `result["errors"][]` with the
  same code. Fail-closed either way. Use
  `--allow-plaintext-on-encrypt-failure` ONLY for debugging.
- **`keboola.variables` row secrets live in `{name, value}` list
  elements**, not dict keys. An early version of the encryption walker
  only scanned `#`-prefixed dict keys and silently shipped plaintext for
  `values: [{name: '#x', value: '...'}]`; fixed before 0.21.0 shipped
  via `_is_secret_name_value_pair`. (`keboola.shared-code` rows carry
  `code_content: [string]` and have no secrets, so the walker correctly
  never fires there.) If you add a new row-hoist component with yet
  another secret shape, extend the walker -- don't patch callers.
- Row-level deployment internals (manifest v3 hashes, 3-way diff, untracked
  row detection, `ROW_HOIST_COMPONENTS`): see
  [`sync-rows-workflow.md`](sync-rows-workflow.md).

## Response structure varies by command

Not all commands return data the same way. Key differences:

| Command | `data` contains |
|---------|----------------|
| `project list` | A **list** directly (not `data.projects`) |
| `config list` | `{"configs": [...]}` |
| `job list` | `{"jobs": [...]}` |
| `lineage show` | `{"lineage_links": [...], "errors": [...]}` |
| `workspace list` | `{"workspaces": [...], "errors": [...]}` |
| `branch list` | `{"branches": [...]}` |
| `config search` | `{"matches": [...], "errors": [...], "stats": {...}}` |
| `storage table-detail` | `{"table_id": ..., "columns": [...], "column_details": [...]}` |
| `storage download-table` | `{"table_id": ..., "output_path": ..., "file_size_bytes": N}` |
| `job terminate` | `{"killed": [...], "already_finished": [...], "not_found": [...], "failed": [...]}` -- four-way partition, NOT a simple success/failure. Always inspect each bucket |

Always check the actual response structure rather than assuming a pattern.

## Multi-project error accumulation

Commands that query multiple projects collect errors per-project without stopping.
One project failing does not block others. Check the `errors` array:

```json
{
  "status": "ok",
  "data": {
    "configs": [...],
    "errors": [
      {"project_alias": "broken-proj", "error_code": "AUTH_ERROR", "message": "..."}
    ]
  }
}
```

**`error_code` survives the catch-all handler -- branch on it, never on the
message (since v0.80.0).** In `data-app list`, `flow list` and the other
multi-project readers, a per-project failure that already carries a code keeps
it instead of being relabelled `UNEXPECTED_ERROR`. So a browser-login
(session) project hitting an unsupported surface reports
`error_code: "AUTH_NOT_SUPPORTED_ON_STACK"` for that one project while the
others succeed, which is the hook for auto-remediating (register a static-token
alias) without parsing prose. Only an exception carrying no code at all falls
back, and then its message is deliberately truncated because its content is
unknown -- do not try to parse a fallback message.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error |
| 2 | Usage error (invalid arguments) |
| 3 | Authentication error (invalid or expired token) -- includes `SESSION_EXPIRED` / `SESSION_NOT_FOUND` / `AUTH_FLOW_DENIED`, whose remedy is `kbagent auth login` (since v0.80.0), and `AUTH_MFA_INVALID` -- `auth login-password` cannot resolve this account's MFA type (WebAuthn/passkey-only), whose remedy is `kbagent auth login` instead (since v0.84.0) |
| 4 | Network error (timeout, unreachable) -- includes `QUEUE_JOB_TIMEOUT` (local gave up AND the remote-kill attempt failed; the remote job may still be running), `AUTH_FLOW_TIMEOUT`, and a session refresh that timed out or could not reach the auth service (`TIMEOUT` / `CONNECTION_ERROR`; a slow auth service is NOT a dead login -- re-run, do not re-login) (since v0.80.0) |
| 5 | Configuration error (corrupt config, missing alias) |
| 6 | Permission denied (blocked by firewall / `--deny-writes` / `--deny-destructive`) |
| 7 | `JOB_TIMEOUT_TERMINATED` -- `job run --timeout` elapsed AND the remote job was successfully cancelled (since 0.22.0). Scripts can distinguish "we killed it" from "it failed on its own" (exit 1) from "it's still running" (exit 4). |

## `job run --wait` polling + log tail (since 0.22.0)

- Polling follows an exponential curve by default: **2s x 30 -> 5s x 48 -> 15s forever**. For a short test job or a test that needs fast turnaround, pass `--poll-strategy fixed` to force the legacy 1s fixed interval.
- On terminal non-success (`error` / `warning` / `terminated`), kbagent fetches the last N Storage Events and attaches them as `logTail` on the response. Controlled by `--log-tail-lines N` (default 200, max 5000, `0` disables).
  - **Errors:** `error.details.logTail` carries the tail when the job surfaces as an exception (exit 1 / `QUEUE_JOB_FAILED`, exit 4 / `QUEUE_JOB_TIMEOUT`).
  - **Non-error terminals** (`warning` / `terminated`): `logTail` is attached to the top-level result dict (exit 0).
- `--timeout N` is a **local** deadline. When it elapses, kbagent issues `POST /jobs/{id}/kill` against the Queue API. Two outcomes:
  - Kill succeeded -> exit **7** with `details.job` + `details.logTail`. The remote is definitely cancelled.
  - Kill failed -> exit **4** with `details.logTail`, `retryable=True`. The remote **may still be running**; investigate before retrying.
- Inspecting events outside of `job run`: `kbagent job detail --project X --job-id N` does not fetch the log tail. To get the raw event stream, call the Storage Events API directly (`GET /v2/storage/events?runId=<runId>`) with the project token.

## `--deny-writes` / `--deny-destructive` firewall (since 0.22.0)

- Session-only. Flags synthesize a `PermissionPolicy` for the current invocation and merge it with any persisted policy in `config.json`. **Never** written to disk.
- Classes: `--deny-writes` appends `cli:write` (covers write+destructive+admin). `--deny-destructive` is narrower -- it appends only `cli:destructive`; pure write ops like `storage create-bucket` stay allowed. (Both flags also appended a matching `tool:*` pattern until v0.85.0; those patterns are inert now that the MCP passthrough is gone.)
- Blocked operation exits **6** with `error.code = PERMISSION_DENIED`. Read commands stay unaffected.
- Safe to run under either flag without mutating the saved policy -- useful when your agent needs a one-shot read-only run on a machine with a write-enabled config.
- `permissions check OPERATION` reflects the EFFECTIVE policy (persisted policy MERGED with session flags) **(since v0.30.5)**. Pre-0.30.5 it consulted only the persisted policy, so an agent doing self-introspection (`kbagent --deny-writes permissions check branch.create`) got `allowed: true` despite the session flag denying that op at execution time. If your agent uses `permissions check` to gate destructive actions and may run against pre-0.30.5 installs, also re-check at execution-time exit codes (6 = denied) rather than trusting the dry probe alone.

## `storage create-table` native types + dev-branch materialize (since 0.25.0)

- **Native types pass through to the Storage API.** `--column pk:VARCHAR(40)`,
  `--column amount:NUMERIC(18,2)`, `--column ts:TIMESTAMP_TZ`,
  `--column meta:VARIANT` all work -- the CLI does only syntactic validation
  (valid identifier, digits+commas length) and Keboola validates
  type/length semantics per backend. Any backend-specific native type
  (Snowflake, BigQuery, Redshift, Synapse) is accepted. The earlier
  whitelist (`STRING, INTEGER, NUMERIC, FLOAT, BOOLEAN, DATE, TIMESTAMP`)
  was removed in 0.25.0.
- **The Storage API derives `basetype`** (`VARCHAR`→`STRING`, `NUMBER`→
  `NUMERIC`, `TIMESTAMP_TZ`→`TIMESTAMP`, `VARIANT`→`STRING`). Do NOT pass
  `basetype` manually; the API will override or reject it.
- **`INTEGER(10)` is invalid.** Keboola's `INTEGER` base type rejects
  length (`'10' is not valid length for INTEGER`). Use
  `--column age:NUMBER(3,0)` instead for narrow integers.
- **`BOOLEAN` defaults must be lowercase.** `--default flag=false`
  succeeds; `--default flag=FALSE` fails with
  `storage.tables.definitionValidation`. The value is normalised to
  uppercase in storage but the input requires lowercase.
- **`--not-null` / `--default` must reference a defined column.**
  Typos exit 2 (`INVALID_ARGUMENT`) before any API call.
- **Dev-branch `create-table` auto-materializes the bucket** when the
  target bucket has not been written to in the branch yet (mirrors the
  official Keboola Go CLI's `EnsureBucketExists`). Response includes
  `auto_created_bucket: true` when this happens. Production writes
  (no `--branch`) never materialize anything.
- **Auto-materialized buckets get `KBC.createdBy.branch.id` stamped**
  (since 0.25.1). On projects with **branched storage** feature flag ON,
  the transformation runner's `output-mapping` rejects buckets without
  this system metadata with `bucket is not assigned to any development
  branch.` kbagent stamps it automatically; the metadata write is
  best-effort (a 403/5xx is logged, create-table still proceeds). If
  another tool created the bucket via raw `POST /v2/storage/branch/<id>/buckets`
  and bypassed kbagent, the bucket will be missing the stamp -- you can
  re-stamp it manually with the Storage API metadata endpoint
  (`POST .../buckets/<id>/metadata` with `provider=system`, key
  `KBC.createdBy.branch.id`, value = branch ID). See
  [storage-types-workflow.md#branched-storage-metadata-stamp-since-0251](../references/storage-types-workflow.md). Closes #224.
- **`storage buckets --branch ID` returns only locally-modified buckets**
  in the branch -- a fresh dev branch lists nothing. That is Storage API
  behaviour, not a CLI bug. Use `storage buckets` (no `--branch`) to see
  production buckets that the branch can read-through.

See [storage-types-workflow.md](storage-types-workflow.md) for the full
type inventory and examples.

## `storage table-detail` is the only way to READ a BigQuery partition layout (since v0.88.0, #621)

- **`create-table` output does not prove anything.** Its `--json` echoes the
  `time_partitioning` / `range_partitioning` / `clustering` you *requested*, not
  what the server registered, and its `--if-not-exists` skip path sets all three
  to `null` on purpose (the existing table's layout is never re-derived). To
  confirm a repartition landed, read it back:
  `kbagent --json storage table-detail --project P --table-id T` ->
  `.definition.timePartitioning` / `.definition.clustering`.
- **Before 0.88.0 the field was dropped entirely.** The service assembled its
  response from a field allowlist that omitted `definition`, so on 0.87.0 and
  earlier the layout is unreachable from kbagent -- verifying meant raw Storage
  API calls or BigQuery metadata access. Version-gate accordingly.
- **`definition` is present on EVERY table-detail response**, untyped and
  Snowflake tables included (connection builds one either way). `null` therefore
  means the stack omitted the key -- it never means "this table is untyped".
  Test for the layout keys, not for `definition`.
- **`.definition.partitions` is unbounded.** It is one entry per physical
  partition, straight from `INFORMATION_SCHEMA.PARTITIONS` -- a DAY-partitioned
  table with a few years of history returns thousands. `--json` carries the whole
  list; human mode prints only the count. Do not pipe the raw JSON of a large
  partitioned table into a context window unfiltered.
- **`storage tables` (the LIST endpoint) still has no layout.** The Storage API's
  `include=` on the list route accepts no `definition` value, so reading the
  layout costs one `table-detail` request per table.

## `storage create-table --source-table-id` + partition/clustering are BigQuery-only (since 0.66.0)

- **`--source-table-id` copies an existing table instead of building from `--column`.**
  The new table's schema is derived from the source and its rows are copied into the
  requested partition/clustering layout (`INSERT … SELECT`, preserving NOT NULL + primary
  key). This is the supported way to repartition a populated BigQuery table; promote it
  with `storage swap-tables`. Mirrors keboola/connection#7697.
- **`--column` and `--source-table-id` are mutually exclusive.** Supplying both, or
  neither, exits 2 (`INVALID_ARGUMENT`) before any API call. `--not-null` / `--default`
  attach to `--column` definitions, so they are also rejected in source mode.
- **Partition/clustering flags also work on a plain `--column` create** (BigQuery only):
  `--time-partitioning-type` (DAY/HOUR/MONTH/YEAR; required when any `--time-partitioning-*`
  is set) + optional `--time-partitioning-field`/`-expiration-ms`; OR
  `--range-partitioning-field`/`-start`/`-end`/`-interval` (all four required together).
  **Range bounds are strings** in the API, and time vs range partitioning are mutually
  exclusive (BigQuery allows one partitioning kind per table).
- **BigQuery-only with a pre-flight guard.** When any source/partition/clustering flag is
  used, `create-table` verifies the project backend (one token-verify call) and fails fast
  with exit 2 + a clear `… require a BigQuery backend` message on a non-BigQuery project,
  before issuing the create. A plain `--column` create makes no extra call. The connection
  API also rejects these server-side (422 `storage.tables.backendDoesNotSupportSourceTable`,
  `sourceAliasNotPersisted`, `sourceTableMissingReferencedColumn`; 404
  `sourceTableNotFound`) as a backstop.
- **Aliases and linked-bucket tables are valid sources.** A persisted alias (materialized
  view) is queryable; a non-persisted alias (project lacks `bigquery-persisted-alias-views`)
  is rejected 422.

## Legacy fake-branch storage warning on `--branch` writes (since 0.25.2)

- **What it is.** Projects without the `storage-branches` feature flag use
  Keboola's legacy fake-branch storage. Writes via `kbagent storage
  create-bucket --branch X` and `storage create-table --branch X` succeed
  at the Storage API level, but the **transformation runner ignores those
  buckets** -- at job time it rewrites `out.c-foo.tbl` to
  `out.c-<X>-foo.tbl` and creates a parallel bucket in the **default
  branch** with the literal branch ID embedded in the bucket name.
- **What kbagent does.** Both write paths consult `verify_token().features`
  once per session (cached on the client) and surface
  `legacy_branch_storage: true` in the JSON response on fake-branch projects.
  Human mode prints a Rich `[yellow]Warning:[/yellow]` line below the success
  message. The behavior of the API call itself is unchanged -- the bucket is
  still created and metadata still stamped (best-effort) -- only the warning
  is new. On `storage-branches`=ON projects the field is `false` and no
  warning is printed.
- **Why it matters for AI agents.** A `kbagent storage create-table --branch
  X` followed by a transformation that targets the same bucket on a
  fake-branch project will see two buckets after the job runs: the one
  kbagent materialized (orphaned, reachable only from `--branch X` view) and
  the one the runner created (`out.c-<X>-...`, in default branch). When the
  user is debugging "why isn't my data here?" the answer is: it's in the
  runner-created bucket, not the kbagent-materialized one. Read the
  warning and surface it to the user.
- **Detection in your own scripts.** Inspect `data.legacy_branch_storage` on
  the JSON response, or call `verify_token().features` directly and check
  for `"storage-branches"`. Both `create-bucket --branch` and `create-table
  --branch` paths surface the flag identically.
- **Migration path.** The right long-term fix is for Keboola Storage to
  finish migrating fake-branch projects to `storage-branches`. Until then,
  the warning is the cleanest signal kbagent can give without changing the
  user-facing command surface. See `storage-types-workflow.md` for the full
  fake-branch vs storage-branches mechanics.

## `sync init --adopt-existing` (since 0.22.0)

- Adopts a `.keboola/manifest.json` written by the kbc Go CLI **in place** instead of overwriting. Idempotent; re-running is a no-op.
- Validates `project_id` from the manifest against the token via `verify_token`. Mismatch exits 5 (`CONFIG_ERROR`) with guidance -- never silently adopts someone else's checkout.
- If no manifest exists, `--adopt-existing` falls through to the normal init path (no error).

## Token handling

- Tokens are always masked in output (e.g. `901-...pt0k`) -- this is normal
- Token can be passed via `--token`, `KBC_TOKEN` env var, or interactive prompt
- Manage API token (since v0.29.0): default-deny on env -- via interactive hidden prompt; pass top-level `--allow-env-manage-token` to opt in to `KBC_MANAGE_API_TOKEN`. Never as CLI argument. See the `(since v0.29.0)` entry at the top of this file.
- Master token for sharing: `KBC_MASTER_TOKEN_{ALIAS}` (e.g. `KBC_MASTER_TOKEN_PROD`) or `KBC_MASTER_TOKEN` as global fallback. Alias is uppercased, hyphens become underscores. Required for `sharing share` and `sharing unshare`; `sharing list/link/unlink` use regular project tokens.

## Branch scope on reads

- **Config commands follow the active branch**: `config list`, `config detail`,
  `config search` scope to the active dev branch (and take an explicit
  `--branch ID`).
- **Storage read commands are the exception**: `storage buckets`, `storage
  bucket-detail`, `storage tables`, `storage table-detail`, and `storage files`
  **ignore the implicit active dev branch** and query production by default.
  The Storage API branch-scoped endpoint only returns resources locally
  modified in the dev branch (empty for a fresh branch), so auto-scoping would
  surprise users with "No tables found". Explicit `--branch ID` still works.
  Storage **write** commands (create-*, upload-*, delete-*, file-*) stay
  branch-aware.

## Conversation ID

Set `KBAGENT_CONVERSATION_ID` env var before running kbagent commands. All API
requests include it as `X-Conversation-ID` header for platform observability.
If unset, the header is omitted.

## Config resolution order

kbagent looks for configuration in this order:
1. `--config-dir` flag
2. `KBAGENT_CONFIG_DIR` env var
3. `.kbagent/` in current or parent directories (local workspace)
4. `~/.config/keboola-agent-cli/` (global)

Use `kbagent init` to create a local `.kbagent/` workspace for per-directory isolation.

## `init --project` filters the copy; it does NOT select an existing project (since v0.59.0)

`--project ALIAS` means something different on `init` than on every other
command. Everywhere else `--project` *selects an existing* project to act on;
on `init` it names which project(s) to **copy out of the global config** into
the new local workspace. Non-obvious rules:

- **It is repeatable and implies `--from-global`.** `kbagent init --project foo`
  is enough -- you do not also pass `--from-global` (there is nowhere else to
  copy from). `--project a --project b` copies exactly those two.
- **Unknown alias fails fast** with `CONFIG_ERROR` (exit 5) and lists the
  available global aliases; no workspace is created.
- **`default_project` repoints** to the first selected alias if the global
  default falls outside the selection.
- **Omitting `--project` copies all global projects** (unchanged behaviour).

Use it to seed a focused single-project workspace without re-entering a Storage
API token already stored globally.

## `KBAGENT_PROJECT` environment variable

Lets callers override the default project for one shell/session without editing
`config.json`. A few non-obvious rules:

- **Empty string counts as unset.** `KBAGENT_PROJECT=""` (or a value consisting
  only of whitespace) is treated exactly like the variable not being set at
  all. This follows the standard Unix shell convention and prevents a stray
  `export KBAGENT_PROJECT=` from silently breaking every subsequent command.
- **Points to an unregistered alias -> hard fail.** If the env var names an
  alias that is NOT in your configured projects, write-ops (the ones that
  consult the pin) fail with `CONFIG_ERROR` and exit code 5. Repair either by
  running `kbagent project use <valid-alias>` and unsetting the env var, or by
  `unset KBAGENT_PROJECT`. The CLI will not fall back silently to the persisted
  pin -- that would mask a misconfiguration.
- **Precedence for resolving the target project** (highest wins):
  1. `--project <alias>` CLI flag (explicit per-command)
  2. `KBAGENT_PROJECT` env var
  3. Persisted pin (`default_project` in `config.json`, set via
     `kbagent project use <alias>`)
  4. Sole-project fallback (if exactly one project is configured)
  5. Hard fail with `CONFIG_ERROR` (no ambiguous defaulting)
- `kbagent project current` reports which of (2) or (3) is active and flags
  when the env var points to an unregistered alias, so you can diagnose
  precedence issues without reading the source.

## `config update` path semantics (paths start at the configuration ROOT)

`kbagent config update` paths are relative to the configuration ROOT, not to
`parameters`: write `--set 'parameters.db.host=...'`, never `--set 'db.host=...'`.
(The removed MCP `update_config` tool used parameters-relative paths, so an old
runbook that says `path: "db.host"` needs the `parameters.` prefix here, and one
that says `path: "parameters.tables"` would double-nest if copied verbatim.)

- `--merge` deep-merges and preserves sibling keys; `--set` implies merge.
- `--dry-run` shows the diff without applying.
- Input can be inline JSON, `@file.json`, or stdin (`-`).
- There is no `str_replace` / `list_append` operation on `config update`. For
  SQL transformation bodies use `transformation edit` (9 ops incl.
  `str_replace`), which is the native replacement for the old
  `update_sql_transformation` tool.

**Examples:**
```bash
# Set a single nested value (--set implies merge)
kbagent --json config update --project P --component-id C --config-id ID \
  --set "parameters.db.host=new-host.example.com"

# Deep-merge a partial JSON (preserves all siblings)
kbagent --json config update --project P --component-id C --config-id ID \
  --configuration '{"parameters": {"tables": {"new": "data"}}}' --merge

# Preview changes before applying
kbagent --json config update --project P --component-id C --config-id ID \
  --set "parameters.config.debug=false" --dry-run

# Update from a file
kbagent --json config update --project P --component-id C --config-id ID \
  --configuration-file updated-config.json --merge
```

## Batch size limits for `str_replace` refactors

Keep a batch of `str_replace` operations to **50 maximum**. Larger batches
(150+) can trip a Storage Events API size limit: the replacements are applied
and a new config version is created, but logging the change event fails with
`400 Bad Request: Request too large` -- a confusing state where the changes
were saved but the call reports failure. (First observed against the old
`update_sql_transformation` MCP tool; the limit is server-side, so
`kbagent transformation edit` is subject to the same ceiling.)

Workaround for large refactors (e.g. removing `AS` from 200 table aliases):
1. Split operations into batches of 50.
2. Run one `kbagent transformation edit` per batch.
3. `kbagent transformation show` between batches -- positional ids renumber
   after structural ops -- and verify each batch before sending the next.

## SQL transformation file layout

When creating or editing SQL transformations via sync, SQL code must go in
`transform.sql`, NOT in `_config.yml`. The `_config.yml` for transformations
should have `parameters: {}` (empty).

**Wrong** -- putting SQL in `_config.yml` parameters:
```yaml
# DO NOT DO THIS -- SQL will be split per line and each line executed separately
parameters:
  blocks:
    - name: Block 1
      codes:
        - name: Code 1
          script:
            - CREATE TABLE foo AS
            - "    SELECT col1"
            - "    FROM bar;"
```

**Correct** -- SQL in `transform.sql`, config has empty parameters:
```yaml
# _config.yml
parameters: {}
```
```sql
-- transform.sql
/* ===== BLOCK: Block 1 ===== */

/* ===== CODE: Code 1 ===== */
CREATE TABLE foo AS
    SELECT col1
    FROM bar;
```

See `scaffold-workflow.md` for the complete file structure reference.

## `config update` auto-normalizes `script[]` (since v0.28.0, expanded v0.31.0)

The Storage API silently accepts shapes for `parameters.blocks[].codes[].script`
that crash at job runtime. Two distinct traps, each with the same observable
signature (200 OK on PUT, version increments, UI looks fine, crash only at
scheduler-time with no attribution back to the offending write).

**Trap 1 -- string vs array (since v0.28.0; #245)**. The runtime
validator rejects:

```
Invalid type for path "root.parameters.blocks.0.codes.X.script".
Expected "array", but got "string"
```

Reported in #245 after a programmatic refactor of 3 production Snowflake
transformations.

**Trap 2 -- list element packs multiple statements (since v0.31.0; #274)**.
The runtime requires exactly one statement per `script[i]` element. A
list like `["CREATE TABLE x AS ...; alter session unset week_start;"]`
(1 element, 2 statements) passes the array-shape validator but crashes
ODBC with:

```
odbc_prepare(): SQL error: Actual statement count 2 did not match the
desired statement count 1, SQL state 0A000 in SQLPrepare
```

Reported in #274 after a Slovak->Czech config migration where text-only
replacements left existing list elements untouched but already-packed
elements survived the round-trip. Live-reproduced against project 901
(`padak`) config `01km0sd189fdrcnjwk89cd1fkc` -- job 1307622107 crashed
with the exact ODBC message above.

`kbagent config update` (and any wrapper that takes a full configuration --
`--configuration`, `--configuration-file`, `--set parameters.blocks.0.codes.0.script=...`,
and dry-run preview) now closes the gap on the write side **before** the
Storage API touch:

- **SQL transformations** (Snowflake / Synapse / Oracle / Redshift /
  BigQuery / DuckDB, plus fragment fallback for `*-exasol-transformation`,
  `*-teradata-transformation`, etc.): the string is split on statement
  boundaries via the existing `split_statements()` state machine that
  already powers `kbagent sync push`. The splitter respects `'...'` /
  `"..."` / `$$...$$` / `--` / `#` / `//` / `/* ... */`, so semicolons
  inside string literals and block comments do NOT cause splits. Since
  v0.31.0, every **list element** is also passed through the same
  splitter -- multi-statement entries are replaced inline so the
  list-of-1-with-2-statements ODBC trap (#274) cannot survive the write.
- **Python / R / `kds-team.app-custom-python`** and any other component
  sharing the `parameters.blocks[].codes[].script` shape: the string is
  wrapped as a single-element array `[script]`. Statement-level split
  does not apply -- the runtime treats the script as one code chunk.
  Non-SQL list elements are never re-split (Python `;` is a valid
  intra-statement separator: `print('a'); print('b')`).
- **Already-correct list values pass through unchanged** (one statement
  per element).

Observability: every normalization is surfaced.
- JSON mode: the result envelope gains a `normalizations` array.
  Two record shapes:
  - **String -> array** (#245): `{"path": "parameters.blocks[0].codes[0].script",
    "action": "sql_split" | "wrap_array", "before_type": "str",
    "after_type": "list", "after_length": 3}` -- path points at the
    whole `script` field.
  - **List element re-split** (#274; since v0.31.0):
    `{"path": "parameters.blocks[0].codes[0].script[2]", "action":
    "sql_resplit", "before_type": "str", "after_type": "list",
    "before_length": 1, "after_length": 2}` -- path points at the
    **original** element index on input (not the post-split position;
    that's the only useful number for mapping the warning back to your
    source payload). Empty array when nothing was normalized.
- Human mode: a yellow `Auto-normalized N script field(s) to array
  (string -> list). See --json for details.` warning followed by a
  per-element trace. The warning line is action-agnostic so it fires
  for `sql_split`, `wrap_array`, AND `sql_resplit`.
- `--dry-run`: the `new_configuration` field already reflects the
  post-normalize shape, so the preview matches what would actually land.

**The trap still exists when bypassing kbagent.** Direct
`PUT /v2/storage/components/{component}/configs/{config}` calls (curl, custom
Python, any other client) do NOT inherit this normalization. If an LLM agent is
composing the configuration JSON itself, prefer `kbagent config update
--configuration ...` (or `kbagent transformation edit`) over raw REST for SQL
transformations -- that way the normalization fires regardless of what the
other client would have done.

Bonus fix in 0.28.0: `kbagent sync push` previously did NOT split semicolons
in BigQuery / DuckDB transformations because those component IDs were
missing from `SQL_TRANSFORMATION_COMPONENTS`. Push collapsed multiple
statements into one `script` element, mirroring closed issue #119 on a
different backend. The 0.28.0 registry now covers BQ / DuckDB explicitly,
plus fragment-based fallback for future / self-hosted SQL backends.

## Snowflake: MULTI_STATEMENT_COUNT

Keboola sends each code block to Snowflake as a single query batch via the ODBC
driver. Snowflake's default `MULTI_STATEMENT_COUNT = 1` means **only one SQL
statement per batch**. If a code block contains multiple statements (e.g.
`SET` + `CREATE TABLE` + `CREATE TABLE`), the job fails with:

```
Actual statement count N did not match the desired statement count 1
```

**Fix:** Add this as the **first code block** in your transformation:

```sql
ALTER SESSION SET MULTI_STATEMENT_COUNT = 0;
```

This allows unlimited statements per code block. The setting persists for the
entire transformation session. Many existing transformations already have this
-- check before adding a duplicate.

**Note:** This is NOT a Keboola bug. It is a Snowflake ODBC driver default.
Semicolons between statements are required and are NOT the problem -- the
session parameter is.

## Snowflake: identifier quoting (case sensitivity)

Snowflake converts **unquoted identifiers to UPPERCASE**. This means:
- `sapi_226` without quotes → Snowflake looks for `SAPI_226` → **not found**
- `"sapi_226"` with quotes → Snowflake uses `sapi_226` as-is → **works**

**Rule:** Always double-quote ALL parts of Snowflake direct-access paths:

```sql
-- CORRECT: all three parts quoted
SELECT * FROM "sapi_1507"."in.c-keboola-ex-db-mysql"."orders"

-- WRONG: database name unquoted → becomes SAPI_1507
SELECT * FROM sapi_1507."in.c-keboola-ex-db-mysql"."orders"
```

This applies to linked bucket paths (`sapi_NNNN`), native bucket paths, and
any identifier containing dots, hyphens, or lowercase letters.

## SQL editing: do NOT use global text replace on identifiers

This applies to ANY operation that rewrites a table or column name in SQL:

- **Renaming** -- changing a table name (`"orders"` → `"objednavky"`)
- **Migration** -- removing input mapping, replacing aliases with direct
  Snowflake paths (`"orders"` → `"sapi_1507"."in.c-db"."orders"`)
- **Refactoring** -- consolidating duplicate workspace tables, changing
  prefixes (`"tmp.X"` → `"stg.X"`)

In all of these, **never use global find & replace**. A table name like
`"orders"` almost always also appears as a **column name** somewhere
(FK reference in `JOIN ON`, aggregation alias in `SELECT`, `WHERE` clause).

Global replace corrupts every scenario:

```sql
-- BEFORE: rename table "orders" → "objednavky"
SELECT SUM(a."orders") AS "orders" FROM "orders" a

-- AFTER global replace (WRONG!):
SELECT SUM(a."objednavky") AS "objednavky" FROM "objednavky" a
-- The column reference and the SELECT alias were renamed too --
-- only the FROM table should have changed.
```

```sql
-- BEFORE: migrate "orders" alias to Snowflake path
SUM(a."orders") AS "orders"

-- AFTER global replace (WRONG!): column becomes a table path
SUM(a."tmp.orders") AS "tmp.orders"  -- no such column
```

```sql
-- BEFORE: "country_locality" is a FK column in JOIN ON
ON pcl."country_locality" = cl."id"

-- AFTER global replace (WRONG!): FK column becomes full path
ON pcl."sapi_1507"."in.c-keboola-ex-db-mysql"."country_locality" = cl."id"
```

**Safe approach (for any rename, migration, or refactor):**

1. Replace ONLY in **table-reference positions**:
   - After `FROM` keyword
   - After `JOIN` keyword
   - In `CREATE ... TABLE "name"` declarations
   - In `INSERT INTO "name"` / `UPDATE "name"` / `DELETE FROM "name"`
2. Do NOT replace in:
   - Column references: `a."orders"`, `SUM("orders")`, `"orders" AS "orders"`
   - `JOIN ON` conditions: `ON a."col_name" = b."id"`
   - `WHERE` conditions, string literals (`'... orders ...'`)
3. **Context detection heuristic:**
   - Preceded by a dot (`alias."name"`) → column, skip
   - Preceded by `FROM` / `JOIN` keyword → table, replace
   - Inside `SELECT` list (between commas, no FROM yet) → column, skip
4. After editing, verify with regex:
   - **Rename**: search for the new name in column positions
     (`alias\."newname"`, `SUM\("newname"\)`) -- must be zero hits
   - **Migration**: `alias\."sapi_\d+"`, `ON.*=\s*"sapi_\d+"`,
     `"tmp\.\w+"` used as column
   - Verify all old occurrences in table positions are gone
5. Workspace tables created by earlier code blocks (e.g. `"tmp.orders"`)
   must NOT be replaced -- they are runtime artifacts, not aliases.

For input mapping migration specifically, see
[sql-migration-workflow](sql-migration-workflow.md) for the full
step-by-step procedure including building the destination→source map.

## Workspace table name conflicts

When multiple code blocks in a transformation create a workspace table with
the **same name** but different schemas, downstream code blocks may fail
because they expect columns from the original version.

**Example:** Code 0 (Setup) creates `"tmp.carts"` with all MySQL columns.
Code 23 later creates `"tmp.carts"` with only 3 columns. Code 25 then fails
because it needs column `"user"` which Code 23's version doesn't have.

**Rule:** When a conflict exists, rename the **secondary** table by adding a
numeric postfix (`"tmp.carts2"`). Keep the original name for the "source"
table (typically the Setup/materialization code that creates the full copy).
Update all references in the code that creates and uses the renamed table.

## Auto-update

kbagent automatically checks for updates on every invocation. When a newer stable
GitHub Release is available, it prefers the release wheel, performs a full
exact-version reinstall, and re-executes the same command seamlessly. This is
transparent -- no user action is normally required.

- Opt-out: `KBAGENT_AUTO_UPDATE=false`
- Version cache: checks the release endpoints at most once per hour
- Skipped for: dev/editable installs, `update`/`version` commands
- Never crashes the CLI -- update failures leave the current invocation running
  and print a recovery command (since v0.76.2)

### Windows updates are deferred, not immediate (since v0.78.0)

`uv tool install` recreates a tool environment by **removing** it and then
building a fresh venv at the same path. It is not atomic and has no rollback.
On POSIX that is harmless; on Windows uv's `kbagent.exe` trampoline holds the
venv's interpreter locked, so the removal deletes what it can, hits a locked
file, and aborts -- leaving a **gutted** venv (`No module named
'rich._windows'`, `cannot import name 'rich_utils' from 'typer'`). Upstream:
astral-sh/uv#11930.

So on Windows kbagent never installs into its own live environment:

- Both `kbagent update` and the startup hook hand the reinstall to a detached
  helper, which waits until **every** kbagent process has exited and installs
  then. The current invocation continues on the old version.
- Consequence: the new version is active on the **next** launch, not this one.
  `kbagent update --json` marks this with `kbagent.deferred: true`; the summary
  line reads `(scheduled)`, which is **not** a failure.
- If a kbagent process (typically `kbagent serve` or `kbagent repl`) never
  exits, the helper installs nothing and reports `skipped` -- also not a
  failure; the environment is untouched and the next launch retries.
- The outcome is printed by the next launch, including a copy-paste recovery
  command when the install failed.
- `KBAGENT_DEFER_UPDATE=1` / `=0` forces the deferred path on or off,
  overriding the platform default.

A slow install is never killed on any platform (also since v0.78.0): the
timeout bounds only how long kbagent waits, because terminating uv mid-write
produces the same half-deleted environment a file lock does. When that happens
the banner says the install is *still running* and deliberately offers no
recovery command -- starting a second installer against an environment a live
uv is rewriting is the corruption itself.

## `lineage build` and sync layouts

`lineage build` reads synced data from disk and supports both layouts produced by
`kbagent sync pull`:

- **Flat** (after `sync pull --project X`): `./.keboola/manifest.json` directly in CWD.
- **Nested** (after `sync pull --all-projects`): `./<alias>/.keboola/manifest.json`
  for each project side by side.

Pass the matching directory to `--directory` / `-d`:

- Flat: `kbagent lineage build -d . -o lineage.json`
- Nested: `kbagent lineage build -d /path/to/parent -o lineage.json`

If the scan finds zero projects, the build still writes the cache file but
emits a warning (both in the human-readable output and as a `warnings` array
in `--json` mode) with a hint about the expected layouts. In JSON mode, inspect
`result["data"]["warnings"]` to detect this situation programmatically.

## Sync and dev branches

When an active branch is set (`branch use --branch ID`), sync commands automatically
scope to that branch:

- `sync pull` writes configs into a **separate directory** named after the branch
  (e.g. `fix-etl/` instead of `main/`)
- `sync diff` and `sync push` read/write from the correct branch directory
- The manifest tracks all branches in `manifest.branches[]`
- Switching back to main (`branch reset`) makes sync target `main/` again

This means you can have production and dev branch configs side by side on disk
without them overwriting each other.

## Common mistakes

- **Forgetting `--json`**: without it, output is human-formatted Rich text, not parseable
- **Assuming `data.projects`**: `project list` returns data as a flat list
- **Passing manage token as argument**: use the interactive prompt (default since v0.29.0), or `--allow-env-manage-token` + `KBC_MANAGE_API_TOKEN` env var for CI
- **Polling after branch create**: kbagent already waits for async completion
- **Not saving workspace password**: only returned once on creation
- **Putting SQL in _config.yml**: SQL transformations must use `transform.sql` with block markers (see above)
- **Auto-running jobs after config update**: never start a job automatically after pushing config changes -- let the user decide when to run

## Project description vs branch description

The "description" shown on the Keboola project dashboard is **not** the same
field as a branch's `description` attribute:

- **Dashboard project description** = `KBC.projectDescription` metadata on the
  **default (main) branch**. Set via `kbagent project description-set` (or
  generically `kbagent branch metadata-set --key KBC.projectDescription --branch default`)
- **Dev branch description** = the `description` field on a dev branch record.
  Set via `kbagent branch create --description "..."`; visible in the branch
  switcher and synced as `description.md` by the kbc CLI

They live at different endpoints in the Storage API
(`/v2/storage/branch/{id}/metadata` vs. `/v2/storage/dev-branches/{id}`),
so setting a branch's description will **not** update the dashboard.

## Storage descriptions: key convention + precedence + partial failures

`kbagent storage describe-bucket / describe-table / describe-column / describe-batch`
write descriptive metadata onto storage objects. Three behaviors are easy to miss:

- **Column descriptions go through the native definition endpoint** *(since
  v0.88.0)*. `describe-column` / `describe-batch` write via
  `PUT /v2/storage/branch/{branch}/tables/{id}/definition` -- the endpoint the
  web UI uses (async `tableDefinitionUpdate` storage job) -- with
  `isDescriptionSystemManaged: false`, which is what stops the next component
  run's Output Mapping from overwriting a hand-authored description. The backend
  mirrors the written value into `columnMetadata` `KBC.description` for typed AND
  untyped tables, so one write is visible to the Keboola UI, to the MCP server
  (`get_tables`), and in the Snowflake `COMMENT` / BigQuery column description.
  *Pre-0.88.0 behaviour:* kbagent stored each description as a flat
  `KBC.column.{name}.description` entry on the **table's** metadata. Nothing but
  kbagent itself ever read that key -- columns documented that way look blank in
  the UI, are invisible to the MCP server, and never reach the warehouse. That
  mirroring is one-way: a `POST .../metadata` write never reaches the native
  field. Convert leftovers with `kbagent storage describe-migrate` (bulk,
  scan-then-confirm, `--dry-run` first); `describe-column` / `describe-batch`
  also migrate remaining legacy entries on whatever table they touch. A column
  whose visible description already differs is skipped as `conflict` (newer value
  wins), an entry for a dropped column is skipped as `orphan` unless
  `--prune-orphans`. **Migrated flat entries are DELETED** -- that is deliberate:
  leaving them would let the read fallback resurrect an old description after
  someone clears the column's text. `table-detail` reads with the precedence
  native definition -> `columnMetadata` `KBC.description` -> legacy flat key,
  always returns `legacy_column_descriptions`, and warns in human mode when
  legacy keys remain (it never writes -- safe under a read-only token or
  `--deny-writes`). Its human-mode Columns table shows a `Description`
  column. On 0.88.0 it did NOT -- there the descriptions were readable only
  through `--json` `column_details[].description`, so a blank-looking terminal
  table on that version does not mean the write failed. (Release step: once this
  ships, tag this sentence `(since vX.Y.Z)` with the version that carried it.) Unknown column names now fail fast BEFORE any write; the old
  flat write accepted typos silently. Table and bucket descriptions are
  unaffected: still `KBC.description` (provider=user) on the object's metadata.
- **`describe-batch` is partial-failure-tolerant.** Item-level errors are
  collected into `result.errors[]` but the batch keeps processing the remaining
  items. The CLI exits non-zero only if `error_count > 0`, so in scripts always
  inspect `errors[]` (or at least `error_count`) rather than relying solely on
  the exit code — and when consuming `--json` output, never trust a zero-exit
  as "everything applied." That tolerance covers **API** failures only: a
  `--from-file` whose shape is wrong (a `tables:` / `buckets:` / `columns:`
  section that is a list instead of a mapping of ID to description, a column
  entry that is not a mapping, a document that is not a mapping at all) is a
  usage error — the whole file is rejected before the first write with
  `INVALID_ARGUMENT` and exit 2, naming the offending key and its actual type.
  Nothing is half-applied. (Release step: once this ships, tag this sentence
  `(since vX.Y.Z)` with the version that carried it.)
- **Description-field precedence: metadata wins.** When both the native Storage
  API `description` field and a user-provided `KBC.description` (provider=user)
  metadata entry are present, `storage bucket-detail` / `storage table-detail`
  surface the **metadata value**. The native field is only settable at object
  creation time via the Storage API; all user updates flow through the metadata
  endpoint, so the metadata entry is the authoritative source. `KBC.description`
  entries whose provider is not `user` (e.g. `system`) are ignored during
  read-back and the native field is used as fallback.
- **`storage bucket-detail` is dialect-aware** *(since v0.25.3)*. Output adapts
  to the bucket's backend:
  - **Snowflake**: `snowflake_database` / `snowflake_schema` and per-table
    `snowflake_path` quoted with `"DB"."schema"."table"`.
  - **BigQuery**: `bigquery_dataset` (and `bigquery_project` when surfaced via
    API `databaseName`) and per-table `bigquery_path` quoted with backticks
    (`` `dataset`.`table` `` or `` `project`.`dataset`.`table` ``).
  Backend-agnostic keys `sql_dialect` (`"snowflake"` / `"bigquery"`) and
  per-table `sql_path` are always present -- prefer them in agent code instead
  of branching on backend yourself. The misleading `snowflake_database` /
  `snowflake_schema` / `snowflake_path` keys are **NOT** emitted on BigQuery
  results in 0.25.3+. *Pre-0.25.3 behaviour:* the function unconditionally
  emitted Snowflake-style keys with double quotes regardless of backend; on a
  BigQuery bucket this produced syntactically invalid SQL (BQ requires
  backticks) AND a fabricated `f"sapi_{project_id}"` database name (BQ has no
  such naming convention). If you see a 0.25.2-or-older bucket-detail JSON
  saved offline against a BQ project, treat the `snowflake_*` fields as
  garbage. The `f"sapi_{project_id}"` Snowflake fallback (when `backendPath`
  is missing) still fires for Snowflake buckets but no longer for BigQuery.
- **BigQuery `databaseName` is usually empty** *(since v0.25.3)*. On Keboola-
  managed BQ projects the Storage API returns `databaseName: ""`, so
  `bucket-detail` cannot construct a fully-qualified `project.dataset.table`
  path -- the resulting `bigquery_path` is dataset-qualified only
  (`` `dataset`.`table` ``) and `bigquery_project` is the empty string. If the
  user needs a full FQN (e.g. for a query against the GCP console or for an
  external tool), ask them for the GCP project name explicitly. On BYODB BQ
  projects `databaseName` is populated and the full FQN is emitted.

## `job terminate` quirks

Queue API's kill endpoint (`POST /jobs/{id}/kill`) has a few non-obvious behaviors the
CLI hides via its four-bucket response, but they matter when interpreting results:

- **Kill is asynchronous.** A successful `killed` entry has
  `desiredStatus=terminating` but the actual `status` does not change immediately.
  The job transitions to `cancelled` (if it was `waiting`) or `terminated`
  (if it was `processing`) within a few seconds. Poll `job detail` for
  `isFinished=true` before assuming it's done.
- **`processing` is transient in the middle of termination.** Between the
  accepted kill and the terminal state, you may briefly observe
  `status=terminating` -- still `isFinished=false`. Don't treat it as an error.
- **Re-terminating a finished job is safe.** Queue API returns HTTP 400 for
  already-terminal jobs; the CLI reports them in `already_finished` rather than
  `failed`. This also covers race conditions where a job finishes between
  `list` and `terminate`.
- **Bogus or already-`success`/`error` IDs hit an inconsistency:** Queue API
  returns HTTP 500 with body `code=404`. The CLI verifies via GET: if the job
  exists and is finished, it lands in `already_finished`; if GET returns 404,
  it lands in `not_found`.
- **`--status` filter is client-side for branches.** Queue API's `/search/jobs`
  does not accept a branch parameter, so `--branch ID` is applied by filtering
  the listed jobs on `branchId`. If you need pristine branch scoping, consider
  using the IDs returned from `job list --status processing` and passing them
  explicitly with `--job-id`.
- **`--status any` is the right default for runaway cleanup.** It fetches all
  recent jobs (no status filter) and keeps only `created`/`waiting`/`processing`
  client-side. Picking a single status misses the other killable states -- e.g.
  a runaway loop often piles up `waiting` jobs while you're typing
  `--status processing`.

## Parquet export: slices, not a single file

- `storage unload-table --file-type parquet` always produces a **sliced** output.
  With `--download`, the result is a **directory** (`./{project}/{table_id}.parquet/`),
  never a single `.parquet` file. If your code expects a single file, adapt it to
  read the directory as a Parquet dataset:
  ```python
  import pyarrow.parquet as pq
  t = pq.read_table("./ALIAS/in.c-bucket.table.parquet/")
  ```
- **Never concatenate Parquet slices.** Each slice is a self-contained Parquet
  file with its own footer. Binary concatenation (how CSV slices are merged)
  would produce an invalid file. For the same reason, `storage file-download`
  auto-detects sliced `.parquet` files and routes them to the per-slice
  downloader -- there is no flag to force single-file mode.
- The manifest sidecar is written as `_manifest.json` (**with a leading
  underscore**). This is intentional: Hive/Spark/pyarrow parquet readers skip
  files starting with `_` or `.` when scanning a directory as a dataset, so the
  manifest is preserved for traceability without breaking direct reads. Same
  convention as `_SUCCESS`, `_metadata`, `_common_metadata` in Hadoop.
- The default path `./{project_alias}/{table_id}.parquet/` mirrors Keboola
  addressing. When exporting multiple tables, each ends up in a predictable
  subdirectory and there is no risk of name collisions. Override with
  `--output DIR` if you need a custom location.

## Flow: conditional flows only; `--component-id` removed (since v0.57.0)

- **RESOLVED (since v0.57.0):** the old foot-gun where `flow new` defaulted to
  `keboola.flow` but `flow detail/update/delete/schedule/...` defaulted to
  `keboola.orchestrator` is **gone**. The `flow` group now targets the single
  component `keboola.flow`, and `--component-id` has been **removed** from every
  `flow` subcommand. Passing it errors with "No such option".
- **`keboola.orchestrator` is dropped (since v0.57.0).** `flow list` does NOT
  list orchestrator configs; it reports their total as `legacy_orchestrator_count`
  (+ a warning) so you can see why a legacy flow "disappeared". There is no
  migration command (cross-component migration is out of scope).
- **IDs are STRINGS (since v0.57.0).** `phase.id`, `task.id`, `next.id`,
  `task.phase`, and `goto` are all JSON strings (`goto` is `string | null`).
  Integer ids fail Draft7 validation and are rejected with
  `INVALID_FLOW_DEFINITION`.
- **The old `dependsOn` phase-DAG template is invalid (since v0.57.0).** Phases
  use `next[].goto` (a phase id or `null` to end) with an optional `condition`;
  a phase with conditional transitions must end with a default (condition-less)
  transition. Tasks are typed (`job`/`notification`/`variable`).
- **`INVALID_FLOW_DAG` was renamed to `INVALID_FLOW_DEFINITION` (since v0.57.0).**
  Update any code/string matching on the old error code.
- **Validation (since v0.57.0):** `kbagent flow validate --file @flow.yaml [--project ALIAS]`.
  With `--project` it fetches the **live** JSON Schema from the stack and runs
  full structural + semantic checks; without `--project` it runs semantic-only
  and adds a note that structural validation was skipped (no schema source).
  Exit 0 valid, exit 2 on errors. Use it in a tight loop before
  `flow new`/`flow update`.
- **Schema is fetched live from the stack, NOT bundled (since v0.57.0).** The
  conditional-flow JSON Schema is served by the stack's component registry and
  read at runtime via the AI Service `configurationSchema` for `keboola.flow`
  (the same path `config new --push` uses). There is nothing vendored, pinned,
  or to re-sync. `flow schema --full` therefore **requires `--project`** (plain
  `flow schema` is still the offline YAML template).
- **Graceful semantic-only degradation (since v0.57.0).** If the live schema
  fetch fails (network error, or the AI Service returns no `configurationSchema`),
  `flow new`/`flow update`/`flow validate --project` do **not** block: structural
  validation is skipped, the semantic checks still run (Storage does not validate
  flow configs server-side), and a `structural schema validation skipped: <reason>`
  warning/note is surfaced. A genuine `INVALID_FLOW_DEFINITION` still rejects the
  write.

## `schedule find --cron-window` is an hour-field approximation

`kbagent schedule find --cron-window "02:00-04:00"` is an **audit helper, not a real cron evaluator**. It parses the *hour* field of the cron expression and asks whether every hour at which the cron fires falls inside the passed window. It deliberately does **not** account for minute precision, day-of-month restrictions, or day-of-week restrictions.

- **Minutes in `--cron-window` are syntactic sugar, but still validated.** The spec `02:00-04:00` is accepted in the `HH:MM-HH:MM` format because it matches how people describe time windows; the matcher itself only uses the hour part. A cron expression `*/10 2-4 * * *` (every 10 minutes within hours 2-4) matches `--cron-window "02:30-04:30"` exactly the same as `--cron-window "02:00-04:00"`. Minute values outside `00-59` (e.g. `02:70-04:88`) are still rejected at parse time so obviously malformed inputs fail loudly.
- **Hour field `*` (fires every hour) never matches a bounded window.** This is intentional: from an audit standpoint, "fires every hour" is the opposite of "confined to a 2-hour window". If you wanted to catch those, pass `--cron-window "00:00-23:00"` (or skip the window filter entirely).
- **Wrap-around windows are not supported.** `--cron-window "22:00-02:00"` returns an error. The error message itself points you at the workaround: split into two passes (`22:00-23:00` and `00:00-02:00`) and union the results in your script.
- **`,` lists, `-` ranges, and `*/N` steps on the hour field are all expanded.** Unparseable inputs fail safe to "no match" rather than "match everything" -- cleanup audits should never accidentally widen.
- **Day-of-week / day-of-month restrictions are ignored.** A cron that only runs on Mondays is matched as if it fired every day. For most audit use-cases this is the right default: "which schedules *can* fire in this window?" is more operationally useful than "which schedules *will* fire today?".

If you need full cron semantics (e.g. "what's the next time this cron fires?") pipe the schedule list into `croniter` or a similar library from your own script -- the CLI deliberately stays out of that space.

## `schedule find` without filters -- `last_run_at` and `matches_cron_window` are `null`

`kbagent schedule find` always emits `last_run_at` and `matches_cron_window` on every row, but **populates them only when the corresponding filter is active**. Without filters both columns are `null`; with `--cron-window` only `matches_cron_window` is populated; with `--not-run-since` only `last_run_at` is populated.

Why not always populate? Because `last_run_at` costs one `list_jobs(limit=1)` Queue API call per unique parent config per project -- paying that unconditionally, to populate a column nobody asked for, is a pointless audit-wide latency hit. `matches_cron_window` is only meaningful relative to a user-supplied window.

- **LLM/agent callers:** do not treat `matches_cron_window: true` as an affirmative signal unless `filters.cron_window` in the response envelope is populated. Before 0.23.0 this defaulted to `true` everywhere, which was misleading.
- **Force `last_run_at` population without filtering:** pass `--not-run-since 0`. That fires the Queue API lookup for every row (N extra calls per project) and returns every row (no staleness filter applied because any past timestamp counts as stale at threshold 0 and `null` also counts as stale).
- **`--not-run-since` + `--branch <DEV_ID>`:** the Queue API has no branch parameter. The timestamp comparison still hits production jobs, so schedules in a dev branch that were freshly deployed will register as stale even if their parent ran on main moments ago.
- **`_fetch_latest_job_ts` silently returns `None` on Queue API errors** -- permission problems on Queue API are invisible in `errors[]`. If one project shows a suspiciously uniform "never ran" cluster, run `kbagent job list --project <alias>` to sanity-check the token.

## `schedule list` + `schedule find` payload size scales with project size, not schedule count

Both commands issue one `list_components_with_configs(branch_id=...)` call per project. That endpoint returns **every** component's configurations + rows + full configuration bodies -- not just `keboola.scheduler`. For a 50-component x 5-config project that is 250 configurations on the wire per project just to extract a handful of scheduler configs + parent names.

The trade-off is deliberate: one big call avoids the O(unique-parents) round-trip a smaller `list_component_configs("keboola.scheduler")` + per-parent `get_config_detail` path would cost, and the parent-name join happens in memory for free. For typical 14-project audits this finishes in seconds. If you encounter memory pressure on unusually large projects, split the audit per-project (`--project X`) instead of fanning out wide. `flow list --with-schedules` uses the lighter per-component path because it does not need the parent-name join that schedule-side audits do.

## Flow: `schedule` is an upsert (no `schedule-update`)

- `kbagent flow schedule` creates a `keboola.scheduler` config on first run
  and **updates the existing one in-place** on subsequent runs. Running it
  twice with different `--cron` values replaces the schedule — it does not
  create a second one. That's why there is no separate `flow schedule-update`
  command.
- To inspect or remove schedules: `kbagent flow schedule-remove` deletes all
  scheduler configs that target the flow. Pair it with `--dry-run` to see the
  affected configs (cron + timezone) without calling `delete_config`.

## Flow: `schedule` activates on the Scheduler Service (since v0.66.1)

- `kbagent flow schedule` registers the `keboola.scheduler` config with the
  **Scheduler Service** (`POST /schedules`) after writing it — writing the
  Storage config alone leaves the cron trigger dormant. Older versions only
  wrote the config: a schedule they created shows `state: enabled` but never
  fires until `flow schedule` is re-run on v0.66.1+ (or the schedule is
  re-saved in the UI).
- Activation failure is **non-fatal**: the config stays written, the result
  carries `activated: false` + a warning, and the exit code stays 0. Typical
  cause is a Storage token without the schedule-management privilege — re-run
  with an admin token to activate.
- `flow schedule-remove` (v0.66.1+) deregisters each schedule from the
  Scheduler Service before deleting its config, so removal stops the trigger.

## `search` is a top-level command, not `config search` (since v0.30.0)

`kbagent search QUERY` searches across **all item types** (tables, buckets, configs, flows, data apps, transformations) via the Storage API global-search endpoint. It is distinct from `kbagent config search --query Q` which scans only configuration JSON bodies.
- `search --search-type config-based` delegates to `config search` internally but exposes the unified results shape. It matches **case-insensitively** (since v0.84.0) while `config search --query` stays case-sensitive unless `-i` is passed -- the same logical table is often mixed-case in a row name and upper-case in `storage.input.tables[].source`, so a case-sensitive body scan answers "is this referenced anywhere?" with a false no.
- Options (`--type`, `--project`, `--limit`) must come AFTER the QUERY argument: `kbagent search "text" --type table --limit 10`.

## `config row-create` / `row-update` / `row-delete` lifecycle (since v0.30.0)

Full CRUD for configuration rows is exposed as a separate `Rows` command panel:
- `row-create` returns the new row object including `id`. Capture this ID for subsequent `row-update` / `row-delete` calls.
- `row-update` preserves all unspecified fields — pass only the keys you want to change. `--is-disabled` and `--is-enabled` are mutually exclusive flags for toggling the row's active state.
- `row-delete` is **destructive** (gated behind `--allow-destructive` if the session firewall is on). 404 from the API on a non-existent row surfaces as `NOT_FOUND` exit 1 — deletion is **not** treated as idempotent success.
- `--json` mode auto-skips the interactive confirmation prompt on `row-delete`; in human mode pass `--yes` to skip.

## `project status` / `project list` expose `org_id` / `org_name`; `org_name` is Manage-API-only (since v0.40.3)

`ProjectConfig` now persists `org_id` (int | None) and `org_name` (str | None);
both are surfaced verbatim in `kbagent project status` and `kbagent project
list` JSON output. The two fields are populated from **different sources**:

- **`org_id`** comes from `data.organization.id` at the **top level** of the
  Storage API `/v2/storage/tokens/verify` response (NOT under `owner`).
  Populated whenever a project is added / re-verified — including the
  opportunistic backfill that `/projects/status` performs for projects
  registered before this release. The API returns the id as a string
  (e.g. `"73"`); the parser normalises it to int.
- **`org_name`** is **Manage-API-only**. The Storage API never carries it.
  It is populated only when the project flows through `kbagent org setup`
  (which calls `/manage/organizations/{id}`) or when `kbagent project add`
  runs in a context that has a Manage API token. Projects registered via
  plain `kbagent project add` (Storage token only) keep `org_name: null`
  indefinitely.

**AI agent rule of thumb**: when reading `project status` JSON, ALWAYS
handle `org_name: null` even when `org_id` is set. Do not pattern-match on
both being present; the asymmetry is the steady state for the majority of
projects. The web UI Projects table renders `#<org_id>` (e.g. `#73`) as a
fallback when only the id is known, so any agent producing a human-readable
project list should do the same — never render the bare null.

## `config oauth-url` requires a master Storage API token (since v0.30.0)

The OAuth wizard URL embeds a short-lived **child** Storage API token scoped
to the target component. Minting this child token via `POST /v2/storage/tokens`
requires a **master (admin) token** — `canManageTokens` alone is not enough
(issue #599: a non-master token carrying it makes the API 500 instead of 403).

- Pre-flight: `kbagent` calls `verify_token` first and refuses with
  `MISSING_MASTER_TOKEN` (exit 3) before any HTTP write happens. Without this
  guard the Storage API returns a vague 500 "Application error" that misleads
  operators into thinking the OAuth wizard is broken.
- Fix path: re-add the project with a master token
  (`kbagent project edit --project <ALIAS> --token <MASTER_TOKEN>`) or open
  the OAuth flow via the Keboola UI instead.
- AI agents creating the project token via `kbagent project add` /
  `kbagent project refresh` get a non-master token by default — they must
  switch to a master token before calling `config oauth-url`. Granting the
  minted token `canManageTokens` would NOT help: issue #599 established the
  Storage API rejects a non-admin token carrying that flag with a 500
  (`CreateTokenVoter` "impossible state"), so a master token is the only
  working credential here. `token create` carries the same guard since
  v0.89.0 (`token refresh` does not — that endpoint has no such defect).

## `data-app logs` is the only unconstrained log surface (since v0.43.8)

- The upstream `keboola-mcp-server` `get_data_apps` MCP tool hardcodes a
  20-line cap on log output (`_fetch_logs(..., lines=20)` in
  `keboola_mcp_server/tools/data_apps.py`), which is structurally too
  small to capture a data-app spin-up trace. The `[TIMING] git_clone` +
  `uv install` + supervisord boot stanza alone is 30+ lines on a healthy
  `python-js` app; the 20-line cap silently drops everything before
  supervisord. Use `kbagent data-app logs --project X --app-id Y`
  instead — same Data Science endpoint (`/apps/{id}/logs/tail`), no
  client-side cap. Default `--lines 500`; pass `--lines 0` to opt into
  the full current container buffer with no server-side limit.
- `--lines` and `--since` are mutually exclusive on the server (both ->
  HTTP 400 `Only one of "since" or "lines" can be set`); the CLI
  rejects the combination locally with exit 2 + `USAGE_ERROR` to save
  the round-trip. `--since` requires a timezone (`Z` or `+00:00`);
  naive datetimes are rejected at the command boundary via
  `datetime.fromisoformat` before the request.
- Apps that have never started (or were created with `--no-deploy`)
  surface the server's `App "X" is not running` 400 verbatim — recovery
  is `kbagent data-app start` or `data-app deploy`. Auto-suspended apps
  (default `auto_suspend_after_seconds: 900`) hit the same path.
- **Secret-echo risk**: the log buffer can contain runtime secrets the
  app printed to stdout/stderr. Real-world examples: pandas/SQLAlchemy
  tracebacks with embedded connection strings, debug `print(os.environ)`
  output, OAuth state dumped to console, accidental `print(api_key)` in
  dev branches. The `--json` envelope reproduces the body verbatim with
  NO masking — false confidence is worse than honest passthrough. Be
  mindful when piping `--json` output into AI agent context, scheduled-
  agent persisted event logs, or `kbagent serve` SSE streams. The
  permission classification is `read` (no side effects) which is correct
  under `--deny-writes` / `--deny-destructive`; the secret hygiene
  consideration is orthogonal to firewall semantics.
- JSON envelope shape:
  `{project_alias, app_id, lines_requested, since_requested, lines_returned, text}`.
  The `text` key carries the raw plain-text body (trailing `\n`
  preserved as the server emits it); `lines_returned` is
  `text.splitlines()` length. To split into a pipeline:
  `kbagent --json data-app logs --project P --app-id 42 | jq -r .data.text | head -50`.
- Human-mode rendering uses `Console.print(text, markup=False, highlight=False)`
  for the log body so literal `[TIMING]`, `[INFO]`, etc. aren't
  interpreted as Rich tags and timestamps/URLs/IPs aren't auto-colored.
  The header (`Logs for data app <id> in <project>`) keeps Rich
  styling and escapes the interpolated `app_id` / `project_alias`
  values via `rich.markup.escape` per the `commands/config.py`
  precedent.

## `kbagent agent` CRUD works offline; cron firing needs `kbagent serve` running (since v0.44.0)

The `kbagent agent <verb>` command tree reads/writes `<config_dir>/agents.json`
directly via `AgentService`, so `agent list / show / create / update / delete /
run / runs / cron-preview / test / prompt-improve` all work without
`kbagent serve` running. But the cron loop that fires scheduled tasks lives
**inside** the FastAPI lifespan -- tasks created via CLI sit dormant until a
serve instance picks up the file on its next minute-tick.

- **Symptom**: `kbagent agent create --cron "*/5 * * * *" --type ...` returns
  the persisted task with `next_run_at` populated, but the task never
  actually runs. `agent runs <id>` stays empty hour after hour.
- **Cause**: no `kbagent serve` process is running, so the cron loop never
  ticks. The task is correctly persisted; the scheduler just isn't alive.
- **Fix**: start `kbagent serve` (with or without `--ui`) on the machine that
  owns the config directory. The scheduler picks up the new task on the
  next tick and fires it on the cron's schedule.
- **Diagnostic**: `kbagent agent run <id>` works offline (it dispatches via
  the in-process runner without consulting cron). If that runs the action
  successfully but cron-driven runs never happen, the scheduler is offline.

This is the SAME on-disk format the REST router writes via `POST /agents`,
so CLI-created tasks are interchangeable with UI-created and REST-created
tasks -- the only difference is who fires them.

## `agent` action helpers are shared between REST + CLI (since v0.44.0)

The `validate_trigger` (cycle/self-loop check) and `merge_runtime_input`
(per-action-type runtime input merge) helpers live in
`keboola_agent_cli.server.agents_store` so the REST router and the CLI
`AgentService` share the exact same boundary behaviour. If you add a
new action type, update **both** the runner dispatcher (`agent_runner.py`)
**and** `merge_runtime_input` to keep CRUD parity. Tests in
`tests/test_agent_service.py` + `tests/test_agent_cli.py` catch drift.

## `feature` command group: super-admin token, no per-project endpoint, opaque schema (since v0.48.0)

The `feature` group manages Keboola feature flags via the **Manage API**. Five
things trip up callers:

1. **Super-admin manage token required.** `feature list` (the stack catalogue)
   and every project/user mutation need a super-admin Manage API token -- the
   same kind `org setup` uses, NOT the per-project Storage token. It follows the
   default-deny policy: interactive hidden prompt by default; pass top-level
   `--allow-env-manage-token` + `KBC_MANAGE_API_TOKEN` for CI. Do NOT pass the
   token as a CLI flag. A non-super-admin token returns 403 (exit 3).

2. **`--project` is just a handle to the stack URL.** For `feature list` and the
   `user-*` commands the alias only resolves the stack URL -- the catalogue and
   user features are stack-wide, not project-scoped. For `project-*` commands it
   additionally resolves the numeric `project_id` from config. The alias must be
   registered (`kbagent project list`); `project-*` also requires it to carry a
   `project_id`.

3. **No dedicated "project features list" endpoint.** `feature project-show`
   reads the `features` array off `GET /manage/projects/{id}`; `feature
   user-show` reads it off `GET /manage/users/{email}`. There is no
   `/projects/{id}/features` GET. Only the add (`POST .../features`, body
   `{"feature": NAME}`) and remove (`DELETE .../features/{name}`) verbs are
   per-resource.

4. **Request body is `{"feature": NAME}`, not `{"name": NAME}`.** The add
   endpoints take the feature code under the key `feature`. (Some third-party
   notes claim `name` -- that is wrong for this API.)

5. **Feature schema is opaque + shape-variable.** The Manage API publishes no
   feature schema, and a `features` array may come back as a list of objects OR
   a list of bare strings depending on stack/endpoint. kbagent normalises both
   to `{name, title, description, type, ...}` (bare strings become
   `{"name": s}`) and passes unknown keys through unmodified. Treat `name` as
   the only stable field; do not depend on `title`/`type` being populated.

To inspect a project's *enabled* features without a super-admin token, use
`kbagent project info --project P` (read-only) instead -- it returns the enabled
feature list among other project metadata.

## Developer Portal: writes require a human, no exceptions (since v0.49.0)

`kbagent dev-portal {create,patch,upload-icon,publish,deprecate}` always print
the request preview and then require the user to type a random hex code on a
real terminal. There is no `--yes` flag. There is no env-var override. The
command exits 6 (`EXIT_PERMISSION_DENIED`) on a non-TTY shell.

For agentic use: stop at the preview. Use `--dry-run` to get a clean
exit-0 preview you can show the user. Then ask the user to run the same
command without `--dry-run` themselves.

Reads (`dev-portal list`, `dev-portal get`) are unrestricted — peer-research
patterns ("show me how MySQL and Postgres extractors configure themselves")
are agent-friendly via `list --vendor` + `get --app`.

## Headless / token-only invocation: the `__env__` project (since v0.50.0)

A daemon, container, or CI job that has only a token in its environment can run
kbagent with **no `kbagent project add` and no `config.json` on disk**. Set all
three:

```bash
export KBAGENT_PROJECT_FROM_ENV=1
export KBC_TOKEN=<storage-api-token>
export KBC_STORAGE_API_URL=https://connection.<region>.keboola.com
kbagent --json storage file-upload --project __env__ --file screenshot.png
```

kbagent synthesizes an in-memory project under the reserved alias `__env__`.
Pass it as `--project __env__` (or rely on it being the sole/default project for
commands that fall back to the default).

Gotchas:
- **Opt-in is the flag, not the token.** `KBC_TOKEN` alone does nothing here —
  it stays a `project add` fallback. Only `KBAGENT_PROJECT_FROM_ENV` (truthy:
  `1`/`true`/`yes`/`on`) triggers injection. This avoids a phantom project on a
  dev machine that exported `KBC_TOKEN` for an unrelated `project add`.
- **Token is never persisted.** `__env__` is `ephemeral`; even if a write op
  triggers a `config.json` write, the env token is stripped first. There is no
  way to leak it to disk through normal operation.
- **Fail-fast.** Flag set but `KBC_TOKEN` or `KBC_STORAGE_API_URL` missing →
  exit 5 (`config error`) with a clear message, not a silent skip.
- **Same chokepoint for `serve`.** `kbagent serve` started with the same three
  env vars exposes `__env__` too — POST endpoints take `project=__env__`. Both
  CLI and serve resolve through `ConfigStore.load()`, so one env setup covers
  both consumption styles.
- The alias is literally `__env__` (double underscore both sides) — chosen so it
  cannot collide with a real user alias. A real project already registered under
  `__env__` wins; no injection happens.
- **`__env__` shows `project_id` but a blank name in `project list`.** `load()`
  is offline, so the injection recovers `project_id` from the token prefix
  (`{projectId}-{tokenId}-{secret}`) but cannot fetch the real project name.
  Run `kbagent project status --project __env__` (or `project info`) to verify
  the token against the API and see the real name.
- **`KBC_STORAGE_API_URL` is forgiving (since v0.50.0).** A bare host
  (`connection.keboola.com`), a trailing slash, or a full project deep-link
  (`.../admin/projects/123/dashboard`) all normalize to `https://<host>`. Same
  normalization applies to `project add --url` / `project edit --url`. Explicit
  `http://` / `file://` is still rejected; a bad URL fails fast with a clean
  config error (exit 5), not a traceback.

## `stream`: two hosts, secret-in-URL, no auto-sinks (since v0.50.0)

Data Streams has **two hosts**. The *control plane* is `stream.<region>` (derived
from `connection.<region>`, same scheme as `ai.`/`queue.`) and is what the CLI
calls, authenticated with the ordinary per-project **Storage** token — there is
no manage token and no extra prompt. The OTLP *ingestion* endpoint lives on a
**different** host, `stream-in.<region>/otlp/<projectId>/<sourceName>/<secret>`,
and is **returned by the API** in `source.otlp.url` — kbagent never derives it.

The ingest secret is **in the URL path**. `stream detail` / `create-source` mask
it by default in every surface (the `endpoint`, all three per-signal endpoints,
and the raw `source` object echoed in `--json`). Pass `--reveal` to print the
real secret — e.g. to wire `OTEL_EXPORTER_OTLP_ENDPOINT` for a daemon:
`kbagent --json stream detail SRC --project P --reveal`.

Creating an OTLP source **via the raw Stream API** creates only the bare source
— no sinks, no tables. So `kbagent stream create-source --type otlp` (matching
the Keboola UI) **auto-provisions the three sinks** logs/metrics/traces into
bucket `in.c-otlp-<sourceId>`, mapping each record to `id` (uuid) + `datetime`
(ingest time) + `body` (the full flattened OTLP record as JSON), so data lands
out of the box. Provisioning is **idempotent** (only missing signals are added)
and `--no-sinks` opts out for a bare source. The destination tables themselves
materialize lazily on first import (the bucket/table appear in Storage seconds
after the first record arrives, not at create time). `create-source` / `delete`
/ sink creation are **async**: the API returns a Task that kbagent polls to
completion before returning.

## `dev-portal patch`: admin-only fields need an admin-role identity, vendor PATCH lies about why

`PATCH /vendors/{vendor}/apps/{app}` on apps-api `.forbidden()`s 9 fields:
`complexity`, `categories`, `category`, `features`, `forwardToken`,
`forwardTokenDetails`, `injectEnvironment`, `processTimeout`,
`requiredMemory`. Sending any of them via a vendor identity returns
`422 Parameter complexity must be one of: easy, medium, hard` (or the
analogous enum message for the other fields). **The message is a server
bug** — the enum-validation `.error()` annotation lives on the shared
admin schema before `clientAppSchema()` overrides with `.forbidden()`,
so when `.forbidden()` fires Joi reuses the unrelated enum message
instead of saying "this field is not allowed here".

To set any of these you need an admin identity that routes the PATCH
to `PATCH /admin/apps/{app}` instead (since v0.51.1):

```
kbagent dev-portal identity add --alias admin-keboola \
    --username admin@keboola.com --role-hint admin --password-stdin
kbagent dev-portal patch --app keboola.ex-foo \
    --data /tmp/patch.json --identity admin-keboola
```

With `role_hint: vendor` (the default), kbagent now pre-flights the
payload and fails fast with the same guidance instead of letting the
apps-api return the misleading 422 (since v0.51.1). The 9 forbidden
fields are documented in
[keboola/developer-portal:src/lib/validation.js](https://github.com/keboola/developer-portal/blob/master/src/lib/validation.js)
under `clientAppSchema()`.

## `dev-portal identity add`: MFA logins for TOTP accounts need the `challenge` field explicit (since v0.51.1)

The apiary spec calls `challenge` optional with default `SOFTWARE_TOKEN_MFA`
on the second-step `POST /auth/login`, but in practice the server 404s
when it is omitted on a personal-account TOTP login. kbagent now sends
`challenge: SOFTWARE_TOKEN_MFA` explicitly. Single attempt only:
`/auth/login` consumes the session, so any retry with a different
challenge type would always 404 with `Invalid code or auth state for
the user` and mask the real first failure. The raised error includes
the server response body and a hint about TOTP code rotation, so
"stale code" can be distinguished from "wrong code" / "expired session".

## `dev-portal identity {add,edit} --password-stdin` works in both TTY and pipe mode (since v0.51.1)

Pre-0.51.1 the flag did `sys.stdin.read().strip()` unconditionally,
which waits for EOF rather than Enter — pasting a password and pressing
Enter just hung until Ctrl-C. The helper now branches on
`sys.stdin.isatty()`: TTY uses `getpass.getpass()` (hidden, line-based,
Enter confirms); pipe (`echo $PASS | kbagent dev-portal identity add
--password-stdin`) still reads to EOF.

## `sync pull --force` preserves un-pushed edits and aborts on conflict (since v0.53.0)

`--force` is **conflict-aware**, not a blind overwrite. Pre-0.53.0 a force-pull
run while you had un-pushed local edits to a config whose remote was unchanged
**silently corrupted the sync baseline**: it re-stamped the manifest `pull_hash`
from the *edited* on-disk file, so `sync diff` / `sync push` then reported "in
sync" and shipped nothing while the remote still held the old config -- the edits
were stranded with no signal. Fixed: `--force` now branches on the 3-way state.

- Local edited, remote unchanged -> **preserved** (pending delta stays pushable).
- Local edited AND remote also changed (true conflict) -> **aborts** with exit 1
  and error code `SYNC_CONFLICT`; the `--json` envelope carries
  `details.conflicts: [{scope, component_id, config_id, config_name, path, row_id?}]`.
  Resolve via `sync diff` then `sync push`-or-discard, then pull again.
- Local untouched, remote changed -> takes remote (unchanged behavior).

Consequence: `--force` no longer discards non-conflicting local edits. To
intentionally drop a local edit, delete the file (or the config dir) and pull.
Applies at config and row granularity. `--all-projects` reports a per-project
conflict as that project's error without aborting the rest of the batch.

## `ai_agent` `extra_args` need an opt-in env on the kbagent process (since v0.60.2)

`extra_args` on an `ai_agent` task are passed **verbatim** to the underlying AI
CLI (claude/codex/gemini), so they can carry rail-disabling flags
(permission-skip / unrestricted-execution) that turn a contained headless agent
into arbitrary host command execution. Since v0.60.2 they are **ignored by
default** and only honored when the kbagent process running the task -- `kbagent
serve` for scheduled tasks, or a local `agent test` / `agent run` /
`prompt-improve` -- has a truthy `KBAGENT_ALLOW_AI_EXTRA_ARGS` (`1` / `true` /
`yes` / `on`); otherwise they are dropped and a warning is logged. Mirrors the
`--allow-env-manage-token` opt-in. Consequence: a task carrying
`"extra_args": [...]` (or `--extra-arg ...` on the CLI) is a **no-op on the
args** unless the env is set -- if your extra flags "do nothing", check the env
on the kbagent process running the task, not the task definition. (Private
advisory GHSA-777j-6p95-qv3m.)

Related (since v0.60.2): scheduled `ai_agent` subprocesses no longer inherit the
manage (`KBC_MANAGE_API_TOKEN`) or master (`KBC_MASTER_TOKEN*`) tokens from the
serve environment -- an AI agent reaches Keboola via `kbagent http` or by
forking `kbagent`, and never needs those super-admin credentials. `KBC_TOKEN`
(storage) is retained, and `cli_command` tasks are unchanged. (Private advisory
GHSA-wm54-r2hh-cxm9.)

## `data-app git-repo` needs a deployed app (since v0.63.3)

The git-repo introspection command (sandboxes-service
`GET /apps/{id}/git-repo`) returns **409 "App has no Git repository
configured"** until the app has been **deployed at least once** -- even though
`data-app create --git-repo <url>` already wrote the git block into the Storage
config. The git block is synced from the Storage config into the Data Science
app record at *deploy* time; a `--no-deploy` app has no git repo from the
service's point of view. Fix: run `kbagent data-app deploy` (the sync happens
before the container build, so it works even if the build later fails), then
re-run the git-repo command.

> **Removed in v0.66.1:** `data-app git-branches` and `data-app git-entrypoints`
> were dropped -- the sandboxes-service backend removed the underlying
> `/git-repo/branches` and `/git-repo/entrypoints` endpoints (they cannot work
> for managed git repos and will be reworked via git-service). Use `git-repo`
> for clone-URL introspection.

Other behaviors of this family:

- `git-credentials` / `git-credentials-create` only apply to a **managed** git
  repo (`app.managedGitRepoId` set). Apps created via
  `data-app create --git-repo <url>` are **external**, so
  `git-credentials-create` returns **409 "no managed Git repository"** for them.
  These two endpoints also need an **admin** storage token
  (`CanManageAppRepoCredentials`), unlike `git-repo` which needs only the
  ordinary project storage token.
- For `--type http_token`, the create response carries a **one-time secret**
  that is printed once and can never be retrieved again (mirrors
  `data-app password`); the `git-credentials` list never returns it. `--type
  ssh_key` requires a `--public-key` / `--public-key-file` and returns no secret.

## `data-app` managed-repo deploy: omit configVersion (the platform injects clone creds) (since v0.65.0; guidance corrected v0.65.1 -- no credential wiring needed)

`--use-managed-git-repo` provisions an **empty** Keboola-hosted git repo
(POST `useManagedGitRepo:true`) linked to the app via `app.managedGitRepoId`. It
writes **no** `parameters.dataApp.git` block at create and **forces `--no-deploy`**
(an empty repo has nothing to run). It is mutually exclusive with `--git-repo` and
every `--git-*`/PAT flag.

The end-to-end flow that is **verified to deploy and serve** (tic-tac-toe on
`data-science.us-east4.gcp`):

1. `data-app create --use-managed-git-repo` -> app + empty repo.
2. `data-app git-credentials-create --type http_token --permissions readWrite`
   -> one-time secret (needs an admin storage token). This token authenticates
   **your** `git push` in step 3 -- nothing else.
3. `git push https://<token>@<managed-host>/.../app-<id>.git <local>:main`
   (token authenticates as the username, Gitea-style; or as the password with any
   username).
4. `data-app git-repo` introspects the repo immediately -- **unlike external
   repos, no prior deploy is needed** (managed resolves via `managedGitRepoId`,
   returns `is_managed_git_repo: true` + URLs).
5. `data-app deploy` -> clones + builds + runs. **No credential wiring needed:**
   the platform injects the `git clone` credentials at deploy time (per the
   sandboxes-service `tests/e2e/scripts/testManagedGitRepo.sh` contract), so a
   pure managed repo deploys straight from `app.managedGitRepoId`.

The actual bug fixed in 0.65.0 was **kbagent always-pinning `configVersion`**:

- **configVersion at deploy depends on the source location.** `data-app deploy`
  **omits** configVersion for a *pure* managed repo (no git block), which deploys
  from `managedGitRepoId`; it pins the **latest** Storage configVersion only when
  a git block is present (external repos). Pinning a managed app's no-git-block
  Storage config made the runtime demand `dataApp.git.repository` and the deploy
  reverted to `stopped` -- that was the misdiagnosed "could not read Username"
  symptom, NOT a missing credential. Fixed in 0.65.0 by omitting configVersion for
  pure managed repos. There is **no** cross-stack "platform doesn't inject
  credentials" gap (issue #454 closed as not-a-bug).
- **Diagnose a stopped-reverting deploy with `data-app runs`**, not `data-app
  logs`. `runs` returns each attempt's `failure_reason` + `startup_logs` --
  including setup-phase failures -- and works on never-started apps (where `logs`
  returns HTTP 400 "App is not running").
- **Historical note:** 0.65.0 briefly shipped a `data-app git-bind-credential`
  command on the misdiagnosis that a credential had to be wired into the config.
  It was removed in 0.65.1 -- it is unnecessary because no platform-injection gap
  exists.

## Core platform gotchas (version-independent)

These are Keboola-platform behaviors, not kbagent features, so they carry no
`since` tag -- they hold on every kbagent version. The `keboola-expert` agent
prompt keeps only a one-line trigger for each and links here for the full prose.

### `flow update --file` is a full replace of phases + tasks

`kbagent flow update` preserves the body on metadata-only updates (rename,
description). BUT `--file` is a **full-replace** of `phases` + `tasks` -- if your
YAML omits a phase, task, transition, or per-task `retry` that existed before, it
is silently dropped. For structural edits, always fetch via
`kbagent flow detail --json` first, merge your diff locally, run
`kbagent flow validate --file @merged.yaml --project ALIAS`, then push via `--file`.

### Primary keys on new output tables crash the first run

Keboola creates columns as nullable by default on first insert. A PK on a
nullable column crashes the first run. Pattern: strip PKs before first run,
run, restore PKs. Surface this to the user BEFORE they hit the crash.

### `source` vs `destination` in output mappings

`source` = the SQL alias your query creates (e.g. `SELECT ... FROM ... AS
my_out`, source is `my_out`). `destination` = the full storage bucket path
(`in.c-bucket.table`). Swapping them breaks the config SILENTLY -- no error at
save time, just garbage at runtime.

### Linked buckets exist only in the source project

`in.c-X` exists only in the SOURCE project; the destination project must
reference `out.c-X` of the local schema. If you see an input mapping
referencing `in.c-X` in a project that did not create the bucket, it is likely
a linked bucket and the reference needs rewriting to the local alias.

### Google Sheets Writer OAuth is not exportable

NOT exportable via API. On a cross-project migration, the user MUST manually
re-authenticate in the destination project UI. Flag this BEFORE starting the
migration, not after.

### `kbagent storage rename-table` does not exist

The Keboola Storage API does NOT support table renames. The only path is to
create a new table with the desired name and update all downstream configs
that reference the old name. Do NOT propose a rename in a plan -- it sets up
the user for an impossible step. Likewise `kbagent flow migrate` does not exist:
cross-project flow migration is a manual dance (`sync pull` source, edit,
`sync push` destination, or component-by-component via `config detail` +
`config new`).

### `column_metadata: {}` in a synced file is not authoritative

A `sync pull` without the right flags leaves column metadata empty in the local
JSON. That does NOT mean Keboola has no metadata -- always re-fetch via
`kbagent storage table-detail` when deciding about types.

### `Client` library: `query()` needs a provisioned workspace; `branch_id=None` costs a branch-list call (since v0.61.0)

The in-process library facade (`from keboola_agent_cli import Client`, 0.61.0+)
is a thin wrapper, not a workspace manager. Three non-obvious behaviors:

- **`query(workspace_id, sql)` does NOT create a workspace.** The `workspace_id`
  must already exist (make one via `kbagent workspace create` or the Storage
  API first). An unknown id surfaces the Query Service error verbatim.
- **`Client(url, token)` with no `branch_id` resolves the default branch lazily
  on the first `query()`** -- one extra `list_dev_branches` API call, cached
  after. Pass `branch_id=` to skip it (and to target a dev branch). Storage
  Files default to the production scope when `branch_id` is unset.
- **`query()` values are warehouse-serialized strings, NOT native types. (updated
  v0.61.1 -- closes #416)** The Query Service `/results` endpoint returns every
  Snowflake scalar as a JSON string -- `1` -> `"1"`, `1.5` -> `"1.5"`, `true` ->
  `"true"` -- with SQL `NULL` as `None`. The facade is transparent and does not
  coerce, so callers must cast (`int(row["x"])` etc.) for typed values. (Verified
  live against a Snowflake workspace; BigQuery behavior not yet verified.)

### `storage download-table` row filters send `whereValues[]` (array notation) (since v0.62.0)

`--where-column` + `--where-value` + `--where-operator eq|neq` and
`--changed-since` / `--changed-until` filter the export server-side. If you call
the raw Storage API instead of the CLI, the values parameter is `whereValues[]`
(WITH the bracket suffix), not `whereValues`; `whereColumn`/`whereOperator` and
`changedSince`/`changedUntil` are plain. Repeat `--where-value` for an OR set.
This is the credential-only, no-workspace way to pull a filtered/incremental
slice -- `workspace query` with a `WHERE` clause needs a live workspace.

### `storage add-column --not-null` needs an empty table or `--default` (since v0.62.0)

`storage add-column --column name:TYPE(length) [--not-null] [--default VALUE]`
hits the SYNCHRONOUS Storage endpoint (no job to poll). `--not-null` on a table
that ALREADY HAS ROWS is rejected by the backend with an API error (not a local
validation error) unless you also pass `--default` -- the existing rows need a
value for the new non-null column. Add `--default` when the table is non-empty.

### `job run --idempotency-key` is client-side dedup, scoped to one machine (since v0.63.0)

The Keboola Queue API `POST /jobs` accepts **no** client-supplied idempotency /
dedup token -- verified against the live OpenAPI spec (v1.3.8) and the server
source (an internal `deduplicationId` exists but is daemon-only, never read from
the public create-job request). So `kbagent job run --idempotency-key KEY`
de-duplicates **client-side**: a `<config-dir>/job_idempotency.json` map of
`key -> prior job`. Consequences to know:
- Dedup only works where that file is shared -- it is per config-dir / per
  machine. A replay from a *different* machine is NOT deduplicated.
- A prior run that is still running or finished non-failed is returned
  (`idempotent_replay: true`); a prior FAILED run is re-run; `--force-rerun`
  always creates fresh.
- Reusing a key for a *different* `--component-id`/`--config-id` exits
  `INVALID_ARGUMENT` (it refuses to return the wrong job), not silently.
- In-process SDK users get the same via `Client.run_job(idempotency_key=...,
  idempotency_store=JobIdempotencyStore(path))` -- the facade is config-dir-free,
  so you must supply the store path.

### The importable SDK is now typed (`py.typed` + result models) (since v0.63.0)

`keboola_agent_cli` ships a PEP 561 `py.typed` marker, so `mypy`/`ty`/IDEs treat
the in-process library as typed. The high-traffic facade methods return pydantic
models (`JobResult`, `QueryResult`, `UploadTableResult`, `ConfigDetailResult`,
`SyncPushResult`) exported from the package root, instead of bare dicts. Every
model is `extra="allow"`, so a new backend field never raises -- the *named*
fields are the stable, semver-versioned surface and extras stay reachable via
attribute access / `model_dump()`. They also accept the raw API key or the
snake_case field name, so `JobResult.model_validate(service_dict)` works on a
service-layer dict directly. This is a typing/contract addition only; the dict
shapes returned by the service layer and the `--json` CLI output are unchanged.

### `sync clone` needs a fresh target; flow/variable links remap automatically (since v0.63.0)

`sync clone` copies a reference synced tree into a **fresh** target project. Two
things to internalise:
- It does **not** reset config ids to placeholders. The reference's ids simply
  don't exist in the fresh target, so the push diff marks every config `added`
  and assigns new ULIDs. `created_id_map` (keyed by the reference id) then drives
  the Phase-C variable-link remap **and** the new Phase-D `keboola.flow`
  `task.configId` remap (reference→ULID). You do NOT need a separate
  "remap orchestrator/flow task" step — `sync push` now does it for ANY
  fresh-create push, not just clone.
- If the target project already contains configs with the reference's ids, clone
  **fails fast** (`CONFIG_ERROR`) rather than UPDATE them. Clone requires a
  new/empty target. Re-running clone with the same `--target-dir` is idempotent
  (`no_changes`), because after the first push the local manifest carries the new
  ULIDs that match the target remote.
- Override files (`--bucket-map` / `--variable-values` / `--instance-rename`)
  must be **flat `{id: scalar}` mappings** (since v0.89.0). A nested value —
  one fat-fingered colon away from valid YAML, e.g. `in.c-old:` followed by an
  indented `new: in.c-new` — is rejected with `CONFIG_ERROR` (exit 5) naming
  the offending key and its actual type. Older versions silently stringified
  it (`str(dict)` → `"{'new': 'in.c-new'}"`) and pushed that as a "bucket ID".

### `search --regex` matches entity names only; `matched_columns` is textual-only (since v0.67.0)

`kbagent search --regex` opts into the Storage API `mode=regex` global-search
path. Three things verified live against a real stack (2026-07-02):
- Regex is a **whole-term, case-insensitive** match — `report` will NOT match
  `monthly_report`; you must write `.*report.*`. An invalid pattern returns a
  clean 400 (`storage.globalSearch.invalidQuery`), surfaced as an API error.
- Regex runs over **entity names only**, not column names. So `matched_columns`
  is **always empty under `--regex`** — column matching is a textual-mode signal.
- `--regex` is textual-only; combining it with `--search-type config-based` is a
  usage error (exit 2 from the CLI; HTTP 400 from the `serve` REST `/search`).
- On a stack whose Storage API predates the regex mode, the unknown `mode=regex`
  query param may be silently ignored — the search then runs as plain fulltext
  over the literal pattern string (typically 0 results) with no error or
  warning. An unexpectedly empty `--regex` result on an older stack is NOT
  proof that nothing matches; retry without `--regex` to distinguish.

In plain textual mode, a `table` that matched via one of its column names carries
`matched_columns: ["col", ...]` in `--json` output and a populated "Matched
columns" column in the human table. The key is present on **every** result for
shape consistency and is empty (`[]`) when the table matched on its own name
(only column matches populate it). The human table adds
the "Matched columns" column **only when at least one result actually matched via
a column** -- a plain search that hit nothing by column shows no extra column.

## Sync trust cluster: --theirs reconcile, manifest<->disk invariant, isDisabled round-trip, never-fetched guard (since v0.72.0)

Four related sync-engine behaviors landed together (issues #466 / #467 / #472 / #497):

- **`sync pull --theirs` is the supported "discard local, take production" path.**
  It overwrites locally-modified configs AND rows, restores deleted/missing
  files, and resolves true merge conflicts by taking the remote version instead
  of aborting. No `.keboola/manifest.json` hand-editing is ever needed to
  reconcile a drifted tree. Works standalone (does not require `--force`).
- **Plain `sync pull` re-materializes missing local dirs.** A tracked config
  whose directory/`_config.yml` was deleted locally is re-fetched on the next
  pull even when the remote is unchanged (manifest<->disk invariant). The old
  behavior silently reported "Already up to date". NOTE the interplay with the
  GitOps delete flow: delete-dir-then-PUSH still deletes the remote config;
  delete-dir-then-PULL now restores it instead of doing nothing.
- **Config-level `isDisabled` round-trips.** Pull writes a sparse
  `is_disabled: true` line into `_config.yml` (absent key = enabled -- old trees
  do not mass-diff), `sync diff` surfaces enabled/disabled drift (a config
  disabled in production no longer looks "in sync"), and push sends the state:
  an explicit `is_disabled:` key in the local YAML updates the remote; an
  absent key leaves the remote state untouched. Same semantics for rows.
- **Never-fetched guard.** A manifest entry with an EMPTY `pull_hash` and no
  local files (a phantom left by a pre-0.72 name-collision pull, issue #472) is
  excluded from delete planning: `sync push --force` will NOT delete that
  remote config. diff/push report it under `never_fetched` (JSON key + human
  warning); the next `sync pull` materializes it. A properly-pulled config
  (non-empty `pull_hash`) that you delete locally is still planned as a remote
  DELETE on push -- the guard only protects entries that were never on disk.
- **Adopted-by-id push writes the manifest.** Pushing an untracked local file
  whose `_keboola.config_id` resolves on the target branch (the #482
  adopt-update path) now also creates the manifest entry (fresh
  `pull_hash`/`pull_config_hash`), so the very next `sync diff` reads a stable
  entry and a later local deletion is detected -- previously the config stayed
  untracked until the next pull (#497).
- **`sync status` wording.** The all-clear line now reads "No local changes
  detected ... Local check only -- run 'kbagent sync diff'": `status` never
  contacts the API, so it cannot see remote drift; treating it as a
  local-vs-production audit was the #466 trap.

## Table snapshots: restore is `tables-async`, `--name` is required, no overwrite (since v0.75.0)

`kbagent storage table-from-snapshot` (issue #512) creates a NEW table from an
existing snapshot. Three traps, all verified live (us-east4.gcp, 2026-07-22):

- **`--name` is REQUIRED.** The API rejects an omitted or empty name with
  `Table create option "name" is required and cannot be empty` -- the reference
  PHP client's "defaults to the snapshotted table's name" docblock is stale.
- **No overwrite semantics.** Restoring onto an existing table name fails with
  a duplicate-name error. The safe pattern: restore under a new name, verify
  the data, then `storage swap-tables` (or `delete-table` + re-restore) to
  promote it. The destination bucket must already exist.
- **It is not a `create-table` flag.** Restore goes through the classic
  `tables-async` import endpoint; `create-table` uses `tables-definition`,
  which does not accept `snapshotId` -- hence the dedicated command.

Lifecycle: `snapshot-create` (write; receipt carries `snapshot_id`),
`snapshots` (read, per table), `snapshot-detail` (read; global snapshot ID ->
embeds the source `table` object), `snapshot-delete` (destructive: forecloses
restores, source tables untouched; batch-tolerant, exit 1 on any failure).
Snapshot create and restore are async storage jobs -- the CLI polls to
completion, so the receipt's `table.rowsCount` is authoritative.

## The standalone binary never self-updates; recommend its own channel (since v0.79.0)

kbagent ships through two channels: a **Python distribution** (`uv tool install`
/ `pip`) and a **self-contained PyInstaller binary** with no Python runtime,
delivered by Chocolatey, WinGet, Homebrew, apt, dnf, or a signed zip. They are
upgraded in completely different ways, and the wrong advice is actively harmful.

- **Never tell a native-binary user to run `uv tool install` (or `pip install`,
  or `kbagent update`).** That does not upgrade the running binary at all -- it
  creates a SECOND, unrelated kbagent in the uv tool directory, which usually
  precedes the package manager's directory on `PATH`. The user silently starts
  running a different install than the one `choco` / `brew` / `apt` tracks,
  while the packaged binary stays stale. With no Python on the machine it just
  fails.
- **kbagent detects this itself now.** A frozen build is identified via
  `sys.frozen` / `sys._MEIPASS`, and the channel from the binary's own path.
  The startup auto-update hook prints a notification instead of reinstalling
  (throttled to once per version-cache TTL), and `kbagent update` refuses with
  the right command rather than running uv. This includes the deferred Windows
  helper from v0.78.0 -- the guard sits ahead of the `should_defer()` branch, so
  a frozen binary is never scheduled for an install it cannot receive either.
- **Read the channel out of `kbagent version --json`.** A frozen build carries
  two additive keys: `kbagent.install_channel` (`chocolatey` / `winget` /
  `homebrew` / `debian` / `rpm` / `system` / `archive`) and
  `kbagent.upgrade_hint` (always a human sentence). Both are **absent** on
  uv/pip installs -- so `install_channel` present is the reliable "this is a
  native binary" signal. `upgrade_command` stays runnable-or-empty: for
  `archive` and `system` there is no single correct command, so it is `""` and
  the sentence lives in `upgrade_hint`. **Never shell out to `upgrade_hint`**,
  and check `upgrade_command` is non-empty before running it.
- **`upgrade_command` is empty whenever `up_to_date` is not `false`** *(since
  v0.88.0)*. That covers `true` (nothing to upgrade to) and `null` (the release
  feed was unreachable, so kbagent cannot name a target). Before 0.88.0 it was
  built unconditionally, which made both states hand out a wrong command: a
  caller on a **pre-release** compares ahead of the stable release a
  non-`--beta` fetch returns (`0.44.0b1` >= `0.43.3`), so it read
  `up_to_date: true` next to a `--force --reinstall` command pinned to the
  OLDER stable wheel -- a silent downgrade off the beta. With `up_to_date:
  null` it fell through to an **unpinned `git+` source install**, resolving
  whatever the default branch happens to be, on the one path reached precisely
  because kbagent could not establish what the current release is. Gate on
  `up_to_date is False`, not on the string being present.
  Do not infer it from the version string; a frozen binary reports a perfectly
  normal version (PyInstaller bundles the dist metadata, so there is no
  `0.0.0-dev` tell).
- **Upgrade commands by channel:** `choco upgrade keboola-cli2` ·
  `winget upgrade Keboola.KeboolaCLI2` · `brew upgrade keboola-cli2` ·
  `sudo apt-get install --only-upgrade keboola-cli2` ·
  `sudo dnf upgrade keboola-cli2` · hand-unpacked archive -> re-download from
  the GitHub release page. Note the **package** is `keboola-cli2` while the
  **binary** is `kbagent`; the PyPI distribution is a third name, `keboola-cli`.

## `--json` is written as UTF-8, independent of the console codepage (since v0.78.0)

`--json` output no longer goes through the terminal's text encoder. It is
written straight to `sys.stdout.buffer` as UTF-8, so the bytes you parse never
depend on the active console codepage.

- **What this fixes (#546).** On a default Czech / Polish / Hungarian Windows
  console (cp1250), any non-ASCII character in the payload raised
  `UnicodeEncodeError` and killed the command -- an arrow in a flow name was
  enough to make `kbagent --json flow list` unusable. The crash hit the two
  pydantic paths (`model_dump_json` emits raw non-ASCII); `json.dumps` paths
  escaped to `\uXXXX` under its `ensure_ascii` default and so survived.
- **Covers all machine output**, not just the success envelope: the error
  envelope, the `kbagent --json agent run --stream` NDJSON event lines (which
  use `ensure_ascii=False` on purpose so event text stays readable), and
  `kbagent http`'s JSON printer.
- **Always decode as UTF-8.** Do not decode with `locale.getpreferredencoding()`
  on Windows -- that is cp1250 and will mangle or fail on the same characters
  the fix was written for.
- **Windows line endings changed for JSON only**: machine output now ends `LF`,
  not `CRLF`, because the binary buffer does no newline translation. Human
  (Rich) output is unaffected. No JSON/NDJSON parser cares, but a test that
  string-compares raw stdout bytes on Windows might.
- **Captured or replaced streams fall back to the plain text write** (there is
  no binary buffer to bypass), so in-process test harnesses behave as before.
- Human (Rich) output got the same guarantee later, by a different mechanism --
  see the next entry.

## Redirected human output is UTF-8 too, and no longer crashes (since v0.80.1)

When stdout is **not** a terminal, kbagent reconfigures it to UTF-8. So every
byte you capture from kbagent -- machine or human, `--json` or Rich table -- is
UTF-8 on every platform.

- **What this fixes.** Before v0.80.1, `kbagent semantic-layer --help` and
  `kbagent context` exited **1** with `UnicodeEncodeError` on Windows whenever
  their output was piped or redirected, and any Rich table truncated by width
  emitted a lone `0x85` byte for its ellipsis. An agent shelling out and
  capturing stdout got a crash or undecodable bytes; the same command typed
  into a terminal worked fine, which made it look unreproducible.
- **The split is terminal vs not, not which codepage is set.** Since PEP 528,
  CPython writes to a real Windows console through the console API and already
  reports `utf-8`, so interactive sessions were never affected. Only the
  redirected path fell back to the locale encoding (cp1252 / cp1250). Setting
  `chcp` changes nothing once stdout is a pipe.
- **Terminals are deliberately left untouched.** Forcing UTF-8 bytes at a
  console whose codepage is cp852 would replace a working display with
  mojibake. Do not "fix" this by forcing UTF-8 everywhere.
- **Always decode kbagent output as UTF-8**, never with
  `locale.getpreferredencoding()`. Same rule the `--json` entry above gives, now
  true for human output as well.
- **Redirected Windows output now matches POSIX byte for byte**, including the
  box-drawing characters Rich previously downgraded to ASCII when it thought the
  stream could not encode them. A test that string-compares captured human
  output across platforms will see them agree where they used to differ.
- **On kbagent older than v0.80.1**, the workaround is `PYTHONUTF8=1` or
  `PYTHONIOENCODING=utf-8` in the environment you spawn kbagent from.

## Two commands were broken on Windows until v0.80.1 (since v0.80.1)

Both failed on **every** invocation on Windows, and both worked fine on
macOS/Linux — so a failure report from a Windows user is not a project or
permission problem, it is the platform.

- **`kbagent job run --idempotency-key ...`** raised
  `PermissionError: [WinError 5] Access is denied` before recording anything.
  The dedup store locked the same file it then renamed over, and Windows
  refuses to replace a file that still has an open handle. On kbagent older
  than v0.80.1, omit `--idempotency-key` on Windows and dedupe on your side;
  the flag never persisted a single entry there, so nothing was ever replayed.
- **`kbagent storage download-table`** (and any sliced download) raised
  `PermissionError: [Errno 13] Permission denied`. A temp file was reopened by
  name while still open, which Windows forbids. No workaround below v0.80.1
  other than `storage unload-table --download`.

## `doctor` no longer warns about config-file permissions on Windows (since v0.80.1)

The `config_file` check compared POSIX mode bits against `0600`. Windows
reports `0o666` for any writable file regardless of its ACL, so the check
returned `"status": "warn"` permanently, for every Windows user, with no
possible remedy.

- **If you parse `kbagent --json doctor`**, that check now returns
  `"status": "pass"` on Windows, with a message ending
  `(POSIX mode bits not checked on Windows)`. An agent that treated any `warn`
  as actionable was previously reporting an unfixable problem on every run.
- **POSIX behaviour is unchanged** — a config file that is not `0600` still
  warns there, because there the bits mean something.

## Windows auto-update did not work at all from 0.78.0 to 0.80.1 (since v0.80.2)

If a Windows user reports being on an old kbagent, this is why: between 0.78.0
and 0.80.1 inclusive, the deferred self-update was scheduled and then never
applied. `kbagent update` printed `(scheduled)` and every later launch printed
"Updating in the background", while the version never moved.

- **It fails safe.** Nothing is corrupted (unlike the original #528); the
  install stays complete and usable. The update simply never arrives, and the
  24-hour single-flight marker suppresses retries in the meantime — so the
  banner keeps promising an update that cannot happen.
- **Do not tell a stuck user to run `kbagent update`** on an affected version.
  It takes the same broken path. The reinstall is the only way out:

      uv tool install --force --reinstall "keboola-cli @ https://github.com/keboola/cli/releases/download/v0.80.2/keboola_cli-0.80.2-py3-none-any.whl"

- **A version between 0.78.0 and 0.80.1 on Windows means the user has been
  stuck for a while**, and is missing every Windows fix from 0.80.1 —
  `job run --idempotency-key`, `storage download-table`, `doctor`, and
  redirected-output encoding all landed there.
- POSIX was never affected; it uses the inline install plus re-exec.

## Source files are read as UTF-8, not the host codepage (since v0.80.3)

`lineage build` reads `transform.sql` / `code.py` as UTF-8 regardless of the
machine's locale, and tolerates files that are not valid UTF-8.

- **Before v0.80.3** the reads used the platform default. On a Czech or Polish
  Windows box (cp1250) a single accented character in a SQL comment aborted the
  entire build with `UnicodeDecodeError`. Workaround on older versions:
  `PYTHONUTF8=1`.
- **A build is now reproducible across machines.** Previously the same synced
  tree could decode differently on Windows and on Linux, so a lineage graph was
  quietly host-dependent.
- **Undecodable bytes cost a comment, not the graph** -- `errors="replace"`.
  Table identifiers are ASCII, so references are never lost.
- **Data inputs stay strict**: `dev-portal create --data`, `patch
  --value-file`, and `semantic-layer reference-data set --members-file` read
  UTF-8 without a fallback, so a genuinely mis-encoded input file fails loudly
  and identically everywhere rather than being silently mis-parsed.

## `billing credits`: credits vs. minutes, array not object, feature-gated before it ever hits the network (since v0.84.2)

`billing credits [--project ALIAS ...]` is a read-only PAYG (pay-as-you-go)
balance check (`GET /credits` on `billing.{stack}`, plain Storage token).
Four things a coding agent will otherwise get wrong:

- **The API speaks credits; the Keboola UI speaks minutes.** `consumed` and
  `remaining` in the raw response are in PAYG credits. The CLI derives
  `consumed_minutes` / `remaining_minutes` as `credits * 60`
  (`MINUTES_PER_CREDIT` in `constants.py`) because that is the unit the UI
  actually displays. Always report the pre-computed `*_minutes` fields when a
  user asks "how many minutes do I have left" -- do not hand-multiply the
  credit fields yourself, and never assume the reverse conversion.
- **`stats.workspaceJobs` is an ARRAY, not the object shape the public
  Keboola docs show.** The real payload is
  `"workspaceJobs": [{"workspaceType": "sandbox-sql", "warehouseSize":
  "small", "consumed": 5.0}, ...]` -- one entry per workspace type/size
  combination, not a single object keyed by type. The CLI's `row.workspace_jobs`
  is already this list, projected to snake_case; do not try to read it as a
  dict.
- **`PAYG_NOT_AVAILABLE` is a feature-flag verdict, not a network failure.**
  Before calling the billing host at all, the service checks the
  `pay-as-you-go` flag in `owner.features` (`client.has_feature(PAYG_FEATURE)`).
  A project without that flag never makes a billing-host request -- on some
  stacks (e.g. a plain `eu-central-1` project) the `billing.{stack}` hostname
  does not even resolve (NXDOMAIN). Seeing `error_code: PAYG_NOT_AVAILABLE`
  in a project's `errors` entry means "this project isn't on PAYG," not "the
  billing service is down." A genuine connection/DNS failure past the
  feature gate is reported separately and mentions the host is unreachable.
- **Balance only -- no purchase history, no invoice IDs.** `billing credits`
  cannot answer "when did we last top up" or "what's our Stripe invoice ID."
  That data lives on `connection.{stack}` `/pay-as-you-go/billing/*`, which
  does not accept a Storage API token -- it is the still-open primary ask of
  issue #594. Do not imply this command covers billing/invoice history; tell
  the user it is out of reach from the CLI today.

## A Storage job that failed instantly used to report success (since v0.84.3)

Every async Storage operation -- table import/export, `create-table`,
`delete-table`, `truncate-table`, snapshot create/restore, dev-branch
create/delete, bucket share, workspace table load -- enqueues a Storage job and
waits on the same shared poller. Before v0.84.3 that poller checked for a
terminal state in **two** places, and they had drifted: the check on each
*polled* body raised `STORAGE_JOB_FAILED` on `status: error`, but the check on
the *initial* response body (the one the enqueuing POST/PUT/DELETE returns)
handed the job straight back to the caller.

So when the Storage API failed **fast** -- answering with an already-terminal
error instead of a queued `waiting` job -- the failure was swallowed. Every call
site returns either that job or `job.get("results", {})`, and an error job
carries no `results`, so the operation came back as an **empty success**: exit
code 0, no error output, nothing actually done. Jobs that failed after at least
one poll always raised correctly, which is why this never showed up in normal
use -- only fast failures were affected.

- **Since v0.84.3** the terminal state is evaluated once, at the top of the poll
  loop, so the initial body and every polled body travel identical code. A fast
  failure now raises `STORAGE_JOB_FAILED` like any other failed job.
- **If you have a script that treated an empty result as success**, it will now
  see the error it should always have seen. That is the intended change; check
  for `if not result:` style guards written against the old behaviour.
- **Nothing else moved**: wait budgets (`STORAGE_JOB_MAX_WAIT` 60 s,
  `IMPORT_JOB_MAX_WAIT` / `EXPORT_JOB_MAX_WAIT` 600 s), poll cadence, the
  up-to-one-interval deadline overshoot, error messages and exit codes are
  byte-identical.
- **Known narrowness, unchanged by the fix**: the poller recognises only
  `success` and `error`. Any other terminal status the Storage API might report
  would exhaust the whole budget and surface as `STORAGE_JOB_TIMEOUT`
  (`retryable: true`) rather than a failure -- so a wait that times out suspiciously
  fast against a small budget is worth reading as "unknown status", not "still
  running".

## `config update --set 'state...'` is now a hard error, not a silent no-op (since v0.84.2)

Before v0.84.2, `--set` on `config update` / `config row-update` applied every
path to `configuration` unconditionally. `state` is a **sibling** of
`configuration` in the Storage API response (see the `config detail
--with-state` entry above), not a child of it -- so
`--set 'state.storage.input.files[0].lastImportId=176200172'` silently wrote
to `configuration.state.storage...` instead. The command exited 0, bumped the
config version, and even showed a plausible-looking `--dry-run` diff, while
the actual runtime `state` was completely untouched (#593). A second,
independent bug compounded it: `set_nested_value` (`json_utils.py`) splits
paths on `.` only, so the `files[0]` segment became a literal dict key
`"files[0]"` rather than list index `0` -- the resulting structure wasn't
even shaped like a real state document.

- **Since v0.84.2**, any `--set PATH=VALUE` whose first dot-separated segment
  matches a known top-level field of the config detail response (`state`,
  `rows`, `name`, `description`, `id`, `version`, `currentVersion`,
  `changeDescription`, `created`, `creatorToken`, `isDeleted`, `isDisabled`)
  is **rejected before any network call**, exit code 2 (usage error). This
  fires under `--dry-run` too -- there is no way to preview your way past it.
- The error names the offending path and its first segment, and routes you to
  the right tool: `state.*` -> `kbagent config state-set`; `name` -> `--name`;
  `description` -> `--description`; `isDisabled` on a row -> `config
  row-update --is-disabled/--is-enabled` (the config-level flag is simply not
  settable via `--set`); anything else -> "not settable via --set". If a
  component genuinely has a `configuration.<prefix>` key with one of these
  names, pass the full body via `--configuration JSON|@file|-` instead of
  `--set`.
- **A plain `--set 'parameters.x=y'` is unaffected** -- only paths whose
  *first* segment collides with a sibling field are rejected; the guard does
  not touch anything under `configuration`.
- **`files[0]` bracket syntax is now also rejected**, by the same guard, with
  the same exit 2, before any network call -- with a message pointing at the
  `files.0` form (which already worked, and still does, over an existing
  list). It no longer silently creates a literal `"files[0]"` key. Note the
  example path at the top of this entry contains BOTH mistakes at once: a
  `state` first segment and a `files[0]` segment. Either one alone is now
  enough to be rejected.
- If you hit the new exit-2 error on a script written before v0.84.2 and the
  intent really was to edit runtime state, switch to `kbagent config
  state-set --state ...` (see [config-state-workflow](config-state-workflow.md))
  -- do not work around the guard by nesting the path differently.

## `changed_since: adaptive` with empty state reloads the ENTIRE file history (since v0.84.2)

This is a platform behaviour, not a kbagent bug, but it is the trap that
motivated `config state-set` (#593) and it is easy to hit by accident on a
dev branch. Keboola is retiring `processed_tags` / `query` in file input
mapping in favor of `changed_since: adaptive`, which tracks a
`lastImportId`-style checkpoint in the configuration's runtime `state`
(see the `config detail --with-state` entry above for what `state` is).

- **`adaptive` with an empty `state` does not mean "start watching from
  now"** -- it means "there is no checkpoint", so the component downloads
  the component's entire file history from the very beginning. On a real
  project this can mean years of files; one reported case was killed after
  118 seconds with thousands of files already downloaded and no end in
  sight.
- **A dev branch always starts with `state: {}`** (`branch create` gives you
  a fresh runtime state, not a copy of production's). So testing the
  `processed_tags`/`query` -> `changed_since: adaptive` migration on a branch
  -- exactly the safe place you'd want to validate it before touching
  production -- reproduces the full-reload behaviour every time, unless the
  branch's state is seeded first.
- **The fix is to seed, not to skip validation**: `kbagent config state-set
  --branch <dev-branch-id> --state '{"storage": {"input": {"files": [{"tags":
  ["<tag>"], "lastImportId": "<known-recent-id>"}]}}}'` before the first `job
  run` on the branch. Note `tags` is an ARRAY, matching the file input
  mapping's own selection criteria. Do not copy the field types out of this
  example -- run `state-get` against an already-migrated production config
  and mirror what it actually returns. See
  [config-state-workflow](config-state-workflow.md) for the full seed -> run
  -> verify -> merge sequence.
- **An empty state is more dangerous than a seeded one for this input
  mapping type.** For most incremental components an empty/cleared state is
  the deliberate "reprocess everything" reset and is the well-understood,
  documented behaviour. `changed_since: adaptive` inverts the usual safety
  assumption: here the empty state is the expensive, surprising path, and a
  seeded checkpoint is the conservative one. Do not assume "no state = safe
  default" when adaptive is involved.

## A failed POST/PATCH is no longer retried -- and the error now says what to do (since v0.86.0)

kbagent used to retry every request on `429/500/502/503/504`, regardless of
HTTP method. That is safe for a read and unsafe for a write, because a 5xx does
not mean "nothing happened":

- **Keboola's token mint persists the token row before the step that can
  throw.** `POST /v2/storage/tokens` saves the new token, generates its secret,
  and only then resolves bucket permissions -- and that last block is outside
  any transaction. A 500 raised there leaves a live token behind that the
  caller never saw, so three attempts could leave three of them. This is what
  [issue #599](https://github.com/keboola/cli/issues/599) reported from the
  `europe-west3.gcp` stack.
- The same hazard applies to every other non-idempotent call: `job run`,
  `job terminate`, `config new`, `flow new`, `config oauth-url` (which mints a
  token of its own), `data-app deploy`.

What changed:

- **5xx is retried only on `GET/HEAD/OPTIONS/PUT/DELETE`.** A `POST`/`PATCH`
  answered with a 5xx fails on the first attempt.
- **429 is still retried on every method** -- the server is stating it did not
  process the request, so repeating it is safe.
- **Transport failures are split by what they prove.** A refused connection
  (`ConnectError`) or a connect/pool timeout never delivered the request and is
  still retried on any method. A read/write timeout means the request was sent
  and the outcome is unknown, so it is not retried on a `POST`/`PATCH`.

Two consequences for an agent reading an error:

1. **`retryable: false` on a 500 from a write is deliberate, not a
   misclassification.** Do not paper over it with a retry loop of your own. The
   correct next step is to check what the server already did -- for a token
   mint, `kbagent token list`; for a job, `kbagent job list`.
2. **A 5xx message now carries the `exceptionId`** the Keboola API returns
   alongside its generic `"Application error."`, plus one of two hints: the
   request was not retried because the method is not idempotent, or the same
   5xx survived all 3 attempts and is therefore an upstream incident. Quote the
   `exceptionId` when escalating to Keboola support -- it is the only handle
   that traces back to the actual server-side exception.

## Retrying a merge-request `PUT` cannot double-apply the transition -- but it can mask a success as a 4xx (since v0.86.0)

`RETRY_SAFE_METHODS` treats every `PUT` as repeatable on a 5xx, and
`client/merge_requests.py` uses `PUT` for four action-style transitions
(`/request-review`, `/approve`, `/request-changes`, `/merge`). Those read as
commands rather than replacements, so the obvious worry is that a 500 raised
*after* the transition already committed would be retried and fire the
transition -- and its notifications -- a second time. Verified against the
`keboola/connection` source: it cannot.

**The second call is refused structurally, not incidentally.** The MR
lifecycle is a Symfony Workflow `state_machine`
(`MergeRequestLifecycleStateMachine`), and a `state_machine` transition is
enabled only from its declared `from` places:

| Retried call | State after attempt 1 | What attempt 2 gets |
|---|---|---|
| `/request-review` | `in_review` (or `approved` via `skip_review`) | `request_review` is enabled only from `development` -> **422** |
| `/request-changes` | `development` | enabled only from `in_review` / `approved` -> **422** |
| `/merge` | `in_merge` | `MergeProcessor` checks `can(MERGE)` (enabled only from `approved`) behind a per-project MySQL table lock -> **409** `storage.mergeRequests.notReadyToMerge` |
| `/approve` | `in_review` (self-loop) or `approved` | `AddApprovalGuard` blocks: "This reviewer has already approved this request." -> **422** |

`approve` is the only self-loop (`in_review -> in_review`), which is exactly
why it carries a dedicated guard instead of relying on the place structure.

**Notifications cannot double-fire either.** Emails, audit-log entries and
storage events are emitted by `TransitionListener` on
`workflow.merge_request_lifecycle.completed` -- that is, from inside
`apply()`, inside `MergeRequestService`'s `transactional()`. The side effects
are welded to the transition: no transition, no notification.

**What the retry does cost you: the error you see is attempt 2's.** The retry
loop surfaces the last response, so an operation that *succeeded* on attempt 1
and merely lost its response (a 500 after commit, or a read timeout) is
reported as `422 Cannot approve in current state` or `409 not ready to merge`.
Read a 4xx state conflict on a merge-request transition as "check the MR, this
may already be done", never as "it failed": fetch the MR and read its `state`
before repeating anything. The same reasoning applies to every retried
`PUT`/`DELETE` in kbagent, not only to merge requests.

**One residual race, server-side and not kbagent's to fix.**
`AddApprovalGuard` reads the approvals list, and
`MergeRequestService::approve()` inserts the row later in the same
transaction; `bi_rMergeRequestsApprovals` carries indexes but no unique
constraint on `(mergeRequestId, idAdmin)`. Two *overlapping* approve requests
from the same admin could therefore both clear the guard -- and
`hasEnoughApprovals()` counts rows, not distinct admins, so a project
requiring two approvals could reach `approved` on one human. kbagent can only
produce that overlap through a read-timeout retry (30 s read timeout, 1 s
backoff) racing a much shorter transaction, so the window is very narrow.
**Read this as reported, not proven:** the missing constraint and the
row-counting come off the schema and the source, but no duplicate row was
reproduced and the transaction isolation level was not checked -- it may be
latent rather than live. It is tracked upstream as
[keboola/connection#8209](https://github.com/keboola/connection/issues/8209);
check there before relying on any of it.
Nothing was changed in kbagent for it, and there is deliberately no
per-call-site retry opt-out: one auditable method rule beats a flag anyone can
flip on the wrong endpoint.

Note that the merge-request namespace is Layer 3 only today -- no
`services/` entry and no CLI command group -- so it is reachable from the SDK
client, not from a `kbagent <group>` command.

## `token list` is the only way to see what exists -- and it never shows secrets (since v0.86.0)

`kbagent token list --project P` (`GET /v2/storage/tokens`) closes the gap where
the `token` group could mint, revoke, and rotate but not enumerate -- so there
was no way to obtain the `--token-id` that `delete` / `refresh` require without
the web UI.

- **Secrets are stripped from every row, including under `--json`.** On a
  project carrying the `force-decrypted-token` feature the Storage API embeds
  each token's live value in the listing. kbagent removes it at the service
  layer (and in the SDK facade) before anything is rendered: `token create` /
  `token refresh` are the only reveals, and a listing that dumped live values
  would break that contract for every token in the project at once. Do not
  reach for `kbagent http get` to work around this.
- **It needs `canManageTokens` — but unlike `create` it does NOT need a master
  token** (since v0.89.0 the mint is master-guarded, issue #599). A plain
  Storage token gets a 403 -> `ACCESS_DENIED`.
- The master token appears in the listing with `isMasterToken: true` and cannot
  be deleted -- the API refuses.
- SDK parity: `Client.list_tokens() -> list[TokenListEntryResult]`, secrets
  stripped there too.

## A data app without `runtime.workspace.enabled` deploys green and reads nothing (since v0.87.0)

- **`data-app create` now grants Storage access by default.** The new
  `--workspace / --no-workspace` flag writes
  `runtime.workspace.enabled: true` into the `keboola.data-apps` config, and
  `--workspace` is the default. That flag is the **single** switch that makes
  the platform provision the app's ephemeral workspace and inject
  `WORKSPACE_ID`, `QUERY_SERVICE_URL` and `KBC_WORKSPACE_MANIFEST_PATH`. No
  flag, no env vars, no Storage access -- for reads *and* writes.
- **Before v0.87.0 kbagent never wrote it and offered no option to.** Every
  app created with `data-app create` on 0.86.0 or earlier that reads Storage
  has a dead data path unless someone hand-patched the config afterwards.
  If you are debugging an app created by an older kbagent, **check this
  first**:

  ```bash
  kbagent --json config detail --project P --component-id keboola.data-apps \
    --config-id <cfg> | jq '.data.configuration.runtime'
  # missing "workspace": {"enabled": true}  ->  this is your bug
  ```

- **The failure is silent and every platform-level signal is green.** The
  deploy succeeds, `setup_sh` completes, `state=running`,
  `desiredState=running`, the health probe passes. **The platform emits no
  diagnostic at all** -- so do not read "the app is running" as "the app can
  read data".
- **The authoritative check is the config, not the log.** The platform gives
  you nothing to grep for, so read the flag directly:

  ```bash
  kbagent --json config detail --project P --component-id keboola.data-apps \
    --config-id <cfg> | jq '.data.configuration.runtime'
  # no "workspace": {"enabled": true}  ->  this is your bug
  ```

- **Any log line about the missing env var comes from the APP, not the
  platform.** An app that checks its own environment may say something like:

  ```
  [startup] initial cache load failed: Missing env vars: WORKSPACE_ID (or KBC_WORKSPACE_MANIFEST_PATH)
  ```

  That exact wording is the ai-kit DuckDB-cache template's. An app built from
  anything else may log a different message, a generic query error, or
  **nothing at all** -- so a `data-app logs` grep that comes back empty
  proves nothing, and must NOT be read as ruling the workspace flag out.
  Check the config (above) before believing a quiet log.
- **Which of the two bad outcomes you get depends on the app's own code.** An
  app that catches the failure serves an empty dashboard -- all zeros, no
  error banner. An app that does not crash-loops behind the health probe,
  which is *more* visible and therefore the better of the two. Defensive
  coding is currently punished.
- **Still worth reading `data-app logs` when a data app returns empty
  results**, not just when it fails to start -- but as a *possible* clue, with
  the config check as the decider. Nothing about this surfaces through
  `data-app detail`, `data-app runs`, or the job status.
- **`--no-workspace` omits the key rather than writing `enabled: false`.**
  The request body is byte-identical to 0.86.0, so an app deliberately
  created without Storage access carries no new config noise. Use it only for
  an app that never touches Storage.
- **The block is a SIBLING of `runtime.backend`, not a replacement.** The
  correct shape (matching what the UI and MCP's `modify_python_js_data_app`
  both write) is:

  ```json
  "runtime": {
    "backend":   {"size": "small", "type": "e2bSandbox"},
    "workspace": {"enabled": true}
  }
  ```

- **Retrofitting requires a redeploy, not just a config update.** A config
  change alone never reaches the running container:

  ```bash
  kbagent config update --project P --component-id keboola.data-apps \
    --config-id <cfg> --merge --set 'runtime.workspace.enabled=true'
  kbagent data-app deploy --project P --app-id <ID> --wait
  ```

  `data-app deploy` pins the LATEST config version, so the plain form is
  correct here -- do not pass `--config-version`.
- **Not gated on any project feature.** The `data-apps-storage-workspace`
  project flag is being enabled everywhere and then removed, so the config
  option is the sole control. kbagent deliberately does **not** implement
  MCP's legacy fallback of injecting `WORKSPACE_ID` through
  `parameters.dataApp.secrets` (`keboola-mcp-server` still does that when the
  feature is off) -- do not add it, and do not expect to see it.
- **`--json` reports it:** the create envelope gains a `workspace` boolean,
  and the human output prints the Storage-access state on every create --
  loudly when it is off. The REST surface mirrors the flag as
  `"workspace": true` in the `POST /data-apps/{project}` body.

## `token list --with-last-used`: `never` and `unknown` are not the same answer (since v0.88.0)

`--with-last-used` derives each token's most recent activity so you can tell
dormant tokens from live ones before revoking anything. Storage token payloads
carry no `lastUsed` field (only the Manage API's PAT response does), so the
value comes from each token's own event feed -- **one extra API call per
token**, which is why the flag is opt-in. Never add it to a `token list` whose
only purpose is finding a `--token-id`.

- **Read `lastUsedStatus`, never bare `lastUsed`.** Four values:
  - `used` -- `lastUsed` is a real timestamp.
  - `never` -- **proven** never used: minted inside the ~6-month event-retention
    window with no activity of its own. This is the finding worth surfacing; it
    usually means a mis-provisioning or an onboarding step that failed silently.
  - `unknown` -- older than retention, so the API genuinely cannot say. Do NOT
    report this as "never used"; it is exactly the population someone is about
    to revoke.
  - `error` -- that token's lookup failed. The row degrades and the error lands
    in the top-level `errors`; the rest of the audit still completes.
- **Dev-branch activity is invisible.** `GET /v2/storage/tokens/{id}/events` is
  not branch-addressable and always resolves to the default branch, narrowing to
  `idBranch == <production> OR NOT EXISTS idBranch`. A token used only inside a
  development branch therefore reads as dormant. On a project that uses branches,
  check `branch list` before recommending a revocation.
- **Do not hand-roll this against the raw endpoint.** The feed mixes events the
  token *performed* with events performed *on* it, so a fresh token's newest raw
  event is its own `storage.tokenCreated` and `events[0]` reports a never-used
  token as "used today" -- backwards. kbagent narrows server-side with
  `q=token.id:{id}`. Filtering client-side is not equivalent: right after a
  rotation the single event a `limit=1` fetch returns is that rotation, leaving
  the filter with nothing and calling an active token unused.
- `lastUsedEvent` distinguishes human traffic (`storage.tablesListed`,
  `storage.tableDetail`) from agent traffic
  (`ext.keboola.mcp-server-tool.*`) -- useful when deciding whether a token
  belongs to a person who left or to an automation still running.
- The default `token list` shape is unchanged: without the flag none of these
  keys appear and no `errors` list is added.
- SDK parity: `Client.list_tokens(with_last_used=True)`; the fields land on
  `TokenListEntryResult` as `last_used`, `last_used_event`, `last_used_status`.

## `token list --columns` is human-output only (since v0.88.0)

`--columns` (repeatable, e.g. `--columns description --columns last_used`)
selects *and orders* the Rich table. `Refreshed` is now part of the default
column set -- `--json` always returned it, the fixed table just had no way to
show it.

- **`--json` is deliberately unaffected.** The machine contract stays whole no
  matter what the human view is narrowed to, so never use `--columns` to shape
  data you are about to parse.
- Valid names: `id`, `description`, `created`, `refreshed`, `expires`, `master`,
  `created_by`, `last_used`, `last_used_event`. An unknown name exits 2 and
  prints the valid list.
- An explicit `--columns` is taken literally -- combining it with
  `--with-last-used` does *not* silently append the derived columns. Name
  `last_used` yourself if you want it.
- **`last_used` / `last_used_event` REQUIRE `--with-last-used`** and exit 2
  without it. They are derived per token, so with no derivation there is
  nothing honest to render -- and the alternative (a blank or a default
  label) would state something about data nobody fetched.
## `--include-usage` counts mappings, not SQL text (since v0.88.0)

`storage tables --include-usage` reports a table as used only when a
configuration names it in `storage.input.tables[].source` or
`storage.output.tables[].destination`. A transformation whose SQL says
`SELECT * FROM in.c-main.orders` but whose input mapping does not list the
table is **not** reported.

That is deliberate -- the question the column answers is "what breaks if this
table changes?", and matching free text would answer it with false positives.
It also means the column is not a complete "is this string anywhere?" audit:
for that, use `search --search-type config-based` (which scans whole bodies),
optionally narrowed with `--scope`.

Matching is case-insensitive but whole-id: `in.c-main.order` does not match
`in.c-main.orders`.

## `sharing link` does not inherit the source bucket's stage (since v0.88.0)

`--stage` defaults to `in`, the stage kbagent has always used. MCP's
`link_shared_bucket` instead derives the stage from the source bucket, so the
same link lands in different places depending on which tool you drive. If you
are porting an MCP call that relied on the derived stage, pass `--stage out`
explicitly for an `out.*` source.

## `job detail` needs `--log-tail-lines` to show logs (since v0.88.0)

The log tail is off by default so a plain `job detail` stays one API call.
`job run --wait` still attaches a tail on its own for terminal failures --
that behaviour is unchanged, and its `--log-tail-lines` is capped at the same
maximum as `job detail`'s.

## A scaffolded `keboola.flow` config can now be pushed from disk (since vNEXT)

`config new --component-id keboola.flow` (no `--push`) used to write a
`_config.yml` with no `_keboola` block at all -- every other scaffold
category (extractor/writer, SQL/Python transformation, custom Python app)
always got `_keboola: {component_id: ...}` appended, but the flow builder
(`_build_flow_config_yml` in `services/component_service.py`) never emitted
one. `sync push` resolves the component of an untracked local config from
`_keboola.component_id` (`_find_untracked_configs` in `sync_service.py`);
without it the config resolved to `"unknown"`, so a scaffolded flow could
never be pushed via the documented scaffold -> edit -> `sync push` workflow
(issue #650). Fixed by appending the same footer the other categories get.
No CLI behavior change beyond the file content -- if you were hand-patching
scaffolded flow files with a `_keboola` block as a workaround, that step is
no longer necessary.
