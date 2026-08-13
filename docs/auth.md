# Browser login — `kbagent auth`

`kbagent auth login` signs you in to a Keboola stack through a real browser
instead of a pasted Storage API token. The result is a **programmatic
session**: a short-lived access token (`kbc_at_*`) plus a rotating refresh
token (`kbc_rt_*`) that kbagent renews for you (since v0.80.0).

> **Read this first: `auth login` needs a human at a browser.**
>
> There is **no headless or unattended path for `auth login`**. It opens a
> browser window, or prints a code you type into a page on another device. An
> AI agent must never run it on its own initiative — if asked to "set up
> kbagent auth", hand the command back to the person and wait for them to
> finish.
>
> For CI, containers, cron, or any other unattended context, you have two
> options: a **static Storage token** (`kbagent project add --token ...`, or
> the token-only `KBAGENT_PROJECT_FROM_ENV=1` + `KBC_TOKEN` +
> `KBC_STORAGE_API_URL` path -- unaffected by anything on this page), or
> **`kbagent auth login-password`** (since v0.81.0) if you specifically need a
> full USER-scoped session rather than a single project's token -- see
> [section 2b](#2b-auth-login-password-the-unattended-exception) below. It is
> the one deliberate exception to "no unattended path": it needs an account's
> password (and TOTP seed, if MFA is on) as CI secrets, not a browser.

## TL;DR

```bash
# 1. Sign in to a stack (a browser opens; finish the login there)
kbagent auth login --stack https://connection.keboola.com

# 2. Pick which projects the session should register locally
kbagent auth register-projects            # interactive picker
kbagent auth register-projects --all      # or non-interactively

# 3. Use them like any other project
kbagent project list                      # the Auth column says "session"
kbagent storage buckets --project my-project-9840

# 4. Check on / tear down the session
kbagent auth status
kbagent auth logout --remove-projects
```

Everything else on this page is about the parts that are **not** symmetric with
a static token: which commands work, what the errors mean, and what you are
accepting when you serve a session project over HTTP.

---

## 1. Two credential models, one config

The credential type is a property of **each project entry**, not of the stack
and not a global mode. It lives in that entry's `token` field in `config.json`:

| | Static token | Browser-login session |
|---|---|---|
| `config.json` `token` field | the real Storage token | the sentinel `kbc-session://{project_id}` |
| Where the live credential lives | that same field | `auth.json`, keyed by stack URL |
| Sent to the API as | `X-StorageApi-Token: <token>` | `Authorization: Bearer <access token>` |
| Renewal | you rotate it (`project refresh`) | automatic refresh-token rotation |
| Identity | the token's own scope | the **user** who logged in |
| Registered by | `project add`, `org setup` | `auth login --register-projects`, `auth register-projects` |
| `project list` `Auth` column | `static` | `session` |

One `config.json` can freely mix both. `make_client_factory`
(`services/base.py`) is the single branch point and decides **per project** on
every command, so a static-token project never touches any of the session
machinery: no `auth.json` read, no refresh, no bearer header.

### There is no fallback from session to static

A session-registered project has no static token stored anywhere; the sentinel
*occupies* that field. So a code path that only understands static Storage
tokens cannot quietly fall back — it fails with
`AUTH_NOT_SUPPORTED_ON_STACK` (see [section 4](#4-what-works-on-a-session-project)).
This is deliberate: the alternative is sending the literal string
`kbc-session://9840` to an API as if it were a credential, which yields an
opaque 401 at best and a persisted garbage credential at worst.

The reverse direction *is* possible, because it is an explicit request — see
[section 6](#6-converting-a-session-project-to-a-static-token).

---

## 2. The commands

### `auth login`

```bash
kbagent auth login [--stack URL|ALIAS] [--device-code] [--register-projects]
```

- **The stack is resolved, never discovered.** `--stack` accepts a stack URL
  (`https://connection.keboola.com`) or an existing project alias, whose stack
  URL is then used; an alias is matched first, since aliases are an exact,
  closed set. Without `--stack`, the default project's stack is used. With
  neither, the command fails and names the fix.
- **PKCE authorization-code flow by default.** A loopback listener on
  localhost receives the callback after you approve in the browser.
- **The device flow is the fallback**, and it is chosen automatically when a
  same-machine browser cannot work: a remote SSH session (`SSH_CONNECTION` /
  `SSH_TTY`), a container (`/.dockerenv` or `$container`), WSL without a
  working `wslview`, or no browser opener at all. The reason is printed. It
  also takes over if PKCE fails **before** the code exchange (bind failure,
  browser could not open, callback timed out). `--device-code` forces it.
- On the device flow the verification URL and short code are printed — to
  stderr under `--json`, so piping stdout still leaves the human something to
  read. Session tokens themselves are never printed.
- **The new session is persisted before the one it replaces is revoked**, so an
  interruption can never leave you with no usable credential.
- Right after a successful login, kbagent offers the project picker described
  below (TTY, non-`--json`), or prints a one-line hint pointing at
  `auth register-projects`.

### 2b. `auth login-password` -- the unattended exception

```bash
kbagent auth login-password --email EMAIL --password PASSWORD [--totp-secret SECRET] \
  [--stack URL|alias] [--register-projects]
```

The one command in this whole page that IS safe for a CI job or an agent to
run non-interactively -- because it needs credentials handed to it, not a
browser. A password grant, straight to the auth service, no loopback
listener, no user interaction of any kind.

- **`--email` / `--password` / `--totp-secret`** also read from
  `KBC_LOGIN_EMAIL` / `KBC_LOGIN_PASSWORD` / `KBC_LOGIN_TOTP_SECRET` env vars
  (the exact convention `KBC_TOKEN` already uses), so a workflow sets them
  once in a step's `env:` block instead of passing flags:
  ```yaml
  - name: Sign in
    env:
      KBC_LOGIN_EMAIL: ${{ secrets.KBC_LOGIN_EMAIL }}
      KBC_LOGIN_PASSWORD: ${{ secrets.KBC_LOGIN_PASSWORD }}
      KBC_LOGIN_TOTP_SECRET: ${{ secrets.KBC_LOGIN_TOTP_SECRET }}
    run: kbagent auth login-password --register-projects
  ```
- **`--totp-secret` is the account's base32 TOTP *seed*** -- the same string
  an authenticator app scans from the enrollment QR code, not a live 6-digit
  code. kbagent computes the current code itself (`auth/totp.py`, plain
  stdlib RFC 6238 -- no dependency added) at the moment it calls the login
  endpoint. Nobody types a live code; the seed is the only secret involved.
- **Only TOTP-based MFA can be resolved this way.** If the account's MFA
  factor is WebAuthn/passkey instead, there is no shared secret to compute
  a response from -- a WebAuthn ceremony is a live cryptographic exchange
  that can only run in a real browser holding the actual passkey/security
  key, a hard constraint of the protocol, not a missing feature here -- this
  command fails fast with `AUTH_MFA_INVALID` naming `auth login` as the
  fallback for that account.
- The resulting session is stored in `auth.json` and, from here on, shares
  the **same mechanics** as a browser-login session: same bearer dispatch,
  same refresh rotation, same `--register-projects` contract, same
  `project list` `Auth` column (`session`), same
  [section 4](#4-what-works-on-a-session-project) restrictions. Its
  **privilege** is not always the same -- see the next point.
- **For an MFA-enabled account, this session carries a live 3-hour "sudo"
  window that a browser-login session usually does not.** The password
  flow completes MFA and creates the session in one server-side step
  (`createSessionAfterMfa`), which stamps the sudo timestamp unconditionally;
  PKCE/device instead inherit whatever sudo state the browser session
  already had, which is typically stale or absent. Sudo gates exactly the
  account-takeover-shaped operations on the Connection UI/API (PAT
  create/revoke, TOTP delete, WebAuthn delete/register, recovery-code
  regeneration, revoke-all-sessions) -- none of which kbagent itself calls,
  but any script holding this session's tokens effectively can for the next
  3 hours. Treat the CI secrets backing `login-password` accordingly.
- **Two CI jobs must not share one MFA-enabled account within the same
  30-second window.** The server accepts each TOTP code exactly once; a
  second `login-password` call submitting a code for the same time slice
  fails outright, and a 429/5xx retry never resubmits a stale code either
  (see the code-level note in `auth/auth_client.py`). Give concurrent
  matrix-build legs their own service account, or serialize the login step.
- **Security posture matters here more than for a single project's token.**
  A password (+ TOTP seed) is the account's full ambient identity, not a
  scoped credential -- whoever holds these CI secrets can do anything that
  account can do, everywhere it has access, not just one project's Storage
  routes. Use a dedicated, least-privileged service account created
  specifically for this pipeline; never a real person's own login. Revoking
  access means changing that account's password (and re-enrolling MFA), not
  a lightweight per-secret revoke.

### `auth register-projects`

```bash
kbagent auth register-projects [--stack URL|ALIAS] [--all] [--project-id ID ...] \
                               [--alias ID=ALIAS ...] [--yes]
```

Registers projects the session can see as local aliases. Run it any time after
a login — you do not have to remember `--register-projects` at login time.

| Invocation | Selection |
|---|---|
| `--all` | Every accessible project. Mutually exclusive with `--project-id`. |
| `--project-id ID` (repeatable) | Only those ids. An id the session cannot access is an error, not a silent skip. |
| Neither | Interactive checkbox picker (arrow keys, `space` to toggle, `a` for all, `enter` to accept). Requires a TTY without `--json`. |

`--all` and `--project-id` are the non-interactive forms, so **this** command
is safe for an agent to run once a human has logged in. Suggested aliases are
the project name slugified and suffixed with the project id, so two projects
sharing a name still get distinct, usable aliases; `--alias ID=ALIAS`
overrides. An already-registered project is reported as `exists` and left
alone — nothing overwrites an alias you already have.

### `auth status`

```bash
kbagent auth status [--stack URL|ALIAS]
```

| Status | Meaning | Exit code |
|---|---|---|
| `live` | Access token still valid | 0 |
| `refreshed` | Access token was renewed during the check | 0 |
| `degraded` | The auth service was unreachable; on-disk expiry data is being reported instead | 0 |
| `expired` | The refresh token expired or was revoked | 3 |
| `missing` | No session is stored for this stack | 3 |

`degraded` exits 0 on purpose: a network blip must not be reported as a dead
session. Scripts can branch on the exit code without parsing `--json`.

The output also lists the projects the session can access, and any **orphaned
server sessions** — sessions whose remote revoke did not confirm; `auth logout`
retries them.

### `auth logout`

```bash
kbagent auth logout [--stack URL|ALIAS] [--remove-projects] [--yes]
```

Local credentials are **always** cleared, even when the remote revoke fails or
is uncertain; that outcome is reported distinctly rather than dressed up as a
clean success. `--remove-projects` additionally drops the local aliases this
session registered — projects you later converted to a static token are left
alone, since they no longer belong to the session
([section 6](#6-converting-a-session-project-to-a-static-token)).

Because `--remove-projects` deletes project entries, it needs the `admin`
permission class, while the bare `auth logout` needs only `write`. A policy that
denies `cli:admin` to keep an agent out of the project registry still lets it end
its own session.

Logout clears the whole per-stack record, so an orphan it could not revoke is
also **forgotten locally** — the last place it is reported is that logout's own
output (`orphans_remaining` in `--json`). A session listed there is still live on
the server until its refresh token expires; end it from the Keboola UI if that
matters. Repeated logins do not have this problem: each one carries the
outstanding orphan list forward, so `auth logout` still retries every one of
them.

---

## 3. Where things live on disk

Both files sit in the same config directory — `~/.config/keboola-agent-cli/` by
default, or whatever `--config-dir`, `KBAGENT_CONFIG_DIR`, or a project-local
`.kbagent/` resolves to (see [User Guide](guide.md)):

| File | Contents | Mode |
|---|---|---|
| `config.json` | Projects, aliases, defaults. Session projects hold the sentinel here. | `0600` |
| `auth.json` | The live sessions, keyed by stack URL. | `0600` |
| `auth.json.lock` | Sidecar lock guarding each read/write of `auth.json`. Concurrent refreshes are serialised by a lease recorded inside `auth.json` itself, so this lock is never held while a request is in flight. | — |

The sentinel is an ordinary string in an existing `config.json` field, so the
file's schema and `CURRENT_CONFIG_VERSION` accommodate it as they are and any
kbagent build can still load it. Such a build simply sees a project whose token
looks odd, and every path that would spend it as a credential refuses to.

`auth.json` is re-tightened to `0600` if another process widens it.

---

## 4. What works on a session project

| Area | On a session project |
|---|---|
| Storage API — `storage`, `config`, `job`, `flow` (read/list), `branch`, `workspace`, `search`, `sync`, `transformation` | Works, over bearer auth, including refresh rotation and a single 401 retry |
| Manage API — `project` (members, invitations), `org`, `feature`, `sharing` (project-token path) | Works |
| `kbagent serve` (REST API + Web UI) | Works, for the Storage and Manage paths — with the accepted risks in [section 5](#5-session-projects-in-kbagent-serve) |
| `kai` | `AUTH_NOT_SUPPORTED_ON_STACK` |
| `semantic-layer` (Metastore Service) | `AUTH_NOT_SUPPORTED_ON_STACK` |
| `data-app` (Data Science Service) | `AUTH_NOT_SUPPORTED_ON_STACK` |
| `stream` (Data Streams Service) | `AUTH_NOT_SUPPORTED_ON_STACK` |
| `tool` (MCP server subprocess) | `AUTH_NOT_SUPPORTED_ON_STACK` |
| AI Service paths — `docs query`, `config examples`, `config new`, `component detail`, `component list --query`, `flow new` / `update` / `validate --project` | `AUTH_NOT_SUPPORTED_ON_STACK` |
| Scheduler Service paths — `flow schedule`, `flow schedule-remove` | `AUTH_NOT_SUPPORTED_ON_STACK` |
| `sharing`, when it needs a master token | `AUTH_NOT_SUPPORTED_ON_STACK` unless a master token is in the environment |
| The importable SDK (`keboola_agent_cli.Client`) | `AUTH_NOT_SUPPORTED_ON_STACK` — construct it with a static token ([Python SDK](sdk.md)) |

`SESSION_UNSUPPORTED_FEATURES` in `services/_auth_registration.py` is the
in-code version of this list. `auth login` and `auth register-projects` print it
once they have registered something, and carry it as
`session_unsupported_features` in `--json`, so you learn the restrictions up
front rather than at first use. `auth status` does not carry that field.

Notes worth knowing before you hit them:

- **`dev-portal` is unaffected.** It authenticates with its own Developer
  Portal identity (`dev-portal identity add`), not with a project token, so a
  session project changes nothing there.
- **`flow` splits.** `flow list` / `flow detail` are plain Storage calls and
  work. `flow new` / `flow update` / `flow validate --project` fetch the live
  schema from the AI Service, so on a session project they fail rather than
  falling back to the semantic-only validation they use when the schema fetch
  merely errors.
- **`config` mostly works; `config new` depends on its flags.** `config list`,
  `detail`, `search`, `update`, the row and metadata subcommands and
  `variables-*` are pure Storage calls. `config new` builds its scaffold from the
  component schema fetched from the AI Service, and the scaffold is only skipped
  for `--push --no-files` (`commands/config.py:1335`) — so the default
  scaffold-writing form fails on a session project no matter what
  `--no-validate` says. `config new --push --no-files` stays on the Storage path
  as long as validation does not fire: pass `--no-validate`, or omit an explicit
  `--configuration` body, which auto-skips it. Otherwise write the config with
  `config update` (or `sync push`).
- **The failure is immediate and typed**, not an opaque 401 from the service:
  the guard fires before the client is even constructed and names the feature
  it refused.

---

## 5. Session projects in `kbagent serve`

`serve` supports session projects because it never turns a project into
credentials itself — every service in its registry resolves its own client
factory, so the REST surface inherits exactly the CLI's behaviour, bearer
support and fail-fast guards alike.

Two properties come with that, and both are **consciously accepted** in
exchange for being able to drive a session-backed project from the web UI at
all: the serve token borrows a user identity (whoever holds
`KBAGENT_SERVE_TOKEN` acts as the signed-in Keboola user for as long as the
session lives), and refresh-token rotation was designed for short CLI
invocations rather than a daemon up for weeks. Both are spelled out, with what
to do about them, in
[`kbagent serve` > Session-registered projects](web-server.md#session-registered-projects)
— read that before exposing a session project over HTTP. For a project you
would rather not expose this way, register it with a static Storage token; that
path has neither property.

How a credential failure reaches a REST caller:

| Condition | HTTP | `error_code` |
|---|---|---|
| A static-token-only path was handed a session project | 400 | `AUTH_NOT_SUPPORTED_ON_STACK` |
| The session expired or was revoked while the server ran | 401 | `SESSION_EXPIRED` |
| No session is stored for the project's stack | 401 | `SESSION_NOT_FOUND` |

The 400 lines up with exit code 5 on the CLI — same code, same meaning, over
either surface. The two 401s are the caller's authentication problem rather
than an upstream fault, and the server cannot clear them on its own: a browser
login only completes where a human sits, so the remedy names
`kbagent auth login` **on the host running serve**.

---

## 6. Converting a session project to a static token

Sometimes you want a project registered by browser login to keep working
somewhere sessions do not reach — a scheduled agent, a CI step, the MCP
subprocess. Give that project a static token explicitly:

```bash
kbagent project edit --project my-project-9840 --token YOUR_STATIC_TOKEN
```

This is allowed and it warns while doing it: the project becomes a
static-token project, so `kbagent auth logout --remove-projects` will no longer
clean it up — remove it with `kbagent project remove` when you are done.

What will **not** convert it behind your back:

- `kbagent project refresh` and `kbagent org setup --refresh` **skip** session
  projects and say why ("nothing to refresh; session access tokens rotate
  automatically"), including under `--force`. A bulk maintenance command never
  changes a project's credential type.
- Any other path that would overwrite the sentinel with a token is rejected
  with `AUTH_NOT_SUPPORTED_ON_STACK`.

To see which is which at a glance: `kbagent project list` and
`kbagent project status` carry an `Auth` column, `kbagent project info` an
`Auth` row, and `--json` output an `auth_mode` field (`session` / `static`) on
every project entry. In the human tables a session project's `Token` cell is a
dash, so a masked sentinel never reads as a plausible truncated credential.

---

## 7. Errors and what to do about them

`--json` output carries a stable `error_code`; branch on that, never on the
message text. Full catalogue:
[Error Code Reference](error-codes.md#programmatic-auth-browser-login).

| Code | What happened | What to do |
|---|---|---|
| `AUTH_NOT_SUPPORTED_ON_STACK` | Either the stack has no browser login, or a static-token-only path was handed a session project | Use a static Storage token for that project or that command ([section 4](#4-what-works-on-a-session-project)) |
| `AUTH_BROWSER_UNAVAILABLE` | No usable browser for the loopback flow | Nothing to do: the no-browser case degrades to the device flow rather than failing, so this code rarely surfaces. `--device-code` skips the loopback attempt entirely |
| `AUTH_FLOW_TIMEOUT` | The callback or device poll deadline elapsed | Re-run `auth login` and complete the browser step |
| `AUTH_FLOW_DENIED` | You (or the authorization server) declined the request | Re-run and approve, or check your stack permissions |
| `AUTH_FLOW_EXPIRED` | The device code / authorization code expired unused | Re-run `auth login` |
| `AUTH_STATE_MISMATCH` | The PKCE callback's `state` did not match the one issued | Re-run `auth login`; if it repeats, something is intercepting the callback |
| `SESSION_EXPIRED` | The refresh token expired or was revoked | `kbagent auth login` again — on the host, if this came from `serve` |
| `SESSION_NOT_FOUND` | No session is stored for this stack | `kbagent auth login --stack <url-or-alias>` |
| `AUTH_MFA_INVALID` | `auth login-password` hit an MFA factor it cannot resolve (e.g. WebAuthn-only) | Use `kbagent auth login` for that account instead |

In a multi-project command, a per-project failure appears in the `errors` array
of the result envelope with its own `error_code`, so one session project cannot
mask the outcome for the static ones alongside it.

The same codes reach a REST caller of `kbagent serve` with the HTTP statuses in
[section 5](#5-session-projects-in-kbagent-serve).

---

## See also

| Document | What it adds |
|---|---|
| [README](../README.md#setup-options) | The four ways to register projects, side by side |
| [User Guide](guide.md) | Config directories, per-directory isolation, the permission firewall |
| [Tutorial](TUTORIAL.md) | End-to-end walkthrough of registering projects and installing the plugin |
| [Error Code Reference](error-codes.md) | Every `error_code` kbagent emits |
| [`kbagent serve`](web-server.md#session-registered-projects) | The REST/Web UI side of session projects |
| [Python SDK](sdk.md) | The importable `Client`, which takes a static token |
| [Design record](programmatic-auth-login-plan.md) | Why the flow is shaped this way, in detail |
