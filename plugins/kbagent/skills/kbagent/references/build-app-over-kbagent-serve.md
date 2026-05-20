# Building an App over `kbagent serve`

This skill teaches you to scaffold a use-case-specific React application
that lives inside `kbagent serve --ui`, talks to the FastAPI HTTP API,
and optionally invokes AI for specific actions.

It is the frontend twin of [`programming-with-cli.md`](programming-with-cli.md)
(which covers using kbagent as a Python SDK). Read this **before** writing
any code under `web/frontend/`.

## When to build an App vs a Playbook vs neither

Decide first — it saves a rewrite:

| User wants | Build a |
|---|---|
| Linear workflow with 3–8 steps and human approval at gates | Playbook (Agent Studio runtime, see `docs/agents-v2.md`) |
| Explore data per-row with custom visualisations (histograms, diffs) | **App** (this skill) |
| Recurring dashboard that aggregates across projects and lets the user drill down | **App** (this skill) |
| One-shot data task (export a table, run a job, dump a config) | `kbagent` CLI -- no UI needed |
| AI-driven analysis with arbitrary tool use | Playbook with a single `analyse` step |

An App may **embed AI calls as buttons** (e.g., "propose column type",
"rewrite this SQL for BigQuery"). Those calls are still HTTP -- the AI
runs server-side, the App just renders the result. Apps and Playbooks
coexist; they are not exclusive.

## Architecture in one paragraph

`kbagent serve` exposes a typed FastAPI HTTP API on `/api/*` (151 paths,
24 routers). The React SPA in `web/frontend/` is served alongside on the
same origin, with cookie-based auth that the browser attaches
transparently. **Apps** are React components dropped into
`web/frontend/src/apps/<slug>/` -- a registry picks them up at build
time, wires them into the sidebar, and Router renders them at
`app:<slug>`. No manual routing.

```
┌──────────────────────────────────────────────────────────────┐
│ web/frontend/src/apps/<slug>/              ← your app lives here │
│   index.tsx     ← default export: AppManifest                    │
│   <Component>.tsx                                              │
│   types.ts     ← (optional) app-local helper types               │
├──────────────────────────────────────────────────────────────┤
│ web/frontend/src/api/                                            │
│   generated.ts  ← auto, from /openapi.json (do not edit)         │
│   types.ts      ← re-exports `paths`, `components`               │
│   client.ts     ← `api.get/post`, `sseSubscribe`, `ssePost`      │
├──────────────────────────────────────────────────────────────┤
│ kbagent serve (FastAPI)                                          │
│   GET /jobs?project=...&status=...                               │
│   POST /agents/{id}/run/stream  ← SSE for long-running AI work   │
│   ... (151 paths total -- see web/frontend/src/api/openapi.json) │
└──────────────────────────────────────────────────────────────┘
```

## Step-by-step: create a new app

### 1. Pick the slug

URL-safe, kebab-case. Must match the folder name.

### 2. Create the folder

```bash
mkdir -p web/frontend/src/apps/<slug>
```

### 3. Write `index.tsx` with a default-exported manifest

```tsx
// web/frontend/src/apps/<slug>/index.tsx
import { Sparkles } from "lucide-react";
import type { AppManifest } from "../_registry";
import { MyAppPage } from "./MyAppPage";

const manifest: AppManifest = {
  slug: "<slug>",                  // must match folder name
  label: "My App",                 // sidebar text, lowercase-ish
  section: "Apps",                 // sidebar section header (optional)
  icon: Sparkles,                  // any lucide-react icon
  component: MyAppPage,            // the page React component
  description: "What this app does in one line.",
};

export default manifest;
```

### 4. Build the page component

Use the existing NERD UI primitives -- do **not** invent your own. They
carry both light and dark mode styles and match the rest of the UI.

```tsx
// web/frontend/src/apps/<slug>/MyAppPage.tsx
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { Empty, ErrorBox, Loading, PageTitle } from "../../components/Empty";
import { DataTable } from "../../components/Table";
import type { Job, ProjectError } from "../../types";

interface JobsResp {
  jobs: Job[];
  errors: ProjectError[];
}

export function MyAppPage() {
  const q = useQuery<JobsResp>({
    queryKey: ["my-app-jobs"],
    queryFn: () => api.get("/jobs", { query: { limit: 100 } }),
    refetchInterval: 30_000,
  });

  return (
    <div className="space-y-4">
      <PageTitle title="My App" description="What's on the screen." />
      {q.isLoading ? <Loading /> : null}
      {q.error ? <ErrorBox message={(q.error as Error).message} /> : null}
      {q.data?.jobs.length === 0 ? <Empty title="No jobs found" /> : null}
      {q.data?.jobs && q.data.jobs.length > 0 ? (
        <DataTable
          rows={q.data.jobs}
          rowKey={(j) => `${j.project_alias}:${j.id}`}
          columns={[
            { header: "Project", cell: (j) => j.project_alias },
            {
              header: "Status",
              cell: (j) => <span className="nerd-pill-green">{j.status}</span>,
            },
            { header: "Component", cell: (j) => j.component },
          ]}
        />
      ) : null}
    </div>
  );
}
```

