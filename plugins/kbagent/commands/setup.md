---
description: One-command kbagent setup -- install the CLI if missing, connect a Keboola project (browser login, then an account login from the environment, then a static token), then verify with `kbagent doctor`. Idempotent; safe to re-run.
allowed-tools: Bash
argument-hint: [stack URL and/or a project alias, e.g. "https://connection.north-europe.azure.keboola.com" -- whatever is missing is asked for]
---

# /kbagent:setup -- get from zero to a verified connection

Run the whole first-time setup in one invocation: CLI present, project
connected, setup verified. Every step is **conditional on a check**, so
re-running this after a partial setup only fills the gaps.

## Non-negotiable rules

- **Never print, echo, log, or persist a credential.** No secret in a
  command line -- not `--token <value>`, not `--password`, not
  `--totp-secret`, not `echo`, not a heredoc. The CLI has hidden prompts
  and reads `KBC_TOKEN` / `KBC_LOGIN_*` from the environment; use those.
  Never *solicit* a password or TOTP seed either -- if those are not
  already in the environment, that route is simply closed.
- **Use `--json`** for every check you have to parse. Parse the JSON;
  do not scrape human-mode Rich output. One exception to know about: in
  `--json` mode the auth commands print their human panel -- including a
  device-login URL and code -- to **stderr**, and it is not in the JSON. If
  you ever run one of those yourself, capture `2>&1`.
- **Do not re-run a step that already passes.** Never re-register or
  overwrite an existing project or alias.
- **The human part of a browser login is the approval, not the typing.**
  If your harness can run a **background** shell, 3a is yours to drive:
  start the device-code login there, relay the verification URL and user
  code into the chat, and let the user approve in their browser. Only two
  things are genuinely handed over: that browser approval, and the hidden
  token prompt (3c). If your harness has **no** background shell, say so
  at the top of step 3 and hand the login command to the user's terminal
  instead. Either way: never run a login in a *foreground* tool shell
  (its ~120 s timeout kills the flow mid-flight), never run one from an
  unattended task (nobody is there to approve), and if any login call you
  did run gets interrupted, check `kbagent --json auth status` before
  re-running anything -- a blind retry is what orphans a session.

## Behavior

