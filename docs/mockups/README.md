# Agent Studio — UI Mockups

Visual concept screens for the Playbook / Blueprint surface proposed in
[`docs/agents-v2.md`](../agents-v2.md). Every mockup conforms to the
canonical [`docs/agent-studio-design-system.md`](../agent-studio-design-system.md)
— that document is the source of truth for the **NERD UI** colours,
typography, layout, and component anatomy. Mockups are screenshots of
the design system rendered with Playbook content, not exceptions to it.

> Agent Studio does **not** introduce a new design language. It plugs
> into the NERD UI already shipping in
> [`web/frontend`](../../web/frontend) — the React + Tailwind app served
> by `kbagent serve --ui` at `http://127.0.0.1:8001/`. The mockups read
> as screenshots of the existing NERD UI extended with Playbook and
> Blueprint pages.

## Light = primary, dark = secondary

Since the May 2026 pivot, **light mode is the primary surface**. New
users with no saved theme preference land in light (unless their OS
declares `prefers-color-scheme: dark`). Dark mode remains a
first-class alternative — every component carries both variants
through Tailwind `dark:` utilities — but the default mockup set
showcases light. The companion `*_dark.png` files preserve the dark
variant for engineering-facing decks, internal architecture
diagrams, or any context where the terminal aesthetic resonates
harder than the clean-office one.

| File | Primary (light) | Secondary backup (dark) |
|---|---|---|
| Playbooks Library | [`01-playbooks-library.png`](01-playbooks-library.png) | [`01-playbooks-library_dark.png`](01-playbooks-library_dark.png) |
| Blueprints Catalogue | [`02-blueprints-catalog.png`](02-blueprints-catalog.png) | [`02-blueprints-catalog_dark.png`](02-blueprints-catalog_dark.png) |
| Playbook Builder | [`03-playbook-builder.png`](03-playbook-builder.png) | [`03-playbook-builder_dark.png`](03-playbook-builder_dark.png) |
| Run View with HITL | [`04-run-view-hitl.png`](04-run-view-hitl.png) | [`04-run-view-hitl_dark.png`](04-run-view-hitl_dark.png) |
| Approval Modal | [`05-approval-modal.png`](05-approval-modal.png) | [`05-approval-modal_dark.png`](05-approval-modal_dark.png) |
| Connections Picker | [`06-connections-picker.png`](06-connections-picker.png) | [`06-connections-picker_dark.png`](06-connections-picker_dark.png) |

All 12 PNGs are 2752×1536 (16:9 @ 2K), generated with Gemini 3 Pro
Image (`mcp__nanobanana__generate_image`, `model_tier: pro`,
`resolution: 2k`).

## What every mockup shares

- The full 224-px sidebar (`bg-white/80` in light, `bg-zinc-950/60`
  in dark) with keboola-green (`#22c55e`) active indicator and the
  animated pulse-dot `kbagent` wordmark — visible on every screen,
  even behind modals.
- The same nav structure with 7 sections (HOME / MANAGE / BROWSE /
  DEVELOP / INSIGHTS / AI / TOOLS / ADMIN). Playbooks + Blueprints
  sit inside `AI / TOOLS`.
- **JetBrains Mono / Menlo monospace for every visible string** —
  page titles, body, identifiers, numbers, button labels, dropdown
  values. The only sans-serif anywhere is inside rendered markdown
  artifacts (`.markdown-body`), which never appears in this set.
- Status indicators rendered as **outlined dot + label** (`● Active`),
  never as solid-fill pills. In light: green/amber/red borders on
  white. In dark: same borders on translucent zinc-900/40 cards.
- Light body is flat off-white (`zinc-50` `#fafafa`), no gradient —
  border contrast (`zinc-200`) carries the visual rhythm.
- Dark body uses the radial-gradient (`#0a0a14` → `#050508`) that
  gives NERD UI its "machine room glow" look.
- TopBar with project picker `data-streams-testing`, branch picker
  `main` + `PROD` chip, theme toggle (Moon icon + `DARK` in light
  mode, Sun icon + `LIGHT` in dark — the toggle always shows the
  *target* mode), and `kbagent serve` indicator.
