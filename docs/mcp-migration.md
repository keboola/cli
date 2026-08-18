# Migrating off the MCP passthrough (removed in v0.85.0)

kbagent v0.85.0 removed the MCP passthrough (epic #390 phase 3):
- `kbagent tool list` / `kbagent tool call`
- `kbagent agent --type mcp_tool` scheduled tasks (existing tasks no longer run;
  they are kept on disk so you can migrate them)
- the `/mcp/*` REST routes of `kbagent serve`
- automatic install/update of `keboola-mcp-server`

Every MCP tool has a native command. Replace `tool call <name>` with the
command below; replace `agent --type mcp_tool --tool <name> --input JSON`
with `--type cli_command --argv ...` using the same command.

## Tool -> command map

| MCP tool | Native command | Notes |
|---|---|---|
| `add_config_row` | `kbagent config row-create` | |
| `create_config` | `kbagent config new` | use `--push` for one-shot remote create |
| `create_sql_transformation` | `kbagent transformation create` | |
| `get_components` | `kbagent component list` | or `component detail` |
| `get_config_examples` | `kbagent config examples` | |
| `get_configs` | `kbagent config list` | or `config detail` |
| `run_sync_action` | `kbagent component sync-action` | |
| `update_config` | `kbagent config update` | |
| `update_config_row` | `kbagent config row-update` | |
| `update_sql_transformation` | `kbagent transformation edit` | |
| `docs_query` | `kbagent docs query` | |
| `create_conditional_flow` | `kbagent flow new` | |
| `create_flow` | `kbagent flow new` | legacy `keboola.orchestrator` flows were dropped in 0.57.0; kbagent creates conditional flows (`keboola.flow`) only |
| `get_flow_examples` | `kbagent flow examples` | |
| `get_flow_schema` | `kbagent flow schema` | `--full` for the JSON Schema |
| `get_flows` | `kbagent flow list` | or `flow detail` |
| `modify_flow` | `kbagent flow update` | |
| `update_flow` | `kbagent flow update` | |
| `get_jobs` | `kbagent job list` | or `job detail` |
| `run_job` | `kbagent job run` | add `--wait` to collect the result |
| `create_oauth_url` | `kbagent config oauth-url` | |
| `create_python_js_data_app_git_credential` | `kbagent data-app git-credentials-create` | |
| `delete_python_js_data_app_draft` | `kbagent data-app delete` | |
| `deploy_data_app` | `kbagent data-app deploy` | or `data-app stop` |
| `get_data_apps` | `kbagent data-app list` | or `data-app detail` |
| `modify_python_js_data_app` | `kbagent data-app create` | update via `config update` + `data-app secrets-set` |
| `modify_streamlit_data_app` | `kbagent data-app create` | update via `config update` + `data-app secrets-set` |
| `get_project_info` | `kbagent project info` | |
| `update_project_description` | `kbagent project description-set` | |
| `query_data` | `kbagent workspace query` | intentionally unported (#390): `workspace query` composes multi-step SQL over a persistent workspace instead of an implicit one-shot SELECT |
| `find_component_id` | `kbagent component list` | use `--query` |
| `search` | `kbagent search` | |
| `get_semantic_context` | `kbagent semantic-layer show` | or `semantic-layer get-context` |
| `get_semantic_schema` | `kbagent semantic-layer schema` | |
| `search_semantic_context` | `kbagent semantic-layer search-context` | |
| `validate_semantic_query` | `kbagent semantic-layer validate` | intentionally unported (#390): model-level validation; the MCP tool's per-query string heuristics were rejected as drift-prone |
| `get_buckets` | `kbagent storage buckets` | or `storage bucket-detail` |
| `get_tables` | `kbagent storage tables` | or `storage table-detail` |
| `update_descriptions` | `kbagent storage describe-batch` | or `describe-table` / `describe-bucket` / `describe-column` |

## Still using keboola-mcp-server elsewhere (Claude Desktop, Cursor)?

kbagent no longer updates it for you. Keep it fresh yourself:

    uv tool install --upgrade --prerelease=allow keboola-mcp-server

(`--prerelease=allow` is required: the server depends on a pre-release-only
transitive package; without the flag uv silently resolves an ancient version.)

## Migrating a scheduled task

1. `kbagent agent show TASK_ID` -- read the old `params` (tool, project, input).
2. Find the native command in the table above.
3. `kbagent agent update TASK_ID` cannot change the action type -- create a new
   task with `--type cli_command --argv <cmd> --argv <sub> --argv --project=ALIAS ...`
   and `kbagent agent delete OLD_ID --yes`.
