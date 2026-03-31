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
- Master token for sharing: `KBC_MASTER_TOKEN_{ALIAS}` (e.g. `KBC_MASTER_TOKEN_PROD`) or `KBC_MASTER_TOKEN` as global fallback. Alias is uppercased, hyphens become underscores. Required for `sharing share` and `sharing unshare`; `sharing list/link/unlink` use regular project tokens.

## MCP tool call gotchas

- **Read tools** (multi_project=true): automatically query all projects. No `--project` needed.
- **Write tools** (multi_project=false): require `--project` to specify the target.
- **Auto-expand**: tools like `get_tables` that need `bucket_ids` auto-resolve them by calling `get_buckets` first.
- **Input validation**: tool input is validated against the tool's `inputSchema` before dispatch.
  Only pass parameters defined in the schema. Unexpected parameters cause Pydantic validation errors.
- **Branch scope**: when active branch is set, MCP tools and config commands automatically scope to that branch.
  `branch_id` is a **CLI flag** (`--branch`), NOT a tool input parameter -- do not pass it inside `--input`.
  Config read commands (`config list`, `config detail`, `config search`) also support `--branch`.
- **Schema discovery**: use `kbagent --json tool list` to inspect each tool's `inputSchema` and find
  accepted parameters. For example, `get_configs` takes `configs` (a list of `{component_id, configuration_id}`
  objects), not a flat `config_id` string.

## Conversation ID

Set `KBAGENT_CONVERSATION_ID` env var before running kbagent commands. All API
requests include it as `X-Conversation-ID` header for platform observability.
If unset, the header is omitted.

## Config resolution order

kbagent looks for configuration in this order:
1. `--config-dir` flag
2. `KBAGENT_CONFIG_DIR` env var
3. `.kbagent/` in current or parent directories (local workspace)
4. `~/.config/keboola-agent-cli/` (global)

Use `kbagent init` to create a local `.kbagent/` workspace for per-directory isolation.

## Batch size limits for update_sql_transformation

When using `update_sql_transformation` with `str_replace` operations, **limit batches
to 50 operations maximum**. Larger batches (150+) may trigger a Storage Events API
size limit: the replacements are applied and a new version is created, but the MCP
server fails to log the change event and returns `isError: true` with
`400 Bad Request: Request too large`. This creates a confusing state where changes
were saved but the tool reports failure.

Workaround for large refactors (e.g. removing `AS` from 200 table aliases):
1. Split operations into batches of 50
2. Call `update_sql_transformation` once per batch
3. Verify each batch succeeded before sending the next

## SQL transformation file layout

When creating or editing SQL transformations via sync, SQL code must go in
`transform.sql`, NOT in `_config.yml`. The `_config.yml` for transformations
should have `parameters: {}` (empty).

**Wrong** -- putting SQL in `_config.yml` parameters:
```yaml
# DO NOT DO THIS -- SQL will be split per line and each line executed separately
parameters:
  blocks:
    - name: Block 1
      codes:
        - name: Code 1
          script:
            - CREATE TABLE foo AS
            - "    SELECT col1"
            - "    FROM bar;"
```

**Correct** -- SQL in `transform.sql`, config has empty parameters:
```yaml
# _config.yml
parameters: {}
```
```sql
-- transform.sql
/* ===== BLOCK: Block 1 ===== */

/* ===== CODE: Code 1 ===== */
CREATE TABLE foo AS
    SELECT col1
    FROM bar;
```

See `scaffold-workflow.md` for the complete file structure reference.

## Common mistakes

- **Forgetting `--json`**: without it, output is human-formatted Rich text, not parseable
- **Assuming `data.projects`**: `project list` returns data as a flat list
- **Passing manage token as argument**: use env var `KBC_MANAGE_API_TOKEN` instead
- **Polling after branch create**: kbagent already waits for async completion
- **Not saving workspace password**: only returned once on creation
- **Putting SQL in _config.yml**: SQL transformations must use `transform.sql` with block markers (see above)
