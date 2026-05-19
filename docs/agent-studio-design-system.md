# Agent Studio — Design System

> **Status**: Canonical visual reference. Agent Studio (Playbooks /
> Blueprints) **does not** introduce a new design language. It plugs
> into the **NERD UI** already shipping in
> [`web/frontend`](../web/frontend) — the React + Tailwind app served by
> `kbagent serve --ui` at `http://127.0.0.1:8001/`.
>
> Treat this document as the authoritative description of NERD UI, plus
> the rules for how Playbook / Blueprint pages and components extend it.
>
> **Reference implementation**: run `kbagent serve --ui` and open
> `http://127.0.0.1:8001/`. Toggle the theme button in the TopBar to
> verify both modes; both are first-class. Any disagreement between
> this document and the running UI is a bug in this document — file an
> issue and update the spec to match the code, not the other way around.

---

## 1. Identity

### 1.1 Wordmark

The wordmark is **`kbagent`** in the brand green
(`text-keboola`, `font-bold`, `tracking-wider`, `text-sm`), preceded by a
small **animated pulse dot** (`w-2 h-2 rounded-full bg-keboola animate-pulse`)
that visually conveys "live local kernel".

Immediately after the wordmark, an inline code-style comment reads
`// NERD UI` (`text-zinc-500 dark:text-zinc-600 text-xs`). One line below,
the subtitle `Keboola kernel + UI` sits in `text-zinc-500 text-[10px]`.

This appears once, at the top of the sidebar. Do not repeat it elsewhere.

For the Playbook surface, the sidebar header stays exactly as-is.

### 1.2 Voice

NERD UI's voice is **deliberately terminal / engineer-native**:

- Lowercase casual lines: `loading`, `filter tools...`,
  `select project`, `localhost only ・ bearer auth ・ kernel: python ・ ui: typescript`.
- Code-comment idioms: `// NERD UI`.
- Status bar treated like a Unix tool footer.
- Section labels in tiny uppercase tracking-widest:
  `HOME`, `MANAGE`, `BROWSE`, `DEVELOP`, `INSIGHTS`, `AI / TOOLS`, `ADMIN`.
- Inline microcopy uses `--` em-dash separators and command-line
  metaphors: "Two paths -- pick the one that fits your task."

The Playbook surface adopts the same voice. No marketing copy. Sentences
short. Mono font carries most of the load.

### 1.3 Theme

Default theme is **light**. The app reads
`localStorage["kbagent.theme"]` on first paint (anti-FOUC inline script
in `index.html`); when nothing is saved it falls back to OS
`prefers-color-scheme: dark` (a user whose whole desktop is dark gets a
dark kbagent), otherwise it lands in light. The toggle is in the
TopBar (`Sun` / `Moon` icon plus uppercase label "LIGHT" / "DARK") and
flips a single `dark` class on `<html>`.

Every Playbook component must carry both light and dark variants
through Tailwind `dark:` utilities — never a single-mode style. Both
modes are first-class; the only asymmetry is which one a user with no
saved preference sees first.

---

## 2. Colour tokens

Source: [`web/frontend/tailwind.config.ts`](../web/frontend/tailwind.config.ts).

### 2.1 Brand palette

| Token | Hex | Usage |
|---|---|---|
| `keboola` (DEFAULT / 500) | `#22c55e` | Primary action, active nav, pulse dot, success state. |
| `keboola-50` | `#f0fdf4` | Tinted backgrounds (Playbooks list cards). |
| `keboola-400` | `#4ade80` | Hover state on dark backgrounds. |
| `keboola-600` | `#16a34a` | Pressed state. |
| `keboola-700` | `#15803d` | Deepest brand swatch. |
| `accent` | `#22d3ee` | Secondary accent (cyan) — `Sparkles` and MCP icons. |
| `neon.green` | `#39ff14` | Strong "still-alive" indicator (rarely used; live event highlight). |
| `neon.pink` | `#ff10f0` | "AI / agentic" connotation — `Brain` icon, `more agentic` pill. |
| `neon.amber` | `#ffaa00` | Warning state — DEV branch, dev/non-prod banners. |

### 2.2 Neutrals

NERD UI uses Tailwind's `zinc` palette across the board:

- **Light mode**: body `bg-zinc-50 text-zinc-900`, cards `bg-white`,
  borders `border-zinc-200`, tertiary text `text-zinc-500`.
- **Dark mode**: body `radial-gradient(at 0% 0%, #0a0a14 0%, #050508 100%)`
  with `text-zinc-100`, cards `bg-zinc-900/40 backdrop-blur` over the
  gradient, borders `border-zinc-800` / `border-zinc-900`, tertiary
  text `text-zinc-500` / `text-zinc-600`.

