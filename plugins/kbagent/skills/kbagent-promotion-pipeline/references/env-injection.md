# Why every step uses `KBAGENT_PROJECT_FROM_ENV` / `__env__`

kbagent's normal mode of operation is a **registered project**: `kbagent
project add --project ALIAS --url URL --token TOKEN` writes the token into
`~/.config/keboola-agent-cli/config.json`, and every later command references
that alias. That's the right model for a developer's own machine, but wrong
for CI: it means a token would have to be written to disk (or the config
file would have to be committed, which is worse -- a secret in git history).

Since 0.50.0, kbagent supports a headless alternative purpose-built for this:
set `KBAGENT_PROJECT_FROM_ENV=1` together with `KBC_TOKEN` and
`KBC_STORAGE_API_URL`, and kbagent synthesizes an **in-memory** project under
the reserved alias `__env__` for that process only -- no `project add`, no
`config.json` write, nothing to clean up afterward. Every command in this
skill's generated workflows passes `--project __env__` for exactly this
reason.

## Two tokens, two projects, same alias name

Because `__env__` is resolved from whatever `KBC_TOKEN` /
`KBC_STORAGE_API_URL` happen to be set in the current step's `env:` block,
the **same alias name** (`__env__`) can point at two completely different
physical Keboola projects across two steps in the same job -- the pull step
sets the source project's token, the validate/push steps set the destination
project's token. There is no conflict because each step's environment is
isolated; kbagent never persists what `__env__` resolved to.

## What this buys you

- The token is a GitHub Actions secret, masked in logs, never written to a
  file kbagent (or a subsequent step) could accidentally commit.
- No `project add`/`project remove` housekeeping in CI -- the "project"
  exists only for the duration of one step.
- The same generated workflow works identically whether the source and
  destination happen to be on the same Keboola stack or different ones --
  `KBC_STORAGE_API_URL` is set explicitly per step from the pipeline
  definition, not inferred from a registered project's stored URL.
