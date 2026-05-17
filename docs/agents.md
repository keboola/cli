# Agent Teams PRD

## Summary

`kbagent serve` should evolve from a single-agent task scheduler into a
local **Agent Office**: a runtime where users compose teams of AI agents,
give them governed access to Keboola data and business tools, and watch
their work progress through plans, artifacts, approvals, and shipped
outputs.

The current `Agent Tasks` feature is the right foundation: it already
persists scheduled tasks, runs local AI CLIs (`claude`, `codex`, `gemini`),
streams events, records costs, and injects the live `kbagent serve` API
back into subprocesses. The next step is to add team-level orchestration,
a central tool registry, shared artifacts, and native templates for data
teams and broader business roles.

## Problem

Today an `AgentTask` is one action:

- `mcp_tool`: call one Keboola MCP tool.
- `cli_command`: run one `kbagent` command.
- `ai_agent`: run one local AI CLI with one prompt.

This is useful for scheduled checks and single-purpose assistants, but it
does not support complex business workflows:

- No first-class team, role, responsibility, or ownership model.
- No shared workspace where agents can exchange plans, requirements,
  decisions, SQL, documents, patches, and review notes.
- No central view of which tools are available through `kbagent`, MCP,
  local AI CLIs, or business integrations.
- No dependency graph, fan-out, fan-in, review step, or blocking state.
- No approval queue for risky writes, external messages, or destructive
  operations.
- No reusable templates for project management, customer support,
  finance, sales operations, compliance, marketing, or engineering
  delivery.

The result is that the product can run agents, but it cannot yet run
agentic teams.

## Goals

- Make `kbagent serve` the local agent operating system for Keboola and
  business workflows.
- Let users create teams, not only individual scheduled tasks.
- Discover and expose the tools available to local AI CLIs and configured
  MCP servers through one governed Tool Broker.
- Provide native team templates for both Keboola/data use cases and
  general business roles.
- Keep local control: the user's local machine, local AI CLI credentials,
  and local config remain the execution boundary.
- Keep the core `kbagent` CLI useful without Agent Office. Agent Office
  is an optional orchestration layer built on stable `kbagent serve` APIs.
- Provide strong observability: every plan, tool call, artifact, blocker,
  approval, cost, and final output is auditable.
- Make write operations safe by default with policy, risk classification,
  dry-run support, and human approvals.

## Non-Goals

- Do not build a hosted SaaS control plane in the first version.
- Do not replace Slack, Teams, Jira, GitHub, Linear, Google Workspace, or
  Microsoft 365. Integrate with them as tools.
- Do not allow autonomous destructive actions without explicit user policy.
- Do not assume every user has the same AI CLI or MCP setup.
- Do not require a specific AI vendor. Claude, Codex, Gemini, and future
  local agents should be interchangeable behind adapters.
- Do not require a separate OS, container, VM, or cloud sandbox for MVP.
  Agents execute through the user's local AI CLI and inherit that local
  tool's native permission model.
- Do not put Slack, Teams, Jira, Office, CRM, or other business-tool
  SDKs into the core CLI dependency path. Those integrations should be
  optional providers.

## Product Boundary

Decision record: [ADR 0001: Agent Office Product Boundary](adr/0001-agent-office-product-boundary.md).

The right product shape is a hybrid:

- `kbagent` remains the local Keboola runtime: CLI commands, config,
  project tokens, permissions, `kbagent serve`, Keboola REST/MCP access,
  Tool Broker primitives, audit, approvals, and simple Agent Tasks.
- Agent Office is a higher-level orchestration layer: teams, roles,
  orchestrator loops, business templates, external stakeholder workflows,
  shared artifacts, and the office-style UI.

For MVP, Agent Office can live in this repository because `kbagent serve`
already has the essential pieces: FastAPI, React UI, SSE streaming, local
AI CLI execution, run history, and Keboola context. Splitting too early
would slow iteration and make the one-command personal workflow worse.

The implementation should still be designed as if Agent Office might be
extracted later:

- Core `kbagent` must remain useful when Agent Office is disabled or not
  installed.
- Agent Office should consume `kbagent serve` through stable APIs rather
  than reaching into CLI internals.
- Business integrations should enter through provider interfaces, MCP,
  or custom tool definitions, not core dependencies.