The radial-gradient body in dark mode is what gives NERD UI its
"machine room glow" look. Never replace it with a flat dark colour.

### 2.3 Status pills

Three pre-defined utility classes; use them, do not invent new colours
per status:

- `.nerd-pill` — neutral zinc.
- `.nerd-pill-green` — `keboola` family. Used for "default", "active",
  success, done, scheduled.
- `.nerd-pill-amber` — `neon.amber`. Used for warnings, DEV branch,
  waiting-for-input.
- `.nerd-pill-red` — `red-300` / `red-700/40`. Used for errors, failed
  runs, destructive actions.

For Playbook run states, map the `AssignmentRun.status` enum to these:
- `queued`, `running`, `scheduled`, `done` → green
- `blocked`, `waiting_for_approval`, `reviewing` → amber
- `failed`, `cancelled` → red
- `draft` → plain neutral `.nerd-pill`

---

## 3. Typography

### 3.1 Body font

**`'JetBrains Mono', Menlo, monospace`** — applied to `<body>` via the
`font-mono` Tailwind class. **The entire app is monospace by default.**
This is the single most distinctive choice of NERD UI; do not override
it for Playbook pages.

The one documented exception is `.markdown-body` (and its `.lg`
variant), which forces a sans-serif system font stack for **rendered
markdown content only** (artifact viewers, run reports). The override is
scoped to that class to prevent leakage. Playbook artifact viewers must
use `.markdown-body`/`.markdown-body-lg` for the same reason.

### 3.2 Scale (from observed usage)

| Token | Usage |
|---|---|
| `text-[10px]` `uppercase` `tracking-widest` | Sidebar section labels (`HOME`, `MANAGE`, ...), StatBar text, StatTile label cap. |
| `text-xs` (12px) | Sub-labels, captions, pill content, code blocks. |
| `text-sm` (14px) | Nav item labels, body text, button labels. |
| `text-2xl` / `text-3xl` (large bold) | Page titles ("Good Morning", "Agent Tasks", "MCP Tools"). |
| Big-mono StatTile values | e.g. `6 / 6`, `0 active`, `0 loaded` — sized roughly `text-2xl font-bold` in the existing tiles. |

### 3.3 Casing

- Headings: Title Case ("Schedule your first agent").
- Buttons: lowercase + symbol ("+ New task", "+ New CLI task", "Ask").
- Section labels: ALL CAPS tracking-widest.
- Mono identifiers: case as-is (`keboola.ex-salesforce`,
  `pb_42a1`, `data-streams-testing`).

---

## 4. Layout shell (`Shell.tsx`)

```
┌─────────────────────────────────────────────────────────────────┐
│ Sidebar w-56         │ TopBar h-12                              │
│ (light: white/80     │ (project picker + branch picker + theme  │
│  backdrop-blur,      │  toggle + kbagent serve indicator)       │
│  dark: zinc-950/60)  ├──────────────────────────────────────────│
│                      │ Main content (overflow-auto, p-6)        │
│ Logo block           │                                          │
│ // NERD UI           │   PageTitle (title + description +       │
│ Keboola kernel + UI  │     actions slot)                        │
│                      │                                          │
│ HOME                 │   <page body>                            │
│   Dashboard          │                                          │
│ MANAGE               │                                          │
│   Projects           │                                          │
│   Branches           │                                          │
│   Doctor             │                                          │
│   Changelog          │                                          │
│ BROWSE               │                                          │
│   ...                │                                          │
│ DEVELOP              │                                          │
│   ...                │                                          │
│ INSIGHTS             │                                          │
│   ...                │                                          │
│ AI / TOOLS           │                                          │
│   MCP Tools          │                                          │
│   Local AI           │                                          │
│   Agent Tasks        │                                          │
│   Playbooks   (NEW)  │                                          │
│   Blueprints  (NEW)  │                                          │
│ ADMIN                │                                          │
│   ...                │                                          │
├──────────────────────┴──────────────────────────────────────────│
│ StatusBar h-6: kbagent serve <version> · …  · localhost only…   │
└─────────────────────────────────────────────────────────────────┘
```

- Sidebar width: `w-56` (224 px). Internally `space-y-4` between
  section groups, `space-y-0.5` between nav items.
- Active nav: `bg-keboola/10 text-keboola border border-keboola/30`.
- Inactive: `text-zinc-600 hover:bg-zinc-100 hover:text-zinc-900 border border-transparent dark:text-zinc-400 dark:hover:bg-zinc-900 dark:hover:text-zinc-100`.
- Nav icons: lucide-react at `w-3.5 h-3.5`. Match an existing nav
  category when adding a new entry rather than introducing a new icon
  style.

