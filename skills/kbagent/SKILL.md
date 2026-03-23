---
name: kbagent
description: >
  Use when working with Keboola projects via kbagent CLI — exploring configurations,
  browsing job history, analyzing cross-project lineage, calling MCP tools, managing
  dev branches, or debugging SQL in workspaces. Triggers: kbagent, Keboola project,
  keboola configs, keboola lineage, keboola jobs, keboola transformations, keboola MCP,
  across all Keboola projects.
---

# kbagent — Keboola Agent CLI

## First step — always

Run this before doing anything else. It loads complete usage instructions for all commands:

```bash
kbagent context
```

## Rules

- Always use `--json` flag: `kbagent --json <command>`
- `project list` returns `data` as a **list** (not `data.projects`)
- `config list` returns `data.configs` as a flat list
- Multi-project read operations run in parallel automatically — no need to loop over projects
- Tokens are always masked in output — expected behavior

## Common workflows

**Explore the whole organization:**
```bash
kbagent --json project list
kbagent --json lineage show
kbagent explorer        # generates + opens interactive HTML dashboard in browser
```

**Browse configs across all projects:**
```bash
kbagent --json config list --component-type transformation
kbagent --json config list --component-type extractor
kbagent --json config detail --project <alias> --component-id <id> --config-id <id>
```

**Check recent jobs or failures:**
```bash
kbagent --json job list --status error
kbagent --json job list --project <alias> --limit 20
```

**Call MCP tools across all projects in parallel:**
```bash
kbagent --json tool list
kbagent --json tool call get_buckets
kbagent --json tool call list_configs --project <alias>
```

**Debug SQL without running full jobs:**
```bash
kbagent --json workspace from-transformation --project <alias> --component-id <comp> --config-id <id>
kbagent --json workspace query --project <alias> --workspace-id <id> --sql "SELECT ..."
kbagent --json workspace delete --project <alias> --workspace-id <id>
```

**Dev branches:**
```bash
kbagent --json branch create --project <alias> --name "fix-xyz"
# all subsequent tool calls auto-use the active branch
kbagent --json branch merge --project <alias>   # returns UI URL for safe review, resets to main
```

## First-time setup (if kbagent not yet installed)

```bash
uv tool install git+https://github.com/padak/keboola_agent_cli
uv tool install --prerelease=allow keboola-mcp-server
kbagent org setup --org-id <ORG_ID> --url <STACK_URL>   # prompts interactively for Manage API token
kbagent doctor
```