- Team templates should be data-driven where practical, so they can move
  to a separate package or marketplace later.
- The default personal experience should stay simple: `kbagent serve --ui`
  can expose Agent Office when the optional feature is available.

Recommended packaging path:

1. Build Tool Broker and policy primitives inside `kbagent`.
2. Build Agent Teams MVP in this repo behind an optional extra, feature
   flag, or clearly isolated module.
3. Keep business integrations optional and provider-based.
4. Extract `agent-office` into a separate package/app only after templates,
   UI, and integrations outgrow the core Keboola workflow.

Architectural rule:

> `kbagent serve` is the local Keboola capability provider and governed
> tool host. Agent Office is an optional orchestration layer built on top
> of it. Core `kbagent` must remain useful without Agent Office, and
> Agent Office must consume `kbagent` through stable APIs rather than
> CLI internals.

## Personas

| Persona | Need |
|---|---|
| Data engineer | Delegate investigation, SQL, lineage, job triage, and config work across projects. |
| Analytics lead | Ask a team to produce a trustworthy data product, report, or semantic layer change. |
| Project manager | Gather stakeholder input, summarize requirements, create tickets, and track delivery. |
| Operations lead | Monitor recurring business processes and escalate anomalies. |
| Support lead | Triage customer issues, collect context, draft replies, and route fixes. |
| Revenue operator | Analyze pipeline, produce account follow-ups, and coordinate sales actions. |
| Finance operator | Reconcile data, investigate variances, and prepare approval-ready summaries. |
| Compliance owner | Audit access, evidence, policy drift, and exception handling. |
| Executive sponsor | Request an outcome and inspect progress without reading raw logs. |

## Product Concept

### Agent Office

The Agent Office is the user-facing metaphor and the execution dashboard.
It should not be only decorative. Each room represents a real run state:

| Room | Runtime meaning |
|---|---|
| Open Floor | Parallel work items in progress. |
| Meeting Room | Handoffs, clarification, review, and decision points. |
| Whiteboard | Planning, decomposition, architecture, and synthesis. |
| Inspector | QA, reviewer, evaluator, policy checker. |
| Ops Room | Scheduled monitors, running jobs, external integrations. |
| Archive | Completed outputs and reusable artifacts. |

The right panel should stay operational: roster, current work, blockers,
tool calls, cost, progress, artifacts, approvals, and final outputs.

### Team

A Team is a reusable definition:

- Goal and default operating mode.
- Roster of agent roles.
- Allowed tool groups.
- Policies and approval rules.
- Templates for artifacts and final outputs.
- Default planning strategy.
- Optional schedule or manual trigger.

### Team Run

A Team Run is one execution:

- User objective and runtime input.
- Orchestrator plan.
- Work item DAG.
- Agent assignments.
- Shared artifacts.
- Event timeline.
- Tool call audit log.
- Approval queue.
- Final report and shipped outputs.

### Agent Role

An Agent Role is not just a prompt. It includes:

- Name, responsibility, and success criteria.
- AI CLI/model preference.
- Tool permissions.
- Autonomy level.
- Required inputs.
- Expected artifacts.
- Review rules.
- Escalation behavior.

### Work Item

A Work Item is a unit of team execution:

- Title and objective.
- Assigned role.
- Dependencies.
- Status: queued, running, blocked, waiting_for_approval, reviewing,
  done, failed, cancelled.
- Progress and confidence.
- Linked artifacts.
- Tool calls.
- Handoff notes.

### Artifact

Artifacts are the shared blackboard. Examples:

- `brief.md`
- `plan.md`
- `requirements.md`
- `questions-for-stakeholders.md`
- `decisions.json`
- `findings/*.md`
- `queries/*.sql`
- `patches/*.diff`
- `reports/*.md`
- `slides-outline.md`
- `handoffs/*.json`
- `review-comments.json`

Agents should communicate through artifacts, not only through stdout.

## Tool Broker

The Tool Broker is the key platform layer. It gives every agent a
consistent, governed way to discover and call tools, regardless of which
AI CLI is running.

### Tool Providers