### 4.1 Where Playbook / Blueprint pages live

Add **two** new entries to the `AI / Tools` section in
[`web/frontend/src/layout/Sidebar.tsx`](../web/frontend/src/layout/Sidebar.tsx):

```tsx
{ id: "playbooks", label: "Playbooks", icon: BookOpen },     // NEW
{ id: "blueprints", label: "Blueprints", icon: LayoutGrid }, // NEW
```

Suggested lucide icons: `BookOpen` for Playbooks, `LayoutGrid` for
Blueprints. (Both fit the existing 1.5-px-stroke set.)

`Agent Tasks` stays as-is for backwards compatibility (see
[`docs/agents-v2.md`](agents-v2.md) § 23 Migration).

### 4.2 TopBar

The existing `TopBar.tsx` is untouched. The Playbook surface continues
to read `useUIState().project` and `useUIState().branchId` — every
Playbook lives inside the currently selected project, exactly like
every other NERD UI page.

---

## 5. Component primitives (`index.css`)

Use the `.nerd-*` utility classes that already exist. Do not introduce
parallel CSS for the Playbook feature.

### 5.1 `.nerd-card`

```
rounded-md border border-zinc-200 bg-white p-4
dark:border-zinc-800 dark:bg-zinc-900/40 dark:backdrop-blur
```

The default container for every grouping (a Playbook card in the
library, the Builder hero, the run timeline pane, an approval modal).
Add brand-tinted border for emphasis:
`border-keboola/30 bg-white dark:bg-zinc-900/40` — see Dashboard's
"Ask the local AI" hero.

### 5.2 `.nerd-btn`

```
px-3 py-1.5 rounded border border-zinc-300 text-sm
hover:border-keboola hover:text-keboola transition-colors
dark:border-zinc-700
```

This is the default button. For AI-emphasis CTAs add
`hover:text-neon-pink hover:border-neon-pink/40`.

For destructive: `hover:border-red-300 hover:text-red-500`.

There is **no** filled-background "primary" button variant. The active
state of brand actions is conveyed by the keboola-green outline that
appears on hover/focus. This is intentional — NERD UI prefers the
"console command" feel.

### 5.3 `.nerd-input`

```
px-3 py-1.5 rounded border border-zinc-300 bg-white text-sm
focus:outline-none focus:border-keboola
dark:border-zinc-800 dark:bg-zinc-950
```

Used for text inputs, search boxes, the Builder textarea, the Local AI
prompt.

### 5.4 `.nerd-code`

```
font-mono text-xs text-zinc-700 bg-zinc-100 border border-zinc-200 rounded p-3 overflow-auto
dark:text-zinc-300 dark:bg-zinc-950 dark:border-zinc-800
```

For inline payloads — message previews in the Approval modal, SOP step
detail bodies, JSON output. **Use this for the body of every
Playbook approval preview** (not a generic gray inset).

### 5.5 Status pills

`.nerd-pill`, `.nerd-pill-green`, `.nerd-pill-amber`, `.nerd-pill-red`.
Plus an inline `<span className="w-2 h-2 rounded-full bg-keboola animate-pulse" />`
for live indicators (used in Agent Tasks table to mark enabled rows).

### 5.6 StatTile

Observed on the Dashboard. Anatomy:

- `.nerd-card` with explicit `relative` for the icon.
- Top-right icon (lucide) at `w-4 h-4`.
- Tiny uppercase caps label at top: `text-[10px] uppercase tracking-widest text-zinc-500`.
- Big mono value: `text-2xl font-bold` (e.g. `6 / 6`, `0 active`).
- Optional sub-line in `text-zinc-500 text-xs`.

For Playbook stats: number of Playbooks, runs today, cost today, oldest
pending approval.

### 5.7 TwoPathEmpty

Observed on the Agents page. Used when a feature has two distinct
on-ramps and the user must pick. Anatomy:

- Bold centered title and one-line subline.
- 2-column grid of large cards.
- Each card: big icon (40-px lucide, brand-coloured), bold title,
  multi-line description, single CTA button. Optional small pill at
  the top-right of one card ("more agentic" amber in Agents).

The Playbook Library's empty state uses this pattern with the two
paths being **Start from a Blueprint** (green icon) and
**Describe in plain English** (neon-pink "more agentic" pill, AI
generation path).

### 5.8 DataTable

For Playbook listings beyond ~6 rows, the existing
[`Table.tsx`](../web/frontend/src/components/Table.tsx) `DataTable`
component is the right surface. Same row-click semantics open a
detail drawer.

### 5.9 Drawer

