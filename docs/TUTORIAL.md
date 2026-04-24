# kbagent Tutorial

End-to-end walkthrough for getting kbagent running the way you need it.
Pick the section that matches your situation; skip the rest.

> For the full command reference, run `kbagent context` or see
> [plugins/kbagent/skills/kbagent/SKILL.md](../plugins/kbagent/skills/kbagent/SKILL.md).
> For permissions and per-directory isolation details, see
> [docs/guide.md](guide.md).

---

## Prerequisites

Install the CLI once (auto-updates on every launch):

```bash
uv tool install git+https://github.com/padak/keboola_agent_cli
```

Verify:

```bash
kbagent --version
kbagent doctor
```

`kbagent doctor` walks through config, connectivity, CLI version, MCP
server, and Claude Code plugin detection. Everything it reports as
`warn` or `fail` comes with a concrete repair step.

![kbagent doctor output](assets/demo-doctor.gif)

---

## 1. Add a single project (you already have a Storage API token)

You have a Storage API token from the Keboola UI (Settings → API Tokens).
Nothing else is needed.

```bash
kbagent project add --project prod \
  --url https://connection.keboola.com \
  --token YOUR_STORAGE_API_TOKEN
```

Aliases are arbitrary; pick names that make sense in your head
(`prod`, `dev`, `client-a`, `my-project`). The CLI uses them on every
subsequent command (`--project prod`).

Alternative ways to pass the token (same effect, safer for shell history):

```bash
# Env var
export KBC_TOKEN=YOUR_STORAGE_API_TOKEN
kbagent project add --project prod --url https://connection.keboola.com

# Interactive prompt (stdin hidden)
kbagent project add --project prod --url https://connection.keboola.com
# -> prompts for the token
```

Verify:

```bash
kbagent project list --json
kbagent project status                 # tests connectivity against the API
```

![kbagent project add flow](assets/demo-add-project.gif)

Stack URLs by region (use the one matching your Keboola account):

| Stack | URL |
|---|---|
| US | `https://connection.keboola.com` |
| EU | `https://connection.eu-central-1.keboola.com` |
| Azure EU | `https://connection.north-europe.azure.keboola.com` |
| GCP US | `https://connection.us-east4.gcp.keboola.com` |
| GCP EU | `https://connection.europe-west3.gcp.keboola.com` |

### Pin a default project

Most commands take `--project ALIAS`. If you work mostly with one, pin
it to avoid typing the flag every time:

```bash
kbagent project use prod
kbagent project current              # shows what's effectively active
```

`KBAGENT_PROJECT=prod` env var overrides the pin for a single shell
session.

---

## 2. Add many projects (you have a Manage / Personal Access Token and the project IDs)

When you know exactly which project IDs you want -- but your token is a
*Manage API token* (org admin) or a *Personal Access Token* (any member) --
use `org setup --project-ids`. kbagent creates a Storage API token in
each listed project and registers them all locally, in parallel.

```bash
export KBC_MANAGE_API_TOKEN=YOUR_MANAGE_OR_PAT_TOKEN

kbagent org setup \
  --project-ids 901,9621,10539 \
  --url https://connection.keboola.com \
  --dry-run                           # always dry-run first
```

The dry-run prints what would happen (create token + register alias
per project, skip already-registered ones). After reviewing, re-run
without `--dry-run` or add `--yes`:

```bash
kbagent org setup \
  --project-ids 901,9621,10539 \
  --url https://connection.keboola.com \
  --yes
```

Flags worth knowing:

| Flag | What it does |
|---|---|
| `--token-description "kbagent-<user>"` | Prefix for the Storage tokens kbagent creates in each project. Helpful for auditing later: "which tokens did kbagent give me?" |
| `--token-expires-in 3600` | Create ephemeral tokens that auto-expire (seconds). Default: never expires. |
| `--refresh` | Regenerate Storage tokens for projects already registered with invalid/expired tokens. |