| Provider | Examples |
|---|---|
| `kbagent_api` | REST endpoints exposed by `kbagent serve`. |
| `kbagent_cli` | Existing `kbagent` commands. |
| `keboola_mcp` | Tools from `keboola-mcp-server`. |
| `local_mcp` | MCP servers already configured for Claude, Codex, Gemini, or the user. |
| `repo` | Git status, branches, diffs, tests, PR operations. |
| `communication` | Slack, Teams, email, calendar. |
| `work_management` | Jira, Linear, GitHub Issues, Asana, Monday. |
| `office_docs` | Google Drive, Docs, Sheets, Slides, SharePoint, Office files. |
| `custom_http` | User-defined HTTP tools with schemas and auth config. |
| `manual` | Human input request, approval, or uploaded file. |

### Tool Metadata

Every tool should have a normalized manifest:

```json
{
  "id": "slack.send_message",
  "provider": "communication",
  "name": "Send Slack message",
  "description": "Send a message to a Slack channel or user.",
  "input_schema": {},
  "output_schema": {},
  "risk": "external_send",
  "side_effects": true,
  "supports_dry_run": true,
  "requires_approval_by_default": true,
  "auth_state": "available",
  "scopes": ["communication:write"],
  "tags": ["stakeholder-input", "notification"]
}
```

### Risk Classes

| Risk | Meaning | Default policy |
|---|---|---|
| `read` | Reads local or remote state. | Allowed when provider is enabled. |
| `compute` | Runs local analysis without side effects. | Allowed with timeout. |
| `write` | Mutates non-production state. | Dry-run first when available. |
| `external_send` | Sends messages outside the local runtime. | Approval required. |
| `destructive` | Deletes, truncates, revokes, overwrites, terminates. | Approval required. |
| `admin` | Tokens, users, roles, org setup, billing-like actions. | Approval required. |
| `secret` | Reads or handles sensitive values. | Denied unless explicitly enabled. |

### Discovery

At startup and on demand, `kbagent serve` should discover:

- Available AI CLIs: command path, version, auth health where detectable.
- Built-in `kbagent serve` endpoints from OpenAPI.
- Existing `keboola-mcp-server` tools.
- User-enabled MCP servers from known local AI configuration locations,
  where this can be done safely and explicitly.
- User-defined custom tools stored in the kbagent config directory.

Discovery should be transparent. The UI must show what was found, where
it came from, whether it is enabled, and what permissions it has.

### Proposed API

```text
GET  /tools/providers
GET  /tools
GET  /tools/{tool_id}
POST /tools/{tool_id}/call
POST /tools/discover

GET  /ai-clis
POST /ai-clis/{cli}/health-check

GET  /policies
PATCH /policies
POST /approvals/{approval_id}/approve
POST /approvals/{approval_id}/reject
```

The existing `/mcp/tools` routes can remain, but team agents should use
the Tool Broker as the higher-level interface.

## Native Team Templates

Templates should be opinionated starting points. Users can duplicate and
edit them, but the first-run experience should work without designing a
team from scratch.

### Keboola and Data Templates

| Template | Roles | Primary tools | Output |
|---|---|---|---|
| Failed Jobs Triage Team | Triage lead, log analyst, remediation planner, reviewer | Jobs, configs, workspace, Slack/Teams optional | Root-cause report, suggested fixes, optional stakeholder update. |
| Data Quality SWAT Team | Profiler, anomaly investigator, SQL analyst, QA reviewer | Storage, workspace query, lineage, docs | Data quality report with checks and next actions. |
| Semantic Layer Builder | Data modeler, metric designer, validation agent, documentation agent | Semantic layer, storage, workspace, docs | Proposed datasets, metrics, relationships, validation report. |
| Storage Cleanup Advisor | Usage analyst, lineage checker, cost estimator, approver | Storage, lineage, jobs | Safe-to-delete/archive list and savings estimate. |
| Lineage Investigator | Graph builder, dependency analyst, report writer | Lineage, sync, storage | Impact map and Mermaid/HTML report. |
| Data App Builder | Product analyst, app engineer, data engineer, QA | Data apps, storage, workspaces, repo | Running data app plan or scaffold and validation notes. |
| Org Onboarding Team | Project registrar, access auditor, inventory writer | Org, project, members, configs | Organization inventory and onboarding checklist. |

### Business Templates

