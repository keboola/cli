# Programmatic Auth (Browser Login) workflow

> Audience: a human user of kbagent (or an agent relaying instructions to
> one) who wants to authenticate via a browser instead of pasting a static
> Storage API token. Goal: sign in once, understand what got stored where,
> and know how to check on / tear down the session later.
> Since v0.77.0. Full command reference: `commands-reference.md` >
> "Programmatic Auth (Browser Login)". Gotchas: `gotchas.md` > "Programmatic
> auth (browser login) is human-only; sentinel tokens; v1 scope".

## Read this first: `auth login` needs a human

`kbagent auth login` opens a real browser window (or, on the device flow,
prints a short code you type into a page on any device). There is **no**
headless or unattended path -- an AI agent must never run this command on
its own initiative. If an agent is asked to "set up kbagent auth" or
"log me in", the correct behavior is to hand the exact command back to the
user and wait:

```
Please run this yourself in a terminal where a browser can open:

  kbagent auth login --register-projects

Then let me know once it's done and I'll continue with `kbagent auth status`.
```

For CI, containers, or any other unattended context, keep using a static
Storage token (`kbagent project add --token ...` or
`KBAGENT_PROJECT_FROM_ENV`) -- that path is unchanged by this feature.

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
5. With `--register-projects`, writes each accessible project into
   `config.json` as a normal alias -- except its `token` field is the
   sentinel string `kbc-session://{project_id}`, not a real token. An alias
   that already points at the same project+stack is left alone
   (`status: "exists"`); one pointing somewhere else is skipped with a
   warning (`status: "skipped"`) -- registration never overwrites an
   existing static-token project.

A session is **user-scoped, not project-scoped**: one login is enough for
every project you can see. Which project a given `kbagent` command talks to
is still chosen the normal way (`--project`, `KBAGENT_PROJECT`, the pinned
default) -- the session just supplies the credential, and the CLI adds
`X-KBC-ProjectId` per request.

## The loop

```
# 1. Sign in (opens a browser; falls back to a device code if needed)
kbagent auth login --register-projects

# 2. See what you're signed in as, and which projects got registered
kbagent --json project list | jq '.[] | select(.token | startswith("kbc-session://"))'

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

Session auth is wired through the **Storage and Manage** command paths only.
Every other surface recognizes the `kbc-session://` sentinel and refuses
fast with `AUTH_NOT_SUPPORTED_ON_STACK` instead of silently sending the
sentinel string as if it were a real credential:

- `kbagent serve` (the REST API / web UI backend)
- the importable SDK (`from keboola_agent_cli import Client`)
- the MCP subprocess (`kbagent tool ...`, `kbagent agent --type mcp_tool`)
- the AI Service client (`kai`, `docs query`, component schema lookups)
- the Data Science client (`data-app ...`)
- the Metastore client (`semantic-layer ...`)
- the Developer Portal client (`dev-portal ...`)
- the Stream client (`stream ...`)

If your workflow needs one of those, register the same project again under
a different alias with a static Storage token:

```
kbagent project add --project my-project-static --url <stack> --token <token>
```

The two aliases can coexist; only the sentinel-token one is guarded.

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
- **A session-registered project 401s on an older kbagent**: pre-0.77.0
  builds have no concept of the sentinel token and will try to send
  `kbc-session://...` as a literal `X-StorageApi-Token`. Upgrade kbagent, or
  use a static-token project instead.
- **You want to see the actual token value**: you can't, by design. It lives
  only in `auth.json` (0600) for the CLI's own use. Do not `cat` it or paste
  its contents anywhere -- if you need a credential for a script or another
  tool, use a static Storage token instead.

## Boundaries (what this surface does NOT own)

- It does not replace static Storage tokens -- both coexist indefinitely.
  Static tokens remain the only supported path for CI/CD, containers, and
  any other unattended context.
- It does not manage Manage-API super-admin credentials (`feature`,
  `org setup`, member administration) -- those keep demanding the existing
  interactive manage-token prompt; a programmatic session is user-scoped and
  does not carry admin privileges.
- It does not expose the stored token through any CLI command, `--json`
  output, or log line, ever.
