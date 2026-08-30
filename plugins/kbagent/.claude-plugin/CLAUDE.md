# kbagent plugin — operational guidance

This plugin exposes a CLI (`kbagent`), three skills (`kbagent`,
`kbagent-cicd-migration`, `kbagent-promotion-pipeline`), three slash
commands (`/kbagent:setup`, `/keboola`, `/kbagent:review`), and two
specialist subagents (`keboola-expert`, `kbagent-pr-reviewer`). All are
namespaced under `kbagent:`.

`/kbagent:setup` is the first-run entry point: it installs the CLI if
missing, connects a project, and verifies with `kbagent doctor` -- every
step conditional, so it is safe to re-run. It runs in the main context and
spawns no subagent. The two subagents serve disjoint domains:

| Subagent | Use for | Slash command | Trigger phrases |
|---|---|---|---|
| `keboola-expert` | Operations on a Keboola Connection PROJECT (configs, flows, jobs, storage, branches, sync, migrations) | `/keboola <task>` | "update flow", "list configs", "run a job", "migrate", "debug a transformation" |
| `kbagent-pr-reviewer` | Code review of a `keboola-agent-cli` PULL REQUEST (this repo) | `/kbagent:review [PR_NUM]` | "review PR", "review this branch", "code review", "PR comments" |

Mismatching the subagent to the domain wastes a fresh context window.
`keboola-expert` does not know how to grep CONTRIBUTING.md silent-drift
surfaces; `kbagent-pr-reviewer` does not know how to run a Keboola job.

## For Claude Code main agents reading this file

### Path A: user wants Keboola Connection work done

When the user's task touches Keboola Connection (configs, flows, jobs,
storage, branches, sync, migrations):

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

### Path B: user wants a PR on this repo reviewed

When the user is on a feature branch with an open PR and asks for a
code review (or runs `/kbagent:review`):

**Default strategy: invoke `kbagent:kbagent-pr-reviewer` via the
`/kbagent:review` slash command.** The slash command resolves the PR
from the current branch (or accepts an explicit number) and spawns
the subagent with the PR context. The subagent runs the full
review playbook from `CONTRIBUTING.md` (3-layer architecture, Plugin
synchronization map, silent-drift hunt, test coverage, behavior
verification) and posts a single `gh pr review --comment` to the PR.

```
# Either:
/kbagent:review                # detect PR for current branch
/kbagent:review 227            # explicit number / URL

# Or directly via Task tool:
Task(
  subagent_type="kbagent-pr-reviewer",
  description="Review PR #227",
  prompt="Review the kbagent PR below. ..."   # see commands/review.md for shape
)
```

The reviewer is **read-only on the working tree** (no `Write`/`Edit`,
no branch switching, no push) and **comment-only on GitHub** (never
`--approve`, never `--request-changes`, never `gh pr merge`). Verdict
in the comment body is advice; the human author retains every veto.

### Why delegate

The keboola-expert subagent runs in a fresh context window with a
system prompt that inlines:

- 6 non-negotiable rules (fresh-fetch, dry-run, no chaining, no MCP
  passthrough, CLI-over-REST, version gate)
- A tool-selection matrix covering every common Keboola intent
- Inline gotchas from past failure modes observed in internal sessions
- An output contract with a verification payload the parent can parse

These rules are observably NOT followed reliably when the main agent
tries to do the work in the saturated session context. Delegation buys
a clean slate per task.

### When NOT to delegate (Path A, `keboola-expert`)

- Trivial read (`list projects`, `version`, `changelog`): fine to shell
  out to `kbagent --json ...` directly from the main context.
- User is already asking for a plan / explanation, no execution needed:
  main context can answer from the skill.
- User explicitly asks for a raw command (`just show me the curl
  equivalent`): subagent would refuse; politely decline and point the
  user at the `kbagent serve` REST API for programmatic integrations.
- User asks to log in / set up auth via a browser (`kbagent auth
  login`): the human part is APPROVING in the browser, not driving the
  command. In an ATTENDED session whose harness has a background
  shell, the main context SHOULD complete it: run `kbagent auth login
  --device-code --stack <URL> --register-projects` in a **background**
  shell capturing stdout+stderr (human mode, not `--json` -- there the
  URL and code go to stderr), relay the verification URL and user code
  to the user, then confirm with `kbagent --json auth status` (exit 0
  = signed in, exit 3 = not yet). Never in a foreground tool shell
  (its ~120 s timeout kills the flow mid-flight) and never blind-retry
  -- check `auth status` first. With no background shell available,
  hand the plain command back to the user and wait. For an UNATTENDED
  context `auth login` is out entirely (nobody can approve), and the
  answer is NOT automatically a static Storage token: if the user has
  account credentials for this purpose, `kbagent auth login-password`
  (0.84.0+) is the CI-safe, headless alternative and an agent MAY run
  it directly; fall back to a static Storage token only when no such
  credentials exist.

### When NOT to delegate (Path B, `kbagent-pr-reviewer`)

- User asks "what does PR #N change?" -- a summary, not a review. Read
  the diff directly via `gh pr diff <N>` and summarise; do not spawn the
  subagent (it would burn a context window producing a full report).
- User wants to actually approve / request-changes. The subagent is
  comment-only; that ergonomic decision is the user's via GitHub UI or
  `gh pr review --approve` themselves.
- User asks for a review of a non-kbagent repo. The reviewer's playbook
  is hard-wired to this codebase's CONTRIBUTING.md / Plugin sync map; it
  has no value on other repos.

### Handoff protocol

When the subagent returns:

- If `status: "applied"` — relay the payload, summarize, done.
- If `status: "dry_run_only"` — relay the diff and explicitly ask the
  user whether to apply. Do NOT auto-apply from the main context.
- If `status: "refused"` — relay the refusal and the repair path.
  Do NOT attempt the task yourself — that defeats the delegation.
- If the subagent asks a clarification question — relay it to the user.

## For Claude Code users

- **Start here: run `/kbagent:setup`.** One command -- it installs the
  kbagent CLI if it is missing, connects a Keboola project (browser
  login, falling back to `auth login-password` from the environment and
  then a static token, per the order above), and verifies the result with
  `kbagent doctor`. Idempotent, so re-running it after a partial setup
  only fills the gaps.
- Initialize a project workspace: `kbagent init --from-global`
  (writes `.kbagent/config.json` whose first field is a `_warning`
  steering any LLM that reads the file away from direct REST calls)
- Use `/keboola <task>` to explicitly invoke the expert subagent for
  Keboola Connection work
- Use `/kbagent:review` (with no args, while on a PR's branch) to ask
  the read-only reviewer to leave a structured comment review on the
  PR. Requires `gh auth login`
- Or let description-matching auto-trigger the skill for ambient help
- Install or reinstall this plugin from Keboola's marketplace:
  `/plugin marketplace add keboola/ai-kit` then
  `/plugin install kbagent@keboola-claude-kit`. A copy installed from the
  older `keboola-agent-cli` marketplace still works, but that entry is
  deprecated -- `kbagent doctor` says so and prints those two lines

## Version

This plugin is versioned in `.claude-plugin/plugin.json`. kbagent CLI
version should match or exceed the plugin version (plugin references
commands that the CLI must actually ship).
