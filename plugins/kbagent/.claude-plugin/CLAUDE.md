# kbagent plugin — operational guidance

This plugin exposes a CLI (`kbagent`), a skill (`kbagent`), a slash
command (`/keboola`), and a specialist subagent (`keboola-expert`).
All are namespaced under `kbagent:`.

## For Claude Code main agents reading this file

When the user's task touches Keboola Connection (configs, flows, jobs,
storage, branches, sync, MCP tools, migrations):

**Default strategy: delegate to `kbagent:keboola-expert` via the
`Task` tool.**

```
Task(
  subagent_type="keboola-expert",
  description="<6-8 word task summary>",
  prompt="<verbatim user task>"
)
```

or equivalently the user types `/keboola <task>`.

### Why delegate

The keboola-expert subagent runs in a fresh context window with a
system prompt that inlines:

- 6 non-negotiable rules (fresh-fetch, dry-run, no chaining, CLI-over-
  MCP, CLI-over-REST, version gate)
- A tool-selection matrix covering every common Keboola intent
- Inline gotchas from past failure modes observed in internal sessions
- An output contract with a verification payload the parent can parse

These rules are observably NOT followed reliably when the main agent
tries to do the work in the saturated session context. Delegation buys
a clean slate per task.

### When NOT to delegate

- Trivial read (`list projects`, `version`, `changelog`): fine to shell
  out to `kbagent --json ...` directly from the main context.
- User is already asking for a plan / explanation, no execution needed:
  main context can answer from the skill.
- User explicitly asks for a raw command (`just show me the curl
  equivalent`): subagent would refuse; politely decline and show the
  `kbagent --hint client <cmd>` snippet instead.

### Handoff protocol

When the subagent returns:

- If `status: "applied"` — relay the payload, summarize, done.
- If `status: "dry_run_only"` — relay the diff and explicitly ask the
  user whether to apply. Do NOT auto-apply from the main context.
- If `status: "refused"` — relay the refusal and the repair path.
  Do NOT attempt the task yourself — that defeats the delegation.
- If the subagent asks a clarification question — relay it to the user.

## For Claude Code users

- Install the kbagent CLI: `uv tool install git+https://github.com/padak/keboola_agent_cli`
- Initialize a project workspace: `kbagent init --from-global`
  (writes `.kbagent/config.json` whose first field is a `_warning`
  steering any LLM that reads the file away from direct REST calls)
- Use `/keboola <task>` to explicitly invoke the expert subagent
- Or let description-matching auto-trigger the skill for ambient help

## Version

This plugin is versioned in `.claude-plugin/plugin.json`. kbagent CLI
version should match or exceed the plugin version (plugin references
commands that the CLI must actually ship).
