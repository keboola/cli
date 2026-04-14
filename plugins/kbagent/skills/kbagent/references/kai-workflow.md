# Kai (Keboola AI Assistant) Workflow

Kai is Keboola's cloud AI assistant with MCP access to project data.
kbagent bridges Claude Code (local) to Kai (cloud) for Keboola-specific questions.

> **BETA**: Kai commands require a project with the `agent-chat` feature enabled.
> Token authentication requirements are being finalized.

## When to use Kai vs local tools

| Situation | Use |
|-----------|-----|
| Need project-specific context (tables, configs, lineage) | `kbagent kai ask` |
| Simple data listing (buckets, tables, configs) | `kbagent config list`, `kbagent storage tables` |
| Need Keboola domain knowledge (component behavior, best practices) | `kbagent kai ask` |
| Need to modify data (upload, create, delete) | Direct CLI commands |

## Quick start

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

All kai commands support `--json` for structured output:

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

## Common patterns for Claude Code

```bash
# Use kai ask as a Keboola knowledge oracle
kbagent --json kai ask --project prod -m "Is it safe to drop bucket in.c-legacy?"

# Get project overview for onboarding
kbagent --json kai ask --project prod -m "Describe the data flow in this project"

# Debug a failed job
kbagent --json kai ask --project prod -m "Why did job 12345 fail?"
```