- StatusBar reading
  `kbagent serve 0.44.0b1 · localhost only ・ bearer auth ・ kernel: python ・ ui: typescript`.
- The canonical example fixtures from
  [§ 10 of the design system](../agent-studio-design-system.md#10-canonical-example-fixtures)
  so the screens read as one continuous product (same workspace,
  same playbooks, same run IDs).

## Per-scene summary

| # | Mockup | What it shows | PRD section |
|---|---|---|---|
| 01 | Playbooks Library | `Playbooks` nav active. 3×2 grid of `.nerd-card` cards (white, zinc-200 border) showing mono `pb_xxxx · vN` chips, outlined status dot+label, mono cost/run averages. `+ New playbook` outline button top-right. | §20.1 |
| 02 | Blueprints Catalogue | `Blueprints` nav active. Category filter row of `.nerd-btn` chips (active = keboola-green outline), 3×3 card grid with mono category tags and mono `Systems: keboola.ex-* + ...` captions. | §12, §18 |
| 03 | Playbook Builder | NL → SOP creation. Left pane: 4 dashed-row configurators, hero "Automate your work.", 6 quick-start chips, brand-bordered builder card. Right pane: AgentRunView-style generation progress with mono timestamps, ✓/●/○ glyphs, `.nerd-code` detected-requirements block. | §20.3 |
| 04 | Run View with HITL | Active run of `Cross-source CRM Cleanup`. Connection chips with mono `keboola.ex-*` captions. SOP panel with GOAL + numbered STEPS, current step in keboola-green. Right pane: tabs, `BudgetBadge` `● $1.20 / $5.00 · 12% · 124k tokens`, amber `waiting_for_input` banner, HITL radio panel with `email OR phone` selected. | §20.5 |
| 05 | Approval Modal | External-send approval gate over a dimmed `Daily AR Deductions` run view. Amber `external_send` pill, `.nerd-code` Slack message preview, 2-column detail grid (recipient / sender / risk class / undo window / body hash / reason), `sha256:9f2c…b481a3 · ● verified`, `Expires in 58:23` countdown, ghost Reject / outline Edit / brand-hover Approve actions, `// 5-second undo window` caption. | §14, §20.5 |
| 06 | Connections Picker | Three-tier connection model over the dimmed New Playbook page. KEBOOLA BUILT-INS (6 cards), FROM YOUR KEBOOLA PROJECT (auto-discovered Salesforce / HubSpot / Zendesk / Snowflake / GA / Stripe via `keboola.ex-* / keboola.wr-*`, `View all 1,247 components →` link), DIRECT API CONNECTIONS (Slack / Gmail / Linear / Teams). Footer: `Selected: 6 · 3 read · 1 write · 2 R/W`. | §9, §20.4 |

## Re-generation

The single source of truth for the visual contract is
[`docs/agent-studio-design-system.md`](../agent-studio-design-system.md).

### Light variant (primary) — conditioning workflow

After several iterations we found that pure text-to-image regeneration
of the full NERD UI shell from a long prompt produces sidebars with
hallucinated nav items (e.g. "blain", "Categoria", "JetBrains Mono"
listed as nav entries). The shell is too dense — 18 nav items × 7
section headers — for the model to render reliably alongside detailed
page content. The fix that worked: **condition on a real screenshot
of the running NERD UI**.

The two reference screenshots used for the current set live in
[`_references/`](_references/):
- [`_references/kbagent-light-reference.png`](_references/kbagent-light-reference.png) (1920×1080 viewport)
- [`_references/kbagent-light-reference-2560.png`](_references/kbagent-light-reference-2560.png) (2560×1440 viewport)

Both are screenshots of the live Dashboard at
`http://127.0.0.1:8001/` in light mode, captured via Playwright.

To regenerate any light mockup:

1. Make sure `kbagent serve --ui` is running, theme set to light.
2. (Optional) Refresh the reference screenshot with Playwright:
   ```python
   await page.setViewportSize({"width": 2560, "height": 1440})
   await page.goto("http://127.0.0.1:8001/")
   await page.evaluate("localStorage.setItem('kbagent.theme', 'light')")
   await page.screenshot(path="docs/mockups/_references/kbagent-light-reference-2560.png")
   ```
3. Call `mcp__nanobanana__generate_image` with:
   - `mode: "edit"`
   - `input_image_path_1: "/path/to/docs/mockups/_references/kbagent-light-reference-2560.png"`
   - `model_tier: "pro"`, `aspect_ratio: "16:9"`, `resolution: "2k"`
   - Prompt: instruct the model to PRESERVE the sidebar, TopBar, and
     StatusBar exactly, with one additive change ("add Playbooks and
     Blueprints under AI / TOOLS"), and REPLACE the main content area
     with the scene-specific recipe from
     [§ 9.3 of the design system](../agent-studio-design-system.md#93-scene-specific-recipes).

Use the fixtures from
[§ 10](../agent-studio-design-system.md#10-canonical-example-fixtures)
so the new mockup matches the rest of the set.

### Dark variant (secondary) — text-to-image workflow

The dark variant doesn't have a clean live-UI reference to condition
on (the dark mockups predate the light pivot). Regenerate the dark
mockups with the legacy text-to-image preamble at
[§ 9.2 of the design system](../agent-studio-design-system.md#92-canonical-prompt-preamble-dark-mode--secondary)
plus the same scene recipe from § 9.3.

The dark PNGs in the table above were generated this way and have the
same well-known sidebar limitation (mostly-correct section headers,
some hallucinated items inside) — see "Known limitations" below.

### Known limitations of the AI-generated mockups

These mockups are concept screens, not pixel-perfect production
screenshots. Two visible artefacts the current generation cannot
fully avoid:

1. **OCR-style typos in small sidebar labels.** The output resolution
   from `mode: "edit"` is ~1376×768 regardless of source dimensions,
   so JetBrains Mono labels at ~10 px height occasionally drift one
   or two characters ("MANAGE" → "MURACE", "BROWSE" → "BBOSE",
   "SQL Workspaces" → "SGL Workspaces"). The structure — every
   section header, every item, the active highlight, the order — is
   preserved. Treat the visible labels as illustrative; the canonical
   spelling lives in
   [`web/frontend/src/layout/Sidebar.tsx`](../../web/frontend/src/layout/Sidebar.tsx)
   and design system § 4.
2. **Output downscaled to ~720p in edit mode.** Native resolution is
   1376×768, smaller than the dark variants' 2752×1536. For a sales
   deck this is fine; for print-quality assets the dark variant is
   sharper, but the brand registry says light is primary.

Each PNG has a `*_thumb.jpeg` thumbnail beside it for quick browsing.

## Earlier names

Two entities were renamed during design iteration:

- *Assignment* → **Playbook** (every kbagent playbook is a runnable
  SOP procedure — "playbook" captures the procedural, reusable nature
  better and is distinct from generic SaaS "assignment").
- *Solution* → **Blueprint** (a vertical template you fork into a new
  Playbook — "blueprint" reads as engineering-grade pre-design).

Both renamings flow through `docs/agents-v2.md`, the design system,
and every mockup.

## Why the visual style shifted twice

- **Iteration 1** used a generic light-mode SaaS look with Inter
  typography and pastel-filled status pills. Retired — the Agent
  Studio surface lives inside the existing kbagent NERD UI, so it
  must adopt mono typography and outlined status indicators.
- **Iteration 2** went the other way: dark-by-default with neon
  accents, monospace everywhere. That captured NERD UI faithfully
  but defaulted to a mode that felt heavy for sales decks and
  customer-facing dossiers.
- **Iteration 3 (current)** keeps the NERD UI typography and
  primitives but swaps the default to **light**. Dark remains a
  first-class alternative for engineering-facing surfaces. Both
  are kept on disk: this README's table links to both variants.

See [`docs/agent-studio-design-system.md` § 11
Anti-patterns](../agent-studio-design-system.md#11-anti-patterns) for
the things this set deliberately avoids.
