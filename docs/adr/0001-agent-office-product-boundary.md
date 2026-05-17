# ADR 0001: Agent Office Product Boundary

## Status

Accepted

## Date

2026-05-17

## Context

`kbagent serve` now exposes kbagent capabilities as a local HTTP API and
can host scheduled Agent Tasks that run `mcp_tool`, `cli_command`, or
local AI CLI (`claude`, `codex`, `gemini`) actions.

The next product direction is Agent Office: teams of local AI agents with
roles, shared artifacts, governed tool access, business templates,
stakeholder workflows, and an office-style UI. This is broader than
Keboola CLI usage. It can include Slack, Teams, Jira, GitHub, Google
Workspace, Microsoft 365, CRM, support, finance, and project-management
tools through MCP or other provider interfaces.

The risk is scope creep. If Agent Office is built directly into the core
CLI, `kbagent` could become a large general-purpose agent platform with
heavy business-tool dependencies. If it is split into a separate project
too early, the first useful personal workflow becomes harder: users would
lose the simple `kbagent serve --ui` path and we would duplicate runtime,
auth, UI, SSE, and Keboola context.

## Decision

Use a hybrid product boundary:

- `kbagent` remains the local Keboola runtime and governed capability
  provider: CLI commands, config, project tokens, permissions,
  `kbagent serve`, Keboola REST/MCP access, Tool Broker primitives,
  audit, approvals, and simple Agent Tasks.
- Agent Office is an optional orchestration layer built on stable
  `kbagent serve` APIs: teams, roles, orchestrator loops, business
  templates, external stakeholder workflows, shared artifacts, and the
  office-style UI.
- The MVP can live in this repository for iteration speed and the
  one-command local experience.
- The implementation must be structured so Agent Office can later be
  extracted into a separate package or app that depends on `kbagent serve`.

Core rule:

> `kbagent serve` is the local Keboola capability provider and governed
> tool host. Agent Office is an optional orchestration layer built on top
> of it. Core `kbagent` must remain useful without Agent Office, and
> Agent Office must consume `kbagent` through stable APIs rather than CLI
> internals.

## Consequences

- `kbagent serve --ui` can expose Agent Office for personal/local use,
  but core CLI workflows must work without it.
- Business integrations must enter through provider interfaces, MCP, or
  custom tool definitions, not default core dependencies.
- Tool Broker and policy primitives belong in `kbagent`, because they
  govern access to Keboola and other local capabilities.
- Team runtime code should be isolated behind an optional module, extra,
  feature flag, or clearly separable package boundary.
- Team templates should be data-driven where practical, so they can move
  to a marketplace or separate package later.
- APIs should be designed before UI shortcuts, because stable APIs are
  the extraction path.

## Alternatives Considered

### Build Agent Office Deeply Into `kbagent`

This gives the fastest single-repo development path, but it risks turning
the CLI into a broad business-agent platform. It would also invite heavy
optional integrations into the default install path.

Rejected as the long-term boundary.

### Start a Separate Agent Office Project Immediately

This keeps `kbagent` small, but it slows down the MVP and duplicates the
existing serve/runtime/UI foundations. It also weakens the personal local
workflow where `kbagent serve --ui` is the natural entry point.

Rejected for MVP timing.

### Use Only External AI CLI Native Tooling

This avoids new kbagent runtime work, but leaves no central policy,
approval, audit, artifact, or team orchestration model. It also makes
tool discovery inconsistent across Claude, Codex, Gemini, and MCP setups.

Rejected because governance and observability are product requirements.

## Related Documents

- [Agent Teams PRD](../agents.md)
