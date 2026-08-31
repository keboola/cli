# RFC 0001: Autonomous Agent Self-Service Project Provisioning

## Status

Draft / Proposed — request for comments. Not accepted, nothing implemented.

## Date

2026-05-27

## Context

The goal under discussion: **an external AI agent downloads `kbagent`, creates
its own Keboola project, and — over time — pays for it, with no human in the
operational loop.** "External" means the agent is *not* operating inside a
Keboola organization that already exists; it wants to become a customer in its
own right.

This is the hardest of three scenarios (the other two — Keboola provisioning
projects inside its own org, and a Keboola customer's agent provisioning inside
the customer's already-billed org — are strictly easier and are *not* the
subject of this RFC).

### What exists today (the `kbagent` side)

- `ManageClient` ([manage_client.py](../../src/keboola_agent_cli/manage_client.py))
  wraps `get_project`, `list_organization_projects`, `create_project_token`,
  invitations and members. **It has no `create_project` wrapper** — so `kbagent`
  cannot create a project today, even though the Manage API endpoint itself
  exists (see "Verified Manage API surface" below). Account/organization
  creation has no API at all.
- `OrgService.setup_organization` ([org_service.py](../../src/keboola_agent_cli/services/org_service.py))
  registers *already-existing* projects: it mints a Storage token via the Manage
  API and writes the project into `config.json`. The provisioning chain is:

  ```
  create_account/org (NO API) → create_project (API exists, wrapper MISSING) → create_project_token (EXISTS) → project add (EXISTS)
  ```

