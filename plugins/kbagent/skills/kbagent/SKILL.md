---
name: kbagent
description: >
  Use when working with Keboola Connection projects via kbagent CLI.
  Covers: exploring and searching configurations (extractors, writers, transformations),
  browsing job history, analyzing cross-project data lineage, calling MCP tools
  across multiple projects, managing development branches, debugging SQL in
  temporary workspaces, bulk-onboarding organizations, syncing project configs
  as local files (GitOps), git-branching with Keboola dev branch isolation,
  sharing buckets across projects, linking shared data,
  and syncing storage metadata and job history. Triggers: kbagent, Keboola project,
  keboola configs, keboola jobs, keboola lineage, keboola transformations,
  keboola MCP tools, keboola workspace, SQL debugging, keboola branches,
  keboola organization, keboola sharing, bucket sharing, link bucket,
  keboola sync, keboola git,
  keboola gitops, sync pull, sync push, sync diff, branch-link,
  search configs, find in configurations, audit configurations,
  input mapping migration, remove input mapping, Snowflake paths,
  MULTI_STATEMENT_COUNT, statement count error, SQL transformation migration.
---

# kbagent -- Keboola Agent CLI

## How to use this skill

This skill contains everything you need. The decision table below maps goals to commands.
For detailed workflows, see the `references/` docs linked at the bottom.

For **command flags and parameters**, use `kbagent <command> --help` (e.g. `kbagent config new --help`).

If kbagent is not installed or you need the full standalone reference, run `kbagent context`.

## Rules

1. **Always use `--json`**: `kbagent --json <command>` for parseable output
2. **Set conversation ID**: before first kbagent call, run `export KBAGENT_CONVERSATION_ID="<unique-id>"` (e.g. session UUID). All API requests include this as `X-Conversation-ID` header for platform observability.
3. **Multi-project by default**: read commands query ALL connected projects in parallel -- no need to loop
4. **Write commands need `--project`**: specify the target project alias
5. **Tokens are always masked** in output -- this is expected, not an error

## Choosing the right approach

