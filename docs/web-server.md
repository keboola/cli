# `kbagent serve` — HTTP server + agent host + Web UI

## TL;DR

`kbagent serve` is the kbagent **kernel exposed as an HTTP API**, plus a
local **agent host** that schedules background tasks (cron + kbagent CLI
runs + AI CLIs), plus a **React web UI** that drives both. Single Python
process, localhost-only, bearer-auth, scoped to one config directory.

```
┌─────────────────────────────────────────────────────────────┐
│ Browser ── React SPA (web/frontend, Vite :5173)             │
│   │                                                         │
│   │  REST + SSE  via /api/*                                 │
│   ▼                                                         │
│ Node BFF (web/backend, Fastify :8000)                       │
│   │  injects bearer token                                   │
│   ▼                                                         │
│ kbagent serve (Python FastAPI :8001)                        │
│   ├─ REST endpoints over every kbagent service              │
│   ├─ asyncio cron scheduler (agent_runner)                  │
│   └─ subprocesses: kbagent CLI / claude / codex / gemini    │
│       └─ they call back via `kbagent http` to *this* serve  │
│   │                                                         │
│   ▼                                                         │
│ Keboola APIs (Storage, Queue, Manage, AI, ...)              │
└─────────────────────────────────────────────────────────────┘
```

## Why this exists

Three stages, each adds capability over the previous one.

### Stage 1 — API for programmatic access (today)

The CLI is fine when a human types `kbagent project list`. It is awkward
when something *programmatic* — a different tool, a Streamlit app, a
notebook, a webhook — wants to ask "what configs are in project X?"
Forking a CLI per question is slow, swallows logs, and re-parses JSON
output.

`kbagent serve` solves that by exposing the same Python services as a
proper HTTP API with an OpenAPI schema. Anyone can build apps on top,
in any language. The CLI itself stays unchanged; the server is a
parallel surface.

### Stage 2 — Local agent host (today, in progress)

Once you have a long-running serve with a token, the obvious next step
is "let me schedule things to run inside it". Two flavours:

- `cli_command` — periodic `kbagent <cmd>` runs (the *cron-for-kbagent* use case).
- `ai_agent` — periodic prompts to a **local AI CLI** (`claude`, `codex`,
  `gemini`). The AI can use its own tools (file ops, web search, its own MCP
  servers)
  to satisfy the prompt, *and* it can call back into this serve via
  `kbagent http get /…` because the scheduler injects the serve URL +
  bearer token into the subprocess environment.

The result is a local control plane: agents that wake up on a cron,
do work using the user's own AI subscription, and write the result back
to a run history that the UI displays.

### Stage 3 — Replace the Keboola UI for everyday work (vision)

The endgame: the operator's daily Keboola interaction happens **here**,
not on `connection.keboola.com`. They open the local UI in the morning,
see overnight agent results, ask Kai a question, run a SQL workspace,
review job failures — without ever opening the official UI. Power users
keep the official UI for the things this UI doesn't cover; everyone
else lives here, with their own agents that know their projects.

## Capabilities

### REST API (`/api/*` after BFF, `/` directly on serve)

One router per kbagent service area, each mirroring that area's CLI
commands. **The endpoint list is not maintained here** — it lives in
[`web-server-endpoints.md`](web-server-endpoints.md), generated from the
running app's own OpenAPI spec and gated in CI by `make endpoints-check`,
so it cannot drift from the server. This section describes the shape of
the surface; that file is the inventory.

The routers group into the categories declared in
`server/app.py::OPENAPI_TAGS` (which is also what tabs the Swagger UI):

| Category | Routers |
|---|---|
| Project Management | `auth` `projects` `members` `org` `feature` `billing` `token` |
| Configurations | `configs` `components` `transformations` `encrypt` |
| Data | `storage` `stream` `search` `sharing` |
| Execution | `jobs` `flows` `schedules` `notifications` `data-apps` `workspaces` |
| Development | `branches` `lineage` `semantic-layer` |
| AI & Tools | `kai` `documentation` `ai-chat` `agents` |
| Read-only | `dev-portal` |
| System | `health` (plus `/version`, `/changelog`, `/doctor`, `/ui-config`) |

