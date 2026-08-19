# MCP Removal from kbagent (v0.85.0) — Design

Date: 2026-08-18
Status: approved
Tracking: epic #390 phase 3 (removal promised for v0.85.0, "end of August 2026")
Closes as side effect: #478 (MCP tool classification fail-open — the classifier is deleted)

## 1. Scope

Three layers are removed together in one PR (five reviewable commits):

1. **Passthrough** — `tool list` / `tool call`, `McpService`, `McpServerManager`,
   `/mcp/*` REST routes, the parity map, the weekly parity canary.
2. **Server management** — installation and auto-update of `keboola-mcp-server`
   (`version_service`, `auto_update`, `doctor --fix`, `install.sh` marketing).
3. **Presentation** — SPA "MCP Tools" page, docs, plugin surfaces.

The `mcp>=1.0.0,<2.0.0` runtime dependency is dropped from `pyproject.toml`
(sole consumer: `services/mcp_service.py`).

The only surviving trace is a **tombstone** for the persisted
`agent --type mcp_tool` action so user data in `agents.json` survives the upgrade.

### Live vs. historical surfaces

Live documents are rewritten; historical records are left alone. Old
`changelog.py` entries, `docs/adr/0001-*`, `docs/superpowers/specs/*`, and
`docs/axi-mapping-report.md` are dated decision records — do not edit them.

## 2. Tombstone for `mcp_tool` tasks (the delicate part)

`agents_store.load_tasks()` skips entries that fail validation with only a
`logger.warning`, and `save_tasks()` rewrites the whole file from the loaded
list. Dropping `"mcp_tool"` from the `ActionType` literal would therefore
**silently delete the user's task from disk** on the next unrelated write.
So the literal keeps `"mcp_tool"` as a tombstone:

```python
ActionType = Literal["mcp_tool", "cli_command", "ai_agent"]  # mcp_tool = tombstone

#: Action types removed in 0.85.0. Kept in ``ActionType`` on purpose: load_tasks()
#: skips entries that fail validation and save_tasks() rewrites the file from the
#: loaded list, so dropping the literal would silently delete the user's task from
#: disk on the next unrelated write. Round-trip must survive; execution must not.
REMOVED_ACTION_TYPES: frozenset[str] = frozenset({"mcp_tool"})
REMOVED_ACTION_MESSAGE = (
    "agent action type 'mcp_tool' was REMOVED in kbagent v0.85.0 (epic #390). "
    "This task no longer runs. Recreate it with --type cli_command using the "
    "native kbagent command -- see docs/mcp-migration.md for the tool->command map."
)
```

Behavior per entry point:

| Path | Behavior |
|---|---|
| `scheduler_loop` (agent_runner.py) | `continue` **before** the `enabled` check — never dispatches, even with `enabled: true` on disk |
| `run_task_once` (manual/API run) | dispatch branch replaced with `raise` → run persists as `status="error"` with `REMOVED_ACTION_MESSAGE`. Must not silently no-op |
| `run_broadcaster.py` (UI-driven run) | same: the `_run_mcp_tool` fallback branch raises with the same message (fifth dispatch site — easy to miss) |
| `agent create/test --type mcp_tool` (CLI) | rejected at argument parsing, exit 2 |
| `POST /agents` with `mcp_tool` | 422 |
| `agent list` / `GET /agents` | keeps the additive `deprecation` key, reworded to "removed" |
| `kbagent doctor` | `mcp_tool_tasks` check flips WARN → **FAIL** ("these tasks no longer run"). `--fix` never touches `agents.json` — deleting user data is not an auto-fix |

`annotate_mcp_tool_deprecation` moves from the deleted `mcp_parity.py` into
`agents_store.py` (owner of the model), removing the mcp_parity import from
`commands/agent.py` and `server/routers/agents.py`.

## 3. Parity map becomes a migration document

`mcp_parity.py` is deleted as code, but its 37-tool → native-command table is
transplanted into a new `docs/mcp-migration.md` (including the `note` columns).
Referenced by: the 0.85.0 changelog entry, `gotchas.md`, and
`REMOVED_ACTION_MESSAGE`.

## 4. Commit breakdown (single PR)

One PR — a half-removed intermediate state is broken (SPA calls `/mcp/tools`,
`cli.py` wiring), and convention #17 requires doc sync in the same PR anyway.
Five separately reviewable commits:

| # | Commit | Content |
|---|---|---|
| 1 | `refactor(agent): tombstone the mcp_tool action type` | agents_store, agent_runner, **run_broadcaster**, commands/agent.py, routers/agents.py, doctor_service + tests |
| 2 | `feat!: remove the MCP passthrough` | delete mcp_service, mcp_transport, commands/tool.py, mcp_parity.py, server/routers/mcp.py, scripts/check_mcp_parity.py, scripts/benchmark.py, canary workflow; drop `install-mcp` + `parity-check` from Makefile; edit cli.py, server/dependencies.py, server/app.py, permissions.py, constants.py, output.py (tool table renderers), errors.py (MCP_ERROR), services/_auth_registration.py, scripts/check_sentinel_guards.py; drop `mcp` dep from pyproject.toml |
| 3 | `feat!: stop managing keboola-mcp-server` | version_service (~142 MCP lines), auto_update (~89), doctor check 5 + `ensure_mcp_installed`, install.sh, commands/version.py, server/routers/health.py `/version` payload |
| 4 | `feat(ui): drop the MCP Tools page` | web/frontend: Mcp.tsx (delete), Sidebar, App, Dashboard, state, types, Agents.tsx (drop the third action flavour from the form; keep rendering persisted mcp_tool tasks as removed/errored) |
| 5 | `docs: sync every MCP surface + bump 0.85.0` | README, CLAUDE.md, commands/context.py, plugins (SKILL.md, keboola-expert.md, commands-reference.md, gotchas.md, delete mcp-workflow.md, plugin CLAUDE.md), docs/guide.md, docs/TUTORIAL.md, docs/web-server.md, docs/build-your-own-client.md, docs/error-codes.md, new docs/mcp-migration.md, changelog entry, version bump + `make version-sync` |

## 5. Permissions details (commit 2)

Beyond `classify_mcp_tool`: remove `tool.list` / `tool.call` from
`OPERATION_REGISTRY`, the `tool:read|write|destructive` category patterns,
the `tool:*` glob branch, and the MCP prefix tuples. A **persisted** policy
containing `tool:write` patterns must keep loading: patterns are plain strings
matched against operations, and no operation starts with `tool:` anymore, so
stale patterns simply never match. Lock this in with a test. `permissions list`
drops its MCP tool section.

## 6. Sentinel-guard CI coupling (commit 2)

`make check-sentinel-guards` pairs every `require_static_token` guard with an
entry in `SESSION_UNSUPPORTED_FEATURES`. Removing the MCP guards requires
deleting `"kbagent tool (MCP server subprocess)"` from
`services/_auth_registration.py` AND the two `"MCP server subprocess"` aliases
in `scripts/check_sentinel_guards.py` `FEATURE_ALIASES`. Breaking one side
fails `make check`.

## 7. Auto-update cache (commit 3)

`_read_cache()` validates only `last_check` + `latest_version`; the old
`mcp_latest_version` / `mcp_install_method` keys are carried but ignored —
verified tolerant, no migration needed. Add a regression test: a cache file
written by ≤0.84.x (with MCP keys) loads cleanly in 0.85.0.

The changelog entry must hand Claude Desktop users the replacement command:
`uv tool install --upgrade --prerelease=allow keboola-mcp-server`.

## 8. DO NOT TOUCH list (hard rule for implementation agents)

- `kai_service.py` `mcp_status` + `commands/kai.py` MCP mentions — that is the
  **Kai server's** MCP connection on Keboola's side, not our passthrough.
- Provenance docstrings in ported code (transformation, flow, semantic-layer,
  component sync-action, json_utils, client/_transfer, data_science_client,
  workspace_service) — they document where the port came from; keep them.
- Historical records: old `changelog.py` entries, `docs/adr/`,
  `docs/superpowers/specs/`, `docs/axi-mapping-report.md`,
  `docs/issue-63-*`, `docs/programmatic-auth-login-plan.md`.
- `resources/flow/__init__.py` vendoring notes (flow examples/schema provenance).
- `web/*/package-lock.json` (coincidental substring matches).

## 9. Testing

Delete 5 MCP test files (~2,421 lines): test_mcp_service, test_mcp_transport,
test_mcp_parity_map, test_mcp_deprecation_warnings, test_mcp_tool_task_detection.

New tests:
- round-trip: `agents.json` with an `mcp_tool` task survives
  `load → upsert another task → save` without data loss;
- scheduler tick skips an `mcp_tool` task even with `enabled: true`;
- manual run persists `status="error"` carrying `REMOVED_ACTION_MESSAGE`;
- `agent create --type mcp_tool` → exit 2; `POST /agents` → 422;
- `doctor` FAILs when a tombstone task exists;
- auto-update cache with legacy MCP keys loads without error;
- persisted permission policy with `tool:*` patterns loads and never matches.

Revise: test_e2e.py, test_cli.py, test_permissions*.py, test_server_smoke.py,
test_doctor_service.py, test_agent_service.py, test_base_service.py,
test_output.py, test_auto_update.py, test_version_service.py, test_update_runner.py,
test_auth_sentinel*.py, tests/conftest.py (drop the autouse
`KBAGENT_MCP_TRANSPORT` fixture), tests/helpers.py.

## 10. Impact summary

- ~5,700 lines removed (2,282 production + 601 scripts + 170 frontend +
  2,421 tests + ~250 version/update).
- Runtime dependency `mcp` dropped → smaller wheel and PyInstaller binary.
- #478 closed by deletion; epic #390 phase 3 delivered on the promised
  version and date.
- Feature loss: Claude Desktop users lose MCP-server auto-update; changelog
  provides the manual replacement command.