The existing [`Drawer.tsx`](../web/frontend/src/components/Drawer.tsx)
slides in from the right. Playbook run detail (showing the
SOP timeline, approval queue, cost summary) uses the Drawer, **not** a
full-page route — matches how Agent Tasks show run history.

### 5.10 AgentRunView (timeline)

This is the most reusable existing component for Playbook UX. Its
three-pane layout (Steps timeline ‖ Step detail ‖ Cost & tokens) is
exactly what Playbook runs need:

```
┌───────────────┬─────────────────────────┬─────────────┐
│  Steps        │  Step detail            │  Cost &     │
│  (timeline)   │  (selected step body)   │  tokens     │
│  thinking →   │  Bash(...)              │  Opus 4.7   │
│  Bash         │  └ command: ls          │  $0.0234    │
│  └ ok         │  └ output: ...          │  1.2k tok   │
│  Read         │                         │  Tools used │
│  Result       │                         │  Bash × 4   │
└───────────────┴─────────────────────────┴─────────────┘
```

For Playbook runs, the timeline lanes are: `session init` → `SOP step
N` → `HITL question` → `approval request` → `tool call` → `artifact
write` → `step done` → `playbook done`.

The Artifacts tab heuristic (`extractArtifacts(steps)`) and the
markdown-body styling for reports are both reused as-is.

---

## 6. Patterns

### 6.1 PageTitle

Every page starts with the `PageTitle` component (in
[`Empty.tsx`](../web/frontend/src/components/Empty.tsx)): bold title,
single-paragraph description, and a right-aligned `actions` slot —
always lowercase + symbol prefixed (e.g. `+ New task`, `+ New workspace`).

Playbook pages follow the same shape:
- `Playbooks` / "Reusable agentic procedures running inside kbagent serve. Each playbook is a SOP, a set of connections, skills, logins, and triggers. ..." / `+ New playbook`
- `Blueprints` / "Curated playbook templates grounded in Keboola data. Fork one to get a working Playbook in seconds." / no action slot (read-only catalogue).

### 6.2 Dashboard hero (AI prompt)

The Dashboard's brand-bordered `.nerd-card` with the `Sparkles` icon
and a borderless input that hands off to Local AI is the canonical
"speak to the system" entry point. **The Playbook Builder reuses this
exact pattern** — sparkles icon, borderless input, "Generate" CTA.

The hand-off is the same: the user types, hits Generate, the runtime
streams generation progress (mirrors how
`setPendingLocalAiMessage` + page navigation works today).

### 6.3 Empty state with two paths

See § 5.7. Used in Agents today; reused for the empty Playbook
Library.

### 6.4 Sub-tab pills

