# Programmatic Auth (Browser Login and Unattended Login) workflow

> Audience: a human user of kbagent (or an agent relaying instructions to
> one) who wants to authenticate via a browser instead of pasting a static
> Storage API token -- or, since v0.84.0, an agent running unattended with
> real account credentials for CI. Goal: sign in once, understand what got
> stored where, and know how to check on / tear down the session later.
> Since v0.80.0 (browser login), v0.84.0 (unattended `login-password`).
> Full command reference: `commands-reference.md` > "Programmatic Auth
> (Browser Login)". Gotchas: `gotchas.md` > "Programmatic auth (browser
> login) is human-only; sentinel tokens; v1 scope" and > "`auth
> login-password` is the CI-safe, headless exception...".

## Read this first: `auth login` needs a human -- `auth login-password` does not

`kbagent auth login` opens a real browser window (or, on the device flow,
prints a short code you type into a page on any device). There is **no**
headless or unattended path for *this specific command*. Two rules are
absolute:

- **Never run it in a foreground tool shell.** It blocks until the human
  finishes; a foreground shell timeout (typically ~120s) kills the flow
  mid-flight.
- **Never run it from an unattended or headless task at all** -- no CI step,
  no scheduled agent task, no background job with nobody watching the chat.
  Those have their own paths (`auth login-password`, or a static token).

### Attended sessions: the agent drives the login

When a human IS present in the conversation and a **background** shell is
available, the agent should drive the login itself rather than handing it
off:

```bash
# 1. start it in a BACKGROUND shell (never foreground)
kbagent auth login --device-code --stack https://connection.keboola.com --register-projects
```

- `--device-code` forces the RFC 8628 flow, which is the agent-friendly one:
  the verification URL and the short `user_code` are printed **immediately**,
  before polling starts, and flushed. In human output mode they go to
  stdout; under `--json` the panel goes to **stderr**, so capture `2>&1`
  there. Human mode is the easier read.
- **Relay the URL and the code to the user in chat.** The CLI also
  best-effort opens the browser on the host machine, but the user may be
  looking at a different device.
- **Confirm completion by polling `kbagent --json auth status`** -- exit
  code `0` means signed in (`live` / `refreshed` / `degraded`), exit code
  `3` means not yet (`missing` / `expired`). That is the completion signal;
  do not guess from the login process's own output, and never re-run login
  blind -- check `auth status` first.
- `auth.json` is written atomically on success only, so an abandoned or
  killed attempt leaves nothing half-written.
- `--register-projects` is fully non-interactive, so it is safe to include
  in the backgrounded command.

### Fallback: hand it to the user's terminal

With no background shell available, hand the exact command back to the user
and wait:

```
Please run this yourself in a terminal where a browser can open:

  kbagent auth login --register-projects

Then let me know once it's done and I'll continue with `kbagent auth status`.
```

### Unattended contexts

For CI, containers, or any other unattended context there are two options
(since v0.84.0): if the task has account email + password (+ a TOTP seed for
MFA), use `kbagent auth login-password` -- see "Unattended login" below, an
agent MAY run it directly. Otherwise keep using a static Storage token
(`kbagent project add --token ...` or `KBAGENT_PROJECT_FROM_ENV`) -- that
path is unchanged by either feature.

## What `login` actually does

1. Resolves the target **stack** (not project) -- `--stack` accepts a bare
   stack URL (`connection.keboola.com`) or an existing project alias (its
   stack is used); omitted, it falls back to the default project's stack.
2. Picks a flow:
   - **PKCE (browser) by default.** Opens the Keboola login page in your
     default browser via a short-lived local (loopback) HTTP listener that
     receives the redirect once you approve.
   - **RFC 8628 device flow** when `--device-code` is passed, or
     automatically when the browser flow cannot be attempted at all (no
     usable browser handler, SSH session, container, WSL without a working
     opener). You'll see a `user_code` and a URL -- open it on ANY device
     (your phone is fine) and enter the code.
   - PKCE only falls back to the device flow on a **pre-exchange** failure
     (loopback bind failure, no browser, callback timeout). Once the browser
     redirect actually arrives, there is no fallback -- an error past that
     point is terminal (e.g. a `state` mismatch, or the exchange itself
     failing) and login simply fails; re-run the command.
3. On success, stores a "programmatic session" -- a short-lived access token
   (`kbc_at_*`, ~1h) and a longer-lived rotating refresh token (`kbc_rt_*`,
   ~30d) -- in `auth.json` **plaintext at 0600**, next to `config.json` (same
   config directory; same permission posture as the static tokens already
   there). It never appears in `config.json` itself.
