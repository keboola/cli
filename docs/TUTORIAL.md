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
uv tool install git+https://github.com/keboola/cli
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
# Interactive (default since v0.29.0): kbagent prompts for the Manage API
# token on stdin. No env var, no shell history.
kbagent org setup \
  --project-ids 901,9621,10539 \
  --url https://connection.keboola.com \
  --dry-run                           # always dry-run first

# CI / non-interactive: opt in to env-var resolution.
export KBC_MANAGE_API_TOKEN=YOUR_MANAGE_OR_PAT_TOKEN
kbagent --allow-env-manage-token org setup \
  --project-ids 901,9621,10539 \
  --url https://connection.keboola.com \
  --dry-run
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

**Security note (since v0.29.0)**: `KBC_MANAGE_API_TOKEN` is **ignored
by default** -- the env var is read only when the top-level
`--allow-env-manage-token` flag is passed. Without the flag, kbagent
prompts on stdin (hidden input). kbagent never accepts the token as a
CLI argument (`--token xxx`) -- that would leak into shell history and
process listings. The default-deny closes an AI-exfiltration risk where
any subprocess running as the same user (including the AI agent itself)
inherits env vars.

---

## 3. Add a whole organization (you are org admin)

If you are an org admin with a Manage API token, register **every**
project in an organization in one shot:

```bash
# Interactive (default since v0.29.0):
kbagent org setup \
  --org-id 123 \
  --url https://connection.keboola.com \
  --dry-run

# CI / non-interactive:
export KBC_MANAGE_API_TOKEN=YOUR_ORG_ADMIN_MANAGE_TOKEN
kbagent --allow-env-manage-token org setup \
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
/plugin marketplace add keboola/cli
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
   cover; on `isError: true`, fall back to the `kbagent serve` REST API
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

## 8. Advanced storage: native column types + dev-branch materialize

`kbagent storage create-table` accepts the seven Keboola base types
(`STRING`, `INTEGER`, `NUMERIC`, `FLOAT`, `BOOLEAN`, `DATE`, `TIMESTAMP`)
**and** any native backend type the Storage API supports. The CLI does
only syntactic validation; length/precision/scale validation is delegated
to Keboola, which returns precise per-backend errors.

In a dev branch, a `create-table` call against an unmaterialized bucket
does not fail with `Bucket not found` -- kbagent auto-creates the bucket
in the branch first, mirroring the official Keboola Go CLI's
`EnsureBucketExists`. The response surfaces this via
`auto_created_bucket: true`.

![storage create-table with native types and branch auto-materialize](assets/demo-storage-types.gif)

### 8.1 `--column` grammar

```
--column name                      # bare name -> STRING (backend default)
--column name:TYPE                 # e.g. id:INTEGER
--column name:TYPE(length)         # e.g. amount:NUMERIC(18,2), pk:VARCHAR(40)
```

Attribute flags (repeatable):

- `--not-null COL` -- marks a defined column `nullable: false`
- `--default NAME=VALUE` -- sets a `DEFAULT` expression. Booleans must be
  lowercase (`--default flag=false`); uppercase is rejected by the API.

Unknown names passed to `--not-null` or `--default` exit 2
(`INVALID_ARGUMENT`) before any API call -- typos fail fast.

### 8.2 Retyping after profiling -- a real use case

Picture an existing table `out.c-adaptive.out` with 128k rows. You ran
`kbagent workspace query` to profile it and now know exactly what fits:

- `pkey` is a SHA-1, exactly 40 characters long
- `tz_offset` is always within `[-25200, 25200]`
- `num_members` tops out at 51
- `ts` is an RFC-822 datetime

The old `--column` flag hid all of that information and collapsed every
string to the default `VARCHAR(16777216)`. With 0.25.0+:

```bash
kbagent --json storage create-table \
  --project prod --bucket-id in.c-slack --name messages \
  --column 'pkey:VARCHAR(40)' \
  --column 'channel_id:VARCHAR(20)' \
  --column 'tz_offset:NUMBER(6,0)' \
  --column 'num_members:NUMBER(3,0)' \
  --column 'ts:TIMESTAMP_TZ' \
  --column 'ch_name:VARCHAR(80)' \
  --column 'is_admin:BOOLEAN' \
  --primary-key pkey \
  --not-null pkey --not-null ts \
  --default num_members=0 --default is_admin=false