The command is **idempotent**: running it again skips projects that
are already registered. Safe to re-run after adding new project IDs.

**Security note**: `KBC_MANAGE_API_TOKEN` is read only from env or
from an interactive hidden prompt. kbagent never accepts it as a CLI
argument (`--token xxx`) -- that would leak into shell history and
process listings.

---

## 3. Add a whole organization (you are org admin)

If you are an org admin with a Manage API token, register **every**
project in an organization in one shot:

```bash
export KBC_MANAGE_API_TOKEN=YOUR_ORG_ADMIN_MANAGE_TOKEN

kbagent org setup \
  --org-id 123 \
  --url https://connection.keboola.com \
  --dry-run
```

The dry-run reports how many projects will be registered and the
alias naming scheme (slug-safe, lowercased, unique). Apply with:

```bash
kbagent org setup --org-id 123 --url https://connection.keboola.com --yes
```

Supports the same `--token-description`, `--token-expires-in`, and
`--refresh` flags as `--project-ids`.

### Re-running later

When the organization adds new projects, re-run the same command.
kbagent skips already-registered projects by matching their numeric
`project_id`, so only the new ones are added.

```bash
kbagent org setup --org-id 123 --url https://connection.keboola.com --yes
# "Registered 2 new project(s); skipped 42 already-registered project(s)."
```

Use `--refresh` if some tokens expired or were revoked upstream --
kbagent will generate new Storage tokens for those specific projects.

---

## 4. Global vs. local config

kbagent supports two config locations. Both have the same JSON shape;
they differ in scope and precedence.

### Global config (one per user)

Default location:

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/keboola-agent-cli/config.json` |
| Linux | `~/.config/keboola-agent-cli/config.json` |
| Windows | `%APPDATA%\keboola-agent-cli\config.json` |

- Set automatically on the first `project add` / `org setup` call.
- Permissions `0600` (owner read/write only).
- Shared across all terminals and working directories.
- Best for: one-person setup, everything in one place, no project
  isolation needed.

### Local config (per-directory)

```bash
cd ~/projects/client-a
kbagent init --from-global     # creates ./.kbagent/config.json
```

Creates `.kbagent/config.json` in the current working directory.
Once present, any `kbagent` command run from this directory (or any
subdirectory) uses the local config instead of the global one.

- `.kbagent/` is auto-added to `.gitignore` on init.
- Tokens are NOT portable -- copy only project aliases + URLs when
  sharing with teammates, not the token values.
- Best for: multiple clients/orgs on the same machine, isolation
  between repos, read-only sandboxing for AI agents.

### Resolution precedence

When kbagent looks up which config to use, it walks this chain:

```
1. --config-dir /path/to/dir      (explicit CLI flag)
2. KBAGENT_CONFIG_DIR env var
3. .kbagent/ in CWD or any ancestor directory
4. global default (platformdirs-based, see table above)
```

First match wins. The rest are ignored.

### Read-only sandboxing for AI agents

If you want to let Claude Code (or any other agent) explore a project
without the ability to modify anything:

```bash
cd ~/projects/client-a
kbagent init --from-global --read-only
```

This creates `.kbagent/config.json` with a permissions policy that
denies all write CLI commands and all write MCP tools. It also:

- Sets `config.json` to permissions `0400` (read-only even for you;
  kbagent reads via its own path).
- Writes `.claude/settings.json` with Claude Code deny-rules that
  block editing `.kbagent/config.json` and running
  `kbagent permissions set|reset`.

The agent can still browse, list, search, inspect -- but every write
attempt fails with exit code 6 and a clear error message.

See [docs/guide.md](guide.md#permissions) for the full firewall model.

### The `_warning` field in `config.json`

From v0.24.0, every `.kbagent/config.json` write begins with a
`_warning` string:

```json
{
  "_warning": "THESE ARE KEBOOLA STORAGE API TOKENS. NEVER use them to call the Keboola REST API directly ...",
  "version": 1,
  "projects": { ... }
}
```

The field is silently ignored by kbagent on load -- it exists purely
to steer any AI agent that reads the file toward `kbagent <command>`
instead of copying the token into a raw REST call.

---

## 5. Install the Claude Code plugin

The CLI (`uv tool install ...`) and the Claude Code plugin are two
**separate** installations:

- CLI provides `kbagent` in your shell.
- Plugin teaches Claude Code to use that CLI effectively: skill,
  slash commands, specialist subagent.

You can use the CLI without the plugin (any shell, any agent). You
cannot use the plugin without the CLI.

### Install

In Claude Code, run:

```
/plugin marketplace add padak/keboola_agent_cli
/plugin install kbagent@keboola-agent-cli
```

Claude Code clones the marketplace and drops the plugin into
`~/.claude/plugins/cache/keboola-agent-cli/kbagent/<version>/`.

### Verify

Outside Claude Code:

```bash
kbagent doctor
```

Look for the "Claude Code plugin" check:

- `pass` with a version -- plugin is installed and cached.
- `warn` with install commands -- Claude Code is present but the
  plugin is not cached. Run the `/plugin` commands above.
- `skip` -- Claude Code is not installed on this host.

If kbagent and the plugin drift out of sync, the `pass` message tells
you to run `/plugin update kbagent` in Claude Code.

### What the plugin ships

| Component | What it does |
|---|---|
| `kbagent` skill | Loaded into the main agent when it recognises Keboola-related prompts. 10 rules + a decision table mapping goals to commands. |
| `/keboola <task>` slash command | Explicitly delegates a Keboola task to the specialist subagent (see §6). |
| `kbagent:keboola-expert` subagent | Fresh-context specialist with non-negotiable rules, tool matrix, inline gotchas, and a JSON verification payload output contract. |
| Plugin-level `CLAUDE.md` | Instructs the main agent *when* to delegate vs. handle inline. |

---

## 6. Using the agent and slash commands

Once the plugin is installed, you have three ways to use kbagent from
Claude Code, each with a different trade-off.

### A) Let the skill auto-trigger

Just talk to Claude naturally. When your prompt matches keywords like
*kbagent*, *Keboola project*, *keboola configs*, *SQL debugging*, the
skill loads automatically and the main agent has the full command
reference in context.

```
You: "List all Snowflake transformations in project prod that
      reference the orders table."