4. Fetches the list of projects the session can access (introspection).
5. Registers projects into `config.json` as normal aliases whose `token`
   field is the sentinel string `kbc-session://{project_id}` rather than a
   real token. `--register-projects` registers every accessible project
   straight away. Without that flag, a TTY in human output mode gets the
   interactive picker described below; anywhere else, login prints a
   one-line hint pointing at `auth register-projects`. Either way a failure
   here never changes login's own already-successful exit code.

A session is **user-scoped, not project-scoped**: one login is enough for
every project you can see. Which project a given `kbagent` command talks to
is still chosen the normal way (`--project`, `KBAGENT_PROJECT`, the pinned
default) -- the session just supplies the credential, and the CLI adds
`X-KBC-ProjectId` per request.

## Unattended login: `auth login-password` (since v0.84.0)

The CI-safe counterpart to `login` above: never opens a browser, completes
entirely over HTTP, and is safe to run from a secret-backed workflow step --
or directly by an agent that was given real account credentials for this
purpose.

```
kbagent auth login-password --email E (--password-stdin | --password P)
                             [--totp-secret SEED] [--stack URL|alias]
                             [--register-projects]
```

- `--email` / `KBC_LOGIN_EMAIL`, and the password via `--password-stdin`
  (preferred -- hidden prompt on a TTY, reads to EOF on a pipe),
  `--password`, or `KBC_LOGIN_PASSWORD` (a CI secret in the step's `env:`
  block).
- `--totp-secret` / `KBC_LOGIN_TOTP_SECRET` is the account's **base32 TOTP
  seed** from its authenticator enrollment -- **not** a live 6-digit code.
  kbagent computes the current code itself (stdlib RFC 6238) immediately
  before submitting it, so no human ever types a code into this flow. Only
  required if the account has TOTP-based MFA configured.
- An account with **WebAuthn/passkey-only MFA cannot use this command** --
  that ceremony needs a browser. The CLI raises `AUTH_MFA_INVALID` (exit 3);
  fall back to `kbagent auth login` for that account.
- Everything after the token exchange -- session persistence, best-effort
  revoke of the session it replaces, introspection, `--register-projects` --
  is identical to `login` above; the result is stored in `auth.json` the
  same way and follows the same v1 scope restrictions.
- **Security note.** Storing an account's password (and TOTP seed) as CI
  secrets is a bigger blast radius than a single scoped Storage token --
  whoever holds them can do anything that account can do, not just what one
  project's token allows. Use a dedicated, least-privileged service account,
  never a real human's own credentials. For an MFA-enabled account the
  resulting session also carries a live 3-hour sudo window that a
  browser-login session usually does not (see `docs/auth.md`).
- Two CI jobs calling `login-password` with the **same** service account's
  TOTP secret within the same 30-second time slice: the second one fails --
  the server accepts each TOTP code exactly once. Avoid sharing one
  MFA-enabled account across concurrent matrix-build legs, or serialize the
  login step.

## Registering projects: `auth register-projects`

```
kbagent auth register-projects [--stack URL|alias] [--all]
                               [--project-id ID ...] [--alias ID=ALIAS ...] [--yes]
```

Registers an existing session's accessible projects as `config.json`
aliases, without re-running `login`. Run it any time while the session is
still live -- it is the way to register a project you skipped earlier, or to
register one at all if you logged in without `--register-projects`.

Exactly one selection method applies:

| Invocation | Selection |
|---|---|
| `--all` | Every accessible project. Mutually exclusive with `--project-id`. |
| `--project-id ID` (repeatable) | Only those ids. An id the session cannot access is a `ConfigError`, not a silent skip. |
| Neither | The interactive picker -- but only on a TTY without `--json`. |

`--all` and `--project-id` are the non-interactive forms, so this one
command is safe for an agent to run *after* the session exists -- including
right after an agent-driven backgrounded login, once `auth status` reports
exit 0. The browser step itself still needs the human.

### The picker

Every project the session can see is listed with a suggested alias.
Candidates not yet registered start **checked**, so a bare `enter`
registers all of them; already-registered rows start unchecked and carry an
"already registered" tag.

| Key | Action |
|---|---|
| up / down, or `j` / `k` | Move the cursor |
| `space` | Toggle the row under the cursor |
| `a` | Toggle select-all / select-none |
| `enter` | Accept the current selection |
| `q`, `esc`, `ctrl-c` | Cancel -- registers nothing |

After the selection there is a single `Edit aliases?` confirm defaulting to
**no**. Declining keeps the suggested alias each row already displayed;
accepting opens a per-project alias prompt. `--yes` skips only this final
confirmation.

On a piped stdin, or a terminal without real interactive capabilities, the
picker degrades to a typed prompt accepting numbers, ranges, `all`, or
`none` -- so a non-interactive-but-not-`--json` invocation can still select
something. In a non-TTY or `--json` context with neither `--all` nor
`--project-id`, the command fails fast telling you to pass one of them
instead of hanging on a prompt.