`ai-chat` is the one router with no CLI counterpart — it exists to stream
the web UI's chat. `agents` mirrors `kbagent agent *` (both sides read the
same `agents.json`); what is serve-only there is the cron loop, so a
scheduled task fires only while the server runs. `auth` mirrors only the
read/audit half of `kbagent auth` *(since vNEXT)* — `login` /
`login-password` / `logout` deliberately have no endpoint — and it is so far
the only router that enforces the permission policy; see "`/auth/*` — three
read/audit endpoints, three deliberate gaps" below.

Going the other way, several CLI surfaces are deliberately CLI-only: `sync`
(filesystem-local by design), `permissions`, and `init`. The mirrors still
considered missing are tracked in #657, and the fact that `permissions`
constrains only `/auth/*` and not the other ~30 routers is #655.

Auto-generated OpenAPI spec at `/openapi.json`, Swagger UI at `/docs`.

An upstream Keboola failure surfaces through one global handler: a
`NOT_FOUND` answers **404** (since 0.90.0 — it used to be 502, which told
callers to retry a request that can never succeed), an expired/missing browser
session answers **401**, and every other `KeboolaApiError` answers **502**. The
body is always the `{"status": "error", "error": {"code", "message"}}`
envelope, so the `error.code` — not the HTTP status alone — is what a client
should branch on. `GET /components/{id}` in particular no longer 404s for a
component the AI Service does not index: it falls back to the project's Storage
catalog and marks the response `documentation_source: "storage_catalog"`.

### Streaming endpoints (Server-Sent Events)

Six routes stream instead of returning a body. They are ordinary entries in
the generated reference, so this list is about *why* each one streams:

- `GET /jobs/{project}/{job_id}/stream` — live job status transitions + log
  tail. The only one using `sse_starlette`'s `EventSourceResponse`; the rest
  return a `StreamingResponse` with `media_type="text/event-stream"`.
- `POST /agents/{task_id}/run/stream` — a scheduled task run, streamed as it
  happens rather than polled.
- `POST /agents/test/stream` — the same for an ad-hoc (unsaved) action.
- `POST /agents/prompt/improve/stream` — AI prompt rewriting; streams because
  the underlying AI CLI does.
- `POST /ai/chat/stream` — local AI chat responses, token by token.
- `POST /workspaces/sql/improve/stream` — AI SQL helper over a workspace.

Note that `GET /agents/{task_id}/runs/{run_id}/events` looks like a stream but
is not: it replays a finished run's event timeline as one JSON response.

Never wired (no route exists): `/branches/{project}/reset/stream`,
`/lineage/build/stream`, `/kai/chat/stream` — all three remain plain
request/response despite being long-running.

### Agent scheduler

A single asyncio coroutine, started on FastAPI lifespan, that ticks
every minute and dispatches due tasks. State lives in the resolved
config directory:

- `<config_dir>/agents.json` — task definitions (mode 0600).
- `<config_dir>/agent_runs/<task_id>.jsonl` — append-only run history.

croniter parses the cron expression. Each task records `last_run_at`
and `next_run_at` so re-runs after restarts pick up where they left off.

### Web UI (`web/frontend`)

A NERD-themed React SPA that drives the API:

- **Command palette** — `Ctrl+K` / `Cmd+K` anywhere: fuzzy jump to any
  page, switch the active project, toggle the theme, open Swagger `/docs`,
  reopen **What's new**. Arrows + enter, esc closes.