```

Wrap each `name:TYPE(length)` spec in single quotes (or escape the
parentheses with `\(...\)`); the shell otherwise treats `(` and `)`
specially.

### 8.3 Dev branch auto-materialize

Keboola dev branches have an isolated storage namespace. A production
bucket is transparently readable from a branch but cannot accept
branch-scoped writes until it is **materialized** there (a branch-local
bucket with the same ID). Without kbagent, you had to manually call
`create-bucket --branch <id>` first; with 0.25.0+ `create-table` does it
for you:

```bash
# Production bucket in.c-archive exists but this branch is fresh.
kbagent --json storage create-table \
  --project prod --branch 1234567 \
  --bucket-id in.c-archive --name snapshot \
  --column id:INTEGER --column payload:VARIANT \
  | jq '.data.auto_created_bucket'
# true
```

The second `create-table` against the same bucket in the same branch
returns `auto_created_bucket: false` -- the bucket is already there.

Production writes (no `--branch`) never materialize anything.

### 8.4 Snowflake type cheat sheet

| CLI input | Snowflake result | `basetype` |
|---|---|---|
| `STRING` | `VARCHAR(16777216)` | `STRING` |
| `VARCHAR(n)` / `CHAR(n)` | `VARCHAR(n)` | `STRING` |
| `TEXT` | `VARCHAR(16777216)` | `STRING` |
| `INTEGER` | `NUMBER(38,0)` | `INTEGER` |
| `NUMERIC` | `NUMBER(38,9)` | `NUMERIC` |
| `NUMBER(p,s)` / `DECIMAL(p,s)` | `NUMBER(p,s)` | `NUMERIC` |
| `FLOAT` / `DOUBLE` | `FLOAT` | `FLOAT` |
| `BOOLEAN` | `BOOLEAN` | `BOOLEAN` |
| `DATE` | `DATE` | `DATE` |
| `TIMESTAMP` / `TIMESTAMP_NTZ` | `TIMESTAMP_NTZ(9)` | `TIMESTAMP` |
| `TIMESTAMP_LTZ` | `TIMESTAMP_LTZ(9)` | `TIMESTAMP` |
| `TIMESTAMP_TZ` | `TIMESTAMP_TZ(9)` | `TIMESTAMP` |
| `TIME` | `TIME` | `STRING` |
| `VARIANT` / `OBJECT` / `ARRAY` | `VARIANT` / `OBJECT` / `ARRAY` | `STRING` |

The `basetype` column is what the API derives automatically and what
downstream Keboola components read for schema inference. You do not
need to pass `basetype` manually.

For the full reference including the BOOLEAN/INTEGER gotchas, see
[plugins/kbagent/skills/kbagent/references/storage-types-workflow.md](../plugins/kbagent/skills/kbagent/references/storage-types-workflow.md).

---

## 9. Data apps lifecycle

A Keboola data app is **not one resource** -- it's a deployment record on
the Data Science API plus a configuration on the Storage API
(`keboola.data-apps`), and they have to stay in sync. Each API only owns
part of the picture, and the obvious-looking calls have four documented
footguns the platform does not surface as errors:

1. **The redeploy contract.** `PATCH /apps/{id}` with bare
   `desiredState=running` silently pins to the empty shell that
   `POST /apps` minted, so the runner errors `dataApp.git.repository is
   required in /data/config.json` -- only visible in the UI Terminal Log,
   never in any HTTP response.
2. **Per-project KMS encryption.** Encrypted git PATs (`KBC::ProjectSecure*`)
   are bound to the project that minted them; ciphertext from project A
   does not decrypt in project B.
3. **Cleanup-in-finally.** A failed initial deploy leaks the empty shell
   from `POST /apps` if the caller does not delete it manually.
4. **Transient `state == stopped`.** During the very first deploy the
   platform transitions `created -> stopped -> starting -> running`, so a
   naive poll that exits on `stopped` reports a phantom failure.

`kbagent data-app` (since 0.27.0) encodes all four in the service layer,
so the `--json` output you see at the CLI is what would have happened if
you had done everything right at the raw HTTP level. The eight
subcommands -- `list`, `detail`, `create`, `deploy`, `start`, `stop`,
`delete`, `password` -- cover the full lifecycle end to end.

### 9.1 Public-repo golden path

The simplest path: a public git repo, no auth gate, three commands from
zero to a running container.

```bash
# Register the project once (Storage API token from the UI).
kbagent project add --project prod \
  --url https://connection.keboola.com \
  --token YOUR_STORAGE_TOKEN

