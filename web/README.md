# kbagent web UI

Two ways to run the UI:

- **End-user / single-process** -- one Python command, no Node runtime needed:
  `kbagent serve --ui`. FastAPI mounts the built React SPA on `/`, injects
  the bearer token into `index.html`, and rewrites `/api/*` to bare API
  routes. Use this for "just give me the UI" deployments.
- **Dev mode (3 terminals)** -- Vite dev server + Node BFF + kbagent serve.
  Used when iterating on the React app: hot reload, source maps, the works.
  See [Dev mode](#dev-mode-three-terminals) below.

## Single-process mode (recommended for users)

```bash
# one-time
uv pip install -e ".[server]"
(cd web/frontend && npm install && npm run build)   # produces web/frontend/dist

# every time
uv run kbagent serve --ui --port 8001 --config-dir ~/.config/keboola-agent-cli
# Open the URL printed at startup -- the browser is auto-authenticated.
```

What `--ui` does on top of `kbagent serve`:

1. Mounts `web/frontend/dist` at `/` so the SPA loads from the same port
   as the API.
2. Server-renders `index.html` with `<script>window.__KBAGENT_TOKEN = "..."</script>`
   so the SPA boots already authenticated. No paste step, no Node BFF.
3. Adds a path-rewrite middleware so the SPA's existing `/api/*` calls
   reach the bare endpoints (`/projects`, `/configs`, ...).
4. Accepts `?_kbagent_token=...` as a fallback because `EventSource`
   (the SSE primitive used on the Jobs page) cannot send custom headers.

`--ui-dist PATH` overrides the dist location; `$KBAGENT_UI_DIST` works
the same way. Both imply `--ui`.

The bearer token is still printed at startup so curl / scripts /
`kbagent http get` keep working alongside the browser session.

## Architecture

```
  React SPA  (web/frontend, Vite + TanStack Query + Tailwind + Monaco + Mermaid)
      |
      |  Single-process mode: SPA + API on the same FastAPI port,
      |                        token injected into index.html.
      |
      |  Dev mode: SPA via Vite dev server, /api/* proxied to ↓
      v
  Node BFF   (web/backend, Fastify + undici)         (DEV / OPTIONAL)
      |  REST + SSE injects bearer token, no business logic
      v
  kbagent serve  (Python, FastAPI + uvicorn)
      |
      v
  Keboola APIs (Storage, Queue, Manage, AI, MCP)
```

The BFF is now optional -- it exists to enable the Vite dev workflow
(hot reload across all three layers) and to host the SPA from a
different process when that's preferred. **Production deployments can
run a single uvicorn process via `--ui`.**

**Building your own client** instead of the bundled SPA? Run plain
`kbagent serve` (no `--ui`). It exposes the same REST + SSE endpoints
and an OpenAPI schema at `/openapi.json`; authenticate with the bearer
token printed at startup or via `$KBAGENT_SERVE_TOKEN`. The
`kbagent http get|post|patch|delete` CLI is a thin wrapper for shell
use.

## Dev mode (three terminals)

Use this when editing React / TypeScript and you want hot reload.

Prerequisite: `uv pip install -e ".[server]"`, Node 20+, and
`(cd web/backend && npm install)` / `(cd web/frontend && npm install)`.

### Terminal 1 -- the kbagent kernel

```bash
uv run kbagent serve --port 8001 --config-dir /tmp/kbagent/.kbagent
# Prints a bearer token on startup -- copy it.
```

### Terminal 2 -- the Node BFF

```bash
cd web/backend
KBAGENT_SERVE_TOKEN=<token-from-terminal-1> PORT=8000 npm run dev
```

### Terminal 3 -- the React dev server

```bash
cd web/frontend
npm run dev
# Open http://localhost:5173/
```

The Vite dev server proxies `/api/*` and `/__bff/*` to the BFF; the BFF
attaches the bearer token and forwards to `kbagent serve`. Hot reload
works for all three layers.

## Production-only BFF mode (legacy)

If you'd rather keep the BFF in production (e.g. you've got auth /
logging middleware in Fastify you want to preserve), the original flow
still works:

```bash
make web-build        # builds the React app into web/frontend/dist
uv run kbagent serve --port 8001 --config-dir ~/.config/keboola-agent-cli &
cd web/backend
STATIC_DIR=../frontend/dist \
KBAGENT_SERVE_TOKEN=<token> \
PORT=8000 npm start
# Open http://localhost:8000/
```

## Architecture notes

- **No business logic in the BFF**: it injects the bearer token and
  proxies (REST + SSE) verbatim. If you want a different UI language,
  swap the BFF -- the React app talks to it through `/api/*`, the BFF
  talks to kbagent serve through OpenAPI.
- **Manage tokens are per-request**: writing operations that need
  `KBC_MANAGE_API_TOKEN` (org setup, member invites/removes) prompt
  for the token in a modal and pass it as `X-Manage-Token` for that
  one request. Never persisted, never logged.
- **SSE streams**: jobs page subscribes to
  `/api/jobs/{project}/{id}/stream` for live status + log events.
  The BFF passes the chunks through; the React `EventSource` API
  receives them directly.
- **SQL workspaces**: Monaco editor + `/api/workspaces/.../query`
  with CSV-rendering of statement results.
- **MCP tools**: each tool's `inputSchema` is rendered as a generic
  JSON input on the MCP page so you can call any tool the server
  exposes without UI changes.
- **Lineage**: cross-project bucket-sharing graph rendered as a
  Mermaid diagram + tabular edge list.

## Repo layout

```
web/
  backend/                 Node 20 + Fastify (TypeScript)
    src/
      server.ts            entrypoint
      proxy.ts             REST + SSE proxy with bearer auth injection
      config.ts            env-driven config
    package.json
    tsconfig.json
  frontend/                React 18 + Vite + Tailwind + TanStack Query
    src/
      api/client.ts        thin fetch wrapper + SSE helper
      state.tsx            global UI state (selected project, branch, page)
      types.ts             permissive mirror of kbagent service shapes
      layout/               Sidebar, TopBar, StatusBar, Shell
      components/           Empty, Table, JsonView, ManageTokenModal
      pages/                one file per feature area (20 pages)
      App.tsx              page router (state-driven)
      main.tsx             React entry
      index.css             Tailwind base + NERD theme components
    index.html
    vite.config.ts
    tailwind.config.ts
    package.json
    tsconfig.json
```
