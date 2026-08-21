# kbagent -- Keboola Agent CLI

One CLI to manage all your Keboola projects. Designed to be driven by AI agents -- Claude Code, Codex, Gemini, Cursor -- but works great standalone too.

No more switching between the UI, the old CLI, and raw API calls. `kbagent` wraps everything into workflow-oriented commands where dev branches propagate automatically, multi-project operations run in parallel, and AI agents can be sandboxed safely.

![kbagent in action](docs/assets/demo-hero.gif)

## Install

```bash
curl -LsSf https://raw.githubusercontent.com/keboola/cli/main/install.sh | sh
```

This installs a **prebuilt wheel** from the latest GitHub release -- a few-seconds download, no source build. Building from `git+` instead recompiles the bundled React SPA via npm on every install, which takes minutes on WSL ([#353](https://github.com/keboola/cli/issues/353)). The script bundles the `[server]` extras by default (set `KBAGENT_NO_SERVER=1` for a CLI-only install) and needs only `curl` + [`uv`](https://docs.astral.sh/uv/).

Requires **Python >=3.12**. `uv` normally fetches a matching interpreter on its own even if your default Python is older, but if it doesn't (offline, or Python downloads disabled) the install fails with `does not satisfy Python>=3.12` -- set `UV_PYTHON` (a standard `uv` env var) to a 3.12-or-newer interpreter you actually have -- `UV_PYTHON=3.12`, `UV_PYTHON=3.13`, or a full path. It has to reach `sh`, not `curl` -- `curl -LsSf https://raw.githubusercontent.com/keboola/cli/main/install.sh | UV_PYTHON=3.12 sh`, or `export UV_PYTHON=3.12` beforehand. Prefixing the whole pipeline (`UV_PYTHON=3.12 curl ... | sh`) assigns it to `curl`, where it has no effect.

Prefer to build from source, or pin a specific ref?

```bash
uv tool install git+https://github.com/keboola/cli
```

### Windows

The `curl ... | sh` one-liner at the top needs a POSIX shell. Windows has shipped `curl.exe` since Windows 10 1803, but it has no `sh` -- so the command gets halfway and then fails with `'sh' is not recognized`, in both `cmd` and PowerShell. Installing Git for Windows does not fix it either: the installer only puts `C:\Program Files\Git\cmd` on `PATH`, and `sh.exe` lives in `usr\bin`.

Pick whichever fits the machine:

**PowerShell, no POSIX shell needed** -- the same wheel install.sh would have fetched:

```powershell
winget install --id astral-sh.uv -e            # skip if uv is already installed
$ver = (Invoke-RestMethod "https://api.github.com/repos/keboola/cli/releases/latest").tag_name.TrimStart('v')
uv tool install --force `
  "keboola-cli[server] @ https://github.com/keboola/cli/releases/download/v$ver/keboola_cli-$ver-py3-none-any.whl"
uv tool update-shell                            # puts %USERPROFILE%\.local\bin on PATH
```

Works in the in-box Windows PowerShell 5.1 -- no PowerShell 7 needed.

Then **open a new shell** -- `uv tool update-shell` edits the persisted `PATH`, and the current session will not see it.

**Already have Git for Windows?** Run the documented script through its bash:

```powershell
& "C:\Program Files\Git\bin\bash.exe" -lc "curl -LsSf https://raw.githubusercontent.com/keboola/cli/main/install.sh | sh"
```

**No Python on the machine?** Every release ships a self-contained binary -- download `keboola-cli2_<version>_windows_amd64.zip` from the [releases page](https://github.com/keboola/cli/releases/latest), unpack it, and put the folder on `PATH`. It carries its own interpreter, needs neither Python nor uv, and (since 0.79.0) will not try to self-update -- it tells you to re-download instead.

A note on package managers, so nobody loses an afternoon to it: there is **no WinGet package** -- nothing has been submitted to `microsoft/winget-pkgs` -- and the **Chocolatey package lags far behind** (`keboola-cli2` was still on 0.66.1 while the current release was 0.80.0). Check its version before trusting it, or just use one of the three paths above.

A fresh Windows install has no real Python either: `python` resolves to a Microsoft Store stub that only prints an ad for the Store, so a script that probes "is `python` on PATH?" will wrongly conclude yes. None of the paths above need Python on `PATH` -- uv brings its own.

Auto-updates kbagent on every launch; the self-update prefers the prebuilt wheel when available. Run `kbagent changelog` to see what changed. (Since 0.85.0 kbagent updates itself only -- if you also run `keboola-mcp-server` in Claude Desktop / Cursor, refresh it yourself with `uv tool install --upgrade --prerelease=allow keboola-mcp-server`.)

## Web UI (optional)

Want a browser dashboard? One command:

```bash
uv tool install --with 'keboola-cli[server]' 'git+https://github.com/keboola/cli'
kbagent serve --ui
# Open the URL printed at startup -- the browser is auto-authenticated.
```

The React SPA is bundled inside the wheel by a hatchling build hook (requires Node 20+ on the install host so `npm run build` can run during wheel creation). Single Python process at runtime; no Node needed once installed. Covers everything the CLI exposes (projects, configs, storage, jobs, flows, schedules, lineage, scheduled AI agents with cost/token timeline). Agent runs that produce long-form reports (e.g. "Storage Cleanup Advisor", "Schedule Drift Detector") surface in a dedicated **Artifacts** tab — GFM-rendered preview in a VSCode-style viewer with one-click Copy / Download `.md` for hand-off to Slack, Notion, or your editor. See [`web/README.md`](web/README.md) for the dev-mode setup with hot reload.

## Agent Tasks

Schedule AI agents to run **inside `kbagent serve`** -- cron, manual triggers, or chained (one agent finishes, another starts). Each task picks one of two action flavours:

- **AI agent** -- `claude` / `codex` / `gemini` with a custom prompt. The subprocess inherits `KBAGENT_SERVE_URL` + `KBAGENT_SERVE_TOKEN` so it calls back via `kbagent http get /...` instead of forking fresh CLI processes against stale config.
- **Raw kbagent CLI** -- any `kbagent ...` command with its args (encrypted secrets supported).

Every run is recorded as a persisted timeline (JSONL on disk, `0600`) with authoritative cost & token accounting (Opus 4.7 / Sonnet 4.6 / Haiku 4.5 pricing built-in) and per-step replay over SSE. Long-form markdown reports (e.g. "Storage Cleanup Advisor", "Schedule Drift Detector") auto-surface in a dedicated **Artifacts** tab with GFM preview + one-click Copy / Download `.md` for Slack, Notion, or your editor.

Build the agent once, schedule it, walk away — the platform handles auth, scheduling, history, cost reporting, and report rendering.

### Get started

The scheduler runs **inside `kbagent serve`** -- the same single Python process that hosts the Web UI. If you already installed kbagent with the `[server]` extras (see [Web UI](#web-ui-optional) above), you're set; otherwise:

```bash
uv tool install --with 'keboola-cli[server]' 'git+https://github.com/keboola/cli'
kbagent serve --ui
# Open the URL printed at startup -> sidebar "Agent Tasks" -> "+ New task".
```

Keep `kbagent serve` running for the scheduler to fire CRON triggers. Run history is persisted (JSONL on disk), so closing the server doesn't lose past runs -- it just pauses future scheduling until you restart. Architecture detail and the full endpoint reference live in [`docs/web-server.md`](docs/web-server.md).

## For AI agents

This CLI is built AI-first. Every command outputs structured JSON (`--json`), errors include machine-readable codes, and the permission firewall enforces safety at the code level -- not via prompt instructions.

**Claude Code plugin** (agent learns all 100+ commands + gets a specialist subagent for writes):

```
/plugin marketplace add keboola/ai-kit
/plugin install kbagent@keboola-claude-kit
```

Then either let the `kbagent` skill auto-trigger from natural prompts, or delegate explicitly with `/keboola <task>` -- the slash command spawns a `kbagent:keboola-expert` subagent with fresh context, hard rules (fresh fetch, dry-run first, prefer CLI over raw REST, version gate), and a JSON verification payload. See [docs/TUTORIAL.md §6](docs/TUTORIAL.md#6-using-the-agent-and-slash-commands).

**Any other agent** -- just tell it to run `kbagent context` and it gets the full command reference.

**What you can ask your agent:**

> "Give me a full inventory of all Keboola projects -- configs, jobs, components, data volumes."

> "Find the last failed job in project X, figure out why it crashed, spin up a workspace with the input data, and fix the SQL."

> "Compare the SQL transformation between production and the dev branch."

> "Create a new Snowflake transformation that joins orders and customers, push it to a dev branch."

> "Set up a weekly Storage Cleanup advisor that flags orphan tables, estimates monthly Snowflake savings, and writes a markdown report I can read in the dashboard."

### Sandboxing

```bash
kbagent init --from-global --read-only
```

Three protection layers (kbagent policy + filesystem chmod + Claude Code deny rules) prevent the agent from writing, deleting, or bypassing restrictions. See [Permissions Guide](docs/guide.md#permissions) for details.

## Use as a library

Besides the CLI and `kbagent serve`, kbagent exposes a small **stateless, importable client** for in-process use -- a Keboola Data App, a transformation, or any Python service can run Query Service SQL and read/write Storage Files without spawning the CLI, running the daemon, or maintaining a config-dir. Auth is the storage token you pass in (12-factor); nothing is written to disk.

```python
import os
from keboola_agent_cli import Client

with Client(url=os.environ["KBC_URL"], token=os.environ["KBC_TOKEN"]) as kbc:
    rows = kbc.query(workspace_id, "SELECT id, name FROM customers")  # list[dict]

    meta = kbc.files.upload(b"hello", name="greeting.txt", tags=["demo"])
    data = kbc.files.read_bytes(meta.id)                              # bytes
    files = kbc.files.list(tags=["demo"])                             # list[FileEntry]
```

`query()` reads results inline and returns rows keyed by column name; `files` returns a uniform `FileEntry` shape and reads bytes straight into memory; `run_job()` / `config_detail()` / `upload_table()` return typed pydantic models. Everything exported from `keboola_agent_cli` is committed public API (semver). For lower-level endpoints, reach for `Client.raw` (the underlying `KeboolaClient`).

**Full SDK reference:** [docs/sdk.md](docs/sdk.md) -- the deep guide (every method, the typed result-model contract, `py.typed`, idempotent `run_job`, gotchas, and how to extend the SDK). **Runnable demo:** [`examples/storage_tui/`](examples/storage_tui/) -- a terminal Storage browser built entirely on this `Client`.

## 30-second demo

![30-second demo](docs/assets/demo-readme-main.gif)

```bash
# Connect a project (Storage API token from Keboola UI)
kbagent project add --project prod \
  --url https://connection.keboola.com --token YOUR_TOKEN

# Find anything (table / config / flow / data app) across ALL projects in one call
kbagent search "customer_id"

# Or scan inside config bodies (slower, deeper)
kbagent config search --query "customer_id"

# Run a job and wait for it to finish (with log tail on failure)
kbagent job run --project prod --component-id keboola.ex-db-snowflake \
  --config-id 456 --wait --log-tail-lines 200

# Debug a failing SQL transformation with real data (no full job needed)
kbagent workspace from-transformation --project prod \
  --component-id keboola.snowflake-transformation --config-id 789
kbagent workspace query --project prod --workspace-id WS_ID \
  --sql "SELECT * FROM users LIMIT 10"
```

## What it does

| Area | What you get |
|------|-------------|
| **Multi-project** | All read commands query every connected project in parallel. One command, all projects. |
| **Search** | `kbagent search "QUERY"` -- find tables, configs, flows, data apps across every connected project in one call (since 0.30.0). Backed by Storage `global-search`; falls back to per-project body scan with `--search-type config-based`. |
| **Configurations** | List, search, inspect, scaffold, update, delete configs. Full-text search across all config bodies (incl. rows). Row CRUD (`row-create / row-update / row-delete`) with `--merge`, `--set`, `--dry-run`, `--is-disabled / --is-enabled` (since 0.30.0). OAuth wizard URL minting with short-lived child tokens (`config oauth-url`, since 0.30.0). Variables management (`variables-set / -get / -clear`). Metadata CRUD + folder grouping. Output-bucket override (`set-default-bucket`). String-script auto-normalize for SQL transformations (closes the silent runtime crash from #245, since 0.28.0). |
| **Jobs** | List, inspect, run with `--wait` polling (exponential curve), `--timeout` auto-kill, log tail on failure. Row-level execution for multi-row configs. Bulk terminate by ID list or filter (`job terminate --status processing` -- since 0.20.2). |
| **Flows** | Create, update, delete **conditional flows** (`keboola.flow`) with schema-backed validation (`next[].goto` transitions + conditions; typed `job`/`notification`/`variable` tasks; string ids). Offline `flow validate` and `flow schema --full`. Attach cron schedules (timezone + enabled/disabled state). `keboola.orchestrator` is not supported (dropped in 0.57.0). |
| **Storage** | Buckets, tables, files -- full CRUD. Upload CSV (auto-creates bucket+table). Download by file ID or by tag. Descriptions on buckets/tables/columns (batch-applicable from YAML). Native column types (`VARCHAR(40)`, `NUMBER(18,2)`, `TIMESTAMP_TZ`, `VARIANT`, ...) with per-column `--not-null` and `--default` flags; dev branches auto-materialize target buckets on first write. **`storage swap-tables`** -- atomically swap a typed rebuild back into the original table name in a dev branch without touching downstream config references (since 0.28.0; closes the typify migration footgun). Streamed downloads cap memory at ~1 MiB regardless of table size. Parquet export via `unload-table --file-type parquet`. BigQuery dialect-aware paths in `bucket-detail`. |
| **Dev branches** | Create a branch, activate it, and every command auto-targets it. Configs, storage writes, sync -- everything follows. Storage reads default to production (safer). |
| **Sync & GitOps** | Pull configs as YAML, edit in IDE, push back. SQL/Python extracted as real files. Diff and status tracking. Adopt existing kbc Go CLI checkouts (`sync init --adopt-existing`). |
| **Agent Tasks** | Schedule AI agents inside `kbagent serve` (CRON / manual / chained). Two action flavours per task: `claude` / `codex` / `gemini` with a prompt, or a raw kbagent CLI command. Per-run cost & token timeline with authoritative Claude 4.x pricing built-in; persisted JSONL history (`0600`); live SSE replay; **Artifacts tab** auto-renders long-form markdown reports (GFM tables, Copy / Download `.md`). Subprocesses get `KBAGENT_SERVE_URL` + `KBAGENT_SERVE_TOKEN` auto-injected for self-calls via `kbagent http`. (since 0.40.0) |
| **Workspaces** | Create Snowflake/BQ workspace, load tables, run SQL. Create from transformation config for instant debugging. Orphan detection + garbage collection. |
| **Sharing** | Cross-project bucket sharing with org/project/user access control. Share, link, unlink. |
| **Data apps** | First-class lifecycle for Streamlit / Flask / Node deployments (`keboola.data-apps`). `create / deploy / start / stop / password / delete` (since 0.27.0); `secrets-set / -list / -get / -remove` for `#`-prefixed runtime secrets with per-project KMS encryption (since 0.29.0); `validate-repo` pre-flight Golden Rule check that catches misconfigured git repos before a deploy (since 0.29.0); `logs` tails the container log buffer for triaging stuck deploys / runtime crashes (since 0.43.8). Hides the redeploy contract and per-project KMS encryption of git PATs. |
| **Project members & invitations** | `project invite` (single or `--from-csv` bulk with parallel workers), `project member-list / member-remove / member-set-role`, `project invitation-list / invitation-cancel`. Role whitelist enforced at the CLI layer; Manage API "already invited" treated as `noop` not error (since 0.29.0). |
| **Lineage** | Column-level dependency analysis across projects. SQL/Python parsing, AI-enhanced detection, interactive web browser, Mermaid/HTML/ER export. |
| **Semantic layer** | Define and manage a metastore semantic model per project — datasets, metrics, relationships, constraints, glossary. Validate (incl. `--deep`), export, diff two models/files, import/promote across projects, AI-assisted `build` from tables. `kbagent semantic-layer ...` (alias `sl`). |
| **Kai (AI Assistant)** | Ask Keboola's built-in AI questions about your project. One-shot or chat sessions with full MCP context. |
| **Encryption** | Encrypt secrets (`#password`, `#api_token`) via Keboola Encryption API. Works with `config` writes and `sync push`. |
| **Permissions** | Firewall for AI agents: read-only, deny-writes, deny-destructive (session-only flags or persisted policy). Project pin + `KBAGENT_PROJECT` env override. Code-level enforcement, stable `ErrorCode` enum, not prompt tricks. |
| **Auto-update** | Self-updates kbagent on every startup. "What's new" after each update. Full changelog via `kbagent changelog`. |

## Setup options

Four ways to register projects, depending on what you have. If you are a human
at a terminal with a browser, start with **browser login** (last one below);
the token-based options are the ones to use for CI and anything unattended.

**Single project** — you have a Storage API token from the UI:
```bash
kbagent project add --project prod --url https://connection.keboola.com --token YOUR_TOKEN
```

**Many projects by ID** — you have a Manage API or Personal Access Token + the project IDs:
```bash
# Interactive: kbagent will prompt for the Manage API token (default since v0.28.0).
kbagent org setup --project-ids 901,9621,10539 --url https://connection.keboola.com --yes

# CI / non-interactive: opt in to env-var resolution with --allow-env-manage-token.
KBC_MANAGE_API_TOKEN=your-manage-or-personal-token \
  kbagent --allow-env-manage-token org setup --project-ids 901,9621,10539 --url https://connection.keboola.com --yes
```

**Whole organization** — you are org admin:
```bash
# Interactive (default since v0.28.0): kbagent prompts for the Manage API token.
kbagent org setup --org-id 123 --url https://connection.keboola.com --yes

# CI / non-interactive:
KBC_MANAGE_API_TOKEN=your-org-admin-manage-token \
  kbagent --allow-env-manage-token org setup --org-id 123 --url https://connection.keboola.com --yes
```

**Browser login** — you have no token and a browser on this machine (since 0.80.0):
```bash
# Opens a browser (PKCE), or prints a code to type in on another device.
kbagent auth login --stack https://connection.keboola.com

# Then pick which of the accessible projects to register locally.
kbagent auth register-projects            # interactive picker
kbagent auth register-projects --all      # non-interactive
```
> **Needs a human at a browser.** There is no headless path, so never run
> `auth login` from an unattended AI-agent task or a CI step — use a static
> Storage token there. Session-registered projects also do not work with every
> command (Kai, data apps, semantic layer, streams, the Python SDK need a
> static token). Details, capability matrix and error codes:
> [docs/auth.md](docs/auth.md).

Run `kbagent doctor` to verify setup (token validity, CLI version, Claude Code plugin install).

> **Step-by-step guide with dry-runs, token descriptions, expiry, and
> global-vs-local config:** see [docs/TUTORIAL.md](docs/TUTORIAL.md).

## All commands

Full command reference with flags: [SKILL.md](plugins/kbagent/skills/kbagent/SKILL.md)

```
kbagent search      QUERY [--type table|bucket|config|flow|data-app|transformation]   # cross-project search (0.30.0)
kbagent auth        login | status | register-projects | logout   # browser login, needs a human (0.80.0)
kbagent project     add | list | remove | edit | status | refresh | info | use | current
                    description-get | description-set
                    invite | member-list | member-remove | member-set-role
                    invitation-list | invitation-cancel
kbagent org         setup
kbagent feature     list | project-show | project-add | project-remove
                    user-show | user-add | user-remove   # super-admin Manage API (0.48.0)
kbagent component   list | detail
kbagent dev-portal  identity (add|list|remove|edit|use|current|verify)
                    list | get | create | patch | upload-icon | publish | deprecate   # Developer Portal (0.49.0)
kbagent config      list | detail | search | update | set-default-bucket | rename | delete | new
                    metadata-list | get-metadata | set-metadata | delete-metadata | set-folder
                    variables-set | variables-get | variables-clear
                    row-create | row-update | row-delete
                    oauth-url
kbagent job         list | detail | run | terminate
kbagent flow        list | detail | schema | validate | new | update | delete | schedule | schedule-remove
kbagent storage     buckets | bucket-detail | create-bucket | delete-bucket
                    tables | table-detail | create-table | upload-table | download-table
                    delete-table | truncate-table | delete-column | swap-tables | clone-table
                    describe-bucket | describe-table | describe-column | describe-batch
                    files | file-detail | file-upload | file-download | file-tag | file-delete
                    load-file | unload-table
kbagent stream      list | create-source | detail | delete   # Data Streams / OTLP (0.50.0)
kbagent sharing     list | share | unshare | link | unlink | edges
kbagent data-app    list | detail | create | deploy | start | stop | delete | password | logs
                    secrets-set | secrets-list | secrets-get | secrets-remove
                    validate-repo
kbagent lineage     build | show | info | server
kbagent semantic-layer  model | show | validate | export | diff | import | promote | build | token
                    add | edit | remove   (metric/dataset/relationship/constraint/glossary; alias: sl)
kbagent branch      list | create | use | reset | delete | merge
                    metadata-list | metadata-get | metadata-set | metadata-delete
kbagent workspace   create | list | detail | delete | password | load | query | from-transformation | gc
kbagent sync        init | pull | status | diff | push | branch-link | branch-unlink | branch-status
kbagent schedule    list | detail | find
kbagent kai         ping | preflight | ask | chat | chat-detail | history
kbagent encrypt     values
kbagent permissions list | show | set | reset | check
kbagent agent       list | show | create | update | delete | run | runs | run-detail | run-events
                    test | cron-preview | prompt-improve
kbagent serve       [--host HOST] [--port PORT] [--ui]   # HTTP API + Web UI server
kbagent http        get | post | patch | delete          # calls a running `kbagent serve`
kbagent             init | context | doctor | version | update | changelog

# Global flags: --json, --verbose, --no-color, --config-dir
#               --deny-writes, --deny-destructive (session-only firewall)
#               --allow-env-manage-token (CI opt-in for KBC_MANAGE_API_TOKEN; default-deny since 0.29.0)
```

## Documentation

| Guide | What it covers |
|-------|---------------|
| [Tutorial](docs/TUTORIAL.md) | End-to-end walkthrough: register projects (1, N, whole org), global vs local config, plugin install, using the specialist subagent and `/keboola` slash command. |
| [Browser login](docs/auth.md) | `kbagent auth login` / `status` / `register-projects` / `logout`: the two credential models, what works on a session-registered project, error codes, and the accepted risks of serving one over HTTP. |
| [User Guide](docs/guide.md) | Configuration, permissions, per-directory isolation, workflows |
| [Python SDK](docs/sdk.md) | The in-process importable `Client`: method reference, typed result models, `py.typed`, idempotent jobs, gotchas, and how to extend the SDK. Demo: [`examples/storage_tui/`](examples/storage_tui/). |
| [Build a REST client](docs/build-your-own-client.md) | The `kbagent serve` HTTP API spec for non-Python callers (JS, Go, Slack bots, Web UIs). |
| [MCP migration](docs/mcp-migration.md) | Migrating off the removed MCP passthrough (v0.85.0): the tool-to-command map, what to do with persisted `mcp_tool` agent tasks, and how to keep `keboola-mcp-server` fresh yourself. |
| [Contributing](CONTRIBUTING.md) | Architecture, coding style, adding commands, testing checklist |

## Development

Read [CONTRIBUTING.md](CONTRIBUTING.md) first -- it covers the 3-layer architecture, coding conventions, security principles, and the full checklist for adding new commands.

```bash
git clone https://github.com/keboola/cli.git && cd cli
make install   # uv pip install -e ".[dev]"
make check     # lint + format + test
make hooks     # install pre-commit hook
```

## License

[Apache License 2.0](LICENSE)