# Create the deployment shell + Storage config in one shot.
kbagent --json data-app create \
  --project prod \
  --name "Hello data app" \
  --slug hello \
  --git-repo https://github.com/<owner>/<small-public-repo> \
  --git-public --auth public --no-deploy \
  | jq -r '.data | "id=\(.id) config_id=\(.config_id) config_version=\(.config_version)"'
# id=12345678 config_id=01abcdefghijklmnopqrstuvwxyz config_version=3
```

`type: "python-js"` (the default) covers BOTH a Node app
(`package.json` + entry point) and a Python app (`requirements.txt` +
`app.py`). The runtime auto-detects from what's in the repo.

The `configVersion=3` is the Storage config version after kbagent's PUT.
Storage went `1 -> 2` (the empty shell that `POST /apps` minted, with an
auto-injected `parameters.id` back-pointer to the deployment record) and
`2 -> 3` (the full body with the git block + runtime block + auth block).

```bash
# Deploy: pins the deployment record to configVersion=3 and waits.
kbagent --json data-app deploy \
  --project prod --app-id 12345678 --wait --timeout 300 \
  | jq -r '.data | "state=\(.state) url=\(.url)"'
# state=running url=https://hello-12345678.hub.keboola.com
```

Visit the URL. That's the entire round-trip: ~30s for a small Node
app, longer for the first cold-boot of a heavier Python app.

### 9.2 Private-repo golden path

For a private repo you need a GitHub PAT with `repo:read` scope.
Two non-negotiables:

- **Pass it via env, not argv.** `--git-pat-env GITHUB_PAT_DATAAPP`
  reads the PAT from the named environment variable. The plaintext
  never appears in your shell history, in `ps aux`, or in any kbagent
  output.
- **The PAT is encrypted under THIS project's KMS** before reaching
  Storage. `kbagent data-app create` calls the Encryption API
  (`encryption.<stack>/encrypt`) and only writes the resulting
  `KBC::ProjectSecure*` ciphertext. Ciphertext from one project's
  config does NOT decrypt in another -- copying an encrypted git block
  across projects via raw `kbagent config update` produces a runtime
  failure, not a clear error.

```bash
export GITHUB_PAT_DATAAPP=ghp_xxxxxxxxxxxxxxxxxxxx

kbagent --json data-app create \
  --project prod \
  --name "Internal dashboard" \
  --slug internal-dashboard \
  --git-repo https://github.com/<owner>/<private-repo> \
  --git-username YOUR_GITHUB_USER \
  --git-pat-env GITHUB_PAT_DATAAPP \
  --auth password --wait --timeout 300
```

`--auth password` (the default) wraps the app in a simpleAuth gate. The
20-character hex password is auto-generated by the platform on first
deploy. To retrieve it:

```bash
# Manage API token: interactive prompt by default (since v0.29.0). For CI,
# add `--allow-env-manage-token` and set KBC_MANAGE_API_TOKEN in env.
kbagent --json data-app password \
  --project prod --app-id 12345678 \
  | jq -r '.data.password'