### 5. Reload

```bash
make web-dev   # or just: cd web/frontend && npm run dev
```

The sidebar shows your app under the "Apps" section. Click it. Done.

## NERD UI primitives -- the building blocks

These exist in `web/frontend/src/`. Reuse them, do not duplicate.

| Need | Use | Import from |
|---|---|---|
| Page header | `<PageTitle title="..." description="..." />` | `components/Empty` |
| Loading state | `<Loading />` | `components/Empty` |
| Error banner | `<ErrorBox message={msg} />` | `components/Empty` |
| Empty state | `<Empty title="..." hint="..." />` | `components/Empty` |
| Tabular data | `<DataTable rows columns rowKey onRowClick />` | `components/Table` |
| Side drawer | `<Drawer open onClose>...</Drawer>` | `components/Drawer` |
| Raw JSON viewer | `<JsonView data={obj} />` | `components/JsonView` |
| Status pill | `<span className="nerd-pill-green">ok</span>` | utility class |
| Secondary button | `<button className="nerd-btn">...</button>` | utility class |
| Brand colour text | `<span className="text-keboola">...</span>` | Tailwind |
| Accent (cyan) text | `<span className="text-accent">...</span>` | Tailwind |

Status pills map `AgentRun.status` / `Job.status` like this:

- `success`, `done`, `scheduled` -> `nerd-pill-green`
- `warning`, `processing`, `waiting_for_approval` -> `nerd-pill-amber`
- `error`, `failed`, `cancelled` -> `nerd-pill-red`
- everything else / neutral -> `nerd-pill`

## Data fetching: use `api.get/post`, not raw `fetch`

```tsx
import { api } from "../../api/client";

// GET with typed response and query params
const data = await api.get<MyResponse>("/configs", {
  query: { project: ["proj-a", "proj-b"], component_type: "extractor" },
});

// POST with JSON body
const result = await api.post<RunResponse>("/agents/abc/run", { input });

// DELETE
await api.delete("/agents/xyz");
```

`api` already handles:
- Cookie-based auth on same-origin (`credentials: "include"`)
- Error envelope unwrapping (throws `ApiError` with `code` and `status`)
- 204 No Content
- JSON vs text response auto-detection

**Never** call `fetch()` directly from app code. **Never** put a bearer
token in JavaScript -- the cookie-based auth handles it.

## Streaming long-running calls (SSE)

For AI work that streams progress (chat, agent runs, prompt improvement):

```tsx
import { ssePost } from "../../api/client";

const handle = ssePost(
  "/agents/abc/run/stream",
  { input: "..." },
  {
    progress: (data) => setProgress(data as ProgressEvent),
    result: (data) => setResult(data),
    error: (data) => setError(data),
  },
);
// later: handle.abort()
// completion: handle.done.then(...).catch(...)
```

For GET-style streams (e.g. tailing an existing run), use `sseSubscribe`
which returns a vanilla `EventSource`.

## Types: prefer `components["schemas"]` from the generated client

For new schemas the backend exposes, prefer the generated types:

```tsx
import type { components } from "../../api/types";

type AgentTask = components["schemas"]["AgentTask"];
```

For legacy hand-written types (Job, Bucket, Config, ...), import from
`src/types.ts` -- those mirror the API but are permissive (`Record` for
nested blobs).

Regenerate typed schemas after backend changes:

```bash
make web-gen-types
```

CI runs `make web-types-check` to catch drift.

## AI invocation from an App (the "agents may or may not be involved" part)

**Default to local AI.** `kbagent serve` exposes two distinct AI surfaces
and the choice matters more than it looks:

| Endpoint | Backend | Auth requirement | When to use |
|---|---|---|---|
| `POST /ai/chat/stream` | Local CLI (claude / codex / gemini) spawned as a child process | Just the user's own local CLI install — no Keboola tokens | **Default for apps.** Privacy-preserving, no master-token dependency, free under the user's own subscription. |
| `POST /kai/ask` | Hosted Kai service | **MASTER storage token** on the project | Only when the user has master token configured AND wants hosted Kai specifically (e.g. for Kai's project-aware grounding). |

Most app users do NOT have a master token (they shouldn't, by default
in kbagent ≥0.29). Apps that wire AI through Kai will silently fail
for those users. Use local AI; reserve Kai for a fallback option.

### Pattern A: One-shot AI call

Use the `askLocalAi` helper in `web/frontend/src/api/ai.ts`. It wraps
the SSE plumbing and resolves with the final response text:

```tsx
import { askLocalAi } from "../../api/ai";

const response = await askLocalAi({
  cli: "claude",                  // or "codex" / "gemini"
  message: "Summarise this config in one line: ...",
  project,                        // active project from useUIState()
  branchId,                       // active branch
});
// response is plain text — pipe through your own parser if you need
// structured output. See apps/type-inspector/ai_parse.ts for an example.
```

Wrap in `useMutation` for click-driven flows:

```tsx
const mutation = useMutation({
  mutationFn: () => askLocalAi({ message: "...", project }),
  onSuccess: (text) => setResult(text),
});
```

### Pattern B: Streamed AI call (long, want partial output)

When you want to show the AI's output as it generates (chat-like
typing experience), drop down to `ssePost` directly:

```tsx
import { ssePost } from "../../api/client";

const [text, setText] = useState("");
const handle = ssePost(
  "/ai/chat/stream",
  { cli: "claude", message: "...", project, branch_id: branchId },
  {
    stdout: (d) => {
      // Each CLI emits a different shape. Claude:
      //   { type: "assistant", message: { content: [{ type: "text", text }] } }
      // Codex/Gemini:
      //   { text: "..." }
      // Accumulate per the shape you care about.
    },
    done: (d) => {
      const data = d as { status?: string; response?: string };
      if (data.status === "error") setText("(error)");
      else if (data.response) setText(data.response);
    },
  },
);
// Cancel: handle.abort()
// Completion: handle.done.then(...).catch(...)
```

If you only need the final text, `askLocalAi` already does this for
you — prefer it.

### Letting the user pick which local CLI to use

The user may have `claude`, `codex`, and `gemini` installed in
different combinations. `apps/type-inspector/` ships a tiny dropdown
that switches `cli` between the three; reuse that pattern when the
user might benefit from choosing.

## Gotchas (the things you will get wrong on the first try)

### 1. Response envelopes vary per endpoint -- do not assume `T[]`

`api.get<T>(path)` is a generic; **you are telling TypeScript** what
the response is, the compiler believes you, and runtime explodes if
you are wrong. Many `kbagent serve` routes return envelopes:

```ts
// WRONG -- /projects returns { projects: Project[] }, not Project[]
const r = await api.get<Project[]>("/projects");
r.map(...)        // TypeError: r.map is not a function at runtime

// RIGHT
const r = await api.get<{ projects: Project[] }>("/projects");
r.projects.map(...)
```

Common envelopes in this API:
- `/projects` -> `{ projects: Project[] }`
- `/configs` -> `{ configs: ConfigSummary[], errors: ProjectError[] }`
- `/jobs` -> `{ jobs: Job[], errors: ProjectError[] }`
- `/storage/buckets` -> `{ buckets: Bucket[], errors: ProjectError[] }`

Before typing a query, grep an existing page that hits the same route
and copy its `useQuery<...>` declaration. Or check `openapi.json`.

The `errors` array is partial-success accumulation: when one project
in a multi-project call fails, the others still return. Show those to
the user as a yellow "Partial results" banner -- do not silently drop
them.

### 2. `import.meta.glob` needs `vite/client` types

If `tsc -b` complains "Property 'glob' does not exist on type
'ImportMeta'", add `web/frontend/src/vite-env.d.ts` with a single
line: `/// <reference types="vite/client" />`. The registry uses
`import.meta.glob` to discover apps.

### 3. `--ui-dist` overrides bundled UI for local testing

`kbagent serve --ui` auto-detects the UI bundle in this order:
`$KBAGENT_UI_DIST` -> packaged `_ui_dist` -> repo `web/frontend/dist`
-> `<cwd>/web/frontend/dist`. After `npm run build`, your fresh dist
might be ignored in favour of the packaged one. Use
`kbagent serve --ui-dist /path/to/web/frontend/dist` to force the
fresh build during dev.

## Anti-patterns to avoid

- **Inventing your own design tokens**. If `nerd-pill-amber` exists, use
  it. Do not write `bg-orange-200 text-orange-900 rounded-full px-2`.