### Suggested aliases and collisions

An alias is the project name slugified (`project-{id}` if the name
slugifies to nothing), then suffixed `-{id}`, `-{id}-2`, ... until it is
free of both `config.json` and every earlier row in the same batch. Two
projects sharing a name therefore each get a distinct, usable alias, and a
name colliding with an existing static-token project suffixes away from it.

`--alias ID=ALIAS` (repeatable) overrides the suggestion in every mode, and
pre-fills the picker row. Each result carries a status:

| Status | Meaning |
|---|---|
| `registered` | Written to `config.json` under a session-sentinel token. |
| `exists` | This project is already registered under exactly this alias; no write. |
| `skipped` | The alias is taken -- by a different project, or by this project under another alias. Never overwritten; rename with `kbagent project edit --new-alias`. |

Because the project id, not the alias string, identifies a registration, a
project you already registered under a hand-picked alias is reported under
that alias rather than offered a second, colliding suggestion.

## The loop

```
# 1. Sign in (opens a browser; falls back to a device code if needed)
kbagent auth login --register-projects

# 1-AGENT. Attended session, agent-driven: same login in a BACKGROUND shell,
#          device flow so the URL + code can be relayed to the user in chat.
kbagent auth login --device-code --stack https://connection.keboola.com --register-projects
#          ...then poll for completion (exit 0 = signed in, 3 = not yet):
kbagent --json auth status

# 1-CI. The unattended equivalent, given real account credentials
#       (since v0.84.0) -- no browser, safe from a secret-backed step:
kbagent auth login-password --email "$CI_EMAIL" --password-stdin \
  --totp-secret "$CI_TOTP_SEED" --register-projects <<< "$CI_PASSWORD"

# 2. Register (or top up) local aliases for the projects you want.
#    Interactive picker; --all or --project-id ID for a non-interactive run.
kbagent auth register-projects

# 2b. Which projects are session-backed vs static? Read auth_mode -- never
#     parse the token. Values are exactly "session" or "static", always
#     present. Also on `project status` / `project info`, and over HTTP.
kbagent project list --json | jq '.data[].auth_mode'

# 3. Use the registered projects exactly like any other project alias
kbagent --json config list --project my-project-alias

# 4. Check session health any time (proactively refreshes if the access
#    token is stale -- this is normal, not an error)
kbagent --json auth status

# 5. When you're done (e.g. rotating machines, or revoking access)
kbagent auth logout --remove-projects
```

`auth status` reports one of five states:

