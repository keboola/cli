---
description: Run the kbagent-pr-reviewer subagent against the PR for the current git branch (or a specified PR number/URL). Posts a single structured comment review via `gh pr review --comment --body-file`. Read-only on the working tree; never approves or requests changes on GitHub.
allowed-tools: Task, Bash
argument-hint: [optional PR number or URL; defaults to PR for current branch]
---

# /kbagent:review — autonomous read-only PR review

Spawn the `kbagent-pr-reviewer` subagent against a kbagent (`keboola-agent-cli`)
PR. The subagent runs the full playbook from `CONTRIBUTING.md`
(3-layer architecture, Plugin synchronization map, silent-drift hunt,
test coverage, behavior verification) in a fresh context window and
posts ONE comment review on the PR.

## Behavior

1. **Resolve the target PR** from `$ARGUMENTS`:
   - **Empty `$ARGUMENTS`**: detect the open PR for the current git branch.
     Run:
     ```bash
     gh pr view --json number,url,baseRefName,headRefName,title,state 2>&1
     ```
     If the command errors with "no pull requests found for branch X" or any
     other failure, abort with: *"No open PR found for the current branch.
     Either: (a) push your branch and open a PR via `gh pr create`, or (b)
     call `/kbagent:review <PR_NUMBER>` explicitly."*

   - **`$ARGUMENTS` is a number** (e.g. `227`) or a URL (e.g.
     `https://github.com/.../pull/227`): extract the PR number and verify
     it exists:
     ```bash
     gh pr view <N> --json number,url,baseRefName,headRefName,title,state
     ```
     If `state != "OPEN"`, abort with: *"PR #<N> is not open (state=<state>)."*

   - **`$ARGUMENTS` has trailing free text** after the number / URL: treat
     the trailing text as a `<focus>` hint to forward to the subagent
     (e.g. `/kbagent:review 227 storage layer regression risk`).

2. **Sanity-check the working tree**:
   ```bash
   git rev-parse --abbrev-ref HEAD
   ```
   If the current branch is not the PR's `headRefName`, warn (don't abort):
   *"You're on branch X but the PR is for branch Y. The subagent reads
   source at HEAD via the working tree -- if these aren't the same commit,
   the review may target stale code. Run `gh pr checkout <N>` first if you
   want a strict review."*
   If the user has uncommitted changes (`git status --porcelain` non-empty),
   warn similarly. The user can override by re-invoking after stashing.

3. **Spawn the `kbagent-pr-reviewer` subagent** via the `Task` tool:
   - `subagent_type`: `kbagent-pr-reviewer`
   - `description`: `Review PR #<N>` (6-8 word task summary)
   - `prompt`: build a TASK message containing the resolved fields:
     ```
     Review the kbagent PR below. Follow your system prompt's playbook
     end-to-end. Post the report via gh pr review --comment --body-file.

     <pr_url>https://github.com/.../pull/<N></pr_url>
     <pr_number><N></pr_number>
     <branch><headRefName></branch>
     <base_branch><baseRefName></base_branch>
     <focus>{{trailing args, or empty}}</focus>
     ```

4. **Relay the subagent's brief summary to the user**. The subagent posts the
   full report to GitHub itself; do NOT duplicate the post. If the
   subagent's summary indicates the post failed (auth, rate limit, etc.),
   surface its full report so the user can paste manually.

## Examples

```
/kbagent:review                              # auto-detect PR for current branch
/kbagent:review 227                          # explicit PR number
/kbagent:review https://github.com/padak/keboola_agent_cli/pull/227
/kbagent:review 227 focus on the new cache semantics  # forwarded as <focus>
```

## Why this lives as `slash command + subagent` (not just inline)

- The slash command runs in the user's main conversation context. It can
  see the current branch, current uncommitted state, and the user's task
  history. It cannot pollute the main agent's context with the 1500+ token
  review playbook.
- The subagent runs in a fresh window with the full playbook loaded at
  full weight. The non-negotiable rules (read-only, comment-only, ≤15
  findings, file:line citations, verify-don't-assume) survive long
  sessions because they're loaded once on a clean slate -- which is
  exactly the discipline gap that makes review-by-main-context unreliable.

See `plugins/kbagent/agents/kbagent-pr-reviewer.md` for the full subagent
system prompt.

## Hard guardrails (the slash command is allow-listed; the subagent is too)

- The subagent has only `Bash, Read, Grep, Glob`. No `Write`, no `Edit`,
  no working-tree mutation.
- The subagent's prompt forbids `gh pr review --approve`, `--request-changes`,
  `gh pr merge`, `gh pr close`, `gh pr ready`, `git push`. The only
  GitHub-side mutation is `gh pr review --comment --body-file`.
- The verdict in the comment body is advice. The human author makes the
  final approve / request-changes decision via GitHub UI or `gh pr review`
  themselves.