```

Claude reads the skill, plans a `kbagent config search` + `kbagent
config detail` sequence, runs them, and returns the answer. Best for
read-only exploration and short one-off questions.

**Trade-off**: the main agent carries the skill rules through a long
conversation. As context fills up, rule compliance drifts. For
multi-step writes, prefer the next option.

### B) Delegate explicitly with `/keboola <task>`

```
/keboola update the description on flow 300555360 to "daily ETL refresh"
/keboola create a workspace and run SELECT COUNT(*) FROM orders
/keboola migrate flow 123 from proj-a to proj-b
```

The slash command spawns the `kbagent:keboola-expert` subagent. The
subagent runs in a **fresh context window** with the full system
prompt (non-negotiable rules, tool matrix, inline gotchas, output
contract) at full weight. It plans, executes, and returns a structured
verification payload.

Behaviour on writes:

1. Subagent fetches the current state via `kbagent --json ... detail`.
2. Runs the mutative command with `--dry-run`.
3. Returns `status: "dry_run_only"` + the diff, asking you to confirm.
4. You reply "apply" (or equivalent), and the main agent re-invokes
   the subagent with the dry-run timestamp.
5. Subagent re-runs without `--dry-run`, verifies, returns
   `status: "applied"` + verification.

If the subagent cannot safely complete the task (e.g. kbagent version
lacks the required command), it returns `status: "refused"` plus a
concrete repair path. The main agent relays that; it does NOT attempt
the task itself (that defeats the delegation).

### C) Invoke via `Task` tool explicitly

For programmatic use (e.g. in a custom orchestrator script), spawn
the subagent directly:

```
Task(
  subagent_type="keboola-expert",
  description="<6-8 word summary>",
  prompt="<verbatim task>"
)
```

Same contract as the slash command, no `/keboola` indirection.

### When to use which

| Situation | Use |
|---|---|
| Quick read ("list, show, what is, find") | Skill auto-trigger |
| Any write operation | `/keboola <task>` -- forces dry-run + confirm |
| Long migration / multi-step refactor | `/keboola` per task, one task per response |
| Programmatic orchestration (scripts, CI) | `Task(subagent_type="keboola-expert", ...)` |

### The 8 non-negotiable rules the subagent enforces

The `keboola-expert` subagent carries these rules as hard constraints
you can rely on:

1. **Fresh fetch before write** -- never reuses a stale config dump.
2. **Dry-run first, then confirm, then apply** -- no direct mutative
   call without explicit go-ahead.
3. **Never chain `config update` + `job run`** in one response.
4. **Prefer CLI over MCP tool call** -- MCP only when CLI does not
   cover; on `isError: true`, fall back to `kbagent --hint client`
   instead of retrying with reformatted inputs.
5. **Prefer CLI over REST** -- never constructs raw
   curl/httpx/requests calls against `*.keboola.com`.
6. **Version gate** -- refuses the task if required commands are
   missing from the installed kbagent version, returning a repair
   path (`kbagent update`).
7. **Always use `--json`** on every `kbagent` invocation.
8. **Token discipline** -- never reads `.kbagent/config.json` to
   extract a token.

If you notice the subagent violating any of these, that is a bug;
file an issue with the prompt and the payload it returned.

### Debugging and observability

- Every subagent invocation returns a verification payload with
  timestamps. You can inspect `fresh_fetch_ts`, `dry_run_ts`,
  `apply_ts`, `commands_executed` directly in the response.
- Plain `kbagent` commands also accept `--verbose` for HTTP-level
  trace.
- `kbagent doctor` surfaces any configuration, connectivity, or
  plugin issues before they bite during a real task.

---

## 7. GitOps: sync configs as local files + git branches

Once projects are registered, you can treat their configurations as
ordinary source code: a directory tree of YAML/SQL/Python files under
git. `kbagent sync pull` downloads the configs; edits go through git
(`git diff`, PR review, merge); `kbagent sync push` uploads the result.

The killer feature is **git-branching mode**: each git feature branch
is bound to its own Keboola development branch. `sync pull`/`sync push`
from `main` touches production; from `feature/X` touches **only** the
dev branch `X`. Unlinked branches are refused outright -- there is no
path to accidentally push `feature/risky-experiment` to prod.

![kbagent sync pull + branch-link workflow](assets/demo-sync-pull.gif)

### 7.1 First-time setup

Run these in a dedicated directory (a fresh repo, or a subfolder of an
existing one). The sync state lives in `.keboola/`, the local project
config in `.kbagent/`, and the actual configs under `main/` (and later
`feature-*/`, one per linked branch).

```bash
cd ~/projects/padak
git init -q -b main
kbagent sync init --project padak-2-0 --git-branching
# -> creates .keboola/manifest.json      (project ID, apiHost, naming rules)
# -> creates .keboola/branch-mapping.json (git-branch -> Keboola-branch)

