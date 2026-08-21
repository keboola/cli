---
description: One-command kbagent setup -- install the CLI if missing, connect a Keboola project via browser login (token fallback), then verify with `kbagent doctor`. Idempotent; safe to re-run.
allowed-tools: Bash
argument-hint: [optional stack URL or project name, e.g. "https://connection.north-europe.azure.keboola.com"]
---

# /kbagent:setup -- get from zero to a verified connection

Run the whole first-time setup in one invocation: CLI present, project
connected, setup verified. Every step is **conditional on a check**, so
re-running this after a partial setup only fills the gaps.

## Non-negotiable rules

- **Never print, echo, log, or persist a token.** No token in a command
  line -- not `--token <value>`, not `echo`, not a heredoc. The CLI has a
  hidden prompt and reads `KBC_TOKEN` from the environment; use those.
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

3. **Connect a project (no token pasting).**
   ```bash
   kbagent --json auth login --register-projects
   ```
   Browser login (PKCE loopback, with an automatic device-code fallback),
   registering every project the session can reach under a local alias.
   Tell the user a browser window / verification code is coming and that
   this step is theirs to complete -- then wait for it.

   The result carries `session_unsupported_features`: the command surfaces
   a browser session does not serve, whose canonical list is
   `SESSION_UNSUPPORTED_FEATURES` in
   `src/keboola_agent_cli/services/_auth_registration.py`. Read the key off
   the result and relay it verbatim; never hand-list it from memory.

   **Static-token fallback** -- use it when *either* the user needs one of
   the surfaces named in `session_unsupported_features`, or there is no
   browser at all (headless host, container, CI, SSH without forwarding):
   ```bash
   kbagent --json project add --project '<ALIAS>' --url <STACK_URL>
   ```
   Never pass `--token` on the command line. Two safe routes:
   - `KBC_TOKEN` is already exported in the environment -> run it as-is.
   - Otherwise hand the command to the **user** to run in their own
     terminal: `project add` prompts for the token with hidden input, and
     that prompt needs a real TTY, which a tool-run shell does not have.

   Ask the user for the alias and stack URL if `$ARGUMENTS` did not
   supply them.

4. **Verify.**
   ```bash
   kbagent --json doctor
   ```
   Read the check list off the JSON and interpret it for the user rather
   than dumping it. Call out:
   - config file + permissions and per-project connectivity -- any `fail`
     here means step 2/3 did not really land; fix that before declaring
     success.
   - the **`claude_plugin` check** -- `pass` means the plugin is cached
     (a version-drift note asks for `/plugin update kbagent`); `warn`
     means Claude Code is present but the plugin is not cached, so print
     the two `/plugin` lines doctor gives you; `skip` means Claude Code
     was not detected on this host. `doctor` deliberately does not
     install the plugin -- `/plugin` is an in-session user command.

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
  list`, `auth login`, `project add`, `doctor`). This file is the
  *ordering and the conditionals*, not new behavior -- so it cannot drift
  away from the CLI's semantics.
- Idempotence is a checking discipline, and checks are cheap here: the
  agent reads `--json`, compares, and skips. A shell script would have to
  reimplement that.
