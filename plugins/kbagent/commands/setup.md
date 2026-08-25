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
  do not scrape human-mode Rich output.
- **Do not re-run a step that already passes.** Never re-register or
  overwrite an existing project or alias.
- **When a step needs a human, say so and stop guessing.** Browser login
  is a human action; announce it, then wait for the outcome.

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
   - **Not found** -> install it:
     ```bash
     curl -LsSf https://raw.githubusercontent.com/keboola/cli/main/install.sh | sh
     ```
     The installer puts `kbagent` on `PATH` for **its own process only**.
     If `kbagent --json version` still fails right after, run
     `source $HOME/.local/bin/env` (or tell the user to open a new shell)
     and retry once. If it fails a second time, stop and report the
     installer output -- do not attempt a third install.

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

   **3a. Browser login -- the default, and nothing to paste.**
   ```bash
   kbagent --json auth login --stack <STACK_URL> --register-projects
   ```
   PKCE loopback with an automatic device-code fallback, registering every
   project the session can reach under a local alias. Tell the user a browser
   window / verification code is coming and that this step is theirs to
   complete -- then wait for it.

   The result carries `session_unsupported_features`: the command surfaces
   a browser session does not serve, whose canonical list is
   `SESSION_UNSUPPORTED_FEATURES` in
   `src/keboola_agent_cli/services/_auth_registration.py`. Read the key off
   the result and relay it verbatim; never hand-list it from memory.

   Drop to 3b when *either* the user needs one of the surfaces named in
   `session_unsupported_features`, or there is no browser at all (headless
   host, container, CI, SSH without port forwarding).

   **3b. Account login from the environment** (kbagent 0.84.0+) -- try this
   *before* reaching for a static token, whenever `KBC_LOGIN_EMAIL` and
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
     which this grant cannot resolve without a browser -- and both routes
     into 3b are routes where browser login is unavailable or insufficient.
     Go to 3c; do not retry `login-password` and do not loop back to 3a.

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
   than dumping it. Call out:
   - config file + permissions and per-project connectivity -- any `fail`
     here means step 2/3 did not really land; fix that before declaring
     success.
   - the **`claude_plugin` check** -- `pass` means the plugin is cached;
     relay whatever note doctor attaches to it verbatim (a version-drift
     hint naming the exact copy to update, or a migration hint if the
     copy was installed from the deprecated marketplace). `warn` means
     Claude Code is present but the plugin is not cached, so print the
     `/plugin` lines doctor gives you. `skip` means Claude Code was not
     detected on this host. Always quote doctor's own `/plugin` lines
     rather than any names hardcoded here -- doctor is the single source
     of truth for the marketplace and plugin names. `doctor` deliberately
     does not install the plugin -- `/plugin` is an in-session user
     command.

5. **Close it out.** One short line: what is connected, and two or three
   things to try next -- e.g. `/keboola list all configs in <alias>`,
   `kbagent project list`, `kbagent context`.

## Examples

```
/kbagent:setup
/kbagent:setup https://connection.north-europe.azure.keboola.com
/kbagent:setup my-prod-project
```

## Why this is a slash command and not a CLI subcommand

- The last mile of setup lives *inside* Claude Code: `/plugin marketplace
  add` and `/plugin install` are in-session commands a background CLI
  cannot invoke (`services/doctor_service.py` says so explicitly). A
  command that already runs in the session can at least read the plugin
  state and speak plainly about it.
- Every step it runs is an existing, tested verb (`version`, `project
  list`, `auth login`, `auth login-password`, `project add`, `doctor`).
  This file is the
  *ordering and the conditionals*, not new behavior -- so it cannot drift
  away from the CLI's semantics.
- Idempotence is a checking discipline, and checks are cheap here: the
  agent reads `--json`, compares, and skips. A shell script would have to
  reimplement that.