| Template | Roles | Primary tools | Output |
|---|---|---|---|
| Project Management Office | PM lead, stakeholder interviewer, ticket writer, risk tracker | Slack/Teams, email, calendar, Jira/Linear/GitHub Issues, docs | Requirements brief, ticket plan, open questions, stakeholder follow-ups. |
| Product Discovery Team | Researcher, data analyst, PM, reviewer | Slack/Teams, docs, analytics/Keboola, issue tracker | Opportunity brief, evidence table, recommended MVP. |
| Executive Chief of Staff | Briefing analyst, metrics analyst, comms drafter | Calendar, email, docs, Keboola dashboards/data | Daily/weekly executive brief with risks and decisions needed. |
| Customer Success Desk | Account analyst, health-score analyst, follow-up drafter | CRM, Slack/email, Keboola, docs | Account summary, risk flags, next-best actions. |
| Support Triage Team | Intake agent, reproducer, knowledge-base searcher, escalation writer | Support system, Slack/Teams, repo, docs | Classified queue, drafted replies, escalation packets. |
| Sales and RevOps Team | Pipeline analyst, account researcher, outreach drafter, CRM updater | CRM, email, calendar, Keboola, docs | Pipeline summary, account plans, approved CRM updates. |
| Marketing Campaign Ops | Audience analyst, content planner, performance analyst, reviewer | Docs, analytics/Keboola, ad platforms, Slack | Campaign plan, reporting pack, optimization backlog. |
| Finance Operations Team | Reconciliation analyst, variance investigator, approver liaison | Sheets, ERP/export files, Keboola, docs | Variance report, reconciled tables, approval-ready notes. |
| People Ops Team | Policy analyst, survey analyst, comms drafter | Docs, HRIS exports, Slack/email, calendar | Policy brief, survey insights, draft announcements. |
| Legal and Compliance Team | Evidence collector, policy checker, reviewer, exception tracker | Docs, tickets, access logs, Keboola, Slack/email | Audit packet, exceptions list, remediation tasks. |
| Procurement and Vendor Team | Vendor researcher, contract summarizer, risk analyst | Docs, email, spreadsheets, issue tracker | Vendor comparison, renewal brief, approval checklist. |
| Engineering Delivery Team | Tech lead, implementer, reviewer, release manager | Repo, tests, GitHub/GitLab, Slack/Teams, kbagent | Implementation plan, PR, review notes, release checklist. |

### Template Requirements

Each template must define:

- Required user input.
- Optional connected tools.
- Roles and role prompts.
- Allowed tool groups per role.
- Default artifacts.
- Approval gates.
- Completion criteria.
- Example run.
- Failure and escalation behavior.

Example: Project Management Office should not assume Slack is available.
If Slack is missing, it should fall back to manual questions in the UI
and produce a stakeholder-question artifact.

## Execution Model

### Orchestrator Loop

Every team run starts with an orchestrator role:

1. Read the user objective and selected template.
2. Inspect available tools and policy.
3. Create a plan and work item DAG.
4. Assign work to roles.
5. Start independent work items in parallel.
6. Watch artifacts, blockers, tool failures, and confidence.
7. Request human approval or stakeholder input when required.
8. Fan in partial results.
9. Trigger reviewer/evaluator roles.
10. Iterate until acceptance criteria are met or stop with a clear blocker.
11. Produce final report and shipped outputs.

### Agent Prompt Contract

Every agent should receive:

- Role definition and current work item.
- Allowed tools, with a link to the Tool Broker registry.
- Shared artifact paths it may read.
- Artifact path it must write.
- Completion criteria.
- Policy reminder.
- Upstream context and downstream handoff requirements.

Agents should not be asked to infer their own tool universe from memory.
They should query the Tool Broker.

### Persistence Layout

Proposed file layout under the resolved kbagent config directory:

```text
teams.json
team_templates/
  custom/*.json
team_runs/
  <team_id>/
    <run_id>/
      run.json
      events.jsonl
      plan.json
      work_items.json
      approvals.jsonl
      tool_calls.jsonl
      artifacts/
        brief.md
        plan.md
        findings/
        reports/
```

Files should use `0600` permissions because tool calls and artifacts can
contain sensitive business context.

## Proposed Data Model