1. **Is the CLI installed?**
   ```bash
   kbagent --json version 2>&1
   ```
   - **Runs** -> continue to step 2. If `kbagent.install_channel` is
     present, this is a standalone/packaged build (Homebrew, Chocolatey,
     winget, an unpacked archive, ...). **Respect it** -- do not run the
     installer over it; the JSON's `upgrade_command` / `upgrade_hint` is
     the only sanctioned way that install gets updated.
   - **Not found** -> install it, but **check the platform first**: the
     one-liner and its PATH fix-up are both POSIX-only.

     *macOS / Linux / WSL / Git Bash:*
     ```bash
     curl -LsSf https://raw.githubusercontent.com/keboola/cli/main/install.sh | sh
     ```
     The installer puts `kbagent` on `PATH` for **its own process only**.
     If `kbagent --json version` still fails right after, run
     `source $HOME/.local/bin/env` (or tell the user to open a new shell)
     and retry once.

     *Windows with no POSIX shell:* there is no `install.sh` route here and
     `$HOME/.local/bin/env` does not exist. Quote the user the PowerShell
     block from README's Install section
     (<https://github.com/keboola/cli#install>) -- `winget install --id
     astral-sh.uv -e`, then `uv tool install` of the release wheel, then
     `uv tool update-shell` -- and tell them `update-shell` edits the
     *persisted* PATH, so they must open a **new** shell before `kbagent`
     resolves. If Git for Windows is installed, README's documented
     alternative is to run the POSIX one-liner through its bash instead.
     Quote README rather than paraphrasing it; that block is versioned and
     this file is not.

     Either way, if it fails a second time, stop and report the installer
     output -- do not attempt a third install.

2. **Is a project already connected?**
   ```bash
   kbagent --json project list
   ```
   If the list is non-empty, say which aliases are already connected and
   **skip straight to step 4**. Do not touch existing entries.

3. **Connect a project.** This step needs the **stack URL**, and it needs it
   first: login is not stack discovery, and step 2 just established there is
   no registered project to infer one from -- so `auth login` without
   `--stack` fails with `CONFIG_ERROR` ("No stack to log into"). Take the URL
   from `$ARGUMENTS` when it looks like one; otherwise ask the user for it
   before running anything (it is the host they see in the Keboola UI, e.g.
   `https://connection.north-europe.azure.keboola.com`).

   Then work down this ladder and **stop at the first rung that lands**. It
   is the order this repo has documented since 0.84.0
   (`plugins/kbagent/.claude-plugin/CLAUDE.md`,
   `skills/kbagent/references/auth-workflow.md`): browser login -> account
   login from the environment -> static token.

   **3a. Browser login -- the default, and nothing to paste. Drive it
   yourself in a background shell; the user only approves in the browser.**
   ```bash
   kbagent auth login --device-code --stack <STACK_URL> --register-projects
   ```
   Run that in a **background** shell with stdout **and** stderr captured,
   and leave it running. `--device-code` forces the RFC 8628 flow, which
   prints a verification URL and a short user code **before** the polling
   loop starts -- so the pair is in the output within a second or two, long
   before the flow completes. Read it out of the early output and relay both
   into the chat: the URL to open and the code to type. If the CLI happens
   to be running on the user's own machine it will also best-effort open
   that URL in their browser, so the tab may already be waiting -- relay the
   pair anyway, it costs nothing and covers the remote case.

   Deliberately **without `--json`**: in human mode the panel goes to
   stdout, whereas in `--json` mode it is written to **stderr** and stdout
   stays empty until the flow ends. Human mode is the readable one here.

   Then confirm it landed, and never assume it from the relay alone:
   ```bash
   kbagent --json auth status
   ```
   Exit 0 means signed in (`live` / `refreshed` / `degraded`); exit 3 means
   not yet (`missing` / `expired`). Poll it once the user says they
   approved, or once the background process exits. If it is still exit 3,
   ask -- do **not** start a second login blind; that is exactly how you
   produce the `orphaned_session_id` warning and leave a session
   `kbagent auth logout` has to chase.

   Two hard limits on where this may run:
   - **Never in a foreground tool shell.** PKCE waits `AUTH_CALLBACK_TIMEOUT`
     (115 s) on the loopback callback and the device flow polls until the
     server's `expires_in` -- minutes. A foreground shell on a ~120 s
     timeout gets **killed mid-flow**, and the kill tells you nothing: the
     login may well have landed a second later.
   - **Never from an unattended task.** There is nobody at a browser to
     approve, so the flow can only expire. Unattended contexts belong on
     3b or 3c.

   **No background shell in your harness?** Say so at the start of this
   step, then hand the plain command to the user's own terminal and wait to
   be told it is done:
   ```bash
   kbagent auth login --stack <STACK_URL> --register-projects
   ```
   Confirm with the same `kbagent --json auth status` afterwards.

   `auth status` deliberately does **not** carry
   `session_unsupported_features` (the surfaces a browser session cannot
   serve). The login command prints them in the user's own terminal; to read
   the list yourself, `kbagent --json auth register-projects --all` ships it
   and is a no-op on anything already registered (status `exists`). Either
   way relay it verbatim and never hand-list it from memory -- the canonical
   copy is `SESSION_UNSUPPORTED_FEATURES` in
   `src/keboola_agent_cli/services/_auth_registration.py`.

   **If 3a is not the answer, route on *why*** -- the two reasons are not
   interchangeable, and treating them as one strands the user:
   - **No browser at all** (headless host, container, CI, SSH without port
     forwarding) -> try **3b**, then 3c.
   - **The user needs one of the surfaces named in
     `session_unsupported_features`** -> go **straight to 3c, skipping 3b**.
     `login-password` mints the *same kind of session* as 3a -- both return
     through `_finalize_login`, so both carry the identical
     `SESSION_UNSUPPORTED_FEATURES` list -- so 3b would report success and
     stop the ladder while leaving the user exactly as unable to do the
     thing they came for. Only a static token serves those surfaces.

   **3b. Account login from the environment** (kbagent 0.84.0+) -- the
   no-browser rung, and the only route into it. Try this *before* reaching
   for a static token, whenever `KBC_LOGIN_EMAIL` and
   `KBC_LOGIN_PASSWORD` are both already exported (plus
   `KBC_LOGIN_TOTP_SECRET` if the account has TOTP-based MFA):
   ```bash
   kbagent --json auth login-password --stack <STACK_URL> --register-projects
   ```
   The command reads all three values straight off the environment
   (`commands/auth.py`, `envvar=` on `--email` / `--password` /
   `--totp-secret`), so no secret enters the conversation and none lands on a
   command line. Never pass them as flags. Two guards:
   - **Version gate.** `login-password` does not exist before 0.84.0.
     Compare `kbagent.version` from step 1; if it is older, skip to 3c.
   - **`AUTH_MFA_INVALID`.** The account's MFA is WebAuthn/passkey-only,
     which this grant cannot resolve without a browser -- and the only way
     to reach 3b is that no browser is available. Go to 3c; do not retry
     `login-password`, and do not loop back to 3a.

   If those variables are absent, skip 3b silently. Never ask the user to
   export a password or a TOTP seed to satisfy this step -- a static token is
   the smaller blast radius, which is exactly why it is the next rung.

   **3c. Static token -- the last resort.**
   ```bash
   kbagent --json project add --project '<ALIAS>' --url <STACK_URL>
   ```
   Never pass `--token` on the command line. Two safe routes:
   - `KBC_TOKEN` is already exported in the environment -> run it as-is.
   - Otherwise hand the command to the **user** to run in their own
     terminal: `project add` prompts for the token with hidden input, and
     that prompt needs a real TTY, which a tool-run shell does not have.

   Ask the user for the alias if `$ARGUMENTS` did not supply one.

4. **Verify.**
   ```bash
   kbagent --json doctor
   ```
   Read the check list off the JSON and interpret it for the user rather
   than dumping it. What decides success is the CLI half: config file +
   permissions and per-project connectivity -- any `fail` there means step
   2/3 did not really land, and that is what you fix before declaring
   anything.

   The **`claude_plugin` check is an upgrade, not a prerequisite**, and a
   `warn` or `skip` on it must never block you from reporting setup
   successful. A connected CLI is the whole product; the plugin only adds
   richer in-session guidance on top. Read it as:
   - `pass` -- the plugin is cached; relay whatever note doctor attaches
     verbatim (a version-drift hint naming the exact copy to update, or a
     migration hint if the copy came from the deprecated marketplace).
   - `warn` -- Claude Code is present but the plugin is not cached. Print
     the `/plugin` lines doctor gives you as an optional next step.
   - `skip` -- Claude Code was not detected on this host. Nothing is
     wrong; this client simply has no plugin surface.

   Always quote doctor's own `/plugin` lines rather than any names
   hardcoded here -- doctor is the single source of truth for the
   marketplace and plugin names. `doctor` deliberately does not install
   the plugin: `/plugin` is an in-session user command.

5. **Close it out.** One short line: what is connected, and two or three
   things to try next -- e.g. `/keboola list all configs in <alias>`,
   `kbagent project list`, `kbagent context`.

## Per-client notes

The CLI is the portable part; only the last mile differs per client.

- **Claude Code** -- everything above works as written, plugin included.
- **Cursor** -- runs plugin slash commands, but its marketplace add needs
  the **full** URL: `/plugin marketplace add https://github.com/keboola/ai-kit`.
  The short `keboola/ai-kit` form is rejected there with
  `[invalid_argument]`. Quote the full URL when you relay doctor's lines
  to a Cursor user.
- **Claude Desktop** -- no slash-command surface at all, so `/kbagent:setup`
  is not typable. Setup arrives instead through the kbagent skill's
  natural-language triggers ("set up Keboola", "connect my Keboola
  project"), or by walking the same ladder manually with the CLI.
- **Any other client with a shell** -- `kbagent project add` (or the login
  ladder) plus `kbagent context` is a complete setup. `kbagent context`
  is the manual substitute for the plugin: it prints the full command
  surface into the session, which is what the skill would otherwise load.

Login is **shared across clients**: the session lands in `auth.json`
beside `config.json` on the local machine, and every client reads the
same files. Signing in from any one of them signs in the rest -- so if
the user already logged in elsewhere on this host, step 2 finds the
projects and you skip step 3 entirely.

## Examples

```
/kbagent:setup
/kbagent:setup https://connection.north-europe.azure.keboola.com
/kbagent:setup my-prod-project
```

## Why this is a slash command and not a CLI subcommand

- The *optional* last mile of setup lives inside the client: `/plugin
  marketplace add` and `/plugin install` are in-session commands a
  background CLI cannot invoke (`services/doctor_service.py` says so
  explicitly). A command that already runs in the session can at least
  read the plugin state and speak plainly about it. That last mile is an
  **upgrade, not a prerequisite** -- the connection itself is pure CLI
  (`auth login` / `project add`), so any client with a shell can be fully
  set up without a plugin, and `kbagent context` is the manual substitute
  that teaches its agent the same command surface.
- Every step it runs is an existing, tested verb (`version`, `project
  list`, `auth login`, `auth login-password`, `auth status`, `project
  add`, `doctor`). This file is the
  *ordering and the conditionals*, not new behavior -- so it cannot drift
  away from the CLI's semantics.
- Idempotence is a checking discipline, and checks are cheap here: the
  agent reads `--json`, compares, and skips. A shell script would have to
  reimplement that.
