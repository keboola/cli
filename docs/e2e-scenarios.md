# E2E Test Scenarios

End-to-end tests that exercise the full CLI against a real Keboola project.

## Running

```bash
# All E2E tests (~2.5 min)
E2E_API_TOKEN=xxx E2E_URL=connection.keboola.com make test-e2e

# Only the main scenario (36 steps)
E2E_API_TOKEN=xxx E2E_URL=connection.keboola.com \
    uv run pytest tests/test_e2e.py::TestFullE2E -v -s

# Without credentials -- all tests are skipped automatically
make test-e2e
```

## Test classes

| Class | Tests | What it covers |
|-------|------:|----------------|
| `TestFullE2E` | 1 (41 steps) | Progressive scenario building state from empty project |
| `TestE2EErrorHandling` | 6 | Invalid tokens, nonexistent resources, correct exit codes |
| `TestE2EJsonConsistency` | 2 | All read commands return valid JSON; token never leaks |
| `TestE2ESyncWorkflow` | 1 (5 steps) | Sync init/pull/status/diff/push in a temp git repo |
| `TestE2EToolCommands` | 2 | MCP tool list + tool call (skipped if no MCP server) |

---

## TestFullE2E -- Main scenario (36 steps)

All steps run sequentially. Each step builds on the state created by previous steps.
Resources are prefixed with `e2e-{timestamp}` and cleaned up via yield fixture even on failure.

### Phase 1: Setup

| Step | Command | What is verified |
|-----:|---------|------------------|
| 1 | `version`, `changelog`, `context` | Offline commands work, version contains dot, context mentions kbagent |
| 2 | `init` | Creates `.kbagent/` directory, returns `created: true` |
| 3 | `project add` | Registers project, returns alias/name/id, token is masked |
| 4 | `project list`, `project status` | Project appears in list, status is `ok` with response time |
| 5 | `doctor` | Health check passes (`summary.healthy: true`) |

### Phase 2: Read empty project

| Step | Command | What is verified |
|-----:|---------|------------------|
| 6 | `config list`, `storage buckets`, `job list` | Empty lists with correct JSON structure, no errors |

### Phase 3: Storage CRUD

| Step | Command | What is verified |
|-----:|---------|------------------|
| 7 | `storage create-bucket` | Bucket ID starts with `in.c-`, tracked for cleanup |
| 8 | `storage buckets`, `storage bucket-detail` | Bucket appears in listing, detail returns correct ID |
| 9 | `storage create-table` | Table created with typed columns (INTEGER, STRING) and primary key |
| 10 | `storage upload-table` | 5-row CSV uploaded, `imported_rows: 5` |
| 11 | `storage upload-table --incremental` | 3 more rows appended, download verifies 8 total rows |
| 12 | `storage tables`, `storage table-detail` | Table in listing, column details match (id, name, value) |
| 13 | `storage download-table` | Full download: 8 rows, correct IDs. With `--columns`/`--limit`: subset verified |
| 14 | `storage unload-table --download` | Table exported to file storage, file_id > 0, file downloaded |
| 15 | `storage load-file` | CSV uploaded as file, then loaded into table via `load-file` |

### Phase 4: Config operations

| Step | Command | What is verified |
|-----:|---------|------------------|
| 16 | `config list`, `config detail`, `config search`, `config search --ignore-case` | Config found by name, detail has correct parameters, search matches by pattern |
| 17 | `config update --set`, `--dry-run`, `--name/--description`, `--configuration` | Nested key set preserves siblings, dry-run shows diff, full replace removes old keys |
| 18 | `config update --merge` | Partial JSON deep-merged, existing keys preserved alongside new ones |
| 19 | `config new --component-id keboola.ex-http` | Scaffold generated with `_config.yml` file |

### Phase 5: Component discovery

| Step | Command | What is verified |
|-----:|---------|------------------|
| 20 | `component list`, `component list --type extractor`, `component detail` | Components listed (after config exists), type filter works, detail has schema info |

### Phase 6: Workspace lifecycle

| Step | Command | What is verified |
|-----:|---------|------------------|
| 21 | `workspace create` | Returns workspace_id, host, schema, user, password |
| 22 | `workspace list` | Workspace appears in project listing |
| 23 | `workspace detail` | Returns backend, host, schema, user |
| 24 | `workspace password` | New password returned (non-empty) |
| 25 | `workspace load --tables TABLE_ID` | Test table loaded into workspace |
| 26 | `workspace query --sql "SELECT COUNT(*)"` | SQL executed successfully |
| 27 | `workspace delete` | Workspace removed |

Steps 21-27 are wrapped in try/except -- if workspace API is unavailable on the stack, they are skipped gracefully.

### Phase 7: Transformation job run (Snowflake SQL)

| Step | Command | What is verified |
|-----:|---------|------------------|
| 28 | `storage create-bucket` (out stage) + API `create_config` | Output bucket created, Snowflake transformation config created with SQL: `SELECT id, name, CAST(value AS INT) AS value, CAST(value AS INT) * 2 AS doubled_value` |
| 29 | `job run --wait --timeout 300` | Transformation executes, job status is `success` |
| 30 | `job detail --job-id ID` | Completed job detail: `status=success`, `isFinished=true`, component is `keboola.snowflake-transformation` |
| 31 | `storage download-table` (output table) | Output downloaded, 9 rows, every row has `doubled_value == value * 2` |
| 32 | `config delete` + `storage delete-bucket --force` | Transformation config and output bucket cleaned up |

### Phase 8: File operations

