# Building your own client against `kbagent serve`

The bundled React SPA (`kbagent serve --ui`) is one of many possible clients
for the kbagent HTTP API. This guide is the **complete spec** for anyone --
human or AI agent -- writing a different client: a Streamlit dashboard, a
Next.js app, a Python automation script, a Slack bot, an OpenAPI-generated
TypeScript SDK, anything that speaks HTTP.

The intent: **a sufficiently capable AI agent (Claude, Codex, Gemini) should
be able to read this single document plus `/openapi.json` and synthesise a
working client without asking further questions.**

If you find an answer you needed and the doc did not give you, [open an
issue](https://github.com/keboola/cli/issues) — drift on this
file blocks the use case it exists for.

---

## TL;DR

```bash
# 1. Start the server
kbagent serve --port 8001 --config-dir ~/.config/keboola-agent-cli &
#    Prints a bearer token on stdout. Copy it, or set KBAGENT_SERVE_TOKEN
#    in the environment beforehand to pin a value.

# 2. Discover what is callable
curl -s -H "Authorization: Bearer $KBAGENT_SERVE_TOKEN" \
  http://127.0.0.1:8001/openapi.json | jq '.paths | keys | .[]' | head

# 3. Call any endpoint
curl -s -H "Authorization: Bearer $KBAGENT_SERVE_TOKEN" \
  http://127.0.0.1:8001/projects | jq
```

Three things every client must implement:

1. **Bearer-auth** every request (`Authorization: Bearer <token>`) — except a
   handful of explicit public paths (see [Auth flow](#auth-flow) below).
2. **Parse the kbagent error envelope** on every non-2xx response — see
   [Error envelope](#error-envelope) for the shape and the full code list.
3. **Use SSE for any streaming endpoint** (job log tail, agent run timeline).
   Do not poll the regular detail endpoints in a tight loop — see
   [Streaming (SSE)](#streaming-sse).

That is it. Everything below is depth.

---

## What `kbagent serve` is

A single FastAPI process exposing **every kbagent capability** as a REST
endpoint, plus an async cron scheduler ("agent host") running locally. State
lives in one config directory (`config.json`, `agents.json`, run history
JSONL) at `0600` permissions. No external DB. No leader election. Localhost-
only by default (`--host 127.0.0.1`).

The same Python services that back the `kbagent` CLI back the server —
identical return shapes, identical error codes. If `kbagent --json
project list` returns a shape, `GET /projects` returns the same shape.

Three audiences:

- **Browser** — the bundled React SPA via `kbagent serve --ui` (cookie auth).
- **Scheduled subprocess** — an AI CLI (claude / codex / gemini) the
  scheduler spawned. Calls back via `kbagent http get/post/...` which
  forwards to this serve (bearer-header auth from
  `KBAGENT_SERVE_TOKEN` env).
- **External client** — that's you. Pick any language, any framework. The
  contract below is your only constraint.

---

## Endpoint discovery

Three ways, all equivalent for routing:

| URL | What it gives you |
|---|---|
| `GET /openapi.json` | The full OpenAPI 3.x schema. Feed it to `openapi-typescript`, `openapi-python-client`, or read it directly. |
| `GET /docs` | Swagger UI -- click endpoints, see schemas, try them with a paste-in bearer token. |
| `GET /redoc` | ReDoc UI -- same data, easier to read top-to-bottom as a reference. |

All three are **public** (no auth) so a client can discover the server
without first knowing the token. Everything else needs auth.

### Top-level resource map

```
/projects        kbagent project (add, list, info, use, current, refresh, ...)
/members         kbagent project member-* / invitation-*
/configs         kbagent config (list, detail, search, update, ...)
/components      kbagent component (list, detail, scaffold)
/storage         kbagent storage (buckets, tables, files, swap-tables, ...)
/jobs            kbagent job (list, detail, run, terminate) + SSE stream
/branches        kbagent branch (list, create, use, reset, delete, merge)
/workspaces      kbagent workspace (CRUD, password, load, query)
/flows           kbagent flow (CRUD, schema, schedule)
/schedules       kbagent schedule (list, detail, find)
/lineage         kbagent lineage (build, show, info)
/sharing         kbagent sharing (list, share, link, unshare, unlink, edges)
/data-apps       kbagent data-app (CRUD, deploy, start/stop, secrets-*, validate-repo)
/kai             kbagent kai (ping, ask, chat, history, preflight, chat-detail)
/encrypt         kbagent encrypt values
/search          kbagent search QUERY
/org             kbagent org setup
/agents          scheduled agent tasks (no CLI counterpart -- new in 0.40.0)
/health          /health/ping, /health/auth-info, /health/doctor
/docs /redoc     Swagger UI / ReDoc
/openapi.json    full OpenAPI 3.x schema
```

Every prefix maps to one file under
[`src/keboola_agent_cli/server/routers/`](../src/keboola_agent_cli/server/routers/)
and (for everything except `/agents` and `/health`) one service under
[`src/keboola_agent_cli/services/`](../src/keboola_agent_cli/services/). When
in doubt about a parameter, read the service: it is the authoritative
behavioural spec.

---

## Auth flow

`kbagent serve` mints one random 32-byte URL-safe bearer token on every
start. The token is printed to stdout once; clients pick it up via one of
three transport mechanisms.

```
                                ┌─────────────────────────────┐
                                │  kbagent serve              │
                                │  random bearer printed once │
                                └────────────┬────────────────┘
                                             │
        ┌────────────────────────────────────┼───────────────────────────┐
        │                                    │                           │
   header path                          cookie path                 BFF / proxy
   ────────────                         ────────────                ───────────
 Authorization: Bearer <T>            kbagent_session=<T>         Authorization header
 set by: client code                  set by: GET / on --ui       set by: BFF middleware
 used by: curl / scripts /            used by: browser SPA in     used by: Node
           AI subprocess /            single-process UI mode;     web/backend in
           your dashboard             EventSource SSE             3-process dev mode
```

### 1. Bearer header (the canonical path)

Every request adds `Authorization: Bearer <token>`. Token comparison uses
`hmac.compare_digest` (timing-attack resistant). No cookies, no query
params, no quirks.

```bash
export KBAGENT_SERVE_TOKEN=<token-from-stdout>
curl -H "Authorization: Bearer $KBAGENT_SERVE_TOKEN" http://127.0.0.1:8001/projects
```

This is what you should default to in any non-browser client (Python
scripts, Slack bots, CI jobs, AI agents calling back into the serve).

### 2. HttpOnly cookie (browser-only, `--ui` mode)

When the server is started with `--ui`, `GET /` (and `GET /index.html`) is
**unauthenticated** and serves the SPA shell HTML with a `Set-Cookie:
kbagent_session=<token>; HttpOnly; SameSite=Strict; Path=/` response
header. The browser attaches that cookie to every same-origin subsequent
request automatically. The auth middleware accepts it as a Bearer-fallback
when no `Authorization` header is present.

Security properties of the cookie path (you do **not** get any of these
with the header path, hence the split):

- `HttpOnly`: JS cannot read the cookie → no XSS exfiltration.
- `SameSite=Strict`: no cross-origin sends → no CSRF.
- No `Secure` flag: kbagent serve defaults to plain http on 127.0.0.1; if
  you front it with TLS, set `Secure` at the proxy.
- No `max-age` → session cookie, cleared when the browser closes.

This path is **not** for non-browser clients — `curl --cookie ...` works
mechanically, but you have no good way to get the cookie set in the first
place if you also need to consume the bootstrap HTML.

### 3. The `X-Manage-Token` header (per-request, never persisted)

Operations that touch the **Manage API** (organisation setup, project
member invites/removes, role changes) need a Keboola Manage API token in
addition to the Bearer token. They are pulled from `X-Manage-Token` per
request — never persisted server-side, never logged.

```bash
curl -H "Authorization: Bearer $KBAGENT_SERVE_TOKEN" \
     -H "X-Manage-Token: $KBC_MANAGE_API_TOKEN" \
     -X POST \
     -H "Content-Type: application/json" \
     -d '{"org_id": 123, "url": "https://connection.keboola.com"}' \
     http://127.0.0.1:8001/org/setup
```

The SPA prompts for it in a modal per write action, forwards it as a single
request header, and discards it. The CLI's `--allow-env-manage-token` flag
is **not** plumbed through to the server — server clients have to pass the
header explicitly.

### Public paths (no auth required)

```
/health/ping        readiness probe -- always returns {"status": "ok"}
/health/auth-info   public discovery: auth mode, version
/openapi.json       full schema
/docs /redoc        Swagger / ReDoc UIs
GET / + /index.html /assets/* (only in --ui mode -- the SPA shell)
```

Anything else requires auth.

---

## Error envelope

Every non-2xx response carries this exact JSON body:

```json
{
  "status": "error",
  "error": {
    "code": "STABLE_MACHINE_READABLE_CODE",
    "message": "Human-readable description."
  }
}
```

Programmatic callers should **always** parse `error.code` (stable, listed
below) and surface `error.message` to humans. Do not pattern-match the
message text — it is informational and may be improved between releases.

Successful 2xx responses follow the matching envelope:

```json
{
  "status": "ok",
  "data": {
    "...": "endpoint-specific payload"
  }
}
```

Some endpoints (notably `/jobs/.../stream`) bypass the envelope and stream
raw SSE; see [Streaming (SSE)](#streaming-sse).

### Error codes

The full enum is defined in
[`src/keboola_agent_cli/errors.py`](../src/keboola_agent_cli/errors.py)
(class `ErrorCode`). HTTP status code mapping is below; codes are stable
across versions (adding a new one is a minor bump, renaming/removing is a
major bump).

| HTTP status | Common codes you should expect | Cause |
|---|---|---|
| 400 | `CONFIG_ERROR`, `VALIDATION_ERROR`, `INVALID_ARGUMENT`, `MISSING_PARAMETER`, `HTTP_ERROR` | Bad client request, missing field, bad path. |
| 401 | `UNAUTHORIZED`, `INVALID_TOKEN`, `MISSING_MASTER_TOKEN` | Bearer header / cookie missing or wrong; Manage operation without `X-Manage-Token`. |
| 403 | `ACCESS_DENIED`, `PERMISSION_DENIED` | Token valid but operation forbidden by policy or upstream Keboola ACL. |
| 404 | `NOT_FOUND`, `HTTP_ERROR` | Resource not found, route not found. |
| 422 | `VALIDATION_ERROR` | Pydantic body validation failed. |
| 502 | `API_ERROR`, `KAI_ERROR`, `QUEUE_JOB_FAILED`, `STORAGE_JOB_FAILED` | Upstream Keboola API returned an error. |
| 503 | `KAI_NOT_ENABLED`, `RETRY_EXHAUSTED`, `CONNECTION_ERROR` | Upstream temporarily unavailable. |
| 500 | `INTERNAL_ERROR`, `UNKNOWN_ERROR` | Bug in the server. File an issue. |

Domain-specific codes (`DATA_APP_*`, `KAI_*`, `JOB_TIMEOUT_TERMINATED`,
`INVALID_FLOW_DEFINITION`, …) appear alongside the generic ones when the failure
is specific to a feature area. Treat them as informational refinement —
the HTTP status is the contract.

---

## REST patterns

The whole API follows three patterns: read (idempotent GET), write
(POST / PATCH / DELETE), and long-running write (poll or stream).

### Read

```bash
# Single resource
curl -H "Authorization: Bearer $T" http://127.0.0.1:8001/projects/prod

# Filter / list
curl -H "Authorization: Bearer $T" \
  'http://127.0.0.1:8001/configs?project=prod&component_type=writer'

# Cross-project (when the endpoint supports it)
curl -H "Authorization: Bearer $T" \
  'http://127.0.0.1:8001/storage/tables?project=prod&project=staging'
```

Query parameters mirror CLI flags one-for-one. Repeated `project=` matches
the CLI's repeatable `--project`.

### Write (synchronous)

```bash
curl -H "Authorization: Bearer $T" \
     -H "Content-Type: application/json" \
     -X POST \
     -d '{"project": "prod", "stage": "in", "name": "raw_orders"}' \
     http://127.0.0.1:8001/storage/buckets
```

PATCH for partial updates, DELETE for removals. Inputs are Pydantic-
validated against the OpenAPI schema — read the schema, do not guess shape.

### Long-running write

Two patterns coexist. Use the SSE one when the endpoint exposes a
`/stream` suffix; otherwise poll.

**Polling pattern** (e.g. `POST /jobs/{project}/run` + `GET /jobs/{project}/{job_id}`):

```python
import time
import httpx

with httpx.Client(
    base_url="http://127.0.0.1:8001",
    headers={"Authorization": f"Bearer {token}"},
) as client:
    started = client.post("/jobs/prod/run", json={"component_id": ..., "config_id": ...})
    started.raise_for_status()
    job_id = started.json()["data"]["job_id"]

    while True:
        detail = client.get(f"/jobs/prod/{job_id}").json()["data"]
        if detail["isFinished"]:
            break
        time.sleep(detail.get("nextPollSeconds", 2))
    print(detail["status"], detail.get("result"))
```

**Streaming pattern**: see the next section.

---

## Streaming (SSE)

SSE endpoints stream live events instead of forcing the client into a poll
loop. Today the active one is `GET /jobs/{project}/{job_id}/stream`; the
agent-run live tail uses the same pattern.

### Event shape

Each event is a JSON object emitted as a single SSE message:

```
event: status
data: {"status": "processing", "job": {...}}

event: log
data: {"line": "INFO  ..."}

event: done
data: {"final": "success", "job": {...}}
```

Event names: `status`, `log`, `error`, `done`. The handler should dispatch
on `event:` and JSON-parse `data:` per message. Use the bundled SPA's SSE
client as a reference — it is a 60-line vanilla parser:
[`web/frontend/src/api/client.ts` `ssePost()` and `sseSubscribe()`](../web/frontend/src/api/client.ts).

### Python consumer

```python
import json
import httpx

with httpx.Client(
    base_url="http://127.0.0.1:8001",
    headers={
        "Authorization": f"Bearer {token}",
        "Accept": "text/event-stream",
    },
    timeout=None,
) as client, client.stream(
    "GET",
    f"/jobs/prod/{job_id}/stream",
) as response:
    event_name = "message"
    data_lines: list[str] = []
    for line in response.iter_lines():
        if line.startswith(":"):
            continue  # SSE comment / heartbeat
        if line == "":
            # blank line: dispatch the accumulated event
            if data_lines:
                payload = json.loads("\n".join(data_lines))
                print(event_name, payload)
            event_name = "message"
            data_lines = []
            continue
        field, _, value = line.partition(":")
        value = value.lstrip(" ")
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
```

### Browser consumer

`EventSource` cannot send custom headers — that is why the cookie path
exists. In `--ui` mode:

```ts
const es = new EventSource("/api/jobs/prod/12345/stream", { withCredentials: true });
es.addEventListener("status", (msg) => console.log("status", JSON.parse(msg.data)));
es.addEventListener("log", (msg) => console.log("log", JSON.parse(msg.data)));
es.addEventListener("done", () => es.close());
```

In API-only mode (no `--ui`), there is no cookie surface, so the browser
needs a backend proxy that injects the Authorization header (the Node BFF
under `web/backend/` is one such proxy; you can replace it with any
language).

---

## CORS

Default allowed origins (when `--cors-origin` is not passed):

```
http://localhost:5173    # Vite dev
http://localhost:8000    # legacy Node BFF
http://127.0.0.1:5173
http://127.0.0.1:8000
```

Pass `--cors-origin https://my-app.example` (repeatable) to allow more.
Credentials are allowed (cookie path), all methods, all headers.

---

## Client examples

### Single-file Python script (httpx)

```python
"""Smallest possible kbagent serve client — list projects."""
import os

import httpx

BASE = os.environ.get("KBAGENT_SERVE_URL", "http://127.0.0.1:8001").rstrip("/")
TOKEN = os.environ["KBAGENT_SERVE_TOKEN"]

with httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
    response = client.get("/projects")
    response.raise_for_status()
    for project in response.json()["data"]:
        print(project["alias"], "—", project["url"])
```

Make it richer with one more page (configs of one project, jobs in one
component) and you have a Slack-bot or a daily report.

### TypeScript fetch wrapper

The bundled SPA's wrapper at
[`web/frontend/src/api/client.ts`](../web/frontend/src/api/client.ts)
(227 lines, fully commented) is your reference. It handles:

- REST GET / POST / PATCH / DELETE with the kbagent error envelope.
- Same-origin cookie credentials (no-op in API-only mode).
- SSE GET (`EventSource`) and SSE POST (hand-rolled `fetch` + parser).

Copy it verbatim into any Vite / Next.js / Remix project; it depends only
on the standard `fetch` and `EventSource` browser APIs.

### Streamlit dashboard skeleton

```python
"""Minimal Streamlit page showing kbagent projects + jobs."""
import os

import httpx
import streamlit as st

st.set_page_config(page_title="Keboola overview", layout="wide")

BASE = os.environ.get("KBAGENT_SERVE_URL", "http://127.0.0.1:8001").rstrip("/")
TOKEN = os.environ["KBAGENT_SERVE_TOKEN"]

client = httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {TOKEN}"})


@st.cache_data(ttl=60)
def projects() -> list[dict]:
    return client.get("/projects").json()["data"]


@st.cache_data(ttl=30)
def recent_jobs(alias: str) -> list[dict]:
    return client.get(f"/jobs?project={alias}&limit=10").json()["data"]


alias = st.sidebar.selectbox("Project", [p["alias"] for p in projects()])

st.header(f"Recent jobs — {alias}")
st.dataframe(recent_jobs(alias))
```

`streamlit run app.py` and you have the bones of a custom Keboola
dashboard. Add deep lineage, SQL queries, scheduled agents — every endpoint
that powers the bundled SPA is available the same way.

### OpenAPI-generated SDK

```bash
# TypeScript
npx openapi-typescript http://127.0.0.1:8001/openapi.json -o kbagent.types.ts

# Python
uvx openapi-python-client generate --url http://127.0.0.1:8001/openapi.json
```

The schemas are derived from Pydantic models, so the generated SDKs are
type-precise. The Bearer-auth scheme is in the OpenAPI spec
(`securitySchemes`) so most generators wire it automatically.

---

## "Spawn an AI agent to build me a client"

If you are delegating UI generation to an AI agent (the use case this doc
exists for), give the agent this exact prompt skeleton:

> Write a [Streamlit dashboard | Next.js page | …] that uses the
> `kbagent serve` HTTP API. Read
> [`docs/build-your-own-client.md`](./build-your-own-client.md) and
> `http://127.0.0.1:8001/openapi.json` first. Auth via
> `Authorization: Bearer ${KBAGENT_SERVE_TOKEN}`; the token is in env.
> Do not write a polling loop where an SSE stream is available
> (`/jobs/{p}/{id}/stream`). Parse the `{"status":"error","error":{"code","message"}}`
> envelope on any non-2xx response.
>
> Goal: …

Anything more specific is application logic — leave it to the agent.

The agent will:

1. `curl http://127.0.0.1:8001/openapi.json` to enumerate endpoints.
2. Match the user's goal against the endpoint map in this doc.
3. Generate a thin client (often verbatim from
   [`web/frontend/src/api/client.ts`](../web/frontend/src/api/client.ts)
   or the Python example above).
4. Build the feature using `fetch` / `httpx` calls against typed routes.

If the agent invents endpoints or skips auth, your prompt was missing this
file — point them at it and rerun.

---

## Production / hardening notes

- **Localhost-only by default.** `--host 0.0.0.0` works for trusted
  networks; never expose to the open internet without TLS termination and
  rate limiting in front.
- **Single random token per process.** Rotating it means restarting the
  process — pre-export `KBAGENT_SERVE_TOKEN` to pin a value across
  restarts (e.g. for systemd / k8s).
- **No multi-tenancy.** Two `kbagent serve` instances on different ports
  with different `--config-dir` are independent. Singletons by design.
- **No persistence beyond the config dir.** No DB, no Redis. Run-history
  JSONL files at `<config_dir>/agent_runs/`; restarting the process
  reloads them from disk. Backups = file backups.
- **Audit trail.** Every Storage write goes through Keboola's own audit
  (the upstream Storage API); `kbagent serve` adds nothing on top. If you
  need request-level audit, log it in your client (or front the server
  with a reverse proxy that logs).

---

## Where to look next

- [`docs/web-server.md`](web-server.md) — architectural overview of
  `kbagent serve` and the scheduler loop.
- [`docs/web-server-endpoints.md`](web-server-endpoints.md) — every route,
  generated from the app's own OpenAPI spec, so it never lags the server.
- [`web/README.md`](../web/README.md) — running the bundled SPA, both
  single-process and dev-mode flows.
- [`src/keboola_agent_cli/server/routers/`](../src/keboola_agent_cli/server/routers/)
  — one router file per resource area; the canonical behavioural spec
  beneath the OpenAPI schema.
- [`web/frontend/src/api/client.ts`](../web/frontend/src/api/client.ts) —
  227-line TypeScript reference client. Copy-paste into your own project.
- [`src/keboola_agent_cli/errors.py`](../src/keboola_agent_cli/errors.py)
  — full `ErrorCode` enum.
