# RFC: Multi-Platform Extensions for kbagent (Microsoft Fabric + Databricks)

> Status: **Proposal / for discussion** — not yet implemented.
> Author: design exploration (Claude Code, max effort).
> Scope: how to make `kbagent` the single best interface to control and
> observe **Keboola + Microsoft Fabric + Databricks**, with the other
> platforms shipping as **toggleable extensions**, expanding both the CLI
> command surface and the `kbagent serve` HTTP surface, so that one app can
> show pipelines / failed jobs across all three systems in one place.

---

## 0. TL;DR (the distilled recommendation)

Build an **in-process `PlatformProvider` adapter**, selected by a `kind`
discriminator on a new `connections` config concept, with **optional pip
extras + lazy imports** for the heavy platform SDKs. Lead with a **unified,
normalized, READ-FIRST** model (`runs`, `pipelines`, `connections`) aligned to
the **OpenLineage** vocabulary, exposed through **one** set of `kbagent serve`
routers and **one** set of cross-platform CLI commands.

Explicitly **do not** build (YAGNI): a plugin discovery framework
(entry-points/gRPC/subprocess protocol), write-parity across platforms,
cross-platform lineage *stitching*, or an embedded metadata backend
(Marquez/OpenMetadata) — in v1.

This is community-pattern **(d)** + a slice of **(a)**. It is the only option
that satisfies the 5-Whys root need (a normalized cross-platform *operational
view*) while passing KISS/YAGNI for a small team.

**Decisions accepted (review, 2026-05):**

1. **Approach = Idea 2** — in-process `PlatformProvider` + `kind` discriminator
   + pip extras.
2. **v1 = read-only** — observability only (`connections` / `pipelines` /
   `runs`), no write parity.
3. **First external platform = Databricks** — official SDK lowers risk and
   validates the normalized envelope before paying Fabric's Entra-OAuth cost.