When a page has tabs (Storage's `buckets | tables | files`,
SemanticLayer's CRUD tabs), each tab is a `.nerd-btn` whose active
state is `border-keboola text-keboola`. Playbook detail tabs follow the
same pattern: `Current Job | Past Jobs | Evaluations | Schedule & Trigger | My Settings`.

### 6.5 Dropdown picker

Project + Branch pickers in TopBar set the pattern for any custom
dropdown: trigger is a `.nerd-btn`-like button with chevron, popover
opens below with optional search input, body is a vertical list of
hover-highlighted rows. For lists >5, a filter input is added.

Used in Playbook surface for: connection picker, skill picker, login
picker, trigger picker.

### 6.6 SSE streaming run view

Established by Agents: `ssePost` from
[`api/client.ts`](../web/frontend/src/api/client.ts) opens an SSE
stream, events flow into `AgentRunView`. Playbook runs reuse the same
client and event envelope (see PRD § 7 `AssignmentRun.sse_event_log`).

### 6.7 PROD vs DEV branch indicator

When the user is on a non-default branch, the BranchPicker turns
neon-amber. **Every Playbook page must honour this**: if `branchId !==
null`, the page top bar (or PageTitle subtitle) carries a faint
`neon-amber/40` tint, and any write actions confirm with "on branch
#{id}" copy. Don't let an unaware user run a destructive Playbook step
on the wrong branch.

### 6.8 Approval modal

For Playbook external-send approvals (the security-critical surface
defined in PRD § 14): use a centered modal layered over the page,
**not** a Drawer. Anatomy:

- Header row: `.nerd-pill-amber` for the risk class, lucide warning
  icon, `Approval Required` heading.
- `.nerd-code` block for the message preview (mono font is already the
  default, but `.nerd-code` adds the inset background + border).
- Stat-grid for `recipient` / `risk class` / `body hash` / `expires at`
  in `text-xs` rows with tiny-caps labels.
- Bottom row: ghost `Reject`, outline `Edit then approve`, brand-hover
  `Approve & send` — all three are `.nerd-btn`, the last has explicit
  `hover:text-keboola hover:border-keboola/40`.
- After approve click: 5-second undo banner replaces the button row,
  amber tinted.

---

## 7. State model

Add new fields to [`web/frontend/src/state.tsx`](../web/frontend/src/state.tsx)
`useUIState`:

- `playbookId: string | null` — currently inspected Playbook, null on
  the library page.
- `playbookRunId: string | null` — currently inspected run (drives the
  Drawer).
- `playbookFormDraft: PlaybookFormDraft | null` — Builder local draft
  for an unsaved Playbook.

These mirror how `useUIState` already tracks `project`, `branchId`,
`pendingLocalAiMessage`. Keep the same conventions: null when not
applicable, lower-case keys, no Redux.

For server data, use TanStack Query (already wired) with the existing
[`api/client.ts`](../web/frontend/src/api/client.ts) helpers:

```ts
useQuery({
  queryKey: ["playbooks"],
  queryFn: () => api.get<{ playbooks: Playbook[] }>("/playbooks"),
  refetchInterval: 10_000,  // match Agents' polling cadence
});
```

The 10-second polling cadence on Agents is the right baseline for
Playbooks too.

---

## 8. New components introduced by Playbook surface

Each lives under `web/frontend/src/components/` and is built from the
primitives in § 5 and § 6.

| Component | Built from | Purpose |
|---|---|---|
| `PlaybookCard` | `.nerd-card` + StatTile-style metadata | Library grid cell. |
| `BlueprintCard` | `.nerd-card` + category pill | Catalogue grid cell. |
| `PlaybookBuilder` | Dashboard hero + AgentRunView-style right pane | NL → SOP creation flow. |
| `PlaybookSopPanel` | `.nerd-card` containing GOAL + numbered STEPS | Read-only SOP renderer with active-step highlight. |
| `HitlQuestionPanel` | `.nerd-card` with `.nerd-pill-amber` banner + radio cards | HITL question rendering. |
| `ApprovalModal` | new modal pattern from § 6.8 | External-send approval gate. |
| `ConnectionsModal` | dropdown picker (§ 6.5) at modal scale | 3-tier connection picker (Keboola Built-ins · Auto-discovered · Direct API). |
| `BudgetBadge` | `.nerd-pill-green` with mono content | "● $1.20 / $5.00 · 12% tokens" indicator. |
| `RunStatusBanner` | `.nerd-pill-*` at full width | Run status row at top of run pane. |

All component files include the existing two-line header comment style
(see `Sidebar.tsx`, `TopBar.tsx`) explaining their place in the page.

---

## 9. Mockup-generation contract

Mockups in [`docs/mockups/`](mockups/) must read as **screenshots of
the running NERD UI extended with Playbook surface**. Not generic
SaaS, not Inter-typography. Specifically:

1. **Default mode**: light. Dark variants are acceptable as secondary
   illustrations (e.g., engineering-facing docs, terminal-aesthetic
   marketing) but not the primary mockup.
2. **Body font**: JetBrains Mono / Menlo / monospace — **everywhere
   except** rendered markdown content.
3. **Colour**: Keboola green (`#22c55e`) for primary, brand accent
   colours from § 2.1 for secondary signals (neon-pink for AI,
   neon-amber for DEV branch / waiting, cyan for MCP).
4. **Shell**: 224-px sidebar with the exact section structure from
   § 4 (HOME / MANAGE / BROWSE / DEVELOP / INSIGHTS / AI / TOOLS /
   ADMIN), `// NERD UI` annotation, `Keboola kernel + UI` subtitle.
5. **TopBar**: project picker (alias from § 10.1), branch picker with
   `main PROD` label, theme toggle, `kbagent serve` indicator.
6. **StatusBar**: `kbagent serve <version>  · localhost only · bearer auth · kernel: python · ui: typescript`.

### 9.1 Canonical prompt preamble (light mode — primary)

Paste this verbatim at the top of every nano-banana prompt for the
default light-mode mockups. Variant prompts append scene-specific
instructions below.

```text
Modern screenshot of "kbagent NERD UI" — a local data-engineering tool
running at http://127.0.0.1:8001/. 16:9 desktop view at 2K, sharp text.

DEFAULT MODE: light — body is a flat off-white (zinc-50, #fafafa);
no radial gradient. Sidebar bg-white/80 with backdrop-blur and a
1-px zinc-200 right border. Cards bg-white with 1-px zinc-200
borders (no backdrop-blur effect — light cards sit on a flat
surface). Text zinc-900 primary, zinc-700 secondary, zinc-500
tertiary. Hover background zinc-100.

TYPOGRAPHY: JetBrains Mono / Menlo monospace for EVERY visible
string — page titles, body, mono identifiers, numbers, button
labels, dropdown values. The only sans-serif is inside rendered
markdown reports (not in this mockup unless a markdown artifact is
explicitly shown). All section labels in tiny uppercase
letter-spacing 0.2em, text-zinc-500.

SHELL (always present, never collapsed):
- Sidebar w-56 (224 px) on the left with bg-white/80 backdrop-blur,
  border-right border-zinc-200.
- Header block: small green animated pulse dot
  (`bg-keboola=#22c55e`) + keboola-green wordmark "kbagent" (bold,
  tracking-wider, text-sm) + dim "// NERD UI" comment in zinc-500
  text-xs + "Keboola kernel + UI" zinc-500 text-[10px] subtitle.