- The Manage token is **default-deny** (CLAUDE.md convention #12): never
  persisted, never a CLI argument, env var ignored without
  `--allow-env-manage-token`. An agent must never hold a privileged credential.
- [ADR 0002](../adr/0002-sec-09-config-privilege-separation.md) establishes the
  binding principle this RFC inherits: *a guard can only constrain a principal
  that lives in a different trust domain.* Quotas and spend caps are only real
  if enforced **outside** the agent's trust domain.

### What exists today (the Keboola platform side)

Verified against public sources (2026):

- Keboola **has** a public self-service signup and an **always-free tier with no
  credit card required**; billing is freemium / usage-based (free runtime
  minutes, then per-minute overage).
- There is **no public API for account or organization creation** — signup is a
  **web UI flow only**. This is the single most important constraint in this RFC.

### Verified Manage API surface (2026)

Confirmed from the official `keboola/kbc-manage-api-php-client` (`src/Client.php`):

- `createProject(int $organizationId, array $params)` → **`POST /manage/organizations/{orgId}/projects`**. The body is a free-form object (the PHP client passes `$params` straight through); from the UI we know **`name`** and a **project template/type** are the meaningful inputs, the rest optional (backend, data-retention, …). Requires an **org-admin Manage token** and an **already-existing `organizationId`**.
- `createProjectStorageToken(int $projectId, array $params)` → `POST /manage/projects/{id}/tokens` — exactly what our `create_project_token` already wraps.
- `giveProjectCredits(int $projectId, array $params)` → `POST /manage/projects/{id}/credits` — **a billing primitive already exists** (relevant to layer 4 / Phase 2).
- A marketplace-token path (`resolveMarketplaceToken` → project creation from an AWS/marketplace purchase token) exists in Keboola billing code — an existing precedent for "purchase → project" automation.

Net: **layer-1 project creation is a confirmed, thin wrapper**; the genuine gap for the *external* scenario is the **account/organization creation + free-tier-via-API + mandate** front door, which is *not* part of this Manage API surface.

## The problem is four independent layers, not one

| Layer | Question | External-scenario difficulty |
|---|---|---|
| 1. Provisioning | Technically create the project | 🟢 small once an org exists; 🔴 no API to create the *account/org* |
| 2. Root of trust | Who authorizes the agent to provision | 🟡 solvable via a sponsor-issued scoped credential |
| 3. Identity & legal | Who owns the account and accepts ToS | 🔴 hard ceiling — the agent is not a legal person |
| 4. Payment | Who pays, and how | 🔴 solvable only by delegated budget; never the agent itself |

The reason "agent creates and pays for a project with no humans" *feels*
impossible is that these four are usually conflated. Separated, three of them
have concrete answers and one is a genuine product/legal decision.

## Core realization: "no humans" means moving consent to a sponsor mandate

No 2026 payment framework (AP2, Stripe/OpenAI ACP, x402) grants an AI agent
legal personhood. In every model the **principal is a human or organization**;
the agent is an executor acting under a delegation. "No human in the loop" is
therefore achievable only in one precise sense:

> A **sponsor** (a legal person or organization) signs a **mandate once**,
> pre-authorizing the agent to onboard, provision, and spend within stated
> bounds. The agent then operates autonomously *inside* that mandate with no
> per-action human approval.

This is exactly Google AP2's **Intent Mandate** (the *Human-Not-Present* case):
the sponsor pre-approves conditions — budget ceiling, project count, allowed
operations, ToS acceptance on the agent's behalf — and the agent acts later
without the human present. It is *not* an agent acting with no principal at all;
that is neither legal nor desirable.

`★ The two facts that make this tractable`
1. Keboola's **always-free tier needs no card**, so **onboarding and payment are
   separable**. An agent can be fully onboarded onto a free project *before*
   payment is ever solved. "Pay over time" is a strictly later phase.
2. The decisive blocker is **not** in this repo. It is the **missing
   Keboola-side signup/onboarding API**. `kbagent` can build the entire client
   half, but the platform must expose an agent-onboarding endpoint first.

## Proposed architecture

### A. Onboarding (layers 1 + 2) — *requires a new Keboola platform API*

A new **Agent Onboarding API** on the Keboola platform (NOT in this repo) that
exchanges a signed sponsor mandate for a narrowly-scoped onboarding result:

```
agent ──(signed Intent Mandate)──▶  POST /agent-onboarding   (Keboola platform, NEW)
                                          │  verify mandate signature + sponsor identity
                                          │  enforce per-mandate project quota
                                          ▼
                                    free-tier project + scoped Storage token (no Manage token)
```

The token returned is a **project-scoped Storage token**, never a Manage token —
the agent gets exactly enough to work in its one project and nothing org-wide.
On the client side, `kbagent` adds a single thin command:

```
kbagent onboard --mandate <signed-mandate-file>
  → calls /agent-onboarding, then reuses the existing `project add` path to
    register the returned project + token in config.json
```

This keeps the [ADR 0001](../adr/0001-agent-office-product-boundary.md) boundary
intact: `kbagent` stays the local Keboola capability provider; onboarding is one
more governed capability, and the privileged half lives on the platform.

### B. Identity & legal (layer 3) — hard ceiling, handled by the mandate

The agent cannot be the legal owner. The mandate names the **sponsor** as the
account principal and carries the sponsor's ToS acceptance. The provisioned
project is *owned by the sponsor*; the agent holds delegated access only. This
does not remove the human — it **relocates** the human to a one-time signing
event outside the operational loop.

### C. Payment (layer 4) — phased, always a delegated budget

| Phase | Mechanism | Who pays / is liable | Maturity |
|---|---|---|---|
| 1 | **Free tier, no payment** | nobody — within free limits | 🟢 ships first |
| 2 | **Prepaid balance + auto-topup** bound to the sponsor's instrument | sponsor signs a recurring top-up mandate | 🟢 mature pattern |
| 3 | **x402 / AP2 rail** for usage-metered spend | sponsor's wallet/instrument under policy | 🟡 emerging, optional |

In the Keboola model, billing already aggregates to the Organization/Maintainer,
so "pay for the project" reduces to "the sponsor's billing account funds a
prepaid balance the agent draws down." The agent never holds the payment
instrument. Notably the Manage API already exposes `giveProjectCredits`
(`POST /manage/projects/{id}/credits`) and a marketplace-purchase → project path,
so Phase 2 builds on **existing** billing primitives rather than net-new surface.

### D. Trust model (inherited from ADR 0002)

Sponsor and agent are **two trust domains**. Every limit that matters is enforced
on the platform side, never in a CLI flag the agent can override.

| Principal | Holds | May do | Enforced by |
|---|---|---|---|
| Sponsor (legal person/org) | payment instrument, ToS acceptance, mandate signing key | set budget, project quota, revoke mandate | — (root of trust) |
| AI agent | a signed mandate + a project-scoped Storage token | onboard + operate inside one project, up to quota/spend cap | **Keboola platform** (quota, spend cap, mandate expiry) — *not* `kbagent` |

The CLI-side `--deny-writes` / `--deny-destructive` flags remain *guard rails
against agent mistakes*, exactly as ADR 0002 frames them — they are not the
enforcement boundary for an adversarial or runaway agent. The enforcement
boundary is the platform's per-mandate quota and spend cap.

## What would need to be built

**Keboola platform (outside this repo — product dependency):**
- Agent Onboarding API: mandate verification, sponsor-identity binding,
  per-mandate project quota, free-tier project creation, scoped-token issuance.
- Server-side spend cap + project quota enforcement tied to the mandate.
- Anti-abuse: rate limiting and sponsor-bound quotas (see Risks).

**`kbagent` (this repo):**
- `ManageClient.create_project()` + `OrgService.provision_project()` — the
  missing layer-1 wrapper. Useful *immediately* for the easier internal scenarios
  and as the test harness for everything here.
- `kbagent onboard --mandate ...` — client for the new platform API.
- Mandate handling (parse/verify a sponsor-signed Intent Mandate VC).

## Phasing

- **Phase 0 — `project create` wrapper.** Add `create_project` to `ManageClient`
  (`POST /manage/organizations/{id}/projects`, endpoint now confirmed) and a
  `kbagent org provision` command. Unlocks the internal/customer scenarios today
  and is the test substrate for the rest. *Fully inside this repo; no platform
  work.*
- **Phase 1 — free-tier mandate onboarding.** Requires the platform Agent
  Onboarding API + `kbagent onboard`. No payment. Delivers "agent onboards itself
  with no human in the loop" against the free tier.
- **Phase 2 — prepaid + auto-topup.** Sponsor-funded balance; agent spends within
  cap. Delivers "pays over time."
- **Phase 3 — x402 / AP2 rail.** Optional usage-metered autonomous payment.

## Open questions

1. **Will Keboola expose a public agent-onboarding front door?** Project creation
   *within an existing org* is a confirmed Manage API endpoint, but it needs an
   org-admin token and a pre-existing organization. **Account/organization
   creation and free-tier provisioning have no public API** (web-UI only), and
   there is no mandate-exchange endpoint. That front door is the gating product
   decision — without it the external scenario cannot exist in robust form.
2. **Own organization per agent, or a shared "agent org"?** Affects billing
   aggregation, isolation, and anti-abuse.
3. **Mandate format:** adopt AP2 Verifiable Credentials, or a Keboola-specific
   signed token? AP2 buys interoperability with the wider agent-payments
   ecosystem at the cost of early-stage maturity.
4. **Anti-abuse:** free tier + no card + programmatic onboarding = a project-farm
   incentive. What binds a mandate to a real, rate-limited sponsor?
5. **KYC for paid accounts:** at what spend threshold does a paid agent account
   require sponsor verification?

## Risks / Honest limits

- **The blocker is not in this repo.** Without a Keboola-side signup/onboarding
  API, the external scenario is blocked at a layer `kbagent` cannot reach. This
  RFC can deliver Phase 0 alone; Phases 1–3 are platform-gated.
- **The agent is never a legal person.** ToS acceptance, ownership, and liability
  always rest with the sponsor. "No humans" is "no humans *in the loop*," not "no
  humans *at all*."
- **Anti-abuse is first-class, not an afterthought.** A programmatic free-tier
  onboarding path is a spam/fraud magnet; it must ship *with* rate limiting and
  sponsor-bound quotas, not after.
- **Caps must be server-side.** Per ADR 0002, any quota or spend cap expressed as
  a CLI flag is friction, not enforcement — the agent shares the CLI's trust
  domain and can override it. Enforcement lives on the platform.

## Alternatives considered

### Automate the web signup flow (headless browser)

Drive the public signup UI with Playwright/Chrome. **Rejected.** Brittle against
UI changes, fights anti-bot defenses, and almost certainly violates ToS. It also
provides no mandate/audit trail and no clean payment delegation. This is the
first idea everyone has and it is a dead end.

### Crypto-only (x402 as the sole rail)

Give the agent a stablecoin wallet and pay per call. **Rejected as the sole
rail.** Narrow ecosystem, ignores the free-tier on-ramp, and solves none of the
identity/ToS problem. Kept as an *optional* Phase 3 rail.

### Internal-only provisioning

Only ever let agents provision inside an existing, already-billed org.
**Out of scope** — that is the easier scenario this RFC explicitly excludes,
though Phase 0 delivers exactly it as a byproduct.

## Related documents

- [ADR 0001 — Agent Office Product Boundary](../adr/0001-agent-office-product-boundary.md)
- [ADR 0002 — Config Privilege Separation](../adr/0002-sec-09-config-privilege-separation.md)
- CLAUDE.md convention #12 (Manage token default-deny)
- `OrgService.setup_organization` — the existing register-existing-projects path
