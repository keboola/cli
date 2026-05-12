# kbagent web UI

Three-tier "NERD UI" over the kbagent kernel.

```
  React SPA  (web/frontend, Vite + TanStack Query + Tailwind + Monaco + Mermaid)
      |  REST + SSE  via /api/*  (Vite dev proxy or BFF static-served)
      v
  Node BFF   (web/backend, Fastify + undici)
      |  REST + SSE  injects bearer token, no business logic
      v
  kbagent serve  (Python, FastAPI + uvicorn -- NEW CLI command in 0.34+)
      |
      v
  Keboola APIs (Storage, Queue, Manage, AI, MCP)
```

The three layers are intentionally three different languages, so the
language boundaries enforce clean separation: the only contract between
them is HTTP/JSON. **Everything the CLI can do, the UI can do** -- both
sides talk to the same Python services through different transports.

## Quick start (dev mode, three terminals)

Prerequisite: install kbagent with the optional `server` extra so FastAPI/
uvicorn/sse-starlette are available, and Node 20+ for the BFF/frontend.

```bash
# one-time
uv pip install -e ".[server]"
(cd web/backend && npm install)
(cd web/frontend && npm install)
```

### Terminal 1 -- the kbagent kernel

```bash
# Point at your test config dir (any kbagent config layout works):
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

## Production / single-process mode

```bash
make web-build        # builds the React app into web/frontend/dist
uv run kbagent serve --port 8001 --config-dir ~/.config/keboola-agent-cli &
cd web/backend
STATIC_DIR=../frontend/dist \
KBAGENT_SERVE_TOKEN=<token> \
PORT=8000 npm start
# Open http://localhost:8000/
```

In production mode the BFF serves the React build statically and proxies
`/api/*` to kbagent serve -- there's no Vite dev server.

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