<!-- BEGIN AUTO-GENERATED COMMANDS -->
| Goal | Command |
|------|---------|
| Update kbagent to the latest version | `kbagent update` |
| List all operations with their risk category and current allowed/denied status | `kbagent permissions list` |
| Show the current active permission policy | `kbagent permissions show` |
| Set the permission policy (firewall rules) | `kbagent permissions set --mode MODE` |
| Remove all permission restrictions | `kbagent permissions reset` |
| Check if a specific operation is allowed | `kbagent permissions check <OPERATION>` |
| Add a new Keboola project connection | `kbagent project add --project ALIAS` |
| List all connected Keboola projects | `kbagent project list` |
| Remove a Keboola project connection | `kbagent project remove --project ALIAS` |
| Edit an existing Keboola project connection | `kbagent project edit --project ALIAS` |
| Test connectivity to connected Keboola projects | `kbagent project status` |
| Set up projects and register them in the kbagent config | `kbagent org setup --url URL` |
| List available components from connected projects | `kbagent component list` |
| Show detailed information about a specific component | `kbagent component detail --component-id COMPONENT-ID` |
| List configurations from connected projects | `kbagent config list` |
| Show detailed information about a specific configuration | `kbagent config detail --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Search through configuration bodies for a string or pattern | `kbagent config search --query QUERY` |
| Update a configuration's name and/or description | `kbagent config update --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Delete a configuration from a project | `kbagent config delete --project PROJECT --component-id COMPONENT-ID --config-id CONFIG-ID` |
| Generate boilerplate configuration files for a Keboola component | `kbagent config new --component-id COMPONENT-ID` |
| List jobs from connected projects | `kbagent job list` |
| Show detailed information about a specific job | `kbagent job detail --project PROJECT --job-id JOB-ID` |
| List storage buckets with sharing/linked bucket information | `kbagent storage buckets` |
| Show detailed bucket info including Snowflake direct access paths | `kbagent storage bucket-detail --project PROJECT --bucket-id BUCKET-ID` |
| List storage tables from a project | `kbagent storage tables --project PROJECT` |
| Create a new storage bucket | `kbagent storage create-bucket --project PROJECT --stage STAGE --name NAME` |
| Create a typed storage table | `kbagent storage create-table --project PROJECT --bucket-id BUCKET-ID --name NAME --column col:TYPE` |
| Upload a CSV file into a storage table | `kbagent storage upload-table --project PROJECT --table-id TABLE-ID --file FILE` |
| Delete one or more storage tables | `kbagent storage delete-table --project PROJECT --table-id TABLE-ID` |
| Delete one or more storage buckets | `kbagent storage delete-bucket --project PROJECT --bucket-id BUCKET-ID` |
| List shared buckets available for linking | `kbagent sharing list` |
| Enable sharing on a bucket | `kbagent sharing share --project PROJECT --bucket-id BUCKET-ID --type SHARING-TYPE` |
| Disable sharing on a bucket | `kbagent sharing unshare --project PROJECT --bucket-id BUCKET-ID` |
| Link a shared bucket into a project | `kbagent sharing link --project PROJECT --source-project-id SOURCE-PROJECT-ID --bucket-id BUCKET-ID` |
| Remove a linked bucket from a project | `kbagent sharing unlink --project PROJECT --bucket-id BUCKET-ID` |
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
| Download configurations from a Keboola project to local files | `kbagent sync pull` |
| Show which local configurations have been modified, added, or deleted | `kbagent sync status` |
| Show detailed diff between local and remote configurations | `kbagent sync diff` |
| Push local configuration changes to a Keboola project | `kbagent sync push` |
| Link the current git branch to a Keboola development branch | `kbagent sync branch-link --project PROJECT` |
| Remove the branch mapping for the current git branch | `kbagent sync branch-unlink` |
| Show the branch mapping status for the current git branch | `kbagent sync branch-status` |
<!-- END AUTO-GENERATED COMMANDS -->

### Sync pull notable flags

| Flag | Effect |
|------|--------|
| `--with-samples` | Download CSV data previews (tables >30 columns auto-trimmed to first 30) |
| `--job-limit N` | Max recent jobs per config (default 5) |
| `--no-storage` | Skip storage bucket/table metadata |
| `--no-jobs` | Skip per-config job history |
| `--sample-limit N` | Max rows per sample (default 100) |
| `--max-samples N` | Max tables to sample (default 50) |

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

## Workflow references

| Workflow | Reference |
|----------|-----------|
| All commands cheat sheet | [commands-reference](references/commands-reference.md) |
| Creating new configurations | [scaffold-workflow](references/scaffold-workflow.md) |
| MCP tools (multi-project read/write) | [mcp-workflow](references/mcp-workflow.md) |
| Workspace SQL debugging | [workspace-workflow](references/workspace-workflow.md) |
| Bucket sharing & linking | [sharing-workflow](references/sharing-workflow.md) |
| Dev branches | [branch-workflow](references/branch-workflow.md) |
| Sync & Git-branching (GitOps) | [sync-workflow](references/sync-workflow.md) |
| Reading synced data | [reading-synced-data](references/reading-synced-data.md) |
| SQL migration (input mapping removal) | [sql-migration-workflow](references/sql-migration-workflow.md) |
| Response parsing gotchas | [gotchas](references/gotchas.md) |

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
kbagent --json project add --project prod --url https://connection.keboola.com --token YOUR_TOKEN

# Or bulk-onboard from organization (org admin)
KBC_MANAGE_API_TOKEN=xxx kbagent --json org setup --org-id 123 --url https://connection.keboola.com --yes

# Or onboard specific projects (any project member, uses Personal Access Token)
KBC_MANAGE_API_TOKEN=xxx kbagent --json org setup --project-ids 901,9621,10539 --url https://connection.keboola.com --yes
```