- **What's new popup** *(since 0.90.0)* — a curated per-version highlights
  modal, shown once per version. See
  [What's-new popup](#whats-new-popup) below for the curated
  list's location, the storage key, and the `--no-banner` opt-out.
- **Dashboard** — greeting, big Kai chat input, stat tiles (projects /
  agents / doctor / recent jobs / PAYG credits), scheduled-agent
  activity, suggested next steps, recent jobs panel. The credits tile
  reads `GET /billing/credits` for the active project and shows a muted
  `n/a` on a non-PAYG project (`PAYG_NOT_AVAILABLE`).
- **Projects, Branches, Doctor, Changelog** — manage local config and
  health.
- **Tokens** — scoped Storage tokens for the selected project
  (`/token/{p}/list`): create / rotate / revoke, with the secret revealed
  ONCE in a copy-to-clipboard block, and an opt-in "derive last-used"
  toggle (`with_last_used=true`) that sorts dormant tokens first and
  renders `never` / `unknown` / `error` as distinct pills. A cross-project
  **All Tokens** view reads `GET /token/list` (repeatable `?project=`,
  same convention as `/jobs` and `/billing/credits`; omitted = every
  registered project) — every row carries `project_alias`, and
  `with_last_used=true` sorts dormant-first across every project's tokens
  together rather than grouped per project.
- **Configs, Components (AI search), Storage (with per-column data
  preview), Jobs (cards layout + SSE log stream), Search** — browse a
  selected project.
  - Configs: detail Drawer with **Run job** (`POST /jobs/{p}/run`) and
    **Delete** (soft-delete via `DELETE /configs/…`), plus a **Trash**
    tab (`GET /configs/trash/{p}`) with per-row Restore.
  - Storage: the table drawer's Info tab renders the raw `definition`
    (time / range partitioning, clustering, partition filter, partition
    count) when the stack reports one, and the Schema tab's Description
    cell is click-to-edit through `POST /storage/columns/{p}/{id}/describe`.
  - Jobs: per-row **re-run** and **terminate** (`POST /jobs/{p}/run` /
    `…/terminate`), the latter behind a confirm modal.
- **SQL Workspaces** — info Drawer with credentials + actions sidebar;
  Open SQL Editor opens a Monaco editor with a clickable Storage
  Explorer tree on the left.
- **Flows** — visual Mermaid builder of phase DAG + per-phase task list,
  plus a read-only **Notifications** tab (`GET /notifications`) listing
  who gets paged for the flow, with filter-less project-wide
  subscriptions shown in their own warning-pilled group.
- **Schedules** — cross-project cron list + find-by-window query.
- **Data Apps** — list, start/stop, secrets, validate-repo.
- **Lineage** — Sharing graph (live, cross-project) + Deep lineage (UI
  triggers `sync pull` + `lineage build`, then renders the JSON cache).
- **Kai Chat** — chat history scoped to the current project.
- **Agent Tasks** — cron-scheduled tasks (kbagent CLI / AI agent), with
  run-now, run history with AI-response/stdout panels, and an ad-hoc
  "Test now" button on the create form (runs through `/agents/test`,
  same code path as the scheduler, no persistence).
- **Org Setup, Members, Encrypt** — admin / write actions that need a
  Manage API token. The UI prompts for it per-action via a hidden modal,
  forwards as `X-Manage-Token` for that one request, never persists.

**Shareable deep links.** Every view is addressable, so a URL copied out
of the address bar reopens exactly what the sender was looking at. The
whole navigation state lives in the location hash: `#/<page>` for a page
with no project context, `#/p/<project>/<page>` once a project is
selected, `?branch=<id>` for a dev branch, and `?sel=<object>` for the
page's selected object — a job id on Jobs, `<component>/<config>` on
Configs, and so on. The hash rather than a path, because the SPA is
mounted at the **root** of the same FastAPI app that serves the REST API:
a history-mode `/projects` would collide with the endpoint that returns
JSON, while everything after `#` is never sent to the server at all.
`sel` is opaque to the router — the page that writes it defines its
shape, and it is dropped on any page / project / branch change, since an
object id from one context means nothing in the next. A link whose object
no longer resolves in the current project simply opens the page with no
drawer.

Detail drawers render an **Overview** tab (the payload's fields as
labelled sections, with pills for status and nested blobs kept verbatim)
and keep the untouched response one click away under **Raw JSON** with a
copy button, so nothing the API returned is ever hidden.

## Architecture

Three processes, three languages, one HTTP/JSON contract between each
pair. The boundary is intentional — you can swap any tier without
touching the others.

### Tier 1 — `kbagent serve` (Python, FastAPI)

`src/keboola_agent_cli/server/`:

```
app.py             create_app() factory, lifespan that starts the scheduler,
                   OPENAPI_TAGS, global exception handlers, UI mount
__init__.py        PEP 562 lazy re-export of create_app, so importing a
                   pure-logic sibling does not drag in FastAPI
auth.py            BearerAuthMiddleware (random token on startup)
dependencies.py    ServiceRegistry — singleton holding every kbagent service
agents_store.py    AgentTask / AgentRun + JSON file persistence
agent_runner.py    cron loop + per-action-type dispatch + subprocess env
run_broadcaster.py one live run per task, fanned out to every watching tab
                   (late attach replays what earlier viewers already saw)
pricing.py         per-model USD cost + token breakdown on each persisted run
sse.py             SSE helpers
routers/           one file per service area (jobs.py, storage.py, …)
```

Reuses *every* existing kbagent service unchanged — services already
return JSON-friendly dicts because the CLI's `--json` mode demanded it.

### Tier 2 — Node BFF (TypeScript, Fastify)

`web/backend/`:

```
src/server.ts      Fastify entry — listens on :8000
src/proxy.ts       /api/* → kbagent serve, attaches Bearer token, SSE pass-through
src/config.ts      reads KBAGENT_SERVE_URL + KBAGENT_SERVE_TOKEN from env
```

No business logic. The Bearer token never leaves this process; the
browser never sees it.

### Tier 3 — React UI (TypeScript, Vite)

`web/frontend/`:

```
src/api/client.ts          fetch wrapper + SSE subscriber
src/state.tsx              React Context for active project / branch / page
src/types.ts               TypeScript shapes mirroring server responses
src/layout/                Shell, Sidebar, TopBar (project + branch picker), StatusBar
src/components/            Drawer, DataTable, JsonView, Empty, ManageTokenModal
src/pages/                 one file per area (Dashboard, Storage, Jobs, …)
src/App.tsx                state-driven router (no react-router)
```

Tailwind for styling, TanStack Query for fetching, Monaco for SQL,
Mermaid for graphs.

## How to run

### Requirements

- Python 3.12+ with the optional `server` extra:
  ```bash
  uv pip install -e ".[server]"
  ```
- Node 20+ for the BFF and frontend:
  ```bash
  make web-install
  ```

### One command (recommended for development)

```bash
CONFIG_DIR=/path/to/.kbagent make web-dev
```

Spawns kbagent serve, the Node BFF, and Vite in a single foreground
process. Output is line-prefixed (`[serve]`, `[bff]`, `[vite]`); Ctrl+C
stops everything. Open <http://localhost:5173>.

### Three terminals (HMR per tier)

```bash
# Terminal 1 — kernel
uv run kbagent serve --port 8001 --config-dir /path/to/.kbagent
# copy the printed token

# Terminal 2 — BFF
cd web/backend
KBAGENT_SERVE_TOKEN=<token> PORT=8000 npm run dev

# Terminal 3 — frontend
cd web/frontend
npm run dev
```

### Production-ish (no Vite)

```bash
make web-build
uv run kbagent serve --port 8001 --config-dir ~/.config/keboola-agent-cli &
cd web/backend
STATIC_DIR=../frontend/dist KBAGENT_SERVE_TOKEN=<token> PORT=8000 npm start
```

The BFF then serves the React build statically and proxies `/api/*`.

## Key concepts

### Bearer token at startup

`kbagent serve` mints a random 32-byte URL-safe token on every start
(unless `KBAGENT_SERVE_TOKEN` is pre-exported), prints it once to
stdout, and refuses any request that does not present it as
`Authorization: Bearer <token>`. Public paths: `/health/ping`,
`/health/auth-info`, `/openapi.json`, `/docs`, `/redoc`.

### Session cookie in single-process UI mode

With `kbagent serve --ui`, the browser never sees the bearer token:
`GET /` (and `GET /index.html`) answers the SPA shell with a
`Set-Cookie: kbagent_session=<token>; HttpOnly; SameSite=Strict; Path=/`
session cookie, and the auth middleware accepts that cookie whenever no
`Authorization` header is present. Scripted callers keep using the header.

Two layers keep that cookie from going stale across server restarts
*(since 0.90.0)* — previously a restart (new token) could leave a tab that
reloaded from the browser cache silently 401-ing on every API call, with
each list rendering as empty:

- The shell is served with `Cache-Control: no-cache`, so a reload always
  revalidates against the server — and the bootstrap route always answers
  a full `200` with a fresh `Set-Cookie`.
- The SPA's API client treats a `401` as "cookie may be stale": it
  re-fetches `/` once with `cache: "reload"` (bypassing every cache
  layer), retries the request, and only if the retry still answers `401`
  shows a visible **Session expired** banner (for `SESSION_EXPIRED` /
  `SESSION_NOT_FOUND` the banner carries the server message, which names
  the on-host `kbagent auth login` remedy).

### What's-new popup

*(since 0.90.0)* The web UI shows a curated per-version highlights modal on
load, once per version, so features like the command palette get discovered
instead of waiting to be stumbled upon.

**Curated list — `web/frontend/src/whatsnew.ts`.** A hand-maintained
`WhatsNewRelease[]`, deliberately *not* the raw `changelog.py` output: the
changelog records everything, this reel records the handful of things a UI
user should look at. **Release PRs that ship user-visible UI features must
add an entry here** — same pass that resolves `vNEXT` placeholders. Adding
one is a single array element:

```ts
{ version: "0.90.0", items: [{ title: "…", body: "…", hint: "ctrl+k" }] }
```

A release with no entry of its own is **not** silent: the UI falls back to
the newest entry at or below the running version, so users still see the
most recent curated reel (each shown at most once). Exact matching would
make the feature ship dark — the popup first runs in the release *after*
the one whose highlights seeded the list. Silence happens only when no
entry is `<=` the running version.

**Mechanics.**

- Dismissal is persisted to `localStorage["kbagent.whatsnew.seen"]` as the
  release version string; the popup reappears only when the running version
  moves to another curated entry. A PEP 440 pre-release suffix is stripped
  before matching, so `0.90.0b1` sees the `0.90.0` reel.
- Esc, a backdrop click, and the "got it" button all dismiss and persist.
- The command palette's **What's new** action reopens it on demand,
  ignoring both the seen marker and `--no-banner`.

**Opt-out — `kbagent serve --no-banner`.** Suppresses the *unsolicited*
popup fleet-wide (an explicit palette request still works). The SPA reads
the switch from `GET /ui-config` -> `{"banner": bool}`, and fails **closed**
— while that request is in flight or if it fails, no popup.

It is an endpoint rather than something injected into `index.html`, for two
reasons. There is no injection point to extend: the one that existed
(`window.__KBAGENT_TOKEN`) was removed in favour of the session cookie, and
`tests/test_serve_ui.py` asserts it stays gone. And injection would only
cover `GET /` and `GET /index.html` — the SPA shell is *also* served by the
StaticFiles `html=True` fallback for any unmatched path, and that copy would
carry no config, silently re-enabling the popup an operator had suppressed.
For a suppression flag, failing open is the wrong direction.

### Session-registered projects

Projects registered through `kbagent auth login --register-projects` carry a
`kbc-session://<project_id>` sentinel instead of a Storage token; the live
credential is a browser-login session in `auth.json` on the host. `serve`
supports them for the Storage and Manage paths, because it never turns a
project into credentials itself — every service in the registry resolves its
own client factory, so the REST surface inherits the same bearer support the
CLI has (`server/dependencies.py`). Everything outside those paths fails fast
with `AUTH_NOT_SUPPORTED_ON_STACK` and names the static-token fallback, over
REST exactly as on the CLI. The authoritative list of those surfaces is
`SESSION_UNSUPPORTED_FEATURES` in `services/_auth_registration.py`; see
[Browser login](auth.md) for the same list in prose.

A session that expires while the server runs answers **HTTP 401** with
`error_code: SESSION_EXPIRED`. The server cannot recover on its own: a browser
login only completes where a human sits, so the remedy is `kbagent auth login`
**on the host running serve**, not anything the REST caller can do.

Two properties of this are consciously accepted, traded for being able to drive
a session-backed project from the web UI at all:

- **The serve token borrows a user identity.** A browser-login session is
  USER-scoped, so whoever holds `KBAGENT_SERVE_TOKEN` acts as the signed-in
  Keboola user for as long as that session lives. The serve token is not that
  user's Keboola identity and the REST surface has no second identity layer to
  distinguish them — treat the serve token as equivalent to the session it can
  reach, and keep the default localhost bind unless you have a reason not to.
- **Rotation was designed for short invocations.** Refresh-token rotation
  assumes a CLI process that exits in seconds. In a daemon up for weeks, the
  crash window between persisting a rotated session and revoking the previous
  one stays open far longer, and a crash inside it leaves a server-side session
  that no later `auth logout` can revoke. Such a session expires on its own
  schedule; it cannot be revoked from this CLI afterwards.

For a project you would rather not expose this way, register it with a static
Storage token (`kbagent project add --token`) — that path has neither property.

### `/auth/*` — three read/audit endpoints, three deliberate gaps *(since vNEXT)*

`kbagent auth` now has a `server/routers/auth.py` counterpart, but it mirrors
only the read/audit half of the CLI group:

| Endpoint | CLI equivalent | Permission op |
|---|---|---|
| `GET /auth/projects?stack=` | the interactive picker inside `auth register-projects` (no CLI leaf command of its own) | `auth.projects` (read) |
| `POST /auth/register-projects` | `auth register-projects --all` / `--project-id ID ...` | `auth.register-projects` (write) |
| `GET /auth/status?stack=` | `auth status` | `auth.status` (read) |

`POST /auth/register-projects` takes a body of `{stack?, all?, project_ids?,
aliases?}` (`all` is the wire alias for the service's `select_all`; `aliases`
maps a numeric project id to an alias override, coerced from the JSON body's
string keys) and returns the same `registered` / `exists` / `skipped`
per-project statuses the CLI prints — an existing alias is never overwritten.
None of the three response shapes (`ProjectCandidatesResult`,
`RegisterProjectsResult`, `AuthStatusResult`) ever carries a token value,
including the `kbc-session://` sentinel.

`/auth/*` is also the **first router to enforce the permission policy**: every
route above declares `Depends(require_permission(...))`, so a `--deny-writes`
session (or a persisted `permissions set --mode deny` policy) blocks `POST
/auth/register-projects` over REST exactly as it blocks the CLI command. A
denial answers **HTTP 403** with `error_code: PERMISSION_DENIED` — same code
the CLI exits on. The other ~30 routers do not check the engine yet; see the
gotchas entry on this before assuming a deny policy firewalls the whole REST
surface.

A missing or expired session reaches `GET /auth/projects` and `POST
/auth/register-projects` as a **thrown error**, both funnelled through
`AuthService._introspect_accessible_projects`: no stored session raises
`SESSION_NOT_FOUND`, a stored session whose refresh fails raises
`SESSION_EXPIRED` (via `provider.introspect()`) — both answer **HTTP 401**,
same as every other session-project failure documented above. `GET
/auth/status` is the deliberate exception: it is the probe you call *to find
out* whether a session is dead, so it must not itself fail that way.
`AuthService.status()` catches both cases and always answers **HTTP 200**,
reporting session health in the response body's `status` field instead —
`"missing"` (no stored session), `"expired"` (refresh failed),
`"degraded"` (the auth service was unreachable; locally stored data is shown),
`"refreshed"` (introspection rotated the access token), or `"live"`. A client
that wants to detect a dead session by branching on the HTTP status code must
call `/auth/projects` or `/auth/register-projects`, not `/auth/status` — the
latter always answers 200 and the caller must read `status` from the body.

Registering a project through `POST /auth/register-projects` writes the same
`kbc-session://<project_id>` sentinel `auth login --register-projects` would —
so whoever holds `KBAGENT_SERVE_TOKEN` can grow the set of session-backed
projects this server exposes, still acting as the signed-in user for all of
them, per "Session-registered projects" above.

`login` / `login-password` / `logout` deliberately have **no** endpoint:

- `auth login` opens a browser (or prints a device-flow code) on the host and
  only completes there — a REST caller has no way to sit in that loop.
- `auth login-password` takes a plaintext password (and, for MFA accounts, a
  TOTP seed) meant to flow from a CI secrets store into one `kbagent` CLI
  invocation, never as a REST request body sitting behind this server's own
  bearer token.
- `auth logout` revokes the live session backing every session-registered
  project reachable through this very server. Destroying that session is a
  deliberate host-operator action taken at the CLI, not something a REST
  client holding the serve bearer token should be able to trigger remotely.

Sign in via the CLI directly (`kbagent auth login-password`, or `auth login`
for a human), then use `POST /auth/register-projects` — or `auth
register-projects` on the CLI — to register the resulting session's projects
for `serve` to use.

### Manage tokens are per-request

Operations that need a Keboola Manage API token (`org setup`,
`project invite`, `member-set-role`) read it from an `X-Manage-Token`
header, use it for that single request, and discard it. The token is
never logged, never stored, and the env-var fallback that the CLI has
(`--allow-env-manage-token`) is not exposed by the server.

### Agents call back via `kbagent http`

When the scheduler spawns an AI agent (`claude -p …`), it overlays
three env vars onto the child process:

- `KBAGENT_CONFIG_DIR` — same config the serve uses, so any `kbagent
  <cmd>` the AI runs sees the same projects + tokens + active branches.
- `KBAGENT_SERVE_URL` — `http://127.0.0.1:8001`.
- `KBAGENT_SERVE_TOKEN` — the bearer token.

Plus a short instruction prefix is prepended to the user's prompt
telling the AI: "you can call this serve via `kbagent http get /…` —
that is the preferred path because it shares state with this very
process, instead of forking a CLI tree against possibly stale config."

This is what lets a midnight agent task do meaningful work: it has
the same view of Keboola the operator does, it can call any endpoint in
the reference, and its full response (including any tools it called)
is captured into the run history.

### State on disk

Everything the server persists lives under one config directory
(resolved via `--config-dir`, `KBAGENT_CONFIG_DIR`, or the standard
walk-up rules from `config_store.py`):

```
<config_dir>/
  config.json                projects + tokens + permissions (mode 0600)
  agents.json                scheduled tasks (mode 0600)
  agent_runs/
    <task_id>.jsonl          append-only run history
```

Nothing else. No database, no cache that survives restart, no shared
state with other serve instances. Multiple serves on different ports
(or different config dirs) are independent — no leader election, no
coordination. That is intentional for now: the singleton model fits
the personal-control-plane vision; multi-tenant comes later if at all.

## Where to look next

- `src/keboola_agent_cli/commands/serve.py` — CLI entry point, argv parsing.
- [`web-server-endpoints.md`](web-server-endpoints.md) — every route, generated.
- `src/keboola_agent_cli/server/app.py` — `create_app()` + lifespan +
  `OPENAPI_TAGS` (the router categories above).
- `scripts/gen_endpoint_reference.py` — generates the endpoint reference;
  `make endpoints-gen` after adding a route, or CI fails.
- `src/keboola_agent_cli/server/agent_runner.py` — scheduler + dispatch.
- `src/keboola_agent_cli/server/routers/agents.py` — agent-tasks API.
- `src/keboola_agent_cli/commands/http_client.py` — `kbagent http` subcommand
  used by AI subprocesses to call the live serve.
- `web/README.md` — frontend-specific quickstart.
- `tests/test_server_smoke.py` — minimal end-to-end check that the
  app builds and routes resolve.