| Step | Command | What is verified |
|-----:|---------|------------------|
| 33 | `file-upload`, `files`, `file-detail`, `file-download`, `file-tag --add/--remove`, `file-delete --dry-run`, `file-delete --yes` | Full lifecycle: upload with tags, list by tag, detail shows tags, download content matches, tag add/remove verified, dry-run shows would_delete, actual delete confirmed |

### Phase 9: Encrypt

| Step | Command | What is verified |
|-----:|---------|------------------|
| 34 | `encrypt values` | Input `#password`/`#api_key` encrypted to `KBC::ProjectSecure::...` format |

### Phase 10: Branch lifecycle

| Step | Command | What is verified |
|-----:|---------|------------------|
| 35 | `branch list`, `branch create`, `branch use`, `branch reset`, `branch merge`, `branch delete` | Main branch exists, dev branch created (auto-activates), use/reset toggle active branch in project status, merge returns URL, branch deleted and gone from list |

### Phase 11: Permissions

| Step | Command | What is verified |
|-----:|---------|------------------|
| 36 | `permissions list`, `permissions show`, `permissions check branch.delete` | List returns operations array, show returns policy status, check returns `allowed: true` |

### Phase 12: Sharing and lineage

| Step | Command | What is verified |
|-----:|---------|------------------|
| 37 | `sharing list`, `lineage show` | Both return valid responses (may be empty on single project) |

### Phase 12.5: Kai (Keboola AI Assistant)

| Step | Command | What is verified |
|-----:|---------|------------------|
| 38 | `kai ping` | Server health, timestamp, MCP status. Gracefully skips all kai tests if `agent-chat` feature not enabled |
| 38 | `kai ask -m "..."` | One-shot question, verify response text + chat_id. Skips if auth fails (token type) |
| 38 | `kai history --limit 5` | At least 1 chat after asking |

Steps are wrapped in graceful skip logic — if Kai is not available (feature flag or auth), remaining kai tests are skipped without failing.

### Phase 13: Job commands

| Step | Command | What is verified |
|-----:|---------|------------------|
| 38 | `job list`, `job list --component-id`, `job detail` | List structure correct, component filter works. If jobs exist from uploads: detail returns full job data with status field |

### Phase 14: Cleanup via CLI

| Step | Command | What is verified |
|-----:|---------|------------------|
| 39 | `config delete` | Config removed, confirmed by config_id in response |
| 40 | `storage delete-table --dry-run`, `--yes`, `storage delete-bucket --dry-run`, `--yes` | Dry-run shows would_delete, actual delete confirmed |
| 41 | `project edit`, `project remove` | Edit preserves alias, remove confirmed, project gone from list |

---

## TestE2EErrorHandling

| Test | Command | Expected |
|------|---------|----------|
| `test_add_with_invalid_token` | `project add --token 000-invalid` | Exit code 3, `INVALID_TOKEN` |
| `test_status_of_nonexistent_project` | `project status --project nonexistent` | Exit code 5 |
| `test_remove_nonexistent_project` | `project remove --project nonexistent` | Exit code 5 |
| `test_config_detail_nonexistent` | `config detail --config-id 999999999` | Exit code != 0 |
| `test_download_nonexistent_table` | `download-table --table-id in.c-nonexistent.nonexistent` | Exit code != 0 |
| `test_delete_nonexistent_bucket` | `delete-bucket --bucket-id in.c-nonexistent-bucket-xyz` | Exit code != 0 |

---

## TestE2EJsonConsistency

| Test | What is verified |
|------|------------------|
| `test_all_read_commands_return_valid_json` | `project list`, `project status`, `config list`, `storage buckets`, `job list`, `component list`, `branch list`, `sharing list`, `lineage show`, `doctor`, `permissions list`, `permissions show` -- all return parseable JSON with `status` key |
| `test_token_never_appears_in_any_output` | Full API token never appears in output of `project list`, `project status`, `doctor` |

---

## TestE2ESyncWorkflow

Runs in a temporary git repository (`git init` + initial commit).

| Step | Command | What is verified |
|-----:|---------|------------------|
| 1 | `sync init --project ALIAS --directory DIR` | Returns project_alias in response |
| 2 | `sync pull --project ALIAS --directory DIR` | Exit code 0, files pulled |
| 3 | `sync status --directory DIR` | Exit code 0, returns status structure |
| 4 | `sync diff --project ALIAS --directory DIR` | Exit code 0, returns diff structure |
| 5 | `sync push --project ALIAS --directory DIR --dry-run` | Exit code 0, dry-run shows what would be pushed |

---

## TestE2EToolCommands

Skipped if `keboola-mcp-server` is not installed.

| Test | Command | What is verified |
|------|---------|------------------|
| `test_tool_list` | `tool list --project ALIAS` | Exit code 0, tools returned |
| `test_tool_call_get_buckets` | `tool call get_buckets --project ALIAS` | Exit code 0, bucket data returned |

---

## Commands NOT covered by E2E (with reasons)

| Command | Reason |
|---------|--------|
| `project refresh` | Requires Manage API token (`KBC_MANAGE_API_TOKEN`) |
| `org setup` | Requires Manage API token + destructive (registers projects in org) |
| `sharing share/unshare` | Requires org-level permissions or second project |
| `sharing link/unlink` | Requires shared bucket from another project |
| `permissions set/reset` | Interactive random-code confirmation blocks automated testing |
| `workspace from-transformation` | Requires existing transformation config with input mappings |
| `workspace query --file` | Equivalent to `--sql`; only the input source differs |
| `update` | Would actually update the installed package via PyPI |
| `repl` | Interactive REPL, not testable via CliRunner |
| `doctor --fix` | Installs MCP server binary; side effect not suitable for E2E |
| `init --from-global` | Requires global config with projects; tested via unit tests |
| `init --read-only` | Creates Claude Code permission rules; tested via unit tests |