```text
Team
  id
  name
  description
  template_id
  enabled
  schedule
  roster: AgentRole[]
  tool_policy_id
  default_inputs
  created_at
  updated_at

AgentRole
  id
  name
  responsibility
  ai_cli
  model_hint
  prompt
  allowed_tool_groups
  autonomy_level
  required_artifacts
  review_required

TeamRun
  run_id
  team_id
  objective
  status
  started_at
  ended_at
  summary
  cost
  artifact_root

WorkItem
  id
  run_id
  title
  description
  assigned_role_id
  dependencies
  status
  progress
  confidence
  blocker
  artifacts

ApprovalRequest
  id
  run_id
  work_item_id
  tool_call
  risk
  reason
  status
  requested_at
  decided_at
```

## Proposed Team API

```text
GET    /teams
POST   /teams
GET    /teams/{team_id}
PATCH  /teams/{team_id}
DELETE /teams/{team_id}

GET    /team-templates
POST   /team-templates
GET    /team-templates/{template_id}

POST   /teams/{team_id}/run
POST   /teams/{team_id}/run/stream
GET    /teams/{team_id}/runs
GET    /teams/{team_id}/runs/{run_id}
GET    /teams/{team_id}/runs/{run_id}/events
GET    /teams/{team_id}/runs/{run_id}/work-items
GET    /teams/{team_id}/runs/{run_id}/artifacts
GET    /teams/{team_id}/runs/{run_id}/artifacts/{path}

POST   /teams/{team_id}/runs/{run_id}/input
POST   /teams/{team_id}/runs/{run_id}/pause
POST   /teams/{team_id}/runs/{run_id}/resume
POST   /teams/{team_id}/runs/{run_id}/cancel
```

The existing `/agents` API should remain for simple scheduled tasks.
Teams are the higher-level orchestration layer.

## UI Requirements

### Team Library

- Browse built-in and custom templates.
- Filter by function: Data, PMO, Support, Finance, RevOps, Product,
  Engineering, Compliance.
- Show required tools and missing optional tools.
- Create team from template.

### Team Builder

- Edit roster.
- Choose AI CLI per role.
- Configure tool groups per role.
- Set approval policies.
- Define default artifacts.
- Test-run a single role before saving.

### Office Run View

- Visual office map with active agents by room/state.
- Work item board.
- Roster with progress and current action.
- Inspector drawer for one agent or work item.
- Live event stream.
- Tool call audit log.
- Artifact browser.
- Approval queue.
- Cost and token counters.
- Final report panel.

### Human Input

The UI must support:

- Ask user a question.
- Request approval.
- Upload a file.
- Paste stakeholder response.
- Mark a blocker as resolved.
- Reassign work.
- Stop or pause a run.

## Security and Governance

### Execution Boundary and Sandboxing

Agent Teams should not move execution away from the user's machine by
default. `kbagent serve` starts local AI CLI processes (`claude`, `codex`,
`gemini`) that the user already installed, authenticated, and chose to
trust. Requiring Docker, a VM, or a hosted worker as the default runtime
would break the main advantage of this model: local credentials, local
MCP integrations, local repo access, local keychains, and the user's
existing AI subscriptions are already available.

The product therefore does not need an infrastructure sandbox for MVP.
It does need a **governance sandbox**:

- Agents should receive only the tools their team role is allowed to use.
- Tool calls should go through the Tool Broker when possible, so policy
  is enforced outside the model.
- Risky operations should produce approval requests instead of relying on
  prompt instructions.
- Team runs should use scoped, per-run access to the `kbagent serve`
  surface instead of handing every subprocess an all-powerful bearer token.
- Raw shell access may remain available because local AI CLIs already
  support it, but governed tools should be the preferred path for Keboola
  and business integrations.

This distinction is important: the runtime remains local, but the product
still controls tool visibility, tool invocation, approvals, audit logging,
timeouts, and budgets.

### Policy

Policies should be evaluated before tool execution, not only inside AI
prompts. Prompt instructions are advisory; the Tool Broker is the control
plane.

Required controls:

- Per-provider enable/disable.
- Per-risk default policy.
- Per-team overrides.
- Per-role allowed tool groups.
- Dry-run enforcement when available.
- Approval queue for risky operations.
- Audit log for every tool call.
- Scoped per-run credentials for Tool Broker and `kbagent serve` access.
- Secret masking in prompts, events, and artifacts where possible.

### External Communication