- 7 nav sections, each with a tiny gray-caps label
  (HOME, MANAGE, BROWSE, DEVELOP, INSIGHTS, AI / TOOLS, ADMIN).
  - HOME: Dashboard
  - MANAGE: Projects, Branches, Doctor, Changelog
  - BROWSE: Configs, Components, Storage, Jobs, Search
  - DEVELOP: SQL Workspaces, Flows, Schedules, Data Apps
  - INSIGHTS: Lineage, Semantic Layer
  - AI / TOOLS: MCP Tools, Local AI, Agent Tasks, Playbooks, Blueprints
  - ADMIN: Org Setup, Members, Encrypt
- Each nav item: small lucide icon (w-3.5 h-3.5) left + sm label
  text-zinc-600 inactive, hover bg-zinc-100 + text-zinc-900.
- Active nav item: keboola-green tint (bg-keboola/10 text-keboola
  border border-keboola/30) — identical to dark.
- TopBar h-12 bg-white/80 backdrop-blur, border-b border-zinc-200,
  with project picker (Layers icon + "data-streams-testing" + chevron,
  mono, text-zinc-600), branch picker (GitBranch icon + "main" + "PROD"
  zinc small caps chip), right side has theme toggle (Moon icon +
  "DARK" tiny caps — clicking switches to dark) and kbagent-serve
  indicator (Server icon green + "kbagent serve" zinc-500).
- StatusBar h-6 at bottom: bg-white/80, border-t border-zinc-200,
  "kbagent serve 0.44.0b1" left; "localhost only ・ bearer auth ・
  kernel: python ・ ui: typescript" right. Both in zinc-500 text-[10px].

STATUS INDICATORS: small coloured dot + label (animated pulse
green for "live"). Pills are outlined, not filled — keboola-green
border / keboola-green text on white, neon-amber border / amber-700
text on white, red-300 border / red-700 text on white.

DO NOT: introduce sans-serif body text, pastel pill backgrounds, the
dark-mode radial gradient (light body is flat zinc-50), backdrop-blur
on cards (cards sit on a flat surface in light mode), or narrow
icon-only sidebar — the full 224-px sidebar with section labels is
always present. Border contrast is the dominant visual rhythm in light
mode; do not soften it with washed-out greys.
```

### 9.2 Canonical prompt preamble (dark mode — secondary)

Use this when a mockup is targeted at engineering-facing surfaces or
contexts where a terminal aesthetic resonates harder than a clean
office one (developer docs, conference decks, internal architecture
diagrams). Light remains the *primary* mockup; produce the dark
variant only when the asset's destination calls for it.

The structural rules — 224-px sidebar with all 7 sections, JetBrains
Mono everywhere, outlined status pills, full StatusBar — are identical
to light. Only the surface tokens change.

```text
Modern screenshot of "kbagent NERD UI" — a local data-engineering tool
running at http://127.0.0.1:8001/. 16:9 desktop view at 2K, sharp text.

DEFAULT MODE: dark — body uses a subtle radial gradient from
#0a0a14 (top-left) to #050508 (bottom-right). Sidebar bg
zinc-950/60 with backdrop-blur; cards bg zinc-900/40 with 1-px
zinc-800 border and backdrop-blur. Text zinc-100/200 primary,
zinc-400/500 secondary, zinc-600 tertiary.

TYPOGRAPHY: JetBrains Mono / Menlo monospace for EVERY visible
string — page titles, body, mono identifiers, numbers, button
labels, dropdown values. The only sans-serif is *inside* rendered
markdown reports (not in this mockup unless a markdown artifact is
explicitly shown). All section labels in tiny uppercase
letter-spacing 0.2em.

SHELL (always present, never collapsed):
- Sidebar w-56 (224 px) on the left with bg-zinc-950/60 backdrop-blur,
  border-right border-zinc-900.