| Status | Meaning |
|---|---|
| `live` | Cached access token still fresh; no network call was needed to answer. |
| `refreshed` | The access token had gone stale (normal -- it's ~1h TTL) and was just rotated. |
| `degraded` | A network call failed; reporting the last-known on-disk expiry instead. |
| `expired` | The refresh token itself expired or was revoked -- run `auth login` again. |
| `missing` | No session is persisted for this stack yet. |

## v1 scope: what session auth does NOT cover yet

Session auth is wired through the **Storage and Manage** paths. `kbagent
serve` reaches them too, because it delegates to the same guarded services --
but read the caveat below before serving a session project. Every other
surface recognizes the `kbc-session://` sentinel and refuses fast with
`AUTH_NOT_SUPPORTED_ON_STACK` instead of silently sending the sentinel
string as if it were a real credential:

- `kai`
- `semantic-layer` (Metastore Service)
- `data-app` (Data Science Service)
- `stream` (Data Streams Service)
- `sharing`, unless a master token is set in the environment
- the AI Service paths: `docs query`, `config examples`, `config new`,
  `component detail` / `search`, `flow new` / `update` / `validate`
- the Scheduler Service paths: `flow schedule`, `flow schedule-remove`
- the importable SDK (`from keboola_agent_cli import Client`)

`SESSION_UNSUPPORTED_FEATURES` in `services/_auth_registration.py` is the
in-code copy of that list. `auth login` and `auth register-projects` print it,
and both ship it in `--json` as the additive key
`session_unsupported_features`, so you learn the restrictions up front instead
of at first use. (`auth status` reports session health, not this list.)
Two surfaces are commonly assumed to be on it
and are not: **`dev-portal`** authenticates with its own Developer Portal
identity, never a project token, so a session changes nothing there; and
**`flow` splits** -- `flow list` / `flow detail` are plain Storage calls that
work, while `flow new` / `update` / `validate --project` need the AI Service
and fail.

In a multi-project command (`data-app list`, `flow list`, `storage tables`)
that guard does not abort the whole run: the offending project gets an
`errors[]` entry keeping the real `error_code`
(`AUTH_NOT_SUPPORTED_ON_STACK`, not a generic `UNEXPECTED_ERROR`) while the
other projects succeed. Branch on that code rather
than on the message text.

If your workflow needs one of those, register the same project again under
a different alias with a static Storage token:

```
kbagent project add --project my-project-static --url <stack> --token <token>
```

The two aliases can coexist; only the sentinel-token one is guarded.

### Serving a session project over `kbagent serve`

A session is tied to **your** Keboola identity, while `serve` authenticates
its callers with `KBAGENT_SERVE_TOKEN`. Anyone holding that token therefore
acts as the signed-in user for every session-backed project the server
exposes. Treat the serve token as equivalent to your own credential, or
serve static-token projects instead. See `docs/web-server.md`.

A session that expires while the server is running answers HTTP 401 with
`error_code: SESSION_EXPIRED`. Nothing the caller can send fixes it -- a
browser login only completes on the host, so someone has to run
`kbagent auth login` there.

## Troubleshooting

- **"Browser login is not enabled on this Keboola stack yet"**
  (`AUTH_NOT_SUPPORTED_ON_STACK`, a 404 from the server): the feature is
  behind a per-stack flag and is not turned on for that stack. Use a static
  Storage token instead; this is not a kbagent bug.
- **Callback timeout**: you didn't complete the browser login within the
  window. Re-run `auth login`; if this keeps happening (slow network, a
  proxy blocking the loopback callback), pass `--device-code` instead.
- **Stuck in an SSH session / container / WSL**: the CLI should auto-detect
  this and use the device flow already. If it picks PKCE anyway and it
  can't work, force it explicitly with `--device-code`.
- **`SESSION_EXPIRED` / `SESSION_NOT_FOUND`** on any command: the refresh
  token expired, was revoked (e.g. you logged in again elsewhere and the old
  session became an "orphan"), or no session was ever created for this
  stack. Run `kbagent auth login` again.
- **A session-registered project 401s on an older kbagent**: pre-0.80.0
  builds have no concept of the sentinel token and will try to send
  `kbc-session://...` as a literal `X-StorageApi-Token`. Upgrade kbagent, or
  use a static-token project instead.
- **You want to see the actual token value**: you can't, by design. It lives
  only in `auth.json` (0600) for the CLI's own use. Do not `cat` it or paste
  its contents anywhere -- if you need a credential for a script or another
  tool, use a static Storage token instead.
- **A refresh timed out**: `TIMEOUT` / `CONNECTION_ERROR` (exit 4, network)
  means the auth service was slow or unreachable -- your login is not dead, so
  do not re-run `auth login`. The refresh is one attempt under a short budget
  by design; just run the original command again.
- **`login-password` says "Invalid email or password"**: literally that --
  double-check the credentials, not a kbagent bug. **"Invalid or expired TOTP
  code"** on the MFA step means the seed or the account's server-side clock
  drifted, or the code was already used (see the next point).
- **`login-password` fails intermittently in a CI matrix**: if two jobs
  share one MFA-enabled service account and log in within the same
  30-second window, the second submission is rejected -- the server accepts
  each TOTP time-slice exactly once. Serialize the login step across that
  account, or give each matrix leg its own account.
- **`login-password` raises `AUTH_MFA_INVALID`**: the account's MFA is
  WebAuthn/passkey-only, which this grant cannot resolve without a browser.
  Fall back to `kbagent auth login` for that account (a human, once).

In the human `project list` / `project status` tables the mode shows as an
`Auth` column, and in `project info` as an `Auth` row above the Token rows. A
session project's human `Token` cell is a dash -- the sentinel is not a
credential, and masked (`kbc-...9840`) it reads like a truncated real token.
The `--json` `token` field is unchanged (still masked), so do not expect a dash
there.

## Converting a session project to a static token

Two commands touch a session project's credential, and they behave
differently on purpose:

- **`project refresh` (and `org setup --refresh`) skip it.** There is no
  static token to replace -- the credential lives in `auth.json` and its
  access token rotates on its own -- so the project is reported under
  `skipped` with that reason rather than raising. `--force` does not convert
  it either.
- **`project edit --token` converts it, with a warning.** This is the
  supported deliberate conversion. Once done the alias is a static-token
  project, so `auth logout --remove-projects` no longer cleans it up -- use
  `kbagent project remove`. The warning is identical under `--dry-run`, and
  in `--json` it arrives in an additive top-level `warnings` array.

## Boundaries (what this surface does NOT own)

- It does not replace static Storage tokens -- both coexist indefinitely.
  Static tokens remain a supported path for CI/CD, containers, and any other
  unattended context; `auth login-password` (since v0.84.0) is the other one
  when the task has account credentials rather than a token.
- It does not manage Manage-API super-admin credentials (`feature`,
  `org setup`, member administration) -- those keep demanding the existing
  interactive manage-token prompt; a programmatic session is user-scoped and
  does not carry admin privileges.
- It does not expose the stored token through any CLI command, `--json`
  output, or log line, ever.
