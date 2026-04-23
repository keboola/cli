---
description: Delegate a Keboola Connection task to the keboola-expert specialist subagent (enforces fresh-fetch, dry-run, CLI-over-REST, version gate).
allowed-tools: Task
argument-hint: [task description, e.g. "update flow 123 description in dev branch"]
---

# /keboola — delegate to Keboola expert

Route any Keboola Connection work to the `kbagent:keboola-expert` subagent.
The subagent runs with a fresh context window and a system prompt that
inlines all non-negotiable rules, gotchas, and the tool-selection matrix,
so it does not drift like the main conversation does as tokens pile up.

## Behavior

1. If `$ARGUMENTS` is empty, respond with: "What Keboola task would you
   like me to delegate? Examples: 'list configs in project prod',
   'update flow 300555360 schedule to 0 6 * * *', 'migrate flow 123 from
   source-proj to dest-proj'."
2. Otherwise, spawn the `keboola-expert` subagent via the `Task` tool:
   - `subagent_type`: `keboola-expert`
   - `description`: first 6-8 words of `$ARGUMENTS`
   - `prompt`: the full `$ARGUMENTS` string, verbatim, plus this
     preamble so the subagent knows its calling context:

     ```
     [Main agent is delegating the following Keboola task to you.
      Respond in the same language as this prompt. Return the §7
      output payload from your system prompt. If you need clarification
      rather than execution, return a §8 clarification question
      instead of guessing.]

     TASK: $ARGUMENTS
     ```

3. When the subagent returns, relay its payload to the user verbatim
   and, if the payload's `status` is `"dry_run_only"`, explicitly ask
   the user whether to apply. Do NOT auto-apply.
4. When the subagent `refuses` (e.g. version gate), relay the refusal
   + repair path verbatim. Do not attempt the task yourself in the
   main context -- that defeats the point of delegation.

## Examples

```
/keboola update the description on flow 300555360 to "daily ETL refresh"
/keboola list all configs in kbagent-e2e that touch bucket in.c-stage-L1-SOURCE
/keboola create a workspace and run SELECT COUNT(*) FROM orders
/keboola I'm planning to retype all columns on in.c-stage.orders -- draft a plan
```

## Why explicit delegation beats relying on skill triggers

- Skill auto-triggering depends on the user prompt matching the skill's
  description keywords. That is probabilistic.
- A slash command is deterministic. User typed `/keboola X` -> Claude
  spawns the expert subagent. No "did the skill catch it?" uncertainty.
- The subagent has a fresh context window, so the non-negotiable rules
  are loaded at full weight -- not diluted by 200-turn conversation
  history in the main agent.

See `plugins/kbagent/agents/keboola-expert.md` for the full subagent
system prompt.