- Header block: small green animated pulse dot
  (`bg-keboola=#22c55e`) + keboola-green wordmark "kbagent" (bold,
  tracking-wider, text-sm) + dim "// NERD UI" comment in zinc-600
  text-xs + "Keboola kernel + UI" zinc-500 text-[10px] subtitle.
- 7 nav sections, each with a tiny gray-caps label
  (HOME, MANAGE, BROWSE, DEVELOP, INSIGHTS, AI / TOOLS, ADMIN).
  - HOME: Dashboard
  - MANAGE: Projects, Branches, Doctor, Changelog
  - BROWSE: Configs, Components, Storage, Jobs, Search
  - DEVELOP: SQL Workspaces, Flows, Schedules, Data Apps
  - INSIGHTS: Lineage, Semantic Layer
  - AI / TOOLS: MCP Tools, Local AI, Agent Tasks, Playbooks, Blueprints
  - ADMIN: Org Setup, Members, Encrypt
- Each nav item: small lucide icon (w-3.5 h-3.5) left + sm label.
- Active nav item: keboola-green left tint
  (bg-keboola/10 text-keboola border border-keboola/30).
- TopBar h-12 with project picker (Layers icon + "data-streams-testing"
  + chevron, mono), branch picker (GitBranch icon + "main" + "PROD"
  zinc small caps chip), right side has theme toggle
  (Sun icon + "LIGHT" tiny caps — clicking switches back to light)
  and kbagent-serve indicator (Server icon green + "kbagent serve").
- StatusBar h-6 at bottom: "kbagent serve 0.44.0b1" left;
  "localhost only ・ bearer auth ・ kernel: python ・ ui: typescript"
  right. Both in zinc-600 text-[10px].

STATUS INDICATORS: small coloured dot + label (animated pulse
green for "live"). Pills are outlined, not filled — green border /
green text, amber border / amber text, red border / red text.