kbagent sync pull --project padak-2-0
# -> Pulled 55 configurations (42 rows) into main/
# -> Files written: 194
# -> Storage: 12 buckets, 87 tables
# -> Jobs: 55 configs with job history

git add -A && git commit -m "initial sync"
```

What you end up with on disk:

```
.
├── .kbagent/config.json              # token, local to this workspace
├── .keboola/
│   ├── manifest.json                 # config index + hashes (tracked)
│   └── branch-mapping.json           # git-branch -> Keboola-branch
├── .git/
└── main/                             # <-- production snapshot
    ├── extractor/
    │   └── keboola.ex-db-mysql/
    │       └── Academy/
    │           ├── _config.yml       # YAML: name, parameters, storage
    │           ├── _description.md   # description as Markdown
    │           └── _jobs.jsonl       # last N runs (JSONL)
    ├── transformation/
    │   └── keboola.snowflake-transformation/
    │       └── Adaptive/
    │           ├── _config.yml       # blocks EXCLUDED
    │           └── transform.sql     # SQL with /* ===== BLOCK: ... ===== */
    ├── application/
    │   └── kds-team.app-custom-python/
    │       └── MyApp/
    │           ├── _config.yml       # code + packages EXCLUDED
    │           ├── code.py
    │           └── pyproject.toml    # [tool.keboola] + dependencies
    └── storage/
        ├── buckets.json
        └── tables/                   # read-only metadata (not in manifest)
