# Gotchas -- Response Parsing and Common Pitfalls

## Response structure varies by command

Not all commands return data the same way. Key differences:

| Command | `data` contains |
|---------|----------------|
| `project list` | A **list** directly (not `data.projects`) |
| `config list` | `{"configs": [...]}` |
| `job list` | `{"jobs": [...]}` |
| `lineage show` | `{"lineage_links": [...], "errors": [...]}` |
| `tool list` | `{"tools": [...]}` |
| `tool call` | `{"results": [...]}` (one per project) |
| `workspace list` | `{"workspaces": [...], "errors": [...]}` |
| `branch list` | `{"branches": [...]}` |
| `config search` | `{"matches": [...], "errors": [...], "stats": {...}}` |

Always check the actual response structure rather than assuming a pattern.

## Multi-project error accumulation

Commands that query multiple projects collect errors per-project without stopping.
One project failing does not block others. Check the `errors` array:

```json
{
  "status": "ok",
  "data": {
    "configs": [...],
    "errors": [
      {"project_alias": "broken-proj", "error_code": "AUTH_ERROR", "message": "..."}
    ]
  }
}
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error |
| 2 | Usage error (invalid arguments) |
| 3 | Authentication error (invalid or expired token) |
| 4 | Network error (timeout, unreachable) |
| 5 | Configuration error (corrupt config, missing alias) |

## Token handling

- Tokens are always masked in output (e.g. `901-...pt0k`) -- this is normal
- Token can be passed via `--token`, `KBC_TOKEN` env var, or interactive prompt
- Manage API token: only via `KBC_MANAGE_API_TOKEN` env var or interactive prompt (never as CLI argument)

## MCP tool call gotchas

- **Read tools** (multi_project=true): automatically query all projects. No `--project` needed.
- **Write tools** (multi_project=false): require `--project` to specify the target.
- **Auto-expand**: tools like `list_tables` that need `bucket_id` auto-resolve it by calling `list_buckets` first.
- **Input validation**: tool input is validated against the tool's `inputSchema` before dispatch.
  Only pass parameters defined in the schema. Unexpected parameters cause Pydantic validation errors.
- **Branch scope**: when active branch is set, MCP tools automatically scope to that branch.
  `branch_id` is a **CLI flag** (`--branch`), NOT a tool input parameter -- do not pass it inside `--input`.
- **Schema discovery**: use `kbagent --json tool list` to inspect each tool's `inputSchema` and find
  accepted parameters. For example, `get_configs` takes `configs` (a list of `{component_id, configuration_id}`
  objects), not a flat `config_id` string.

## Config resolution order

kbagent looks for configuration in this order:
1. `--config-dir` flag
2. `KBAGENT_CONFIG_DIR` env var
3. `.kbagent/` in current or parent directories (local workspace)
4. `~/.config/keboola-agent-cli/` (global)

Use `kbagent init` to create a local `.kbagent/` workspace for per-directory isolation.

## Common mistakes

- **Forgetting `--json`**: without it, output is human-formatted Rich text, not parseable
- **Assuming `data.projects`**: `project list` returns data as a flat list
- **Passing manage token as argument**: use env var `KBC_MANAGE_API_TOKEN` instead
- **Polling after branch create**: kbagent already waits for async completion
- **Not saving workspace password**: only returned once on creation