- **Skipping dark mode**. Every Tailwind class needs a `dark:` variant
  unless the colour is identical in both modes (rare). The
  `nerd-*` classes already carry both.
- **Calling `window.location.reload()`** to refresh data. Use
  `useQuery` with `refetchInterval` or `queryClient.invalidateQueries`.
- **Putting business logic in App.tsx or Router**. Keep app code inside
  `apps/<slug>/`. The registry should be the only point of contact.
- **Hardcoding project aliases or branch IDs**. Read them from
  `useUIState()` -- the topbar lets the user switch.
- **Importing from a sibling app**. Apps must not depend on each other.
  If two apps share code, extract it to `web/frontend/src/components/`
  or `web/frontend/src/lib/`.
- **Mocking API responses**. If the endpoint is missing, add the route
  to `serve` first; do not stub.

## Choosing a template

Three archetypes cover most apps. Pick the reference app whose shape
matches yours and start there.

| Archetype | Use when | Reference app |
|---|---|---|
| Dashboard | Cross-project aggregation, KPIs, drill-down | `apps/morning-brief/` |
| Inspector | Per-row profile + per-row actions, including AI-button calls | `apps/type-inspector/` |
| Wizard | Linear stepper with HITL between steps | **Build a Playbook instead** (Agent Studio runtime); see `docs/agents-v2.md`. Wizards belong in the Playbook surface, not in `apps/`. |

If the archetype doesn't fit, start from the closest one and strip.
Don't start from `vite create` -- you will diverge from NERD UI within
two screens.

### The Inspector archetype + embedded AI button

`apps/type-inspector/` is the canonical example of the pattern
"app does the data work, AI is a button". Worth reading even if your
use case is something else, because it codifies four moves:

1. **Pure logic lives in a sibling `.ts` file** (`profile.ts`,
   `kai_parse.ts`) -- unit-tested with vitest, no React imports. The
   page file is layout only.
2. **`useMutation` for the AI call**, not `useQuery`. Triggered by a
   click; state machine per row (`pending | loading | proposed |
   approved | rejected | error`) keeps the UI honest about what
   happened.
3. **Heuristic parsing of free-text AI responses** is unavoidable --
   even when you prompt "reply with only the type", `kai/ask`
   sometimes wraps the answer in backticks or a sentence.
   `kai_parse.ts` ships extraction strategies + tests for the obvious
   wrappers. Reuse this pattern; do not reinvent it ad-hoc.
4. **The destructive step (table swap) is a stub**. The "Apply"
   drawer explains what would happen and offers "Copy as Playbook
   stub" -- because branching + verification + swap is a Playbook
   responsibility, not an app one. Apps produce the input; Playbooks
   execute it. Honour that boundary.

### Backend response shapes you'll need

Reusing the same patterns across apps saves time. The two endpoints
the Inspector relies on are worth knowing:

```
GET /storage/table-detail/{project}/{table_id}
  -> { table_id, columns: string[], column_details: Array<{name, type?, length?}>,
       rows_count, primary_key, metadata, ... }

GET /storage/table-preview/{project}/{table_id}?limit=N&columns=...
  -> { header: string[], rows: unknown[][], row_count }
  Caveat: synchronous export limit is 30 columns. Wider tables
  require paging via the `columns` query param.
```

## When to add a new backend endpoint vs reuse an existing one

If your App needs data that an existing route returns -- reuse it. The
151 routes cover most CRUD. **Do not** add a route just to reshape the
response; do the reshape on the client.

Add a backend route when:
- The shape requires joining data from multiple Keboola APIs server-side
- You need server-side state (caching, queueing) the client cannot do
- The operation is destructive or AI-mediated and must be audited

New routes go in `src/keboola_agent_cli/server/routers/<topic>.py` and
follow the existing pattern (Pydantic models, error envelope, registry
injection). Then `make web-gen-types` to update the frontend types.

## Verification checklist before claiming "app is done"

- `cd web/frontend && npx tsc -b` passes (no TS errors)
- `cd web/frontend && npm run build` succeeds
- App appears in sidebar under intended section
- Light and dark mode both look right (toggle via TopBar)
- Loading, error, and empty states all render (kill the backend to
  test error; clear filters to test empty)
- No `console.error` in browser DevTools
- No hardcoded project alias, branch ID, or token in the code
- `make web-types-check` clean (no stale generated.ts)

If you have followed this skill, all of the above pass on the first try.
If they do not, the gap is a bug in this skill -- file an issue and
update the skill, not the surrounding code.