```

Two things to notice:

- SQL / Python code is **extracted** into real `.sql` / `.py` files,
  not stored as a YAML multiline string. Your IDE gets syntax
  highlighting, linters, and type-checkers; `git diff` shows per-line
  changes, not YAML escape chaos.
- Encrypted values stay as-is (`KBC::ProjectSecure::...`). sync pull
  never decrypts; sync push re-encrypts `#`-prefixed keys before
  upload. Nonce-only diffs are ignored in `sync diff` (no false
  positives).

### 7.2 Working on a feature branch

```bash
git checkout -b feature/update-etl
kbagent sync branch-link --project padak-2-0
# -> Success: Linked feature/update-etl -> Keboola branch 1294xxx

kbagent sync branch-status
# Branch: feature/update-etl
# Keboola: 1294xxx (feature/update-etl)
# Status: Linked
```

Every `sync pull` / `sync push` invoked from this git branch now
targets the Keboola dev branch. The first `sync pull` on a linked
branch writes into `feature-update-etl/` (the git-branch name,
slug-sanitised); subsequent pulls update it.

Edit loop:

```bash
# 1. Edit files with a normal editor
vim main/transformation/keboola.snowflake-transformation/Adaptive/transform.sql
#   (or feature-update-etl/... once you've pulled on the dev branch)

# 2. See what changed locally (no API call)
kbagent sync status
#   ~ main/transformation/keboola.snowflake-transformation/Adaptive/transform.sql

# 3. Compare local vs. remote dev branch (3-way diff, API call)
kbagent sync diff --project padak-2-0
#   Local changes (push would apply):
#     ~ MODIFIED keboola.snowflake-transformation/Adaptive
#       parameters.blocks.0.codes.0.script[0] changed: ...

# 4. Preview push (no writes)
kbagent sync push --project padak-2-0 --dry-run

# 5. Apply
kbagent sync push --project padak-2-0
#   Pushed: 0 created, 1 updated, 0 deleted
#   (version bumped in Keboola; change description: "Updated via kbagent sync push")
```

### 7.3 Merging back to production

```bash
# Get the Keboola UI merge URL for the dev branch
kbagent branch merge --project padak-2-0
# -> https://connection.keboola.com/admin/projects/10539/dev-branches/1294xxx

# Click the URL, confirm the merge in the Keboola UI.
# The dev branch is now merged into production and auto-deleted.

# Locally: merge the git branch, unlink, refresh main/
git checkout main
git merge --no-ff feature/update-etl
kbagent sync branch-unlink                      # mapping removed, Keboola branch not touched
kbagent sync pull --project padak-2-0 --force   # re-sync main/ with merged state
```

### 7.4 The safety model (why `--git-branching` is non-negotiable)

Without git-branching, `sync pull/push` always target whatever Keboola
branch is currently "active" for the alias -- which is easy to get
wrong. With git-branching, the target is a **pure function of the
current git branch**:

| git branch | `branch-mapping.json` entry | `sync push` target |
|---|---|---|
| `main` / `master` | `{"id": null, "name": "Main"}` | production |
| `feature/X` (linked) | `{"id": "1294xxx", "name": "feature/X"}` | dev branch 1294xxx |
| `feature/Y` (not linked) | *missing* | **refused, exit 5** |

