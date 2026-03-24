---
name: kbagent
description: >
  Use when working with Keboola Connection projects via kbagent CLI.
  Covers: exploring and searching configurations (extractors, writers, transformations),
  browsing job history, analyzing cross-project data lineage, calling MCP tools
  across multiple projects, managing development branches, debugging SQL in
  temporary workspaces, bulk-onboarding organizations, and generating explorer
  dashboards. Triggers: kbagent, Keboola project, keboola configs, keboola jobs,
  keboola lineage, keboola transformations, keboola MCP tools, keboola workspace,
  SQL debugging, keboola branches, keboola organization, keboola explorer,
  search configs, find in configurations, audit configurations.
---

# kbagent -- Keboola Agent CLI

## First step -- always

Load full CLI documentation before doing anything else:

```bash
kbagent context
```

This prints all commands, flags, workflows, and tips. Read it fully before proceeding.

## Rules

1. **Always use `--json`**: `kbagent --json <command>` for parseable output
2. **Multi-project by default**: read commands query ALL connected projects in parallel -- no need to loop
3. **Write commands need `--project`**: specify the target project alias
4. **Tokens are always masked** in output -- this is expected, not an error

## Choosing the right approach

<!-- BEGIN AUTO-GENERATED COMMANDS -->
| Goal | Command |
|------|---------|
| Add a new Keboola project connection | `kbagent project add --alias ALIAS` |
| List all connected Keboola projects | `kbagent project list` |
| Remove a Keboola project connection | `kbagent project remove --alias ALIAS` |
| Edit an existing Keboola project connection | `kbagent project edit --alias ALIAS` |
| Test connectivity to connected Keboola projects | `kbagent project status` |
| Set up all projects from a Keboola organization | `kbagent org setup --org-id ORG-ID --url URL` |
| List configurations from connected projects | `kbagent config list` |
| Show detailed information about a specific configuration | `kbagent config detail --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Search through configuration bodies for a string or pattern | `kbagent config search --query QUERY` |
| Update a configuration's name and/or description | `kbagent config update --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Delete a configuration from a project | `kbagent config delete --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| List jobs from connected projects | `kbagent job list` |
| Show detailed information about a specific job | `kbagent job detail --project PROJECT --job-id JOB-ID` |
| List storage buckets with sharing/linked bucket information | `kbagent storage buckets` |
| Show detailed bucket info including Snowflake direct access paths | `kbagent storage bucket-detail --project PROJECT --bucket-id BUCKET-ID` |
| List storage tables from a project | `kbagent storage tables --project PROJECT` |
| Show cross-project data lineage via bucket sharing | `kbagent lineage show` |
| List development branches from connected projects | `kbagent branch list` |
| Create a new development branch and auto-activate it | `kbagent branch create --project PROJECT --name NAME` |
| Set an existing development branch as active | `kbagent branch use --project PROJECT --branch BRANCH` |
| Reset the active branch back to main/production | `kbagent branch reset --project PROJECT` |
| Delete a development branch | `kbagent branch delete --project PROJECT --branch BRANCH` |
| Get the KBC UI merge URL for a development branch | `kbagent branch merge --project PROJECT` |
| Create a new workspace | `kbagent workspace create --project PROJECT` |
| List workspaces from connected projects | `kbagent workspace list` |
| Show workspace details (password NOT included) | `kbagent workspace detail --project PROJECT --workspace-id WORKSPACE-ID` |
| Delete a workspace | `kbagent workspace delete --project PROJECT --workspace-id WORKSPACE-ID` |
| Reset workspace password and show the new one | `kbagent workspace password --project PROJECT --workspace-id WORKSPACE-ID` |
| Load tables into a workspace | `kbagent workspace load --project PROJECT --workspace-id WORKSPACE-ID --tables TABLES` |
| Execute SQL query in a workspace via Query Service | `kbagent workspace query --project PROJECT --workspace-id WORKSPACE-ID` |
| Create a workspace from a transformation config | `kbagent workspace from-transformation --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| List available MCP tools from the keboola-mcp-server | `kbagent tool list` |
| Call an MCP tool on keboola-mcp-server | `kbagent tool call <TOOL-NAME>` |
| Initialize a sync working directory for a Keboola project | `kbagent sync init --project PROJECT` |
| Download all configurations from a Keboola project to local files | `kbagent sync pull --project PROJECT` |
| Show which local configurations have been modified, added, or deleted | `kbagent sync status` |
| Show detailed diff between local and remote configurations | `kbagent sync diff --project PROJECT` |
| Push local configuration changes to a Keboola project | `kbagent sync push --project PROJECT` |
| Link the current git branch to a Keboola development branch | `kbagent sync branch-link --project PROJECT` |
| Remove the branch mapping for the current git branch | `kbagent sync branch-unlink` |
| Show the branch mapping status for the current git branch | `kbagent sync branch-status` |
| Export project to Twin Format for AI consumption | `kbagent llm export` |
| [deprecated] KBC Explorer dashboard -- use 'sync pull' instead | `kbagent explorer` |
| Generate a tiers.yaml template from registered projects | `kbagent explorer init-tiers` |
<!-- END AUTO-GENERATED COMMANDS -->

## Response format

All JSON responses follow one of two shapes:

**Success:**
```json
{"status": "ok", "data": ...}
```

**Error:**
```json
{"status": "error", "error": {"code": "ERROR_CODE", "message": "...", "retryable": true}}
```

Check the `retryable` field -- if `true`, retry the operation.

For detailed response parsing rules and common pitfalls, see [gotchas](references/gotchas.md).

## First-time setup

If kbagent is not yet installed:

```bash
uv tool install git+https://github.com/padak/keboola_agent_cli
uv tool install --prerelease=allow keboola-mcp-server
kbagent doctor --fix
```

Then add projects:

```bash
# Single project
kbagent --json project add --alias prod --url https://connection.keboola.com --token YOUR_TOKEN

# Or bulk-onboard from organization
KBC_MANAGE_API_TOKEN=xxx kbagent --json org setup --org-id 123 --url https://connection.keboola.com --yes
```