Tools that send messages to Slack, Teams, email, CRM, or issue trackers
need special handling:

- Draft first.
- Show recipient, channel, and body.
- Require approval by default.
- Record final sent message id/link.
- Allow trusted automation only after explicit policy changes.

### Prompt Injection

Agents will read untrusted data from tickets, Slack, docs, and tables.
The runtime should:

- Label untrusted content in prompts.
- Prevent untrusted text from modifying tool policy.
- Keep approval decisions outside the model.
- Avoid putting bearer tokens or secrets into prompts.
- Prefer Tool Broker calls over raw shell commands for governed tools.

## Phased Plan

### Phase 0: Current Foundation

Status: mostly present.

Deliverables:

- `kbagent serve` exposes CLI functions through FastAPI.
- Agent Tasks support `mcp_tool`, `cli_command`, `ai_agent`.
- Local AI CLIs can call back into the live serve via injected env vars.
- UI can create, test, run, stream, and inspect Agent Tasks.
- Run history includes event timeline, stdout/stderr, cost, tokens, and
  tool summary where available.

Exit criteria:

- Document current behavior and limitations.
- Keep `/agents` stable while Teams are introduced separately.

### Phase 1: Tool Broker and AI Runtime Inventory

Objective: make the runtime know what tools exist and how risky they are.

Deliverables:

- Add normalized Tool Broker models.
- Expose `GET /tools`, `GET /tools/providers`, `POST /tools/{id}/call`.
- Wrap existing `/mcp/tools` and `kbagent serve` OpenAPI routes as tool
  providers.
- Add AI CLI inventory: `claude`, `codex`, `gemini` availability and
  basic health check.
- Add tool risk classes, provider enablement, and default policies.
- Add scoped per-run tokens or capability grants for agent subprocesses,
  replacing the current full-server-token pattern for Team runs.
- Add UI page for tool discovery and policy inspection.
- Keep provider interfaces generic so later business tools do not become
  core CLI dependencies.

Acceptance criteria:

- A local AI agent can discover tools from one registry instead of reading
  docs or guessing commands.
- The UI shows missing, disabled, and approval-required tools.
- Existing Agent Tasks can optionally run through Tool Broker calls.

### Phase 2: Team Runtime MVP

Objective: run a small team with orchestrated roles and shared artifacts.

Deliverables:

- Add `Team`, `AgentRole`, `TeamRun`, `WorkItem`, `Artifact`, and
  `ApprovalRequest` persistence.
- Add team run loop with one orchestrator and parallel role execution.
- Add artifact store under `team_runs/<team>/<run>/artifacts`.
- Add SSE stream for team run events.
- Add minimal Team API.
- Add UI: Team list, create from template, run stream, work item board,
  artifact browser.
- Isolate the Team runtime behind an optional module, extra, or feature
  flag so the core CLI and existing `Agent Tasks` remain small.

MVP template:

- "Data Product Builder": PM, data analyst, data engineer, reviewer.
- Inputs: project alias, business question, target output.
- Tools: Keboola storage/jobs/workspace, docs artifact store, optional
  Slack/Teams manual input.
- Output: requirements brief, SQL/findings, validation notes, final report.

Acceptance criteria:

- One user can start a team run manually.
- The orchestrator creates a plan with at least three work items.
- Two work items can run in parallel.
- Agents write artifacts that downstream agents read.
- The reviewer can block completion until issues are fixed.

### Phase 3: Business Templates and Integrations

Objective: make the feature valuable outside data engineering.

Deliverables:

- Add built-in template library for PMO, Product Discovery, Support,
  Customer Success, RevOps, Finance Ops, People Ops, Legal/Compliance,
  Procurement, Engineering Delivery, and Keboola/data workflows.
- Add optional providers for communication, work management, and office
  documents through MCP or custom tool definitions.
- Do not add heavyweight business integration SDKs to the default
  `kbagent` install; load them through optional provider packages or
  existing local MCP servers.
- Add missing-tool fallback paths, especially manual user input.
- Add external-send approval flow.
- Add stakeholder input workflow: draft message, approve, send, wait for
  response, resume team run.

Acceptance criteria:

- Project Management Office template can gather missing requirements via
  Slack/Teams or manual UI input.
- Support Triage template can classify issues and draft replies without
  sending them until approved.
