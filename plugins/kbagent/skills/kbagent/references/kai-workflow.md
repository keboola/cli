# Kai (Keboola AI Assistant) Workflow -- DEPRECATED

> **DEPRECATED (since v0.77.0). Do not recommend the `kai` group for new work.**
>
> `kbagent kai *` talks to the **legacy `kai-assistant` backend**, which is
> frozen: Linear AI-3388 was **canceled**, and product confirmed that only the
> successor backend will receive further work. That successor is **`kai-agent`**
> (Linear AI-3391) — a different API surface that is **not wired into kbagent**.
> Retargeting is a separate, future task.
>
> **The group will be removed in a later minor, and there is no replacement in
> the interim.** Behavior is unchanged in 0.77.0 — every subcommand still works
> exactly as before against a master Storage token; each one now emits a
> deprecation warning on stderr (human mode) and an additive `deprecation` key
> in the `--json` success payload.
>
> **What to use instead today:**
> - `kbagent docs query "QUESTION"` for "how do I ..." documentation questions
>   (AI Service RAG, works with any token, does **not** see project data).
> - Native commands for project data: `storage tables`, `storage table-detail`,
>   `config list`, `config detail`, `search`, `job detail`, `lineage show`.
> - The MCP integration for agentic exploration.
>
> This file is kept only so that existing `kai` users can still read the
> mechanics. Everything below describes behavior that is deprecated.

Kai is Keboola's cloud AI assistant with MCP access to project data.
kbagent bridges Claude Code (local) to Kai (cloud) for Keboola-specific questions.

> **Token requirement**: Kai commands require a project added with its MASTER
> Storage API token and the `agent-chat` feature enabled. Custom Storage API
> tokens cannot access Kai.

## When to use Kai vs local tools

`kai` is deprecated, so the honest answer is "prefer the right-hand column".
The historical guidance was:

| Situation | Use |
|-----------|-----|
| Need project-specific context (tables, configs, lineage) | ~~`kbagent kai ask`~~ (DEPRECATED) -> `storage tables`, `config detail`, `lineage show` |
| Simple data listing (buckets, tables, configs) | `kbagent config list`, `kbagent storage tables` |
| Need Keboola domain knowledge (component behavior, best practices) | ~~`kbagent kai ask`~~ (DEPRECATED) -> `kbagent docs query` |
| Need to modify data (upload, create, delete) | Direct CLI commands |

## Quick start (deprecated -- still functional)

```bash
# Check if Kai is available
kbagent kai ping --project my-project

# Ask a question about the project
kbagent kai ask --project my-project -m "What tables do I have?"

# Multi-turn conversation
kbagent kai chat --project my-project -m "Help me debug my pipeline"
# Note the chat_id in the response, then continue:
kbagent kai chat --project my-project --chat-id CHAT_ID -m "What about the error in step 3?"

# View chat history
kbagent kai history --project my-project --limit 10
```

## Feature detection

Kai requires the `agent-chat` feature flag on the project. If not enabled,
kai commands return error code `KAI_NOT_ENABLED` with a clear message.

Check via: `kbagent --json kai ping --project ALIAS` — exit code 0 means Kai is available.

## JSON output

All kai commands support `--json` for structured output. Since v0.77.0 every
success payload also carries an additive `deprecation` string key (existing
keys are untouched; error envelopes carry no such key), so a script can detect
the deprecation programmatically:

```bash
# Ping
kbagent --json kai ping --project my-project
# {"status": "ok", "data": {"timestamp": "...", "mcp_status": "ok", ...}}

# Ask
kbagent --json kai ask --project my-project -m "How many tables?"
# {"status": "ok", "data": {"chat_id": "uuid", "response": "You have 19 tables."}}

# History
kbagent --json kai history --project my-project
# {"status": "ok", "data": {"chats": [...], "has_more": false}}
```

## Common patterns for Claude Code (DEPRECATED -- do not suggest these)

These are kept for reference only. Use `kbagent docs query` for documentation
questions and native commands for project data.

```bash
# Use kai ask as a Keboola knowledge oracle
kbagent --json kai ask --project prod -m "Is it safe to drop bucket in.c-legacy?"

# Get project overview for onboarding
kbagent --json kai ask --project prod -m "Describe the data flow in this project"

# Debug a failed job
kbagent --json kai ask --project prod -m "Why did job 12345 fail?"
```