Three consequences:

1. **You cannot push to prod from a feature branch.** The mapping
   simply doesn't point there. There is no flag to override this.
2. **An unlinked branch is blocked, not defaulted.** `sync pull/push`
   from a branch without a mapping exits with code 5 and a message
   telling you to run `sync branch-link` first. Silent fallback to
   production would be the dangerous behaviour; kbagent refuses.
3. **`main` is opt-in for production writes.** Pushing on `main`
   targets prod by design, so production changes go through
   `git checkout main && git merge` -- the same gate as any other
   prod deploy.

Test this once on a throwaway project (e.g. `kbagent org setup
--project-ids`). The only way to understand the safety boundary is
to hit it:

```bash
git checkout -b feature/unlinked
kbagent sync pull --project padak-2-0
# Error: Git branch 'feature/unlinked' is not linked to a Keboola branch.
# Run 'kbagent sync branch-link --project ALIAS' first.    (exit 5)
```

### 7.5 Common gotchas

| Situation | What to do |
|---|---|
| `sync pull` says `Skipped (N) -- locally modified` | A file was edited since the last pull. Run `sync diff` to inspect, `sync push` to upload your edits, or `sync pull --force` to overwrite local. |
| `sync push` reports `name drift warnings` | The local directory name doesn't match the config name (someone renamed the dir directly). Run `kbagent config rename` or `sync pull` to fix. |
| You already have a `.keboola/manifest.json` from the Go `kbc` CLI | `kbagent sync init --project X --adopt-existing` -- validates `project_id` against the alias's token, no overwrite. |
| You want a preview of a huge pull | `sync pull --dry-run --no-storage --no-jobs` -- prints what would be written without touching the disk. |
| You need CSV data samples too | `sync pull --with-samples` -- adds `storage/samples/{bucket}/{table}/sample.csv`. Encrypted columns mask to `***ENCRYPTED***`. |

For the full GitOps reference (3-way diff internals, row-level sync
for variables, secret encryption contract, `kbc` Go CLI compatibility),
see
[plugins/kbagent/skills/kbagent/references/sync-workflow.md](../plugins/kbagent/skills/kbagent/references/sync-workflow.md).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `kbagent: command not found` after `uv tool install` | Ensure `~/.local/bin` (or uv's tool dir) is on your PATH. `uv tool update-shell` can help. |
| `kbagent doctor` reports `warn` for plugin | Run the two `/plugin` commands shown in the warning, from inside Claude Code. |
| Plugin version != CLI version | In Claude Code: `/plugin update kbagent`. |
| `org setup` fails with `401 Unauthorized` | Your `KBC_MANAGE_API_TOKEN` is wrong for this stack or role. Manage tokens are stack-specific and require the right scope. |
| `org setup --org-id` fails with `403` | You are not an org admin. Use `--project-ids` with a Personal Access Token instead (works for any project member). |
| Changes from `kbagent config update` do not show in UI | You are on a dev branch. Run `kbagent branch list` and `kbagent project current` to verify the active branch; changes in a dev branch merge to production only via the UI merge step (`kbagent branch merge` returns the merge URL). |
| The specialist subagent does not spawn when I type `/keboola X` | Plugin is not installed or is outdated. Run `kbagent doctor` and follow the reported commands. |

---

## Where to go next

- **Permissions and sandboxing**: [docs/guide.md](guide.md)
- **All CLI commands with flags**: [plugins/kbagent/skills/kbagent/SKILL.md](../plugins/kbagent/skills/kbagent/SKILL.md)
  or run `kbagent context`.
- **Common workflows and use-cases**: [docs/use-cases.md](use-cases.md)
- **Error codes**: [docs/error-codes.md](error-codes.md)
- **Developing kbagent itself**: [CONTRIBUTING.md](../CONTRIBUTING.md)