- Finance Ops template can produce a variance report from uploaded files
  or connected sheets.
- Every business template degrades gracefully when optional integrations
  are unavailable.

### Phase 4: Office UI and Operational Observability

Objective: make complex team runs understandable while they are happening.

Deliverables:

- Add Office visual run view.
- Map rooms to real execution states.
- Add live roster, blockers, work item progress, and active tool calls.
- Add approval inbox.
- Add artifact diff/preview.
- Add run replay for completed team runs.
- Add cost, token, elapsed-time, and tool-call dashboards.

Acceptance criteria:

- A user can understand a team run in under one minute without reading raw
  logs.
- A blocked run clearly shows who is blocked, why, and what input is needed.
- Historical runs can be replayed with the same event model as live runs.

### Phase 5: Advanced Autonomy and Governance

Objective: support long-running and higher-trust teams safely.

Deliverables:

- Add schedules for Teams, not only Agent Tasks.
- Add recurring monitors that can spawn team runs.
- Add policy presets: read-only, draft-only, supervised writes, trusted
  workspace, production guarded.
- Add budget limits per run/team/day.
- Add evaluator roles and scorecards.
- Add automatic retry with bounded attempts and changed prompts.
- Add run pause/resume across server restarts.
- Add exportable audit packets for compliance.

Acceptance criteria:

- A scheduled team can run overnight and pause for approvals.
- A user can set a cost budget and stop runs when exceeded.
- A production write cannot happen without a recorded policy decision.

### Phase 6: Sharing, Marketplace, and Extensibility

Objective: let users and teams share useful offices and templates.

Deliverables:

- Import/export team templates as JSON/YAML.
- Template versioning and migration.
- Local marketplace page.
- Custom tool builder UI.
- Template validation and dry-run preview.
- Optional publishing path through the existing plugin marketplace.
- Evaluate whether Agent Office should become a separate package or app
  that depends on `kbagent serve` as its local Keboola API.

Acceptance criteria:

- A user can export a working Project Management Office template and
  another user can import it.
- Custom tools appear in Tool Broker with schema, risk, and policy.
- Template upgrades do not break existing teams without user approval.

## MVP Recommendation

Build the MVP around one cross-functional workflow:

**"Launch a Data Product Team"**

Roles:

- PM Agent: clarifies requirements, asks stakeholders for missing input.
- Data Analyst Agent: explores Keboola data and proposes metrics.
- Data Engineer Agent: writes SQL or semantic-layer changes.
- Reviewer Agent: validates output, checks assumptions, and blocks weak
  answers.
- Orchestrator Agent: plans, assigns, tracks, and synthesizes.

Why this MVP:

- It uses Keboola as the data backbone.
- It proves business-role templates through the PM Agent.
- It requires shared artifacts.
- It needs real tool discovery.
- It has clear safety gates.
- It demonstrates the "office" concept without needing every integration
  on day one.

Expected final outputs:

- Requirements brief.
- Open stakeholder questions and answers.
- Data discovery notes.
- SQL or semantic-layer proposal.
- Validation report.
- Final business summary.
- Optional tickets or Slack/Teams drafts pending approval.

## Success Metrics

| Metric | Target signal |
|---|---|
| Team activation | Users create a team from template without reading docs. |
| Run completion | Team runs finish with usable artifacts, not only chat output. |
| Tool reliability | Tool calls are discoverable, schema-valid, and policy checked. |
| Human control | Risky actions produce approval requests instead of silent writes. |
| Business usefulness | Non-data templates produce real briefs, tickets, drafts, or reports. |
| Observability | Users can diagnose blockers and failures from the UI. |
| Reuse | Users duplicate, edit, and rerun templates for recurring work. |

## Open Questions

- Which local MCP configuration sources should be auto-discovered first,
  and which should require manual import?
- Should Team artifacts be editable in the UI, or only produced by agents
  and downloadable?
- Should Teams reuse existing `AgentTask` records internally, or run on a
  separate scheduler and event model?
- How much shell access should remain available once Tool Broker exists?
- Which business integrations should be first-class versus custom MCP
  tools?
- What is the minimum policy UX that keeps power users fast without making
  production writes too easy?
- Should team templates live only in code at first, or be data files that
  users can inspect and override?