Still open: naming (DA-6), demand validation of the two named apps (DA-7),
explicit `ext enable/disable` vs. implicit, and the doc-sync budget (#17).

---

## 1. The ask (problem statement)

From the request, verbatim intent:

1. kbagent should become the **best interface** to control Keboola, Fabric,
   and Databricks.
2. The other platforms come as **extensions you turn on/off**.
3. It must **expand the scope of CLI commands** *and* of **HTTP methods**.
4. End goal: an **app that shows pipelines across all systems**, or **error
   jobs across all systems, in one place**.
5. Reuse the **single API we already have** via `kbagent serve`.

Two concrete "killer apps" are named: **cross-platform pipeline view** and
**cross-platform failed-job view**. Both are *observability* (read) use cases.

---

## 2. 5-Whys: what is the *real* need?

| # | Why? | Answer |
|---|------|--------|
| 1 | Why multi-platform in one CLI? | Data teams (and AI agents) operate across ≥2 platforms; context-switching between 3 consoles is the daily tax. |
| 2 | Why does cross-platform matter *specifically*? | The two named apps: "what pipelines exist" and "what failed", spanning all systems. |
| 3 | Why through `kbagent serve` (one HTTP API)? | So apps/agents are built once, not re-implementing per-platform auth, pagination, retries. |
| 4 | Why reuse kbagent rather than a new tool? | kbagent already owns auth, retry/backoff, the permission firewall, audit trail, multi-project parallelism, JSON mode, and AI-agent ergonomics (hints, `context`). Rebuilding that elsewhere is waste. |
| 5 | Why *toggleable* extensions? | Most users are Keboola-only; forcing Fabric/Databricks SDKs and surface area on them is dependency bloat + larger blast radius + a confused AI agent. |

**Root need (distilled):**

> A **unified, normalized, read-first operational view** (jobs/runs +
> pipelines) across data platforms, exposed through the **existing**
> `kbagent serve` API and CLI, **without bloating the Keboola-only path**.

**The single most important consequence:** the primary value is the
**unified READ model (observability)**, *not* write parity. Triggering a
Databricks job or a Fabric pipeline from kbagent is a *nice-to-have*; seeing
all failures in one place is the *reason*. This reframing is what makes the
project tractable. (See Devil's Advocate §7.)

---

## 3. Current architecture — where the seams already are

Grounded in the code (not assumptions):

### 3.1 The three layers already *are* an adapter pattern

```
CLI Commands (commands/)  -->  Services (services/)  -->  HTTP Clients
  Typer, output                 Business logic            client.py / manage_client.py / ai_client.py
```

- `BaseService` (`services/base.py:49`) takes `config_store` +
  `client_factory: Callable[[str, str], KeboolaClient]` via DI, and offers
  `_run_parallel(projects, worker_fn)` (`services/base.py:115`) — a
  ThreadPoolExecutor scaffold with **per-project error accumulation**
  (one project failing does not stop others).
- Commands are thin: parse args → call a service → format output. They never
  touch HTTP.

**Implication:** we generalize the existing seam to a `PlatformProvider`; we
do **not** invent a new architecture.

### 3.2 `BaseHttpClient` is already generic enough

- `BaseHttpClient.__init__(base_url, token, headers, timeout)`
  (`http_base.py:42`) — base URL + arbitrary headers + shared
  retry/backoff (429/5xx, exponential) + error mapping with token masking.
- The only Keboola-specific bit is the static helper
  `_derive_service_url(stack_url, "queue")` (`http_base.py:64`) which rewrites
  `connection.` → `<prefix>.`. A `FabricClient`/`DatabricksClient` simply
  *won't call it*; they pass their own `base_url` + auth header.

**Implication:** new platform clients are `BaseHttpClient` subclasses and get
retry/backoff/masking for free. Fabric (no official SDK) especially benefits.

### 3.3 Config is Keboola-coupled — but versioned for migration

- `ProjectConfig` (`models.py:8`) = `stack_url` + `token` (+ ids), with an
  HTTPS-only validator. **No `provider`/`kind` field.**
- `AppConfig` (`models.py:79`) = `projects: dict[str, ProjectConfig]`, and
  crucially `version: int = 1` *"for future migrations"*.
- The config file is `0600`, atomic writes; `mask_token()` everywhere.
- The stored `CLAUDE_CONFIG_WARNING` literally says *"THESE ARE KEBOOLA
  STORAGE API TOKENS"* — a sign of how Keboola-centric the security model is
  today (see Devil's Advocate §7).

**Implication:** the clean extension point is a **`connections` map with a
discriminated union on `kind`**, gated by `AppConfig.version` bump + migration.

### 3.4 `serve` = FastAPI with **hand-written, hardcoded** routers

- `create_app(...)` builds a FastAPI app and calls `app.include_router(...)`
  ~30 times, one per service router in `server/routers/`. **No route is
  auto-generated from CLI commands.**
- Auth = a bearer token (`KBAGENT_SERVE_TOKEN`, generated at startup).
- `kbagent http get/post/...` (`commands/http_client.py`) is a thin client
  that forwards to the running serve using `KBAGENT_SERVE_URL` +
  `KBAGENT_SERVE_TOKEN`.

**Implication (and a trap):** every new endpoint is hand-written. If we add
per-platform write endpoints, the surface multiplies by platform. So serve
should expose **one normalized router set** (`/runs`, `/pipelines`,
`/connections`) backed by the aggregator — not N routers per platform.

### 3.5 There is **no** plugin/extension mechanism in the CLI core

- Hardcoded `app.add_typer(...)` in `cli.py`; no `entry_points`, no dynamic
  import, no feature-flag toggles.
- The only "toggle by install" precedent is the `[project.optional-dependencies]
  server = [...]` extra (FastAPI/uvicorn). `pip install keboola-agent-cli[server]`.

**Implication:** "extension" must be introduced from scratch. Good news: we get
to pick the *simplest* mechanism rather than inherit a heavy one. The `[server]`
extra is the precedent to copy.

### 3.6 MCP layer is protocol-generic (relevant to Idea 5)

- `McpService` wraps `keboola-mcp-server` as a subprocess (stdio or persistent
  HTTP), with a generic tool-list/tool-call surface and per-request credentials
  via headers (`X-Storage-Token`, `X-Storage-API-URL`, `X-Branch-ID`). It
  assumes a single stack/token pair per request today.

---

## 4. New platforms — API surface (research summary)

### 4.1 Databricks — the *easy* one (official SDK exists)

- **Auth:** PAT (`Authorization: Bearer dapi…`) or OAuth M2M / service
  principals; `DATABRICKS_HOST` + `DATABRICKS_TOKEN`; reuses `~/.databrickscfg`
  profiles. Account-level APIs use a separate `accounts.*` host.
- **Jobs (use 2.2):** `GET /api/2.2/jobs/list`, `GET /api/2.2/jobs/runs/list`,
  `GET /api/2.2/jobs/runs/get?run_id=…`, `POST /api/2.2/jobs/run-now`.
  Run output: `GET /api/2.1/jobs/runs/get-output?run_id=<task_run_id>` (must use
  the **task** run id for multi-task jobs).
- **Run state:** `state.life_cycle_state` (PENDING/RUNNING/TERMINATED/…) +
  `state.result_state` (SUCCESS/FAILED/TIMEDOUT/CANCELED) + `state.state_message`.
- **Clusters:** `GET /api/2.0/clusters/list`. **Workspaces (account):**
  `GET /api/2.0/accounts/<id>/workspaces`.
- **Lineage:** UC lineage REST `GET /api/2.0/unity-catalog/lineage/…` and/or
  `system.access.lineage` via the SQL Statement Execution API.
- **Official tooling:** `databricks-sdk` (Python) covers Jobs/Clusters/UC/SQL +
  an `AccountsClient`; Databricks CLI v1 is built on it.

→ **`DatabricksProvider` can be a thin wrapper over `databricks-sdk`.**

### 4.2 Microsoft Fabric — the *hard* one (auth + no SDK)

- **Auth:** Microsoft Entra ID. Service principal via client-credentials grant
  against `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`,
  scope `https://api.fabric.microsoft.com/.default`, used as
  `Authorization: Bearer <token>`. **Tokens expire** → we own acquisition +
  refresh + caching. (kbagent today only handles static, non-expiring tokens.)
- **Base:** `https://api.fabric.microsoft.com/v1`.
- **Discovery:** `GET /v1/workspaces`, `GET /v1/workspaces/{ws}/items`
  (notebooks, pipelines, lakehouses, …).
- **Monitoring (the unified surface):**
  `GET /v1/workspaces/{ws}/items/{item}/jobs/instances` ("item job instances" /
  job scheduler) — run history + state + failure info.
- **Caveat:** Data Factory **pipeline** run metadata in the public REST API is
  *less complete* than the Monitoring Hub UI. Design for "list runs / latest /
  status / failure-reason-when-present", not rich pipeline internals.
- **Official tooling:** **No** single official full Python SDK, **no** single
  official Fabric CLI covering the whole REST surface. We do raw REST (via
  `BaseHttpClient`) + token acquisition (hand-rolled OAuth or `azure-identity`).

→ **`FabricProvider` = `BaseHttpClient` subclass + an `EntraTokenProvider`.**
The auth/refresh is the genuine engineering cost here, not the REST calls.

### 4.3 The normalization that matters

The only normalization with high value-per-line is the **run status enum** and
a **deep-link** back to each platform's console. Everything else can be a
`raw` passthrough. See §10.

---

## 5. How the community solves "pluggable multi-platform" (research summary)

| Pattern | Real-world examples | Isolation | Dep bloat | Complexity (small team) | Multi-language |
|--------|---------------------|-----------|-----------|--------------------------|----------------|
| **(a) entry-points + pip extras** | dbt adapters, Airflow providers, pytest/Datasette plugins | none (in-proc) | per-extra, opt-in | low–medium | no |
| **(b) PATH subprocess** | `gh`/`kubectl`/`git`/`helm` plugins | strong | isolated per binary | medium–high (ad-hoc protocol, distribution) | yes |
| **(c) gRPC / separate binary** | Terraform providers, Steampipe, Vault | strong + typed | isolated | **high** (protobuf, lifecycle, versioning) | yes |
| **(d) in-process adapter behind a `provider` config field** | dbt's *user-facing* `type:`; Great Expectations stores; boto3 client factory | none (in-proc) | one env (mitigate via extras) | **low** | no |

**Cross-system observability/lineage standards:**

- **OpenLineage (+ Marquez):** open standard for **Job / Run / Dataset** events;
  native integrations for Airflow, dbt, Spark, Flink, Dagster. → **Adopt as the
  vocabulary** for our normalized model and serve schema. Optionally *emit*
  events later; do **not** embed Marquez.
- **OpenMetadata:** heavyweight metadata platform. → Possible future *export
  target*, not a dependency.
- **OpenTelemetry:** for telemetry of *our tool* (traces/metrics of the API
  calls), not the data model. → Optional, later.

**Community verdict for a small team (KISS/YAGNI):** pattern **(d)** as the
core, optional deps via **extras** (a slice of **(a)**), and defer entry-points
discovery until external third-party providers actually exist.

---

## 6. Five candidate approaches

Each is scored against the root need (§2) and KISS/YAGNI.

### Idea 1 — Parallel per-platform stacks
Add `kbagent databricks …` and `kbagent fabric …` command groups, each with its
own services, clients, and `serve` routers — a vertical clone of the Keboola
stack per platform.

- ➕ Conceptually simple per platform; no shared abstraction to design.
- ➖ **Does not deliver the killer apps**: there is no single `/runs` or
  `runs --status error` across platforms — the user would still aggregate three
  shapes by hand. Serve routers multiply by platform (§3.4 trap). Massive
  doc-sync burden (convention #17 × 3).
- **Verdict:** fails the root need. This is the "obvious" path that misses the
  point (the value is the *union*, not three silos).

### Idea 2 — In-process `PlatformProvider` adapter + `kind` discriminator + extras  ✅ RECOMMENDED
One `PlatformProvider` Protocol with normalized read methods; built-in
implementations (Keboola/Databricks/Fabric) selected by a connection's `kind`;
an aggregator fans out across enabled connections; **unified** commands
(`runs`, `pipelines`, `connections`) and **one** normalized serve router set;
heavy SDKs behind pip extras with lazy imports.

- ➕ Directly produces `runs --status error` and `pipelines` across all
  platforms — the two named apps. One serve router set. One CLI surface. Easy
  to unit-test each provider. Extras keep the Keboola-only path lean.
- ➖ Providers live in one repo/process (discipline on module boundaries
  needed); a 4th-party provider needs a repo PR until/unless we add
  entry-points later (a non-breaking add).
- **Verdict:** the KISS/YAGNI winner. Matches community pattern (d)+extras.

### Idea 3 — Entry-points plugin framework
Define a `keboola_agent_cli.providers` entry-point group; `kbagent[fabric]`
ships a separate package that *registers* a provider; core discovers providers
from the environment (dbt-adapter style).

- ➕ Third parties can ship providers without repo access; provider versions
  decouple from core.
- ➖ We have **exactly three, all first-party** providers. Discovery machinery,
  a compatibility policy, and "unknown plugin version" handling are
  **speculative generality**. Same in-process isolation as Idea 2 but more
  moving parts.
- **Verdict:** YAGNI *now*. It is a **strict superset** of Idea 2 — and Idea 2
  can grow into it later by adding discovery to the provider factory **without
  an architectural rewrite**. So: build Idea 2, keep Idea 3 as a documented
  future seam.

### Idea 4 — Subprocess / CLI-wrapping extensions
kbagent shells out to the official `databricks` CLI and a Fabric helper as
PATH plugins (`kbagent-databricks`, gh-extension style), exchanging JSON.

- ➕ Strong process isolation; language-agnostic; no SDKs in core env.
- ➖ We must invent an argv/JSON/stdio protocol and a version handshake;
  distribution UX is worse (users install separate binaries); and crucially
  **building one unified `serve` HTTP API on top of subprocesses is awkward**
  (proxy/latency) — directly at odds with goal #5. Fabric has no such CLI to
  wrap anyway.
- **Verdict:** complexity without payoff for in-house providers. Reject.

### Idea 5 — MCP federation
Don't add HTTP clients at all. Register each platform's MCP server (Keboola
already; Databricks has an official MCP server; Fabric MCP is emerging) and let
the existing MCP layer federate tools. Unified surface = MCP tools.

- ➕ Reuses the existing generic MCP machinery; minimal new client code; great
  for *ad-hoc / write* operations and for LLM-driven exploration.
- ➖ Yields a **heterogeneous bag of tools, not a normalized `/runs?status=error`**
  — so it does **not** deliver the named apps cleanly, and building a polished
  cross-platform app on a tool-bag is harder than on a normalized REST. Fabric
  MCP is immature. Credential plumbing per platform is still needed. MCP
  per-request creds assume a single stack/token (§3.6) and need rework for
  multi-platform.
- **Verdict:** **complementary, not core.** Adopt as an *optional later track*
  for write/ad-hoc ops (low effort, high LLM value) — but the unified
  observability model must be the normalized provider layer (Idea 2).

### Idea 6 — Buy, don't build (point kbagent at an existing meta-tool)
Stand up OpenLineage+Marquez (or OpenMetadata, or Steampipe) and have kbagent
read the unified view from it.

- ➕ No normalization code; mature lineage models.
- ➖ Heavy infra to deploy/operate; ingestion still needs per-platform
  collectors (which is the work we were trying to avoid); contradicts "one CLI /
  one serve API" — now there's a second system to run.
- **Verdict:** over-weight for a small team and the stated goal. Borrow
  OpenLineage's *vocabulary* (free, §10), skip its *infrastructure*.

---

## 7. Devil's advocate

**DA-1 — Is kbagent even the right home?** Its identity, security warning
("THESE ARE KEBOOLA STORAGE API TOKENS", §3.3), and the `keboola-expert`
subagent are Keboola-centric. Multi-platform risks **diluting the product** and
confusing the AI agent's tool selection.
→ *Mitigation:* strict namespacing (`connections` with `kind`; provider
modules under `providers/`); the read-only normalized surface is additive and
clearly labeled per-platform. The reuse of auth/retry/firewall/serve is real
and large. Net: worth it, **if** we keep providers walled off and don't let
"Keboola" assumptions leak into the shared layer. (Counts as a hard design
constraint, not just a worry.)

**DA-2 — The unified schema is a leaky abstraction (the LCD trap).** Keboola
jobs (component+config+row), Databricks multi-task runs (task-level outputs),
and Fabric item job instances have genuinely different semantics. A
lowest-common-denominator `Run` is either too thin to be useful or too lossy.
→ *Mitigation:* normalize **only the envelope** (`id, platform, pipeline,
status-enum, started/ended, duration, error_summary, deep_link`) and carry a
`raw` passthrough for platform specifics. This is exactly OpenLineage's bet
(Run/Job + facets). And **read-only** — we never try to unify *write*
semantics, which is where abstractions truly break. If even the envelope proves
lossy in practice, that is the signal to stop at "federated but not unified".

**DA-3 — The real cost is auth, not REST.** Keboola = static token. Databricks
= PAT/OAuth + host (+ account host). **Fabric = Entra ID service principal with
token expiry/refresh** — machinery kbagent has never needed. Most of the
schedule risk is here.
→ *Mitigation:* lean on `databricks-sdk` (auth solved) and treat
`FabricProvider`'s `EntraTokenProvider` as the single riskiest unit — build and
test it first in Phase 2, behind the extra. Be honest in estimates: Fabric ≈
2× Databricks effort.

**DA-4 — serve router explosion.** Hand-written routers (§3.4) × per-platform
writes = a maintenance bomb, and every command also hits ~7 doc-sync surfaces
(convention #17).
→ *Mitigation:* serve exposes **one** normalized router set; **no** write
parity in v1. This is both KISS and a direct match to the named apps.

**DA-5 — Cross-platform lineage is a trap.** kbagent's existing deep lineage is
Keboola-only and non-trivial. *Stitching* Keboola→Databricks→Fabric lineage is
a research project.
→ *Mitigation:* v1 lineage = **per-platform only** (or omitted). Adopt the
OpenLineage vocabulary so stitching is *possible* later, but treat stitching as
YAGNI-until-proven (likely never for the named apps).

**DA-6 — Naming.** "kbagent" = *Keboola* agent. A multi-platform tool with a
single-vendor name is odd.
→ *Out of scope for the technical design; flagged for the product owner.* Does
not block; an alias/rename can come later.

**DA-7 — Steelman of "do nothing / use vendor consoles".** Each platform has a
capable UI + CLI/SDK already. Why a meta-layer?
→ The answer is *only* the union: "all failures, one place" and "all pipelines,
one place", scriptable via one HTTP API for agents. If the user does not
actually live across platforms daily, the whole premise weakens — **validate
the two named apps are real recurring needs before Phase 1.**

---

## 8. Distilled recommendation (KISS / YAGNI applied)

**Adopt Idea 2**, read-first, with these hard constraints:

1. **Read-first.** v1 = `connections`, `pipelines`, `runs` (incl.
   `runs --status error`). Writes are a later, opt-in track.
2. **Normalize the envelope, keep `raw`.** Status enum + deep-link are the only
   high-value normalizations. Vocabulary aligned to **OpenLineage**.
3. **One serve router set, one CLI surface.** No per-platform router/command
   silos in v1.
4. **Optional deps via extras + lazy import + fail-fast.**
   `keboola-agent-cli[databricks]`, `[fabric]`, `[all]`. Missing extra →
   explicit "install X" error (matches the repo's "no silent defaults" rule).
5. **Don't build** (YAGNI): entry-points discovery (Idea 3), subprocess/gRPC
   (Idea 4/c), lineage stitching, write parity, embedded metadata backend
   (Idea 6). Keep Idea 5 (MCP federation) as a *complementary* later track for
   write/ad-hoc ops.
6. **Provider isolation is a design rule** (DA-1): no Keboola assumption leaks
   into the shared aggregator/serve layer.

Why it passes the gates:
- **KISS:** one interface (`PlatformProvider`), one factory, one aggregator,
  one router set. No IPC, no protocol, no discovery.
- **YAGNI:** exactly the surface the two named apps need; everything
  speculative is deferred behind documented, non-breaking seams.
- **5-Whys:** delivers the *union read view* (the root need) via the *existing
  serve API* (goal #5) without taxing Keboola-only users (toggle via extras).

---

## 9. Concrete design

### 9.1 Config — `connections` with a discriminated union

Introduce a `connections` map (alias → connection) discriminated on `kind`.
Bump `AppConfig.version` to `2` and migrate existing `projects` →
`connections[*] (kind="keboola")` on load (keep `projects` readable for
back-compat / rollback during a deprecation window).

```python
# models.py (sketch)
from typing import Literal, Annotated
from pydantic import BaseModel, Field

class KeboolaConnection(BaseModel):
    kind: Literal["keboola"] = "keboola"
    stack_url: str                    # existing https validator
    token: str                        # masked, 0600
    # … existing ProjectConfig fields (project_id, active_branch_id, org_*)

class DatabricksConnection(BaseModel):
    kind: Literal["databricks"] = "databricks"
    host: str                         # https://<workspace-host>
    token: str | None = None          # PAT  (or…)
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None
    account_id: str | None = None     # for account-level workspace listing

class FabricConnection(BaseModel):
    kind: Literal["fabric"] = "fabric"
    tenant_id: str
    client_id: str
    client_secret: str                # masked; token acquired+cached at runtime
    # scope defaults to https://api.fabric.microsoft.com/.default

Connection = Annotated[
    KeboolaConnection | DatabricksConnection | FabricConnection,
    Field(discriminator="kind"),
]

class AppConfig(BaseModel):
    version: int = 2
    default_connection: str = ""
    connections: dict[str, Connection] = Field(default_factory=dict)
    extensions: dict[str, bool] = Field(default_factory=dict)  # explicit on/off
    # … existing fields (max_parallel_workers, permissions)
    projects: dict[str, "KeboolaConnection"] = Field(default_factory=dict)  # legacy, read-only
```

**Toggle semantics (the "turn on/off" requirement):** an extension `kind` is
*active* when **(a)** its extra is installed **and** **(b)**
`extensions[kind] is True` (default `True` for `keboola`, `False` otherwise).
Surface it as `kbagent ext list | enable <kind> | disable <kind>`. This gives
the explicit on/off the request asks for, while extras give the dependency
isolation.

### 9.2 Layer 3 — platform clients

- `DatabricksClient` — thin wrapper over `databricks-sdk` (`WorkspaceClient`),
  **lazy-imported**. Optionally reuse `~/.databrickscfg`.
- `FabricClient(BaseHttpClient)` — `base_url=https://api.fabric.microsoft.com/v1`,
  injects `Authorization: Bearer <token>` from an `EntraTokenProvider`
  (client-credentials grant, token cached until expiry, refresh on 401). Reuses
  the inherited retry/backoff/error mapping.

### 9.3 Layer 2 — `PlatformProvider` Protocol + providers + aggregator

```python
# services/providers/base.py (sketch)
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

class RunStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    RUNNING = "running"
    CANCELED = "canceled"
    WAITING = "waiting"

@dataclass(frozen=True)               # dataclasses, not tuples (CONTRIBUTING)
class Pipeline:
    connection: str; platform: str; id: str; name: str; deep_link: str

@dataclass(frozen=True)
class Run:
    connection: str; platform: str; id: str
    pipeline_id: str; pipeline_name: str
    status: RunStatus
    started_at: str | None; ended_at: str | None; duration_s: float | None
    error_summary: str | None; deep_link: str
    raw: dict                          # platform-specific passthrough

class PlatformProvider(Protocol):
    platform: str
    def list_pipelines(self) -> list[Pipeline]: ...
    def list_runs(self, *, status: RunStatus | None = None, limit: int | None = None) -> list[Run]: ...
    def get_run(self, run_id: str) -> Run: ...
```

- `KeboolaProvider` — wraps the **existing** `JobService` / flow logic (proves
  the seam with **zero new platform** in Phase 0).
- `DatabricksProvider`, `FabricProvider` — Phases 1 & 2.
- `MultiPlatformService` — resolves *active* connections, builds a provider per
  connection via a `provider_factory` (its **own** factory, not the
  Keboola-typed `client_factory`), fans out in parallel reusing the
  `_run_parallel` shape, **accumulates per-connection errors** (a Fabric auth
  failure must not hide Databricks results), and merges into `list[Run]` /
  `list[Pipeline]`. Built as a *new* small aggregator — do **not** retrofit
  `BaseService`'s `ProjectConfig`/`KeboolaClient` generics in Phase 0.

### 9.4 Layer 1 — unified commands

```
kbagent connections list
kbagent pipelines [--platform keboola|databricks|fabric] [--connection ALIAS]
kbagent runs [--status error|running|success|…] [--platform …] [--connection …] [--limit N]
kbagent ext list | enable <kind> | disable <kind>
```

Per-platform escape hatches (`kbagent databricks …`) are added **only if** a
real need appears (YAGNI). Each new command must satisfy the repo's E2E rule
(#16) and the doc-sync surfaces (#17).

### 9.5 serve — one normalized router set

Add `server/routers/platforms.py`:
`GET /connections`, `GET /pipelines`, `GET /runs?status=error&platform=…`.
Backed by `MultiPlatformService`, inheriting the existing bearer-token auth.
**Payoff:** an agent inside a scheduled task does
`kbagent http get "/runs?status=error"` and gets failures across all platforms
from the one API — exactly goal #5. (Optional later: `GET /openlineage/events`.)

### 9.6 Extras + lazy import + fail-fast

```toml
[project.optional-dependencies]
databricks = ["databricks-sdk>=0.30"]
fabric     = ["azure-identity>=1.17"]   # token acquisition; REST via httpx
all        = ["keboola-agent-cli[databricks,fabric]"]
```

```python
def _require_databricks():
    try:
        from databricks.sdk import WorkspaceClient  # noqa: F401
    except ImportError as e:
        raise ConfigError(
            "Databricks support needs the extra: pip install keboola-agent-cli[databricks]"
        ) from e
```

---

## 10. Normalized model + OpenLineage alignment

**Status mapping (the core normalization):**

| Normalized | Keboola (Queue) | Databricks (`result_state`/`life_cycle_state`) | Fabric (job instance status) |
|------------|-----------------|-----------------------------------------------|------------------------------|
| `success`  | `success`       | `SUCCESS`                                     | `Completed` |
| `error`    | `error`/`warning` | `FAILED`/`TIMEDOUT`/`INTERNAL_ERROR`        | `Failed` |
| `running`  | `processing`    | `RUNNING`/`PENDING`/`TERMINATING`             | `InProgress` |
| `canceled` | `terminated`/`cancelled` | `CANCELED`                           | `Cancelled` |
| `waiting`  | `waiting`/`created` | (queued)                                  | `NotStarted` |

Map `Run`/`Pipeline` to **OpenLineage** `RunEvent` (eventType, run.runId,
job.namespace+job.name, inputs/outputs) shapes in the serve schema; keep `raw`
for everything platform-specific. This buys interoperability (Airflow/dbt/Spark
already speak it) at zero infra cost. Emitting events to a backend = later/opt.

---

## 11. Phased plan (ship value early, de-risk the abstraction first)

**Phase 0 — Build the seam (no new platform).**
`connections` config + migration (`version`→2), `PlatformProvider` Protocol +
normalized models, refactor existing Keboola job/flow *read* into
`KeboolaProvider`, `MultiPlatformService` aggregator, unified `connections` /
`pipelines` / `runs` commands + `ext` toggle, serve `platforms` router — all
over **Keboola only**. *De-risks the abstraction before any vendor work.*

**Phase 1 — Databricks (read).** `[databricks]` extra, `DatabricksClient`
(sdk), `DatabricksProvider`. `runs --status error` now spans Keboola+Databricks.
Validate the envelope is actually useful on two real platforms.

**Phase 2 — Fabric (read).** `[fabric]` extra, `EntraTokenProvider` (build +
test first — the riskiest unit), `FabricClient`, `FabricProvider`. Three
platforms in one view.

**Phase 3 — opt-in, YAGNI-gated.** Any of: trigger runs (write); MCP federation
(Idea 5) for ad-hoc ops; OpenLineage event emission; per-platform escape-hatch
commands. Build only on demonstrated demand.

Every phase: unit tests + E2E (#16) + the full doc-sync (#17) +
`changelog.py` entry + `make version-sync` + `(since vX.Y.Z)` tags.

---

## 12. Risks & open decisions (for the product owner)

> Headline items #1 and #3 (and the overall approach) were **accepted** — see §0.

1. **Read-only v1, or read+write?** ✅ **Decided: read-only.**
2. **Command UX:** lead with unified `runs`/`pipelines` (recommended) vs.
   per-platform groups.
3. **Which platform first?** ✅ **Decided: Databricks** (official SDK, lower
   risk — validate the abstraction before paying Fabric's auth cost).
4. **Explicit `ext enable/disable`** vs. "configured connection = enabled".
   (Recommendation: explicit, since the request says "turn on/off".)
5. **Naming** (DA-6): does "kbagent" stay multi-platform-branded?
6. **Validate demand** (DA-7): are the two named apps real recurring needs?
7. **Doc-sync tax** (convention #17): each new command touches ~7
   silent-drift surfaces — budget for it.

## 13. Rough effort

- **Phase 0:** small–medium (mostly refactor + new config plumbing + tests).
- **Phase 1:** medium (sdk wrapper + mapping + tests + E2E).
- **Phase 2:** medium–large (Entra OAuth/refresh is the long pole).
- **Phase 3:** scoped per opt-in item.