DO NOT: introduce sans-serif body text, pastel pill backgrounds,
decorative gradients beyond the dark body radial-gradient, or
narrow icon-only sidebar — the full 224-px sidebar with section
labels is always present.
```

### 9.3 Scene-specific recipes

The six canonical mockups in `docs/mockups/`:

| # | File | Scene |
|---|---|---|
| 01 | `01-playbooks-library.png` | `Playbooks` nav active. Page title `Playbooks` + lowercase subtitle. `+ New playbook` outline button top-right. 3×2 grid of `.nerd-card` Playbook cards: each card shows mono `pb_xxxx · vN` chip, name in mono bold, mono "Last run / Next run" line, a status dot + label (green Active, blue Scheduled, amber Waiting, gray Draft), footer mono `N runs · $X.XX avg · tokens`. |
| 02 | `02-blueprints-catalog.png` | `Blueprints` nav active. Page title + subtitle. Category filter row (All, Data Cleanup, Process Mining, Decision Analysis, Decision Triggers, Custom Agent Builder) as `.nerd-btn` chips with the active one keboola-green outlined. Search input right. 3×3 grid of `.nerd-card` Blueprint cards with mono category tag and mono "Systems: keboola.ex-* + ..." caption. |
| 03 | `03-playbook-builder.png` | `Playbooks` nav active. Page title "New Playbook" + pencil edit. 4 collapsed dashed-border rows (Connections, Skills & Files, Logins & Secrets, Plugins). Hero: tiny "WELCOME" caps + bold "Automate your work." + 6 quick-start `.nerd-btn` chips. Then the Playbook Builder card (`.nerd-card` with `border-keboola/30`) containing Sparkles icon + bold "Playbook Builder" + mono italic placeholder + "Generate" button. Right pane: AgentRunView-style with tabs and generation progress (✓ green check / ● keboola-green filled dot / ○ zinc ring) plus mono "Detected requirements: keboola.connection, keboola.ex-salesforce..." block. |
| 04 | `04-run-view-hitl.png` | `Playbooks` nav active. Title "Cross-source CRM Cleanup" + mono "pb_42a1 · rev 3 · ● active" chip. Connections row with mono `keboola.ex-salesforce`, `keboola.ex-hubspot-crm`, `keboola.ex-zendesk-v3`. SOP panel with GOAL paragraph and numbered STEPS (✓ done in zinc-faded, ● current in keboola-green bold, ○ pending zinc). Right pane: tabs (Current Job active), top-right `BudgetBadge` mono "● $1.20 / $5.00 · 12% · 124k tokens", amber `.nerd-pill` banner "● waiting_for_input · run #run-a7b3c2", agent message bubble with mono stats, HITL radio panel with 4 options including selected "email OR phone" (keboola-green ring + tint). |
| 05 | `05-approval-modal.png` | Dimmed background showing the "Daily AR Deductions" run view. Centered modal with `.nerd-card` styling, large size. Header: amber bell-warning icon + bold "Approval Required" + mono amber `.nerd-pill-amber` `external_send`. Title "Send Slack message to #ar-collections". Subtitle mono "playbook: Daily AR Deductions · run #run-14b8e · step 7 of 9". Amber countdown chip "Expires in 58:23". `MESSAGE PREVIEW` tiny caps label then `.nerd-code` block (mono, dark inset) with the Slack body. 2-column detail grid with `RECIPIENT` / `RISK CLASS` / `SENDER` / `BODY HASH` (mono `sha256:9f2c7d8a…b481a3 · ● verified`) / `REASON` / `UNDO WINDOW`. Action row: ghost Reject, outline `Edit then approve`, brand-hover `Approve & send`. |
| 06 | `06-connections-picker.png` | Dimmed background showing the New Playbook page. Centered modal `.nerd-card` ~920 px. Header: lightning-bolt icon + "Select Connections for this Playbook". Search input. Three sections each with tiny-caps label and a 2-col grid of `.nerd-card`-style toggle rows: KEBOOLA BUILT-INS (Storage, Workspace SQL, Semantic Layer, Lineage, Jobs, Branches), FROM YOUR KEBOOLA PROJECT (Salesforce / HubSpot / Zendesk / Snowflake / GA / Stripe with mono `keboola.ex-* + keboola.wr-*` captions and "1,247 components" link), DIRECT API CONNECTIONS (Slack / Gmail / Linear / Teams). Bottom row: mono "Selected: 6 · 3 read · 1 write · 2 R/W" left, Cancel / Confirm buttons right. |

---

## 10. Canonical example fixtures

| Field | Value |
|---|---|
| Project alias | `data-streams-testing` |
| Branch | `main` (label "PROD") |
| Primary Playbook | `Cross-source CRM Cleanup` (`pb_42a1`, rev 3) |
| Secondary Playbook | `Daily AR Deductions` (`pb_a4d8`, rev 5) |
| Library cards | `pb_42a1`, `pb_8c33`, `pb_e09f`, `pb_a4d8`, `pb_61cb`, `pb_draft` |
| Workspace handle | `stoical.eastern-3…` (omit when irrelevant) |
| HITL example run | `#run-a7b3c2` on `pb_42a1` rev 3 |
| Approval example run | `#run-14b8e` on `pb_a4d8` rev 5 |
| Budget badge | `$1.20 / $5.00 · 12% · 124k tokens` |
| Body hash | `sha256:9f2c7d8a…b481a3` |
| Approval expiry | `58:23` |
| Storage tables | `in.c-sf.contact` (12,847), `in.c-hs.contact` (8,402), `in.c-zd.user` (3,219) → `out.c-crm-clean.customers` |
| Skill versions | `entity-resolution v1.2`, `schema-normalization v0.9`, `data-quality-profiling v1.0` |
| Connections | Salesforce (`keboola.ex-salesforce + keboola.wr-salesforce`), HubSpot (`keboola.ex-hubspot-crm`), Zendesk (`keboola.ex-zendesk-v3`), Snowflake (`keboola.wr-db-snowflake`) |
| kbagent serve version | `0.44.0b1` |

The set is small on purpose. Re-using these handles across screens
makes the mockup set read as one continuous product, not six unrelated
screenshots.

---

## 11. Anti-patterns

- **Sans-serif body anywhere except `.markdown-body`.** Mono is the
  signature. Use `.markdown-body` only in artifact viewers / report
  panes.
- **Pastel-filled status pills.** Use the outlined `.nerd-pill-*`
  classes.
- **A narrow icon-only sidebar** for documentation parity. The full
  224-px sidebar is always present in mockups.
- **A different brand colour for Playbook-vs-Agent-Tasks.** Both use
  the same keboola-green primary; Playbook does not introduce a new
  brand colour.
- **Hand-rolled buttons/inputs.** Reuse `.nerd-btn`, `.nerd-input`,
  `.nerd-card` — extending them with extra Tailwind utilities is fine,
  but never invent a parallel base.
- **Bright "approve" buttons.** Even the Approval modal's primary
  action uses the hover-keboola pattern, not a filled green button.
- **Removing the StatusBar.** `localhost only ・ bearer auth ・ kernel:
  python ・ ui: typescript` is part of the brand — every screen has it.

---

## 12. Versioning

This document tracks the actual code in
[`web/frontend`](../web/frontend). Material changes to the visual
contract require:

1. Updating this file in the same PR as the code change.
2. Regenerating affected mockups in `docs/mockups/`.
3. Noting "Agent Studio UI" entry in the next `kbagent` changelog.

Routine spelling/copy fixes do not need a release note.