# <20-character hex password, e.g. a1b2c3d4e5f6a7b8c9d0>
```

The password cannot be rotated; to change it, delete and recreate the
app. (See [§3](#3-add-a-whole-organization) for `KBC_MANAGE_API_TOKEN`
setup -- it is the same Manage token `org setup` uses.)

### 9.3 Roll out a new version: `data-app deploy` after `config update`

This is the easiest gotcha to fall into. Editing the data-app's Storage
config bumps the **Storage** version, but the deployment record's
`configVersion` is a **pinned pointer** that does NOT auto-advance:

```bash
# Bump auto-suspend from 15 minutes to 60.
kbagent --json config update \
  --project prod --component-id keboola.data-apps --config-id 01abcdefghijklmnopqrstuvwxyz \
  --set 'parameters.autoSuspendAfterSeconds=3600' --merge \
  | jq -r '.data.version'
# 4

# At this point the running container is still at configVersion=3.
# Verify with detail:
kbagent --json data-app detail --project prod --app-id 12345678 \
  | jq -r '.data | "storage=\(.config_version_storage) deployed=\(.config_version_deployed)"'
# storage=4 deployed=3      <-- not in sync

# Roll out: re-pin the deployment to the latest Storage version.
kbagent --json data-app deploy --project prod --app-id 12345678 --wait
```

`data-app deploy` reads the latest Storage version, then PATCHes the
deployment record with the trio
`{desiredState=running, configVersion=<latest>, restartIfRunning=true}`
together. Sending only one or two of those returns HTTP 422 from the
platform; the CLI always sends all three. This is the **redeploy
contract** -- the headline of the feature.

`data-app start` is a different command for a different job. It wakes a
container the platform parked due to `autoSuspendAfterSeconds` of
inactivity. It does NOT bump the deployed `configVersion` -- it just
reuses whatever was pinned. Use `start` for waking a parked container;
use `deploy` for rolling out a new code or config version.

### 9.4 `stop` is reversible, `delete` is not

```bash
# Reversible: tears down the container, preserves the URL + Storage config.
kbagent --json data-app stop --project prod --app-id 12345678 --wait
# state=stopped, desiredState=stopped

# Wake it back up at the same configVersion (no version bump).
kbagent --json data-app start --project prod --app-id 12345678 --wait
# state=running

# Or: just hit the URL. The platform typically auto-wakes parked
# containers on incoming HTTP traffic (~30-60s cold-boot). Use
# `data-app start` when you want an explicit, observable wake.
```

```bash
# Irreversible: the Data Science API cascade-deletes the deployment
# record AND the linked Storage config server-side. The URL is
# permanently retired.
kbagent --json data-app delete --project prod --app-id 12345678 --yes
```

A second `data-app list` after the delete returns the project's other
apps; the deleted app does not come back, and a fresh `create` against
the same slug mints a new numeric `id` (the URL hostname embeds the
numeric id, so even with the same slug the new URL differs).

For the full reference including the API endpoints behind each command,
the four-footgun mental model in detail, and encrypted-PAT round-trip
shapes, see
[plugins/kbagent/skills/kbagent/references/data-app-workflow.md](../plugins/kbagent/skills/kbagent/references/data-app-workflow.md).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `kbagent: command not found` after `uv tool install` | Ensure `~/.local/bin` (or uv's tool dir) is on your PATH. `uv tool update-shell` can help. |
| `kbagent doctor` reports `warn` for plugin | Run the two `/plugin` commands shown in the warning, from inside Claude Code. |
| Plugin version != CLI version | In Claude Code: `/plugin update kbagent`. |
| `org setup` exits 2 with `Warning: KBC_MANAGE_API_TOKEN found in environment but ignored` | Default-deny since v0.29.0 -- pass `--allow-env-manage-token` (top-level flag) to opt in to env resolution, or run interactively to use the prompt. |
| `org setup` fails with `401 Unauthorized` | Your manage token is wrong for this stack or role. Manage tokens are stack-specific and require the right scope. |
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
